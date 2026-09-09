#!/usr/bin/env -S uv run python
"""Operator CLI for the call/message contact directory.

Manages ``data/contacts.json``: list contacts, add/update/forget entries,
seed aliases + keywords, and on a successful mutation mirror the JSON to
the Railway volume at ``/app/data/contacts.json`` then trigger
``railway redeploy`` so the in-process ``_cached`` contacts refresh.

Resolution is **username-only** (ADR 0011) — every contact carries a
Telegram ``@username`` that ``telegram_bridge.place_call`` /
``send_text`` pass to ``Telethon.get_entity``. Phone-based resolution
was retired because contacts who hide their phone on Telegram aren't
reachable that way.

Usage:
    ./scripts/contacts.py list [--json] [--no-interactive]
    ./scripts/contacts.py show <first_name>
    ./scripts/contacts.py add <first_name> <@username>
        [--alias A]... [--keyword K]...
    ./scripts/contacts.py update <first_name>
        [--username @new] [--alias A]... [--keyword K]...
        [--rename NEW]
    ./scripts/contacts.py forget <first_name> [-y]
    ./scripts/contacts.py sync <first_name>
        # verifies the contact's @username resolves on the container
    ./scripts/contacts.py pull

Top-level flags:
    --no-remote      skip Railway pull/push (offline mode)
    --no-redeploy    skip railway redeploy after a push

The ``list`` command opens a row-numbered REPL where every CLI verb is
available (``add``, ``update``, ``forget``, ``sync``, ``show``, ``pull``).
The table re-renders after each mutation, and a single
``railway redeploy`` fires at REPL exit if any mutation ran — pass
``--no-redeploy`` to skip.

``<first_name>`` is matched accent + case-insensitively against the
canonical ``first_name`` of each contact — same fold rule the Realtime
tools use.

For a new contact *and* a dedicated directline DID (a Twilio number that
routes straight to this contact) in one guided step, use the
``scripts/add_contact.py`` provisioner instead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTACTS_PATH = REPO_ROOT / "data" / "contacts.json"
REMOTE_CONTACTS_PATH = "/app/data/contacts.json"

sys.path.insert(0, str(REPO_ROOT / "src"))

from _railway import railway_available  # noqa: E402
from _repl import ReplRow, Verb, run_repl  # noqa: E402
from _sync import SyncSpec, with_sync  # noqa: E402
from _telegram_resolve import (  # noqa: E402
    TelegramResolveError,
    railway_redeploy,
    resolve_username,
)


@dataclass
class _ContactRow:
    first_name: str
    telegram_username: str
    aliases: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


# Telegram usernames: 5–32 chars, must start with a letter, then letters /
# digits / underscores. We require the leading ``@`` so operators can
# paste handles straight from a Telegram profile.
_USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")


# -- store helpers ----------------------------------------------------------


def _fold(s: str) -> str:
    return (
        unicodedata.normalize("NFKD", s)
        .encode("ascii", "ignore")
        .decode()
        .casefold()
        .strip()
    )


def _validate_username(handle: str) -> str | None:
    """Return None if ``handle`` is a valid ``@username``, else an error string."""
    if not handle:
        return "username is empty"
    if not handle.startswith("@"):
        return f"username must start with '@' (got {handle!r})"
    if not _USERNAME_RE.match(handle):
        return (
            f"{handle!r} is not a valid Telegram username "
            "(5–32 chars, letter first, then letters/digits/underscores)"
        )
    return None


def _load(path: Path) -> list[_ContactRow]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[_ContactRow] = []
    for entry in raw.get("contacts", []):
        out.append(
            _ContactRow(
                first_name=entry["first_name"],
                telegram_username=entry.get("telegram_username") or "",
                aliases=list(entry.get("aliases", [])),
                keywords=list(entry.get("keywords", [])),
            )
        )
    return out


def _save(path: Path, rows: list[_ContactRow]) -> None:
    payload = {"contacts": [asdict(r) for r in rows]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def _find_idx(rows: list[_ContactRow], first_name: str) -> int | None:
    needle = _fold(first_name)
    if not needle:
        return None
    for i, r in enumerate(rows):
        if _fold(r.first_name) == needle:
            return i
    return None


def _require_existing(rows: list[_ContactRow], first_name: str) -> int:
    idx = _find_idx(rows, first_name)
    if idx is None:
        sys.stderr.write(f"error: no contact named {first_name!r}\n")
        sys.exit(2)
    return idx


# -- post-mutation redeploy -------------------------------------------------


def _maybe_redeploy(args: argparse.Namespace) -> None:
    """Trigger ``railway redeploy`` unless opted out.

    The FastAPI process caches contacts at module level
    (``outside_line.contacts._cached``); a redeploy is how the live process
    picks up our changes. In REPL mode each action sets
    ``args.no_redeploy=True`` so the driver's ``on_exit`` fires one
    batched redeploy at session exit instead of one per mutation.
    """
    if args.no_redeploy:
        return
    if not railway_available():
        sys.stderr.write("[railway redeploy skipped] railway CLI unavailable\n")
        return
    rc = railway_redeploy()
    if rc == 0:
        print("  railway: redeploy triggered")
    else:
        sys.stderr.write(
            f"[railway redeploy failed] exit code {rc}\n"
            f"  retry by hand: railway redeploy --yes\n"
        )


# -- table rendering --------------------------------------------------------


def _print_table(rows: list[_ContactRow]) -> None:
    if not rows:
        print("(no contacts)")
        return
    print(
        f"{'#':>4}  {'FIRST_NAME':<14}  {'TG_USERNAME':<22}  "
        f"{'ALIASES':<22}  KEYWORDS"
    )
    for i, r in enumerate(rows, start=1):
        aliases = ",".join(r.aliases) or "-"
        keywords = ",".join(r.keywords) or "-"
        print(
            f"[{i:>2}]  {r.first_name:<14}  {r.telegram_username:<22}  "
            f"{aliases:<22}  {keywords}"
        )


# -- subparser builders (shared by main() and REPL) -------------------------


def _build_add_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="add", add_help=False)
    p.add_argument("first_name")
    p.add_argument("username", help="Telegram @handle, e.g. @some_handle")
    p.add_argument("--alias", action="append", help="add an alias (repeatable)")
    p.add_argument("--keyword", action="append", help="add a keyword (repeatable)")
    return p


def _build_update_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="update", add_help=False)
    p.add_argument("first_name")
    p.add_argument(
        "--username", default=None, help="new Telegram @handle"
    )
    p.add_argument(
        "--alias",
        action="append",
        default=None,
        help="replace the aliases list (repeatable; pass once per alias)",
    )
    p.add_argument(
        "--keyword",
        action="append",
        default=None,
        help="replace the keywords list (repeatable; pass once per keyword)",
    )
    p.add_argument(
        "--rename",
        default=None,
        help="rename the contact (the first_name becomes this value)",
    )
    return p


def _build_show_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="show", add_help=False)
    p.add_argument("first_name")
    return p


def _build_forget_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forget", add_help=False)
    p.add_argument("first_name")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    return p


def _build_sync_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sync", add_help=False)
    p.add_argument("first_name")
    return p


# -- commands ---------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    rows = _load(args.contacts_path)
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False))
        return 0
    _print_table(rows)
    if args.no_interactive or not sys.stdin.isatty():
        return 0
    return _run_repl(args)


def cmd_show(args: argparse.Namespace) -> int:
    rows = _load(args.contacts_path)
    idx = _require_existing(rows, args.first_name)
    r = rows[idx]
    print(f"first_name:        {r.first_name}")
    print(f"telegram_username: {r.telegram_username}")
    print(f"aliases:           {', '.join(r.aliases) or '-'}")
    print(f"keywords:          {', '.join(r.keywords) or '-'}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    rows = _load(args.contacts_path)
    if _find_idx(rows, args.first_name) is not None:
        sys.stderr.write(
            f"error: contact {args.first_name!r} already exists "
            f"(use `update` to change it)\n"
        )
        return 2
    err = _validate_username(args.username)
    if err is not None:
        sys.stderr.write(f"error: {err}\n")
        return 2
    row = _ContactRow(
        first_name=args.first_name,
        telegram_username=args.username,
        aliases=list(dict.fromkeys(args.alias or [])),
        keywords=list(dict.fromkeys(args.keyword or [])),
    )
    rows.append(row)
    _save(args.contacts_path, rows)
    print(f"added {row.first_name} ({row.telegram_username})")
    _maybe_redeploy(args)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    rows = _load(args.contacts_path)
    idx = _require_existing(rows, args.first_name)
    current = rows[idx]
    changes: dict[str, Any] = {}
    if args.rename is not None:
        if _find_idx(rows, args.rename) not in (None, idx):
            sys.stderr.write(
                f"error: rename target {args.rename!r} collides with another contact\n"
            )
            return 2
        changes["first_name"] = args.rename
    if args.username is not None:
        err = _validate_username(args.username)
        if err is not None:
            sys.stderr.write(f"error: {err}\n")
            return 2
        changes["telegram_username"] = args.username
    if args.alias is not None:
        changes["aliases"] = list(dict.fromkeys(args.alias))
    if args.keyword is not None:
        changes["keywords"] = list(dict.fromkeys(args.keyword))

    if not changes:
        sys.stderr.write("error: nothing to update (pass at least one --flag)\n")
        return 2

    updated = replace(current, **changes)
    rows[idx] = updated
    _save(args.contacts_path, rows)
    print(f"updated {updated.first_name}: {', '.join(sorted(changes))}")
    _maybe_redeploy(args)
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    rows = _load(args.contacts_path)
    idx = _require_existing(rows, args.first_name)
    row = rows[idx]
    if not args.yes:
        try:
            ans = input(f"forget contact {row.first_name}? [y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() != "y":
            print("aborted")
            return 0
    rows.pop(idx)
    _save(args.contacts_path, rows)
    print(f"forgot {row.first_name}")
    # Redeploy happens at top-level CLI exit or batches at REPL exit.
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Verify the contact's ``@username`` resolves on the container.

    Runs ``Telethon.get_entity(handle)`` against the agent's session on
    Railway and prints the resolved user_id. Pure probe — never mutates
    the contacts JSON, never redeploys.
    """
    rows = _load(args.contacts_path)
    idx = _require_existing(rows, args.first_name)
    row = rows[idx]
    if not railway_available():
        sys.stderr.write("error: railway CLI unavailable; cannot resolve\n")
        return 1
    try:
        resolved = resolve_username(row.telegram_username)
    except TelegramResolveError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    user_id = resolved.get("user_id")
    username = resolved.get("username")
    first_name = resolved.get("first_name") or ""
    print(
        f"resolved {row.telegram_username} → user_id={user_id} "
        f"(username={username!r} first_name={first_name!r})"
    )
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    print(f"pulled {REMOTE_CONTACTS_PATH} → {args.contacts_path}")
    return 0


