"""Round-trip tests for the sidecar IPC wire protocol (telegram_ipc).

Pure codec coverage — every message shape encodes and decodes back to the
same value, including the two edges that bit us in the design review:
Telegram user ids + wall-clock capture timestamps exceed 2**32 (must be u64),
and ``capture_ms=None`` must survive as a sentinel, not become 0.
"""

from __future__ import annotations

import asyncio

import pytest

from outside_line import telegram_ipc as ipc
from outside_line.contacts import Contact

# A realistic Telegram user id (> u32) and a real epoch-ms capture time (> u32).
BIG_UID = 5000000001
BIG_CAPTURE_MS = 1717002000123
PCM_960 = b"\x01\x02" * 480  # one 10 ms / 48 kHz mono int16 granule = 960 bytes


def _roundtrip(frame: bytes) -> tuple[int, bytes]:
    """Split an encoded frame back into (msg_type, body) via the header math."""
    (length,) = ipc._HEADER.unpack(frame[:4])
    assert len(frame) == 4 + length
    payload = frame[4:]
    return payload[0], payload[1:]


def test_encode_frame_length_covers_type_and_body() -> None:
    frame = ipc.encode_frame(ipc.MSG_PING, b"abc")
    (length,) = ipc._HEADER.unpack(frame[:4])
    assert length == 1 + 3  # type byte + body
    msg_type, body = _roundtrip(frame)
    assert msg_type == ipc.MSG_PING
    assert body == b"abc"


def test_encode_frame_empty_body() -> None:
    frame = ipc.encode_frame(ipc.MSG_EVENT_READY)
    msg_type, body = _roundtrip(frame)
    assert msg_type == ipc.MSG_EVENT_READY
    assert body == b""


def test_audio_to_contact_roundtrip_with_capture() -> None:
    frame = ipc.encode_audio_to_contact(BIG_UID, PCM_960, BIG_CAPTURE_MS)
    msg_type, body = _roundtrip(frame)
    assert msg_type == ipc.MSG_AUDIO_TO_CONTACT
    uid, pcm, cap = ipc.decode_audio_to_contact(body)
    assert uid == BIG_UID
    assert pcm == PCM_960
    assert cap == BIG_CAPTURE_MS


def test_audio_to_contact_none_capture_survives_as_none() -> None:
    frame = ipc.encode_audio_to_contact(BIG_UID, PCM_960, None)
    _, body = _roundtrip(frame)
    uid, pcm, cap = ipc.decode_audio_to_contact(body)
    assert uid == BIG_UID
    assert pcm == PCM_960
    assert cap is None  # sentinel, not 0


def test_audio_from_contact_roundtrip() -> None:
    frame = ipc.encode_audio_from_contact(BIG_UID, PCM_960)
    msg_type, body = _roundtrip(frame)
    assert msg_type == ipc.MSG_AUDIO_FROM_CONTACT
    uid, pcm = ipc.decode_audio_from_contact(body)
    assert uid == BIG_UID
    assert pcm == PCM_960


def test_rpc_request_roundtrip() -> None:
    frame = ipc.encode_rpc_request(42, "place_call", {"timeout_s": 60})
    msg_type, body = _roundtrip(frame)
    assert msg_type == ipc.MSG_RPC_REQUEST
    request_id, method, args = ipc.decode_rpc_request(body)
    assert request_id == 42
    assert method == "place_call"
    assert args == {"timeout_s": 60}


def test_rpc_ok_roundtrip() -> None:
    frame = ipc.encode_rpc_ok(7, BIG_UID)
    _, body = _roundtrip(frame)
    request_id, obj = ipc.decode_rpc_response(body)
    assert request_id == 7
    assert obj == {"ok": True, "result": BIG_UID}


def test_rpc_err_roundtrip() -> None:
    frame = ipc.encode_rpc_err(9, "RuntimeError", "not authorized")
    _, body = _roundtrip(frame)
    request_id, obj = ipc.decode_rpc_response(body)
    assert request_id == 9
    assert obj["ok"] is False
    assert obj["error"] == {"type": "RuntimeError", "msg": "not authorized"}


def test_bridge_ended_roundtrip() -> None:
    frame = ipc.encode_bridge_ended(BIG_UID, "sidecar_crashed")
    msg_type, body = _roundtrip(frame)
    assert msg_type == ipc.MSG_EVENT_BRIDGE_ENDED
    uid, reason = ipc.decode_bridge_ended(body)
    assert uid == BIG_UID
    assert reason == "sidecar_crashed"


def test_contact_roundtrip_preserves_tuples() -> None:
    c = Contact(
        first_name="Marco",
        aliases=("marc", "marco m"),
        telegram_username="@marco_example",
        keywords=("brother", "mom"),
    )
    back = ipc.contact_from_dict(ipc.contact_to_dict(c))
    assert back == c
    assert isinstance(back.aliases, tuple)
    assert isinstance(back.keywords, tuple)


def test_contact_from_dict_tolerates_missing_optional_fields() -> None:
    back = ipc.contact_from_dict({"first_name": "Ana", "telegram_username": "@ana"})
    assert back == Contact(
        first_name="Ana", aliases=(), telegram_username="@ana", keywords=()
    )


# --- read_frame against a real asyncio.StreamReader ---


def _feed(reader: asyncio.StreamReader, data: bytes) -> None:
    reader.feed_data(data)


@pytest.mark.asyncio
async def test_read_frame_reads_consecutive_frames() -> None:
    reader = asyncio.StreamReader()
    _feed(reader, ipc.encode_audio_to_contact(BIG_UID, PCM_960, BIG_CAPTURE_MS))
    _feed(reader, ipc.encode_bridge_ended(BIG_UID, "LEFT_CALL"))
    reader.feed_eof()

    t1, b1 = await ipc.read_frame(reader)
    assert t1 == ipc.MSG_AUDIO_TO_CONTACT
    assert ipc.decode_audio_to_contact(b1) == (BIG_UID, PCM_960, BIG_CAPTURE_MS)

    t2, b2 = await ipc.read_frame(reader)
    assert t2 == ipc.MSG_EVENT_BRIDGE_ENDED
    assert ipc.decode_bridge_ended(b2) == (BIG_UID, "LEFT_CALL")

    assert await ipc.read_frame(reader) is None  # clean EOF


@pytest.mark.asyncio
async def test_read_frame_truncated_body_raises() -> None:
    reader = asyncio.StreamReader()
    frame = ipc.encode_audio_from_contact(BIG_UID, PCM_960)
    _feed(reader, frame[:-10])  # chop the tail
    reader.feed_eof()
    with pytest.raises(ipc.IpcProtocolError):
        await ipc.read_frame(reader)


@pytest.mark.asyncio
async def test_read_frame_rejects_oversized_length() -> None:
    reader = asyncio.StreamReader()
    _feed(reader, ipc._HEADER.pack(ipc.MAX_FRAME_BYTES + 1))
    reader.feed_eof()
    with pytest.raises(ipc.IpcProtocolError):
        await ipc.read_frame(reader)
