"""Tests for the sidecar supervisor (ADR 0017).

The happy path + respawn use the real --echo sidecar over a real socket (with
sub-second probe intervals). The crash-loop → degraded transition is unit-tested
by driving ``_respawn`` with a failing spawn, so it needs no timing.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile

import pytest

from outside_line.contacts import Contact
from outside_line.telegram_bridge_client import SidecarDied
from outside_line.telegram_supervisor import SidecarSupervisor


def _socket_path() -> str:
    return tempfile.mktemp(prefix="rc_sup_", suffix=".sock", dir="/tmp")


def _contact(username: str = "@marco_example") -> Contact:
    return Contact(
        first_name="Marco", aliases=(), telegram_username=username, keywords=()
    )


def _fast_supervisor(socket_path: str) -> SidecarSupervisor:
    return SidecarSupervisor(
        socket_path,
        echo=True,
        probe_interval_s=0.1,
        probe_timeout_s=0.5,
        probe_fail_k=2,
        first_boot_timeout_s=5.0,
        child_term_grace_s=1.0,
    )


@pytest.mark.asyncio
async def test_supervisor_starts_connects_and_stops() -> None:
    socket_path = _socket_path()
    sup = _fast_supervisor(socket_path)
    try:
        await sup.start()
        uid = await sup.client.place_call(_contact())
        assert uid > 0
        assert sup._proc is not None and sup._proc.returncode is None
    finally:
        await sup.stop()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(socket_path)
    # After stop, the child is gone.
    assert sup._proc is None


@pytest.mark.asyncio
async def test_supervisor_respawns_after_child_death() -> None:
    socket_path = _socket_path()
    sup = _fast_supervisor(socket_path)
    try:
        await sup.start()
        await sup.client.place_call(_contact())
        old_pid = sup._proc.pid

        sup._proc.kill()  # simulate a sidecar crash

        # The monitor should respawn a fresh child and reconnect the client.
        async def _healed() -> bool:
            for _ in range(100):  # up to ~10s
                proc = sup._proc
                if proc is not None and proc.pid != old_pid and proc.returncode is None:
                    with contextlib.suppress(SidecarDied, Exception):
                        await sup.client.place_call(_contact())
                        return True
                await asyncio.sleep(0.1)
            return False

        assert await _healed()
        assert not sup.degraded
    finally:
        await sup.stop()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(socket_path)


@pytest.mark.asyncio
async def test_crash_loop_enters_degraded_mode(monkeypatch) -> None:
    sup = SidecarSupervisor(
        "/tmp/rc_never.sock", echo=True, respawn_max=3, respawn_window_s=60.0
    )

    async def _always_fail() -> None:
        raise RuntimeError("simulated respawn failure")

    monkeypatch.setattr(sup, "_spawn_and_connect", _always_fail)

    # respawn_max-1 respawns keep trying; the respawn_max'th trips the guard.
    await sup._respawn()
    assert not sup.degraded
    await sup._respawn()
    assert not sup.degraded
    await sup._respawn()
    assert sup.degraded  # crash loop → give up, leave sidecar down

    # In degraded mode the client is disconnected, so bridge calls raise.
    with pytest.raises(SidecarDied):
        await sup.client.resolve_user_id(_contact())
