"""Focused tests for `_run_directline_phase` covering the 4 race quadrants.

The directline race model (ADR 0013) has two independent events:

  * ``gate_done_event`` — set by media() when the WS start arrives.
  * ``tg_task`` resolving (to a user_id on pickup, None on timeout).

The combined state at the moment ``_run_directline_phase`` is called
gives us four quadrants we want covered:

  1. IVR done before TG answered  → play Nokia hum to caller, wait for TG
  2. TG answered before IVR done → tg_task is already done with a user_id
  3. TG never answered            → tg_task done with None → fallback=True
  4. Caller hung up during wait   → ws_closed wakes the race, no fallback

We stub out ``_run_bridge_phase`` and ``_ring_audio_driver`` so these
tests don't need a real Telegram bridge or Twilio WS — they exercise
the dispatch logic only.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from outside_line import twilio_handler as twh
from outside_line.contacts import Contact
from outside_line.directlines import DirectlineSession


CONTACT = Contact(
    first_name="Marco",
    aliases=("uncle",),
    telegram_username="marco_example",
    keywords=(),
)


def _empty_audio_inbound() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    return q


async def _noop_send(_payload: bytes) -> None:
    return None


def _fake_bridge() -> object:
    """Minimal stand-in for TelegramBridge — we patch out _run_bridge_phase
    so the bridge's actual methods are never called except .hangup() and
    .discard_outgoing_call() (the two TG teardown methods used by the
    directline phase)."""

    class _B:
        hangup = AsyncMock()
        discard_outgoing_call = AsyncMock()
        send_to_contact = AsyncMock()

    return _B()


@pytest.mark.asyncio
async def test_quadrant_2_tg_already_answered_dispatches_to_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TG answered before IVR done → skip Nokia hum, dispatch to bridge."""
    bridge_called = {"hits": 0, "user_id": None}

    async def _fake_bridge_phase(*, user_id: int, **_kw: object) -> str:
        bridge_called["hits"] += 1
        bridge_called["user_id"] = user_id
        return "bridge_ended"

    monkeypatch.setattr(twh, "_run_bridge_phase", _fake_bridge_phase)

    session = DirectlineSession(call_sid="CA_t2", contact=CONTACT)
    session.gate_done_event.set()
    session.tg_task = asyncio.create_task(_resolved_to(42))
    session.user_id = 42
    await asyncio.sleep(0)  # let the task settle

    outcome = await twh._run_directline_phase(
        ws_closed=asyncio.Event(),
        audio_inbound=_empty_audio_inbound(),
        send_to_twilio=_noop_send,
        telegram_bridge=_fake_bridge(),  # type: ignore[arg-type]
        session=session,
        metrics_ctx=twh._BridgeMetricsCtx(),
    )
    assert outcome.fallback is False
    assert outcome.reason == "bridge_bridge_ended"
    assert bridge_called["hits"] == 1
    assert bridge_called["user_id"] == 42


