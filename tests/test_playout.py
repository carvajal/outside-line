"""PlayoutTracker unit tests — the mark/clock floor-release logic (ADR 0020).

Deterministic: a fake monotonic clock is injected, and the mark "wire" is a
recording stub. Async entry points are drained with ``asyncio.run`` (the
suite has no global asyncio_mode).
"""

from __future__ import annotations

import asyncio

from outside_line.playout import ULAW_BYTES_PER_S, PlayoutTracker


class _Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _tracker(
    grace_s: float = 1.5, *, failing_send: bool = False
) -> tuple[PlayoutTracker, _Clock, list[str]]:
    clock = _Clock()
    sent: list[str] = []

    async def send_mark(name: str) -> None:
        if failing_send:
            raise RuntimeError("ws down")
        sent.append(name)

    return (
        PlayoutTracker(send_mark=send_mark, grace_s=grace_s, clock=clock),
        clock,
        sent,
    )


def test_deadline_accumulates_and_reanchors() -> None:
    tr, clock, _ = _tracker()
    # 1 s of audio queued while the buffer is empty: drains at now+1.
    tr.note_sent(ULAW_BYTES_PER_S)
    assert tr._deadline == clock.now + 1.0
    # Another 0.5 s while still draining: extends, doesn't re-anchor.
    tr.note_sent(ULAW_BYTES_PER_S // 2)
    assert tr._deadline == clock.now + 1.5
    # Long idle past the deadline: next chunk re-anchors at *now*.
    clock.now += 60.0
    tr.note_sent(ULAW_BYTES_PER_S // 10)
    assert tr._deadline == clock.now + 0.1


def test_zero_audio_response_arms_nothing() -> None:
    tr, _, sent = _tracker()
    asyncio.run(tr.response_ended("resp_A"))
    assert sent == []
    assert not tr.is_draining()


def test_echo_releases_the_floor() -> None:
    tr, clock, sent = _tracker()
    tr.note_sent(ULAW_BYTES_PER_S * 20)  # a 20 s answer
    asyncio.run(tr.response_ended("resp_A"))
    assert len(sent) == 1 and "resp_A" in sent[0]
    # response.done fired, but playback is still draining: floor held.
    clock.now += 5.0
    assert tr.is_draining()
    # Twilio's echo is the release — even well before the clock deadline.
    tr.mark_echoed(sent[0])
    assert not tr.is_draining()


def test_grace_expiry_backstop_and_late_echo_idempotency() -> None:
    tr, clock, sent = _tracker(grace_s=1.5)
    tr.note_sent(ULAW_BYTES_PER_S * 2)
    asyncio.run(tr.response_ended("resp_A"))
    # Just past the predicted drain: still inside grace, floor held.
    clock.now += 2.5
    assert tr.is_draining()
    # Past deadline + grace: echo is provably lost, backstop opens.
    clock.now += 1.5
    assert not tr.is_draining()
    # The straggler echo arrives many seconds later: pure no-op.
    clock.now += 10.0
    tr.mark_echoed(sent[0])
    tr.mark_echoed(sent[0])  # and a duplicate
    tr.mark_echoed("mark-from-mars")  # and a foreign name
    assert not tr.is_draining()


def test_failed_mark_send_still_fails_closed() -> None:
    tr, clock, _ = _tracker(grace_s=1.0, failing_send=True)
    tr.note_sent(ULAW_BYTES_PER_S)  # 1 s of audio
    asyncio.run(tr.response_ended("resp_A"))  # send raises, swallowed
    # No echo will ever come, but the clock+grace still guards the tail.
    assert tr.is_draining()
    clock.now += 2.1  # past deadline (1 s) + grace (1 s)
    assert not tr.is_draining()


def test_multiple_queued_responses_hold_until_last() -> None:
    tr, _, sent = _tracker()
    tr.note_sent(ULAW_BYTES_PER_S * 10)
    asyncio.run(tr.response_ended("resp_A"))
    tr.note_sent(ULAW_BYTES_PER_S * 3)
    asyncio.run(tr.response_ended("resp_B"))
    assert len(sent) == 2
    tr.mark_echoed(sent[0])
    assert tr.is_draining()  # resp_B's audio still playing
    tr.mark_echoed(sent[1])
    assert not tr.is_draining()


def test_reset_forgets_everything() -> None:
    tr, clock, sent = _tracker()
    tr.note_sent(ULAW_BYTES_PER_S * 30)  # e.g. RING-phase ringback bytes
    asyncio.run(tr.response_ended("resp_A"))
    tr.reset()
    assert not tr.is_draining()
    # bytes_since_mark was zeroed too: a fresh no-audio done arms nothing.
    asyncio.run(tr.response_ended("resp_B"))
    assert len(sent) == 1  # only resp_A's mark from before the reset
    # And the deadline re-anchored: new audio drains relative to now.
    tr.note_sent(ULAW_BYTES_PER_S)
    assert tr._deadline == clock.now + 1.0
