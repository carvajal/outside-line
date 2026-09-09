"""In-process proxy that speaks the TelegramBridge public surface but drives a
sidecar process over UDS (ADR 0017).

``app.state.telegram_bridge`` becomes a ``TelegramBridgeClient`` instead of a
real ``TelegramBridge``, so ``twilio_handler`` / ``directlines`` are unchanged —
they still call ``place_call`` / ``send_to_contact`` / ``hangup`` and await
``bridge_ended_event``. The difference is that the crash-prone ntgcalls code
now lives in a child process; a native SIGSEGV there costs one bridge attempt,
not the whole FastAPI server.

"Mirror bridge API" (the chosen IPC design): all soxr resampling stays on this
(FastAPI) side exactly as today, so only ready-to-send PCM48 frames cross the
wire and ADR 0009's 10 ms / 960-byte framing invariant is preserved by
construction. Two Unix-domain stream connections are used — a CONTROL channel
(RPCs + async events) and an AUDIO channel (the ~100-200 fps hot path) — so an
audio flood can never head-of-line-block an RPC reply.

Per-call state (``bridge_ended_event``, the registered callbacks,
``_current_user_id``) is **local** to this proxy — closures never cross the
wire. The sidecar has its own fixed callbacks that just serialize frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from . import telegram_ipc as ipc
from ._latency import BridgeMetrics
from .contacts import Contact
from .log import get_logger

log = get_logger(__name__)

OnContactAudio = Callable[[int, bytes], Awaitable[None]]
OnBridgeEnded = Callable[[int, str], Awaitable[None]]

# Wall-clock slack added on top of an RPC's own ``timeout_s`` before the proxy
# gives up on a wedged sidecar. The sidecar enforces the real timeout; this is
# only a dead-man's switch so the sidecar's own error normally wins.
_RPC_SLACK_S = 10.0
# Timeout for control RPCs that don't carry their own (hangup, resolve, …).
_DEFAULT_RPC_TIMEOUT_S = 30.0


class BridgeRPCError(Exception):
    """Re-raised on the proxy side when the sidecar returns an RPC error.

    Callers already ``except Exception`` broadly, so preserving the remote
    ``type: message`` in the string is enough — we don't reconstruct the exact
    exception class across the wire.
    """


class SidecarDied(Exception):
    """The sidecar connection dropped (crash / kill) — an in-flight RPC or the
    initial ready handshake could not complete."""


class TelegramBridgeClient:
    """Duck-typed drop-in for ``TelegramBridge`` backed by a sidecar process."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._loop: asyncio.AbstractEventLoop | None = None
        self._control_reader: asyncio.StreamReader | None = None
        self._control_writer: asyncio.StreamWriter | None = None
        self._audio_reader: asyncio.StreamReader | None = None
        self._audio_writer: asyncio.StreamWriter | None = None
        self._reader_tasks: list[asyncio.Task[None]] = []
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._next_request_id = 1
        self._ready: asyncio.Future[None] | None = None
        self._closing = False
        # Liveness: the supervisor sends MSG_PING; the sidecar answers PONG.
        # A single event is enough because pings are issued one at a time.
        self._pong_event = asyncio.Event()

        # --- Local per-call state (mirrors TelegramBridge; never on the wire) ---
        self._current_user_id: int | None = None
        self._on_contact_audio: OnContactAudio | None = None
        self._on_bridge_ended: OnBridgeEnded | None = None
        self._bridge_metrics: BridgeMetrics | None = None
        self._incoming_frame_logged = False
        # Last reason the bridge ended — lets the BRIDGE phase tell a normal
        # contact hangup ("LEFT_CALL") from a sidecar crash ("sidecar_crashed").
        self._last_bridge_ended_reason: str | None = None
        self.bridge_ended_event = asyncio.Event()

    # --- lifecycle ---------------------------------------------------------

    async def connect(
        self, *, connect_timeout_s: float = 10.0, ready_timeout_s: float = 30.0
    ) -> None:
        """Connect both channels to an already-listening sidecar, await READY.

        Retries the initial connect for ``connect_timeout_s`` so the caller
        (or the SidecarSupervisor) can spawn the sidecar and connect without a
        hand-rolled "wait for socket to appear" loop.
        """
        self._loop = asyncio.get_running_loop()
        deadline = self._loop.time() + connect_timeout_s
        while True:
            try:
                (
                    self._control_reader,
                    self._control_writer,
                ) = await asyncio.open_unix_connection(self._socket_path)
                break
            except FileNotFoundError, ConnectionRefusedError:
                if self._loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.05)

        self._audio_reader, self._audio_writer = await asyncio.open_unix_connection(
            self._socket_path
        )
        # Tag each connection so the sidecar attaches the right loop.
        self._control_writer.write(bytes((ipc.CH_CONTROL,)))
        self._audio_writer.write(bytes((ipc.CH_AUDIO,)))
        await self._control_writer.drain()
        await self._audio_writer.drain()

        self._ready = self._loop.create_future()
        self._reader_tasks = [
            self._loop.create_task(self._control_reader_loop()),
            self._loop.create_task(self._audio_reader_loop()),
        ]
        await asyncio.wait_for(self._ready, ready_timeout_s)
        log.info("telegram.bridge_client.ready", socket=self._socket_path)

    async def start(self) -> None:
        """API-compat with ``TelegramBridge.start``.

        In production the ``SidecarSupervisor`` spawns the process and calls
        :meth:`connect` (it owns the spawn+respawn lifecycle). This bare
        ``start`` just connects to whatever is already listening.
        """
        await self.connect()

    async def stop(self) -> None:
        """Close both channels and cancel the reader loops."""
        self._closing = True
        for writer in (self._control_writer, self._audio_writer):
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
        for task in self._reader_tasks:
            task.cancel()
        for task in self._reader_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        log.info("telegram.bridge_client.stopped")

    async def ping(self, timeout: float = 1.0) -> bool:
        """Liveness probe for the SidecarSupervisor. True iff a PONG returns
        within ``timeout``."""
        writer = self._control_writer
        if writer is None or self._closing:
            return False
        self._pong_event.clear()
        try:
            writer.write(ipc.encode_frame(ipc.MSG_PING))
        except Exception:
            return False
        try:
            await asyncio.wait_for(self._pong_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def trigger_sidecar_crash(self) -> None:
        """Verification hook (ADR 0017): ask the sidecar to SIGSEGV itself.

        Fire-and-forget — the sidecar dies without replying, so we don't
        register a pending future. The dropped connection then drives the
        normal crash path (synthesized ``sidecar_crashed`` + supervisor
        respawn). Only honored sidecar-side when SIDECAR_DEBUG_CRASH=1.
        """
        writer = self._control_writer
        if writer is None or self._closing:
            raise SidecarDied("control channel not connected")
        request_id = self._next_request_id
        self._next_request_id += 1
        writer.write(ipc.encode_rpc_request(request_id, "__debug_crash", {}))

    async def _reset_for_reconnect(self) -> None:
        """Tear down the current connection so :meth:`connect` can re-run against
        a freshly respawned sidecar (called by the supervisor on respawn).

        Not ``stop`` — ``_closing`` stays False so the reconnected client is
        live again.
        """
        for task in self._reader_tasks:
            task.cancel()
        for task in self._reader_tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._reader_tasks = []
        for writer in (self._control_writer, self._audio_writer):
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
        self._control_reader = self._control_writer = None
        self._audio_reader = self._audio_writer = None
        for request_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(SidecarDied("sidecar respawning"))
            self._pending.pop(request_id, None)

    # --- public bridge surface (RPCs) --------------------------------------

    def register_callbacks(
        self,
        on_contact_audio: OnContactAudio | None,
        on_bridge_ended: OnBridgeEnded | None,
        *,
        bridge_metrics: BridgeMetrics | None = None,
    ) -> None:
        """Last-writer-wins per-call hookup — identical semantics to the real
        bridge. Closures stay local to this process."""
        self._on_contact_audio = on_contact_audio
        self._on_bridge_ended = on_bridge_ended
        self._bridge_metrics = bridge_metrics

    async def place_call(self, contact: Contact, *, timeout_s: int = 60) -> int:
        # Clear before placing so the bridge phase only sees the post-place_call
        # edge (mirrors TelegramBridge.place_call).
        self.bridge_ended_event.clear()
        self._current_user_id = None
        self._incoming_frame_logged = False
        uid = await self._rpc(
            "place_call",
            {"contact": ipc.contact_to_dict(contact), "timeout_s": timeout_s},
            timeout=timeout_s + _RPC_SLACK_S,
        )
        self._current_user_id = uid
        return uid

    async def resolve_user_id(self, contact: Contact) -> int:
        return await self._rpc(
            "resolve_user_id",
            {"contact": ipc.contact_to_dict(contact)},
            timeout=_DEFAULT_RPC_TIMEOUT_S,
        )

    async def send_text(self, contact: Contact, message: str) -> int:
        return await self._rpc(
            "send_text",
            {"contact": ipc.contact_to_dict(contact), "message": message},
            timeout=_DEFAULT_RPC_TIMEOUT_S,
        )

    async def hangup(self, user_id: int) -> None:
        try:
            await self._rpc(
                "hangup", {"user_id": user_id}, timeout=_DEFAULT_RPC_TIMEOUT_S
            )
        finally:
            if self._current_user_id == user_id:
                self._current_user_id = None

    async def discard_outgoing_call(self, user_id: int) -> None:
        await self._rpc(
            "discard_outgoing_call",
            {"user_id": user_id},
            timeout=_DEFAULT_RPC_TIMEOUT_S,
        )

    async def send_to_contact(
        self, user_id: int, pcm48k: bytes, capture_time_ms: int | None = None
    ) -> None:
        """Hot path: fire-and-forget one PCM frame onto the audio channel.

        Buffered write, no per-frame drain — UDS loopback drains far faster
        than the ~200 fps we push, so this stays close to the in-process cost.
        """
        writer = self._audio_writer
        if writer is None or self._closing:
            return
        writer.write(ipc.encode_audio_to_contact(user_id, pcm48k, capture_time_ms))

    # --- RPC plumbing ------------------------------------------------------

    async def _rpc(self, method: str, args: dict, *, timeout: float):
        writer = self._control_writer
        if writer is None or self._closing:
            raise SidecarDied(f"control channel not connected for {method!r}")
        assert self._loop is not None
        request_id = self._next_request_id
        self._next_request_id += 1
        fut: asyncio.Future[dict] = self._loop.create_future()
        self._pending[request_id] = fut
        writer.write(ipc.encode_rpc_request(request_id, method, args))
        try:
            obj = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(request_id, None)
        if not obj.get("ok"):
            err = obj.get("error") or {}
            raise BridgeRPCError(f"{err.get('type', 'Error')}: {err.get('msg', '')}")
        return obj.get("result")

    # --- reader loops ------------------------------------------------------

    async def _control_reader_loop(self) -> None:
        assert self._control_reader is not None
        try:
            while True:
                frame = await ipc.read_frame(self._control_reader)
                if frame is None:
                    break
                msg_type, body = frame
                if msg_type == ipc.MSG_RPC_RESPONSE:
                    request_id, obj = ipc.decode_rpc_response(body)
                    fut = self._pending.get(request_id)
                    if fut is not None and not fut.done():
                        fut.set_result(obj)
                elif msg_type == ipc.MSG_EVENT_BRIDGE_ENDED:
                    uid, reason = ipc.decode_bridge_ended(body)
                    await self._handle_bridge_ended(uid, reason)
                elif msg_type == ipc.MSG_EVENT_READY:
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(None)
                elif msg_type == ipc.MSG_PONG:
                    self._pong_event.set()
        except ipc.IpcProtocolError, ConnectionError, OSError:
            pass
        finally:
            self._on_connection_lost()

    async def _audio_reader_loop(self) -> None:
        assert self._audio_reader is not None
        try:
            while True:
                frame = await ipc.read_frame(self._audio_reader)
                if frame is None:
                    break
                msg_type, body = frame
                if msg_type == ipc.MSG_AUDIO_FROM_CONTACT:
                    uid, pcm = ipc.decode_audio_from_contact(body)
                    await self._handle_contact_audio(uid, pcm)
        except ipc.IpcProtocolError, ConnectionError, OSError:
            pass
        finally:
            self._on_connection_lost()

    # --- inbound event handling (mirrors TelegramBridge handlers) ----------

    async def _handle_contact_audio(self, uid: int, pcm: bytes) -> None:
        cb = self._on_contact_audio
        if cb is None or self._current_user_id is None or uid != self._current_user_id:
            return
        if not pcm:
            return
        now_ns = time.monotonic_ns()
        bm = self._bridge_metrics
        if bm is not None:
            bm.tg_callback_interval.tick(now_ns)
            bm.tg_callbacks_total += 1
            # One 960 B frame per IPC message, so frames-per-callback is
            # trivially 1 (a metrics-shape change vs the in-process bridge,
            # which concatenated sub-frames — the load-bearing signal is
            # frames_b, which still counts).
            bm.tg_frames_per_callback.add_ms(1.0)
            bm.tg_bytes_per_callback.add_ms(float(len(pcm)))
        if not self._incoming_frame_logged:
            self._incoming_frame_logged = True
            if bm is not None and bm.first_frame_ns == 0:
                bm.first_frame_ns = now_ns
                ring_to_bridge_ms: float | None = None
                if bm.ring_started_ns:
                    ring_to_bridge_ms = round(
                        (bm.bridge_started_ns - bm.ring_started_ns) / 1e6, 2
                    )
                bridge_to_first_frame_ms = round(
                    (bm.first_frame_ns - bm.bridge_started_ns) / 1e6, 2
                )
                log.info(
                    "phase.bridge.setup_timeline",
                    user_id=uid,
                    ring_to_bridge_ms=ring_to_bridge_ms,
                    bridge_to_first_frame_ms=bridge_to_first_frame_ms,
                )
            log.info(
                "telegram.bridge.incoming_first_frame", user_id=uid, bytes=len(pcm)
            )
        try:
            await cb(uid, pcm)
        except Exception:
            log.exception("telegram.bridge.on_contact_audio_failed")

    async def _handle_bridge_ended(self, uid: int, reason: str) -> None:
        if self._current_user_id is not None and uid != self._current_user_id:
            return
        self._current_user_id = None
        self._last_bridge_ended_reason = reason
        self.bridge_ended_event.set()
        cb = self._on_bridge_ended
        if cb is None:
            return
        try:
            await cb(uid, reason)
        except Exception:
            log.exception("telegram.bridge.on_bridge_ended_failed")

    def _on_connection_lost(self) -> None:
        """Both reader loops call this when their channel closes.

        Fails any in-flight RPCs so a blocked ``place_call`` raises promptly,
        and — if a bridge was live — synthesizes ``on_bridge_ended`` with reason
        ``"sidecar_crashed"`` so ``_run_bridge_phase`` unblocks exactly like a
        contact hangup and (commit 6) the agent can apologize + offer to retry.

        Idempotent: called from both reader loops, but the ``_current_user_id``
        clear makes the crash synthesis fire at most once. A clean ``stop`` sets
        ``_closing`` first, so shutdown never looks like a crash.
        """
        if self._closing:
            return
        for request_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(SidecarDied("sidecar connection lost"))
            self._pending.pop(request_id, None)
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(SidecarDied("sidecar closed before ready"))

        uid = self._current_user_id
        if uid is None:
            return
        self._current_user_id = None
        self._last_bridge_ended_reason = "sidecar_crashed"
        self.bridge_ended_event.set()
        log.warning("telegram.bridge.sidecar_crashed_midbridge", user_id=uid)
        cb = self._on_bridge_ended
        if cb is not None and self._loop is not None:
            self._loop.create_task(
                self._safe_on_bridge_ended(cb, uid, "sidecar_crashed")
            )

    async def _safe_on_bridge_ended(
        self, cb: OnBridgeEnded, uid: int, reason: str
    ) -> None:
        try:
            await cb(uid, reason)
        except Exception:
            log.exception("telegram.bridge.on_bridge_ended_failed")
