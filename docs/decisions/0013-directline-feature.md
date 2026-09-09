# 0013 — Directlines: dedicated DIDs that connect a caller straight to one contact

**Date:** 2026-06-15
**Status:** Accepted

## Context

Every inbound call today lands on the agent (the OpenAI Realtime
session), which identifies the caller, takes a request, and on demand
bridges them to a Telegram contact via the `call_contact` tool. The
audio bridge (ADR 0009) hands the floor cleanly agent → contact
mid-call.

For **1:1 relationships** — a specific caller dialing a specific
contact, no triage needed — routing through the agent adds latency and
a mid-call voice change (synth agent, then live human) for no benefit.
A dedicated Twilio DID that maps to one Telegram contact takes the
agent out of the call path entirely: lower time-to-connected, one
stable voice end-to-end, and a much simpler failure story. The caller
hears only non-vocal hold audio (silent TwiML `<Pause>` blocks during
the DTMF acceptance gate, then a hum if we're still ringing the
contact when the gate completes) until the contact's live voice comes
up.

## Decision

Add a "directline" routing layer on top of the existing voice webhook.
Implementation in `src/outside_line/directlines.py`,
`src/outside_line/twilio_handler.py`, and `scripts/directlines.py`. The
`call_contact` tool flow is unchanged — directline is **additive**.

### The race model — two independent timelines

From T0 (caller arrives at our DID), two independent timelines run in
parallel:

* **Caller-side (Twilio).** TwiML executes
  `<Pause 20s>` → `<Play digits=1>` → `<Pause 20s>` → `<Connect><Stream>`.
  Stream opens ~T0+40 s. No audio path to caller before that (Twilio
  renders silence between events).
* **Contact-side (Telegram).** `TelegramBridge.place_call(contact,
  timeout_s=70)` starts at T0, spawned in `voice()` before TwiML
  returns. Resolves to `user_id` on pickup, `None` on timeout/error.

Two events drive transitions:

* `gate_done_event` — set by `media()` when the WS `start` event
  arrives (the stream is open; caller-side audio path is live).
* `tg_task.done()` — set when `place_call` returns.

The 4-quadrant state table determines what plays where:

