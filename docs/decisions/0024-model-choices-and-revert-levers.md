# 0024 — Model choices and revert levers

**Date:** 2026-07-11
**Status:** Accepted

Folds ADRs 0015 (caller transcription), 0016 (search/text model) and 0019
(realtime model). Those three recorded the same decision three times, for
three model slots. This is the merged record; the numbers stay retired.

## Context

Four model slots drive the agent, and each one will be obsolete within
months:

| Slot | Setting | Where it runs |
|---|---|---|
| Realtime conversation | `openai_realtime_model` | `realtime_agent.py` → `client.realtime.connect(...)` |
| Caller-speech transcription | `openai_transcription_model` (+ `openai_transcription_language`) | the Realtime session's `audio.input.transcription` slot |
| Search / text reasoning | `openai_search_model` | `tools/search.py`, `case_brief_lookup` |
| Post-call memory merge | `caller_memory_model` | `memory_updater.py` |

The recurring question is not "which model" — that answer expires — but
**how a model gets swapped when the answer changes.**

## Decision

**Every model id is a plain env-overridable `str`, never a `Literal`.** A new
snapshot needs a platform variable, not a code change, not a redeploy. The
process restarts on the variable change and picks the new id up.

That makes the revert lever the interesting artifact:

| Variable | Default | Reverts |
|---|---|---|
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | the conversational surface |
| `OPENAI_TRANSCRIPTION_MODEL` | `gpt-4o-transcribe` | caller-speech transcription |
| `OPENAI_TRANSCRIPTION_LANGUAGE` | `en` | blank = auto-detect |
| `OPENAI_SEARCH_MODEL` | `gpt-5.4-mini` | search + all text reasoning |
| `SEARCH_WEB_TIMEOUT_S` | `15.0` | the search worker's patience |

A model change is therefore an A/B on real traffic with an instant rollback,
not a deploy with a rollback plan.

## Findings worth keeping

Three non-obvious things the three original records established, each of
which cost a live investigation:

- **Input transcription is a side channel — pick it for quality, not
  latency.** It does not gate the spoken reply: the realtime model answers
  from the input audio directly, and turn detection commits the turn
  independently of when transcription completes. A heavier transcribe model
  costs nothing on the caller's critical path. Its output *is* load-bearing
  downstream, though — transcript logs, the post-call memory merge, and the
  empty-transcript nudge all read it — so accuracy moves memory quality and
  how often the agent spuriously asks the caller to repeat.

- **`filters.allowed_domains` on the hosted `web_search` tool is a hard
  restriction, not a hint.** Results come *only* from the listed domains, so
  an allowlist meant to steer news sourcing silently cripples every unrelated
  lookup (weather, sports, general questions). Steer source diversity from the
  worker prompt instead and leave the filter for a future path that can detect
  the query type first.

- **Don't widen the search timeout without a mid-wait filler.** Every second
  past the agent's short spoken preamble is dead air on a live call. The
  timeout is capped below what the search worker could usefully consume,
  deliberately; raise it only once something is speaking during the wait.

## Rejected alternatives

- **A mini realtime tier.** Cheaper and faster, but this is a small number of
  high-value turns per call at high reasoning effort, not a latency-sensitive
  scripted IVR. Strongest available realtime reasoning wins.
- **A full-duplex consumer model.** Evaluated and not adoptable: no developer
  API, and backchannels-on-by-default is the inverse of what this line needs
  (patience with a slow talker, silence on line noise).
- **Swapping the search backend** for a dedicated search vendor. The failure
  modes were prompt and config problems on the hosted tool, not retrieval
  problems. No new dependency was warranted.
- **Pinning model ids as `Literal` types** for type safety. It buys a
  compile-time check on a value that changes faster than the code, at the cost
  of the revert lever above.

## Consequences

- Model upgrades are cheap and reversible; that is the whole point.
- The defaults in `config.py` are a snapshot of one moment and are expected to
  drift. Treat them as starting points, not recommendations.
- Because reverts happen through the platform rather than through git, a model
  actually running in production may differ from the default in this repo.
  The session-opened log line carries the model ids in use for exactly this
  reason.
