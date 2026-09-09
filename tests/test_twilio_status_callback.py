"""Focused tests for the ``POST /twilio/voice/status`` endpoint.

Three cases pin the load-bearing dispatch logic:

  1. Terminal ``CallStatus`` for an unknown ``CallSid`` → 200 + no-op
     (the most common case — mainline calls fire this every time).
  2. Terminal ``CallStatus`` for a known directline ``CallSid`` →
     200 + ``_finalize_directline_session`` runs (the load-bearing
     scenario — caller hung up during the gate before the WS opened, the
     statusCallback is the only signal in <70 s).
  3. Intermediate ``CallStatus`` (``ringing``) → 200 + registry
     untouched (defends against an upstream Twilio default change).

We call the endpoint coroutine directly with a fake ``Request`` instead
of FastAPI ``TestClient`` — same pattern as
``tests/test_directline_phase.py``. The endpoint reads ``request.body()``
and ``request.app.state.telegram_bridge`` only; both are easy to stub.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from outside_line import directlines
from outside_line import twilio_handler as twh
from outside_line.contacts import Contact
from outside_line.directlines import DirectlineSession


CONTACT = Contact(
    first_name="Marco",
    aliases=("uncle",),
    telegram_username="marco_example",
    keywords=(),
)


def _fake_request(body_bytes: bytes, bridge: object | None) -> object:
    """Minimal stand-in for FastAPI ``Request`` — only the two attributes
    ``voice_status`` reads."""

    class _Req:
        app = SimpleNamespace(state=SimpleNamespace(telegram_bridge=bridge))

        async def body(self) -> bytes:
            return body_bytes

    return _Req()


def _fake_bridge() -> object:
    class _B:
        hangup = AsyncMock()
        discard_outgoing_call = AsyncMock()

    return _B()


@pytest.fixture(autouse=True)
def _clear_registry() -> object:
    """Wipe the process-local directline registry between tests so
    test_2's registered session doesn't bleed into the others."""
    directlines._SESSION_REGISTRY.clear()
    yield
    directlines._SESSION_REGISTRY.clear()


@pytest.mark.asyncio
async def test_unknown_call_sid_returns_200_no_op() -> None:
    """Mainline calls (and any unknown sid) → 200 with no teardown."""
    bridge = _fake_bridge()
    body = b"CallSid=CAunknown&CallStatus=completed"
    request = _fake_request(body, bridge)

    resp = await twh.voice_status(request)  # type: ignore[arg-type]

    assert resp.status_code == 200
    bridge.hangup.assert_not_awaited()
    bridge.discard_outgoing_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_call_sid_runs_finalize_helper() -> None:
    """Directline call_sid registered → 200 + the not-answered teardown
    branch fires (contact never picked up)."""
    bridge = _fake_bridge()
    session = DirectlineSession(call_sid="CA_known", contact=CONTACT)
    session.user_id = 99
    # tg_answered_ns stays None → not-answered branch
    directlines.register_session(session)

    body = b"CallSid=CA_known&CallStatus=completed"
    request = _fake_request(body, bridge)

    resp = await twh.voice_status(request)  # type: ignore[arg-type]

    assert resp.status_code == 200
    bridge.discard_outgoing_call.assert_awaited_once_with(99)
    bridge.hangup.assert_not_awaited()
    # Registry entry consumed
    assert directlines.peek_session("CA_known") is None


@pytest.mark.asyncio
async def test_intermediate_status_does_not_touch_registry() -> None:
    """``ringing`` (or any non-terminal CallStatus) → 200 + registry
    entry preserved. Defends against a Twilio default-change that
    would start firing intermediate events on the same URL."""
    bridge = _fake_bridge()
    session = DirectlineSession(call_sid="CA_pending", contact=CONTACT)
    session.user_id = 42
    directlines.register_session(session)

    body = b"CallSid=CA_pending&CallStatus=ringing"
    request = _fake_request(body, bridge)

    resp = await twh.voice_status(request)  # type: ignore[arg-type]

    assert resp.status_code == 200
    bridge.discard_outgoing_call.assert_not_awaited()
    bridge.hangup.assert_not_awaited()
    # Registry entry survived the no-op
    assert directlines.peek_session("CA_pending") is session
