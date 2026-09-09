"""Append-only per-call transcript writer.

One JSONL file per call at ``data/transcripts/<call_sid>.jsonl``. Each line
is a single event — caller turn, agent turn, tool call, or system note from
the voice-gate decision. Open-append-close per line so a crash mid-call
leaves a valid JSONL prefix on disk.

Transcripts are sourced from OpenAI Realtime events we already pay for via
``audio.input.transcription`` in the session config — no extra API
call, no Twilio Voice Intelligence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .log import get_logger

log = get_logger(__name__)

Role = Literal["caller", "agent", "tool", "system"]

DEFAULT_DIR = Path("data/transcripts")


class TranscriptWriter:
    """Append JSONL rows for a single call.

    The output file is created lazily on first ``append`` so calls that
    never produce a transcribed turn don't leave an empty file behind.
    """

    def __init__(self, call_sid: str, base_dir: Path = DEFAULT_DIR) -> None:
        self.call_sid = call_sid
        self.path = base_dir / f"{call_sid}.jsonl"
        self._initialized = False

    def append(self, role: Role, text: str, **meta: Any) -> None:
        try:
            if not self._initialized:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._initialized = True
            row: dict[str, Any] = {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "call_sid": self.call_sid,
                "role": role,
                "text": text,
            }
            # Drop None meta to keep lines tight; preserve everything else
            # (including falsy ints/strings the caller passed explicitly).
            for k, v in meta.items():
                if v is not None:
                    row[k] = v
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            # Transcript writes are observability, not correctness — a
            # disk full / permission error must never break the call.
            log.warning("transcripts.append.failed", call_sid=self.call_sid, role=role)
