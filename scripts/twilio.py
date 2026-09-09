#!/usr/bin/env python3
"""Wrapper for the twilio CLI.

- Loads this repo's ``.env`` (so credentials don't have to be exported manually).
- Remaps ``TWILIO_API_KEY_SID`` / ``TWILIO_API_KEY_SECRET`` (the .env names this
  project picked) to ``TWILIO_API_KEY`` / ``TWILIO_API_SECRET``
  (the names the twilio-cli expects for API-Key auth).
- Replaces the current process with ``twilio`` using ``os.execvp`` (same
  semantics as ``exec twilio "$@"`` in shell — exit status passes through).

Usage:
    ./scripts/twilio.py <subcommand> [flags...]

Examples:
    ./scripts/twilio.py phone-numbers:list
    ./scripts/twilio.py phone-numbers:update +15551230100 \\
        --voice-url=https://abc.ngrok-free.app/twilio/voice
    ./scripts/twilio.py help phone-numbers:update
"""

from __future__ import annotations

import os
import shutil
import sys

from _dotenv import ENV_FILE, load_dotenv

# Keys we need to find in the environment (or .env) for API-Key auth to work.
REQUIRED = ("TWILIO_ACCOUNT_SID", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET")

# twilio-cli 6.2.4 eagerly require()s an ESM package (@octokit/core, via a
# bundled CI script) at load time, which throws ERR_REQUIRE_ESM on Node < 20.19.
# Homebrew's twilio formula pins node@20 for exactly this reason, but if node@20
# isn't installed the CLI silently falls through to whatever Node is on PATH
# (e.g. an fnm-managed Node 18) and every command crashes. So pin node@20 for
# the child process. node@20 is keg-only (off the global PATH), so prepending it
# to the child's PATH never affects other Node projects on this machine.
def _find_node20() -> str | None:
    """Directory of a Node >= 20 install to prepend to PATH, or None.

    Checks Homebrew's keg-only node@20 in the usual prefixes (Apple
    Silicon, Intel mac / Linuxbrew). If none is found the caller warns
    and lets the ambient PATH Node take its chances.
    """
    prefixes = [os.environ.get("HOMEBREW_PREFIX", ""), "/opt/homebrew", "/usr/local"]
    for prefix in prefixes:
        if not prefix:
            continue
        candidate = os.path.join(prefix, "opt", "node@20", "bin")
        if os.path.isfile(os.path.join(candidate, "node")):
            return candidate
    return None


def main() -> int:
    if shutil.which("twilio") is None:
        sys.stderr.write(
            "error: 'twilio' CLI not found on PATH.\n"
            "       install with: brew tap twilio/brew && brew install twilio\n"
        )
        return 127

    # Merge .env into environ without clobbering values already set in the shell.
    env = os.environ.copy()
    for key, value in load_dotenv().items():
        env.setdefault(key, value)

    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        sys.stderr.write(
            f"error: missing {', '.join(missing)} (check {ENV_FILE})\n"
        )
        return 2

    env["TWILIO_API_KEY"] = env["TWILIO_API_KEY_SID"]
    env["TWILIO_API_SECRET"] = env["TWILIO_API_KEY_SECRET"]

    node20 = _find_node20()
    if node20:
        # Pin Node >= 20.19 ahead of any fnm/other Node on PATH (child
        # process only).
        env["PATH"] = f"{node20}{os.pathsep}{env.get('PATH', '')}"
    else:
        sys.stderr.write(
            "warning: no dedicated Node >= 20.19 found — twilio-cli 6.2.4 "
            "crashes with ERR_REQUIRE_ESM on older Node. If the CLI fails, "
            "install Node 20+ (macOS: brew install node@20) or make it the "
            "default on PATH.\n"
        )

    os.execvpe("twilio", ["twilio", *sys.argv[1:]], env)


if __name__ == "__main__":
    raise SystemExit(main())
