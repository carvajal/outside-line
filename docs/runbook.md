# Runbook

How to operate, pause, and debug a deployed instance. The generic
requirements are a way to tail the app's logs and a way to reach the
data volume; the `railway …` commands below are the reference
deployment's version of each — substitute your host's equivalents.

## Pause the agent

Two levers, in order of bluntness:

1. **Point the Twilio webhooks at nothing.** In the Twilio console (or
   via `scripts/twilio.py phone-numbers:update` — needs twilio-cli +
   Node ≥ 20.19, ADR 0021), set the voice URL of
   the mainline (and any directline DIDs) to an empty-TwiML bin or a
   dead URL. Calls then fail fast at Twilio without touching the app.
   Remember every DID carries **two** webhooks (voice + status
   callback) — `scripts/dev_mode.py` flips both in lockstep.
2. **Stop the service** — scale to zero / stop it in your host's
   dashboard (`railway down` removes the latest deployment on the
   reference stack). Callers hear Twilio's error tone until it's back.

To block a single caller instead: `./scripts/callers.py allow <phone> off`.

## Rotate credentials

All secrets live in Railway variables (prod) and `.env` (local). After
any rotation, redeploy (`railway up`) or restart the service.

| Credential | Where to rotate | Notes |
|---|---|---|
| `OPENAI_API_KEY` | platform.openai.com → API keys | Used by the Realtime session and every tool worker. |
| `TWILIO_API_KEY_SID` / `TWILIO_API_KEY_SECRET` | Twilio console → API keys | Revoke the old key after the new one is live; API keys rotate independently of the Auth Token. |
| `TWILIO_AUTH_TOKEN` | Twilio console → account settings | Rotating it breaks webhook signature validation until the new value is deployed — rotate + deploy in one motion. |
| Telegram session | delete `data/sessions/*.session` on the volume, re-run `scripts/warm_account.py` locally, copy the fresh session up | The session file *is* the credential; treat it like a password. `TELEGRAM_API_ID`/`API_HASH` identify the app and rarely need rotating. |
| `CALLER_ALERTS_RESEND_API_KEY` | resend.com → API keys | Fail-open: a blank key just disables the per-call report email. |

## Read the logs

Tail your host's logs (`railway logs` on the reference deployment) —
structlog output, one JSON object per line.
Event names worth grepping:

- `twilio.voice.` / `caller.` — inbound webhook decisions (allowed,
  blocked, gate vs skip-gate)
- `twilio.signature.` — webhook signature validation (rejected /
  validation_disabled)
- `realtime.` — Realtime session lifecycle (session.opened, greeting,
  tool dispatch)
- `phase.bridge.` — contact-bridge start/stall/end
- `directline.` — directline routing, ring, teardown
- `telegram.` / `sidecar` — the voice sidecar (crashes land here;
  ADR 0017)
- `case_brief_lookup.` / `search_web.` — tool workers, incl. full tool
  output at INFO
- `memory.` — post-call memory merge

## Debug a bad call

1. **Find the call SID** — from the per-call report email, or
   `railway logs | grep call_sid`, or `./scripts/archive.py list`.
2. **Lifecycle journal:** `data/calls/<sid>.jsonl` on the volume — one
   line per lifecycle event with elapsed-seconds offsets; the fastest
   way to see which phase a call died in.
3. **Transcript:** `./scripts/transcripts.py show <sid>` (mirrors from
   the volume via `railway ssh`), or `./scripts/archive.py show <sid>`.
4. **Recording** (only if `RECORDINGS_ENABLED=true`):
   `./scripts/recordings.py fetch <call_sid>` fetches from Twilio;
   `./scripts/archive.py play <sid>`.
5. **Sidecar crashes:** look for `sidecar_crashed` + the faulthandler
   dump in the logs; ADR 0017 describes the supervisor and the
   `/debug/crash-sidecar` reproduction hook (`SIDECAR_DEBUG_CRASH=1`).

## Deploy

`railway up --ci --message "<short>"` from the repo root — deploys are
manual (no GitHub trigger). Verify with
`curl https://<your-app-domain>/healthz` → `{"status":"ok"}`. If a
deploy seems stuck, check `railway deployment list` first — if the
newest entry predates your change, no deploy was triggered.

Production boots refuse to start with blank hard-required credentials
(`require_boot_credentials` in `config.py`, enforced when
`ENVIRONMENT != development`) — a loud early failure instead of a
silent half-working agent.

## Operator state (the volume)

Everything mutable lives under `/app/data` (Railway volume):
`callers.json` (allow/skip-gate), `contacts.json`, `directline_numbers.json`,
`memory/<phone>.md`, `transcripts/`, `calls/`, `sessions/`,
`case_brief/brief.md`. The operator CLIs (`scripts/callers.py`,
`contacts.py`, `directlines.py`) sync local edits to the volume
(ADR 0006); the app process caches contacts and the case brief per
process, so a redeploy/restart picks up edits.
