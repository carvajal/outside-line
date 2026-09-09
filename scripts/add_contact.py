#!/usr/bin/env -S uv run python
"""One-shot provisioning for a new contact + its directline DID.

The operator supplies the contact's details (Telegram username, first
name, optional aliases/keywords, desired area code) and drives the two
subcommands here:

* ``search`` — read-only. Lists buyable local numbers in an area code (or
  reports ``NO_INVENTORY`` so the caller can fall back to a nearby overlay).
  Never spends money.
* ``provision`` — the mutating, money step. Given an exact phone number and
  the contact fields, it: (1) refuses on a fuzzy name collision, (2)
  idempotently buys + wires the DID (reusing an already-owned number),
  (3) registers the directline, (4) adds the contact (which triggers a
  ``railway redeploy`` so the in-process contact cache reloads), then (5)
  waits for the redeploy and verifies resolution. Every run prints a
  machine-readable JSON summary on stdout so a wrapper can recover precisely.

It *shells out* to the existing operator CLIs rather than reimplementing
them — ``scripts/twilio.py`` (buy/wire), ``scripts/directlines.py`` (map),
``scripts/contacts.py`` (contact + redeploy) — and reuses ``scripts/_dev_mode.py``
for the prod webhook URLs + managed-name convention, so a newly bought DID is
consistent with dev-mode's prod/local sweep by construction.

Usage:
    ./scripts/add_contact.py search --area-code 512 [--limit 5]
    ./scripts/add_contact.py provision --phone-number +15551230177 \\
        --username @sam_example --first-name Sam \\
        --aliases "Sam Reyes, Sammy" [--keywords cousin] \\
        [--label "Sam directline"] [--allow-collision] \\
        [--no-wait-deploy] [--no-verify]

Contact-only additions (no DID) stay on ``scripts/contacts.py add``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Stdlib-only sibling; safe to import at module load. Reused so the friendly
# name + webhook URLs live in exactly one place (dev-mode's sweep depends on
# the same convention).
from _dev_mode import (  # noqa: E402
    MANAGED_FRIENDLY_NAME_PREFIX,
    desired_status_callback_url,
    desired_voice_url,
    list_phone_numbers,
    poll_healthz,
    prod_url,
)

TWILIO = str(REPO_ROOT / "scripts" / "twilio.py")
DIRECTLINES = str(REPO_ROOT / "scripts" / "directlines.py")
CONTACTS = str(REPO_ROOT / "scripts" / "contacts.py")

# ``railway deployment list`` rows look like:
#   <uuid> | BUILDING | 2026-07-08 14:48:54 -07:00
_DEPLOYMENT_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f-]{27,})\s*\|\s*([A-Z_]+)\s*\|"
)
_TERMINAL_OK = {"SUCCESS"}
_TERMINAL_FAIL = {"FAILED", "CRASHED"}

# Exit codes a wrapper can branch on.
EXIT_OK = 0
EXIT_ERROR = 2
EXIT_PARTIAL = 3
EXIT_COLLISION = 4


# -- small helpers ----------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def _parse_csv(raw: str | None) -> list[str]:
    """Split a comma-separated flag into a clean, de-duplicated list.

    ``"Sam Reyes, Sammy ,,Sam Reyes"`` ->
    ``["Sam Reyes", "Sammy"]``. Order-preserving, whitespace
    trimmed, empties dropped, duplicates removed.
    """
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if item and item not in out:
            out.append(item)
    return out


def _repeat(flag: str, values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        out += [flag, v]
    return out


def _friendly_name(first_name: str) -> str:
    """The managed Twilio friendly name a directline DID must carry.

    ``outside-line directline (<FirstName>)`` — the prefix is what includes the
    DID in dev-mode's prod/local sweep; the parenthetical contact name is the
    per-directline disambiguator (ADR 0013 / dev-mode convention).
    """
    return f"{MANAGED_FRIENDLY_NAME_PREFIX}directline ({first_name})"


def _emit(
    status: str,
    *,
    did: str | None = None,
    steps: dict[str, str] | None = None,
    next_hint: str | None = None,
    **extra: object,
) -> None:
    """Print the machine-readable summary a wrapper consumes."""
    payload: dict[str, object] = {"status": status}
    if did:
        payload["did"] = did
    payload["steps"] = steps or {}
    if next_hint:
        payload["next"] = next_hint
    payload.update(extra)
    print(json.dumps(payload, indent=2))


# -- search -----------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    """Read-only inventory probe for an area code. Never spends money."""
    proc = _run(
        [
            TWILIO,
            "api:core:available-phone-numbers:local:list",
            "--country-code=US",
            f"--area-code={args.area_code}",
            "--voice-enabled",
            "--properties=phoneNumber,locality,region",
            f"--limit={args.limit}",
            "-o",
            "json",
        ]
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        _emit(
            "ERROR",
            steps={"search": "twilio_error"},
            area_code=args.area_code,
            error=proc.stderr.strip()[:500],
        )
        return EXIT_ERROR
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        rows = []
    available = [
        {
            "phone_number": r.get("phoneNumber"),
            "locality": r.get("locality"),
            "region": r.get("region"),
        }
        for r in (rows if isinstance(rows, list) else [])
    ]
    _emit(
        "OK" if available else "NO_INVENTORY",
        steps={"search": "ok"},
        area_code=args.area_code,
        available=available,
    )
    if not available:
        # Loud on stderr too so a human tailing the run sees it immediately.
        sys.stderr.write(f"NO_INVENTORY area_code={args.area_code}\n")
    return EXIT_OK


# -- provision --------------------------------------------------------------


def _collision_hits(
    first_name: str, aliases: list[str], pool: object | None = None
) -> list[str]:
    """Existing contacts whose fuzzy resolution collides with the new name.

    Reuses the live resolver (``contacts.find_contacts``) so this matches
    exactly what the agent would do at call time — the invariant: a new name
    must not silently resolve to an existing contact. Returns the
    colliding first names (empty = clear).
    """
    from outside_line import contacts as contacts_mod

    if pool is None:
        pool = contacts_mod.load_contacts()
    hits: list[str] = []
    for query in [first_name, *aliases]:
        for c in contacts_mod.find_contacts(query, pool):  # type: ignore[arg-type]
            if c.first_name not in hits:
                hits.append(c.first_name)
    return hits


def _newest_deployment() -> tuple[str, str] | None:
    """``(deployment_id, STATUS)`` of the most recent Railway deployment."""
    proc = _run(["railway", "deployment", "list"])
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        m = _DEPLOYMENT_RE.search(line)
        if m:
            return m.group(1), m.group(2)
    return None


def _wait_for_new_deployment(prev_id: str | None, timeout: float) -> str:
    """Block until a deployment newer than ``prev_id`` reaches a terminal state.

    Returns ``"success"``, ``"failed:<STATUS>"``, or ``"timeout"``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        newest = _newest_deployment()
        if newest and newest[0] != prev_id:
            dep_id, st = newest
            if st in _TERMINAL_OK:
                return "success"
            if st in _TERMINAL_FAIL:
                return f"failed:{st}"
        time.sleep(10)
    return "timeout"


