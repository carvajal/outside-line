"""Supervised sidecar that hosts the ntgcalls voice stack out of process (ADR 0017).

Run as ``python -m outside_line.telegram_sidecar --socket PATH [--echo]``.

The FastAPI process (via ``TelegramBridgeClient``) drives this over two
Unix-domain stream connections — CONTROL (RPCs + events) and AUDIO (the hot
path). This process hosts the *real* ``TelegramBridge`` unchanged, so a native
SIGSEGV in ntgcalls (ntgcalls#51) kills only this child; the parent survives
and re-spawns us.

``--echo`` swaps the real bridge for an in-memory ``EchoBridge`` that needs no
Telegram creds — used to prove the transport before real ntgcalls goes across
the wire.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import hashlib
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from . import telegram_ipc as ipc
from .config import settings
from .contacts import Contact
from .log import get_logger, setup_logging

log = get_logger(__name__)

# Native crash trace lands here (beside the socket, on the /app/data volume).
# The supervisor reads it on child death; kept in sync with
# telegram_supervisor.CRASH_LOG_NAME.
CRASH_LOG_NAME = "telegram_sidecar.faulthandler.log"

OnContactAudio = Callable[[int, bytes], Awaitable[None]]
OnBridgeEnded = Callable[[int, str], Awaitable[None]]


class BridgeLike(Protocol):
    """The duck-typed surface the sidecar hosts (TelegramBridge or EchoBridge)."""

    bridge_ended_event: asyncio.Event

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def place_call(self, contact: Contact, *, timeout_s: int = 60) -> int: ...
    async def send_to_contact(
        self, user_id: int, pcm48k: bytes, capture_time_ms: int | None = None
    ) -> None: ...
    async def hangup(self, user_id: int) -> None: ...
    async def discard_outgoing_call(self, user_id: int) -> None: ...
    async def resolve_user_id(self, contact: Contact) -> int: ...
    async def send_text(self, contact: Contact, message: str) -> int: ...
    def register_callbacks(
        self,
        on_contact_audio: OnContactAudio | None,
        on_bridge_ended: OnBridgeEnded | None,
        *,
        bridge_metrics: Any = None,
    ) -> None: ...


def _fake_uid(contact: Contact) -> int:
    """Deterministic >2**32 user id from a username (exercises the u64 path)."""
    digest = hashlib.sha1(contact.telegram_username.encode()).digest()
    return int.from_bytes(digest[:5], "big")


class EchoBridge:
    """In-memory fake bridge for transport tests — no Telegram.

    ``place_call``/``resolve_user_id`` return a deterministic uid; each
    ``send_to_contact`` frame is echoed straight back through
    ``on_contact_audio``; ``place_call`` for username ``@boom`` raises so RPC
    error propagation can be tested.
    """

    def __init__(self) -> None:
        self.bridge_ended_event = asyncio.Event()
        self._on_contact_audio: OnContactAudio | None = None
        self._on_bridge_ended: OnBridgeEnded | None = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def register_callbacks(
        self,
        on_contact_audio: OnContactAudio | None,
        on_bridge_ended: OnBridgeEnded | None,
        *,
        bridge_metrics: Any = None,
    ) -> None:
        self._on_contact_audio = on_contact_audio
        self._on_bridge_ended = on_bridge_ended

    async def resolve_user_id(self, contact: Contact) -> int:
        return _fake_uid(contact)

    async def place_call(self, contact: Contact, *, timeout_s: int = 60) -> int:
        if contact.telegram_username == "@boom":
            raise RuntimeError("simulated place_call failure")
        return _fake_uid(contact)

    async def send_to_contact(
        self, user_id: int, pcm48k: bytes, capture_time_ms: int | None = None
    ) -> None:
        cb = self._on_contact_audio
        if cb is not None:
            await cb(user_id, pcm48k)

    async def hangup(self, user_id: int) -> None:
        return None

    async def discard_outgoing_call(self, user_id: int) -> None:
        return None

    async def send_text(self, contact: Contact, message: str) -> int:
        return 12345


class SidecarServer:
    """Listens on the UDS, marshals IPC ⇄ bridge method calls / callbacks."""

    def __init__(self, bridge: BridgeLike, socket_path: str) -> None:
        self._bridge = bridge
        self._socket_path = socket_path
        self._control_writer: asyncio.StreamWriter | None = None
        self._audio_writer: asyncio.StreamWriter | None = None
        self._control_ready = asyncio.Event()
        self._audio_ready = asyncio.Event()
        self._server: asyncio.Server | None = None

    async def serve_forever(self) -> None:
        Path(self._socket_path).parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._socket_path)
        self._server = await asyncio.start_unix_server(
            self._on_connection, path=self._socket_path
        )
        log.info("telegram.sidecar.listening", socket=self._socket_path)

        # Wait for the proxy to bring up both channels before we register
        # callbacks + start the bridge — so a callback can never fire before a
        # writer exists.
        await self._control_ready.wait()
        await self._audio_ready.wait()
        self._bridge.register_callbacks(
            self._emit_contact_audio, self._emit_bridge_ended
        )
        await self._bridge.start()
        self._send_control(ipc.encode_frame(ipc.MSG_EVENT_READY))
        log.info("telegram.sidecar.ready")

        # NB: not `async with self._server` — its __aexit__ awaits
        # wait_closed(), which blocks on lingering connection transports.
        # Shutdown is driven by cancelling this task + close() below.
        await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()  # stop accepting; do NOT wait_closed (hangs)
        with contextlib.suppress(Exception):
            await self._bridge.stop()

    # --- connection intake ---

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            hello = await reader.readexactly(1)
        except asyncio.IncompleteReadError:
            writer.close()
            return
        channel = hello[0]
        try:
            if channel == ipc.CH_CONTROL:
                self._control_writer = writer
                self._control_ready.set()
                await self._control_loop(reader)
            elif channel == ipc.CH_AUDIO:
                self._audio_writer = writer
                self._audio_ready.set()
                await self._audio_loop(reader)
            else:
                log.warning("telegram.sidecar.bad_channel_hello", byte=channel)
        finally:
            # Close our end when the peer goes away, so the server's
            # connection set drains and shutdown isn't wedged.
            with contextlib.suppress(Exception):
                writer.close()

    async def _control_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                frame = await ipc.read_frame(reader)
                if frame is None:
                    break
                msg_type, body = frame
                if msg_type == ipc.MSG_RPC_REQUEST:
                    request_id, method, args = ipc.decode_rpc_request(body)
                    # Dispatch concurrently: a blocking place_call (awaiting
                    # pickup for up to timeout_s) must NOT block a concurrent
                    # discard_outgoing_call/hangup — that's the cancel-mid-ring
                    # path directlines relies on.
                    asyncio.create_task(self._dispatch_rpc(request_id, method, args))
                elif msg_type == ipc.MSG_PING:
                    self._send_control(ipc.encode_frame(ipc.MSG_PONG))
        except ipc.IpcProtocolError, ConnectionError, OSError:
            pass

    async def _audio_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                frame = await ipc.read_frame(reader)
                if frame is None:
                    break
                msg_type, body = frame
                if msg_type == ipc.MSG_AUDIO_TO_CONTACT:
                    uid, pcm, cap = ipc.decode_audio_to_contact(body)
                    try:
                        await self._bridge.send_to_contact(uid, pcm, cap)
                    except Exception:
                        log.exception("telegram.sidecar.send_to_contact_failed")
        except ipc.IpcProtocolError, ConnectionError, OSError:
            pass

    async def _dispatch_rpc(self, request_id: int, method: str, args: dict) -> None:
        try:
            result = await self._call_method(method, args)
            self._send_control(ipc.encode_rpc_ok(request_id, result))
        except Exception as exc:
            log.exception("telegram.sidecar.rpc_failed", method=method)
            self._send_control(
                ipc.encode_rpc_err(request_id, type(exc).__name__, str(exc))
            )

    async def _call_method(self, method: str, args: dict) -> Any:
        if method == "__debug_crash":
            # Verification hook: SIGSEGV this process so we can prove the
            # supervisor + apology path against a real native crash. Gated by
            # an env flag so it's inert in prod (ADR 0017 §Verification).
            if os.environ.get("SIDECAR_DEBUG_CRASH") == "1":
                log.warning("telegram.sidecar.debug_crash_requested")
                os.kill(os.getpid(), signal.SIGSEGV)
            raise ValueError("debug crash not enabled")
        if method == "place_call":
            return await self._bridge.place_call(
                ipc.contact_from_dict(args["contact"]), timeout_s=args["timeout_s"]
            )
        if method == "resolve_user_id":
            return await self._bridge.resolve_user_id(
                ipc.contact_from_dict(args["contact"])
            )
        if method == "send_text":
            return await self._bridge.send_text(
                ipc.contact_from_dict(args["contact"]), args["message"]
            )
        if method == "hangup":
            await self._bridge.hangup(args["user_id"])
            return None
        if method == "discard_outgoing_call":
            await self._bridge.discard_outgoing_call(args["user_id"])
            return None
        raise ValueError(f"unknown RPC method {method!r}")

    # --- bridge callbacks → IPC (run on the sidecar loop, per pytgcalls) ---

    async def _emit_contact_audio(self, uid: int, pcm: bytes) -> None:
        writer = self._audio_writer
        if writer is not None:
            writer.write(ipc.encode_audio_from_contact(uid, pcm))

    async def _emit_bridge_ended(self, uid: int, reason: str) -> None:
        self._send_control(ipc.encode_bridge_ended(uid, reason))

    def _send_control(self, frame: bytes) -> None:
        writer = self._control_writer
        if writer is not None:
            writer.write(frame)


def _build_real_bridge() -> BridgeLike:
    """Construct the real Telethon + PyTgCalls bridge, hosted in this sidecar.

    Moved verbatim from the pre-ADR-0017 in-process main.py lifespan. Imports
    are lazy so the echo path (and anything importing this module for its
    constants) never loads the native ntgcalls lib.
    """
    from pytgcalls import PyTgCalls

    from .telegram_bridge import TelegramBridge, make_telethon

    telethon = make_telethon()
    call_py = PyTgCalls(telethon)
    return TelegramBridge(telethon, call_py)


async def _run(make_bridge: Callable[[], BridgeLike], socket_path: str) -> None:
    # Build the bridge HERE, under asyncio.run's loop. PyTgCalls captures
    # asyncio.get_event_loop() at construction (pytgcalls.py:42); building it
    # before the loop exists pinned every ntgcalls future to the wrong loop, so
    # place_call raised "Future attached to a different loop" (ADR 0017).
    bridge = make_bridge()
    server = SidecarServer(bridge, socket_path)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    serve_task = loop.create_task(server.serve_forever())
    stop_task = loop.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (serve_task, stop_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await server.close()
    log.info("telegram.sidecar.stopped")


# Module-level so the crash-log file object isn't GC'd (which would close the
# fd faulthandler writes the native trace to).
_CRASH_FH = None


def _enable_faulthandler(socket_path: str) -> None:
    """Dump all-thread tracebacks on SIGSEGV/SIGFPE/SIGABRT to a DURABLE file.

    The native ntgcalls crash is the whole reason this process exists; routing
    the trace to a file beside the socket (on the Railway /app/data volume,
    ADR 0004) means it survives the respawn — the supervisor reads and re-logs
    it to the host logs on child death (durable crash capture, ADR 0017).
    Falls back to stderr if the volume isn't writable.
    """
    global _CRASH_FH
    crash_path = Path(socket_path).with_name(CRASH_LOG_NAME)
    try:
        _CRASH_FH = open(crash_path, "a", buffering=1)
        faulthandler.enable(file=_CRASH_FH, all_threads=True)
    except OSError:
        faulthandler.enable(all_threads=True)


def run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="outside_line.telegram_sidecar")
    parser.add_argument("--socket", required=True, help="Unix-domain socket path")
    parser.add_argument(
        "--echo", action="store_true", help="use the in-memory EchoBridge (no Telegram)"
    )
    args = parser.parse_args(argv)

    setup_logging(settings.log_level)
    _enable_faulthandler(args.socket)
    make_bridge: Callable[[], BridgeLike] = (
        EchoBridge if args.echo else _build_real_bridge
    )
    log.info("telegram.sidecar.start", echo=args.echo, socket=args.socket)
    asyncio.run(_run(make_bridge, args.socket))


if __name__ == "__main__":
    run()
