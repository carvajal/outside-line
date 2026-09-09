"""Directline registry — dedicated Twilio DIDs that route straight to a
Telegram contact, with the agent fully out of the call path on the happy path.

A directline is a 1:1 mapping ``twilio_did → telegram_username``. When
an inbound call's ``To`` matches an entry here, the voice handler:

  1. Spawns a background task that calls
     :meth:`TelegramBridge.place_call` immediately (so the contact's
     phone starts ringing while Twilio is still rendering the DTMF
     gate's silent ``<Pause>`` blocks).
  2. Returns the usual gate TwiML with an extra
     ``<Parameter name="directline_call_sid" ...>`` so the Media Stream
     WS handler can re-join the in-memory :class:`DirectlineSession`
     when it accepts the stream.

The session registry is process-local (in-memory dict keyed by call_sid).
A 70 s orphan-cleanup task is spawned alongside each session so a caller
who hangs up during the gate window doesn't leave a dangling Telegram
ring task. See ``docs/decisions/0013-directline-feature.md`` for the
race-model rationale.

Persistence shape (``data/directline_numbers.json``)::

    {
      "+15551230188": {
        "telegram_username": "sam_handle",
        "label": "Sam direct line",
        "enabled": true
      },
      ...
    }

The leading ``@`` on the username is stripped on read so the on-disk
shape mirrors how operators type it.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .callers import normalize_e164
from .config import settings
from .contacts import Contact
from .log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class DirectlineEntry:
    """One row from ``data/directline_numbers.json``."""

    twilio_did: str        # E.164 (normalized on load)
    telegram_username: str  # without the leading "@"
    label: str | None = None
    enabled: bool = True


@dataclass
class DirectlineSession:
    """Per-call in-memory state. Created in ``voice()``, joined in ``media()``.

    The ``tg_task`` resolves to the answering contact's user_id (int) on
    pickup, or ``None`` on timeout / error. ``gate_done_event`` is set by
    the WS handler when the Media Stream's ``start`` event arrives —
    that's the signal the gate completed and the caller-side audio path
    is open. ``user_id`` and ``tg_answered_ns`` are written by the ring
    task; readers may see them as None until the task resolves.
    """

    call_sid: str
    contact: Contact
    label: str | None = None
    tg_task: asyncio.Task[int | None] | None = None
    user_id: int | None = None
    tg_answered_ns: int | None = None
    gate_done_event: asyncio.Event = field(default_factory=asyncio.Event)


# -- store ----------------------------------------------------------------


class DirectlinesStore:
    """Atomic JSON-file store keyed by E.164 DID. Mirrors ``CallersStore``."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "directlines.load_failed",
                error=repr(exc),
                path=str(self._path),
            )
            return {}
        if not isinstance(data, dict):
            log.warning(
                "directlines.load_unexpected_shape",
                path=str(self._path),
                got_type=type(data).__name__,
            )
            return {}
        return data

    def _save_atomic(self, data: dict[str, dict[str, object]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    # -- handler-facing -----------------------------------------------------

    def lookup(self, twilio_did: str) -> DirectlineEntry | None:
        """Return the entry for ``twilio_did`` (E.164), or None.

        Disabled entries (``enabled=False``) return None — the call falls
        through to the default the agent path. Garbage on disk falls open
        (warning logged in ``_load``); a malformed entry returns None.
        """
        normalized = normalize_e164(twilio_did)
        if normalized is None:
            return None
        raw = self._load().get(normalized)
        if not isinstance(raw, dict):
            return None
        handle = raw.get("telegram_username")
        if not isinstance(handle, str) or not handle:
            log.warning(
                "directlines.entry_skipped",
                did=normalized,
                reason="missing telegram_username",
            )
            return None
        enabled = bool(raw.get("enabled", True))
        if not enabled:
            return None
        label = raw.get("label")
        return DirectlineEntry(
            twilio_did=normalized,
            telegram_username=handle.lstrip("@"),
            label=label if isinstance(label, str) and label else None,
            enabled=True,
        )

    # -- CLI-facing ---------------------------------------------------------

    def list_all(self) -> list[DirectlineEntry]:
        """Return all entries sorted by DID (stable for operator review)."""
        out: list[DirectlineEntry] = []
        for did, raw in self._load().items():
            if not isinstance(raw, dict):
                continue
            handle = raw.get("telegram_username")
            if not isinstance(handle, str) or not handle:
                continue
            label = raw.get("label")
            out.append(
                DirectlineEntry(
                    twilio_did=did,
                    telegram_username=handle.lstrip("@"),
                    label=label if isinstance(label, str) and label else None,
                    enabled=bool(raw.get("enabled", True)),
                )
            )
        out.sort(key=lambda e: e.twilio_did)
        return out

    def get(self, twilio_did: str) -> DirectlineEntry | None:
        """CLI-facing lookup that ignores the ``enabled`` flag (so the
        operator can inspect disabled rows). ``lookup`` is what the call
        path uses."""
        normalized = normalize_e164(twilio_did)
        if normalized is None:
            return None
        raw = self._load().get(normalized)
        if not isinstance(raw, dict):
            return None
        handle = raw.get("telegram_username")
        if not isinstance(handle, str) or not handle:
            return None
        label = raw.get("label")
        return DirectlineEntry(
            twilio_did=normalized,
            telegram_username=handle.lstrip("@"),
            label=label if isinstance(label, str) and label else None,
            enabled=bool(raw.get("enabled", True)),
        )

    def upsert(
        self,
        twilio_did: str,
        telegram_username: str,
        *,
        label: str | None = None,
        enabled: bool | None = None,
    ) -> DirectlineEntry:
        """Create or update an entry. Returns the resulting entry."""
        normalized = normalize_e164(twilio_did)
        if normalized is None:
            raise ValueError(f"not a valid phone number: {twilio_did!r}")
        handle = telegram_username.lstrip("@").strip()
        if not handle:
            raise ValueError("telegram_username is required")
        data = self._load()
        existing = data.get(normalized) if isinstance(data.get(normalized), dict) else None
        merged: dict[str, object] = dict(existing or {})
        merged["telegram_username"] = handle
        if label is not None:
            merged["label"] = label or None
        if enabled is not None:
            merged["enabled"] = bool(enabled)
        merged.setdefault("enabled", True)
        data[normalized] = merged
        self._save_atomic(data)
        return DirectlineEntry(
            twilio_did=normalized,
            telegram_username=handle,
            label=merged.get("label") if isinstance(merged.get("label"), str) else None,  # type: ignore[arg-type]
            enabled=bool(merged.get("enabled", True)),
        )

    def set_enabled(self, twilio_did: str, on: bool) -> DirectlineEntry | None:
        normalized = normalize_e164(twilio_did)
        if normalized is None:
            return None
        data = self._load()
        existing = data.get(normalized)
        if not isinstance(existing, dict):
            return None
        merged = dict(existing)
        merged["enabled"] = bool(on)
        data[normalized] = merged
        self._save_atomic(data)
        return self.get(normalized)

    def remove(self, twilio_did: str) -> bool:
        normalized = normalize_e164(twilio_did)
        if normalized is None:
            return False
        data = self._load()
        if normalized not in data:
            return False
        del data[normalized]
        self._save_atomic(data)
        return True


# -- module-level singleton ------------------------------------------------

_default_store: DirectlinesStore | None = None


def default_store() -> DirectlinesStore:
    """The store used by the live request path. Path comes from settings."""
    global _default_store
    if _default_store is None:
        _default_store = DirectlinesStore(settings.directlines_path)
    return _default_store


def lookup(twilio_did: str) -> DirectlineEntry | None:
    """Convenience wrapper over :meth:`DirectlinesStore.lookup`."""
    return default_store().lookup(twilio_did)


# -- in-memory per-call session registry -----------------------------------
#
# Hands off state between the voice() POST handler (which spawns the
# ring task) and the media() WS handler (which joins it ~40 s later when
# the DTMF gate completes). Process-local; we don't persist the session
# anywhere because both endpoints run in the same FastAPI worker.

_SESSION_REGISTRY: dict[str, DirectlineSession] = {}


def register_session(session: DirectlineSession) -> None:
    """Register a session for later pickup by ``pop_session``."""
    _SESSION_REGISTRY[session.call_sid] = session


def pop_session(call_sid: str) -> DirectlineSession | None:
    """Remove and return the session for ``call_sid``, or None."""
    return _SESSION_REGISTRY.pop(call_sid, None)


def peek_session(call_sid: str) -> DirectlineSession | None:
    """Return the session for ``call_sid`` without removing it."""
    return _SESSION_REGISTRY.get(call_sid)


def _registry_snapshot() -> dict[str, DirectlineSession]:
    """For tests / debug only — returns the live registry dict (not a copy)."""
    return _SESSION_REGISTRY
