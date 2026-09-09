#!/usr/bin/env -S uv run python
"""Operator CLI for the directline registry.

A *directline* is a dedicated Twilio DID that, when dialed, routes
straight to one Telegram contact. The agent stays out of the call path on
the happy path — it only picks up if the contact doesn't answer.
See ``docs/decisions/0013-directline-feature.md``.

Manages ``data/directline_numbers.json``: list known directlines, add a
new one, toggle the ``enabled`` flag, set a human label, remove
entries. ``list`` prints a row-numbered table and drops into a small
REPL so the operator can act on rows by number.

Usage:
    ./scripts/directlines.py list [--json] [--no-interactive]
    ./scripts/directlines.py show <did> [--json]
    ./scripts/directlines.py add <did> <@username> [--label "Sam direct"]
    ./scripts/directlines.py label <did> <label>
    ./scripts/directlines.py enable <did>
    ./scripts/directlines.py disable <did>
    ./scripts/directlines.py remove <did> [-y]
    ./scripts/directlines.py pull

By default every command pulls the authoritative
``data/directline_numbers.json`` from the Railway volume before running
and pushes back after a mutation. Pass ``--no-remote`` to operate
against the local copy only.

To buy a new directline DID and map it to a (new) contact in one guided
step, use ``scripts/add_contact.py`` — this
CLI is the lower-level primitive it calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRECTLINES_PATH = REPO_ROOT / "data" / "directline_numbers.json"
REMOTE_DIRECTLINES_PATH = "/app/data/directline_numbers.json"

sys.path.insert(0, str(REPO_ROOT / "src"))

# Imported after sys.path tweak (matches the pattern in callers.py).
from outside_line.callers import normalize_e164  # noqa: E402
from outside_line.directlines import DirectlinesStore  # noqa: E402

# Sibling-script imports.
from _repl import ReplRow, Verb, run_repl  # noqa: E402
from _sync import SyncSpec, with_sync  # noqa: E402


def _require_did(raw: str) -> str:
    """Normalize ``raw`` to E.164 or exit 2 with a clear error."""
    did = normalize_e164(raw)
    if did is None:
        sys.stderr.write(
            f"error: {raw!r} is not a valid phone number "
            f"(expected E.164 like +15551230188 or a 10-digit US number)\n"
        )
        sys.exit(2)
    return did


def _store(path: Path) -> DirectlinesStore:
    return DirectlinesStore(path)


def _print_table(entries: list[object]) -> None:
    """Render the row-numbered table. ``entries`` is list[DirectlineEntry]."""
    if not entries:
        print("(no directlines)")
        return
    print(
        f"{'#':>4}  {'DID':<16}  {'ENABLED':<7}  {'USERNAME':<24}  LABEL"
    )
    for i, e in enumerate(entries, start=1):
        enabled = "yes" if e.enabled else "NO"  # type: ignore[attr-defined]
        username = f"@{e.telegram_username}"  # type: ignore[attr-defined]
        label = e.label or "-"  # type: ignore[attr-defined]
        did = e.twilio_did  # type: ignore[attr-defined]
        print(f"[{i:>2}]  {did:<16}  {enabled:<7}  {username:<24}  {label}")


# -- subparser builder for `add` (shared by main() and REPL) ----------------


def _build_add_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="add", add_help=False)
    p.add_argument("did", help="Twilio DID (E.164 or 10-digit US)")
    p.add_argument("username", help="Telegram @username (with or without @)")
    p.add_argument("--label", default=None, help="optional human label")
    return p


# -- commands ---------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    entries = _store(args.directlines_path).list_all()
    if args.json:
        out = [
            {
                "twilio_did": e.twilio_did,
                "telegram_username": e.telegram_username,
                "label": e.label,
                "enabled": e.enabled,
            }
            for e in entries
        ]
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    _print_table(list(entries))
    if args.no_interactive or not sys.stdin.isatty():
        return 0
    return _run_repl(args)


def cmd_show(args: argparse.Namespace) -> int:
    did = _require_did(args.did)
    entry = _store(args.directlines_path).get(did)
    if entry is None:
        sys.stderr.write(f"error: no directline for {did}\n")
        return 2
    if args.json:
        print(
            json.dumps(
                {
                    "twilio_did": entry.twilio_did,
                    "telegram_username": entry.telegram_username,
                    "label": entry.label,
                    "enabled": entry.enabled,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"did:       {entry.twilio_did}")
    print(f"username:  @{entry.telegram_username}")
    print(f"enabled:   {entry.enabled}")
    print(f"label:     {entry.label or '-'}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    did = _require_did(args.did)
    try:
        entry = _store(args.directlines_path).upsert(
            did, args.username, label=args.label
        )
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    print(
        f"added {entry.twilio_did} → @{entry.telegram_username} "
        f"(enabled={entry.enabled}, label={entry.label!r})"
    )
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    did = _require_did(args.did)
    store = _store(args.directlines_path)
    existing = store.get(did)
    if existing is None:
        sys.stderr.write(f"error: no directline for {did}\n")
        return 2
    entry = store.upsert(did, existing.telegram_username, label=args.label)
    print(f"{entry.twilio_did}: label={entry.label!r}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    did = _require_did(args.did)
    entry = _store(args.directlines_path).set_enabled(did, True)
    if entry is None:
        sys.stderr.write(f"error: no directline for {did}\n")
        return 2
    print(f"{entry.twilio_did}: enabled={entry.enabled}")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    did = _require_did(args.did)
    entry = _store(args.directlines_path).set_enabled(did, False)
    if entry is None:
        sys.stderr.write(f"error: no directline for {did}\n")
        return 2
    print(f"{entry.twilio_did}: enabled={entry.enabled}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    did = _require_did(args.did)
    if not args.yes:
        try:
            ans = input(f"remove directline {did}? [y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() != "y":
            print("aborted")
            return 0
    deleted = _store(args.directlines_path).remove(did)
    if deleted:
        print(f"removed {did}")
    else:
        print(f"(no directline for {did})")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    # All the work happens in with_sync's pull step before this runs.
    print(f"pulled {REMOTE_DIRECTLINES_PATH} → {args.directlines_path}")
    return 0


# -- REPL -------------------------------------------------------------------


def _run_repl(args: argparse.Namespace) -> int:
    """Row-numbered REPL with the shared driver. Every mutation re-enters
    ``with_sync`` so the local change is mirrored to Railway atomically."""
    spec = SyncSpec(
        kind="file", remote=REMOTE_DIRECTLINES_PATH, local=args.directlines_path
    )
    no_remote = args.no_remote or args.directlines_path != DEFAULT_DIRECTLINES_PATH

    def _load_rows() -> list[ReplRow]:
        return [
            ReplRow(idx=i + 1, key=e.twilio_did)
            for i, e in enumerate(_store(args.directlines_path).list_all())
        ]

    def _render(_rows: list[ReplRow]) -> None:
        _print_table(list(_store(args.directlines_path).list_all()))

    def _do_show(did: str, _extra: list[str]) -> bool:
        entry = _store(args.directlines_path).get(did)
        if entry is None:
            print(f"  no directline for {did}")
            return False
        print(
            f"{entry.twilio_did}  @{entry.telegram_username}  "
            f"enabled={entry.enabled}  label={entry.label!r}"
        )
        return False

    def _do_add(extra: list[str]) -> bool:
        try:
            parsed = _build_add_parser().parse_args(extra)
        except SystemExit:
            return False
        ns = argparse.Namespace(directlines_path=args.directlines_path, **vars(parsed))
        rc = with_sync(spec, no_remote=no_remote, mutating=True, fn=lambda: cmd_add(ns))
        return rc == 0

    def _do_label(did: str, extra: list[str]) -> bool:
        if not extra:
            print("  usage: label <n> <text>")
            return False
        new_label = " ".join(extra)

        def _apply() -> int:
            store = _store(args.directlines_path)
            existing = store.get(did)
            if existing is None:
                print(f"  no directline for {did}")
                return 0
            entry = store.upsert(did, existing.telegram_username, label=new_label)
            print(f"{entry.twilio_did}: label={entry.label!r}")
            return 0

        rc = with_sync(spec, no_remote=no_remote, mutating=True, fn=_apply)
        return rc == 0

    def _toggle(did: str, extra: list[str], *, on: bool) -> bool:
        def _apply() -> int:
            entry = _store(args.directlines_path).set_enabled(did, on)
            if entry is None:
                print(f"  no directline for {did}")
                return 0
            print(f"{entry.twilio_did}: enabled={entry.enabled}")
            return 0

        rc = with_sync(spec, no_remote=no_remote, mutating=True, fn=_apply)
        return rc == 0

    def _do_remove(did: str, _extra: list[str]) -> bool:
        def _apply() -> int:
            deleted = _store(args.directlines_path).remove(did)
            print(f"removed {did}" if deleted else f"(no directline for {did})")
            return 0

        rc = with_sync(spec, no_remote=no_remote, mutating=True, fn=_apply)
        return rc == 0

    def _do_pull(extra: list[str]) -> bool:
        if extra:
            print("  usage: pull")
            return False
        rc = with_sync(
            spec,
            no_remote=no_remote,
            mutating=False,
            fn=lambda: (
                print(f"pulled {REMOTE_DIRECTLINES_PATH} → {args.directlines_path}")
                or 0
            ),
        )
        return rc == 0

    verbs = [
        Verb("show", needs_row=True, usage="show <n>", handler=_do_show),
        Verb(
            "add",
            needs_row=False,
            usage="add <did> <@username> [--label …]",
            handler=_do_add,
        ),
        Verb("label", needs_row=True, usage="label <n> <text>", handler=_do_label),
        Verb(
            "enable",
            needs_row=True,
            usage="enable <n>",
            handler=lambda did, extra: _toggle(did, extra, on=True),
        ),
        Verb(
            "disable",
            needs_row=True,
            usage="disable <n>",
            handler=lambda did, extra: _toggle(did, extra, on=False),
        ),
        Verb("remove", needs_row=True, usage="remove <n>", handler=_do_remove),
        Verb("pull", needs_row=False, usage="pull", handler=_do_pull),
    ]
    return run_repl(load_rows=_load_rows, render=_render, verbs=verbs)


# -- argparse wiring --------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--directlines-path",
        type=Path,
        default=DEFAULT_DIRECTLINES_PATH,
        help=(
            f"override the directlines JSON path "
            f"(default: {DEFAULT_DIRECTLINES_PATH})"
        ),
    )
    p.add_argument(
        "--no-remote",
        action="store_true",
        help="skip the Railway-volume pull/push and operate against --directlines-path only",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="row-numbered table + REPL")
    p_list.add_argument("--json", action="store_true", help="emit JSON instead")
    p_list.add_argument(
        "--no-interactive",
        action="store_true",
        help="print table and exit; no REPL",
    )
    p_list.set_defaults(func=cmd_list, mutating=False)

    p_show = sub.add_parser("show", help="pretty-print one directline")
    p_show.add_argument("did")
    p_show.add_argument("--json", action="store_true", help="emit JSON instead")
    p_show.set_defaults(func=cmd_show, mutating=False)

    p_add = sub.add_parser(
        "add",
        help="create or update a directline",
        parents=[_build_add_parser()],
    )
    p_add.set_defaults(func=cmd_add, mutating=True)

    p_label = sub.add_parser("label", help="set or replace the human label")
    p_label.add_argument("did")
    p_label.add_argument("label", help="label text; pass an empty string to clear")
    p_label.set_defaults(func=cmd_label, mutating=True)

    p_enable = sub.add_parser("enable", help="mark a directline enabled (call routes via it)")
    p_enable.add_argument("did")
    p_enable.set_defaults(func=cmd_enable, mutating=True)

    p_disable = sub.add_parser(
        "disable", help="mark a directline disabled (call falls through to default the agent)"
    )
    p_disable.add_argument("did")
    p_disable.set_defaults(func=cmd_disable, mutating=True)

    p_remove = sub.add_parser("remove", help="remove a directline entry entirely")
    p_remove.add_argument("did")
    p_remove.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_remove.set_defaults(func=cmd_remove, mutating=True)

    sub.add_parser(
        "pull",
        help="refresh the local directlines JSON from the Railway volume (no mutation)",
    ).set_defaults(func=cmd_pull, mutating=False)

    args = p.parse_args()

    spec = SyncSpec(
        kind="file",
        remote=REMOTE_DIRECTLINES_PATH,
        local=args.directlines_path,
    )
    # Non-default --directlines-path → operator is targeting a sandbox file;
    # skip remote sync regardless of --no-remote (matches callers.py logic).
    no_remote = args.no_remote or args.directlines_path != DEFAULT_DIRECTLINES_PATH
    return with_sync(
        spec,
        no_remote=no_remote,
        mutating=args.mutating,
        fn=lambda: args.func(args),
    )


if __name__ == "__main__":
    raise SystemExit(main())
