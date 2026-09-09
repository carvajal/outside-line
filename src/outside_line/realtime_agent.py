"""OpenAI Realtime WS client.

Owns the Realtime session: open the WS, configure it with the GA-shape
``session.update``, push Twilio audio into ``input_audio_buffer`` and
fire a caller-provided coroutine for every ``response.output_audio.delta``
chunk so the Twilio side can send it back down the Media Stream.

Tools: ``search_web``, ``case_brief_lookup``, ``call_contact``,
``message_contact``.

**Per-call freshness.** Every inbound PSTN call constructs a brand-new
``RealtimeSession`` (the Twilio handler does this in the AGENT phase, and
again for each post-BRIDGE reconnect). Every ``__aenter__`` opens a fresh
OpenAI Realtime WS connection, and every ``__aexit__`` tears it down.
All session state (active response id, in-flight tool tasks,
bridge request) is *instance-level* — there is no module- or class-level
mutable state, so no caller's context can leak into another's. The only
thing carried in from outside is the per-caller ``caller_memory`` text
(read fresh from ``data/memory/<phone>.md`` by the handler), which is
the intended exception.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import cast

from openai import AsyncOpenAI

from .audio_bridge import apply_gain, pcm16_to_mulaw
from .config import settings
from .contacts import Contact, find_contacts
from .log import get_logger
from .persona import SYSTEM_PROMPT
from .playout import PlayoutTracker
from .tools.case_brief import case_brief_lookup
from .tools.search import search_web, today_local
from .transcripts import TranscriptWriter

# Dependency-injected closure the twilio handler builds once per session
# for the message_contact tool. The dispatcher resolves a Contact and
# hands it in — the session has no direct Telegram coupling. call_contact
# similarly resolves to a Contact in dispatch and stashes it on
# bridge_request for the handler's RING phase to consume.
SendTextFn = Callable[[Contact, str], Awaitable[str]]

GREETING_INSTRUCTION = 'Greet by saying only: "Hello?"'

# Spoken verbatim by the agent when the tool dispatcher itself blows up
# (worker crash, unknown tool name, bad arg JSON). The `search_web`
# implementation has its own apology for upstream failures; this one
# covers our side of the wire.
_TOOL_RECOVERY_REPLY = "Sorry, I ran into a problem. Could you say that again?"


def _no_match_reply(query: str) -> str:
    """Spoken reply when the directory has zero matches for the caller's clue."""
    q = (query or "").strip()
    if q:
        return f"I couldn't find anyone called '{q}' in the list. What's their exact name?"
    return "Who should I call? Tell me the name."


def _ambiguous_reply(candidates: list[Contact]) -> str:
    """Spoken disambiguation: 'I have X, Y, or Z — which one?'."""
    names = [c.first_name for c in candidates[:3]]
    if len(names) == 2:
        return f"I have {names[0]} or {names[1]} — which one?"
    return f"I have {', '.join(names[:-1])} or {names[-1]} — which one?"

# Custom function tool registered with the Realtime session. The hosted
# `web_search` tool can't be used here — it hangs from Realtime — so we
# expose a custom function and delegate to the Responses API in the
# worker (see `tools/search.py`).
_SEARCH_WEB_TOOL: dict[str, object] = {
    "type": "function",
    "name": "search_web",
    "description": (
        "Look up current information on the web — news, weather, prices, "
        "sports scores, anything that needs fresh data. Returns concrete, "
        "up-to-date detail. For flight prices and fares it may come back "
        "with only an approximate range or an honest 'no exact price': web "
        "search can't see live booking systems, so relay whatever it "
        "returns and never present a fare as confirmed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "What to search for. Pass it in whichever language "
                    "the caller actually used — do NOT translate."
                ),
            },
            "language": {
                "type": "string",
                "enum": ["en", "es"],
                "description": (
                    "The language the caller is currently speaking — "
                    "'en' for English, 'es' for Spanish. The answer "
                    "MUST come back in this language."
                ),
            },
        },
        "required": ["query", "language"],
    },
}

