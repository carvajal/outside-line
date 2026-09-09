# Roadmap

Open, well-scoped improvements. Each entry is a condensed
problem/fix pair; the durable design record for anything that ships
belongs in `docs/decisions/`.

## Deterministic directline retry

**Problem.** On a directline no-answer, the fallback agent session only
gets a narration hint about who didn't pick up; the *retry* turn runs on
the persistent session, and the model re-derives the `call_contact`
argument freely — a real call once retried the wrong contact. The
mitigations that shipped (non-contact example name in the persona;
fuller spoken identifier in the no-answer instruction) lower the odds
but don't make the mixup impossible.

**Fix.** In a directline context the target is already known
(`DirectlineSession.contact`), so the retry must not go through
free-text `call_contact` at all: pin the contact on the fallback
session, expose a **parameterless** `retry_call` tool that re-rings the
pinned contact, and disable (or hard-override) `call_contact` for that
session. For a directline ("one number = one person", ADR 0013) the safe
default is to lock the session to the pinned contact.

**Verify.** Dial a directline whose contact won't answer, let it fall
back, ask for a retry, and confirm the same contact re-rings even when
the caller's memory file names a different contact.

## Skip the hangup RPC on a sidecar crash

**Problem.** On a sidecar SIGSEGV mid-bridge, `_run_bridge_phase`'s
cleanup unconditionally awaits `telegram_bridge.hangup(user_id)`. The
sidecar is dead, so the RPC blocks ~2 s until `SidecarDied`, delaying
the crash recording by that long and logging a noisy
`phase.bridge.hangup_failed_in_cleanup`.

**Fix.** Gate the hangup on the exit reason the cleanup already knows:
skip it when `reason == "sidecar_crashed"` — there is nothing to hang
up (the WebRTC peer died with the process; a fresh sidecar holds no
reference to the call).

**Verify.** Re-run the `/debug/crash-sidecar` live test on a bridged
call: the recording should start ~immediately after
`phase.bridge.ended`, with no `hangup_failed_in_cleanup` line.

## Migrate Twilio automation off the CLI onto the REST API

**Problem.** The repo runs two Twilio access patterns: direct REST via
httpx (recordings/archive — zero extra dependencies, proven) and the
`twilio-cli` wrapper (`scripts/twilio.py` → used by `_dev_mode.py` and
`add_contact.py`), which drags a fragile Node 20 + twilio-cli dependency
chain (ADR 0021 pinned around one breakage already).

**Fix.** Extract a shared `scripts/_twilio_rest.py` helper (creds
loading, `2010-04-01` base URL, authenticated httpx client, error
surfacing), adopt it in the existing REST scripts, then move the ~6
CLI-backed operations (`phone-numbers:list/update/create/remove`,
available-number search, calls list/fetch) onto it. Keep
`scripts/twilio.py` as the interactive escape hatch only. Do it
incrementally — helper first, then one subcommand family per commit,
each read-back-verified against a live DID.

## Documentation

- **Deep-dive docs**, if the hub doc's summaries stop being enough (inbound-voice,
  realtime-brain, tools, telegram-bridge, audio-pipeline, platform) —
  write them if/when the hub doc's summaries stop being enough.
- **`docs/observability.md`** — which structlog events to grep and
  where things land.
- **`docs/caller-memory.md`** — the full memory loop: file shape,
  post-call merge, how memory reaches the session instructions.
- **`docs/cost-model.md`** — per-call cost breakdown (Twilio minutes,
  Realtime audio tokens, tool calls, Resend).
