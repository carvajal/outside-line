#!/usr/bin/env -S uv run python
"""Operator CLI for the caller registry + allow list + gate-skip list.

Manages ``data/callers.json``: list known callers, label them, toggle
the ``allowed`` flag (must be on for the call to reach the agent at all)
and the ``skip_gate`` flag (skips the DTMF gate), and forget entries.
``list`` prints a row-numbered table and
drops into a small REPL so the operator can act on rows by number.

Usage:
    ./scripts/callers.py list [--json] [--no-interactive]
    ./scripts/callers.py show <phone> [--json]
    ./scripts/callers.py add <phone> [--label "Operator cell"]
    ./scripts/callers.py label <phone> <label>
    ./scripts/callers.py allow <phone> on|off
    ./scripts/callers.py skip-gate <phone> on|off
    ./scripts/callers.py memory show  <phone>
    ./scripts/callers.py memory edit  <phone>
    ./scripts/callers.py memory set   <phone>    (reads stdin)
    ./scripts/callers.py memory clear <phone> [-y]
    ./scripts/callers.py forget <phone> [-y]
    ./scripts/callers.py pull

The ``<phone>`` argument is normalized to E.164 — ``(555) 123-0100``,
``555 123 0100``, and ``+15551230100`` all resolve to the same entry.

The ``list`` command opens a row-numbered REPL where every CLI verb is
available (``add``, ``label``, ``allow/skip-gate``, ``memory``,
``forget``, ``show``, ``pull``). The table re-renders after each
mutation so row indices always reflect the latest data.

By default every command pulls the authoritative ``data/callers.json``
from the Railway volume before running, so reads reflect prod-side
``record_call`` bumps and writes never clobber a fresh remote state.
Pass ``--no-remote`` to skip the round-trip and operate against the
local copy only (offline use). ``pull`` is an explicit remote→local
refresh with no mutation. REPL actions also re-enter the sync wrapper
so each mutation is its own atomic pull→mutate→push.

Memory subcommands sync the ``data/memory/`` directory (one
``<E.164>.md`` per caller) with the same pull→mutate→push pattern as
the registry. Memory files are loaded at the start of every call and
written by the agent's post-call LLM merge — the CLI lets you seed, edit,
or clear them by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALLERS_PATH = REPO_ROOT / "data" / "callers.json"
DEFAULT_MEMORY_DIR = REPO_ROOT / "data" / "memory"
REMOTE_CALLERS_PATH = "/app/data/callers.json"
REMOTE_MEMORY_DIR = "/app/data/memory"

sys.path.insert(0, str(REPO_ROOT / "src"))

# Imported after sys.path tweak so the script runs without `uv pip install -e .`
# (matches the pattern of scripts/contacts.py).
from outside_line.callers import (  # noqa: E402
    CallersStore,
    is_allowed_entry,
    normalize_e164,
    skips_gate_entry,
)

# Sibling-script imports (scripts/ is on sys.path because the file is
# invoked as ./scripts/callers.py).
from _repl import ReplRow, Verb, run_repl  # noqa: E402
from _sync import SyncSpec, with_sync  # noqa: E402


def _require_phone(raw: str) -> str:
    """Normalize ``raw`` to E.164 or exit 2 with a clear error."""
    phone = normalize_e164(raw)
    if phone is None:
        sys.stderr.write(
            f"error: {raw!r} is not a valid phone number "
            f"(expected E.164 like +15551230100 or a 10-digit US number)\n"
        )
        sys.exit(2)
    return phone


def _store(path: Path) -> CallersStore:
    return CallersStore(path)


def _entry_summary(entry: dict[str, Any]) -> str:
    """One-line "allowed=…, skip_gate=…, label=…" for REPL output."""
    return (
        f"allowed={is_allowed_entry(entry)} "
        f"skip_gate={skips_gate_entry(entry)} "
        f"label={entry.get('label')!r}"
    )


# -- table rendering --------------------------------------------------------


def _print_table(rows: list[tuple[str, dict[str, Any]]]) -> None:
    if not rows:
        print("(no callers)")
        return
    print(
        f"{'#':>4}  {'PHONE':<16}  {'ALLOW':<5}  {'SKIP':<4}  "
        f"{'CALLS':>5}  {'LAST_SEEN':<25}  LABEL"
    )
    for i, (phone, entry) in enumerate(rows, start=1):
        allowed = "yes" if is_allowed_entry(entry) else "NO"
        skip = "yes" if skips_gate_entry(entry) else "-"
        calls = int(entry.get("call_count", 0) or 0)
        last_seen = str(entry.get("last_seen") or "-")
        label = entry.get("label") or "-"
        print(
            f"[{i:>2}]  {phone:<16}  {allowed:<5}  {skip:<4}  "
            f"{calls:>5}  {last_seen:<25}  {label}"
        )


# -- subparser builder for `add` (shared by main() and REPL) ----------------


def _build_add_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="add", add_help=False)
    p.add_argument("phone")
    p.add_argument("--label", default=None, help="optional human label")
    return p


# -- commands ---------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    rows = _store(args.callers_path).list_all()
    if args.json:
        out = [
            {"phone": phone, **entry}  # type: ignore[dict-item]
            for phone, entry in rows
        ]
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    _print_table(rows)
    if args.no_interactive or not sys.stdin.isatty():
        return 0
    return _run_repl(args)


def cmd_show(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    entry = _store(args.callers_path).get(phone)
    if entry is None:
        sys.stderr.write(f"error: no entry for {phone}\n")
        return 2
    if args.json:
        print(json.dumps({"phone": phone, **entry}, indent=2, sort_keys=True))
        return 0
    print(f"phone:       {phone}")
    print(f"label:       {entry.get('label') or '-'}")
    print(f"allowed:     {is_allowed_entry(entry)}")
    print(f"skip_gate:   {skips_gate_entry(entry)}")
    print(f"call_count:  {int(entry.get('call_count', 0) or 0)}")
    print(f"first_seen:  {entry.get('first_seen') or '-'}")
    print(f"last_seen:   {entry.get('last_seen') or '-'}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    entry = _store(args.callers_path).add(phone, label=args.label)
    print(f"added {phone} ({_entry_summary(entry)})")
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    entry = _store(args.callers_path).set_label(phone, args.label)
    print(f"{phone}: label={entry.get('label')!r}")
    return 0


def cmd_allow(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    on = args.state == "on"
    entry = _store(args.callers_path).set_allowed(phone, on)
    print(f"{phone}: allowed={is_allowed_entry(entry)}")
    return 0


def cmd_skip_gate(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    on = args.state == "on"
    entry = _store(args.callers_path).set_skip_gate(phone, on)
    print(f"{phone}: skip_gate={skips_gate_entry(entry)}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    if not args.yes:
        try:
            ans = input(f"forget caller {phone}? [y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() != "y":
            print("aborted")
            return 0
    deleted = _store(args.callers_path).forget(phone)
    if deleted:
        print(f"forgot {phone}")
    else:
        print(f"(no entry for {phone})")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    # All the work happens in with_sync's pull step before this runs.
    print(f"pulled {REMOTE_CALLERS_PATH} → {args.callers_path}")
    return 0


# -- memory subcommands ----------------------------------------------------


def _memory_file(args: argparse.Namespace, phone: str) -> Path:
    return Path(args.memory_dir) / f"{phone}.md"


def _editor_command() -> list[str]:
    """Resolve the user's editor, with sensible fallbacks.

    ``$EDITOR`` first (so a user-set ``code -w`` or ``hx`` is honored),
    then ``vim``, then ``nano``. The fallback chain is intentional —
    every Unix box has at least one of these.
    """
    env = (os.environ.get("EDITOR") or "").strip()
    if env:
        return env.split()
    for candidate in ("vim", "nano", "vi"):
        if shutil.which(candidate):
            return [candidate]
    sys.stderr.write(
        "error: no editor found ($EDITOR unset and vim/nano/vi not on PATH)\n"
    )
    sys.exit(2)


def cmd_memory_show(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    path = _memory_file(args, phone)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"(no memory file for {phone} — try `memory edit {phone}`)")
        return 0
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_memory_edit(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    path = _memory_file(args, phone)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Seed an empty file with a header stub so $EDITOR opens with a
    # hint about what shape to write in.
    if not path.exists():
        stub = (
            f"# Memory: {phone}\n\n"
            "<!-- Free-form markdown. Common things to capture:\n"
            "     - Identity / nicknames\n"
            "     - Location\n"
            "     - Family relationships\n"
            "     - Communication style (formal / familiar, language)\n"
            "     - Recurring concerns or topics\n"
            "     - Important dates\n"
            "  -->\n"
        )
        path.write_text(stub, encoding="utf-8")
    cmd = [*_editor_command(), str(path)]
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.stderr.write(f"editor exited rc={rc}\n")
        return rc
    print(f"saved {path}")
    return 0


def cmd_memory_set(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    text = sys.stdin.read()
    if not text.strip():
        sys.stderr.write("error: stdin is empty (pass the memory content via stdin)\n")
        return 2
    path = _memory_file(args, phone)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = text.rstrip() + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    print(f"wrote {len(content)} bytes → {path}")
    return 0


def cmd_memory_clear(args: argparse.Namespace) -> int:
    phone = _require_phone(args.phone)
    path = _memory_file(args, phone)
    if not path.exists():
        print(f"(no memory file for {phone})")
        return 0
    if not args.yes:
        try:
            ans = input(f"delete memory for {phone}? [y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() != "y":
            print("aborted")
            return 0
    path.unlink()
    print(f"deleted {path}")
    return 0


# -- REPL -------------------------------------------------------------------


def _run_repl(args: argparse.Namespace) -> int:
    """Drive the row-numbered REPL with the shared driver.

    Every mutating action re-enters ``with_sync`` so the local change is
    mirrored to Railway atomically (pull → mutate → push). The shared
    driver re-renders the table after each mutating action so row
    indices stay accurate.
    """
    spec = SyncSpec(kind="file", remote=REMOTE_CALLERS_PATH, local=args.callers_path)
    no_remote = args.no_remote or args.callers_path != DEFAULT_CALLERS_PATH

    memory_spec = SyncSpec(
        kind="dir",
        remote=REMOTE_MEMORY_DIR,
        local=args.memory_dir,
        dir_extensions=(".md",),
    )
    no_remote_mem = args.no_remote or args.memory_dir != DEFAULT_MEMORY_DIR

    def _load_rows() -> list[ReplRow]:
        return [
            ReplRow(idx=i + 1, key=phone)
            for i, (phone, _e) in enumerate(_store(args.callers_path).list_all())
        ]

    def _render(_rows: list[ReplRow]) -> None:
        _print_table(_store(args.callers_path).list_all())

    def _do_show(phone: str, _extra: list[str]) -> bool:
        entry = _store(args.callers_path).get(phone)
        if entry is None:
            print(f"  no entry for {phone}")
            return False
        print(f"{phone}  {_entry_summary(entry)}")
        print(
            f"  call_count={int(entry.get('call_count', 0) or 0)}  "
            f"first_seen={entry.get('first_seen')}  "
            f"last_seen={entry.get('last_seen')}"
        )
        return False

    def _do_add(extra: list[str]) -> bool:
        try:
            parsed = _build_add_parser().parse_args(extra)
        except SystemExit:
            return False
        ns = argparse.Namespace(callers_path=args.callers_path, **vars(parsed))
        rc = with_sync(spec, no_remote=no_remote, mutating=True, fn=lambda: cmd_add(ns))
        return rc == 0

    _FIELD_METHODS = {
        "allow": "set_allowed",
        "skip-gate": "set_skip_gate",
    }

    def _toggle(phone: str, extra: list[str], *, field: str) -> bool:
        if not extra or extra[0] not in ("on", "off"):
            print(f"  usage: {field} <n> on|off")
            return False
        on = extra[0] == "on"
        method = _FIELD_METHODS[field]

        def _apply() -> int:
            entry = getattr(_store(args.callers_path), method)(phone, on)
            print(f"{phone}: {_entry_summary(entry)}")
            return 0

        rc = with_sync(spec, no_remote=no_remote, mutating=True, fn=_apply)
        return rc == 0

    def _do_label(phone: str, extra: list[str]) -> bool:
        if not extra:
            print("  usage: label <n> <text>")
            return False
        new_label = " ".join(extra)

        def _apply() -> int:
            entry = _store(args.callers_path).set_label(phone, new_label)
            print(f"{phone}: label={entry.get('label')!r}")
            return 0

        rc = with_sync(spec, no_remote=no_remote, mutating=True, fn=_apply)
        return rc == 0

    def _do_forget(phone: str, _extra: list[str]) -> bool:
        def _apply() -> int:
            deleted = _store(args.callers_path).forget(phone)
            print(f"forgot {phone}" if deleted else f"(no entry for {phone})")
            return 0

        rc = with_sync(spec, no_remote=no_remote, mutating=True, fn=_apply)
        return rc == 0

    def _do_memory(phone: str, extra: list[str]) -> bool:
        """``memory <n>`` shows the file; ``memory <n> edit`` opens it in
        $EDITOR. The mutating edit goes through its own ``with_sync``
        against the memory dir so the round-trip is atomic. Returns
        False (no callers.json re-render needed)."""
        path = Path(args.memory_dir) / f"{phone}.md"
        action = extra[0] if extra else "show"
        if action == "show":
            if not path.exists():
                print(f"  (no memory for {phone} — `memory {phone[-4:]} edit`)")
                return False
            print(path.read_text(encoding="utf-8"), end="")
            return False
        if action == "edit":

            def _apply() -> int:
                ns = argparse.Namespace(phone=phone, memory_dir=args.memory_dir)
                return cmd_memory_edit(ns)

            with_sync(memory_spec, no_remote=no_remote_mem, mutating=True, fn=_apply)
            return False
        print(f"  usage: memory <n> [show|edit]   (got {action!r})")
        return False

    def _do_pull(extra: list[str]) -> bool:
        if extra:
            print("  usage: pull")
            return False
        # with_sync(mutating=False) pulls remote → local without pushing.
        # Return True so the driver re-renders against the fresh data.
        rc = with_sync(
            spec,
            no_remote=no_remote,
            mutating=False,
            fn=lambda: (
                print(f"pulled {REMOTE_CALLERS_PATH} → {args.callers_path}") or 0
            ),
        )
        return rc == 0

    verbs = [
        Verb("show", needs_row=True, usage="show <n>", handler=_do_show),
        Verb("add", needs_row=False, usage="add <phone> [--label …]", handler=_do_add),
        Verb(
            "allow",
            needs_row=True,
            usage="allow <n> on|off",
            handler=lambda phone, extra: _toggle(phone, extra, field="allow"),
        ),
        Verb(
            "skip-gate",
            needs_row=True,
            usage="skip-gate <n> on|off",
            handler=lambda phone, extra: _toggle(phone, extra, field="skip-gate"),
        ),
        Verb("label", needs_row=True, usage="label <n> <text>", handler=_do_label),
        Verb("memory", needs_row=True, usage="memory <n> [edit]", handler=_do_memory),
        Verb("forget", needs_row=True, usage="forget <n>", handler=_do_forget),
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
        "--callers-path",
        type=Path,
        default=DEFAULT_CALLERS_PATH,
        help=f"override the callers JSON path (default: {DEFAULT_CALLERS_PATH})",
    )
    p.add_argument(
        "--memory-dir",
        type=Path,
        default=DEFAULT_MEMORY_DIR,
        help=f"override the memory directory (default: {DEFAULT_MEMORY_DIR})",
    )
    p.add_argument(
        "--no-remote",
        action="store_true",
        help="skip the Railway-volume pull/push and operate against --callers-path only",
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

    p_show = sub.add_parser("show", help="pretty-print one caller's record")
    p_show.add_argument("phone", help="phone number (any common shape)")
    p_show.add_argument("--json", action="store_true", help="emit JSON instead")
    p_show.set_defaults(func=cmd_show, mutating=False)

    p_add = sub.add_parser(
        "add",
        help="create an entry without recording a call (call_count stays 0, allowed=True)",
        parents=[_build_add_parser()],
    )
    p_add.set_defaults(func=cmd_add, mutating=True)

    p_label = sub.add_parser("label", help="set or replace the human label")
    p_label.add_argument("phone")
    p_label.add_argument("label", help="label text; pass an empty string to clear")
    p_label.set_defaults(func=cmd_label, mutating=True)

    p_allow = sub.add_parser(
        "allow",
        help="toggle the allowed flag (off = call gets a busy signal + email alert)",
    )
    p_allow.add_argument("phone")
    p_allow.add_argument("state", choices=("on", "off"))
    p_allow.set_defaults(func=cmd_allow, mutating=True)

    p_skip_gate = sub.add_parser(
        "skip-gate",
        help="toggle the skip_gate flag (on = skip DTMF gate)",
    )
    p_skip_gate.add_argument("phone")
    p_skip_gate.add_argument("state", choices=("on", "off"))
    p_skip_gate.set_defaults(func=cmd_skip_gate, mutating=True)

    p_forget = sub.add_parser("forget", help="remove the entry entirely")
    p_forget.add_argument("phone")
    p_forget.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_forget.set_defaults(func=cmd_forget, mutating=True)

    sub.add_parser(
        "pull",
        help="refresh the local callers.json from the Railway volume (no mutation)",
    ).set_defaults(func=cmd_pull, mutating=False, target="callers")

    # All non-memory subcommands target the callers.json file.
    for parser in (p_list, p_show, p_add, p_label, p_allow, p_skip_gate, p_forget):
        parser.set_defaults(target="callers")

    p_memory = sub.add_parser(
        "memory",
        help="view, edit, set, or clear the per-caller memory markdown file",
    )
    mem_sub = p_memory.add_subparsers(dest="memory_cmd", required=True)

    p_mem_show = mem_sub.add_parser("show", help="print the memory file content")
    p_mem_show.add_argument("phone")
    p_mem_show.set_defaults(func=cmd_memory_show, mutating=False, target="memory")

    p_mem_edit = mem_sub.add_parser("edit", help="open the memory file in $EDITOR")
    p_mem_edit.add_argument("phone")
    p_mem_edit.set_defaults(func=cmd_memory_edit, mutating=True, target="memory")

    p_mem_set = mem_sub.add_parser("set", help="write memory file from stdin")
    p_mem_set.add_argument("phone")
    p_mem_set.set_defaults(func=cmd_memory_set, mutating=True, target="memory")

    p_mem_clear = mem_sub.add_parser("clear", help="delete the memory file")
    p_mem_clear.add_argument("phone")
    p_mem_clear.add_argument(
        "-y", "--yes", action="store_true", help="skip confirmation"
    )
    p_mem_clear.set_defaults(func=cmd_memory_clear, mutating=True, target="memory")

    args = p.parse_args()

    if args.target == "memory":
        spec = SyncSpec(
            kind="dir",
            remote=REMOTE_MEMORY_DIR,
            local=args.memory_dir,
            dir_extensions=(".md",),
        )
        no_remote = args.no_remote or args.memory_dir != DEFAULT_MEMORY_DIR
    else:
        spec = SyncSpec(
            kind="file",
            remote=REMOTE_CALLERS_PATH,
            local=args.callers_path,
        )
        # If the operator overrode --callers-path to a non-default path
        # they're targeting a sandbox file; skip remote sync regardless
        # of --no-remote.
        no_remote = args.no_remote or args.callers_path != DEFAULT_CALLERS_PATH
    return with_sync(
        spec,
        no_remote=no_remote,
        mutating=args.mutating,
        fn=lambda: args.func(args),
    )


if __name__ == "__main__":
    raise SystemExit(main())
