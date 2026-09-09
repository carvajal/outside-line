# 0005 — Caller alerts via the Resend transactional API

**Date:** 2026-06-02
**Status:** Accepted (transport decision stands; the first-time alert it
was written for was replaced by the per-call report — ADR 0022 — which
uses the same Resend path)

## Context

The caller registry feature emailed the operator the first time a new
phone number called the agent (since folded into the per-call report,
ADR 0022 — the transport below carried over unchanged). The email is
operator-facing only — single recipient, low volume, never
user-visible. Two questions: what
transport, and how to call it from the async `voice()` handler
without blocking the TwiML response.

The first cut shipped Gmail SMTP via `aiosmtplib`. That timed out
on the first real send from Railway. Railway documents that
**outbound SMTP (ports 25 / 465 / 587) is blocked on Hobby and
below — Pro plan is required to keep SMTP open** (see
<https://docs.railway.com/networking/outbound-networking>). HTTPS-only
APIs are the supported path on Hobby. Switched to **Resend**.

## Decision

Use **[Resend](https://resend.com)** as the transactional email
transport. POST to `https://api.resend.com/emails` with a Bearer API
key, fired from the handler via `asyncio.create_task` so the call
path is non-blocking.

`aiosmtplib` is removed from `[project] dependencies` in
`pyproject.toml`, where `httpx` is already a runtime dep.

## Rejected alternatives

- **Upgrade Railway to Pro plan ($20/month).** Keeps the original
  Gmail SMTP code. Overkill if email is the only reason — every other
  Railway feature is fine on Hobby. Revisit if we need other Pro-only
  capabilities later.
- **SendGrid / Mailgun / Postmark.** Equivalent functionality;
  Resend's free tier (100/day) and developer experience (one
  endpoint, no SMTP-style setup, no domain required for low volume)
  edge them out for one-recipient operator alerts.
- **Stdlib + provider-agnostic HTTP wrapper.** Premature — one
  transport, one call site, no benefit from indirection.

## Configuration

Four settings on `Settings` (env-overridable; secrets live in `.env`
locally and Railway variables in prod — never in code):

| Setting                          | Default                          | Purpose                                                      |
| -------------------------------- | -------------------------------- | ------------------------------------------------------------ |
| `CALLER_ALERTS_ENABLED`          | `true`                           | Kill switch — skip all email work when `false`.              |
| `CALLER_ALERTS_EMAIL_TO`         | `""`                             | Recipient address. Empty disables the alert (fail-open).     |
| `CALLER_ALERTS_EMAIL_FROM`       | `Outside Line <onboarding@resend.dev>` | Resend's test domain works without DNS — sends only to the |
|                                  |                                  | email used to sign up for Resend. Replace with a real        |
|                                  |                                  | `Agent <agent@yourdomain.com>` once a domain is verified at  |
|                                  |                                  | https://resend.com/domains.                                  |
| `CALLER_ALERTS_RESEND_API_KEY`   | `""`                             | Resend Bearer token (https://resend.com/api-keys).           |

## Failure semantics

Fail-open across the board:

- Any blank required setting → the alert is silently skipped with a
  single `caller.alert.skipped` info log. The call path is never
  affected.
- HTTP errors (transport-level + 4xx/5xx from Resend) log
  `caller.alert.failed` at warning level with the status + body
  preview. Never raises into the caller.
- The send runs in a background task; the TwiML response is returned
  before the HTTP POST even leaves the box.

## Consequences

- One less external dep (`aiosmtplib`); `httpx` is already in the
  image so no install footprint change.
- API keys can be revoked / rotated in the Resend dashboard
  independently of the recipient's mailbox credentials.
- Domain verification is deferred until volume / branding requires
  it. The default `onboarding@resend.dev` is fine for operator alerts;
  a real domain unlocks "from any address" delivery.
- The email plumbing lives in one module
  (`src/outside_line/alerts.py`) — easy to swap if HTML templating or
  fan-out is ever needed. (The per-call report, ADR 0022, is the current
  sole sender.)