@pytest.mark.asyncio
async def test_quadrant_1_gate_done_first_plays_hum_then_bridges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IVR done before TG answered → Nokia hum runs, then bridge dispatches."""
    hum_run = asyncio.Event()

    async def _fake_hum(_send: object, cancel: asyncio.Event) -> None:
        hum_run.set()
        # Park until cancelled (mirrors the real driver shape).
        try:
            await cancel.wait()
        except asyncio.CancelledError:
            pass

    bridge_hit = {"hits": 0}

    async def _fake_bridge_phase(**_kw: object) -> str:
        bridge_hit["hits"] += 1
        return "bridge_ended"

    monkeypatch.setattr(twh, "_ring_audio_driver", _fake_hum)
    monkeypatch.setattr(twh, "_run_bridge_phase", _fake_bridge_phase)

    # TG task that resolves only after we manually nudge it.
    tg_resolve = asyncio.Event()

    async def _slow_tg() -> int | None:
        await tg_resolve.wait()
        return 99

    session = DirectlineSession(call_sid="CA_t1", contact=CONTACT)
    session.gate_done_event.set()
    session.tg_task = asyncio.create_task(_slow_tg())

    audio_inbound = _empty_audio_inbound()
    audio_inbound.put_nowait((b"x", 0))  # one frame to be drained

    phase_task = asyncio.create_task(
        twh._run_directline_phase(
            ws_closed=asyncio.Event(),
            audio_inbound=audio_inbound,
            send_to_twilio=_noop_send,
            telegram_bridge=_fake_bridge(),  # type: ignore[arg-type]
            session=session,
            metrics_ctx=twh._BridgeMetricsCtx(),
        )
    )
    # Hum should have started before the bridge dispatches.
    await asyncio.wait_for(hum_run.wait(), timeout=1.0)
    # Now resolve the TG ring → phase should cancel the hum + dispatch.
    tg_resolve.set()
    outcome = await asyncio.wait_for(phase_task, timeout=1.0)
    assert outcome.fallback is False
    assert bridge_hit["hits"] == 1


@pytest.mark.asyncio
async def test_quadrant_3_tg_no_answer_returns_fallback() -> None:
    """TG resolved to None → fallback=True with the apology instruction."""
    session = DirectlineSession(call_sid="CA_t3", contact=CONTACT)
    session.gate_done_event.set()
    session.tg_task = asyncio.create_task(_resolved_to(None))
    await asyncio.sleep(0)

    outcome = await twh._run_directline_phase(
        ws_closed=asyncio.Event(),
        audio_inbound=_empty_audio_inbound(),
        send_to_twilio=_noop_send,
        telegram_bridge=_fake_bridge(),  # type: ignore[arg-type]
        session=session,
        metrics_ctx=twh._BridgeMetricsCtx(),
    )
    assert outcome.fallback is True
    assert outcome.reason == "tg_no_answer"
    assert outcome.first_response_instruction is not None
    assert "Marco" in outcome.first_response_instruction


@pytest.mark.asyncio
async def test_quadrant_4_ws_close_during_ring_cancels_tg_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS closes while TG still ringing → cancel TG + send MTProto discard.

    The discard branch (not hangup) fires because ``tg_answered_ns`` is
    None — the contact didn't pick up before the caller hung up.
    ``session.user_id`` is set up front by ``_directline_ring``'s
    pre-resolution step (simulated here by assignment), so the cancel
    site has the chat_id needed for the discard.
    """

    async def _fake_hum(_send: object, cancel: asyncio.Event) -> None:
        try:
            await cancel.wait()
        except asyncio.CancelledError:
            pass

    monkeypatch.setattr(twh, "_ring_audio_driver", _fake_hum)

    tg_cancelled = asyncio.Event()

    async def _slow_tg() -> int | None:
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            tg_cancelled.set()
            raise
        return 0

    session = DirectlineSession(call_sid="CA_t4", contact=CONTACT)
    session.gate_done_event.set()
    session.tg_task = asyncio.create_task(_slow_tg())
    session.user_id = 42  # pre-resolved by _directline_ring in real code

    ws_closed = asyncio.Event()
    bridge = _fake_bridge()

    phase_task = asyncio.create_task(
        twh._run_directline_phase(
            ws_closed=ws_closed,
            audio_inbound=_empty_audio_inbound(),
            send_to_twilio=_noop_send,
            telegram_bridge=bridge,  # type: ignore[arg-type]
            session=session,
            metrics_ctx=twh._BridgeMetricsCtx(),
        )
    )
    # Let the phase enter the "still ringing" branch (hum starts, race awaits)
    await asyncio.sleep(0.05)
    # Caller hangs up mid-ring
    ws_closed.set()

    outcome = await asyncio.wait_for(phase_task, timeout=1.0)
    assert outcome.fallback is False
    assert outcome.reason == "ws_closed_during_ring"
    # TG task was cancelled (not allowed to keep ringing into the void).
    assert tg_cancelled.is_set()
    # MTProto discard fired against the pre-resolved user_id; hangup did
    # NOT fire because the contact never answered.
    bridge.discard_outgoing_call.assert_awaited_once_with(42)
    bridge.hangup.assert_not_awaited()


