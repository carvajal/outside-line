"""Telethon client + py-tgcalls wiring for the Telegram leg of a call.

``place_call`` rings a contact's Telegram via a 1:1 P2P call
(``pytgcalls.play(user_id, ExternalMedia.AUDIO, …)``); ``hangup`` releases it.
The bidirectional audio bridge on top:

- ``send_to_contact`` pushes the caller's PCM to the contact via
  ``call_py.send_frame``.
- An ``on_update(stream_frame INCOMING/MICROPHONE)`` handler forwards the
  contact's PCM to a caller-provided ``on_contact_audio`` coroutine.
- An ``on_update(chat_update LEFT_CALL | DISCARDED_CALL)`` handler fires the
  caller-provided ``on_bridge_ended`` coroutine when the contact hangs up.
- ``send_text`` sends a plain Telegram message via Telethon — used by the
  ``message_contact`` realtime tool for dictated texts.

Design (see ADR 0008):
- Modality = 1:1 P2P. ``pytgcalls.play(chat_id>0, …)`` branches to ``CallConfig``
  → ``phone.requestCall`` MTProto → real incoming-call ring on contact's app.
- ``ExternalMedia.AUDIO`` opens the wire as raw-PCM-fed via ``send_frame``.
- 48 kHz mono matches what the OpenAI Realtime model emits after resample,
  so the bridge needs no second conversion.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from telethon import TelegramClient

from pytgcalls import PyTgCalls, filters
from pytgcalls.types import (
    CallConfig,
    ChatUpdate,
    ExternalMedia,
    Frame,
    MediaStream,
    RecordStream,
    StreamFrames,
)
from pytgcalls.types.stream.device import Device
from pytgcalls.types.stream.direction import Direction
from pytgcalls.types.raw import AudioParameters

from ._latency import BridgeMetrics
from .config import settings
from .contacts import Contact
from .log import get_logger

log = get_logger(__name__)

_AUDIO_PARAMS = AudioParameters(bitrate=48000, channels=1)

OnContactAudio = Callable[[int, bytes], Awaitable[None]]
OnBridgeEnded = Callable[[int, str], Awaitable[None]]


def make_telethon() -> TelegramClient:
    """Construct a Telethon client bound to the agent session.

    Caller is responsible for ``await client.connect()`` (or ``.start()``).
    Telethon's session-path arg is a stem (no ``.session`` suffix).
    """
    stem = settings.telegram_session_path.removesuffix(".session")
    return TelegramClient(stem, settings.telegram_api_id, settings.telegram_api_hash)


class TelegramBridge:
    """High-level wrapper around Telethon + PyTgCalls for outbound P2P calls."""

    def __init__(self, telethon: TelegramClient, call_py: PyTgCalls) -> None:
        self._telethon = telethon
        self._call_py = call_py
        self._current_user_id: int | None = None
        self._on_contact_audio: OnContactAudio | None = None
        self._on_bridge_ended: OnBridgeEnded | None = None
        self._incoming_frame_logged = False
        self._bridge_metrics: BridgeMetrics | None = None
        # Signaled by `_on_chat_update` when the peer leaves the call.
        # Cleared at the start of every `place_call` so consecutive bridge
        # attempts in the same WS see a fresh edge. The twilio handler's
        # bridge phase awaits this event with a timeout.
        self.bridge_ended_event = asyncio.Event()

    async def start(self) -> None:
        """Connect Telethon, bring PyTgCalls online, and register update handlers."""
        if not self._telethon.is_connected():
            await self._telethon.connect()
        if not await self._telethon.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized — "
                "run scripts/warm_account.py first."
            )
        await self._call_py.start()
        self._register_update_handlers()
        log.info("telegram.bridge.started")

    def register_callbacks(
        self,
        on_contact_audio: OnContactAudio | None,
        on_bridge_ended: OnBridgeEnded | None,
        *,
        bridge_metrics: BridgeMetrics | None = None,
    ) -> None:
        """Last-writer-wins per-call hookup.

        Twilio's WS handler instantiates one bridge across all calls but at
        most one call is bridged at any moment (MVP single-call constraint),
        so overwriting the callbacks at each call's start is safe.

        ``bridge_metrics`` is the per-bridge ``BridgeMetrics`` container
        the ``_on_stream_frame`` handler ticks callback-cadence + frames-
        per-callback + bytes-per-callback into. Owned by the BRIDGE
        phase, not the singleton bridge — passed in here so the bridge
        can stay stateless across calls. Pass ``None`` (the default) for
        non-BRIDGE registrations to skip the sampling entirely.
        """
        self._on_contact_audio = on_contact_audio
        self._on_bridge_ended = on_bridge_ended
        self._bridge_metrics = bridge_metrics

    async def resolve_user_id(self, contact: Contact) -> int:
        """Resolve ``contact.telegram_username`` → Telegram ``user_id``.

        Cheap follow-up call: Telethon caches entities after the first
        lookup. Exists so the directline ring task can stash ``user_id``
        on the session BEFORE ``place_call`` blocks on the contact's
        pickup — necessary for the cancel-mid-flight teardown path
        (``discard_outgoing_call``) to know which call to discard.
        """
        entity = await self._telethon.get_entity(contact.telegram_username)
        return entity.id

    async def place_call(self, contact: Contact, *, timeout_s: int = 60) -> int:
        """Resolve contact.telegram_username → user_id, place P2P call, return user_id.

        Resolution is via Telegram ``@username`` (ADR 0011), not phone —
        public usernames are reachable regardless of the contact's phone
        privacy setting and don't require the agent's address book to be
        seeded first.
        """
        # Clear before placing so the upcoming bridge phase only sees the
        # post-place_call edge, not a leftover set from a prior attempt
        # in the same WS.
        self.bridge_ended_event.clear()
        entity = await self._telethon.get_entity(contact.telegram_username)
        await self._call_py.play(
            entity.id,
            MediaStream(ExternalMedia.AUDIO, _AUDIO_PARAMS),
            CallConfig(timeout=timeout_s),
        )
        # Without a PLAYBACK sink the C++ on_frames callback never fires
        # for the peer's audio — the stream_frame INCOMING/MICROPHONE
        # handler we register in `start()` would never see anything.
        # record() configures MICROPHONE=EXTERNAL on the PLAYBACK side
        # so ntgcalls emits each incoming RTP frame to our handler.
        await self._call_py.record(
            entity.id,
            RecordStream(audio=True, audio_parameters=_AUDIO_PARAMS),
        )
        self._current_user_id = entity.id
        self._incoming_frame_logged = False
        log.info(
            "telegram.call.placed",
            contact=contact.first_name,
            user_id=entity.id,
        )
        return entity.id

    async def send_to_contact(
        self,
        user_id: int,
        pcm48k: bytes,
        capture_time_ms: int | None = None,
    ) -> None:
        """Send one chunk of 48 kHz mono int16 PCM to the contact's mic input.

        ``capture_time_ms``, when provided, is forwarded as
        ``Frame.Info.capture_time`` so the contact-side WebRTC NetEQ can
        size its jitter buffer against a wall-clock-aligned capture
        instant instead of the default 0. No-op when ``None`` (legacy
        callers keep the three-arg ``send_frame`` shape).
        """
        if capture_time_ms is None:
            await self._call_py.send_frame(user_id, Device.MICROPHONE, pcm48k)
        else:
            await self._call_py.send_frame(
                user_id,
                Device.MICROPHONE,
                pcm48k,
                Frame.Info(capture_time=capture_time_ms),
            )

    async def send_text(self, contact: Contact, message: str) -> int:
        """Send a Telegram text message; return the message id.

        Uses the same Telethon session that powers ``place_call`` — no
        pytgcalls involvement. Resolution is via the contact's
        ``telegram_username`` (ADR 0011), so the recipient does not need
        to be in the agent's address book.
        """
        entity = await self._telethon.get_entity(contact.telegram_username)
        sent = await self._telethon.send_message(entity, message)
        log.info(
            "telegram.message.sent",
            contact=contact.first_name,
            msg_id=sent.id,
            chars=len(message),
        )
        return sent.id

    async def hangup(self, user_id: int) -> None:
        """Release the P2P call. Safe to call even if the contact never answered."""
        try:
            await self._call_py.leave_call(user_id)
        finally:
            if self._current_user_id == user_id:
                self._current_user_id = None
        log.info("telegram.call.hangup", user_id=user_id)

    async def discard_outgoing_call(self, user_id: int) -> None:
        """Send ``phone.discardCall`` for an in-flight outgoing P2P call.

        Use this in cancel paths where ``place_call`` was interrupted
        before the contact answered. ``hangup`` (= pytgcalls
        ``leave_call``) can't reach the MTProto discard step in this
        state: after ``play()``'s cancellation, ``_p2p_configs`` is
        popped, ``is_p2p_waiting`` is False, and ``_binding.stop`` then
        raises ``ConnectionNotFound`` → ``NotInCallError``, short-
        circuiting before ``_app.discard_call`` runs (see
        ``pytgcalls/methods/calls/leave_call.py:27-31``). The
        ``InputPhoneCall`` cache populated by ``request_call`` survives
        cancellation, so we can go straight to the BridgedClient.

        Also runs ``_clear_call`` to tear down the ntgcalls C++ binding
        entry for ``user_id``. Without this, the binding keeps the
        chat_id in ``_binding.calls()`` indefinitely (the MTProto-side
        ``DISCARDED_CALL`` update doesn't propagate to ``_clear_call`` —
        only ``LEFT_CALL`` does, see
        ``pytgcalls/methods/internal/handle_mtproto_updates.py:75-77``).
        A subsequent ``place_call`` to the same contact would then take
        the fast path in ``pytgcalls/methods/stream/play.py:46-58`` —
        ``set_stream_sources`` + return, no MTProto ``request_call``,
        contact's phone never rings. Verified empirically.

        No-ops cleanly if no MTProto call exists for ``user_id`` (cache
        miss → ``discard_call`` returns early; ``_binding.stop`` raises
        ``ConnectionNotFound`` → ``_clear_call`` suppresses it).
        """
        await self._call_py._app.discard_call(user_id, False)
        await self._call_py._clear_call(user_id)
        log.info("telegram.call.discarded_outgoing", user_id=user_id)

    async def stop(self) -> None:
        """Disconnect Telethon. PyTgCalls cleans up with the client."""
        if self._telethon.is_connected():
            await self._telethon.disconnect()
        log.info("telegram.bridge.stopped")

    def _register_update_handlers(self) -> None:
        """Wire the two pytgcalls update streams into our caller callbacks.

        Registered once at ``start()`` time — the callbacks themselves can
        be swapped out per-call via ``register_callbacks``.
        """

        async def _on_stream_frame(_: PyTgCalls, update: StreamFrames) -> None:
            cb = self._on_contact_audio
            uid = self._current_user_id
            if cb is None or uid is None or update.chat_id != uid:
                return
            # Concat all sub-frames in the update — each is raw 48k mono
            # int16 bytes, same layout `send_frame` expects.
            payload = b"".join(f.frame for f in update.frames) if update.frames else b""
            if not payload:
                return
            # ntgcalls 2.3.0 leaves Frame.Info.capture_time at 0 for
            # MICROPHONE frames in our build — the NTP-aligned absolute
            # timestamp we'd need for end-to-end direction-B latency is
            # not populated. Cadence-based sampling (callback interval,
            # frames + bytes per callback) is the path that works.
            now_ns = time.monotonic_ns()
            bm = self._bridge_metrics
            if bm is not None:
                bm.tg_callback_interval.tick(now_ns)
                bm.tg_callbacks_total += 1
                bm.tg_frames_per_callback.add_ms(float(len(update.frames)))
                bm.tg_bytes_per_callback.add_ms(float(len(payload)))
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
                    "telegram.bridge.incoming_first_frame",
                    user_id=uid,
                    bytes=len(payload),
                )
            try:
                await cb(uid, payload)
            except Exception:
                log.exception("telegram.bridge.on_contact_audio_failed")

        async def _on_chat_update(_: PyTgCalls, update: ChatUpdate) -> None:
            uid = self._current_user_id
            if uid is None or update.chat_id != uid:
                return
            if not (update.status & ChatUpdate.Status.LEFT_CALL):
                return
            cb = self._on_bridge_ended
            reason = update.status.name or "LEFT_CALL"
            self._current_user_id = None
            self.bridge_ended_event.set()
            log.info("telegram.bridge.contact_left", user_id=uid, reason=reason)
            if cb is None:
                return
            try:
                await cb(uid, reason)
            except Exception:
                log.exception("telegram.bridge.on_bridge_ended_failed")

        self._call_py.add_handler(
            _on_stream_frame,
            filters.stream_frame(Direction.INCOMING, Device.MICROPHONE),
        )
        self._call_py.add_handler(
            _on_chat_update,
            filters.chat_update(ChatUpdate.Status.LEFT_CALL),
        )
