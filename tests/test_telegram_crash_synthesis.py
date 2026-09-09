"""Crash-synthesis + durable-capture tests (ADR 0017, commit 4).

When the sidecar dies mid-bridge the connection drops with no LEFT_CALL update,
so the proxy must *synthesize* on_bridge_ended(reason="sidecar_crashed") to
unblock _run_bridge_phase (and, in commit 6, trigger the agent's apology). The
supervisor tails the sidecar's durable faulthandler trace into Railway on death.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile

import pytest

from outside_line.contacts import Contact
from outside_line.telegram_bridge_client import TelegramBridgeClient
from outside_line.telegram_supervisor import SidecarSupervisor


def _socket_path() -> str:
    return tempfile.mktemp(prefix="rc_crash_", suffix=".sock", dir="/tmp")


def _contact(username: str = "@marco_example") -> Contact:
    return Contact(
        first_name="Marco", aliases=(), telegram_username=username, keywords=()
    )


@pytest.mark.asyncio
async def test_connection_loss_synthesizes_sidecar_crashed() -> None:
    client = TelegramBridgeClient("/tmp/unused.sock")
    client._loop = asyncio.get_running_loop()
    fired: list[tuple[int, str]] = []

    async def on_ended(uid: int, reason: str) -> None:
        fired.append((uid, reason))

    client.register_callbacks(None, on_ended)
    client._current_user_id = 555

    client._on_connection_lost()

    assert client.bridge_ended_event.is_set()
    assert client._last_bridge_ended_reason == "sidecar_crashed"
    await asyncio.sleep(0)  # let the scheduled callback run
    assert fired == [(555, "sidecar_crashed")]

    # Idempotent: a second loop's finally must not re-fire.
    client._on_connection_lost()
    await asyncio.sleep(0)
    assert fired == [(555, "sidecar_crashed")]


@pytest.mark.asyncio
async def test_clean_close_does_not_synthesize_crash() -> None:
    client = TelegramBridgeClient("/tmp/unused.sock")
    client._loop = asyncio.get_running_loop()
    fired: list[tuple[int, str]] = []

    async def on_ended(uid: int, reason: str) -> None:
        fired.append((uid, reason))

    client.register_callbacks(None, on_ended)
    client._current_user_id = 555
    client._closing = True  # a graceful stop, not a crash

    client._on_connection_lost()
    await asyncio.sleep(0)

    assert not client.bridge_ended_event.is_set()
    assert fired == []


@pytest.mark.asyncio
async def test_killed_sidecar_fires_sidecar_crashed_end_to_end() -> None:
    socket_path = _socket_path()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "outside_line.telegram_sidecar",
        "--socket",
        socket_path,
        "--echo",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    client = TelegramBridgeClient(socket_path)
    # Sidecar writes its crash trace beside the socket (fixed file name).
    faulthandler_log = os.path.join(
        os.path.dirname(socket_path), "telegram_sidecar.faulthandler.log"
    )
    try:
        await client.connect(connect_timeout_s=10.0, ready_timeout_s=10.0)
        ended: asyncio.Queue[tuple[int, str]] = asyncio.Queue()

        async def on_ended(uid: int, reason: str) -> None:
            await ended.put((uid, reason))

        uid = await client.place_call(_contact())
        client.register_callbacks(None, on_ended)

        # The durable faulthandler file is opened at sidecar startup.
        assert os.path.exists(faulthandler_log)

        proc.kill()  # simulate a native SIGSEGV

        recv_uid, reason = await asyncio.wait_for(ended.get(), 5.0)
        assert recv_uid == uid
        assert reason == "sidecar_crashed"
        assert client.bridge_ended_event.is_set()
    finally:
        await client.stop()
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5.0)
        for path in (socket_path, faulthandler_log):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)


def test_drain_crash_log_reads_new_bytes_once(tmp_path) -> None:
    sup = SidecarSupervisor(str(tmp_path / "s.sock"))
    sup._crash_log_path.write_text(
        "Fatal Python error: Segmentation fault\n  ntgcalls...\n"
    )

    sup._drain_crash_log()
    advanced = sup._crash_log_offset
    assert advanced > 0

    # Nothing new the second time.
    sup._drain_crash_log()
    assert sup._crash_log_offset == advanced