@pytest.mark.asyncio
async def test_play_tg_holding_audio_emits_10ms_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the framing contract: every send_to_contact payload is 960 B.

    ntgcalls' send_external_frame is 10 ms-granular (960 B = 480 samples =
    10 ms @ 48 kHz mono int16). A 20 ms (1920 B) submission gets
    reinterpreted as one 10 ms frame and half the audio is silently
    dropped — the same caller→contact gotcha (BRIDGE pump splits each
    20 ms Twilio frame into two 10 ms halves for the same reason). This
    test guards the directline holding pump against the same regression.
    """
    from outside_line.audio_bridge import (
        PCM48_10MS_FRAME_BYTES,
        RINGBACK_PCM48_LOOP,
        WAIT_MESSAGE_PCM48_BYTES,
        chunk_pcm48_10ms,
    )

    # No-op sleep so we don't pay 360 × 10 ms of wall time on the wait
    # phrase. Reverted at test teardown by the monkeypatch fixture.
    async def _no_sleep(_duration: float) -> None:
        return None

    monkeypatch.setattr(twh.asyncio, "sleep", _no_sleep)

    recorded: list[bytes] = []
    gate_done = asyncio.Event()

    expected_wait_chunks = chunk_pcm48_10ms(WAIT_MESSAGE_PCM48_BYTES)
    # Stop the pump after the wait phrase + one ringback chunk so we
    # exercise both code paths in a single run.
    stop_after_n = len(expected_wait_chunks) + 1

    async def _record(user_id: int, payload: bytes) -> None:
        recorded.append(payload)
        if len(recorded) >= stop_after_n:
            gate_done.set()

    class _B:
        send_to_contact = staticmethod(_record)

    await twh._play_tg_holding_audio(
        bridge=_B(),  # type: ignore[arg-type]
        user_id=42,
        gate_done_event=gate_done,
    )

    assert recorded, "pump produced no submissions"
    bad_sizes = sorted({len(p) for p in recorded if len(p) != PCM48_10MS_FRAME_BYTES})
    assert not bad_sizes, f"non-960B payloads observed: {bad_sizes}"

    if WAIT_MESSAGE_PCM48_BYTES:
        assert recorded[: len(expected_wait_chunks)] == expected_wait_chunks, (
            "wait-phrase chunks don't match WAIT_MESSAGE_PCM48_BYTES prefix"
        )
        ringback_chunks = chunk_pcm48_10ms(RINGBACK_PCM48_LOOP)
        assert recorded[len(expected_wait_chunks)] == ringback_chunks[0], (
            "first ringback chunk doesn't match RINGBACK_PCM48_LOOP[0]"
        )
    else:
        # Asset absent in this env — wait phrase block skipped; everything
        # recorded must be ringback chunks.
        ringback_chunks = chunk_pcm48_10ms(RINGBACK_PCM48_LOOP)
        assert recorded[0] == ringback_chunks[0]


# -- helpers ---------------------------------------------------------------


async def _resolved_to(value: int | None) -> int | None:
    """Return ``value`` so callers can wrap it in a ready Task."""
    return value


# -- no-answer instruction: name the pinned contact, keep the handle private --

_ANY = Contact(
    first_name="Nia",
    aliases=("Nia Rodriguez",),
    telegram_username="nia_example",
    keywords=("niece", "Rodriguez"),
)


def test_no_answer_instruction_voices_relationship_not_handle() -> None:
    """A relationship keyword surfaces as "their <relationship>" so the
    target is named specifically; the telegram handle never appears
    (spoken-out-loud ban)."""
    assert twh._contact_descriptor(_ANY) == "Nia, their niece"
    text = twh._no_answer_instruction(_ANY)
    assert "Nia, their niece" in text
    assert "nia_example" not in text


def test_no_answer_instruction_falls_back_to_first_name() -> None:
    """No relationship keyword (only a surname) → bare first name, no
    "their …"."""
    surname_only = Contact(
        first_name="Nia",
        aliases=(),
        telegram_username="nia_example",
        keywords=("Rodriguez",),
    )
    assert twh._contact_descriptor(surname_only) == "Nia"
    assert "their " not in twh._no_answer_instruction(surname_only)


def test_prompts_carry_no_real_contact_example_name() -> None:
    """Persona + contact tool descriptions must not prime a fixture
    contact's name — a primed example name can mis-route a retry. The
    example name "Rigoberto" must resolve to nobody, so residual priming
    fails safe."""
    import json

    from outside_line.contacts import find_contacts
    from outside_line.persona import SYSTEM_PROMPT
    from outside_line.realtime_agent import _CALL_CONTACT_TOOL, _MESSAGE_CONTACT_TOOL

    tool_text = json.dumps(
        [_CALL_CONTACT_TOOL, _MESSAGE_CONTACT_TOOL], ensure_ascii=False
    )
    assert "Marco" not in SYSTEM_PROMPT
    assert "Marco" not in tool_text
    # Against the module's own fixture pool (never the operator's real
    # data/contacts.json): the example name must score zero.
    assert find_contacts("Rigoberto", contacts=[CONTACT, _ANY]) == []
