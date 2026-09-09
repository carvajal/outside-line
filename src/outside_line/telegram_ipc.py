"""Wire protocol for the Telegram-bridge sidecar IPC (ADR 0017).

The ntgcalls voice stack lives in a supervised sidecar process so a native
SIGSEGV (ntgcalls#51) costs one bridge attempt, not the whole FastAPI server.
The FastAPI process talks to that sidecar over **two** Unix-domain stream
connections — a CONTROL channel (RPCs + async events) and an AUDIO channel
(the ~100-200 fps hot path) — so a flood of audio frames can never
head-of-line-block an RPC reply (e.g. a ``place_call`` result).

Both channels share one framing:

    ┌──────────────┬─────────┬──────────────────────────┐
    │ u32 length   │ u8 type │ body (length-1 bytes)     │   length is big-endian
    └──────────────┴─────────┴──────────────────────────┘   and covers type+body.

This module is a **pure codec** — no sockets are opened here beyond the
``read_frame`` helper that consumes an ``asyncio.StreamReader`` — so every
message shape round-trips in unit tests without spawning a process.

Design notes:
- ``send_to_contact`` payloads cross the wire **verbatim** (whatever bytes the
  FastAPI side passes — typically one 960-byte / 10 ms PCM48 granule). The
  codec never assumes a fixed frame size, so ADR 0009's framing invariant is
  preserved by construction: we resample+split on the FastAPI side exactly as
  today and the sidecar forwards frames untouched.
- Telegram user ids and wall-clock capture timestamps both exceed 2**32, so
  they ride as u64.
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any

from .contacts import Contact

# --- Channel-hello bytes (first byte the proxy sends on each connection so the
# sidecar can attach the right reader/writer loop). ---
CH_CONTROL = 0x01
CH_AUDIO = 0x02

# --- Message types (the frame's type byte). ---
MSG_RPC_REQUEST = 0x01  # control: proxy → sidecar, correlated by request_id
MSG_RPC_RESPONSE = 0x02  # control: sidecar → proxy, {ok, result} | {ok:false, error}
MSG_EVENT_BRIDGE_ENDED = 0x03  # control: sidecar → proxy, {user_id, reason}
MSG_EVENT_READY = 0x04  # control: sidecar → proxy, bridge.start() done
MSG_PING = 0x05  # control: proxy → sidecar liveness probe
MSG_PONG = 0x06  # control: sidecar → proxy liveness reply
MSG_AUDIO_TO_CONTACT = 0x10  # audio: proxy → sidecar, [u64 uid][u64 cap][pcm]
MSG_AUDIO_FROM_CONTACT = 0x11  # audio: sidecar → proxy, [u64 uid][pcm]

_HEADER = struct.Struct(">I")  # frame length prefix
_AUDIO_TO = struct.Struct(">QQ")  # uid, capture_ms
_AUDIO_FROM = struct.Struct(">Q")  # uid
_REQID = struct.Struct(">I")  # rpc request id

# capture_time_ms sentinel meaning "None" (legacy 3-arg send_frame shape).
_CAPTURE_NONE = 0xFFFFFFFFFFFFFFFF

# Guard against a desynced stream handing us a bogus multi-GB length. Audio
# frames are ~960 B and JSON RPCs are tiny; 1 MiB is a very comfortable ceiling.
MAX_FRAME_BYTES = 1 << 20


class IpcProtocolError(Exception):
    """Raised on a malformed frame (bad length, truncated stream)."""


# --- Low-level framing ---


def encode_frame(msg_type: int, body: bytes = b"") -> bytes:
    """Frame one message: [u32 length][u8 type][body]."""
    return _HEADER.pack(len(body) + 1) + bytes((msg_type,)) + body


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """Read one framed message from ``reader``.

    Returns ``(msg_type, body)``, or ``None`` on a clean EOF between frames
    (the peer closed the connection) — the caller treats that as "sidecar
    gone". A truncated frame mid-read raises ``IpcProtocolError``.
    """
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None  # clean close on a frame boundary
        raise IpcProtocolError("truncated frame header") from exc
    (length,) = _HEADER.unpack(header)
    if length < 1 or length > MAX_FRAME_BYTES:
        raise IpcProtocolError(f"frame length {length} out of range")
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise IpcProtocolError("truncated frame body") from exc
    return payload[0], payload[1:]


# --- Audio (hot path) ---


def encode_audio_to_contact(uid: int, pcm: bytes, capture_ms: int | None) -> bytes:
    cap = _CAPTURE_NONE if capture_ms is None else capture_ms
    return encode_frame(MSG_AUDIO_TO_CONTACT, _AUDIO_TO.pack(uid, cap) + pcm)


def decode_audio_to_contact(body: bytes) -> tuple[int, bytes, int | None]:
    uid, cap = _AUDIO_TO.unpack_from(body, 0)
    pcm = body[_AUDIO_TO.size :]
    return uid, pcm, (None if cap == _CAPTURE_NONE else cap)


def encode_audio_from_contact(uid: int, pcm: bytes) -> bytes:
    return encode_frame(MSG_AUDIO_FROM_CONTACT, _AUDIO_FROM.pack(uid) + pcm)


def decode_audio_from_contact(body: bytes) -> tuple[int, bytes]:
    (uid,) = _AUDIO_FROM.unpack_from(body, 0)
    return uid, body[_AUDIO_FROM.size :]


# --- Control RPCs ---


def encode_rpc_request(request_id: int, method: str, args: dict[str, Any]) -> bytes:
    body = (
        _REQID.pack(request_id) + json.dumps({"method": method, "args": args}).encode()
    )
    return encode_frame(MSG_RPC_REQUEST, body)


def decode_rpc_request(body: bytes) -> tuple[int, str, dict[str, Any]]:
    (request_id,) = _REQID.unpack_from(body, 0)
    obj = json.loads(body[_REQID.size :])
    return request_id, obj["method"], obj["args"]


def encode_rpc_ok(request_id: int, result: Any) -> bytes:
    body = _REQID.pack(request_id) + json.dumps({"ok": True, "result": result}).encode()
    return encode_frame(MSG_RPC_RESPONSE, body)


def encode_rpc_err(request_id: int, exc_type: str, msg: str) -> bytes:
    body = (
        _REQID.pack(request_id)
        + json.dumps({"ok": False, "error": {"type": exc_type, "msg": msg}}).encode()
    )
    return encode_frame(MSG_RPC_RESPONSE, body)


def decode_rpc_response(body: bytes) -> tuple[int, dict[str, Any]]:
    """Return ``(request_id, {"ok": ..., ...})``."""
    (request_id,) = _REQID.unpack_from(body, 0)
    return request_id, json.loads(body[_REQID.size :])


# --- Control events ---


def encode_bridge_ended(user_id: int, reason: str) -> bytes:
    return encode_frame(
        MSG_EVENT_BRIDGE_ENDED,
        json.dumps({"user_id": user_id, "reason": reason}).encode(),
    )


def decode_bridge_ended(body: bytes) -> tuple[int, str]:
    obj = json.loads(body)
    return obj["user_id"], obj["reason"]


# --- Contact (de)serialization (rides in place_call / send_text / resolve args) ---


def contact_to_dict(contact: Contact) -> dict[str, Any]:
    return {
        "first_name": contact.first_name,
        "aliases": list(contact.aliases),
        "telegram_username": contact.telegram_username,
        "keywords": list(contact.keywords),
    }


def contact_from_dict(d: dict[str, Any]) -> Contact:
    return Contact(
        first_name=d["first_name"],
        aliases=tuple(d.get("aliases", ())),
        telegram_username=d["telegram_username"],
        keywords=tuple(d.get("keywords", ())),
    )
