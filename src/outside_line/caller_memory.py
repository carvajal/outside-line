"""Per-caller markdown memory file.

One free-form markdown file per E.164 phone number under
``settings.caller_memory_dir`` (default ``data/memory/``). The file
captures stable facts about the caller — identity, location, family
relationships, communication style, recurring concerns, important
dates — so the agent can greet and treat them with continuity across
calls. Lives on the Railway persistent volume; gitignored under
``data/*``. PII risk surface is identical to ``data/callers.json``.

Loaded at call start (see :mod:`outside_line.twilio_handler` and
:mod:`outside_line.realtime_agent`) and optionally rewritten after the
call by :mod:`outside_line.memory_updater`. The CLI surface lives in
``scripts/callers.py`` (``memory show|edit|set|clear``).

Fail-open everywhere: a missing or unreadable file returns ``None``,
the call still goes through, the agent just doesn't have the context.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import settings
from .log import get_logger

log = get_logger(__name__)


def memory_path(phone: str) -> Path:
    """The on-disk path for ``phone``'s memory file.

    ``phone`` should already be in E.164 form (``+1...``); the call
    sites in this codebase route through ``normalize_e164`` first.
    """
    return settings.caller_memory_dir / f"{phone}.md"


def load_memory(phone: str) -> str | None:
    """Read the memory file for ``phone``. Returns ``None`` on miss.

    Treats every failure mode the same way: missing file, unreadable
    file, empty file, OS error — all return ``None`` so the call path
    is never broken by memory-store problems.
    """
    path = memory_path(phone)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning(
            "caller_memory.load_failed",
            phone=phone,
            path=str(path),
            error=repr(exc),
        )
        return None
    stripped = text.strip()
    return stripped or None


def save_memory(phone: str, text: str) -> None:
    """Atomically write the memory file for ``phone``.

    Mirrors :class:`outside_line.callers.CallersStore`'s
    tempfile-and-replace pattern so a crash mid-write can't corrupt the
    file. Always normalizes content to end with exactly one newline.
    """
    path = memory_path(phone)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = text.rstrip() + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    log.info("caller_memory.saved", phone=phone, path=str(path), bytes=len(content))


def delete_memory(phone: str) -> bool:
    """Delete the memory file for ``phone``. Returns True iff something was removed."""
    path = memory_path(phone)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning(
            "caller_memory.delete_failed",
            phone=phone,
            path=str(path),
            error=repr(exc),
        )
        return False
    log.info("caller_memory.deleted", phone=phone, path=str(path))
    return True
