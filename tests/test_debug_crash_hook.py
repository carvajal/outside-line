"""Debug crash-sidecar hook (ADR 0017 §Verification, commit 7).

Proves the success criterion in CI with the echo sidecar: a real SIGSEGV of the
sidecar (via the env-gated __debug_crash RPC) drops the connection, the proxy
synthesizes "sidecar_crashed", and the supervisor is still alive to respawn.
Also pins that the endpoint is invisible (404) when the flag is unset.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from outside_line import main as main_mod
from outside_line.contacts import Contact
from outside_line.telegram_supervisor import SidecarSupervisor


def _socket_path() -> str:
    return tempfile.mktemp(prefix="rc_dbg_", suffix=".sock", dir="/tmp")


def _contact(username: str = "@marco_example") -> Contact:
    return Contact(
        first_name="Marco", aliases=(), telegram_username=username, keywords=()
    )


def test_crash_endpoint_is_404_when_flag_unset(monkeypatch) -> None:
    monkeypatch.setattr(main_mod.settings, "telegram_api_id", None)  # skip sidecar
    monkeypatch.delenv("SIDECAR_DEBUG_CRASH", raising=False)
    with TestClient(main_mod.app) as client:
        resp = client.post("/debug/crash-sidecar")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_debug_crash_rpc_segfaults_sidecar_and_synthesizes(monkeypatch) -> None:
    monkeypatch.setenv("SIDECAR_DEBUG_CRASH", "1")
    socket_path = _socket_path()
    faulthandler_log = os.path.join(
        os.path.dirname(socket_path), "telegram_sidecar.faulthandler.log"
    )
    sup = SidecarSupervisor(
        socket_path,
        echo=True,
        probe_interval_s=0.1,
        probe_timeout_s=0.5,
        probe_fail_k=2,
        first_boot_timeout_s=5.0,
        child_term_grace_s=1.0,
    )
    try:
        await sup.start()
        uid = await sup.client.place_call(_contact())
        crashed_pid = sup._proc.pid
        ended: asyncio.Queue[tuple[int, str]] = asyncio.Queue()

        async def on_ended(u: int, r: str) -> None:
            await ended.put((u, r))

        sup.client.register_callbacks(None, on_ended)

        await sup.client.trigger_sidecar_crash()

        recv_uid, reason = await asyncio.wait_for(ended.get(), 5.0)
        assert (recv_uid, reason) == (uid, "sidecar_crashed")

        # The child really died of the signal (returncode is negative SIGSEGV).
        async def _child_replaced() -> bool:
            for _ in range(100):
                if sup._proc is not None and sup._proc.pid != crashed_pid:
                    return True
                await asyncio.sleep(0.1)
            return False

        assert await _child_replaced()
        assert not sup.degraded
    finally:
        await sup.stop()
        for path in (socket_path, faulthandler_log):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
