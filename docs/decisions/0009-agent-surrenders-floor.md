# 0009 — The agent fully surrenders the floor during a contact bridge

**Date:** 2026-06-07
**Status:** Accepted

## Context

The first bridge implementation modeled the BRIDGED state as "the
agent is muted but the Realtime session is still open and watching."
That made room for a `hangup_contact` realtime tool — the caller could
ask the agent to hang up mid-bridge and the agent would tear the
Telegram leg down. The audio routing was split awkwardly across two
modules:

- **`RealtimeSession`** owned `_bridged_user_id`; in `send_audio_pcm16`
  it routed caller PCM to Telegram instead of OpenAI when the flag was
  set; in `receive_loop` it dropped the agent's TTS deltas when the flag was
  set; in `__aexit__` it cleaned up a dangling Telegram bridge if the
  WS closed mid-bridge.
- **`twilio_handler.media()`** owned the Telegram→Twilio incoming-audio
  callback (per-call closure registered via `bridge.register_callbacks`),
  and the `on_bridge_ended` plumbing that called back into the session
  to fire a one-shot reconnect response.

This shape was rejected on review. The concrete reasons:

1. **Privacy/attention model mismatch.** A hangup tool implies the agent
   keeps listening to the entire conversation. The bridge is between
   the caller and their contact — the agent should step out, not
   eavesdrop.
2. **Token cost.** Realtime-model minutes burn the entire bridge
   window for nothing useful: caller PCM is silently dropped server-side
   (we don't push it to OpenAI's input_audio_buffer while BRIDGED);
   the agent's TTS deltas are silently dropped client-side (we filter them
   in `receive_loop`).
3. **Split state surface.** The two-module routing made the missing
   `record()` bug harder to diagnose — frames could in principle be
   missing in either of two layers, and triaging required reading both
   files at once.

## Decision

**When `call_contact` succeeds, the Realtime session is torn down
entirely until the bridge ends.** Audio routes directly between the
Twilio WS and the Telegram leg through `twilio_handler.media()`'s new
phase loop; no OpenAI in the loop during BRIDGE. When the contact (or
pytgcalls timeout) ends the bridge, the handler builds a **fresh
`RealtimeSession`** for the reconnect, opened with a one-shot
`first_response_instruction` ("Hi, I'm back").

`hangup_contact` is removed entirely — no tool, no persona clause, no
in-session hang-up wake-phrase support. The bridge ends when the
contact hangs up, the contact doesn't answer, or pytgcalls times out.
Three exit conditions, no fourth.

## Consequences

- **`RealtimeSession` no longer knows about Telegram.** No
  `_bridged_user_id`, no `_telegram_bridge` field, no `on_bridge_ended`
  method, no `__aexit__` cleanup. The session class is pure "agent
  conversation owner."
- **Audio routing during BRIDGE lives in one place** —
  `twilio_handler._run_bridge_phase`. Easier to reason about and to
  diagnose.
- **No token spend during the bridge window.** The Realtime WS is
  closed; OpenAI is idle.
- **Uniform state machine across all `call_contact` outcomes.**
  Answered, no-answer, and lookup-miss all flow through teardown + ring
  + reconnect. The only branch point is the
  `first_response_instruction` on the rebuilt session.

- **Short audio gap on phase transitions.** Tearing down the Realtime
  WS, starting the ringback driver, then building a new session at the
  end of the bridge each cost ~100–500 ms. Acceptable; if measurable
  in practice we'd pre-warm the next session in parallel with the
  bridge phase. Defer until evidence.
- **No mid-bridge agent intervention.** If a caller asks the agent for
  help while bridged, the agent can't respond — they have to wait for
  the contact to hang up. An acceptable trade-off given the privacy
  gains.
- **Reconnect sessions reload caller memory from disk.** Small redundant
  I/O each bridge cycle. Trivially small.

- Conversation history from before the bridge does **not** carry across
  to the reconnect session — the agent "stepped out and came back."
  Acceptable; revisit if callers complain.

## Implementation pointers

- Phase-loop helpers: `_run_agent_phase` / `_run_ring_phase` /
  `_run_bridge_phase` in `src/outside_line/twilio_handler.py`.
- Ring-wait audio: `NOKIA_HUM_ULAW_LOOP` (default) or the synthetic
  ringback `RINGBACK_ULAW_LOOP` in `src/outside_line/audio_bridge.py`,
  selected by `RING_AUDIO_KIND`.
- Signal from `RealtimeSession` to handler when the model invokes
  `call_contact`: `bridge_request: str | None` +
  `bridge_request_event: asyncio.Event`. The handler races the
  `receive_loop` task against `bridge_request_event.wait()`.
- Signal from `TelegramBridge` to handler when the contact hangs up:
  `bridge_ended_event: asyncio.Event` set in `_on_chat_update` on
  `LEFT_CALL`. Cleared at the start of `place_call` so consecutive
  bridge cycles in the same WS see a fresh edge.

## Alternatives considered

- **Keep the in-session BRIDGED model but mute the WS more cheaply.**
  No clean way to silence OpenAI without closing the connection; the
  Realtime API doesn't have a "pause this session" primitive.
- **Move only the audio routing out of the session, keep the session
  alive.** Half-step toward the chosen design; preserves the token cost
  without simplifying the state surface much.
- **Build the next session eagerly during BRIDGE** to mask the
  reconnect gap. Adds complexity for a gap that hasn't been measured to
  be problematic. Deferred until evidence.

## Do not reintroduce

Future operators may be tempted to add `hangup_contact` back when, for
example, a caller asks the agent to hang up mid-bridge. **Do not.**
The bridge ends when the contact hangs up. If a future requirement
genuinely needs mid-bridge agent intervention, that's a re-architecture
discussion — multi-party audio routing, attention model, privacy story —
not a one-line tool registration.

## Addendum (2026-06-07) — ntgcalls is 10 ms-granular

After the rearchitecture landed and the first real bridged call went through,
the audio in the caller→contact direction sounded "rushed and not
legible" — a 2 s utterance played as ~1 s on the contact's phone. The
reverse direction (contact→caller) sounded clean. Symmetric resampling
math, asymmetric symptom.

The log line `telegram.bridge.incoming_first_frame bytes=960` was the
tell: 960 B / 2 (int16) / 48 000 = **10 ms**. ntgcalls / WebRTC's audio
engine ticks in 10 ms frames. We were calling
`pytgcalls.send_frame(... 1920 B)` (one 20 ms chunk per Twilio media
frame). ntgcalls interpreted each call as a single 10 ms frame —
discarding the second half and producing the clean 2× speedup.

Fix in `_run_bridge_phase.pump_to_telegram`: split each resampled
1920 B output into two 960 B halves and submit each via its own
`send_to_contact` call, back-to-back. ntgcalls buffers them internally
and emits at correct 10 ms cadence.

Lesson worth remembering for anything else that touches `send_frame`:
the C++ side is 10 ms-granular. Always submit one 10 ms frame per
call, regardless of upstream chunk size.
