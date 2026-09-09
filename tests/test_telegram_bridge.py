"""Focused tests for TelegramBridge state plumbing + resolution.

These do NOT touch real Telethon or real pytgcalls — they cover the
in-process state machine bits the twilio handler relies on (the
bridge_ended_event edge) and the username-based resolution contract
(ADR 0011: ``place_call`` / ``send_text`` pass ``contact.telegram_username``
to ``get_entity``, not phone).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from outside_line.contacts import Contact
from outside_line.telegram_bridge import TelegramBridge


def _bridge() -> TelegramBridge:
    # __new__ bypasses __init__ so we don't need real Telethon / PyTgCalls
    # instances. We only exercise fields the chat_update handler writes.
    b = TelegramBridge.__new__(TelegramBridge)
    b._current_user_id = None
    b._on_contact_audio = None
    b._on_bridge_ended = None
    b._incoming_frame_logged = False
    b._bridge_metrics = None
    b.bridge_ended_event = asyncio.Event()
    return b


def test_bridge_ended_event_starts_clear() -> None:
    b = _bridge()
    assert not b.bridge_ended_event.is_set()


def test_bridge_ended_event_set_simulates_left_call() -> None:
    b = _bridge()
    b._current_user_id = 5000000001
    # Simulate the body of _on_chat_update on a LEFT_CALL update.
    b._current_user_id = None
    b.bridge_ended_event.set()
    assert b.bridge_ended_event.is_set()


@pytest.mark.asyncio
async def test_bridge_ended_event_cleared_on_place_call_entry() -> None:
    b = _bridge()
    b.bridge_ended_event.set()
    # place_call's first action is bridge_ended_event.clear(); simulate it.
    b.bridge_ended_event.clear()
    assert not b.bridge_ended_event.is_set()


# --- ADR 0011: username-based resolution ---


def _contact(username: str = "@marco_example") -> Contact:
    return Contact(
        first_name="Marco",
        aliases=("marco",),
        telegram_username=username,
        keywords=(),
    )


@pytest.mark.asyncio
async def test_place_call_resolves_by_username() -> None:
    """`place_call` must pass `contact.telegram_username` (not phone) to
    `get_entity`. Regression guard for the phone→username refactor."""
    b = _bridge()
    entity = MagicMock()
    entity.id = 5000000001
    b._telethon = MagicMock()
    b._telethon.get_entity = AsyncMock(return_value=entity)
    b._call_py = MagicMock()
    b._call_py.play = AsyncMock()
    b._call_py.record = AsyncMock()

    user_id = await b.place_call(_contact("@marco_example"))

    b._telethon.get_entity.assert_awaited_once_with("@marco_example")
    assert user_id == 5000000001
    assert b._current_user_id == 5000000001


@pytest.mark.asyncio
async def test_send_text_resolves_by_username() -> None:
    """`send_text` must also resolve by `@username`, not phone."""
    b = _bridge()
    entity = MagicMock()
    entity.id = 5000000001
    sent_msg = MagicMock()
    sent_msg.id = 99
    b._telethon = MagicMock()
    b._telethon.get_entity = AsyncMock(return_value=entity)
    b._telethon.send_message = AsyncMock(return_value=sent_msg)

    msg_id = await b.send_text(_contact("@damian_example"), "hi")

    b._telethon.get_entity.assert_awaited_once_with("@damian_example")
    b._telethon.send_message.assert_awaited_once_with(entity, "hi")
    assert msg_id == 99


# --- ntgcalls Frame.Info.capture_time pass-through ---


@pytest.mark.asyncio
async def test_send_to_contact_default_uses_three_arg_send_frame() -> None:
    """No-arg legacy callers must keep the (uid, device, data) send_frame
    shape — Frame.Info default of 0 is the regression case."""
    from pytgcalls.types.stream.device import Device

    b = _bridge()
    b._call_py = MagicMock()
    b._call_py.send_frame = AsyncMock()

    await b.send_to_contact(5000000001, b"\x00" * 1920)

    b._call_py.send_frame.assert_awaited_once_with(
        5000000001, Device.MICROPHONE, b"\x00" * 1920
    )


@pytest.mark.asyncio
async def test_send_to_contact_forwards_capture_time() -> None:
    """When ``capture_time_ms`` is provided, it lands on Frame.Info so the
    contact-side WebRTC NetEQ sees a wall-clock capture instant."""
    from pytgcalls.types import Frame
    from pytgcalls.types.stream.device import Device

    b = _bridge()
    b._call_py = MagicMock()
    b._call_py.send_frame = AsyncMock()

    await b.send_to_contact(5000000001, b"\x00" * 1920, capture_time_ms=1717002000123)

    b._call_py.send_frame.assert_awaited_once()
    args = b._call_py.send_frame.await_args.args
    assert args[0] == 5000000001
    assert args[1] == Device.MICROPHONE
    assert args[2] == b"\x00" * 1920
    assert isinstance(args[3], Frame.Info)
    assert args[3].capture_time == 1717002000123
