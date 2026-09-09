# 0010 — Twilio webhook signature validation

**Date:** 2026-06-17 (deferral) · 2026-09-09 (implemented)
**Status:** Accepted

## Decision (current)

Both inbound webhooks validate `X-Twilio-Signature` whenever
`TWILIO_AUTH_TOKEN` is set — 403 on a bad or missing signature — via a
stdlib HMAC-SHA1 implementation in `twilio_handler.py`
(`_twilio_signature_valid` / `_verify_twilio_request`); no `twilio` SDK
(the app is httpx-only). The signed URL is rebuilt from
`PUBLIC_BASE_URL` + path, which sidesteps the classic proxy trap where a
TLS terminator makes `request.url` surface as `http://…` while Twilio
signed `https://…`. Blank token = validation skipped with a boot-time
warning; the production preflight (`require_boot_credentials`) makes the
token mandatory outside development. Disable path: unset
`TWILIO_AUTH_TOKEN`.

A focused unit test (`tests/test_twilio_signature.py`) locks the HMAC
helper with a hand-computed vector plus tampered-body/URL rejections.

## Context

Twilio signs every webhook with `X-Twilio-Signature`; verifying it
requires the account **Auth Token**. This project uses **API-Key auth**
for everything else (outbound calls, recording fetches,
`IncomingPhoneNumbers` updates) because API Keys can be revoked
individually without rotating the master credential. Loading the Auth
Token into the runtime was initially deferred to keep its blast radius
out of the env store; validation was deferred with it.

The endpoints in scope:

- `POST /twilio/voice` (`voice()` in `src/outside_line/twilio_handler.py`)
- `POST /twilio/voice/status` (`voice_status()` in the same module)

The Media Streams WebSocket `/twilio/media` is separate — Twilio does not
sign WS handshakes. Its security is the unguessability of the
freshly-minted `streamSid` from the prior `voice()` TwiML, plus the
allow-list having already gated the parent call.

## Why the deferral was ever tenable (kept for the threat model)

A defense-in-depth chain exists independently of signature validation:

1. **Per-caller allow-list.** Unknown / not-allowed numbers get
   `<Reject reason="busy"/>` before any work happens. A spoofed webhook
   would need a `From` already on the allow-list — and still has no PSTN
   leg, so no WebSocket ever opens.
2. **No public surface beyond `/twilio/voice*`.** A forged webhook can
   trigger only a TwiML response nobody receives, a `record_call` write,
   and possibly one email — bounded, idempotent, low-cost.
3. **`/twilio/voice/status` is idempotent.** A forged `completed` payload
   pops a nonexistent session and returns 200 — no state change.

That chain is why validation is a hardening layer here, not load-bearing —
and why "unset the token" remains an acceptable escape hatch for local
tinkering.

## Rejected alternatives

- **Validate with a derived secret** — Twilio's signature is keyed on the
  Auth Token specifically; there is no API-Key-derived variant.
- **Custom bearer token in the webhook URL** — Twilio webhook config can't
  add headers; a query-param secret is brittle (rotation = re-render every
  DID) and weaker than HMAC.
