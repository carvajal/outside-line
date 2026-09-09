# 0021 — twilio-cli pinned to node@20 (ERR_REQUIRE_ESM under Node < 20.19)

**Date:** 2026-07-14
**Status:** Accepted

## Context

Every `twilio` command crashed at module load with `ERR_REQUIRE_ESM`, taking
down every script that shells out to it (`scripts/twilio.py`,
`add_contact.py`, `dev_mode.py`).

Chain (empirically confirmed, not hypothesized):

- twilio-cli 6.2.4's `@twilio/cli-core/src/index.js:19` eagerly `require()`s a
  bundled CI-only script (`.github/scripts/update-release.js`) at import time,
  which in turn `require()`s `@octokit/core@7` — a pure-ESM package
  (`"type": "module"`).
- `require()` of an ESM package throws `ERR_REQUIRE_ESM` on Node **< 20.19**
  (require(esm) was unflagged in Node 20.19 / 22.12). So this is a packaging bug
  that is *fatal only on old Node*.
- Homebrew's twilio formula pins `node@20` for exactly this reason (`bin/twilio`
  prepends `/opt/homebrew/opt/node@20/bin`; `libexec/bin/run` shebang is
  `#!/opt/homebrew/opt/node@20/bin/node`). But on this machine `node@20` was not
  installed, so the launcher's PATH search skipped the dead `node@20` dir and
  fell through to fnm-managed Node **18.20.1** → crash.
- Verified: the same 6.2.4 binary runs cleanly under Node 20.20 / 22.15
  (`twilio --version` → `node-v20.20.2`; live `phone-numbers:list` returns the
  real DID roster).

## Decision

Require Node ≥ 20.19 and enforce it at the single chokepoint. On the
reference workstation (macOS + Homebrew) that means the keg-only `node@20`;
the wrapper degrades to a warning when no Homebrew keg is found, so a
machine whose default Node is already ≥ 20.19 still works.

- **Install `node@20`** (`brew install node@20`, currently 20.20.2). It is
  keg-only — not symlinked onto the global PATH — so it never shadows the
  fnm-managed Node used by every other repo on the machine.
- **Enforce it in [`scripts/twilio.py`](../../scripts/twilio.py).** Every repo
  path routes through this wrapper (`add_contact.py` and `_dev_mode.py`
  use it via their `TWILIO` constant). Before `os.execvpe`, it
  prepends `$HOMEBREW_PREFIX/opt/node@20/bin` to the **child process's** PATH so
  the CLI runs under node@20 regardless of the caller's shell Node. If node@20
  is absent it fails fast with a `brew install node@20` hint instead of a
  cryptic ERR_REQUIRE_ESM stack.

The PATH edit lives only in the `env` dict handed to the one `twilio` child —
it never touches the shell PATH, `~/.zshrc`, or fnm's per-shell shims.

## Trade-off accepted

Node ≥ 20.19 becomes a documented environment requirement. Chosen over:
patching the installed CLI files (wiped on `brew upgrade`), and rewriting the
~6 automation subcommands onto the Twilio REST API via `httpx` (the
`archive.py`/`recordings.py` pattern) — the latter would kill this breakage
class permanently but is a larger rewrite — tracked in `docs/roadmap.md`.

## Verification

- `/opt/homebrew/opt/node@20/bin/node --version` → ≥ `v20.19`.
- `./scripts/twilio.py --version` → `twilio-cli/6.2.4 … node-v20.x`, no crash,
  even with the shell on fnm's Node 18.
- `./scripts/twilio.py phone-numbers:list -o tsv` → live DID list.
- Missing-keg branch (`HOMEBREW_PREFIX=/tmp/nope ./scripts/twilio.py …`)
  prints the warning and proceeds on the ambient PATH Node.
- Shell `node --version` unchanged (still fnm's) — guard is process-local.
