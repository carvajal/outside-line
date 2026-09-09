#!/usr/bin/env -S uv run python
"""List and fetch Twilio call recordings for after-the-fact debug listening.

The runtime app (``twilio_handler._start_recording``) triggers a dual-channel
recording on every inbound call when ``RECORDINGS_ENABLED=true``. Audio lives
on Twilio's infrastructure — this script is the operator-facing way to browse
and download them. Browse in the Console at
https://console.twilio.com/us1/monitor/logs/call-recordings.

Auth: HTTP Basic with the project's Twilio **API Key** (``SK…`` + secret) —
same model as ``scripts/twilio.py``. Credentials come from ``.env``.

Usage:
    ./scripts/recordings.py list [--limit N]
    ./scripts/recordings.py fetch <call_sid_or_recording_sid> [--out PATH]

Examples:
    ./scripts/recordings.py list --limit 10
    ./scripts/recordings.py fetch CAabcdef1234        # latest recording for that call
    ./scripts/recordings.py fetch REabcdef1234        # specific recording by SID
    ./scripts/recordings.py fetch CAabcdef --out /tmp/debug.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from _dotenv import ENV_FILE, env as _env

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "recordings"
TWILIO_API = "https://api.twilio.com/2010-04-01"

REQUIRED = ("TWILIO_ACCOUNT_SID", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET")


def load_creds() -> tuple[str, tuple[str, str]]:
    """Return ``(account_sid, (api_key_sid, api_key_secret))`` or exit."""
    env = _env()
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        sys.stderr.write(f"error: missing {', '.join(missing)} (check {ENV_FILE})\n")
        sys.exit(2)
    return env["TWILIO_ACCOUNT_SID"], (env["TWILIO_API_KEY_SID"], env["TWILIO_API_KEY_SECRET"])


def cmd_list(args: argparse.Namespace) -> int:
    account_sid, auth = load_creds()
    url = f"{TWILIO_API}/Accounts/{account_sid}/Recordings.json"
    resp = httpx.get(url, params={"PageSize": str(args.limit)}, auth=auth, timeout=15.0)
    if resp.status_code >= 400:
        sys.stderr.write(f"error: HTTP {resp.status_code}: {resp.text[:500]}\n")
        return 1
    recordings = resp.json().get("recordings") or []
    if not recordings:
        print("(no recordings)")
        return 0
    # Tabular: SID | CALL_SID | DUR | CH | DATE
    print(f"{'RECORDING_SID':<36}  {'CALL_SID':<36}  {'DUR':>5}  {'CH':>2}  DATE_CREATED")
    for r in recordings:
        print(
            f"{r.get('sid', ''):<36}  "
            f"{r.get('call_sid', ''):<36}  "
            f"{str(r.get('duration', '')):>5}  "
            f"{str(r.get('channels', '')):>2}  "
            f"{r.get('date_created', '')}"
        )
    return 0


def _resolve_recording_sid(account_sid: str, auth: tuple[str, str], sid: str) -> str:
    """Map a Call SID (``CA…``) to its most recent Recording SID (``RE…``).

    A Recording SID passes through unchanged. Anything else is rejected.
    """
    if sid.startswith("RE"):
        return sid
    if not sid.startswith("CA"):
        sys.stderr.write(f"error: SID must start with CA (call) or RE (recording): {sid!r}\n")
        sys.exit(2)
    url = f"{TWILIO_API}/Accounts/{account_sid}/Calls/{sid}/Recordings.json"
    resp = httpx.get(url, auth=auth, timeout=15.0)
    if resp.status_code >= 400:
        sys.stderr.write(f"error: HTTP {resp.status_code}: {resp.text[:500]}\n")
        sys.exit(1)
    recordings = resp.json().get("recordings") or []
    if not recordings:
        sys.stderr.write(f"error: no recordings found for call {sid}\n")
        sys.exit(1)
    # Twilio returns newest-first; take the first.
    return recordings[0]["sid"]


def cmd_fetch(args: argparse.Namespace) -> int:
    account_sid, auth = load_creds()
    recording_sid = _resolve_recording_sid(account_sid, auth, args.sid)
    out = Path(args.out) if args.out else (DEFAULT_OUT_DIR / f"{args.sid}.wav")
    out.parent.mkdir(parents=True, exist_ok=True)
    url = f"{TWILIO_API}/Accounts/{account_sid}/Recordings/{recording_sid}.wav"
    with httpx.stream("GET", url, auth=auth, timeout=60.0) as resp:
        if resp.status_code >= 400:
            sys.stderr.write(f"error: HTTP {resp.status_code}\n")
            return 1
        with out.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                f.write(chunk)
    print(f"wrote {out} ({out.stat().st_size} bytes, recording_sid={recording_sid})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list recent recordings")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_fetch = sub.add_parser("fetch", help="download a recording as .wav")
    p_fetch.add_argument("sid", help="call SID (CA…) or recording SID (RE…)")
    p_fetch.add_argument("--out", help=f"output path (default: {DEFAULT_OUT_DIR}/<sid>.wav)")
    p_fetch.set_defaults(func=cmd_fetch)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
