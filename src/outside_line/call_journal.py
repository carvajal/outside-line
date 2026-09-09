"""Append-only per-call lifecycle journal.

One JSONL file per call at ``data/calls/<call_sid>.jsonl``. Each line is a
single lifecycle *event* — call received, blocked, the agent turn started, a
contact was rung, a bridge opened/closed, the call ended. This is the durable
record the post-call "call-processed" report is built from
(:mod:`outside_line.call_report`), sourced entirely from disk so the report can be
reconstructed even if the process died mid-call — Twilio re-delivers the
terminal ``/twilio/voice/status`` callback to the restarted process, and the
crash-safe JSONL prefix is still on the volume.

Modeled on :class:`outside_line.transcripts.TranscriptWriter`: open-append-close
per row so a crash mid-call leaves a valid JSONL prefix, and every write is
fail-open — a disk error is observability lost, never a broken call.

The transcript (``data/transcripts/``) captures *what was said*; this journal
captures *what happened structurally* — including the no-transcript cases
(blocked calls, gate hang-ups, pure directline bridges) that never produce a
conversational turn.

Events (row shape ``{"ts", "call_sid", "event", **fields}``):

* ``received``   — ``from``, ``to``, ``line`` ("mainline"|"directline"),
  ``line_label``, ``first_time``, ``label``, ``call_count``
* ``blocked``    — caller was not allowed; call rejected busy (no WS)
* ``agent_started`` — a AGENT phase began (caller is talking to the agent)
* ``contact_call`` — ``name``, ``kind`` ("agent"|"directline"): a contact is
  being rung (the agent's ``call_contact`` tool, or a directline DID)
* ``contact_no_answer`` — ``name``: the ring timed out / errored
* ``bridge_started`` — ``name``: caller↔contact audio bridge opened
* ``bridge_ended``   — ``name``, ``reason``: bridge closed (reason is the
  phase exit reason, e.g. ``bridge_ended``/``ws_closed``/``sidecar_crashed``)
* ``ended``      — clean in-process teardown ran (absence ⇒ likely abnormal end)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger(__name__)

DEFAULT_DIR = Path("data/calls")


class CallJournal:
    """Append lifecycle-event rows for a single call.

    The file is created lazily on the first ``append`` so a call that never
    gets past ``voice()`` doesn't leave an empty file behind.
    """

    def __init__(self, call_sid: str, base_dir: Path = DEFAULT_DIR) -> None:
        self.call_sid = call_sid
        self.path = base_dir / f"{call_sid}.jsonl"
        self._initialized = False

    def append(self, event: str, **fields: Any) -> None:
        try:
            if not self._initialized:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._initialized = True
            row: dict[str, Any] = {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "call_sid": self.call_sid,
                "event": event,
            }
            # Drop None fields to keep rows tight; preserve falsy 0/""/False
            # the caller passed on purpose.
            for k, v in fields.items():
                if v is not None:
                    row[k] = v
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            # ``event`` is structlog's reserved positional; passing it as a
            # kwarg raises TypeError *inside* this handler and defeats the
            # fail-open contract, so it ships namespaced.
            log.warning(
                "call_journal.append.failed",
                call_sid=self.call_sid,
                journal_event=event,
            )


def load_journal(call_sid: str, base_dir: Path = DEFAULT_DIR) -> list[dict[str, Any]]:
    """Read a call's journal rows in file order. Missing file ⇒ ``[]``.

    Malformed lines are skipped (a crash can truncate the last line
    mid-write); every valid prefix row is returned.
    """
    path = base_dir / f"{call_sid}.jsonl"
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        return []
    return rows


# ---------------------------------------------------------------------------
# Lifecycle reconstruction (pure — the unit-tested piece).


@dataclass
class Segment:
    """One leg of the call's timeline.

    ``kind`` is ``"agent"`` (caller talking to the agent) or ``"contact"``
    (a Telegram contact was rung). For ``contact``: ``outcome`` is
    ``"bridged"`` (talked, ``seconds`` is the bridge duration) or
    ``"no_answer"`` (``seconds`` is 0); ``via`` is how the contact was
    reached (``"agent"`` = the agent's ``call_contact``, ``"directline"`` = a
    dedicated DID).
    """

    kind: str
    seconds: int = 0
    name: str | None = None
    outcome: str | None = None
    via: str | None = None


@dataclass
class CallSummary:
    """Deterministic reconstruction of a call from its journal.

    Presentation (subject line, HTML/text body) lives in
    :mod:`outside_line.call_report`; this is data only.
    """

    from_number: str | None = None
    to_number: str | None = None
    line: str = "mainline"
    line_label: str | None = None
    label: str | None = None
    first_time: bool = False
    call_count: int | None = None
    blocked: bool = False
    clean_end: bool = False
    total_s: int = 0
    segments: list[Segment] = field(default_factory=list)

    @property
    def agent_s(self) -> int:
        return sum(s.seconds for s in self.segments if s.kind == "agent")

    @property
    def talked_to_agent(self) -> bool:
        return self.agent_s > 0

    @property
    def bridged_contacts(self) -> list[Segment]:
        return [
            s for s in self.segments if s.kind == "contact" and s.outcome == "bridged"
        ]

    @property
    def no_answer_contacts(self) -> list[Segment]:
        return [
            s for s in self.segments if s.kind == "contact" and s.outcome == "no_answer"
        ]


def _epoch(ts: Any) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def summarize_lifecycle(
    events: list[dict[str, Any]], total_s: int | None = None
) -> CallSummary:
    """Walk the ordered event stream into a :class:`CallSummary`.

    Durations are DERIVED from segment-start timestamps (a AGENT span runs
    until the next contact ring / bridge / call-end; a bridge span runs from
    ``bridge_started`` to ``bridge_ended`` or call-end). This means the exact
    ``bridge_ended``/``ended`` events are non-load-bearing for timing — the
    crafter can race the in-process teardown and still get correct numbers.

    ``total_s`` (Twilio ``CallDuration``, if known) bounds the final open
    span and sets ``CallSummary.total_s``; otherwise the last event's
    timestamp is used. The AGENT→RING→BRIDGE→AGENT loop falls out of the
    append-ordered stream naturally (multiple ``agent`` segments).
    """
    summary = CallSummary()
    if not events:
        return summary

    first_ts = _epoch(events[0].get("ts"))
    last_ts = _epoch(events[-1].get("ts"))
    start_ts = first_ts
    if total_s is not None and start_ts is not None:
        end_ts: float | None = start_ts + total_s
    else:
        end_ts = last_ts

    def dur(a: float | None, b: float | None) -> int:
        if a is None or b is None:
            return 0
        return max(0, round(b - a))

    agent_open: float | None = None
    bridge_open: tuple[str | None, str | None, float | None] | None = (
        None  # name, via, ts
    )
    pending_via: dict[str | None, str | None] = {}  # name -> via, from contact_call

    def close_agent(until: float | None) -> None:
        nonlocal agent_open
        if agent_open is not None:
            summary.segments.append(Segment("agent", dur(agent_open, until)))
            agent_open = None

    for row in events:
        event = row.get("event")
        t = _epoch(row.get("ts"))

        if event == "received":
            summary.from_number = row.get("from")
            summary.to_number = row.get("to")
            summary.line = row.get("line") or "mainline"
            summary.line_label = row.get("line_label")
            summary.label = row.get("label")
            summary.first_time = bool(row.get("first_time"))
            cc = row.get("call_count")
            summary.call_count = (
                int(cc) if isinstance(cc, (int, str)) and str(cc).isdigit() else None
            )
        elif event == "blocked":
            summary.blocked = True
        elif event == "agent_started":
            agent_open = t
        elif event == "contact_call":
            close_agent(t)  # the AGENT span ends when we start ringing
            pending_via[row.get("name")] = row.get("kind")
        elif event == "contact_no_answer":
            name = row.get("name")
            summary.segments.append(
                Segment(
                    "contact",
                    0,
                    name=name,
                    outcome="no_answer",
                    via=pending_via.get(name),
                )
            )
        elif event == "bridge_started":
            close_agent(t)  # defensive — directline has no preceding AGENT span
            bridge_open = (row.get("name"), pending_via.get(row.get("name")), t)
        elif event == "bridge_ended":
            if bridge_open is not None:
                name, via, bstart = bridge_open
                summary.segments.append(
                    Segment(
                        "contact", dur(bstart, t), name=name, outcome="bridged", via=via
                    )
                )
                bridge_open = None
        elif event == "ended":
            summary.clean_end = True

    # Close any span still open at call-end (the caller hung up mid-span).
    if bridge_open is not None:
        name, via, bstart = bridge_open
        summary.segments.append(
            Segment(
                "contact", dur(bstart, end_ts), name=name, outcome="bridged", via=via
            )
        )
    close_agent(end_ts)

    if total_s is not None:
        summary.total_s = total_s
    else:
        summary.total_s = dur(start_ts, end_ts)
    return summary
