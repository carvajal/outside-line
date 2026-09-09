"""Per-phone-number caller registry — call counts + allow / skip-gate flags.

Backs four side features that wrap the Twilio voice webhook:

  1. Caller registry — every inbound call is recorded by E.164 phone
     number (first_seen, last_seen, call_count, optional human label).
  2. Allow list — numbers flagged ``allowed=False`` (the default for
     unknown numbers on their first call) get rejected at the voice
     webhook with a busy signal; the WS handler never runs.
  3. First-time flag — :meth:`CallersStore.record_call` returns
     whether this was the first call from this number so the
     call-processed email can flag new callers (see
     :mod:`outside_line.call_report`).
  4. Gate-skip list — numbers flagged ``skip_gate=True`` AND
     ``allowed=True`` skip the DTMF gate (in the voice webhook).
     Entries persisted before the rename may still carry the legacy
     ``bypass`` key; :func:`skips_gate_entry` reads both, and every
     write converges the file on ``skip_gate``.

Store is a single JSON file at ``settings.callers_path`` (default
``data/callers.json``), keyed by E.164. Writes go through a temp file
+ ``os.replace`` so a crash mid-write can't corrupt the store. Reads
fail open: a missing or unreadable file is treated as an empty store
so the call path keeps working. Entries from older versions of the
store are silently upgraded on read via ``dict.get(..., default)`` —
existing numbers default to ``allowed=True``, new numbers to ``False``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .log import get_logger

log = get_logger(__name__)

# Internal entry shape. We don't bother with a dataclass — the entries
# round-trip through JSON, and a few extra keys appearing on disk (from
# a future migration) should be tolerated, not stripped.
Entry = dict[str, object]

# E.164: leading +, then 8 to 15 digits (ITU spec).
_E164_RE = re.compile(r"^\+\d{8,15}$")
# Stripped before normalization. Covers the shapes a human pastes
# from a phone's call log.
_PHONE_NOISE_RE = re.compile(r"[ \-()\.]+")


def normalize_e164(raw: str | None, default_country: str = "1") -> str | None:
    """Normalize a phone string to E.164. Returns ``None`` on garbage.

    Twilio always sends ``From`` already in E.164 (``+1...``) so the
    handler path is effectively idempotent. The normalizer earns its
    keep in the CLI, where the operator pastes ``(555) 123-0100`` or
    ``555-123-0100`` and expects it to just work.
    """
    if not raw:
        return None
    s = _PHONE_NOISE_RE.sub("", raw.strip())
    if not s:
        return None
    if s.startswith("+"):
        return s if _E164_RE.match(s) else None
    if not s.isdigit():
        return None
    candidate = f"+{default_country}{s}" if len(s) == 10 else f"+{s}"
    return candidate if _E164_RE.match(candidate) else None


def _now_iso() -> str:
    # Millisecond precision, Z-suffixed UTC. Matches the shape used in
    # transcripts/* so logs line up across stores.
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class CallersStore:
    """Atomic JSON-file store keyed by E.164 phone number."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, Entry]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "callers.load_failed",
                error=repr(exc),
                path=str(self._path),
            )
            return {}
        if not isinstance(data, dict):
            log.warning(
                "callers.load_unexpected_shape",
                path=str(self._path),
                got_type=type(data).__name__,
            )
            return {}
        return data

    def _save_atomic(self, data: dict[str, Entry]) -> None:
        # Converge persisted entries on the current key: any legacy
        # ``bypass`` value migrates to ``skip_gate`` (unless skip_gate
        # is already present) and the legacy key is dropped. The live
        # store predates the rename, so entries migrate on next touch.
        for entry in data.values():
            if "bypass" in entry:
                legacy = entry.pop("bypass")
                entry.setdefault("skip_gate", legacy)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._path)

    # -- handler-facing ----------------------------------------------------

    def record_call(
        self, phone: str, call_sid: str | None = None
    ) -> tuple[Entry, bool]:
        """Upsert an entry for ``phone`` and return ``(entry, was_first_time)``.

        Always bumps ``call_count`` and ``last_seen``. On insert, sets
        ``first_seen`` to now, ``allowed=False`` (unknown numbers are
        blocked until the operator reviews them), ``skip_gate=False``,
        ``label=None``. The ``call_sid`` is logged but not stored on
        the entry — per-call history lives in the transcripts / archive
        surface.
        """
        now = _now_iso()
        data = self._load()
        existing = data.get(phone)
        was_first_time = existing is None
        if existing is None:
            entry: Entry = {
                "label": None,
                "allowed": False,
                "skip_gate": False,
                "first_seen": now,
                "last_seen": now,
                "call_count": 1,
            }
        else:
            entry = dict(existing)
            entry["last_seen"] = now
            entry["call_count"] = int(entry.get("call_count", 0) or 0) + 1
        data[phone] = entry
        try:
            self._save_atomic(data)
        except OSError as exc:
            log.warning(
                "callers.save_failed",
                error=repr(exc),
                path=str(self._path),
                phone=phone,
            )
        log.info(
            "callers.recorded",
            phone=phone,
            call_sid=call_sid,
            first_time=was_first_time,
            call_count=entry["call_count"],
            allowed=is_allowed_entry(entry),
            skip_gate=skips_gate_entry(entry),
        )
        return entry, was_first_time

    def is_allowed(self, phone: str) -> bool:
        """True iff there's an entry for ``phone`` with ``allowed=True``.

        Unknown numbers (no entry) are NOT allowed — the call path
        treats them as blocked and records them via ``record_call``.
        """
        entry = self._load().get(phone)
        return bool(entry) and is_allowed_entry(entry)

    def skips_gate(self, phone: str) -> bool:
        entry = self._load().get(phone)
        return skips_gate_entry(entry)

    # -- CLI-facing --------------------------------------------------------

    def get(self, phone: str) -> Entry | None:
        return self._load().get(phone)

    def list_all(self) -> list[tuple[str, Entry]]:
        """Return ``[(phone, entry), ...]`` sorted by ``last_seen`` desc."""
        data = self._load()
        return sorted(
            data.items(),
            key=lambda kv: str(kv[1].get("last_seen", "")),
            reverse=True,
        )

    def add(self, phone: str, label: str | None = None) -> Entry:
        """Create an entry for ``phone`` without recording a call.

        Used by the CLI so the operator can pre-register a number
        before its first call. The entry is created ``allowed=True``
        — the CLI is the operator's voice, so anything added this way
        is implicitly trusted (unlike unknown numbers arriving via
        ``record_call``, which start blocked). If the entry already
        exists, only ``label`` is updated when given — ``call_count``,
        timestamps, ``allowed``, ``skip_gate`` are left alone.
        """
        data = self._load()
        existing = data.get(phone)
        if existing is None:
            now = _now_iso()
            entry: Entry = {
                "label": label,
                "allowed": True,
                "skip_gate": False,
                "first_seen": now,
                "last_seen": now,
                "call_count": 0,
            }
        else:
            entry = dict(existing)
            if label is not None:
                entry["label"] = label
        data[phone] = entry
        self._save_atomic(data)
        return entry

    def set_label(self, phone: str, label: str | None) -> Entry:
        data = self._load()
        entry = dict(data.get(phone) or self._blank_entry())
        entry["label"] = label
        data[phone] = entry
        self._save_atomic(data)
        return entry

    def set_allowed(self, phone: str, on: bool) -> Entry:
        data = self._load()
        entry = dict(data.get(phone) or self._blank_entry())
        entry["allowed"] = bool(on)
        data[phone] = entry
        self._save_atomic(data)
        return entry

    def set_skip_gate(self, phone: str, on: bool) -> Entry:
        data = self._load()
        entry = dict(data.get(phone) or self._blank_entry())
        entry["skip_gate"] = bool(on)
        data[phone] = entry
        self._save_atomic(data)
        return entry

    def forget(self, phone: str) -> bool:
        data = self._load()
        if phone not in data:
            return False
        del data[phone]
        self._save_atomic(data)
        return True

    @staticmethod
    def _blank_entry() -> Entry:
        # CLI-initiated mutations on previously-unseen numbers default
        # to allowed=True — anything the operator touches by hand is
        # implicitly trusted. Only record_call's first-time insert
        # blocks by default. (See add() docstring for the same logic.)
        now = _now_iso()
        return {
            "label": None,
            "allowed": True,
            "skip_gate": False,
            "first_seen": now,
            "last_seen": now,
            "call_count": 0,
        }


def skips_gate_entry(entry: Entry | None) -> bool:
    """Read the gate-skip flag with legacy-key fallback.

    Entries written before the ``bypass`` → ``skip_gate`` rename may
    still carry the old key on disk (the live store is a persisted
    volume); reads honor it until a write migrates the entry.
    """
    if not entry:
        return False
    return bool(entry.get("skip_gate", entry.get("bypass", False)))


def is_allowed_entry(entry: Entry) -> bool:
    """Read the ``allowed`` field with the right backfill semantics.

    Pre-existing entries in ``data/callers.json`` from before the
    allowed field was introduced are treated as allowed — at the time
    they were recorded, every caller was implicitly trusted. New
    entries written by ``record_call`` carry an explicit ``False``.
    """
    return bool(entry.get("allowed", True))


# -- module-level singleton ---------------------------------------------

_default_store: CallersStore | None = None


def default_store() -> CallersStore:
    """The store used by the live request path. Path comes from settings."""
    global _default_store
    if _default_store is None:
        _default_store = CallersStore(settings.callers_path)
    return _default_store