# Custom function tool for case-specific questions. Grounded in a curated
# brief + live CourtListener docket feed inside the tool worker, not via
# any web_search registered here. The description carries the routing
# rule: case questions hit `case_brief_lookup`, everything else stays on
# `search_web`.
_CASE_BRIEF_LOOKUP_TOOL: dict[str, object] = {
    "type": "function",
    "name": "case_brief_lookup",
    "description": (
        "Answer a question about THE legal case described in the "
        "operator-authored case brief — the parties (judge, lawyers), "
        "the docket, what's coming up next, any standing instructions "
        "from the lawyers. Use this whenever the caller asks anything "
        "about their own case. Use `search_web` only for general-world "
        "info (weather, news, prices, sports)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The caller's question, passed through verbatim. Do "
                    "NOT translate or paraphrase."
                ),
            },
            "language": {
                "type": "string",
                "enum": ["en", "es"],
                "description": (
                    "The language the caller is currently speaking — "
                    "'en' for English, 'es' for Spanish. The answer "
                    "MUST come back in this language."
                ),
            },
        },
        "required": ["question", "language"],
    },
}

# Contact tools, registered only when a TelegramBridge is wired into the
# session. Descriptions are intentionally transport-agnostic — the agent
# never names Telegram in voice (hard product constraint; see the NOTE in
# persona.py).
_CALL_CONTACT_TOOL: dict[str, object] = {
    "type": "function",
    "name": "call_contact",
    "description": (
        "Place a voice call to a family member or friend and bridge them "
        "in with the caller. Use when the caller asks to speak to "
        "someone (e.g. 'call Rigoberto for me', 'call my mom', 'put my "
        "brother on'). Pass whatever the caller said — name, alias, "
        "relationship — and the system will resolve it. It is safe to "
        "call this more than once in the same conversation: if the tool "
        "comes back with a question ('which one?' or 'no match'), "
        "speak that to the caller verbatim, wait for their answer, then "
        "call this again with the new clue. After a successful call, "
        "you will be muted automatically while the two of them talk; do "
        "NOT keep talking once the bridge is up."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "first_name": {
                "type": "string",
                "description": (
                    "The contact reference as the caller said it — name, "
                    "alias, or relationship ('my brother', 'mom'). "
                    "Preserve accents and capitalization."
                ),
            },
        },
        "required": ["first_name"],
    },
}

_MESSAGE_CONTACT_TOOL: dict[str, object] = {
    "type": "function",
    "name": "message_contact",
    "description": (
        "Send a short text message to a family member or friend. Use "
        "when the caller asks to relay a message rather than place a "
        "call (e.g. 'tell Rigoberto I'll call him at 4', "
        "'tell my brother I'm okay'). Pass whatever the caller said — "
        "name, alias, relationship — and the system will resolve it. "
        "It is safe to call this more than once: if the tool comes "
        "back with a question ('which one?' or 'no match'), speak "
        "that to the caller, wait for their answer, then call this "
        "again with the new clue. Compose the message body in FIRST "
        "PERSON as if it came from the caller (not narrated by you), "
        "in the caller's language, ≤200 chars. Do NOT mention how the "
        "message is delivered."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "first_name": {
                "type": "string",
                "description": (
                    "Recipient reference as the caller said it — name, "
                    "alias, or relationship ('my brother', 'mom')."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "The full message body to send, in first person, in "
                    "the caller's language. Do NOT include greeting "
                    "boilerplate like 'Hi Rigoberto:' — just the content."
                ),
            },
        },
        "required": ["first_name", "message"],
    },
}

log = get_logger(__name__)

AudioCallback = Callable[[bytes], Awaitable[None]]
ClearCallback = Callable[[], Awaitable[None]]

# Realtime API: PCM uses 24 kHz, μ-law/A-law are 8 kHz. Both sides of the
# bridge resample through `audio_bridge`.
REALTIME_PCM_RATE = 24000


