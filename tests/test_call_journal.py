"""Unit tests for ``summarize_lifecycle`` — the pure lifecycle reconstruction.

Coverage is deliberately narrow — turning the flat, append-ordered event stream into ordered segments with
derived durations (across the AGENT→RING→BRIDGE→AGENT loop, the directline
path, and the no-transcript blocked/abnormal cases) is the self-contained,
non-obvious bit. The writer/reader are thin disk I/O validated by a real call.
"""

from __future__ import annotations

from datetime import UTC, datetime

from outside_line.call_journal import summarize_lifecycle

_BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC).timestamp()


def _row(offset: float, event: str, **fields: object) -> dict[str, object]:
    ts = datetime.fromtimestamp(_BASE + offset, UTC).isoformat(timespec="milliseconds")
    return {"ts": ts, "call_sid": "CAtest", "event": event, **fields}


def test_agent_only() -> None:
    events = [
        _row(0, "received", **{"from": "+1305", "to": "+1305", "line": "mainline"}),
        _row(0, "agent_started"),
        _row(180, "ended"),
    ]
    s = summarize_lifecycle(events, total_s=180)
    assert s.line == "mainline"
    assert s.blocked is False
    assert s.clean_end is True
    assert s.talked_to_agent is True
    assert s.agent_s == 180
    assert len(s.segments) == 1 and s.segments[0].kind == "agent"
    assert s.bridged_contacts == []


def test_agent_then_bridge_then_agent_loop() -> None:
    events = [
        _row(0, "received", **{"from": "+1305", "line": "mainline"}),
        _row(0, "agent_started"),
        _row(60, "contact_call", name="Nia", kind="agent"),
        _row(70, "bridge_started", name="Nia"),
        _row(190, "bridge_ended", name="Nia", reason="bridge_ended"),
        _row(191, "agent_started"),
        _row(200, "ended"),
    ]
    s = summarize_lifecycle(events, total_s=200)
    kinds = [(seg.kind, seg.name, seg.outcome, seg.seconds) for seg in s.segments]
    assert kinds == [
        ("agent", None, None, 60),
        ("contact", "Nia", "bridged", 120),
        ("agent", None, None, 9),
    ]
    assert s.agent_s == 69
    assert len(s.bridged_contacts) == 1
    assert s.bridged_contacts[0].name == "Nia"
    assert s.bridged_contacts[0].via == "agent"


def test_agent_then_contact_no_answer() -> None:
    events = [
        _row(0, "received", **{"from": "+1305", "line": "mainline"}),
        _row(0, "agent_started"),
        _row(30, "contact_call", name="Damián", kind="agent"),
        _row(95, "contact_no_answer", name="Damián"),
        _row(96, "agent_started"),
        _row(120, "ended"),
    ]
    s = summarize_lifecycle(events, total_s=120)
    assert s.agent_s == 54
    assert len(s.no_answer_contacts) == 1
    assert s.no_answer_contacts[0].name == "Damián"
    assert s.bridged_contacts == []


def test_directline_bridged_no_agent() -> None:
    events = [
        _row(
            0, "received", line="directline", line_label="Damian", **{"from": "+1555"}
        ),
        _row(0, "contact_call", name="Damián", kind="directline"),
        _row(5, "bridge_started", name="Damián"),
        _row(300, "bridge_ended", name="Damián", reason="ws_closed"),
        _row(300, "ended"),
    ]
    s = summarize_lifecycle(events, total_s=305)
    assert s.line == "directline"
    assert s.line_label == "Damian"
    assert s.talked_to_agent is False
    assert len(s.bridged_contacts) == 1
    assert s.bridged_contacts[0].seconds == 295
    assert s.bridged_contacts[0].via == "directline"


def test_blocked_call() -> None:
    events = [
        _row(0, "received", **{"from": "+1999", "to": "+1305", "line": "mainline"}),
        _row(0, "blocked"),
    ]
    s = summarize_lifecycle(events, total_s=0)
    assert s.blocked is True
    assert s.talked_to_agent is False
    assert s.segments == []


def test_abnormal_end_uses_call_duration() -> None:
    # No `ended` event (process died / caller dropped) — the final AGENT span is
    # bounded by Twilio's CallDuration, and clean_end stays False.
    events = [
        _row(0, "received", **{"from": "+1305", "line": "mainline"}),
        _row(0, "agent_started"),
    ]
    s = summarize_lifecycle(events, total_s=45)
    assert s.clean_end is False
    assert s.agent_s == 45
    assert s.total_s == 45
