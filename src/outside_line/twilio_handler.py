"""Twilio TwiML response + Media Stream WebSocket handler.

This module wires the inbound PSTN path:

  Twilio dials our webhook ─► we return TwiML that sends the single DTMF
  digit some carriers require to accept an inbound call (the account
  holder consents to accept charges on their own line) ─► Twilio opens a
  Media Stream WebSocket to us and streams the call audio, which we bridge
  bidirectionally to an OpenAI Realtime session so the caller talks to the
  agent live.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx
import structlog
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

from . import contacts as contacts_module
from . import directlines
from ._latency import BridgeMetrics, IntervalBuffer, StopwatchBuffer
from .audio_bridge import (
    CRASH_MESSAGE_ULAW_BYTES,
    NOKIA_HUM_ULAW_LOOP,
    RECORDING_DISCLOSURE_PCM48_BYTES,
    RECORDING_DISCLOSURE_ULAW_BYTES,
    RINGBACK_PCM48_LOOP,
    RINGBACK_ULAW_FRAME_BYTES,
    RINGBACK_ULAW_LOOP,
    WAIT_MESSAGE_PCM48_BYTES,
    chunk_pcm48_10ms,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)
from .call_journal import CallJournal
from .call_report import craft_and_send_call_report
from .caller_memory import load_memory
from .callers import (
    CallersStore,
    is_allowed_entry,
    normalize_e164,
    skips_gate_entry,
)
from .contacts import Contact, _fold
from .directlines import DirectlineSession
from .memory_updater import maybe_update_memory
from .playout import PlayoutTracker
from .transcripts import DEFAULT_DIR as TRANSCRIPTS_DIR
from .config import settings
from .log import get_logger
from .realtime_agent import RealtimeSession, SendTextFn
from .tools.message_contact import send_message as _tool_send_message

if TYPE_CHECKING:
    # Annotation-only. The real bridge (and its ntgcalls import) lives in the
    # sidecar process now (ADR 0017); at runtime app.state.telegram_bridge is a
    # TelegramBridgeClient duck-typed to the same surface, so the FastAPI
    # process never loads the crash-prone native lib.
    from .telegram_bridge import TelegramBridge

log = get_logger(__name__)
router = APIRouter()

# Log every Nth `media` frame so we can see liveness without drowning the log
# (Twilio sends ~50 media frames/sec per stream at 20 ms μ-law).
MEDIA_LOG_EVERY = 100

# Convert process-monotonic timestamps to wall-clock ms-since-epoch.
# Captured once at import; kernel time and monotonic time drift slowly
# (microseconds per hour) — well under our 10 ms frame granularity.
# Used to feed ntgcalls Frame.Info.capture_time at BRIDGE-pump send time.
_MONOTONIC_TO_EPOCH_NS = time.time_ns() - time.monotonic_ns()


# Process-wide singleton for the caller registry — safe to share across
# concurrent calls because we drive it from the asyncio event loop only.
_callers_store: CallersStore | None = None


def _get_callers_store() -> CallersStore:
    global _callers_store
    if _callers_store is None:
        _callers_store = CallersStore(settings.callers_path)
    return _callers_store


def _stream_wss_url() -> str:
    """Build the wss:// URL for Twilio's <Stream> from PUBLIC_BASE_URL.

    Fails loud if unset — the only sane fix is to configure the env var.
    """
    if not settings.public_base_url:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL not configured",
        )
    host = urlparse(settings.public_base_url).netloc
    if not host:
        raise HTTPException(
            status_code=500,
            detail=f"PUBLIC_BASE_URL is not a valid URL: {settings.public_base_url!r}",
        )
    return f"wss://{host}/twilio/media"


# ---------------------------------------------------------------------------
# Directline helpers (contact-side timeline).
#
# A directline call routes a dedicated Twilio DID straight to one Telegram
# contact, skipping the agent on the happy path. The race between gate completion
# (caller-side) and contact pickup (contact-side) is modeled as two
# independent events; the contact-side timeline owns ``_directline_ring``
# (started in ``voice()``) and the caller-side timeline owns
# ``_run_directline_phase`` (called from ``media()`` — wired in the next
# commit).


async def _play_tg_holding_audio(
    bridge: TelegramBridge,
    user_id: int,
    gate_done_event: asyncio.Event,
) -> None:
    """Stream the wait phrase + ringback loop to the contact until the gate is done.

    Plays ``WAIT_MESSAGE_PCM48_BYTES`` once (skip if disabled or empty),
    then loops ``RINGBACK_PCM48_LOOP`` at 10 ms cadence. Both abort as
    soon as ``gate_done_event`` is set (caller-side stream opened — the
    BRIDGE phase will take over). Send failures log and exit silently;
    they shouldn't break the call.

    Framing is **10 ms (960 B) per submission**: ntgcalls'
    send_external_frame is 10 ms-granular (same gotcha the BRIDGE pump
    handles by splitting Twilio 20 ms frames into two halves), so
    submitting 20 ms chunks would lose half the audio at the contact
    side. Keep submissions tight to one 10 ms frame at a time.
    """
    if settings.directline_wait_message_enabled and WAIT_MESSAGE_PCM48_BYTES:
        for chunk in chunk_pcm48_10ms(WAIT_MESSAGE_PCM48_BYTES):
            if gate_done_event.is_set():
                return
            try:
                await bridge.send_to_contact(user_id, chunk)
            except Exception:
                log.exception("directline.send_wait_failed", user_id=user_id)
                return
            await asyncio.sleep(0.010)
    chunks = chunk_pcm48_10ms(RINGBACK_PCM48_LOOP)
    if not chunks:
        return
    i = 0
    n = len(chunks)
    while not gate_done_event.is_set():
        try:
            await bridge.send_to_contact(user_id, chunks[i % n])
        except Exception:
            log.exception("directline.send_ringback_failed", user_id=user_id)
            return
        await asyncio.sleep(0.010)
        i += 1


async def _directline_ring(
    session: DirectlineSession,
    bridge: TelegramBridge,
) -> int | None:
    """Place the TG call; if the contact answers before the gate is done, hold them.

    Started in ``voice()`` before the TwiML response returns, so the
    contact's phone starts ringing while Twilio is still rendering the
    DTMF gate's silent ``<Pause>`` blocks. Returns the ``user_id`` on
    pickup, or ``None`` on timeout / error.

    Pre-resolves ``user_id`` and stashes it on the session *before*
    ``place_call`` blocks on pickup. This is what lets the cancel paths
    (``_run_directline_phase`` WS-close branch,
    ``_cleanup_orphan_directline_session``) call
    ``bridge.discard_outgoing_call`` to send the MTProto discard when
    the caller hangs up before the contact answers — without
    ``session.user_id`` populated early, the cancel paths have no
    chat_id to discard against.
    """
    try:
        session.user_id = await bridge.resolve_user_id(session.contact)
    except Exception as exc:
        log.warning(
            "directline.ring.resolve_failed",
            contact=session.contact.first_name,
            call_sid=session.call_sid,
            error=repr(exc),
        )
        return None
    try:
        user_id = await bridge.place_call(
            session.contact,
            timeout_s=settings.directline_ring_timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(
            "directline.ring.failed",
            contact=session.contact.first_name,
            call_sid=session.call_sid,
            error=repr(exc),
        )
        return None
    session.tg_answered_ns = time.monotonic_ns()
    log.info(
        "directline.ring.answered",
        contact=session.contact.first_name,
        call_sid=session.call_sid,
        user_id=user_id,
        gate_done=session.gate_done_event.is_set(),
    )
    if not session.gate_done_event.is_set():
        await _play_tg_holding_audio(bridge, user_id, session.gate_done_event)
    return user_id


async def _teardown_tg_after_cancel(
    bridge: TelegramBridge,
    session: DirectlineSession,
    *,
    where: str,
) -> None:
    """Tear down the TG side after the ring task was cancelled.

    Two branches keyed on whether the contact had already picked up:

    * **Answered** (``session.tg_answered_ns`` set): ``bridge.hangup``
      runs ``leave_call`` → ``_binding.stop`` + ``_app.discard_call``,
      properly closing both the ntgcalls binding and the MTProto call.

    * **Not answered**: ``bridge.discard_outgoing_call`` sends
      ``phone.discardCall`` directly via the BridgedClient. ``hangup``
      would silently fail to discard in this state — see the docstring
      on ``TelegramBridge.discard_outgoing_call`` for the upstream
      mutex / cache-lifecycle interaction in vendored py-tgcalls.

    No-ops if ``session.user_id`` was never resolved (entity lookup
    failed in ``_directline_ring``). ``where`` is included in failure
    logs so we can tell which cancel site (orphan cleanup vs WS-close)
    surfaced the error.
    """
    if session.user_id is None:
        return
    if session.tg_answered_ns is not None:
        try:
            await bridge.hangup(session.user_id)
        except Exception:
            log.exception(
                "directline.teardown_hangup_failed",
                call_sid=session.call_sid,
                user_id=session.user_id,
                where=where,
            )
        return
    try:
        await bridge.discard_outgoing_call(session.user_id)
    except Exception:
        log.exception(
            "directline.teardown_discard_failed",
            call_sid=session.call_sid,
            user_id=session.user_id,
            where=where,
        )


async def _finalize_directline_session(
    session: DirectlineSession,
    bridge: TelegramBridge,
    *,
    where: str,
) -> None:
    """Cancel the directline ring task + tear down the TG side cleanly.

    Shared by every site that decides "this directline session is over":
    the orphan-cleanup timer, the post-gate phase's WS-close branch, and
    the Twilio statusCallback endpoint. Idempotent — setting an already
    set event is a no-op; cancelling an already done task is gated by
    the ``.done()`` check; teardown no-ops when ``user_id`` was never
    resolved.

    Setting ``gate_done_event`` first unblocks ``_play_tg_holding_audio``
    if the contact picked up and the holding-audio pump is still
    looping — without this the cancel-then-await of ``tg_task`` could
    block on the pump finishing its current chunk.
    """
    session.gate_done_event.set()
    if session.tg_task is not None and not session.tg_task.done():
        session.tg_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await session.tg_task
    await _teardown_tg_after_cancel(bridge, session, where=where)


async def _cleanup_orphan_directline_session(
    call_sid: str,
    bridge: TelegramBridge,
    timeout_s: int,
) -> None:
    """Reap a directline session if media() never picks it up.

    Fires ``timeout_s`` after ``voice()`` returns. If the WS handler has
    already popped the session (media() got there first, or the
    statusCallback endpoint finalized it), this is a no-op. Otherwise
    the caller hung up during the gate pause AND Twilio's statusCallback
    delivery either didn't arrive or took too long; we run the same
    teardown the statusCallback would have, just slower.
    """
    await asyncio.sleep(timeout_s)
    session = directlines.pop_session(call_sid)
    if session is None:
        return  # media() or statusCallback got there first
    log.warning(
        "directline.orphan_cleanup",
        call_sid=call_sid,
        had_user_id=session.user_id is not None,
        tg_answered=session.tg_answered_ns is not None,
    )
    await _finalize_directline_session(session, bridge, where="orphan_cleanup")


def _twilio_signature_valid(
    signature: str, url: str, params: list[tuple[str, str]], auth_token: str
) -> bool:
    """Twilio's X-Twilio-Signature scheme: base64(HMAC-SHA1(auth_token,
    url + concat(k+v sorted by key))). Constant-time compare."""
    payload = url + "".join(k + v for k, v in sorted(params))
    digest = hmac.new(
        auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def _verify_twilio_request(request: Request, body_bytes: bytes) -> Response | None:
    """Validate X-Twilio-Signature; ``None`` = accepted, else a 403.

    Skips entirely (returns None) when TWILIO_AUTH_TOKEN is blank — the
    boot-time warning in main.py flags that state, and the production
    preflight makes the token hard-required outside development. The
    token check MUST come before any header access (unit tests drive
    these endpoints with minimal fake Requests).

    The signed URL is rebuilt from ``settings.public_base_url`` + path:
    behind Railway's proxy, ``request.url`` shows the internal http://
    scheme while Twilio signed the public https:// one (the
    X-Forwarded-Proto trap ADR 0010 flags). Falls back to the raw
    request URL when public_base_url is unset.
    """
    auth_token = settings.twilio_auth_token
    if not auth_token:
        return None
    signature = request.headers.get("X-Twilio-Signature", "")
    if settings.public_base_url:
        url = settings.public_base_url.rstrip("/") + request.url.path
    else:
        url = str(request.url)
    params = parse_qsl(
        body_bytes.decode("utf-8", "replace"), keep_blank_values=True
    )
    if not signature or not _twilio_signature_valid(
        signature, url, params, auth_token
    ):
        log.warning(
            "twilio.signature.rejected",
            url=url,
            had_signature=bool(signature),
        )
        return Response(status_code=403)
    return None


@router.post("/twilio/voice")
async def voice(request: Request) -> Response:
    """Inbound call webhook. Returns TwiML.

    The shape returned depends on the caller's allow + skip-gate status:

    * Caller not in ``data/callers.json``, or entry has ``allowed=False``
      — render ``<Reject reason="busy"/>`` so Twilio drops the call
      without opening a media stream. The caller hears a busy signal
      (no confirmation that we're a live number). An email alert
      fires asynchronously on every blocked call, with the exact
      ``./scripts/callers.py allow`` command in the body.

    * Allowed caller, ``skip_gate=True`` — render the bare
      ``<Connect><Stream/></Connect>`` (no DTMF gate). The ``skip_gate``
      flag is carried to the WS handler via ``<Parameter>`` children
      under ``<Stream>`` for log context.

    * Allowed caller, ``skip_gate=False`` (the default) — render the DTMF
      gate:
        1. Wait ``settings.dtmf_gate_seconds`` for the carrier's
           acceptance prompt.
        2. Send DTMF '1' to accept the call (the account holder consents
           to accept charges on their own line).
        3. Wait ``settings.dtmf_gate_seconds`` for the prompt to settle.
        4. Open a bidirectional Media Stream straight to the agent.

    The gate-skip list is the only mechanism to skip the wait — there is
    no URL-level gate-off switch. Toggle via
    ``./scripts/callers.py skip-gate``.

    **Directline routing.** If the dialed Twilio number (``To``) matches
    an entry in ``data/directline_numbers.json``, ``voice()`` ALSO
    spawns a contact-side ring task before returning TwiML so the
    Telegram contact's phone starts ringing in parallel with the gate.
    The TwiML carries an extra ``<Parameter name="directline_call_sid">``
    that tells the WS handler to route into the directline phase (no
    The agent session on the happy path). See
    ``docs/decisions/0013-directline-feature.md``.
    """
    stream_url = _stream_wss_url()

    # Caller registry: upsert per-phone entry, learn if this is the first
    # call from this number. Twilio sends `From` already in E.164; the
    # normalizer is defense-in-depth + lets us tolerate odd shapes.
    # We parse the urlencoded body by hand to avoid pulling in
    # python-multipart just for two fields.
    body_bytes = await request.body()
    rejected = _verify_twilio_request(request, body_bytes)
    if rejected is not None:
        return rejected
    body_fields = parse_qs(body_bytes.decode("utf-8", "replace")) if body_bytes else {}
    from_raw = (body_fields.get("From") or [None])[0]
    call_sid_form = (body_fields.get("CallSid") or [None])[0]
    from_number = normalize_e164(from_raw)
    # `To` (which of our numbers they dialed — mainline or a directline DID)
    # is read up front: the blocked branch below returns before the directline
    # resolution, and it wants the dialed number for the alert email.
    to_raw = (body_fields.get("To") or [None])[0]
    to_did = normalize_e164(to_raw)
    entry: dict[str, object] | None = None
    was_first_time = False
    if from_number:
        try:
            entry, was_first_time = _get_callers_store().record_call(
                from_number,
                call_sid=call_sid_form,
            )
        except Exception:
            log.exception("callers.record.failed", phone=from_number)

    # Open the per-call lifecycle journal (data/calls/<sid>.jsonl) and record
    # the first event. This is the durable spine of the call-processed email
    # (call_report.py) — written here at call start, appended to at the media()
    # phase seams, read back by the /twilio/voice/status trigger. `line` +
    # `line_label` distinguish the mainline from a directline DID (which
    # contact it fronts). `directline_entry` is hoisted here (was resolved
    # lower down) so the blocked branch below can classify the dialed line too.
    directline_entry = directlines.lookup(to_did) if to_did else None
    journal = CallJournal(call_sid_form) if call_sid_form else None
    if journal is not None:
        journal.append(
            "received",
            line="directline" if directline_entry is not None else "mainline",
            line_label=directline_entry.label if directline_entry is not None else None,
            first_time=was_first_time,
            label=(entry.get("label") if entry else None),
            call_count=(entry.get("call_count") if entry else None),
            **{"from": from_number, "to": to_did},
        )

    # Allow list: numbers not in the registry, or with allowed=False,
    # get a busy signal — no media stream, no the agent, no leak that this
    # number is live. Every blocked call from a known phone number
    # fires an email alert with the CLI command to allow it, so the
    # operator can triage from their inbox. allowed=False always wins
    # over the gate skip. Missing/unparseable From (shouldn't happen with
    # real Twilio traffic) is also blocked, silently — there's nothing
    # to alert on.
    allowed = bool(entry) and is_allowed_entry(entry)
    if not allowed:
        if from_number:
            # The blocked call is journaled below; the call-processed email is
            # sent on the terminal /twilio/voice/status callback (ADR 0022) —
            # no start-time alert here anymore.
            log.info(
                "twilio.voice.blocked",
                from_number=from_number,
                first_time=was_first_time,
                call_count=(
                    int(entry["call_count"])
                    if entry and entry.get("call_count")
                    else None
                ),
            )
        else:
            log.warning("twilio.voice.blocked_no_from", call_sid=call_sid_form)
        if journal is not None:
            journal.append("blocked")
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            '  <Reject reason="busy"/>\n'
            "</Response>\n"
        )
        return Response(content=twiml, media_type="application/xml")

    # Resolve directline routing up front — read-only. (`to_did` /
    # `directline_entry` were resolved near the top so the blocked branch and
    # the journal `received` line could classify the dialed line too.)
    # Side-effects (ring task, session registration, orphan cleanup) stay in
    # the routing block lower down so failure modes don't affect this
    # resolution. The triad (entry, contact, bridge) all-or-nothing is what
    # determines whether the directline actually fires; if any piece is
    # missing we fall through to the default the agent path.
    directline_contact = (
        contacts_module.find_by_username(directline_entry.telegram_username)
        if directline_entry is not None
        else None
    )
    bridge_ref: TelegramBridge | None = getattr(
        request.app.state, "telegram_bridge", None
    )
    directline_will_route = (
        directline_entry is not None
        and directline_contact is not None
        and bridge_ref is not None
        and bool(call_sid_form)
    )

    # Directline routing: if the dialed Twilio number (`To`) maps to a
    # configured directline AND its contact resolves AND the Telegram
    # bridge is up, spawn the contact-side ring task NOW (so the TG
    # phone is ringing while Twilio is still rendering the gate) and
    # tag the stream so media() routes to the directline phase instead
    # of opening the agent session.
    directline_call_sid: str | None = None
    if directline_entry is not None and call_sid_form:
        if not directline_will_route:
            # Misconfigured (directline points at an unknown
            # username, or Telegram isn't wired up) — fall through
            # to the default the agent path. Safer than rejecting: the
            # caller is already allowed, the agent is a useful fallback.
            log.error(
                "directline.unavailable",
                twilio_did=to_did,
                username=directline_entry.telegram_username,
                contact_found=directline_contact is not None,
                bridge_available=bridge_ref is not None,
            )
        else:
            # mypy: narrowed by directline_will_route above.
            assert directline_contact is not None
            assert bridge_ref is not None
            dl_session = DirectlineSession(
                call_sid=call_sid_form,
                contact=directline_contact,
                label=directline_entry.label,
            )
            dl_session.tg_task = asyncio.create_task(
                _directline_ring(dl_session, bridge_ref),
                name=f"directline-ring:{call_sid_form}",
            )
            directlines.register_session(dl_session)
            asyncio.create_task(
                _cleanup_orphan_directline_session(
                    call_sid_form,
                    bridge_ref,
                    timeout_s=settings.directline_ring_timeout_s,
                ),
                name=f"directline-cleanup:{call_sid_form}",
            )
            directline_call_sid = call_sid_form
            if journal is not None:
                journal.append(
                    "contact_call",
                    name=directline_contact.first_name,
                    kind="directline",
                )
            log.info(
                "directline.routed",
                twilio_did=to_did,
                contact=directline_contact.first_name,
                call_sid=call_sid_form,
                label=directline_entry.label,
            )

    # Gate-skip list: allowed callers flagged in data/callers.json get the
    # bare stream (no DTMF gate); the flag is carried to the WS handler
    # via the <Parameter> children under <Stream> below (log context).
    skips_gate = skips_gate_entry(entry)
    # Emit both params unconditionally so media() can rely on their
    # presence in start.customParameters. ``directline_call_sid`` is
    # emitted only when this call is routed via a directline DID — its
    # absence is the signal to the WS handler that this is a normal call.
    directline_param_xml = (
        f'      <Parameter name="directline_call_sid" value="{directline_call_sid}"/>\n'
        if directline_call_sid
        else ""
    )
    stream_params_xml = (
        f'      <Parameter name="from" value="{from_number or ""}"/>\n'
        f'      <Parameter name="skip_gate" value="{1 if skips_gate else 0}"/>\n'
        + directline_param_xml
    )

    if skips_gate:
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            "  <Connect>\n"
            f'    <Stream url="{stream_url}">\n'
            f"{stream_params_xml}"
            "    </Stream>\n"
            "  </Connect>\n"
            "</Response>\n"
        )
        log.info(
            "twilio.voice.webhook",
            stream_url=stream_url,
            gate=False,
            skip_gate=True,
            from_number=from_number,
            first_time=was_first_time,
            directline=bool(directline_call_sid),
        )
    else:
        # Leading <Say> forces Twilio to answer the call immediately. Without
        # it, a leading <Pause> makes Twilio ring the line for `gate_seconds`
        # before picking up (per the docs at twilio.com/docs/voice/twiml/pause),
        # which pushes the DTMF tone outside the carrier's "press 1 to
        # accept" prompt window. The SSML <break> renders as silence.
        gate_seconds = settings.dtmf_gate_seconds
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<Response>\n"
            '  <Say><break time="100ms"/></Say>\n'
            f'  <Pause length="{gate_seconds}"/>\n'
            '  <Play digits="1"/>\n'
            f'  <Pause length="{gate_seconds}"/>\n'
            "  <Connect>\n"
            f'    <Stream url="{stream_url}">\n'
            f"{stream_params_xml}"
            "    </Stream>\n"
            "  </Connect>\n"
            "</Response>\n"
        )
        log.info(
            "twilio.voice.webhook",
            stream_url=stream_url,
            gate=True,
            gate_seconds=gate_seconds,
            skip_gate=False,
            from_number=from_number,
            first_time=was_first_time,
            directline=bool(directline_call_sid),
        )
    return Response(content=twiml, media_type="application/xml")


# Terminal CallStatus values per Twilio's call-lifecycle model. Anything
# outside this set is an intermediate state we don't act on (defensive
# against an upstream default change that would start firing the webhook
# on ``ringing`` or ``in-progress``).
_TERMINAL_CALL_STATUSES = frozenset(
    {"completed", "busy", "failed", "no-answer", "canceled"}
)


@router.post("/twilio/voice/status")
async def voice_status(request: Request) -> Response:
    """Twilio call-lifecycle webhook for fast directline teardown.

    Twilio POSTs here once per call when ``CallStatus`` reaches a terminal
    value (default: ``completed``). For directline calls, this signal is
    what closes the latency gap that ``_cleanup_orphan_directline_session``
    cannot — when the caller hangs up during the gate before the WS opens,
    the orphan-cleanup timer is the only other signal, and it fires at
    ``settings.directline_ring_timeout_s`` (~70 s). With this endpoint
    wired the teardown runs within ~1 s of caller hangup.

    Always returns 200. Twilio retries non-2xx for up to 24 hours; an
    idempotent no-op for sessions we don't recognise (mainline calls,
    already-finalized directline sessions, replays) is the right shape.
    """
    body_bytes = await request.body()
    rejected = _verify_twilio_request(request, body_bytes)
    if rejected is not None:
        return rejected
    body_fields = parse_qs(body_bytes.decode("utf-8", "replace")) if body_bytes else {}
    call_sid = (body_fields.get("CallSid") or [None])[0]
    call_status = (body_fields.get("CallStatus") or [None])[0]

    if not call_sid:
        log.warning(
            "directline.status_callback.malformed",
            body_sample=body_bytes[:200].decode("utf-8", "replace"),
        )
        return Response(status_code=200)

    structlog.contextvars.bind_contextvars(call_sid=call_sid)
    try:
        log.info(
            "directline.status_callback.received",
            call_status=call_status,
        )

        if call_status not in _TERMINAL_CALL_STATUSES:
            # Intermediate event — don't pop. Defensive: Twilio's default
            # is to fire only on ``completed`` for IncomingPhoneNumber
            # statusCallback, but the gate makes a future default change
            # safe.
            log.debug(
                "directline.status_callback.intermediate_ignored",
                call_status=call_status,
            )
            return Response(status_code=200)

        # This is the single universal call-ended trigger: fire the
        # call-processed report for EVERY terminal call (blocked, mainline,
        # directline, gate-hangup, crash-recovered). Fire-and-forget +
        # exactly-once via an on-disk marker, so it survives a re-delivery and
        # never blocks the 200 Twilio expects (ADR 0022).
        dur_raw = (body_fields.get("CallDuration") or [None])[0]
        asyncio.create_task(
            craft_and_send_call_report(
                call_sid,
                call_status=call_status,
                call_duration_s=(
                    int(dur_raw) if dur_raw and dur_raw.isdigit() else None
                ),
                from_number=normalize_e164((body_fields.get("From") or [None])[0]),
                to_number=normalize_e164((body_fields.get("To") or [None])[0]),
            )
        )

        session = directlines.pop_session(call_sid)
        if session is None:
            # Mainline call, or another teardown site (media() / WS-close
            # branch / orphan-cleanup) already won the race. No-op.
            log.debug("directline.status_callback.no_session")
            return Response(status_code=200)

        bridge: TelegramBridge | None = getattr(
            request.app.state, "telegram_bridge", None
        )
        if bridge is None:
            # Degraded mode — TG bridge failed to start. We've already
            # popped the registry entry (no leak); nothing more we can
            # do for the TG side without a bridge.
            log.warning(
                "directline.status_callback.no_bridge",
                tg_answered=session.tg_answered_ns is not None,
                had_user_id=session.user_id is not None,
            )
            return Response(status_code=200)

        log.info(
            "directline.status_callback.finalizing",
            had_user_id=session.user_id is not None,
            tg_answered=session.tg_answered_ns is not None,
        )
        await _finalize_directline_session(session, bridge, where="status_callback")
        return Response(status_code=200)
    finally:
        structlog.contextvars.unbind_contextvars("call_sid")


def _twilio_kwargs(msg: dict[str, Any]) -> dict[str, Any]:
    """Rename Twilio's ``event`` key so it doesn't collide with structlog's
    positional ``event`` log-name argument when splatted into ``log.info``.
    """
    out = dict(msg)
    out["twilio_event"] = out.pop("event", None)
    return out


async def _start_recording(call_sid: str) -> None:
    """Kick off a Twilio dual-channel recording on the in-progress call.

    Channel 0 is the caller's audio (inbound to Twilio), channel 1 is what
    Twilio plays back to them (the agent's audio). Audio is stored on Twilio's
    infrastructure; fetch via ``scripts/recordings.py`` or the Twilio Console.

    Fire-and-forget: never raises. A recording failure must not break the
    call. Logs ``twilio.recording.started`` on success and
    ``twilio.recording.failed`` on any error.
    """
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}/Calls/{call_sid}/Recordings.json"
    )
    auth = (settings.twilio_api_key_sid, settings.twilio_api_key_secret)
    data = {"RecordingChannels": "dual"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, data=data, auth=auth)
        if resp.status_code >= 400:
            log.warning(
                "twilio.recording.failed",
                status=resp.status_code,
                body=resp.text[:500],
            )
            return
        payload = resp.json()
        log.info(
            "twilio.recording.started",
            recording_sid=payload.get("sid"),
            status=payload.get("status"),
        )
    except Exception as exc:
        log.warning("twilio.recording.failed", error=repr(exc))


_INSIGHTS_RETRY_DELAYS_S: tuple[float, ...] = (30.0, 60.0, 120.0)


async def _fetch_voice_insights_summary(call_sid: str, ws_close_wall_ms: int) -> None:
    """Pull Twilio Voice Insights Summary after call end. Fire-and-forget.

    Endpoint: ``GET https://insights.twilio.com/v1/Voice/{CallSid}/Summary``.
    Auth: same API Key tuple as recordings / hangup.

    Twilio's pipeline produces summaries 30–120 s post-call (the doc claims
    ~10 s, but 404s persist up to ~15 s in practice). Retry on 404 at
    30 / 60 / 120 s and bail on any other status — 403 (auth), 5xx
    (transient) don't get better by waiting. If the container restarts
    before the third retry lands we lose one summary; accepted trade-off.

    Response shape parsed below matches the modern Voice Insights API
    (carrier_edge / properties.pdd_ms), not the legacy top-level metrics
    block.

    ``ws_close_wall_ms`` is wall-clock at the moment our Media Stream WS
    actually closed; subtracted from Twilio's ``end_time`` (when the PSTN
    call ended on their side) it gives the **phantom-bridge tail** — how
    long Twilio held the WS open with no media flowing. The observability
    counterpart of the bridge stall detector.
    """
    url = f"https://insights.twilio.com/v1/Voice/{call_sid}/Summary"
    auth = (settings.twilio_api_key_sid, settings.twilio_api_key_secret)
    data: dict[str, Any] | None = None
    for delay in _INSIGHTS_RETRY_DELAYS_S:
        await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, auth=auth)
        except Exception as exc:
            log.warning("twilio.insights.error", call_sid=call_sid, error=repr(exc))
            return
        if resp.status_code == 404:
            continue  # not ready yet — try the next delay
        if resp.status_code >= 400:
            log.warning(
                "twilio.insights.failed",
                call_sid=call_sid,
                status=resp.status_code,
                body=resp.text[:300],
            )
            return
        data = resp.json() or {}
        break
    if data is None:
        log.warning(
            "twilio.insights.failed",
            call_sid=call_sid,
            status=404,
            body="summary still 404 after final retry",
        )
        return

    def _g(d: Any, *path: str) -> Any:
        cur: Any = d
        for key in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(key)
        return cur

    # Drift between Twilio-side PSTN end and our WS close. Modern Voice
    # Insights returns ISO 8601 UTC under ``end_time``; ``end_time_string``
    # is a defensive fallback if the field name ever drifts. Drift is
    # logged as None on missing/unparseable values — we'll know to inspect
    # the live response and fix.
    end_time_iso = data.get("end_time") or data.get("end_time_string")
    drift_ms: int | None = None
    if isinstance(end_time_iso, str):
        try:
            end_dt = datetime.fromisoformat(end_time_iso.replace("Z", "+00:00"))
            drift_ms = ws_close_wall_ms - int(end_dt.timestamp() * 1000)
        except ValueError:
            pass

    # Twilio reports jitter as milliseconds already; pass through.
    log.info(
        "twilio.insights.summary",
        call_sid=call_sid,
        duration=data.get("duration"),
        processing_state=data.get("processing_state"),
        pstn_to_ws_close_drift_ms=drift_ms,
        pdd_ms=_g(data, "properties", "pdd_ms"),
        disconnected_by=_g(data, "properties", "disconnected_by"),
        edge_location=_g(data, "carrier_edge", "properties", "edge_location"),
        media_region=_g(data, "carrier_edge", "properties", "media_region"),
        from_carrier=_g(data, "from", "carrier"),
        carrier_inbound_jitter_avg_ms=_g(
            data, "carrier_edge", "metrics", "inbound", "jitter", "avg"
        ),
        carrier_inbound_jitter_max_ms=_g(
            data, "carrier_edge", "metrics", "inbound", "jitter", "max"
        ),
        carrier_outbound_jitter_avg_ms=_g(
            data, "carrier_edge", "metrics", "outbound", "jitter", "avg"
        ),
        carrier_outbound_jitter_max_ms=_g(
            data, "carrier_edge", "metrics", "outbound", "jitter", "max"
        ),
        carrier_packets_lost_pct=_g(
            data, "carrier_edge", "metrics", "inbound", "packets_loss_percentage"
        ),
        carrier_packets_lost=_g(
            data, "carrier_edge", "metrics", "inbound", "packets_lost"
        ),
        carrier_packets_received=_g(
            data, "carrier_edge", "metrics", "inbound", "packets_received"
        ),
        attributes=data.get("attributes"),
    )


# ---------------------------------------------------------------------------
# AGENT / RING / BRIDGE phase loop.
#
# A long-lived ws_reader task feeds μ-law frames into ``audio_inbound``;
# each phase consumes them and routes. On WS close, the reader pushes a
# ``None`` sentinel and any active phase winds down.

_SendToTwilio = Callable[[bytes], Awaitable[None]]
_ClearTwilio = Callable[[], Awaitable[None]]


@dataclass
class _AgentPhaseOutcome:
    bridge_request: Contact | None  # resolved by dispatch; None = call over


@dataclass
class _DirectlineOutcome:
    """Result of ``_run_directline_phase``.

    ``fallback=True`` means TG didn't answer (or errored); media() should
    enter the standard phase loop with ``first_response_instruction`` so
    The agent picks up with an apology. ``fallback=False`` means the call is
    over (bridge ended cleanly, caller hung up, or stall fired); media()
    should fall through to teardown.
    """

    fallback: bool = False
    first_response_instruction: str | None = None
    reason: str = ""


@dataclass
class _BridgeMetricsCtx:
    """Per-WS state needed by the BRIDGE-phase latency telemetry.

    ``drops_total`` is bumped by the overflow branch in ``ws_reader``
    and read+reset by the per-second metric emitter. ``in_bridge_phase``
    is the gate the per-100 ``twilio.media.frame`` counter uses to stay
    silent during BRIDGE (subsumed by ``phase.bridge.latency``).
    ``inbound_interval`` records ws-receive jitter; ``seq_skips`` /
    ``last_seq`` track Twilio sequenceNumber gaps (frames the carrier
    or Twilio's edge dropped before us).
    """

    drops_total: int = 0
    in_bridge_phase: bool = field(default=False)
    inbound_interval: IntervalBuffer = field(default_factory=IntervalBuffer)
    seq_skips: int = 0
    last_seq: int | None = None
    ring_started_ns: int = 0


async def _ring_audio_driver(
    send_to_twilio: _SendToTwilio, cancel: asyncio.Event
) -> None:
    """Stream the configured RING-phase audio loop to Twilio at 20 ms cadence.

    Loop choice (``settings.ring_audio_kind``) is read once at driver start;
    a per-call config swap would mean reloading mid-RING which we never
    want. The loop wraps with a modulo on ``offset`` so a long ring (up
    to the 60 s ``place_call`` timeout) just keeps cycling. Cancelled by
    the RING phase as soon as ``place_call`` returns.
    """
    loop = (
        NOKIA_HUM_ULAW_LOOP if settings.ring_audio_kind == "hum" else RINGBACK_ULAW_LOOP
    )
    frame = RINGBACK_ULAW_FRAME_BYTES
    offset = 0
    try:
        while not cancel.is_set():
            chunk = loop[offset : offset + frame]
            offset = (offset + frame) % len(loop)
            await send_to_twilio(chunk)
            await asyncio.sleep(0.020)
    except asyncio.CancelledError:
        pass


# Extra silence after the crash clip's last frame before the WS closes, so
# Twilio's playout/jitter buffer finishes the recording instead of getting
# cut off mid-word. Frames are sent at ~real-time (20 ms cadence), so the
# unplayed tail is only a few hundred ms; 0.8 s is a comfortable margin.
_CRASH_PLAYOUT_TAIL_S = 0.8


async def _play_ulaw_clip(
    send_to_twilio: _SendToTwilio,
    ws_closed: asyncio.Event,
    clip: bytes = CRASH_MESSAGE_ULAW_BYTES,
) -> None:
    """Play a pre-rendered μ-law clip to the caller once.

    Serves two fixed recordings — the sidecar-crash message (ADR 0017)
    and the recording-disclosure notice — always in a fixed TTS voice,
    NOT the agent's Realtime voice. Finite twin of ``_ring_audio_driver``:
    stride 160 B / 20 ms, bail if the caller hangs up, then drain the
    playout tail. Empty ``clip`` (asset missing) ⇒ no-op.
    """
    if not clip:
        return
    frame = RINGBACK_ULAW_FRAME_BYTES
    for offset in range(0, len(clip), frame):
        if ws_closed.is_set():
            return
        await send_to_twilio(clip[offset : offset + frame])
        await asyncio.sleep(0.020)
    if not ws_closed.is_set():
        await asyncio.sleep(_CRASH_PLAYOUT_TAIL_S)


async def _drain_audio_inbound(
    audio_inbound: asyncio.Queue[tuple[bytes, int] | None],
    ws_closed: asyncio.Event,
) -> None:
    """Discard caller frames until WS close — used by RING."""
    while not ws_closed.is_set():
        item = await audio_inbound.get()
        if item is None:
            return


async def _settle_tasks(*tasks: asyncio.Task[Any]) -> None:
    for t in tasks:
        if not t.done():
            t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


async def _run_agent_phase(
    *,
    ws_closed: asyncio.Event,
    audio_inbound: asyncio.Queue[tuple[bytes, int] | None],
    send_to_twilio: _SendToTwilio,
    clear_twilio_playback: _ClearTwilio,
    playout: PlayoutTracker,
    call_sid: str | None,
    caller_memory: str | None,
    telegram_bridge: TelegramBridge | None,
    send_text_fn: SendTextFn | None,
    first_response_instruction: str | None,
) -> _AgentPhaseOutcome:
    """Build a RealtimeSession; pump audio until bridge_request or WS close."""
    # Fresh floor ledger for this phase: RING ringback and BRIDGE audio
    # flow through the same send_to_twilio closure and inflate the
    # tracker's clock/byte counts; marks armed by a pre-BRIDGE session
    # must not mute this one.
    playout.reset()
    async with RealtimeSession(
        on_audio_out=send_to_twilio,
        on_clear=clear_twilio_playback,
        playout=playout,
        call_sid=call_sid,
        caller_memory=caller_memory,
        bridge_available=telegram_bridge is not None,
        send_text_fn=send_text_fn,
        first_response_instruction=first_response_instruction,
    ) as rt:
        await rt.kickoff_greeting()

        async def pump_caller_audio() -> None:
            while True:
                item = await audio_inbound.get()
                if item is None:
                    return
                frame, _ts = item
                pcm = mulaw_to_pcm16(frame)
                await rt.send_audio_pcm16(pcm)

        rcv_task = asyncio.create_task(rt.receive_loop())
        pump_task = asyncio.create_task(pump_caller_audio())
        req_task = asyncio.create_task(rt.bridge_request_event.wait())
        closed_task = asyncio.create_task(ws_closed.wait())

        try:
            await asyncio.wait(
                [rcv_task, pump_task, req_task, closed_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            await _settle_tasks(rcv_task, pump_task, req_task, closed_task)

        return _AgentPhaseOutcome(bridge_request=rt.bridge_request)


async def _run_ring_phase(
    *,
    ws_closed: asyncio.Event,
    audio_inbound: asyncio.Queue[tuple[bytes, int] | None],
    send_to_twilio: _SendToTwilio,
    telegram_bridge: TelegramBridge | None,
    contact: Contact,
    metrics_ctx: _BridgeMetricsCtx,
) -> tuple[str | None, int | None]:
    """Play ringback, place_call to the already-resolved contact.

    Returns ``(resolved_name, user_id)``:

    - ``(name, None)`` — found but didn't answer / errored.
    - ``(name, user_id)`` — answered; BRIDGE phase follows.

    Lookup happened in the dispatcher; ``contact`` is guaranteed non-None.
    """
    if telegram_bridge is None:
        log.info(
            "phase.ring.skipped", reason="no_bridge", first_name=contact.first_name
        )
        return None, None

    metrics_ctx.ring_started_ns = time.monotonic_ns()
    log.info("phase.ring.started", first_name=contact.first_name)
    cancel_ringback = asyncio.Event()
    ringback_task = asyncio.create_task(
        _ring_audio_driver(send_to_twilio, cancel_ringback)
    )
    drain_task = asyncio.create_task(_drain_audio_inbound(audio_inbound, ws_closed))
    try:
        try:
            user_id = await telegram_bridge.place_call(contact, timeout_s=60)
            log.info(
                "phase.ring.answered",
                first_name=contact.first_name,
                user_id=user_id,
            )
            return contact.first_name, user_id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "phase.ring.failed",
                first_name=contact.first_name,
                error=repr(exc),
            )
            return contact.first_name, None
    finally:
        cancel_ringback.set()
        await _settle_tasks(ringback_task, drain_task)


def _stall_step(
    a_count: int,
    inbound_count: int,
    contact_count: int,
    current_streak: int,
    threshold: int,
) -> tuple[int, bool]:
    """Advance the bridge-stall consecutive-silent counter for one window.

    Returns ``(new_streak, should_fire)``. The detector tears the bridge
    down only when it is silent in BOTH directions — no caller audio in
    (``inbound_count``), none forwarded to the contact (``a_count``), AND
    none arriving from the contact (``contact_count``). Any one of the
    three being non-zero resets the streak: a live contact leg means the
    call is still up even if the caller's inbound leg has briefly gone
    quiet. This covers the observed failure where a caller's carrier stopped
    delivering media for a few seconds while both parties were on the
    line, and ``contact_count`` stayed ~100 fps throughout, yet the old
    two-signal detector tore the live call down.

    ``threshold <= 0`` disables firing (detector off; streak still tracked
    so re-enabling via env reload behaves predictably).

    NOTE: relies on the contact leg streaming continuous frames (~100 fps
    in this build — no Opus DTX). If DTX ever suppressed frames during
    contact silence, ``contact_count`` could legitimately reach 0; the
    ``threshold`` (both legs silent for N seconds) makes a false trip
    unlikely, but a WS-level liveness signal would be the real fix.
    """
    if a_count > 0 or inbound_count > 0 or contact_count > 0:
        return 0, False
    new_streak = current_streak + 1
    should_fire = threshold > 0 and new_streak >= threshold
    return new_streak, should_fire


async def _play_contact_disclosure(
    bridge: TelegramBridge,
    user_id: int,
    ws_closed: asyncio.Event,
) -> None:
    """Speak the recording notice to the contact before the bridge pumps.

    Runs only when recordings + disclosure are both enabled. Mirrors the
    wait-phrase half of ``_play_tg_holding_audio``: 10 ms cadence, bail
    if the caller's WS closed, fail-soft on send errors. Called before
    ``register_callbacks`` so no contact audio can reach the recording
    before the notice lands (notice-before-capture ordering).
    """
    if not (
        settings.recordings_enabled and settings.recording_disclosure_enabled
    ):
        return
    if not RECORDING_DISCLOSURE_PCM48_BYTES:
        return
    for chunk in chunk_pcm48_10ms(RECORDING_DISCLOSURE_PCM48_BYTES):
        if ws_closed.is_set():
            return
        try:
            await bridge.send_to_contact(user_id, chunk)
        except Exception:
            log.exception("recording_disclosure.send_failed", user_id=user_id)
            return
        await asyncio.sleep(0.010)


async def _run_bridge_phase(
    *,
    ws_closed: asyncio.Event,
    audio_inbound: asyncio.Queue[tuple[bytes, int] | None],
    send_to_twilio: _SendToTwilio,
    telegram_bridge: TelegramBridge,
    user_id: int,
    metrics_ctx: _BridgeMetricsCtx,
    timeout_s: float = 1800.0,
) -> str:
    """Pump audio Twilio↔Telegram. Returns the exit reason."""

    await _play_contact_disclosure(telegram_bridge, user_id, ws_closed)

    bridge_metrics = BridgeMetrics(
        ring_started_ns=metrics_ctx.ring_started_ns,
        bridge_started_ns=time.monotonic_ns(),
    )

    async def on_contact_audio(_uid: int, pcm48k: bytes) -> None:
        t = time.monotonic_ns()
        mulaw = pcm16_to_mulaw(pcm48k, sr_in=48000, sr_out=8000)
        bridge_metrics.pcm_to_mulaw.record(t)
        t = time.monotonic_ns()
        await send_to_twilio(mulaw)
        bridge_metrics.ws_send_to_twilio.record(t)

    # Send-side stopwatches (wall-clock-aligned). queue_dwell measures
    # how long a μ-law frame waited from WS receive to dequeue here;
    # resample wraps the mulaw→pcm16 + 8k→48k chain; send_to_contact
    # wraps both 10 ms halves of the ntgcalls send_frame call.
    queue_dwell = StopwatchBuffer()
    resample = StopwatchBuffer()
    send_to_contact_sw = StopwatchBuffer()
    telegram_bridge.register_callbacks(
        on_contact_audio, None, bridge_metrics=bridge_metrics
    )
    metrics_ctx.in_bridge_phase = True
    bridge_start_monotonic = time.monotonic()
    bridge_start_drops = metrics_ctx.drops_total
    bridge_start_seq_skips = metrics_ctx.seq_skips
    frames_a_total = 0
    last_drops_snapshot = bridge_start_drops
    last_seq_skips_snapshot = bridge_start_seq_skips
    stall_window_count = 0
    stall_event = asyncio.Event()
    log.info("phase.bridge.started", user_id=user_id)

    async def pump_to_telegram() -> None:
        # ntgcalls' send_external_frame is 10 ms-granular (WebRTC native
        # Opus tick). The incoming side confirms this: contact frames
        # arrive as 960 B / 48 kHz / int16 = 10 ms. Feeding 20 ms chunks
        # in one call gets interpreted as one 10 ms frame → 2× speedup
        # ("rushed/unintelligible" symptom). Split each Twilio 20 ms
        # frame into two 10 ms halves and submit both.
        while True:
            item = await audio_inbound.get()
            if item is None:
                return
            frame, recv_ns = item
            queue_dwell.record(recv_ns)
            t_resample = time.monotonic_ns()
            # Direct 8 → 48 kHz: the BRIDGE phase has no need for the 24 kHz
            # OpenAI-Realtime intermediate. One soxr call instead of two.
            pcm48 = mulaw_to_pcm16(frame, sr_out=48000)
            resample.record(t_resample)
            # Wall-clock capture instant for Frame.Info.capture_time. Each
            # Twilio 20 ms frame becomes two 10 ms ntgcalls submissions, so
            # the second half advances by 10 ms.
            capture_ms = (recv_ns + _MONOTONIC_TO_EPOCH_NS) // 1_000_000
            half = len(pcm48) // 2
            try:
                t_send = time.monotonic_ns()
                await telegram_bridge.send_to_contact(
                    user_id, pcm48[:half], capture_time_ms=capture_ms
                )
                send_to_contact_sw.record(t_send)
                t_send = time.monotonic_ns()
                await telegram_bridge.send_to_contact(
                    user_id, pcm48[half:], capture_time_ms=capture_ms + 10
                )
                send_to_contact_sw.record(t_send)
            except Exception:
                log.exception("phase.bridge.send_to_contact_failed")
                return
            if bridge_metrics.first_send_ns == 0:
                bridge_metrics.first_send_ns = time.monotonic_ns()
                log.info(
                    "phase.bridge.first_send_to_contact",
                    bridge_to_first_send_ms=round(
                        (
                            bridge_metrics.first_send_ns
                            - bridge_metrics.bridge_started_ns
                        )
                        / 1e6,
                        2,
                    ),
                )

    def _emit_window() -> None:
        nonlocal frames_a_total, last_drops_snapshot, last_seq_skips_snapshot
        nonlocal stall_window_count
        # Each Twilio inbound frame is split into 2× 10 ms halves before
        # ntgcalls send, so divide the stopwatch count by 2 to get
        # frames_a (one per inbound μ-law packet).
        a_count = send_to_contact_sw.count() // 2
        # Snapshot inbound activity BEFORE metrics_ctx.inbound_interval.reset()
        # below — used
        # by the stall detector below to distinguish "WS dead" from "WS
        # alive but pump stuck".
        inbound_count = metrics_ctx.inbound_interval.count()
        # Contact-leg liveness (Lane B) — snapshot before the reset block.
        # Non-zero means the call is still up even if the caller's inbound
        # leg (a_count / inbound_count) has gone quiet, so the stall
        # detector must not fire (a real call once tore down on that false positive).
        contact_count = bridge_metrics.tg_frames_per_callback.count()
        cur_drops = metrics_ctx.drops_total
        cur_skips = metrics_ctx.seq_skips
        log.info(
            "phase.bridge.latency",
            # Carrier-side (inbound from Twilio)
            inbound_jitter_p50_ms=round(metrics_ctx.inbound_interval.p50(), 2),
            inbound_jitter_p95_ms=round(metrics_ctx.inbound_interval.p95(), 2),
            inbound_jitter_max_ms=round(metrics_ctx.inbound_interval.max(), 2),
            seq_skips=cur_skips - last_seq_skips_snapshot,
            # Our pipe — caller → contact
            queue_dwell_p50_ms=round(queue_dwell.p50(), 2),
            queue_dwell_p95_ms=round(queue_dwell.p95(), 2),
            queue_dwell_max_ms=round(queue_dwell.max(), 2),
            resample_max_us=int(resample.max() * 1000),
            send_to_contact_p50_ms=round(send_to_contact_sw.p50(), 2),
            send_to_contact_p95_ms=round(send_to_contact_sw.p95(), 2),
            send_to_contact_max_ms=round(send_to_contact_sw.max(), 2),
            frames_a=a_count,
            # Telegram side — contact → us
            tg_callback_interval_p50_ms=round(
                bridge_metrics.tg_callback_interval.p50(), 2
            ),
            tg_callback_interval_p95_ms=round(
                bridge_metrics.tg_callback_interval.p95(), 2
            ),
            tg_callback_interval_max_ms=round(
                bridge_metrics.tg_callback_interval.max(), 2
            ),
            tg_frames_per_callback_p50=int(bridge_metrics.tg_frames_per_callback.p50()),
            tg_frames_per_callback_max=int(bridge_metrics.tg_frames_per_callback.max()),
            tg_bytes_per_callback_p50=int(bridge_metrics.tg_bytes_per_callback.p50()),
            tg_bytes_per_callback_max=int(bridge_metrics.tg_bytes_per_callback.max()),
            frames_b=contact_count,  # same pre-reset count the stall detector reads
            # Our pipe — contact → caller (return)
            ws_send_to_twilio_p50_ms=round(bridge_metrics.ws_send_to_twilio.p50(), 2),
            ws_send_to_twilio_p95_ms=round(bridge_metrics.ws_send_to_twilio.p95(), 2),
            ws_send_to_twilio_max_ms=round(bridge_metrics.ws_send_to_twilio.max(), 2),
            pcm_to_mulaw_max_us=int(bridge_metrics.pcm_to_mulaw.max() * 1000),
            # Queue health
            audio_inbound_qsize=audio_inbound.qsize(),
            audio_inbound_drops=cur_drops - last_drops_snapshot,
        )
        frames_a_total += a_count
        last_drops_snapshot = cur_drops
        last_seq_skips_snapshot = cur_skips
        # Reset all windowed buffers.
        metrics_ctx.inbound_interval.reset()
        queue_dwell.reset()
        resample.reset()
        send_to_contact_sw.reset()
        bridge_metrics.tg_callback_interval.reset()
        bridge_metrics.tg_frames_per_callback.reset()
        bridge_metrics.tg_bytes_per_callback.reset()
        bridge_metrics.ws_send_to_twilio.reset()
        bridge_metrics.pcm_to_mulaw.reset()
        # Stall detector — tear down only when the bridge is silent in
        # BOTH directions (no caller audio in, none arriving from the
        # contact). A live contact leg (contact_count > 0) proves the call
        # is up even if the caller's inbound media has paused, so we ride
        # through it — the fix for that incident. When the PSTN call truly ends,
        # the WS still lingers ~13 s (ws_closed backstop); a genuine
        # both-way-dead bridge trips this after BRIDGE_STALL_TEAR_DOWN_S s.
        stall_window_count, fire = _stall_step(
            a_count,
            inbound_count,
            contact_count,
            stall_window_count,
            settings.bridge_stall_tear_down_s,
        )
        if fire and not stall_event.is_set():
            stall_event.set()

    async def emit_bridge_metrics() -> None:
        while True:
            await asyncio.sleep(1.0)
            _emit_window()

    pump_task = asyncio.create_task(pump_to_telegram())
    ended_task = asyncio.create_task(telegram_bridge.bridge_ended_event.wait())
    closed_task = asyncio.create_task(ws_closed.wait())
    timeout_task = asyncio.create_task(asyncio.sleep(timeout_s))
    stall_task = asyncio.create_task(stall_event.wait())
    metrics_task = asyncio.create_task(emit_bridge_metrics())

    try:
        done, _pending = await asyncio.wait(
            [pump_task, ended_task, closed_task, timeout_task, stall_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ended_task in done:
            # A sidecar crash also sets bridge_ended_event (synthesized,
            # ADR 0017); the reason string is the discriminator vs a normal
            # contact hangup, so the caller can apologize + offer to retry.
            reason = (
                "sidecar_crashed"
                if getattr(telegram_bridge, "_last_bridge_ended_reason", None)
                == "sidecar_crashed"
                else "bridge_ended"
            )
        elif closed_task in done:
            reason = "ws_closed"
        elif stall_task in done:
            reason = "inbound_stalled"
        elif timeout_task in done:
            reason = "timeout"
        else:
            reason = "pump_done"
        # Roll the trailing partial window into the totals so
        # phase.bridge.ended carries the true counts. We don't emit a
        # partial phase.bridge.latency for it — sub-second samples have
        # unstable p-numbers.
        frames_a_total += send_to_contact_sw.count() // 2
        log.info(
            "phase.bridge.ended",
            user_id=user_id,
            reason=reason,
            bridge_wall_s=round(time.monotonic() - bridge_start_monotonic, 2),
            frames_a_total=frames_a_total,
            tg_callbacks_total=bridge_metrics.tg_callbacks_total,
            drops_total=metrics_ctx.drops_total - bridge_start_drops,
            seq_skips_total=metrics_ctx.seq_skips - bridge_start_seq_skips,
        )
        return reason
    finally:
        await _settle_tasks(
            pump_task, ended_task, closed_task, timeout_task, stall_task, metrics_task
        )
        telegram_bridge.register_callbacks(None, None)
        metrics_ctx.in_bridge_phase = False
        try:
            await telegram_bridge.hangup(user_id)
        except Exception:
            log.exception("phase.bridge.hangup_failed_in_cleanup")


async def _run_directline_phase(
    *,
    ws_closed: asyncio.Event,
    audio_inbound: asyncio.Queue[tuple[bytes, int] | None],
    send_to_twilio: _SendToTwilio,
    telegram_bridge: TelegramBridge,
    session: DirectlineSession,
    metrics_ctx: _BridgeMetricsCtx,
    journal: CallJournal | None = None,
) -> _DirectlineOutcome:
    """Caller-side post-gate routing for a directline call.

    By the time we're called: ``session.gate_done_event`` has been set
    (the WS just opened), the TG ring task is alive (possibly already
    resolved). Branch on its state:

    - **TG already answered** → straight to BRIDGE.
    - **TG still ringing** → play Nokia hum to the caller while we
      await ``tg_task`` or ``ws_closed``. Caller frames are drained so
      the queue can't back up during the hum.
    - **TG resolved to None** (timeout / error) → return fallback=True
      so media() spins up the agent with the no-answer apology.
    - **WS closes during the wait** → cancel the TG ring and exit
      cleanly (no the agent fallback — the caller is gone).

    Bridge exits don't bounce back to the agent: once the contact answers
    the call is theirs, end-to-end. That's the whole point of the
    feature (one stable voice end-to-end).
    """
    contact_name = session.contact.first_name
    tg_task = session.tg_task
    if tg_task is None:
        log.error("directline.phase.no_tg_task", call_sid=session.call_sid)
        return _DirectlineOutcome(
            fallback=True,
            first_response_instruction=_no_answer_instruction(session.contact),
            reason="no_tg_task",
        )

    if not tg_task.done():
        log.info(
            "directline.phase.waiting_for_tg",
            call_sid=session.call_sid,
            contact=contact_name,
        )
        cancel_hum = asyncio.Event()
        drain_task = asyncio.create_task(
            _drain_audio_inbound(audio_inbound, ws_closed),
            name=f"directline-drain:{session.call_sid}",
        )
        hum_task = asyncio.create_task(
            _ring_audio_driver(send_to_twilio, cancel_hum),
            name=f"directline-hum:{session.call_sid}",
        )
        closed_wait = asyncio.create_task(ws_closed.wait())
        try:
            await asyncio.wait(
                [tg_task, closed_wait],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            cancel_hum.set()
            await _settle_tasks(hum_task, drain_task, closed_wait)

        if ws_closed.is_set():
            # Caller hung up before the contact picked up. Run the
            # shared finalize helper: cancel the ring task, then tear
            # down whichever TG state actually exists (live call →
            # hangup, still-ringing outgoing → direct MTProto discard).
            # Cancelling ``place_call`` alone does NOT send
            # ``phone.discardCall`` — the contact's phone would keep
            # ringing without this step. No the agent fallback either way.
            await _finalize_directline_session(
                session, telegram_bridge, where="ws_closed_during_ring"
            )
            return _DirectlineOutcome(reason="ws_closed_during_ring")

    try:
        user_id = tg_task.result()
    except Exception:
        log.exception("directline.tg_task_raised", call_sid=session.call_sid)
        user_id = None

    if user_id is None:
        log.info(
            "directline.phase.no_answer",
            call_sid=session.call_sid,
            contact=contact_name,
        )
        if journal is not None:
            journal.append("contact_no_answer", name=contact_name)
        return _DirectlineOutcome(
            fallback=True,
            first_response_instruction=_no_answer_instruction(session.contact),
            reason="tg_no_answer",
        )

    # Both sides ready — bridge them.
    log.info(
        "directline.phase.bridging",
        call_sid=session.call_sid,
        contact=contact_name,
        user_id=user_id,
    )
    if metrics_ctx.ring_started_ns == 0:
        metrics_ctx.ring_started_ns = time.monotonic_ns()
    if journal is not None:
        journal.append("bridge_started", name=contact_name)
    exit_reason = await _run_bridge_phase(
        ws_closed=ws_closed,
        audio_inbound=audio_inbound,
        send_to_twilio=send_to_twilio,
        telegram_bridge=telegram_bridge,
        user_id=user_id,
        metrics_ctx=metrics_ctx,
    )
    if journal is not None:
        journal.append("bridge_ended", name=contact_name, reason=exit_reason)
    log.info(
        "directline.phase.ended",
        call_sid=session.call_sid,
        exit_reason=exit_reason,
    )
    if exit_reason == "sidecar_crashed":
        # Sidecar SIGSEGV mid-bridge (ADR 0017): play the caller the fixed
        # crash recording, then end the call. fallback stays False so media()
        # falls through to teardown (ws.close()) — no the agent loop, no re-ring.
        await _play_ulaw_clip(send_to_twilio, ws_closed)
        return _DirectlineOutcome(reason="bridge_sidecar_crashed")
    return _DirectlineOutcome(reason=f"bridge_{exit_reason}")


_RECONNECT_INSTRUCTION = (
    "Say very briefly, in a single sentence, that you're back with the "
    'caller. For example: "Hi, I\'m back."'
)


# Family-relationship words (accent/case folded). When a contact carries
# one as a keyword, the agent voices "<first name>, their <relationship>"
# on a no-answer, so the target is named specifically enough to survive
# into a retry turn. Surname/other keywords are skipped so they're never
# spoken.
_RELATIONSHIP_KEYWORDS: frozenset[str] = frozenset(
    {
        "brother",
        "sister",
        "son",
        "daughter",
        "mom",
        "mother",
        "dad",
        "father",
        "uncle",
        "aunt",
        "nephew",
        "niece",
        "cousin",
        "grandpa",
        "grandfather",
        "grandma",
        "grandmother",
        "grandson",
        "granddaughter",
        "husband",
        "wife",
        "godfather",
        "godmother",
        "godson",
        "goddaughter",
    }
)


def _contact_descriptor(contact: Contact) -> str:
    """A warm, spoken identifier: first name plus a relationship keyword
    when one is on file (e.g. "Nia, their niece"). Never the telegram
    handle — that stays internal."""
    for kw in contact.keywords:
        if _fold(kw) in _RELATIONSHIP_KEYWORDS:
            return f"{contact.first_name}, their {kw.lower()}"
    return contact.first_name


def _no_answer_instruction(contact: Contact) -> str:
    who = _contact_descriptor(contact)
    return (
        f"{who} didn't pick up. Tell the caller warmly, in one or two "
        f"sentences, that {who} didn't answer, and ask whether they want "
        f"you to try {contact.first_name} again or would rather do "
        "something else. Don't read this as a script; never say usernames "
        "or app names."
    )


_TIMEOUT_INSTRUCTION = (
    "Sorry, the call dropped on my end. What else can I help you with?"
)


async def _call_teardown(
    *,
    ws: WebSocket,
    reader_task: asyncio.Task[None],
    close_signal_task: asyncio.Task[None],
    from_number_param: str | None,
    call_sid: str | None,
    bound: bool,
    journal: CallJournal | None = None,
) -> None:
    """End-of-call cleanup, factored out of ``media()`` for readability.

    Owns, in order:

    1. Spawning the post-call memory-merge task (fire-and-forget; the only
       thing that intentionally outlives the call).
    2. Cancelling + awaiting ``reader_task`` and ``close_signal_task``.
    3. Defensively clearing per-call ``TelegramBridge`` callbacks (the
       BRIDGE phase normally clears them; this is belt-and-suspenders).
    4. Unbinding the structlog contextvars bound at ``start`` event time.
    5. Closing the Twilio Media Streams WS.

    Behavior is identical to the prior inline ``finally`` block; this is
    a pure move + name so the stage boundary is visible in ``media()``.

    Also stamps the lifecycle journal with ``ended`` — a clean in-process
    termination. Its *absence* in the journal is the signal that the call
    ended abnormally (process died mid-call); the call-processed email reads
    that to flag the report.
    """
    if journal is not None:
        journal.append("ended")
    # 1. Post-call memory merge — fire-and-forget so WS close stays snappy.
    if from_number_param and call_sid:
        transcript_path = TRANSCRIPTS_DIR / f"{call_sid}.jsonl"
        asyncio.create_task(
            maybe_update_memory(from_number_param, call_sid, transcript_path)
        )
    # 2. Stop the reader and the close-signal bridge.
    for t in (reader_task, close_signal_task):
        if not t.done():
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
    # 3. Drop any per-call bridge callbacks the BRIDGE phase might have
    # left wired (defensive — _run_bridge_phase already clears them).
    bridge_local: TelegramBridge | None = getattr(ws.app.state, "telegram_bridge", None)
    if bridge_local is not None:
        bridge_local.register_callbacks(None, None)
    # 4. Unbind contextvars so the next call's logs don't inherit them.
    if bound:
        structlog.contextvars.unbind_contextvars(
            "call_sid", "stream_sid", "from_number", "skip_gate"
        )
    # 5. Close the Twilio WS.
    try:
        await ws.close()
    except RuntimeError:
        pass


@router.websocket("/twilio/media")
async def media(ws: WebSocket) -> None:
    """Twilio Media Streams WebSocket bridged to the agent's phase loop.

    Three phases the WS cycles through within one PSTN call:

    - **AGENT**: a RealtimeSession is active; caller talks to the agent.
    - **RING**: synthetic ringback to caller while we place a contact call.
    - **BRIDGE**: caller ↔ contact direct audio; agent is fully out of the
      loop. Returns to AGENT with a reconnect line when the contact hangs up.

    A long-lived reader task feeds ``audio_inbound`` with μ-law frames.
    Each phase consumes from that queue; on WS close, a sentinel
    terminates the active phase and the loop exits.
    """
    await ws.accept()
    log.info("twilio.media.ws.accepted")

    # === stage: SETUP — per-call state, reader task, close-signal task ===
    # Everything from here through `close_signal_task` is one-time wiring.
    # Per-call state mutated by the ws_reader closure below.
    media_count = 0
    bound = False
    stream_sid: str | None = None
    from_number_param: str | None = None
    call_sid: str | None = None
    skip_gate_param = False
    directline_call_sid_param: str | None = None
    caller_memory_text: str | None = None
    journal: CallJournal | None = None
    start_seen = asyncio.Event()
    ws_closed = asyncio.Event()
    # (μ-law frame, twilio media.timestamp in ms) tuples; consumed by
    # whichever phase is active. The timestamp travels with the frame so
    # the BRIDGE phase can compute direction-A latency at submit time
    # (per `_latency.py`). AGENT and RING discard it. Bounded so a stuck phase can't grow it
    # unboundedly — on overflow we drop the oldest frame (better than
    # backpressure blocking the WS receive loop).
    audio_inbound: asyncio.Queue[tuple[bytes, int] | None] = asyncio.Queue(maxsize=200)
    metrics_ctx = _BridgeMetricsCtx()

    telegram_bridge: TelegramBridge | None = getattr(
        ws.app.state, "telegram_bridge", None
    )

    async def send_to_twilio(mulaw: bytes) -> None:
        if stream_sid is None:
            return
        # Book the bytes even if the send below is swallowed: over-holding
        # the floor on a dying WS is harmless; under-holding leaks.
        playout.note_sent(len(mulaw))
        try:
            await ws.send_json(
                {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(mulaw).decode("ascii")},
                }
            )
        except WebSocketDisconnect, RuntimeError:
            pass

    async def send_mark_to_twilio(name: str) -> None:
        if stream_sid is None:
            return
        try:
            await ws.send_json(
                {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": name},
                }
            )
        except WebSocketDisconnect, RuntimeError:
            pass

    async def clear_twilio_playback() -> None:
        if stream_sid is None:
            return
        try:
            await ws.send_json({"event": "clear", "streamSid": stream_sid})
            log.info("twilio.media.clear_sent")
            # Buffer discarded → nothing is draining anymore. Twilio
            # echoes any pending marks after a clear; they land on the
            # tracker's idempotent no-op path. Reset only on a
            # *successful* clear — if the send failed, audio is still
            # draining and the floor must stay held.
            playout.reset()
        except WebSocketDisconnect, RuntimeError:
            pass

    playout = PlayoutTracker(
        send_mark=send_mark_to_twilio,
        grace_s=settings.playout_mark_grace_s,
    )

    async def ws_reader() -> None:
        nonlocal media_count, bound, stream_sid
        nonlocal from_number_param, call_sid, skip_gate_param, caller_memory_text
        nonlocal directline_call_sid_param
        try:
            while True:
                msg = await ws.receive_json()
                event = msg.get("event")

                if event == "media":
                    media_count += 1
                    # During BRIDGE the per-second phase.bridge.latency
                    # line subsumes this counter — suppress to avoid
                    # noise. Keep it during AGENT / RING so the
                    # 50 fps liveness check is still visible.
                    if not metrics_ctx.in_bridge_phase and (
                        media_count == 1 or media_count % MEDIA_LOG_EVERY == 0
                    ):
                        log.info(
                            "twilio.media.frame",
                            count=media_count,
                            seq=msg.get("sequenceNumber"),
                            media_timestamp=(msg.get("media") or {}).get("timestamp"),
                            track=(msg.get("media") or {}).get("track"),
                        )
                    payload = (msg.get("media") or {}).get("payload")
                    if payload:
                        raw = base64.b64decode(payload)
                        # Stamp at WS receive in monotonic ns. Downstream
                        # consumers (queue dwell, send-side stopwatches)
                        # need a wall-clock-aligned anchor; Twilio's
                        # media.timestamp is stream-relative and not
                        # comparable to time.monotonic_ns().
                        recv_ns = time.monotonic_ns()
                        metrics_ctx.inbound_interval.tick(recv_ns)
                        # Detect Twilio sequenceNumber gaps. Twilio
                        # streams these as 1, 2, 3, … as strings; a gap
                        # means the carrier or Twilio's edge dropped
                        # frames before they reached us.
                        seq_raw = msg.get("sequenceNumber")
                        try:
                            cur_seq = int(seq_raw) if seq_raw is not None else None
                        except TypeError, ValueError:
                            cur_seq = None
                        if cur_seq is not None:
                            if (
                                metrics_ctx.last_seq is not None
                                and cur_seq != metrics_ctx.last_seq + 1
                            ):
                                gap = cur_seq - metrics_ctx.last_seq - 1
                                metrics_ctx.seq_skips += max(gap, 0)
                                log.warning(
                                    "twilio.media.seq_skip",
                                    expected=metrics_ctx.last_seq + 1,
                                    got=cur_seq,
                                    gap=gap,
                                )
                            metrics_ctx.last_seq = cur_seq
                        item = (raw, recv_ns)
                        try:
                            audio_inbound.put_nowait(item)
                        except asyncio.QueueFull:
                            # Drop oldest, retain newest — keeps the agent
                            # responsive to current caller speech.
                            try:
                                audio_inbound.get_nowait()
                                audio_inbound.put_nowait(item)
                                metrics_ctx.drops_total += 1
                            except asyncio.QueueEmpty, asyncio.QueueFull:
                                pass
                    continue

                if event == "start":
                    start = msg.get("start") or {}
                    custom_params = start.get("customParameters") or {}
                    from_number_param = custom_params.get("from") or None
                    skip_gate_param = (
                        str(custom_params.get("skip_gate") or "0") == "1"
                    )
                    directline_call_sid_param = (
                        custom_params.get("directline_call_sid") or None
                    )
                    call_sid = start.get("callSid")
                    if not bound:
                        stream_sid = start.get("streamSid")
                        structlog.contextvars.bind_contextvars(
                            call_sid=call_sid,
                            stream_sid=stream_sid,
                            from_number=from_number_param,
                            skip_gate=skip_gate_param,
                        )
                        bound = True
                    log.info("twilio.media.event", **_twilio_kwargs(msg))
                    media_format = start.get("mediaFormat") or {}
                    if media_format != {
                        "encoding": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "channels": 1,
                    }:
                        # Some carriers force a transcode at Twilio's
                        # edge — flag loudly so it correlates with any
                        # latency spike on the call.
                        log.warning(
                            "twilio.media.format_mismatch",
                            media_format=media_format,
                            from_number=from_number_param,
                        )
                    if from_number_param:
                        try:
                            caller_memory_text = load_memory(from_number_param)
                        except Exception:
                            log.exception(
                                "caller_memory.load.failed",
                                phone=from_number_param,
                            )
                        if caller_memory_text:
                            log.info(
                                "caller_memory.loaded",
                                phone=from_number_param,
                                chars=len(caller_memory_text),
                            )
                    start_seen.set()
                    continue

                if event == "stop":
                    log.info(
                        "twilio.media.event",
                        media_frames_total=media_count,
                        **_twilio_kwargs(msg),
                    )
                    return

                if event == "mark":
                    # Twilio confirming playback reached a mark we sent
                    # after a response's last audio chunk (ADR 0020).
                    name = (msg.get("mark") or {}).get("name")
                    if name:
                        playout.mark_echoed(name)
                    continue

                log.info("twilio.media.event", **_twilio_kwargs(msg))
        except WebSocketDisconnect:
            log.info("twilio.media.ws.disconnected", media_frames_total=media_count)

    reader_task = asyncio.create_task(ws_reader())

    async def signal_ws_close() -> None:
        # Bridge reader-task completion to ws_closed + audio queue sentinel
        # so all phase consumers wake up at once. Voice Insights fetch
        # fires from here (not from the stop-event handler) so it runs on
        # *every* call termination — including the phantom-tail case where
        # Twilio never sends `stop` and the WS just times out. The wall-
        # clock at this point feeds the pstn_to_ws_close_drift_ms metric.
        try:
            await reader_task
        finally:
            ws_closed.set()
            try:
                audio_inbound.put_nowait(None)
            except asyncio.QueueFull:
                pass
            if call_sid:
                ws_close_wall_ms = int(time.time() * 1000)
                asyncio.create_task(
                    _fetch_voice_insights_summary(call_sid, ws_close_wall_ms)
                )

    close_signal_task = asyncio.create_task(signal_ws_close())

    try:
        # === stage: WAIT FOR start event — extract call_sid, memory ===
        # Wait for start (extracts call_sid, from_number, memory, etc.) or
        # WS close if the caller hangs up before we ever see one.
        start_task = asyncio.create_task(start_seen.wait())
        closed_task = asyncio.create_task(ws_closed.wait())
        await asyncio.wait(
            [start_task, closed_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in (start_task, closed_task):
            if not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        if not start_seen.is_set():
            return

        # === stage: RECORDING (off by default) ===
        # Strict notice-before-recording ordering: when disclosure is on,
        # the caller hears "This call may be recorded." BEFORE the Twilio
        # recording is kicked (~2 s cost, only when recording at all).
        if settings.recordings_enabled and call_sid:
            if settings.recording_disclosure_enabled:
                await _play_ulaw_clip(
                    send_to_twilio,
                    ws_closed,
                    clip=RECORDING_DISCLOSURE_ULAW_BYTES,
                )
            asyncio.create_task(_start_recording(call_sid))

        # Lifecycle journal for this call — voice() already wrote `received`;
        # we append the phase seams here and `ended` in teardown. Same file
        # (data/calls/<sid>.jsonl); a second CallJournal instance just appends.
        journal = CallJournal(call_sid) if call_sid else None

        send_text_fn: SendTextFn | None = None
        if telegram_bridge is not None:
            bridge_ref = telegram_bridge

            async def send_text_fn(contact: Contact, message: str) -> str:
                return await _tool_send_message(contact, message, bridge_ref)

        first_response_instruction: str | None = None

        # === stage: DIRECTLINE PHASE (if applicable) ===
        # If voice() tagged this stream as a directline, run the
        # caller-side post-gate routing now. On clean exit (bridge ended
        # normally, caller hung up, etc.) we skip the agent loop entirely.
        # On fallback (TG no-answer), we set `first_response_instruction`
        # + `initial_session = False` and enter the loop so the agent apologises.
        if directline_call_sid_param:
            dl_session = directlines.pop_session(directline_call_sid_param)
            if dl_session is None:
                # Orphan cleanup got there first, or voice() never
                # registered. Close the WS.
                log.error(
                    "directline.session_missing",
                    call_sid=call_sid,
                    directline_call_sid=directline_call_sid_param,
                )
                return
            dl_session.gate_done_event.set()
            if telegram_bridge is None:
                log.error(
                    "directline.no_bridge_at_media",
                    call_sid=call_sid,
                )
                if dl_session.tg_task is not None and not dl_session.tg_task.done():
                    dl_session.tg_task.cancel()
                first_response_instruction = _no_answer_instruction(dl_session.contact)
            else:
                outcome = await _run_directline_phase(
                    ws_closed=ws_closed,
                    audio_inbound=audio_inbound,
                    send_to_twilio=send_to_twilio,
                    telegram_bridge=telegram_bridge,
                    session=dl_session,
                    metrics_ctx=metrics_ctx,
                    journal=journal,
                )
                if not outcome.fallback or ws_closed.is_set():
                    # Bridge handled the call (or it's already over — incl. a
                    # sidecar crash, which plays the recording then ends here);
                    # teardown via the finally.
                    return
                first_response_instruction = outcome.first_response_instruction

        # === stage: PHASE LOOP — AGENT / RING / BRIDGE / AGENT ===
        # Each iteration is one AGENT phase; if the agent requests a contact call
        # the loop continues into RING → BRIDGE → back to AGENT. Exits when
        # the WS closes or the agent returns without a bridge_request.
        while not ws_closed.is_set():
            if journal is not None:
                journal.append("agent_started")
            outcome = await _run_agent_phase(
                ws_closed=ws_closed,
                audio_inbound=audio_inbound,
                send_to_twilio=send_to_twilio,
                clear_twilio_playback=clear_twilio_playback,
                playout=playout,
                call_sid=call_sid,
                caller_memory=caller_memory_text,
                telegram_bridge=telegram_bridge,
                send_text_fn=send_text_fn,
                first_response_instruction=first_response_instruction,
            )

            if ws_closed.is_set() or outcome.bridge_request is None:
                break

            contact = outcome.bridge_request
            if journal is not None:
                journal.append("contact_call", name=contact.first_name, kind="agent")
            _resolved_name, user_id = await _run_ring_phase(
                ws_closed=ws_closed,
                audio_inbound=audio_inbound,
                send_to_twilio=send_to_twilio,
                telegram_bridge=telegram_bridge,
                contact=contact,
                metrics_ctx=metrics_ctx,
            )

            if ws_closed.is_set():
                break

            if user_id is not None and telegram_bridge is not None:
                if journal is not None:
                    journal.append("bridge_started", name=contact.first_name)
                exit_reason = await _run_bridge_phase(
                    ws_closed=ws_closed,
                    audio_inbound=audio_inbound,
                    send_to_twilio=send_to_twilio,
                    telegram_bridge=telegram_bridge,
                    user_id=user_id,
                    metrics_ctx=metrics_ctx,
                )
                if journal is not None:
                    journal.append(
                        "bridge_ended", name=contact.first_name, reason=exit_reason
                    )
                if exit_reason == "ws_closed":
                    break
                if exit_reason == "sidecar_crashed":
                    # Sidecar SIGSEGV mid-bridge (ADR 0017): play the caller the
                    # fixed crash recording, then end the call — no re-ring, no
                    # The agent turn. break → teardown → ws.close().
                    await _play_ulaw_clip(send_to_twilio, ws_closed)
                    break
                if exit_reason == "timeout":
                    first_response_instruction = _TIMEOUT_INSTRUCTION
                else:
                    first_response_instruction = _RECONNECT_INSTRUCTION
            else:
                # Dispatch only sets bridge_request after resolving to a
                # single contact, so this branch is always "found but
                # didn't answer / errored".
                if journal is not None:
                    journal.append("contact_no_answer", name=contact.first_name)
                first_response_instruction = _no_answer_instruction(contact)
    finally:
        # === stage: TEARDOWN — see _call_teardown() ===
        await _call_teardown(
            ws=ws,
            reader_task=reader_task,
            close_signal_task=close_signal_task,
            from_number_param=from_number_param,
            call_sid=call_sid,
            bound=bound,
            journal=journal,
        )
