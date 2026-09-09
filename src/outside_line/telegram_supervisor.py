"""Supervises the Telegram-bridge sidecar process (ADR 0017).

Async, in-process (runs inside the FastAPI lifespan) — unlike the standalone
``scripts/dev_serve.py`` supervisor it borrows its shape from. It spawns the
sidecar, connects a ``TelegramBridgeClient`` to it, and monitors liveness over
the UDS control channel (PING/PONG) plus child exit. On death or
unresponsiveness it respawns and reconnects; a crash loop (``respawn_max``
respawns within ``respawn_window_s``) drops into **degraded mode** — the
sidecar is left down and every bridge call raises, but the FastAPI app stays up
serving the agent (``call_contact`` / ``message_contact`` just fail gracefully).

``supervisor.client`` is the drop-in that goes on ``app.state.telegram_bridge``.
Reconnects happen in place on that same client object, so the reference
twilio_handler holds stays valid across a respawn.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import signal
import sys
import time
from pathlib import Path

from .log import get_logger
from .telegram_bridge_client import TelegramBridgeClient
from .telegram_sidecar import CRASH_LOG_NAME

log = get_logger(__name__)

# Tunables — mirror scripts/dev_serve.py. Overridable via the constructor so
# tests can run with sub-second intervals.
FIRST_BOOT_TIMEOUT_S = 15.0
PROBE_INTERVAL_S = 2.0
PROBE_TIMEOUT_S = 1.0
PROBE_FAIL_K = 3  # consecutive ping fails before respawn
RESPAWN_MAX = 5  # respawns allowed within RESPAWN_WINDOW_S
RESPAWN_WINDOW_S = 60.0
CHILD_TERM_GRACE_S = 2.0  # SIGTERM grace before SIGKILL

_PR_SET_PDEATHSIG = 1  # linux prctl option


def _linux_pdeathsig() -> None:
    """preexec_fn: ask the kernel to SIGTERM this child if the parent dies.

    Prevents an orphaned sidecar holding the Telethon session lock after an
    ungraceful uvicorn death. Linux-only; a no-op elsewhere.
    """
    if sys.platform != "linux":
        return
    with contextlib.suppress(Exception):
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


class SidecarSupervisor:
    def __init__(
        self,
        socket_path: str,
        *,
        echo: bool = False,
        probe_interval_s: float = PROBE_INTERVAL_S,
        probe_timeout_s: float = PROBE_TIMEOUT_S,
        probe_fail_k: int = PROBE_FAIL_K,
        first_boot_timeout_s: float = FIRST_BOOT_TIMEOUT_S,
        respawn_max: int = RESPAWN_MAX,
        respawn_window_s: float = RESPAWN_WINDOW_S,
        child_term_grace_s: float = CHILD_TERM_GRACE_S,
    ) -> None:
        self._socket_path = socket_path
        self._echo = echo
        self._probe_interval_s = probe_interval_s
        self._probe_timeout_s = probe_timeout_s
        self._probe_fail_k = probe_fail_k
        self._first_boot_timeout_s = first_boot_timeout_s
        self._respawn_max = respawn_max
        self._respawn_window_s = respawn_window_s
        self._child_term_grace_s = child_term_grace_s

        self.client = TelegramBridgeClient(socket_path)
        self.degraded = False
        self._proc: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._respawn_times: list[float] = []
        self._stopping = False
        # Durable native-crash trace the sidecar writes; we tail the new bytes
        # into the host logs on child death (durable crash capture, ADR 0017).
        self._crash_log_path = Path(socket_path).with_name(CRASH_LOG_NAME)
        self._crash_log_offset = 0

    async def start(self) -> None:
        """Spawn the sidecar, connect the client, and begin monitoring.

        Raises if the very first boot fails — the caller (lifespan) treats that
        like today's ``bridge.start()`` failure (bridge stays None / degraded).
        """
        await self._spawn_and_connect()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        log.info("telegram.sidecar.supervisor_started", socket=self._socket_path)

    async def stop(self) -> None:
        self._stopping = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._monitor_task
        with contextlib.suppress(Exception):
            await self.client.stop()
        await self._kill_child()
        log.info("telegram.sidecar.supervisor_stopped")

    # --- spawn / kill ------------------------------------------------------

    async def _spawn_sidecar(self) -> asyncio.subprocess.Process:
        argv = [
            sys.executable,
            "-m",
            "outside_line.telegram_sidecar",
            "--socket",
            self._socket_path,
        ]
        if self._echo:
            argv.append("--echo")
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # own process group; killpg targets only it
            preexec_fn=_linux_pdeathsig,  # SIGTERM the child if the parent dies
        )

    async def _spawn_and_connect(self) -> None:
        self._proc = await self._spawn_sidecar()
        try:
            await self.client.connect(
                connect_timeout_s=self._first_boot_timeout_s,
                ready_timeout_s=self._first_boot_timeout_s,
            )
        except Exception:
            await self._kill_child()
            raise

    async def _kill_child(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), self._child_term_grace_s)
        except Exception:
            # Grace elapsed (TimeoutError) or wait failed — escalate to SIGKILL.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2.0)

    # --- monitor / respawn -------------------------------------------------

    async def _monitor_loop(self) -> None:
        consecutive_fails = 0
        while not self._stopping:
            await asyncio.sleep(self._probe_interval_s)
            if self._stopping:
                break

            died = self._proc is not None and self._proc.returncode is not None
            if not died and await self.client.ping(self._probe_timeout_s):
                consecutive_fails = 0
                continue

            if not died:
                consecutive_fails += 1
                if consecutive_fails < self._probe_fail_k:
                    continue

            reason = (
                f"child_exited(rc={self._proc.returncode})"
                if died
                else f"unresponsive({self._probe_fail_k}x)"
            )
            log.warning("telegram.sidecar.respawning", reason=reason)
            await self._respawn()
            consecutive_fails = 0
            if self.degraded:
                break

    def _drain_crash_log(self) -> None:
        """Read any new bytes the sidecar wrote to its faulthandler file and
        re-log them, so a native crash trace shows up in Railway (not just on
        the volume). No-op on a mere unresponsive respawn (nothing new)."""
        try:
            if not self._crash_log_path.exists():
                return
            with open(self._crash_log_path, errors="replace") as fh:
                fh.seek(self._crash_log_offset)
                new = fh.read()
                self._crash_log_offset = fh.tell()
        except OSError:
            return
        if new.strip():
            log.error("telegram.sidecar.crash_trace", trace=new.strip()[-4000:])

    async def _respawn(self) -> None:
        self._respawn_times.append(time.monotonic())
        self._drain_crash_log()
        await self._kill_child()
        await self.client._reset_for_reconnect()

        if self._is_crash_loop():
            self.degraded = True
            log.error(
                "telegram.sidecar.crash_loop_giving_up",
                respawns=len(self._respawn_times),
                window_s=self._respawn_window_s,
            )
            return

        try:
            await self._spawn_and_connect()
            log.info("telegram.sidecar.respawned", respawns=len(self._respawn_times))
        except Exception:
            # Reconnect failed — leave it disconnected; the next monitor tick
            # sees the dead child and retries, counting toward the crash loop.
            log.exception("telegram.sidecar.respawn_failed")

    def _is_crash_loop(self) -> bool:
        if len(self._respawn_times) < self._respawn_max:
            return False
        window = self._respawn_times[-self._respawn_max :]
        return (window[-1] - window[0]) <= self._respawn_window_s
