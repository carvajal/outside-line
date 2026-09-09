#!/usr/bin/env -S uv run python
"""Switch the outside-line Twilio voice path between two modes.

Modes:

* ``prod``    — Twilio → Railway. The server always renders the DTMF gate
                for allowed callers not flagged skip_gate (length =
                ``DTMF_GATE_SECONDS``, default 20). Bypassed callers
                (``./scripts/callers.py skip-gate``) skip the gate — that's
                the only escape hatch.
* ``local``   — Twilio → ngrok, local uvicorn auto-started. Same gate
                behavior; testing fast iteration assumes your number is on
                the gate-skip list.
* ``status``  — Read-only report of the live mode (default if no arg).

``railway up --detach`` runs only when leaving ``local`` (the only state
where Railway's code may have drifted from the working tree). ngrok + the
local uvicorn are stopped on entry to ``prod``, started on entry to
``local``.

Usage:
    ./scripts/dev_mode.py [prod | local | status]
"""

from __future__ import annotations

import argparse
import sys

import _dev_mode as dm


def _say(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", flush=True)


def _fmt_change(key: str, before: str | None, after: str | None) -> str:
    return f"  {key}: {before!r} -> {after!r}"


def _apply_env(updates: dict[str, str | None]) -> None:
    changes = dm.update_env(updates)
    if not changes:
        _say("env", "(no changes)")
        return
    for key, (before, after) in changes.items():
        print(_fmt_change(key, before, after))


def _sweep_one(
    *,
    label: str,
    target: str,
    current: dict[str, str | None],
    setter,
) -> None:
    """Sweep one webhook field (voice or statusCallback) to ``target``.

    Idempotent: no-op if every managed DID already matches; otherwise
    prints one ``<label>[<phone>]: <before> -> <after>`` line per DID
    that actually changed.
    """
    if not current:
        _say(
            "twilio",
            f"no managed numbers found (need friendlyName starting with "
            f"{dm.MANAGED_FRIENDLY_NAME_PREFIX!r}); skipping {label} update",
        )
        return
    target_norm = dm.normalize_voice_url(target)
    if all(dm.normalize_voice_url(u) == target_norm for u in current.values()):
        _say("twilio", f"all {len(current)} managed numbers already on {label}={target!r}")
        return
    after = setter(target)
    for phone in sorted(after):
        before_url = current.get(phone)
        after_url = after[phone]
        if dm.normalize_voice_url(before_url) != dm.normalize_voice_url(after_url):
            print(_fmt_change(f"{label}[{phone}]", before_url, after_url))


def _set_webhooks(target_voice_url: str, target_status_callback_url: str) -> None:
    """Sweep BOTH the voice webhook and the call-lifecycle webhook to the
    given URLs across every managed number.

    Pointing the two webhooks at different hosts would always be a bug
    (the statusCallback teardown helper would target a session
    registered on a different process), so both are swept in lockstep.
    """
    _sweep_one(
        label="voiceUrl",
        target=target_voice_url,
        current=dm.current_twilio_voice_urls(),
        setter=dm.set_twilio_voice_webhook,
    )
    _sweep_one(
        label="statusCallback",
        target=target_status_callback_url,
        current=dm.current_twilio_status_callbacks(),
        setter=dm.set_twilio_status_callback,
    )


def _stop_local_stack() -> None:
    if dm.dev_serve_running():
        _say("uvicorn", "stopping")
        dm.dev_serve_stop()
    else:
        _say("uvicorn", "not running")
    killed = dm.kill_ngrok()
    _say("ngrok", f"{killed} process(es) killed" if killed else "not running")


def _redeploy_if_needed(from_mode: str) -> None:
    if from_mode == "local":
        _say("railway", "redeploying (leaving local) ...")
        dm.railway_up_and_wait()
        _say("railway", "/healthz: 200")
    else:
        _say("railway", "no redeploy needed (not leaving local)")


# ---------------------------------------------------------------------------
# Mode handlers
# ---------------------------------------------------------------------------


def cmd_prod(from_mode: str) -> int:
    _say("mode", f"{from_mode} -> prod")
    _stop_local_stack()
    _apply_env({"PUBLIC_BASE_URL": dm.prod_url()})
    _redeploy_if_needed(from_mode)
    _set_webhooks(
        dm.desired_voice_url("prod"),
        dm.desired_status_callback_url("prod"),
    )
    print()
    print("prod. gate active unless skip_gate is set; the gate-skip list is the escape hatch.")
    return 0


def cmd_local(from_mode: str) -> int:
    _say("mode", f"{from_mode} -> local")
    # .env first so the uvicorn re-exec below picks up the new PUBLIC_BASE_URL.
    _apply_env({"PUBLIC_BASE_URL": dm.dev_url()})
    _say("ngrok", "killing any prior ngrok ...")
    dm.kill_ngrok()
    _say("ngrok", f"starting on {dm.NGROK_DOMAIN} -> :{dm.LOCAL_PORT} ...")
    try:
        url = dm.start_ngrok(dm.NGROK_DOMAIN, dm.LOCAL_PORT)
    except RuntimeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    _say("ngrok", f"tunnel up at {url}")
    _set_webhooks(
        dm.desired_voice_url("local"),
        dm.desired_status_callback_url("local"),
    )
    _say("uvicorn", "starting (or restarting) ...")
    dm.dev_serve_start()
    print()
    print("local. tail logs:  ./scripts/dev_serve.py --tail")
    return 0


def cmd_status() -> int:
    rows = dm.list_phone_numbers()
    managed = [r for r in rows if r.is_managed]
    unmanaged = [r for r in rows if not r.is_managed]
    mode = dm.detect_mode()
    base = dm.env_value("PUBLIC_BASE_URL")
    uvicorn_up = dm.dev_serve_running()
    ngrok_url = dm._public_tunnel_url()

    print(f"mode:                 {mode}")
    if managed:
        print(f"managed numbers ({len(managed)}):")
        for r in managed:
            voice = r.voice_url or "<unset>"
            sc = r.status_callback or "<unset>"
            voice_tag = (
                f"  [{dm.host_to_mode(r.voice_url)}]"
                if mode in ("mixed", "unknown")
                else ""
            )
            sc_tag = (
                f"  [{dm.host_to_mode(r.status_callback)}]"
                if mode in ("mixed", "unknown")
                else ""
            )
            print(f"  {r.phone_number}  {r.friendly_name}")
            print(f"    voiceUrl       : {voice}{voice_tag}")
            print(f"    statusCallback : {sc}{sc_tag}")
    else:
        print(
            f"managed numbers:      (none — need friendlyName prefix "
            f"{dm.MANAGED_FRIENDLY_NAME_PREFIX!r})"
        )
    if mode in ("mixed", "unknown"):
        print("hint:                 ./scripts/dev_mode.py local OR prod to resync")
    if unmanaged:
        max_phone_u = max(len(r.phone_number) for r in unmanaged)
        print(
            f"unmanaged numbers ({len(unmanaged)}):  not rotated by dev-mode — "
            f"rename to {dm.MANAGED_FRIENDLY_NAME_PREFIX!r}-prefixed to manage"
        )
        for r in unmanaged:
            print(f"  {r.phone_number.ljust(max_phone_u)}  {r.friendly_name}")
    print(f"PUBLIC_BASE_URL .env: {base}")
    print(f"uvicorn local:        {'RUNNING' if uvicorn_up else 'not running'}")
    print(f"ngrok tunnel:         {ngrok_url or 'not running'}")
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


HANDLERS = {
    "prod": cmd_prod,
    "local": cmd_local,
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Switch the outside-line Twilio voice path between prod and local.",
    )
    ap.add_argument(
        "mode",
        nargs="?",
        default="status",
        choices=["prod", "local", "status"],
    )
    args = ap.parse_args()

    if args.mode == "status":
        return cmd_status()

    from_mode = dm.detect_mode()
    return HANDLERS[args.mode](from_mode)


if __name__ == "__main__":
    raise SystemExit(main())
