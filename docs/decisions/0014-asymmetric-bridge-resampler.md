# 0014 — Asymmetric BRIDGE resampler: tried, didn't move the artifact

**Date:** 2026-06-18
**Status:** Rejected (kept on file as a guard against re-attempt)

## Context

Calls bridged Twilio ↔ Telegram carried a perceptual "chirpy / metallic"
quality on the contact's voice. A plausible mechanism: the contact-ingress
side calls **stateless** `soxr.resample` on every ~10 ms ntgcalls callback
(48 kHz → 8 kHz). Stateless soxr restarts its polyphase filter per call —
the chunk-boundary discontinuity is mathematically a 100 Hz harmonic comb
(= 1 / 10 ms) and measures ~30 dB worse SNR than the stateful streaming
equivalent on synthetic signals. The same pattern is known and fixed in
[Pipecat issue #2070](https://github.com/pipecat-ai/pipecat/issues/2070)
by switching to `soxr.ResampleStream`.

A first attempt applied `ResampleStream` in **both** directions and was
reverted the same day: startup transients produce variable-length output
for the first several chunks, breaking `pump_to_telegram`'s
`len(pcm48) == 1920 B` invariant that ntgcalls's 10 ms-granular
`send_external_frame` depends on — the contact heard "helicopter noise."
A second attempt applied it to the **inbound direction only** (where
Twilio reframes whatever we send), using `quality="QQ"` to avoid HQ's
~150 ms lookahead and get a steady 80-samples-out-per-480-in cadence.

## What disproved it

The lab evidence held (QQ ≈ 75 dB SNR on a streaming sine sweep vs ≈ 45 dB
stateless). **The real-call evidence didn't.** Three independent signals:

1. The first attempt's revert already noted *"the inbound chirp/static
   was unchanged"* — the same direction had received the same fix with
   no effect. That was underweighted on the retry.
2. Subjective A/B on the post-fix call: quality "feels pretty much the
   same." No perceptual improvement.
3. Spectral A/B between the broken-baseline and post-fix recordings:

   | metric | broken | post-fix | delta |
   |---|---|---|---|
   | `comb_excess_dB` (R, silent windows) | −0.28 dB | −0.30 dB | −0.02 dB |
   | `comb_excess_dB` (R, full call) | −0.11 dB | −0.17 dB | −0.07 dB |

   A real 100 Hz comb would be **strongly positive** (+3 to +10 dB); both
   recordings are near zero. **The artifact the change "fixed" wasn't
   measurably present in the broken baseline to begin with.**

The change cost ~70 LOC of new abstraction (a resampler class with its own
lifecycle, an empty-output guard, a `quality="QQ"` knob requiring
soxr-internals knowledge) plus three tests, for a lab artifact that
doesn't manifest in real calls.

## What's actually likely

The "chirpy / metallic" perception (with "accelerated" and "same phrase
echoed back" descriptors) probably comes from mechanisms outside our
pipeline, each separately testable:

- **Contact-side speakerphone acoustic loopback** + weak/disabled AEC on
  the contact's Telegram client (test: have the contact use earbuds).
- **WebRTC NetEQ time-compression** on the contact's side — WSOLA
  time-compression is pitch-preserved acceleration, exactly matching the
  descriptor (proxy: instrument `tg_callback_interval` /
  `tg_frames_per_callback`).
- **μ-law dynamic-range compression on hot speech** — the contact channel
  near-clips in both recordings; an output-gain reduction is plausibly a
  higher-yield knob than the resampler and was never A/B'd alone.

## Decision

**Revert the production code change; keep the guard rails.** The stateless
resampler stays in both directions. Kept from the attempt:

- `test_outbound_caller_to_contact_frame_is_1920_bytes` in
  `tests/test_audio_bridge.py` — the granule invariant is load-bearing in
  the stateless path too, and the test is cheap insurance against a future
  "unify the resamplers" PR that would re-break the contact side.
- This ADR, as a rejected-approach record: a PR described as "apply
  soxr.ResampleStream to fix chirpy contact audio" should route here and
  stop. The number stays; do not reassign.

## References

- [Pipecat issue #2070](https://github.com/pipecat-ai/pipecat/issues/2070) —
  the upstream pattern. It works for them; it isn't the dominant artifact
  in this 48→8 kHz contact-ingress chain.
- ADR 0009 — the floor-surrender rearchitecture that created the BRIDGE
  phase shape. ADR 0013 — directlines, same BRIDGE phase.
