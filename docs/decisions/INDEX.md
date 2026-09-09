# Architecture Decision Records — index

One row per ADR. Open the file for the full record. New ADRs get the
next sequential number; numbering gaps (0012 is the only one) are
kept stable — never renumber. Cross-reference an ADR by
its number (e.g. `ADR 0009`) in code comments, the hub doc, and other
ADRs.

| # | Title | Date | Status | One-liner |
|---|---|---|---|---|
| [0001](0001-railway-dockerfile-python314.md) | Railway build: Dockerfile instead of Nixpacks (Python 3.14) | 2026-05-29 | Accepted | Pin Python 3.14 + `uv`-based layered build; rejected Nixpacks for reproducibility. |
| 0002 | `httpx` promoted from dev to runtime dependency | 2026-05-31 | Removed | A dependency move whose durable record is `pyproject.toml`. The number stays retired. |
| 0003 | Voice fingerprinting via SpeechBrain ECAPA-TDNN | 2026-05-31 | Removed | The feature was removed (never earned its keep; ~90% of image weight). The number stays retired. |
| [0004](0004-railway-data-volume.md) | Attach a Railway persistent volume at `/app/data` | 2026-06-01 | Accepted | Single volume holds sessions, contacts, callers, memory, transcripts, brief. |
| [0005](0005-caller-alerts-smtp.md) | Caller alerts via the Resend transactional API | 2026-06-02 | Accepted | Hobby tier blocks SMTP 25/465/587 — Resend HTTPS is the only path. |
| [0006](0006-operator-cli-railway-sync.md) | Operator CLIs sync to the Railway volume | 2026-06-02 | Accepted | Atomic `cat > tmp.$$ && mv tmp.$$ final` over `railway ssh`; documented race window. |
| [0007](0007-case-brief-lookup-tool.md) | `case_brief_lookup` tool (CourtListener-grounded answers) | 2026-06-02 | Accepted | Grounded brief + Atom feed; no `web_search`; brief lives only on the volume (never in git). |
| [0008](0008-telegram-voice-call-modality.md) | Telegram voice-call modality: 1:1 P2P via `pytgcalls.play(user_id)` | 2026-06-06 | Accepted | MTProto `phone.requestCall` over the user account; not bot/group/channel. |
| [0009](0009-agent-surrenders-floor.md) | The agent fully surrenders the floor during a contact bridge | 2026-06-07 | Accepted | Tear the Realtime session down for BRIDGE; rebuild on exit. Fixes 2× speed-up + persona leak. |
| [0010](0010-twilio-webhook-signature-validation.md) | Twilio webhook signature validation | 2026-06-17 | Accepted | Deferred at MVP, later implemented: stdlib HMAC-SHA1 on both webhooks whenever TWILIO_AUTH_TOKEN is set; blank token = skip + boot warning. |
| [0011](0011-username-only-contact-resolution.md) | Username-only contact resolution | 2026-06-08 | Accepted | `@username` via Telethon `get_entity`; drop `phone` field; fixes privacy-locked contacts. |
| [0013](0013-directline-feature.md) | Directlines: dedicated DIDs that connect a caller straight to one contact | 2026-06-15 | Accepted | Per-DID 1:1 mapping to a contact; the agent out of the path; race-model design + fallback to the agent on no-answer. |
| [0014](0014-asymmetric-bridge-resampler.md) | Asymmetric BRIDGE resampler: tried, didn't move the artifact | 2026-06-18 | Rejected | `soxr.ResampleStream` half-fix shipped + reverted same day; lab SNR improved 30 dB but no perceptual or spectral movement in real calls. Kept on file to block re-attempts. |
| 0015 | Caller-speech transcription model | 2026-07-04 | Folded | Folded into [0024](0024-model-choices-and-revert-levers.md) — one of three records making the same env-overridable-model decision. The number stays retired. |
| 0016 | `search_web` overhaul: freshness, source steering, text model | 2026-07-04 | Folded | Folded into [0024](0024-model-choices-and-revert-levers.md). The number stays retired. |
| [0017](0017-ntgcalls-process-isolation.md) | Process-isolate the ntgcalls voice stack into a supervised sidecar | 2026-07-06 | Accepted | ntgcalls SIGSEGV (contact-answer) killed all of prod; move Telethon+py-tgcalls into a supervised child, drive it over two UDS channels via a duck-typed `TelegramBridgeClient`. Crash → synthesized `sidecar_crashed` → a fixed apology recording, then hang up; durable faulthandler capture; FastAPI no longer imports the native code. |
| [0018](0018-add-contact-provisioning.md) | Unified contact + directline provisioning | 2026-07-08 | Accepted | A deterministic `add_contact.py` executor (`search`/`provision`) replaces the hand-run three-CLI dance; reuses the existing CLIs + `_dev_mode` conventions. |
| 0019 | Realtime model snapshot bump | 2026-07-11 | Folded | Folded into [0024](0024-model-choices-and-revert-levers.md). The number stays retired. |
| [0020](0020-playout-gated-floor-release.md) | Playout-gated floor release (marks primary, byte-clock backstop) | 2026-07-13 | Accepted | The mute gate released at `response.done` = generation-complete, but Twilio playout lags 16–27 s on long answers (measured: 65–72% of the agent's audible speech was unmuted → talk-past + queued replies). WS-scoped `PlayoutTracker`: Twilio mark echo releases the floor (fail-closed), byte-clock + `PLAYOUT_MARK_GRACE_S` bounds a lost echo, `echo_delta_ms` audits both. Shallow strip-away path for future barge-in / full-duplex. |
| [0021](0021-twilio-cli-node20.md) | twilio-cli pinned to node@20 (ERR_REQUIRE_ESM under Node < 20.19) | 2026-07-14 | Accepted | twilio-cli 6.2.4 eagerly require()s ESM `@octokit/core` at load → crashes on Node < 20.19; Homebrew pins node@20 but it wasn't installed, so the CLI fell through to fnm's Node 18. Require Node ≥ 20.19; `scripts/twilio.py` prepends Homebrew's keg-only node@20 to the child's PATH when present (warns otherwise). Rejected: patching CLI files (wiped on upgrade), REST rewrite (bigger, tracked in the roadmap). |
| [0022](0022-call-processed-report.md) | Call-processed report: notify on call-END, one universal trigger | 2026-07-20 | Accepted | Replace the two start-time alerts with one informational email per call, sent on the terminal `/twilio/voice/status` callback (the only signal that fires for blocked / gate-hangup / connected / crash-recovered calls). Durable `data/calls/<sid>.jsonl` journal + `summarize_lifecycle` feed a crafter that adds a one-shot LLM transcript summary and sends HTML+text via Resend; atomic marker = exactly-once. Retires the `notify` flag whole. |
| 0023 | Daily monitor | 2026-08-19 | Moved | Subsystem split out of this codebase into its own application; the record moved with it. The number stays retired. |
| [0024](0024-model-choices-and-revert-levers.md) | Model choices and revert levers | 2026-07-11 | Accepted | Every model id is a plain env-overridable `str`, never a `Literal` — a swap is a platform variable + restart, not a redeploy. Folds 0015/0016/0019. |

## Numbering notes

Numbers are never reused or renumbered — cross-references in code and
docs must stay stable. Rows without a file are tombstones (see their
Status: *Moved*, *Folded*, or *Removed*); 0012 is a plain numbering gap.
New ADRs use the next sequential number (0025 next).