def _buy_or_reuse(
    e164: str, first_name: str, steps: dict[str, str]
) -> str | None:
    """Idempotently ensure ``e164`` is owned + wired. Returns an error string.

    Reuses an already-owned number (repairing friendly name/webhooks if
    needed) so a re-run after a mid-flow failure never double-charges. Stops
    if a *different* DID already carries this contact's friendly name (would
    mean two numbers for one contact).
    """
    name = _friendly_name(first_name)
    voice_url = desired_voice_url("prod")
    status_url = desired_status_callback_url("prod")

    rows = list_phone_numbers()
    owned = {r.phone_number: r for r in rows}
    name_clash = [
        r for r in rows if r.friendly_name == name and r.phone_number != e164
    ]
    if name_clash:
        return (
            f"friendly name {name!r} already on {name_clash[0].phone_number} — "
            "a directline for this contact may already exist"
        )

    existing = owned.get(e164)
    if existing is not None:
        steps["buy"] = "reused"
        # Repair convention drift on the reused number (best effort).
        _run(
            [
                TWILIO,
                "phone-numbers:update",
                e164,
                f"--friendly-name={name}",
                f"--voice-url={voice_url}",
                "--voice-method=POST",
                "-o",
                "json",
            ]
        )
        return None

    proc = _run(
        [
            TWILIO,
            "api:core:incoming-phone-numbers:create",
            f"--phone-number={e164}",
            f"--friendly-name={name}",
            f"--voice-url={voice_url}",
            "--voice-method=POST",
            f"--status-callback={status_url}",
            "--status-callback-method=POST",
            "-o",
            "json",
        ]
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return f"twilio create failed: {proc.stderr.strip()[:300]}"
    steps["buy"] = "bought"
    return None


def cmd_provision(args: argparse.Namespace) -> int:
    from outside_line import contacts as contacts_mod
    from outside_line import directlines as directlines_mod
    from outside_line.callers import normalize_e164

    steps: dict[str, str] = {}

    e164 = normalize_e164(args.phone_number)
    if e164 is None:
        _emit(
            "ERROR",
            steps=steps,
            error=f"{args.phone_number!r} is not a valid phone number",
        )
        return EXIT_ERROR

    username = args.username if args.username.startswith("@") else f"@{args.username}"
    aliases = _parse_csv(args.aliases)
    keywords = _parse_csv(args.keywords)
    label = args.label or f"{args.first_name} directline"

    # 1. Collision guard (fail-safe unless explicitly overridden).
    hits = _collision_hits(args.first_name, aliases)
    if hits and not args.allow_collision:
        _emit(
            "COLLISION",
            steps={"collision": "blocked"},
            collides_with=hits,
            next_hint=(
                "name/alias fuzzy-matches existing contact(s); pick a distinct "
                "name or re-run provision with --allow-collision"
            ),
        )
        return EXIT_COLLISION
    steps["collision"] = "override" if hits else "clear"

    # 2. Idempotent buy + wire.
    err = _buy_or_reuse(e164, args.first_name, steps)
    if err is not None:
        _emit("ERROR", did=e164, steps=steps, error=err)
        return EXIT_ERROR

    # 3. Directline mapping (upsert; syncs to the volume, no redeploy).
    dl = _run([DIRECTLINES, "add", e164, username, "--label", label])
    if dl.returncode != 0:
        sys.stderr.write(dl.stderr)
        _emit(
            "PARTIAL",
            did=e164,
            steps={**steps, "directline": "failed"},
            error=dl.stderr.strip()[:300],
            next_hint=f"re-run provision (idempotent) or: directlines.py add {e164} {username}",
        )
        return EXIT_PARTIAL

    steps["directline"] = "added"

    # 4. Contact (fires railway redeploy). Add, or update if it already exists.
    prev_deploy = _newest_deployment()
    prev_id = prev_deploy[0] if prev_deploy else None
    flags = _repeat("--alias", aliases) + _repeat("--keyword", keywords)
    add = _run([CONTACTS, "add", args.first_name, username, *flags])
    add_out = add.stdout + add.stderr
    if add.returncode != 0 and "already exists" in add_out:
        upd = _run(
            [CONTACTS, "update", args.first_name, "--username", username, *flags]
        )
        add_out = upd.stdout + upd.stderr
        if upd.returncode != 0:
            sys.stderr.write(upd.stderr)
            _emit(
                "PARTIAL",
                did=e164,
                steps={**steps, "contact": "update_failed"},
                error=upd.stderr.strip()[:300],
            )
            return EXIT_PARTIAL
        steps["contact"] = "updated"
    elif add.returncode != 0:
        sys.stderr.write(add.stderr)
        _emit(
            "PARTIAL",
            did=e164,
            steps={**steps, "contact": "failed"},
            error=add.stderr.strip()[:300],
        )
        return EXIT_PARTIAL
    else:
        steps["contact"] = "added"

    # 5. Redeploy: contacts.py fires it. Confirm it triggered, then wait.
    if "redeploy triggered" in add_out:
        if args.no_wait_deploy:
            steps["redeploy"] = "triggered"
        else:
            outcome = _wait_for_new_deployment(prev_id, args.deploy_timeout)
            steps["redeploy"] = outcome
            if outcome == "success":
                steps["healthz"] = (
                    "ok" if poll_healthz(f"{prod_url()}/healthz") else "down"
                )
    else:
        # railway CLI unavailable / redeploy failed — contact cache won't
        # reload until a manual redeploy.
        steps["redeploy"] = "not_triggered"

    # 6. Resolution proof (ring-safe: local files only, never a live call).
    resolved = True
    if not args.no_verify:
        dl_entry = directlines_mod.DirectlinesStore(
            REPO_ROOT / "data" / "directline_numbers.json"
        ).lookup(e164)
        contact = contacts_mod.find_by_username(
            username, contacts_mod.load_contacts()
        )
        resolved = dl_entry is not None and contact is not None
        steps["verify"] = "ok" if resolved else "unresolved"

    healthy = steps.get("redeploy") in {"success", "triggered"} and steps.get(
        "healthz", "ok"
    ) in {"ok"}
    if resolved and healthy:
        _emit(
            "SUCCESS",
            did=e164,
            steps=steps,
            next_hint=f"ready to try: dial {e164}",
        )
        return EXIT_OK

    _emit(
        "PARTIAL",
        did=e164,
        steps=steps,
        next_hint=(
            "mutations landed; check redeploy/verify above. Cache reloads on a "
            "successful redeploy — re-run provision (idempotent) or trigger "
            "`railway redeploy --yes` if it did not."
        ),
    )
    return EXIT_PARTIAL


# -- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="add_contact.py", description=__doc__.splitlines()[0]
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="list buyable local numbers in an area code")
    s.add_argument("--area-code", required=True, help="US area code, e.g. 512")
    s.add_argument("--limit", type=int, default=5, help="max numbers to return")
    s.set_defaults(func=cmd_search)

    v = sub.add_parser(
        "provision", help="buy+wire a DID, map it, add the contact, redeploy"
    )
    v.add_argument("--phone-number", required=True, help="exact DID to buy (E.164)")
    v.add_argument("--username", required=True, help="Telegram @username")
    v.add_argument("--first-name", required=True, help="name the agent speaks")
    v.add_argument("--aliases", default=None, help="comma-separated alt names")
    v.add_argument("--keywords", default=None, help="comma-separated keywords")
    v.add_argument("--label", default=None, help="directline label (operator display)")
    v.add_argument(
        "--allow-collision",
        action="store_true",
        help="proceed even if the name fuzzy-matches an existing contact",
    )
    v.add_argument(
        "--no-wait-deploy",
        action="store_true",
        help="don't block waiting for the redeploy to finish",
    )
    v.add_argument(
        "--no-verify", action="store_true", help="skip the resolution proof"
    )
    v.add_argument(
        "--deploy-timeout",
        type=float,
        default=360.0,
        help="seconds to wait for the redeploy (default 360)",
    )
    v.set_defaults(func=cmd_provision)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
