# 0008 — Telegram voice-call modality: 1:1 P2P via `pytgcalls.play(user_id)`

**Date:** 2026-06-06
**Status:** Accepted

## Context

An earlier design left the modality of the Telegram leg as an
open choice between two paths:

- **P2P:** Telethon resolves a contact's phone to a user_id; ntgcalls'
  `CreateP2PCall` is invoked via py-tgcalls — Telegram sends
  `phone.requestCall` MTProto → contact's app rings like any normal
  incoming call. The plan hedged that the high-level Python wrapper "does
  not yet expose outbound private calls cleanly" and only the raw
  ntgcalls binding worked.
- **Plan B (group VC):** create a one-time small group with `me` +
  `contact`, start a group voice chat, send the contact a text
  ("📞 Calling you now — open Telegram"). Reliable, but **does not
  ring** — leaves a residual group in the contact's chat list and
  relies on a notification the contact may ignore.

Source-reading py-tgcalls 2.3.0 (current at the time of this ADR)
upstream shows that hedge no longer holds:

- pytgcalls' `play()` entry point branches on the sign of `chat_id`.
  Positive → user_id → P2P
  call (`CallConfig`); negative → group call (`GroupCallConfig`).
  No separate P2P API — the same `play()` covers both.
- `pytgcalls/types/calls/call_config.py`: real, single-field
  `CallConfig(timeout: int = 60)`. Used to set the ring timeout.
- `example/p2p_example/example_p2p.py`: shipped first-class example.
- `phone.requestCall` per
  [core.telegram.org](https://core.telegram.org/method/phone.requestCall)
  fans `updatePhoneCall` with `phoneCallRequested` to all of the
  recipient's active devices — real incoming-call UI, P2P over UDP
  with reflector fallback, Opus over WebRTC.

Only Option A delivers the "ring like a normal call" behavior this
project needs.

Two related drift points surfaced during the same investigation:

1. **Version drift.** An earlier pin was `py-tgcalls 2.2.12 +
   ntgcalls 2.1.0`. The current install is
   `py-tgcalls 2.3.0` (`>=2.2.0,<3.0.0` requirement on `ntgcalls`
   shipped 2.2.2). The newer line carries the actively-maintained
   `play(user_id, …)` surface used here.
2. **Credentials path.** The obvious move is to
   register the app at https://my.telegram.org/apps. That form's
   eligibility check rejected the agent's account (still too new,
   even after the recommended warming and retry recipe). Resolution
   was to register the app from an **older, established account**
   instead; the resulting `api_id`/`api_hash` identify the *app* and
   place no constraint on which user account authenticates against
   it. Telethon then logs in as the agent account against this app —
   the standard pattern every public Telegram client (Telethon's own
   examples, GramJS, etc.) follows.

## Decision

Ship the Telegram leg as Option A — 1:1 P2P. Concretely:

- `src/outside_line/telegram_bridge.py` issues
  `pytgcalls.play(user_id, MediaStream(ExternalMedia.AUDIO,
  AudioParameters(48000, 1)), CallConfig(timeout=60))`. Call setup
  pushes no audio frames — success is the contact's phone ringing.
- Mono 48 kHz matches what the OpenAI Realtime model emits after the
  existing resample chain in `audio_bridge.py`, so the audio bridge
  attaches `send_frame` / `stream_frame` handlers without re-negotiating
  the format.
- `api_id` / `api_hash` can be registered from any established account
  you control. Documented here so the next operator doesn't waste
  cycles re-attempting from a brand-new agent account.
- Plan B (one-time-group + text) is **demoted from documented
  fallback to emergency degrade only.** If a specific contact's P2P
  routing flakes in production we revisit; we do not preemptively
  build the group-VC code path.

## Consequences

- `telegram_bridge.place_call` is the single entry point the audio
  bridge builds on. The handle returned (the contact's `user_id`) is
  the same key `send_frame` / `leave_call` / the `stream_frame`
  filter take, so the audio bridge is purely additive on top of call
  setup.
- Future P2P-specific issues to watch in pytgcalls upstream:
  [pytgcalls#324](https://github.com/pytgcalls/pytgcalls/issues/324)
  (deadlock on video-stream switch — audio-only path unaffected) and
  [#323](https://github.com/pytgcalls/pytgcalls/issues/323) (closed —
  RecordStream silent-decode fix, already in 2.3.0).
- If Telegram ever tightens the "developer ≠ logged-in user" pattern
  (no current signal that they will), we'd need to re-attempt app
  registration from the agent account (after the usual account-warming
  period).
