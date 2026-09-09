"""Integration tests for the sidecar IPC transport (ADR 0017).

Spawns the real ``telegram_sidecar`` process in ``--echo`` mode over a real
Unix-domain socket and drives it through ``TelegramBridgeClient`` — proving the
two-channel transport, RPC correlation/error propagation, audio echo, and that
an audio flood on the AUDIO channel does not block a CONTROL-channel RPC (the
head-of-line-blocking guard the two-channel split exists for).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile

import pytest

from outside_line import telegram_sidecar
from outside_line.contacts import Contact
from outside_line.telegram_bridge_client import BridgeRPCError, TelegramBridgeClient


def _socket_path() -> str:
    # Short path — macOS caps a UDS sun_path at ~104 bytes, and pytest's
    # tmp_path is often longer than that.
    return tempfile.mktemp(prefix="rc_ipc_", suffix=".sock", dir="/tmp")


def _contact(username: str = "@marco_example") -> Contact:
    return Contact(
        first_name="Marco",
        aliases=("marc",),
        telegram_username=username,
        keywords=(),
    )


@contextlib.asynccontextmanager
async def _echo_client():
    """Spawn an --echo sidecar and yield a connected client; tear both down."""
    socket_path = _socket_path()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "outside_line.telegram_sidecar",
        "--socket",
        socket_path,
        "--echo",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    client = TelegramBridgeClient(socket_path)
    try:
        await client.connect(connect_timeout_s=10.0, ready_timeout_s=10.0)
        yield client
    finally:
        await client.stop()
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5.0)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(socket_path)


@pytest.mark.asyncio
async def test_resolve_and_place_call_roundtrip() -> None:
    async with _echo_client() as client:
        uid_resolved = await client.resolve_user_id(_contact())
        uid_placed = await client.place_call(_contact())
        assert uid_resolved == uid_placed  # deterministic echo uid
        assert client._current_user_id == uid_placed


@pytest.mark.asyncio
async def test_send_to_contact_echoes_back_as_on_contact_audio() -> None:
    async with _echo_client() as client:
        received: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()

        async def on_audio(uid: int, pcm: bytes) -> None:
            await received.put((uid, pcm))

        uid = await client.place_call(_contact())
        client.register_callbacks(on_audio, None)

        frame = b"\x11\x22" * 480  # one 960 B PCM48 granule
        await client.send_to_contact(uid, frame)

        recv_uid, recv_pcm = await asyncio.wait_for(received.get(), 5.0)
        assert recv_uid == uid
        assert recv_pcm == frame


@pytest.mark.asyncio
async def test_rpc_error_propagates_as_bridge_rpc_error() -> None:
    async with _echo_client() as client:
        with pytest.raises(BridgeRPCError) as excinfo:
            await client.place_call(_contact("@boom"))
        assert "RuntimeError" in str(excinfo.value)


@pytest.mark.asyncio
async def test_audio_flood_does_not_block_control_rpc() -> None:
    async with _echo_client() as client:
        uid = await client.place_call(_contact())

        async def _drop(_uid: int, _pcm: bytes) -> None:
            return None

        client.register_callbacks(_drop, None)

        frame = b"\x00" * 960
        for _ in range(2000):
            await client.send_to_contact(uid, frame)

        # Despite ~2000 audio frames (and their echoes) in flight, a CONTROL
        # RPC still completes promptly because it rides a separate connection.
        resolved = await asyncio.wait_for(
            client.resolve_user_id(_contact("@other")), 3.0
        )
        assert resolved == telegram_sidecar._fake_uid(_contact("@other"))
