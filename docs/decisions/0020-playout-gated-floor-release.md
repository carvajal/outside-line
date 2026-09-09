# 0020 — Playout-gated floor release (marks primary, byte-clock backstop)

**Date:** 2026-07-13
**Status:** Accepted

## Context

The half-duplex mute gate (`RealtimeSession._agent_has_floor`, no barge-in by
design — noisy phone lines) released caller-audio muting at OpenAI's
`response.done`. That event means *generation* finished, not *playback*: the
model emits audio 2–4× faster than real time, the bridge forwards it to Twilio
immediately, and Twilio's playout buffer drains at 1×. Measured on two real
calls with dual-channel recordings + log drop-counter arithmetic:
**65–72% of the agent's audible speech played with the gate open**, the audible tail
outliving `response.done` by 16–27 s on long answers. Caller speech in that
window committed turns whose replies queued *behind* the draining audio — the
"caller and agent talk past each other" symptom, reproduced with an interjection
test ("say one two three testing" answered only after the story finished).

## Decision

Track playback with a WS-scoped
[`PlayoutTracker`](../../src/outside_line/playout.py) and give the floor predicate
a third term: response active **or** tool in flight **or** `is_draining()`.

- **Primary release — Twilio `mark` echo (event-based, fail-closed).** After
  each response's audio, a `mark` frame named with a unique serial goes down
  the media WS; Twilio echoes it when the audio queued before it has actually
  *played*. A delayed echo costs only extra muteness — never a leak. Echo
  handling is idempotent (unknown/duplicate/expired names are no-ops), so a
  stuck echo arriving seconds later is simply ignored.
- **Backstop — byte-count playout clock (bounded fail-open).** Outbound μ-law
  is exactly **8 000 bytes per second of playback** (codec property; no
  model-speed term anywhere, so generation-speed changes cannot drift it). The
  clock predicts the drain deadline; if an echo hasn't arrived by
  `deadline + PLAYOUT_MARK_GRACE_S` (default 1.5 s) it is provably lost and the
  floor opens anyway. Failure directions are deliberate: the event fails
  closed, the clock bounds the damage when the event fails entirely.
- **Telemetry.** Every echo logs `echo_delta_ms` (echo time − clock
  prediction), so the two mechanisms audit each other on every real call;
  `playout.mark.lost` marks backstop releases. The dropped-frames counter now
  resets at `gate_reopened` (first uploaded frame), keeping the
  50 fps × window arithmetic usable as evidence.

## Constants that must move together

- `ULAW_BYTES_PER_S = 8000` in `playout.py` — invalid the day the Twilio
  `<Stream>` leg stops being 8 kHz μ-law.
- `PLAYOUT_MARK_GRACE_S` (config, env-overridable) — deafness-per-response
  ceiling if marks ever break wholesale.

## Strip-away path (kept deliberately shallow)

For future barge-in or a full-duplex realtime model (gpt-live-style): delete
`playout.py`, the one `is_draining()` term in `_agent_has_floor`, and the mark
plumbing in `twilio_handler` (send closure, `ws_reader` branch, phase reset).
Nothing else knows the tracker exists.

## Trade-off accepted

Talk-over callers are now fully ignored for the *entire audible* answer — that
is the "100% mute while the agent speaks" product intent, and it means head-clipping
of eager replies increases: someone answering over the agent's last sentence must
repeat themselves once he finishes. Chosen consciously over barge-in
(rejected: line noise cutting the agent off) and over a fixed post-`response.done`
guard window (rejected by measurement: the seam scales with answer length).

## Verification

Unit: `tests/test_playout.py` (deadline math, zero-audio no-arm, echo release,
grace expiry + late-echo idempotency, multi-response holds, reset). Prod: real
call re-running the investigation's interjection protocol — the interjection
must never commit, `echo_delta_ms` sane, recording channel-overlap analysis
shows ~0 leaked seconds.