class RealtimeSession:
    """Async context manager around a single Realtime WS connection.

    Usage:

        async with RealtimeSession(on_audio_out=send_to_twilio) as rt:
            await rt.kickoff_greeting()
            # ... in parallel: rt.send_audio_pcm16(...) and rt.receive_loop()
    """

    def __init__(
        self,
        *,
        on_audio_out: AudioCallback,
        on_clear: ClearCallback | None = None,
        playout: PlayoutTracker | None = None,
        call_sid: str | None = None,
        caller_memory: str | None = None,
        bridge_available: bool = False,
        send_text_fn: SendTextFn | None = None,
        first_response_instruction: str | None = None,
    ) -> None:
        self._on_audio_out = on_audio_out
        # Per-caller memory file content (markdown). When present, it's
        # appended to the base persona instructions so the agent greets and
        # treats the caller with continuity. Lifecycle: loaded in
        # twilio_handler before constructing the session, updated by a
        # background LLM-merge task after the call ends (memory_updater).
        self._caller_memory = caller_memory
        # Per-call transcript writer. Optional — local smoke tests
        # construct the session without a call_sid and skip persistence.
        self._transcripts: TranscriptWriter | None = (
            TranscriptWriter(call_sid) if call_sid else None
        )
        # Optional callback fired on caller barge-in so the transport
        # (Twilio Media Streams) can drop audio it has already queued
        # for playback. Without this, the server's response.cancel only
        # stops *future* deltas — the chunks already on the wire keep
        # playing until Twilio's buffer drains.
        self._on_clear = on_clear
        # WS-scoped playout tracker (ADR 0020). Optional so smoke tests
        # and non-Twilio harnesses can run without one; when present it
        # extends the floor past response.done until Twilio's playout
        # buffer actually drains (mark echo, or byte-clock + grace).
        self._playout = playout
        self._cm: object | None = None
        self._conn: object | None = None  # AsyncRealtimeConnection at runtime
        # Single-flag floor. Either the agent has the floor (it is speaking, OR
        # a tool is running on its behalf and the answer is still to come)
        # or the caller does. Two pieces of state are enough to decide:
        #   - _active_response_id: set on response.created, cleared on
        #     response.done. Tracks the agent's currently-speaking response.
        #   - _tool_tasks: non-empty while at least one tool worker is
        #     in flight. Bridges the gap between the agent's preamble
        #     response.done and the eventual response.create that speaks
        #     the tool result. WITHOUT this bridge, caller PCM in the
        #     gap would commit a new turn and the tool result would
        #     arrive into an unrelated context (the original bug).
        # Read together via _agent_has_floor(). No queues, no turn ids,
        # no per-response gating: one tool call at a time, one response
        # at a time, results post unconditionally.
        self._active_response_id: str | None = None
        self._tool_tasks: dict[str, asyncio.Task[None]] = {}
        # Wall-clock start of the current response (set on response.created,
        # cleared on response.done). Used to log first-audio-delta latency
        # once per response — the only telemetry kept from the old
        # per-turn timing machinery.
        self._response_started_at: float | None = None
        self._first_delta_logged: bool = False
        # Caller frames dropped because the agent had the floor. Periodic info
        # log so the operator can see the no-floor-for-caller gate is
        # actually firing on noisy lines. Reset at gate_reopened — the
        # first frame uploaded after the floor (incl. playout drain)
        # releases — so the count measures the full muted window.
        self._dropped_input_frames: int = 0
        # Contact tools. `bridge_available` gates the registration
        # of `call_contact` — the handler sets it True when the Telegram
        # bridge singleton is alive. `send_text_fn` is the DI closure for
        # `message_contact` (still spoken inline; no teardown).
        self._bridge_available = bridge_available
        self._send_text_fn = send_text_fn
        # One-shot opening instruction for the first response. None on the
        # initial session (uses GREETING_INSTRUCTION); set on reconnect
        # sessions after a RING/BRIDGE so the agent can open with "I'm
        # back", "{name} didn't pick up", etc.
        self._first_response_instruction = first_response_instruction
        # Set by the `call_contact` tool branch when dispatch has resolved
        # a single contact from the caller's query. The handler races
        # receive_loop against bridge_request_event, then runs RING/BRIDGE
        # phases against this Contact. Ambiguous / not-found / "many"
        # cases are spoken back to the caller inside dispatch and DO NOT
        # set this — the session continues so the caller can clarify.
        self.bridge_request: Contact | None = None
        self.bridge_request_event = asyncio.Event()

    async def __aenter__(self) -> "RealtimeSession":
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._cm = client.realtime.connect(model=settings.openai_realtime_model)
        self._conn = await self._cm.__aenter__()  # type: ignore[attr-defined]
        await self._configure()
        log.info(
            "realtime.session.opened",
            model=settings.openai_realtime_model,
            voice=settings.openai_realtime_voice,
            reasoning_effort=settings.openai_realtime_reasoning_effort,
            transcription_model=settings.openai_transcription_model,
            transcription_language=settings.openai_transcription_language,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Settle cancelled tool tasks before closing the OpenAI WS so they
        # don't keep running (and logging) past `realtime.session.closed`.
        tasks = list(self._tool_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self._cm is not None:
                await self._cm.__aexit__(exc_type, exc, tb)  # type: ignore[attr-defined]
        finally:
            self._cm = None
            self._conn = None
            self._active_response_id = None
            log.info("realtime.session.closed")

    async def _configure(self) -> None:
        assert self._conn is not None
        tools = [_SEARCH_WEB_TOOL, _CASE_BRIEF_LOOKUP_TOOL]
        if self._bridge_available:
            tools.append(_CALL_CONTACT_TOOL)
        if self._send_text_fn is not None:
            tools.append(_MESSAGE_CONTACT_TOOL)
        instructions = SYSTEM_PROMPT
        if self._caller_memory:
            instructions = (
                f"{SYSTEM_PROMPT}\n\n"
                "# Context about the caller\n\n"
                "This is what you know about the person on the phone. "
                "Use it naturally to treat them with continuity — do "
                "NOT read it out or announce that you have notes; just "
                "let it inform how you talk to them.\n\n"
                f"{self._caller_memory.strip()}\n"
            )
            log.info(
                "realtime.instructions.memory_appended",
                memory_chars=len(self._caller_memory),
            )
        # Give the agent today's date (in the configured agent timezone) so
        # it never states a stale one from training memory. Same anchor
        # search_web uses for its freshness signal.
        instructions = (
            f"{instructions}\n\n# Today's date\n\n"
            f"Today is {today_local()}. Use it as the reference if asked "
            "the date or the day; never give a stale date from memory.\n"
        )
        # Side-channel transcription of the caller's speech (ADR 0024).
        # Language key is omitted when blank so the model auto-detects.
        transcription_cfg: dict[str, str] = {
            "model": settings.openai_transcription_model
        }
        if settings.openai_transcription_language:
            transcription_cfg["language"] = settings.openai_transcription_language
        session_cfg = {
            "type": "realtime",
            "instructions": instructions,
            # gpt-realtime-2 exposes an internal-reasoning knob like the
            # text models do. Higher effort = the model thinks longer
            # before speaking, which is what we want for a low-volume,
            # high-stakes persona call.
            "reasoning": {"effort": settings.openai_realtime_reasoning_effort},
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": REALTIME_PCM_RATE},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": settings.realtime_vad_threshold,
                        "prefix_padding_ms": settings.realtime_vad_prefix_padding_ms,
                        "silence_duration_ms": (
                            settings.realtime_vad_silence_duration_ms
                        ),
                        # No barge-in: while the agent is speaking, caller audio
                        # is ignored entirely. Noisy phone lines make
                        # barge-in mis-fire (background voices,
                        # bumps, breath) cut the agent off mid-sentence. We
                        # short-circuit `send_audio_pcm16` while a response
                        # is active (the real lever) and tell the server
                        # not to interrupt its own response either, so the
                        # two layers agree.
                        "interrupt_response": False,
                        "create_response": True,
                    },
                    "transcription": transcription_cfg,
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": REALTIME_PCM_RATE},
                    "voice": settings.openai_realtime_voice,
                    "speed": settings.openai_realtime_output_speed,
                },
            },
            "tools": tools,
        }
        await self._conn.session.update(session=session_cfg)  # type: ignore[attr-defined]

    async def kickoff_greeting(self) -> None:
        """Tell the server to generate the opening line so the agent speaks
        first — without this the model waits for caller audio and the
        line is silent right after the DTMF tone.

        On the initial session this is the canonical short "Hello?".
        On a session rebuilt after a RING/BRIDGE the handler passes a
        ``first_response_instruction`` so the agent opens with the right
        framing ("I'm back", "{name} didn't pick up", …).
        """
        assert self._conn is not None
        instruction = self._first_response_instruction or GREETING_INSTRUCTION
        await self._conn.response.create(  # type: ignore[attr-defined]
            response={"instructions": instruction},
        )
        log.info(
            "realtime.greeting.requested",
            custom=self._first_response_instruction is not None,
        )

    async def _nudge_repeat_on_empty(self) -> None:
        """Inject a system note telling the agent to ask for a repeat.

        Fired when whisper returns an empty transcript (server VAD
        committed on silence/noise). The note lands in conversation
        history before — or, racily, alongside — the auto-created
        response; even if the current turn improvises, the next turn
        sees the rule. The persona's "ask to repeat" clause is the
        primary lever; this is a safety net for the pure-silence case.
        """
        if self._conn is None:
            return
        nudge_text = (
            "The caller's audio was unintelligible. Say only: 'Sorry, "
            "could you repeat that?' (in the caller's language). "
            "Nothing else."
        )
        try:
            await self._conn.conversation.item.create(  # type: ignore[attr-defined]
                item={
                    "type": "message",
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": nudge_text,
                        }
                    ],
                },
            )
            log.info("realtime.transcript.empty.nudged")
        except Exception:
            log.exception("realtime.transcript.empty.nudge_failed")

    async def _clear_input_buffer_safe(self) -> None:
        """Wipe the OpenAI side's input audio buffer; log + swallow errors.

        Called at two lifecycle moments — right after ``response.created``
        (drop pre-gate uploads that raced the engagement) and right after
        ``response.done`` of the just-finished response (drop partial
        uploads that arrived between gate close and Twilio quiescing).
        Both call sites previously inlined the same try/except + log;
        kept in one place so a future buffer-handling change moves once.
        """
        try:
            await self._conn.input_audio_buffer.clear()  # type: ignore[attr-defined]
        except Exception:
            log.exception("realtime.input_buffer.clear_failed")

    def _agent_has_floor(self) -> bool:
        """True iff the agent is mid-turn — speaking, running a tool whose
        result it will speak, or *still audible* while Twilio drains
        already-generated audio (the playout term; ADR 0020). Caller PCM
        is dropped while this holds, so the server never commits a new
        caller turn until the caller has actually stopped hearing the agent.
        This is the entire turn-taking state machine.
        """
        return (
            self._active_response_id is not None
            or bool(self._tool_tasks)
            or (self._playout is not None and self._playout.is_draining())
        )

    async def send_audio_pcm16(self, pcm16_24k: bytes) -> None:
        """Append one chunk of PCM16 24 kHz little-endian audio.

        While the agent has the floor, caller audio is dropped instead of
        uploaded. Twilio frames keep being consumed so the WS stays
        healthy; OpenAI's server VAD just never sees them, so it can't
        commit a turn against speech that happened during the agent's
        response or while one of its tools was in flight.
        """
        assert self._conn is not None
        if self._agent_has_floor():
            self._dropped_input_frames += 1
            if self._dropped_input_frames % 100 == 0:
                log.info(
                    "realtime.input.dropped_while_busy",
                    dropped_frames=self._dropped_input_frames,
                    response_id=self._active_response_id,
                    tools_in_flight=len(self._tool_tasks),
                    pending_marks=(
                        self._playout.pending_marks if self._playout else 0
                    ),
                )
            return
        if self._dropped_input_frames:
            # First frame through the reopened gate: close out the muted
            # window's ledger here, not at response.done — the floor now
            # outlives generation while Twilio playout drains, and this
            # count ≈ 50 fps × the true gate-closed window.
            log.info(
                "realtime.input.gate_reopened",
                dropped_frames=self._dropped_input_frames,
            )
            self._dropped_input_frames = 0
        audio_b64 = base64.b64encode(pcm16_24k).decode("ascii")
        await self._conn.input_audio_buffer.append(audio=audio_b64)  # type: ignore[attr-defined]

    async def receive_loop(self) -> None:
        """Pull server events until the connection closes, dispatching
        audio deltas to the caller-provided ``on_audio_out`` callback."""
        assert self._conn is not None
        out_chunks = 0
        try:
            async for event in self._conn:  # type: ignore[attr-defined]
                evt_type = getattr(event, "type", None)
                if evt_type == "response.output_audio.delta":
                    self._log_first_delta_once()
                    pcm = base64.b64decode(event.delta)
                    pcm = apply_gain(pcm, settings.openai_realtime_output_gain)
                    mulaw = pcm16_to_mulaw(
                        pcm,
                        sr_in=REALTIME_PCM_RATE,
                        sr_out=8000,
                    )
                    await self._on_audio_out(mulaw)
                    out_chunks += 1
                elif evt_type == "input_audio_buffer.speech_started":
                    await self._on_speech_started()
                elif evt_type == "response.function_call_arguments.done":
                    self._spawn_tool_worker(
                        call_id=event.call_id,
                        name=getattr(event, "name", None),
                        raw_args=getattr(event, "arguments", "") or "",
                    )
                elif evt_type == "error":
                    log.error(
                        "realtime.error",
                        error=cast(object, getattr(event, "error", None)),
                    )
                elif evt_type == "response.created":
                    created_id = getattr(getattr(event, "response", None), "id", None)
                    if created_id is not None:
                        self._active_response_id = created_id
                    self._response_started_at = asyncio.get_running_loop().time()
                    self._first_delta_logged = False
                    log.info("realtime.response.created", response_id=created_id)
                    # Wipe anything that uploaded during the RTT before the
                    # gate engaged — otherwise server VAD will commit it and
                    # fire a deferred response right after this one's done.
                    await self._clear_input_buffer_safe()
                elif evt_type == "response.done":
                    response_obj = getattr(event, "response", None)
                    done_id = getattr(response_obj, "id", None)
                    status = getattr(response_obj, "status", None)
                    just_finished = (
                        done_id is not None
                        and done_id == self._active_response_id
                    )
                    if just_finished:
                        self._active_response_id = None
                        self._response_started_at = None
                    log.info(
                        "realtime.response.done",
                        out_chunks=out_chunks,
                        response_id=done_id,
                        status=status,
                        dropped_input_frames=self._dropped_input_frames,
                    )
                    # Arm the playout mark for whatever audio this
                    # response pushed — on EVERY done, not just
                    # just_finished (the tracker no-ops on zero audio),
                    # so a response whose created-id we missed can't
                    # leave its audible tail ungated. The drop counter
                    # resets at gate_reopened in send_audio_pcm16, once
                    # the floor truly releases.
                    if self._playout is not None:
                        await self._playout.response_ended(done_id)
                    if just_finished:
                        # Drop any partial audio that leaked into the
                        # buffer (race between our gate and Twilio's
                        # frame arrival). Cheap insurance — if nothing
                        # is there the server treats this as a no-op.
                        await self._clear_input_buffer_safe()
                elif evt_type == (
                    "conversation.item.input_audio_transcription.completed"
                ):
                    transcript = getattr(event, "transcript", "") or ""
                    item_id = getattr(event, "item_id", None)
                    log.info(
                        "realtime.transcript.caller",
                        text=transcript,
                        item_id=item_id,
                    )
                    if self._transcripts is not None:
                        self._transcripts.append(
                            "caller",
                            transcript,
                            item_id=item_id,
                        )
                    # Belt-and-suspenders: when whisper returns "" (VAD
                    # committed on silence/noise), inject a system note
                    # so the model has an explicit instruction in-context
                    # to ask for a repeat instead of improvising. The
                    # persona rule covers the "noise transcribed as
                    # words" case; this covers the pure-silence case.
                    if not transcript.strip():
                        await self._nudge_repeat_on_empty()
                elif evt_type in (
                    "response.output_audio_transcript.done",
                    # Older snapshot variant — kept so a model snapshot
                    # rollback (or a future server-side renaming) doesn't
                    # silently drop agent turns from the transcript.
                    "response.audio_transcript.done",
                ):
                    transcript = getattr(event, "transcript", "") or ""
                    response_id = getattr(event, "response_id", None)
                    log.info(
                        "realtime.transcript.agent",
                        text=transcript,
                        response_id=response_id,
                    )
                    if self._transcripts is not None:
                        self._transcripts.append(
                            "agent",
                            transcript,
                            response_id=response_id,
                        )
                elif evt_type == "session.created":
                    log.info("realtime.session.created")
                elif evt_type == "session.updated":
                    log.info("realtime.session.updated")
        except Exception:
            log.exception("realtime.receive_loop.crashed")
            raise

    def _spawn_tool_worker(
        self, *, call_id: str, name: str | None, raw_args: str
    ) -> None:
        log.info("realtime.tool.invoked", call_id=call_id, name=name)
        task = asyncio.create_task(
            self._run_tool(call_id=call_id, name=name, raw_args=raw_args)
        )
        self._tool_tasks[call_id] = task
        task.add_done_callback(lambda _t, cid=call_id: self._tool_tasks.pop(cid, None))

    async def _run_tool(
        self, *, call_id: str, name: str | None, raw_args: str
    ) -> None:
        started = asyncio.get_running_loop().time()
        try:
            try:
                args = json.loads(raw_args or "{}")
            except json.JSONDecodeError:
                log.warning(
                    "realtime.tool.bad_args", call_id=call_id, raw_args=raw_args
                )
                args = {}

            self._record_tool_invocation(call_id, name, args)

            if name == "search_web":
                query = str(args.get("query", "")).strip()
                language = str(args.get("language", "")).strip().lower() or None
                result = await search_web(
                    query, language=language, recent_context=self._caller_memory
                )
                await self._post_tool_result(call_id, result)
                self._record_tool_result(
                    call_id, name, "ok", started, result_preview=result
                )
            elif name == "case_brief_lookup":
                question = str(args.get("question", "")).strip()
                language = str(args.get("language", "")).strip().lower() or None
                result = await case_brief_lookup(question, language=language)
                await self._post_tool_result(call_id, result)
                self._record_tool_result(
                    call_id, name, "ok", started, result_preview=result
                )
            elif name == "call_contact":
                first_name = str(args.get("first_name", "")).strip()
                if not self._bridge_available:
                    reply = "Sorry, I can't call anyone right now."
                    await self._post_tool_result(call_id, reply)
                    self._record_tool_result(
                        call_id, name, "unavailable", started, result_preview=reply
                    )
                else:
                    candidates = find_contacts(first_name) if first_name else []
                    if not candidates:
                        reply = _no_match_reply(first_name)
                        await self._post_tool_result(call_id, reply)
                        log.info(
                            "realtime.call_contact.unknown_name",
                            query=first_name,
                        )
                        self._record_tool_result(
                            call_id, name, "unknown", started, result_preview=reply
                        )
                    elif len(candidates) == 1:
                        # Empty function output, no spoken reply — the
                        # handler tears this session down, runs the RING
                        # phase (place the call + play ringback), and
                        # the next session opens with framing via
                        # first_response_instruction.
                        await self._post_tool_result(call_id, "", speak=False)
                        self.bridge_request = candidates[0]
                        self.bridge_request_event.set()
                        log.info(
                            "realtime.call_contact.resolved",
                            query=first_name,
                            resolved=candidates[0].first_name,
                        )
                        self._record_tool_result(
                            call_id, name, "requested", started
                        )
                    else:
                        reply = _ambiguous_reply(candidates)
                        await self._post_tool_result(call_id, reply)
                        log.info(
                            "realtime.call_contact.ambiguous",
                            query=first_name,
                            candidates=[c.first_name for c in candidates[:3]],
                        )
                        self._record_tool_result(
                            call_id,
                            name,
                            "ambiguous",
                            started,
                            result_preview=reply,
                        )
            elif name == "message_contact":
                first_name = str(args.get("first_name", "")).strip()
                message = str(args.get("message", "")).strip()
                if self._send_text_fn is None:
                    reply = "Sorry, I can't send messages right now."
                    await self._post_tool_result(call_id, reply)
                    self._record_tool_result(
                        call_id, name, "unavailable", started, result_preview=reply
                    )
                else:
                    candidates = find_contacts(first_name) if first_name else []
                    if not candidates:
                        reply = _no_match_reply(first_name)
                        await self._post_tool_result(call_id, reply)
                        log.info(
                            "realtime.message_contact.unknown_name",
                            query=first_name,
                        )
                        self._record_tool_result(
                            call_id, name, "unknown", started, result_preview=reply
                        )
                    elif len(candidates) == 1:
                        reply = await self._send_text_fn(candidates[0], message)
                        await self._post_tool_result(call_id, reply)
                        self._record_tool_result(
                            call_id, name, "ok", started, result_preview=reply
                        )
                    else:
                        reply = _ambiguous_reply(candidates)
                        await self._post_tool_result(call_id, reply)
                        log.info(
                            "realtime.message_contact.ambiguous",
                            query=first_name,
                            candidates=[c.first_name for c in candidates[:3]],
                        )
                        self._record_tool_result(
                            call_id,
                            name,
                            "ambiguous",
                            started,
                            result_preview=reply,
                        )
            else:
                log.warning("realtime.tool.unknown", call_id=call_id, name=name)
                await self._post_tool_result(call_id, _TOOL_RECOVERY_REPLY)
                self._record_tool_result(call_id, name, "unknown_tool", started)
        except asyncio.CancelledError:
            self._record_tool_result(call_id, name, "cancelled", started)
            raise
        except Exception:
            log.exception("realtime.tool.worker.crashed", call_id=call_id)
            self._record_tool_result(call_id, name, "error", started)
            try:
                await self._post_tool_result(call_id, _TOOL_RECOVERY_REPLY)
            except Exception:
                log.exception("realtime.tool.recovery.failed", call_id=call_id)

    def _record_tool_invocation(
        self,
        call_id: str,
        name: str | None,
        args: dict[str, object],
    ) -> None:
        """Write a ``role="tool"`` row for the start of a tool worker.

        Renders as ``search_web(query="…", language="es")`` so the
        transcript reads inline with the conversation.
        """
        if self._transcripts is None:
            return
        rendered_args = ", ".join(f"{k}={v!r}" for k, v in args.items())
        text = f"{name or '<unknown>'}({rendered_args})"
        # Trim to ~120 chars so a long search query doesn't blow up
        # a one-line transcript renderer.
        if len(text) > 120:
            text = text[:117] + "..."
        self._transcripts.append(
            "tool",
            text,
            call_id=call_id,
            name=name,
            args=args,
            phase="start",
        )

    def _record_tool_result(
        self,
        call_id: str,
        name: str | None,
        status: str,
        started: float,
        result_preview: str | None = None,
    ) -> None:
        if self._transcripts is None:
            return
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        preview = (result_preview or "").strip().replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        text = f"{name or '<unknown>'} → {status} ({duration_ms} ms)"
        self._transcripts.append(
            "tool",
            text,
            call_id=call_id,
            name=name,
            status=status,
            duration_ms=duration_ms,
            result_preview=preview or None,
            phase="end",
        )

    async def _on_speech_started(self) -> None:
        """Server VAD detected caller speech.

        If the agent has the floor, ignore it — the upstream PCM-drop gate
        should have prevented this from ever firing, but treat any leak
        as defense-in-depth: do NOT cancel its response, its tool
        workers, or Twilio's playback. The agent finishes what it was doing.

        Otherwise (the normal case: caller takes a new turn after the agent
        is fully done), the server will commit and auto-create a
        response. We just log.
        """
        assert self._conn is not None
        if self._agent_has_floor():
            log.info(
                "realtime.speech_started.ignored",
                response_id=self._active_response_id,
                tools_in_flight=len(self._tool_tasks),
            )
            return
        log.info("realtime.turn.started")

    def _log_first_delta_once(self) -> None:
        """Log one ``realtime.response.first_delta`` line per response —
        the time from response.created to first audio on the wire. Cheap
        latency visibility without per-turn bookkeeping."""
        if self._first_delta_logged or self._response_started_at is None:
            return
        self._first_delta_logged = True
        elapsed_ms = int(
            (asyncio.get_running_loop().time() - self._response_started_at) * 1000
        )
        log.info(
            "realtime.response.first_delta",
            response_id=self._active_response_id,
            elapsed_ms=elapsed_ms,
        )

    async def _post_tool_result(
        self, call_id: str, output: str, *, speak: bool = True
    ) -> None:
        assert self._conn is not None
        # Wipe leak from the tool tail before the server creates the answer.
        # Without this, frames that arrived in the gap between the worker
        # finishing and response.created get committed and produce a
        # second, unwanted response after the real answer.
        try:
            await self._conn.input_audio_buffer.clear()  # type: ignore[attr-defined]
        except Exception:
            log.exception("realtime.input_buffer.clear_failed")
        await self._conn.conversation.item.create(  # type: ignore[attr-defined]
            item={
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
        )
        if speak:
            await self._conn.response.create(response={})  # type: ignore[attr-defined]
        log.info(
            "realtime.tool.result.posted",
            call_id=call_id,
            output_len=len(output),
            speak=speak,
        )
