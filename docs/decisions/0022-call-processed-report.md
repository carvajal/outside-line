# 0022 — Call-processed report: notify on call-END, one universal trigger

**Date:** 2026-07-20
**Status:** Accepted

## Context

The operator got emails **at call START**, from two fire-and-forget senders in
`alerts.py` wired into `voice()`:

- `send_blocked_call_alert` — every blocked (not-allowed) call → `<Reject busy>`.
- `send_notify_alert` — every call from a number opted-in with `notify=True`.

That shape had three problems: it fired before we knew what happened (no
duration, no lifecycle, no summary), it only covered opt-in numbers, and its
subject line was weak. The operator wanted **one informational "call-processed"
email per inbound call, sent when the call ENDS**, covering every outcome
(blocked, directline, agent-only, agent+contact-bridge), with an at-a-glance
subject — and it must **never lose a call**, including unexpected disconnects
and process crashes.

## Decision

Send one call-processed report per call, on call-end, from a single universal
trigger, sourced from a durable on-disk journal.

- **Single trigger = `POST /twilio/voice/status`** (`voice_status()`). It is the
  only signal that fires for every end-state: blocked (`busy`), caller-hangs-up-
  during-the-gate (no WS ever opens), normal connected (`completed`), AND a
  FastAPI process crash mid-call (Twilio re-delivers the terminal callback to the
  restarted process). It carries `CallDuration`/`CallStatus`/`From`/`To`. It is
  already wired on every managed DID incl. the mainline (dev_mode flips it in
  lockstep with `/twilio/voice`). A WS-teardown trigger would only duplicate the
  one case already covered, so it is intentionally omitted.
- **Durable per-call lifecycle journal** — `data/calls/<call_sid>.jsonl`
  ([`call_journal.py`](../../src/outside_line/call_journal.py)), modeled on
  `TranscriptWriter`: append-only, open-append-close per row (crash-safe prefix),
  fail-open. Written from `voice()` (`received`/`blocked`) and the `media()` phase
  seams (`agent_started`, `contact_call`, `contact_no_answer`, `bridge_started`,
  `bridge_ended`, `ended`). `summarize_lifecycle()` reconstructs ordered segments
  with durations **derived from segment-start timestamps + `CallDuration`** — so
  the exact end events are non-load-bearing and the crafter can race in-process
  teardown. This is what carries the no-transcript cases (blocked, gate-hangup,
  pure directline bridge) that never produce a conversational turn.
- **Crafter** — [`call_report.py`](../../src/outside_line/call_report.py)
  `craft_and_send_call_report()`: reads journal + transcript, builds the
  deterministic lifecycle skeleton, adds a one-shot LLM topic/summary over the
  transcript (reuses the `memory_updater` pattern, `OPENAI_SEARCH_MODEL` per
  ADR 0024; fail-open — the skeleton still sends if the LLM errors), and emits a
  clean **HTML + plain-text** email (English) via Resend. Subject is at-a-glance:
  `Agent · <who> · agent 3m + Sam 12m` / `… · blocked (main line)`.
- **Exactly-once** via an atomic `open(marker, "x")` on the volume
  (`data/calls/<call_sid>.emailed`) so Twilio re-deliveries / racing callbacks
  send exactly one email.
- **Retire `notify` entirely** — the report emails on every call, so the
  per-number opt-in is obsolete: the flag, `set_notify`, `is_notify_entry`, the
  `notify` CLI subcommand/column/REPL verb, and both old senders + payload
  builders are removed. `alerts.py` slims to the Resend transport.

No new env vars: the report reuses `CALLER_ALERTS_*` and `OPENAI_SEARCH_MODEL`;
the journal dir is a module constant like the transcript dir.

## Trade-offs accepted

- **Relies on Twilio delivering the terminal statusCallback.** If a delivery is
  lost we lose that one info-email — acceptable (best-effort enrichment, same
  risk class the directline teardown already accepts with its orphan-cleanup
  backstop). A WS-teardown backstop calling the same idempotent crafter is a
  cheap add if delivery ever proves flaky.
- **The caller's final pre-hangup utterance may lack STT.** Caller transcription
  is an async side channel (ADR 0024) whose late events are lost when the OpenAI
  WS closes at teardown — a wait can't recover them. The whole-conversation LLM
  summary absorbs the gap. A short ~2 s settle only flushes already-arrived
  writes.
- **Voice Insights (who-hung-up, jitter) stays log-only** — it lands 30–120 s
  post-call with 404s, which would delay every email. Duration comes free from
  the journal + `CallDuration`; the recording is referenced by an
  `./scripts/archive.py` pointer, never fetched inline.

## Verification

- `summarize_lifecycle` unit test covers the AGENT→RING→BRIDGE→AGENT loop, the
  directline path, blocked, and abnormal-end (no `ended`) cases.
- Blocked-call `POST /twilio/voice` writes `received`+`blocked` to the journal.
- Crafter builds correct subjects/bodies for agent-bridge / directline / blocked;
  a second `craft_and_send_call_report` for the same call no-ops on the marker.
- Real prod test call → email at `CALLER_ALERTS_EMAIL_TO` with the right subject,
  lifecycle bullets, duration, and (if spoken) topic summary; a re-delivered
  status callback sends no duplicate.