# -- REPL -------------------------------------------------------------------


def _run_repl(args: argparse.Namespace) -> int:
    spec = SyncSpec(
        kind="file", remote=REMOTE_CONTACTS_PATH, local=args.contacts_path
    )
    no_remote = args.no_remote or args.contacts_path != DEFAULT_CONTACTS_PATH

    def _load_rows() -> list[ReplRow]:
        return [
            ReplRow(idx=i + 1, key=r.first_name)
            for i, r in enumerate(_load(args.contacts_path))
        ]

    def _render(_rows: list[ReplRow]) -> None:
        _print_table(_load(args.contacts_path))

    def _ns_for(extra: dict[str, Any]) -> argparse.Namespace:
        """Build a Namespace combining session flags + per-verb parsed args.

        ``no_redeploy=True`` is forced so the per-action ``_maybe_redeploy``
        skips redeploy; the driver's ``on_exit`` fires one batched
        redeploy at REPL exit.
        """
        return argparse.Namespace(
            contacts_path=args.contacts_path,
            no_remote=args.no_remote,
            no_redeploy=True,
            **extra,
        )

    def _parse(parser: argparse.ArgumentParser, tokens: list[str]) -> argparse.Namespace | None:
        try:
            return parser.parse_args(tokens)
        except SystemExit:
            return None

    def _do_show(first_name: str, extra: list[str]) -> bool:
        parsed = _parse(_build_show_parser(), [first_name, *extra])
        if parsed is None:
            return False
        cmd_show(_ns_for(vars(parsed)))
        return False

    def _do_add(extra: list[str]) -> bool:
        parsed = _parse(_build_add_parser(), extra)
        if parsed is None:
            return False
        rc = with_sync(
            spec,
            no_remote=no_remote,
            mutating=True,
            fn=lambda: cmd_add(_ns_for(vars(parsed))),
        )
        return rc == 0

    def _do_update(first_name: str, extra: list[str]) -> bool:
        parsed = _parse(_build_update_parser(), [first_name, *extra])
        if parsed is None:
            return False
        rc = with_sync(
            spec,
            no_remote=no_remote,
            mutating=True,
            fn=lambda: cmd_update(_ns_for(vars(parsed))),
        )
        return rc == 0

    def _do_forget(first_name: str, extra: list[str]) -> bool:
        # REPL forget auto-confirms — the operator picked the row deliberately.
        parsed = _parse(_build_forget_parser(), [first_name, "-y", *extra])
        if parsed is None:
            return False
        rc = with_sync(
            spec,
            no_remote=no_remote,
            mutating=True,
            fn=lambda: cmd_forget(_ns_for(vars(parsed))),
        )
        return rc == 0

    def _do_sync(first_name: str, extra: list[str]) -> bool:
        parsed = _parse(_build_sync_parser(), [first_name, *extra])
        if parsed is None:
            return False
        # Probe-only; never mutates state.
        cmd_sync(_ns_for(vars(parsed)))
        return False

    def _do_pull(extra: list[str]) -> bool:
        if extra:
            print("  usage: pull")
            return False
        rc = with_sync(
            spec,
            no_remote=no_remote,
            mutating=False,
            fn=lambda: cmd_pull(_ns_for({})),
        )
        return rc == 0

    def _on_exit(any_mutation: bool) -> None:
        if not any_mutation:
            return
        if args.no_redeploy:
            return
        if not railway_available():
            sys.stderr.write(
                "[railway redeploy skipped] railway CLI unavailable\n"
            )
            return
        rc = railway_redeploy()
        if rc == 0:
            print("railway: redeploy triggered (batched at REPL exit)")
        else:
            sys.stderr.write(
                f"[railway redeploy failed] exit code {rc}\n"
                f"  retry by hand: railway redeploy --yes\n"
            )

    verbs = [
        Verb("show", needs_row=True, usage="show <n>", handler=_do_show),
        Verb(
            "add",
            needs_row=False,
            usage="add <name> <@user> [flags]",
            handler=_do_add,
        ),
        Verb(
            "update",
            needs_row=True,
            usage="update <n> [flags]",
            handler=_do_update,
        ),
        Verb("forget", needs_row=True, usage="forget <n>", handler=_do_forget),
        Verb("sync", needs_row=True, usage="sync <n>", handler=_do_sync),
        Verb("pull", needs_row=False, usage="pull", handler=_do_pull),
    ]
    return run_repl(
        load_rows=_load_rows,
        render=_render,
        verbs=verbs,
        on_exit=_on_exit,
    )


