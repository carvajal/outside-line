# 0006 — Operator CLIs sync to the Railway volume

**Date:** 2026-06-02
**Status:** Accepted

## Context

The repo has three operator CLIs that mutate per-call state on disk:

- `scripts/callers.py` → `data/callers.json` (caller registry + gate-skip list)
- `scripts/contacts.py` → `data/contacts.json` (bridge contacts)
- `scripts/directlines.py` → `data/directline_numbers.json` (DID → contact map)

Production reads and writes the same paths on the Railway volume
mounted at `/app/data` (see ADR 0004): the FastAPI app calls
`CallersStore.record_call` on every inbound call to bump
`call_count` / `last_seen` and update the gate-skip TwiML branch;
the WS handler updates callers on every inbound call.

Until this change, the operator CLIs only touched the **local**
checkout. The Railway copy stayed authoritative, prod kept mutating
it, and the operator had no way to change it short of an interactive
`railway ssh` session. The local copy drifted by definition.

## Decision

Operator CLIs **mirror to and from the Railway volume by default**.
Every invocation runs:

1. **Pull** the authoritative state from `/app/data/...` into the
   local checkout.
2. **Run** the original subcommand against the local file/dir
   (unchanged code path — the store semantics are not touched).
3. **Push** the resulting state back to `/app/data/...` when the
   subcommand was mutating.

The local copy becomes a cache of the remote authoritative state
after each command. Reads stop after step 2; explicit `pull`
subcommands stop after step 1.

Wrappers + helpers live in `scripts/_railway.py` (extracted from
`scripts/archive.py`) and `scripts/_sync.py`. The push path for a
single file is one SSH round-trip:
`cat > <path>.tmp.$$ && mv <path>.tmp.$$ <path>` — atomic on the
remote filesystem. For a directory, the post-pull byte snapshot is
diffed against the post-mutation local state so only changed files
are uploaded, and files the mutation deleted locally are removed
from the remote.

A top-level `--no-remote` flag and an implicit fallback when
`railway status` fails together cover the offline case. The implicit
fallback prints a one-line `[remote sync skipped]` notice; the
explicit opt-out is silent.

## Accepted race window

Between the pull (step 1) and the push (step 3) production's
`record_call` can also write the file. If that happens, the operator
CLI's push will overwrite the prod-side `call_count` bump, losing
that bump. The window is ~1–2 seconds; call volume is a few per day;
the operator runs these commands infrequently. The probability is
small enough that we accept the loss rather than pay for a
mitigation.

If this ever bites in practice, the follow-up is to mutate
**server-side** by invoking the existing store APIs through
`railway ssh "python -c ..."` — race-free because the mutation
happens directly in the process that holds the file. Out of scope
for this change.

## Rejected alternatives

- **Mutate remotely (`railway ssh "python -c ..."`) from day one.**
  Closes the race, but every CLI command becomes "ssh in, run python,
  ssh out" — more moving parts, harder to debug, impossible offline.
  Not worth it for the current race likelihood.
- **A server-side mutation endpoint.** Adds an API surface (auth,
  schema, deploys) for single-operator tooling. Overkill. (Push-only,
  pull-only, and a bare `push` subcommand all reduce to clobber
  hazards or a read-only CLI — rejected for the same reason the
  pull→run→push sandwich exists.)

## Operator behavior

| Command path                                   | Pulls? | Pushes? |
| ---------------------------------------------- | :----: | :-----: |
| `./scripts/callers.py list \| show`            |   ✓    |    -    |
| `./scripts/callers.py add \| label \| skip-gate \| forget` |   ✓    |    ✓    |
| `./scripts/callers.py pull`                    |   ✓    |    -    |
| `... --no-remote ...` (any command)            |   -    |    -    |

## Consequences

- The operator CLIs now require Railway CLI access for the default
  path. Offline use needs `--no-remote` (intentional friction so
  drift is hard to introduce by accident).
- ~1 second of SSH overhead per command. Acceptable given low
  command frequency.
- The race window above is real but unmitigated. Documented; revisit
  if it ever causes lost data.
