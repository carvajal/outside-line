#!/usr/bin/env -S uv run python
"""Browse and search call transcripts captured from OpenAI Realtime events.

Transcripts are written by ``outside_line.transcripts.TranscriptWriter`` to
``data/transcripts/<call_sid>.jsonl`` during every inbound call. This CLI
is the operator-facing way to scan them.

Usage:
    ./scripts/transcripts.py list [--limit N]
    ./scripts/transcripts.py show <call_sid> [--compact]
    ./scripts/transcripts.py search <query> [--limit N] [--role caller|agent|tool|system]

Examples:
    ./scripts/transcripts.py list
    ./scripts/transcripts.py show CAabcdef1234
    ./scripts/transcripts.py search weather --role caller
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS_DIR = REPO_ROOT / "data" / "transcripts"

ROLE_LABEL = {
    "caller": "caller",
    "agent": "agent ",
    "tool": "tool  ",
    "system": "system",
}
ROLE_COMPACT = {"caller": "CL", "agent": "AG", "tool": "TL", "system": "SY"}


def _iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSONL rows from a transcript file, skipping malformed lines."""
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        sys.stderr.write(f"warning: could not read {path}: {exc}\n")


def _resolve_path(sid_or_path: str) -> Path:
    """Accept a bare call SID, a `<sid>.jsonl` name, or a full path."""
    p = Path(sid_or_path)
    if p.exists():
        return p
    candidate = TRANSCRIPTS_DIR / sid_or_path
    if candidate.exists():
        return candidate
    candidate = TRANSCRIPTS_DIR / f"{sid_or_path}.jsonl"
    if candidate.exists():
        return candidate
    sys.stderr.write(
        f"error: no transcript found for {sid_or_path!r} under {TRANSCRIPTS_DIR}\n"
    )
    sys.exit(1)


def _fmt_ts(ts: str, *, time_only: bool = False) -> str:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    return dt.strftime("%H:%M:%S") if time_only else dt.strftime("%Y-%m-%d %H:%M:%S")


def cmd_list(args: argparse.Namespace) -> int:
    if not TRANSCRIPTS_DIR.exists():
        print(f"(no transcripts under {TRANSCRIPTS_DIR})")
        return 0
    files = sorted(
        TRANSCRIPTS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: args.limit]
    if not files:
        print("(no transcripts)")
        return 0
    print(f"{'CALL_SID':<36}  {'DATE':<19}  {'TURNS':>5}  PEEK")
    for path in files:
        rows = list(_iter_rows(path))
        turns = len(rows)
        first_caller = next(
            (r.get("text", "") for r in rows if r.get("role") == "caller"),
            "",
        )
        peek = (first_caller or "").replace("\n", " ").strip()
        if len(peek) > 60:
            peek = peek[:57] + "..."
        date_str = (
            _fmt_ts(rows[0].get("ts", ""))
            if rows
            else _fmt_ts(datetime.fromtimestamp(path.stat().st_mtime).isoformat())
        )
        call_sid = path.stem
        print(f"{call_sid:<36}  {date_str:<19}  {turns:>5}  {peek}")
    return 0


def _render_row(row: dict[str, Any], *, compact: bool) -> str:
    role = row.get("role", "?")
    text = (row.get("text") or "").replace("\n", " ").strip()
    ts = _fmt_ts(row.get("ts", ""), time_only=True)
    label_map = ROLE_COMPACT if compact else ROLE_LABEL
    label = label_map.get(role, role[:2].upper() if compact else role)
    return f"[{ts}]  {label}  {text}"


def cmd_show(args: argparse.Namespace) -> int:
    path = _resolve_path(args.call_sid)
    rows = list(_iter_rows(path))
    if not rows:
        print(f"(empty transcript: {path})")
        return 0
    print(f"# {path.stem}  ({len(rows)} rows, {_fmt_ts(rows[0].get('ts', ''))})")
    for row in rows:
        print(_render_row(row, compact=args.compact))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if not TRANSCRIPTS_DIR.exists():
        sys.stderr.write(f"error: {TRANSCRIPTS_DIR} does not exist yet\n")
        return 1
    needle = args.query.lower()
    role_filter = set(args.role) if args.role else None
    hits: list[tuple[str, str, str, str, str]] = []
    # newest files first so the most recent hits print last and are
    # closest to the operator's cursor when scrolling.
    files = sorted(
        TRANSCRIPTS_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    for path in files:
        for row in _iter_rows(path):
            if role_filter and row.get("role") not in role_filter:
                continue
            text = row.get("text") or ""
            if needle in text.lower():
                hits.append(
                    (
                        _fmt_ts(row.get("ts", "")),
                        path.stem,
                        row.get("role", "?"),
                        text.replace("\n", " ").strip(),
                        path.stem,
                    )
                )
                if len(hits) >= args.limit:
                    break
        if len(hits) >= args.limit:
            break
    if not hits:
        print(f"(no matches for {args.query!r})")
        return 0
    for ts, call_sid, role, text, _ in hits:
        text_trunc = text if len(text) <= 100 else text[:97] + "..."
        print(f"{ts}  {call_sid:<36}  {role:<6}  {text_trunc}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list recent transcripts (newest first)")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="render a single call's transcript")
    p_show.add_argument("call_sid", help="call SID (CA…) or path to a .jsonl file")
    p_show.add_argument("--compact", action="store_true", help="two-letter role tags")
    p_show.set_defaults(func=cmd_show)

    p_search = sub.add_parser("search", help="grep across all transcripts")
    p_search.add_argument("query", help="case-insensitive substring")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument(
        "--role",
        action="append",
        choices=["caller", "agent", "tool", "system"],
        help="restrict to one or more roles (repeatable)",
    )
    p_search.set_defaults(func=cmd_search)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