# -- argparse wiring --------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--contacts-path",
        type=Path,
        default=DEFAULT_CONTACTS_PATH,
        help=f"override the contacts JSON path (default: {DEFAULT_CONTACTS_PATH})",
    )
    p.add_argument(
        "--no-remote",
        action="store_true",
        help="skip Railway pull/push and operate against --contacts-path only",
    )
    p.add_argument(
        "--no-redeploy",
        action="store_true",
        help="skip `railway redeploy` after a mutation",
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

    sub.add_parser(
        "show", help="pretty-print one contact's record", parents=[_build_show_parser()]
    ).set_defaults(func=cmd_show, mutating=False)

    sub.add_parser(
        "add", help="insert a new contact", parents=[_build_add_parser()]
    ).set_defaults(func=cmd_add, mutating=True)

    sub.add_parser(
        "update",
        help="mutate an existing contact",
        parents=[_build_update_parser()],
    ).set_defaults(func=cmd_update, mutating=True)

    sub.add_parser(
        "forget", help="remove a contact entirely", parents=[_build_forget_parser()]
    ).set_defaults(func=cmd_forget, mutating=True)

    sub.add_parser(
        "sync",
        help="verify the contact's @username resolves on the container",
        parents=[_build_sync_parser()],
    ).set_defaults(func=cmd_sync, mutating=False)

    sub.add_parser(
        "pull",
        help="refresh local contacts.json from Railway (no mutation)",
    ).set_defaults(func=cmd_pull, mutating=False)

    args = p.parse_args()

    spec = SyncSpec(
        kind="file",
        remote=REMOTE_CONTACTS_PATH,
        local=args.contacts_path,
    )
    no_remote = args.no_remote or args.contacts_path != DEFAULT_CONTACTS_PATH
    return with_sync(
        spec,
        no_remote=no_remote,
        mutating=args.mutating,
        fn=lambda: args.func(args),
    )


if __name__ == "__main__":
    raise SystemExit(main())
