"""Crash-UX tests (ADR 0017): play a fixed recording, then hang up.

A sidecar SIGSEGV sets bridge_ended_event with reason "sidecar_crashed"; the
bridge phase must surface that (vs a normal "bridge_ended" hangup) so both the
The agent phase loop and the directline path can play the caller the pre-rendered
crash recording and then end the call — no re-ring, no the agent turn.
"""

from __future__ import annotations

import asyncio

import pytest

from outside_line import twilio_handler as twh
from outside_line.contacts import Contact
from outside_line.directlines import DirectlineSession

CONTACT = Contact(
    first_name="Marco", aliases=("uncle",), telegram_username="marco_example", keywords=()
)


async def _noop_send(_payload: bytes) -> None:
    return None


def _fake_bridge(*, ended_reason: str | None):
    """Bridge stand-in whose bridge_ended_event is already set, so
    _run_bridge_phase's race resolves immediately to the ended branch."""

    class _B:
        def __init__(self) -> None:
            self.bridge_ended_event = asyncio.Event()
            self.bridge_ended_event.set()
            if ended_reason is not None:
                self._last_bridge_ended_reason = ended_reason

        def register_callbacks(self, *a: object, **k: object) -> None:
            return None

        async def hangup(self, _uid: int) -> None:
            return None

        async def send_to_contact(self, *a: object, **k: object) -> None:
            return None

    return _B()


@pytest.mark.asyncio
async def test_bridge_phase_reports_sidecar_crashed() -> None:
    reason = await twh._run_bridge_phase(
        ws_closed=asyncio.Event(),
        audio_inbound=asyncio.Queue(maxsize=200),
        send_to_twilio=_noop_send,
        telegram_bridge=_fake_bridge(ended_reason="sidecar_crashed"),  # type: ignore[arg-type]
        user_id=42,
        metrics_ctx=twh._BridgeMetricsCtx(),
    )
    assert reason == "sidecar_crashed"


@pytest.mark.asyncio
async def test_bridge_phase_reports_bridge_ended_on_normal_hangup() -> None:
    reason = await twh._run_bridge_phase(
        ws_closed=asyncio.Event(),
        audio_inbound=asyncio.Queue(maxsize=200),
        send_to_twilio=_noop_send,
        telegram_bridge=_fake_bridge(ended_reason="LEFT_CALL"),  # type: ignore[arg-type]
        user_id=42,
        metrics_ctx=twh._BridgeMetricsCtx(),
    )
    assert reason == "bridge_ended"


@pytest.mark.asyncio
async def test_directline_sidecar_crash_plays_recording_and_ends(monkeypatch) -> None:
    async def _crashed_bridge_phase(**_kw: object) -> str:
        return "sidecar_crashed"

    played: list[bool] = []

    async def _fake_play(*_a: object, **_k: object) -> None:
        played.append(True)

    monkeypatch.setattr(twh, "_run_bridge_phase", _crashed_bridge_phase)
    monkeypatch.setattr(twh, "_play_ulaw_clip", _fake_play)

    session = DirectlineSession(call_sid="CA_crash", contact=CONTACT)
    session.gate_done_event.set()
    session.tg_task = asyncio.create_task(_resolved_to(42))
    session.user_id = 42
    await asyncio.sleep(0)

    outcome = await twh._run_directline_phase(
        ws_closed=asyncio.Event(),
        audio_inbound=asyncio.Queue(maxsize=200),
        send_to_twilio=_noop_send,
        telegram_bridge=_fake_bridge(ended_reason=None),  # type: ignore[arg-type]
        session=session,
        metrics_ctx=twh._BridgeMetricsCtx(),
    )
    # Crash → recording played, then the call ends (fallback False → media()
    # falls through to teardown; no the agent loop, no re-ring instruction).
    assert played == [True]
    assert outcome.fallback is False
    assert outcome.reason == "bridge_sidecar_crashed"
    assert outcome.first_response_instruction is None


async def _resolved_to(value: int | None) -> int | None:
    return value