| Caller side    | Contact side       | Caller hears                | Contact hears                       |
|----------------|--------------------|-----------------------------|-------------------------------------|
| GATE_PENDING   | RINGING            | TwiML silence               | (Telegram's own ringtone)            |
| GATE_PENDING   | ANSWERED           | TwiML silence               | wait phrase + PCM ringback           |
| GATE_DONE      | RINGING            | hold hum                    | (still ringing on TG side)           |
| GATE_DONE      | ANSWERED           | bridge audio from contact   | bridge audio from caller             |
| GATE_DONE      | TIMEOUT/ERROR      | agent fallback session      | n/a                                  |

The two timelines are owned by two coroutines:

* `_directline_ring(session, bridge)` — contact-side. Started in
  `voice()` before TwiML returns. Awaits `place_call`. On pickup, if
  `gate_done_event` isn't set yet, plays `WAIT_MESSAGE_PCM48_BYTES`
  (the pre-rendered "One second please…" phrase) then loops
  `RINGBACK_PCM48_LOOP` to the contact until the event fires.
* `_run_directline_phase(session, ...)` — caller-side. Called from
  `media()` immediately after the WS `start` event. Branches on
  `tg_task` state: already done → BRIDGE; pending → hold hum to
  caller + race; resolved to None → agent fallback with apology.

### Bridge: same as the call_contact flow, ending differently

Once both sides are ready, `_run_bridge_phase` handles the bidirectional
audio pump unchanged (the entire ADR 0009 BRIDGED state shape). The
**difference** is what happens on bridge exit:

* `call_contact` flow: contact hangs up → re-engage the agent with the
  "I'm back" reconnect prompt.
* Directline flow: contact hangs up → call is over. No agent
  reconnect. (The caller dialed this DID to talk to one person; when
  that conversation ends, so does the call.)

### Misconfiguration & failure modes

* **Caller hangs up during the gate.** WS never opens. The
  `_cleanup_orphan_directline_session` task fires at T0+70 s, hangs
  up the (possibly-answered) Telegram leg, cancels the ring task.
* **TG contact rejects / doesn't answer.** `tg_task` resolves to None
  → `_run_directline_phase` returns `fallback=True`. Standard agent
  loop runs with the no-answer apology.
* **Bridge throws or stalls.** Same shape as today's `call_contact`
  (the bridge stall detector also fires here).
* **Malformed config.** Directline points at an unknown username, or
  Telegram isn't wired up at boot → `directline.unavailable` logged,
  call falls through to the default agent path. Safer than rejecting
  a known caller.

### Allowlist / gate-skip interactions

* **Allowlist** is enforced upstream of the directline branch in
  `voice()`. Blocked callers dialing a directline still get
  `<Reject reason="busy"/>`. We don't want random spammers reaching
  the operator's contacts directly.
* **Gate skip** is orthogonal. A caller flagged to skip the DTMF gate
  who dials a directline DID still routes via directline, just
  without the gate; the `gate_done_event` fires almost immediately on
  WS open. Most quadrant transitions degenerate to the "GATE_DONE"
  rows — same code paths.

## Alternatives considered

* **Route directlines through `call_contact`.** Would mean the caller
  *does* speak to the agent briefly ("call Sam for me"); the agent
  invokes `call_contact`; the same BRIDGE phase runs. Rejected: a
  dedicated DID lets us know who the caller wants to reach without
  asking, so the happy path has no agent latency and no mid-call
  voice change.
* **URL-level routing (`/twilio/directline/<contact>`).** Would let
  Twilio's per-DID webhook config carry the routing instead of an
  in-app JSON store. Rejected: requires touching the Twilio console
  to add/move/disable a directline, which is slower and more error-
  prone than `./scripts/directlines.py add` mirroring to the Railway
  volume.
* **Add a `twilio_did` field on `Contact`** instead of a separate
  `data/directline_numbers.json` store. Rejected: conflates the
  stable address-book identity (contact_name, username, aliases —
  the things the agent uses to disambiguate during `call_contact`)
  with the operational DID routing. A separate store also mirrors the
  callers.py operator-surface pattern.
* **Pre-place the call via Twilio Conference API**, joining the caller
  to a Telegram-bridged conference room. Rejected for v1:
  significantly more Twilio surface area (Conference resource,
  participants, hold music, recording wiring) for the same outcome.

## Known limitations (acceptable for v1)

* **Single concurrent active TG call.** `TelegramBridge` is a singleton.
  If a directline call arrives while a `call_contact` (or another
  directline) is already bridging, `place_call` fails and the second
  call degrades to the agent's no-answer fallback. Single-digit
  calls/day volume; future enhancement is a per-user_id bridge pool.
* **Single wait-phrase language.** The pre-rendered asset is rendered
  in one language. A caller expecting another language hears the same
  phrase. Future: render per-language variants, pick by a
  directline-entry language hint.
* **Mid-bridge agent re-engagement skipped.** Today's `call_contact`
  reconnects the agent after the contact ends the call. For
  directlines we don't. The caller can still hang up and redial; we
  accept that trade for one stable voice end-to-end.

## Verification

* Unit tests in `tests/test_directline_phase.py` cover the 4 quadrants
  with a fake bridge (`_run_bridge_phase` and `_ring_audio_driver`
  patched out so dispatch logic is exercised in isolation).
* Curl-shape verification via FastAPI TestClient (sandbox callers +
  directlines JSON, fake TelegramBridge on app.state): all 5 paths
  pass (gated directline, gate-skipped directline, non-directline,
  blocked caller on directline DID, session registry correctness).
* **Prod end-to-end** is the real test — the operator provisions one
  DID via `scripts/add_contact.py provision` (ADR 0018) and validates
  the 4 real-call scenarios (TG-fast, TG-slow, TG-no-answer,
  caller-hangup-during-gate).

## Design refinement (2026-06-17): fast caller-hangup teardown

The original v1 relied on a single signal for cleaning up the Telegram
side when the caller bails: `_cleanup_orphan_directline_session` fires
`DIRECTLINE_RING_TIMEOUT_S = 70` s after `voice()` returns. That's
fine when the caller stays through the gate (the WS opens and the
WS-close branch in `_run_directline_phase` runs teardown promptly),
but it's slow for **scenario 2** — caller hangs up *during* the gate
(before the Media Stream WS opens), then the contact picks up later
and is stuck hearing the wait phrase + ringback for up to ~50 s.

Added a second per-DID webhook:
**`POST /twilio/voice/status`**. Twilio fires it once per call when
`CallStatus` reaches a terminal value (default: `completed`),
regardless of WS state. The handler calls
`directlines.pop_session(CallSid)` and runs the same
`_finalize_directline_session` helper the other teardown sites use —
no new teardown logic, just a faster signal. Scenario-2 holding-audio
window collapses from up to ~70 s to ~1-2 s.

* **IncomingPhoneNumber-level**, not TwiML-level. `<Connect><Stream>`
  doesn't carry a stream-end callback that fires when the parent call
  ends during the inbound TwiML render — the WS hasn't opened yet.
  Per-DID statusCallback on the IncomingPhoneNumber resource is the
  only signal that arrives across all caller-hangup timings.
* **All managed DIDs, not just directlines.** Mainline calls fire
  statusCallback too; the handler's `pop_session` returns `None`
  → logged no-op + 200. Cheap, future-proofs for adding more
  managed numbers.
* **200 unconditionally.** Twilio retries non-2xx for up to 24 h;
  idempotent no-op sidesteps the retry queue when a race already
  finalized the session.
* **The orphan-cleanup timer stays as backstop** for delayed or failed
  webhook delivery, and every managed DID carries **both** webhooks in
  lockstep (`scripts/dev_mode.py` flips voice + statusCallback
  together).
