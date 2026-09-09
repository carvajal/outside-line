#!/usr/bin/env -S uv run python
"""Pull and browse the per-call archive.

Three sources, one local mirror:

  * Twilio  -- canonical ledger of calls that hit the DID, plus recordings
  * Railway -- per-call transcripts (written by the FastAPI app)
  * local   -- ``data/transcripts/``, ``data/recordings/``

``pull`` lists Twilio's recent calls (the ledger), downloads any missing
transcript from Railway via ``railway ssh``, and fetches any missing
recording from Twilio.

``list`` joins Twilio's call list with the local archive and drops into a
small REPL so you can jump to ``show``/``play`` by row number, or refresh
with ``pull`` from inside the REPL (re-fetches and re-renders).

Usage:
    ./scripts/archive.py pull [--since 7d|24h|2026-05-20]
    ./scripts/archive.py list [--since 7d] [--no-interactive]
    ./scripts/archive.py show <call_sid> [--compact]
    ./scripts/archive.py play <call_sid>
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from _dotenv import ENV_FILE, env as _env
from _railway import RAILWAY_DATA, railway_cat, railway_list
from _repl import ReplRow, Verb, run_repl

# Reuse helpers from the sibling scripts (scripts/ is on sys.path because we
# were invoked as ./scripts/archive.py).
from transcripts import _fmt_ts, _iter_rows, _render_row  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS_DIR = REPO_ROOT / "data" / "transcripts"
RECORDINGS_DIR = REPO_ROOT / "data" / "recordings"

TWILIO_API = "https://api.twilio.com/2010-04-01"

REQUIRED_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET")


# -- env / creds -----------------------------------------------------------


def _twilio_creds() -> tuple[str, tuple[str, str]]:
    env = _env()
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    if missing:
        sys.stderr.write(f"error: missing {', '.join(missing)} (check {ENV_FILE})\n")
        sys.exit(2)
    return env["TWILIO_ACCOUNT_SID"], (env["TWILIO_API_KEY_SID"], env["TWILIO_API_KEY_SECRET"])


# -- --since parser --------------------------------------------------------


_DUR_RE = re.compile(r"^(\d+)([smhd])$")
_DUR_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(raw: str | None) -> datetime | None:
    """Accept a duration suffix (``7d``, ``24h``, ``30m``, ``90s``) or an
    ISO date / datetime (``2026-05-20``, ``2026-05-20T12:00:00``).
    Returns a tz-aware UTC datetime, or None if ``raw`` is None.
    """
    if not raw:
        return None
    m = _DUR_RE.match(raw)
    if m:
        n, unit = m.groups()
        return datetime.now(timezone.utc) - timedelta(seconds=int(n) * _DUR_MULT[unit])
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        sys.stderr.write(
            f"error: --since must be a duration (7d, 24h, 30m, 90s) or ISO date "
            f"(2026-05-20[T12:00:00]); got {raw!r}\n"
        )
        sys.exit(2)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# -- Twilio: call ledger ---------------------------------------------------


def fetch_twilio_calls(since: datetime | None, page_size: int = 100) -> list[dict[str, Any]]:
    """Return every Twilio call newer than ``since`` (paginated)."""
    account_sid, auth = _twilio_creds()
    params: dict[str, str] = {"PageSize": str(page_size)}
    if since:
        params["StartTime>"] = since.strftime("%Y-%m-%d")
    url: str | None = f"{TWILIO_API}/Accounts/{account_sid}/Calls.json"
    calls: list[dict[str, Any]] = []
    first = True
    while url:
        resp = httpx.get(
            url,
            params=(params if first else None),
            auth=auth,
            timeout=30.0,
        )
        first = False
        if resp.status_code >= 400:
            sys.stderr.write(f"error: Twilio HTTP {resp.status_code}: {resp.text[:300]}\n")
            sys.exit(1)
        body = resp.json()
        calls.extend(body.get("calls") or [])
        next_uri = body.get("next_page_uri")
        url = f"https://api.twilio.com{next_uri}" if next_uri else None
    # Twilio's StartTime> filter is day-granular; tighten to the exact since
    # if the caller passed an hours/minutes duration.
    if since:
        out = []
        for c in calls:
            st = _parse_twilio_ts(c.get("start_time"))
            if st and st >= since:
                out.append(c)
        return out
    return calls


def _parse_twilio_ts(s: str | None) -> datetime | None:
    """Twilio start_time is RFC 2822 (e.g. 'Mon, 01 Jun 2026 22:16:29 +0000')."""
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


# -- pull subcommand -------------------------------------------------------


def cmd_pull(args: argparse.Namespace) -> int:
    since = parse_since(args.since)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    print("fetching Twilio call ledger" + (f" since {since.isoformat()}" if since else "") + "…")
    calls = fetch_twilio_calls(since)
    print(f"  {len(calls)} calls in window")

    # -- transcripts: railway -> local --
    remote_transcripts = set(railway_list(f"{RAILWAY_DATA}/transcripts"))
    tx_fetched = tx_local = tx_missing_on_railway = 0
    for c in calls:
        sid = c.get("sid")
        if not sid:
            continue
        fname = f"{sid}.jsonl"
        local = TRANSCRIPTS_DIR / fname
        if local.exists():
            tx_local += 1
            continue
        if fname not in remote_transcripts:
            tx_missing_on_railway += 1
            continue
        content = railway_cat(f"{RAILWAY_DATA}/transcripts/{fname}")
        if content is None:
            tx_missing_on_railway += 1
            continue
        local.write_text(content, encoding="utf-8")
        tx_fetched += 1

    # -- recordings: twilio -> local --
    account_sid, auth = _twilio_creds()
    rec_fetched = rec_local = rec_none = rec_failed = 0
    for c in calls:
        sid = c.get("sid")
        if not sid:
            continue
        local = RECORDINGS_DIR / f"{sid}.wav"
        if local.exists():
            rec_local += 1
            continue
        try:
            rec_sid = _resolve_recording_sid_silent(account_sid, auth, sid)
        except _NoRecording:
            rec_none += 1
            continue
        except Exception as exc:
            sys.stderr.write(f"  warn: recording resolve failed for {sid}: {exc}\n")
            rec_failed += 1
            continue
        try:
            _download_recording(account_sid, auth, rec_sid, local)
            rec_fetched += 1
        except Exception as exc:
            sys.stderr.write(f"  warn: recording download failed for {sid}: {exc}\n")
            rec_failed += 1

    print()
    print(f"pull complete: {len(calls)} calls in window")
    print(
        f"  transcripts: {tx_fetched} fetched, {tx_local} already local, "
        f"{tx_missing_on_railway} missing on Railway too"
    )
    print(
        f"  recordings:  {rec_fetched} fetched, {rec_local} already local, "
        f"{rec_none} had no recording, {rec_failed} failed"
    )
    return 0


# -- recording fetch helpers (silent versions of recordings.py logic) -----


class _NoRecording(Exception):
    pass


def _resolve_recording_sid_silent(account_sid: str, auth: tuple[str, str], call_sid: str) -> str:
    url = f"{TWILIO_API}/Accounts/{account_sid}/Calls/{call_sid}/Recordings.json"
    resp = httpx.get(url, auth=auth, timeout=15.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    recordings = resp.json().get("recordings") or []
    if not recordings:
        raise _NoRecording()
    return recordings[0]["sid"]


def _download_recording(account_sid: str, auth: tuple[str, str], recording_sid: str, out: Path) -> None:
    url = f"{TWILIO_API}/Accounts/{account_sid}/Recordings/{recording_sid}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, auth=auth, timeout=60.0) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        with out.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                f.write(chunk)


# -- list subcommand -------------------------------------------------------


def _row_render(idx: int, c: dict[str, Any]) -> dict[str, Any]:
    sid = c.get("sid", "")
    transcript_path = TRANSCRIPTS_DIR / f"{sid}.jsonl"
    if transcript_path.exists():
        with transcript_path.open("r", encoding="utf-8") as f:
            txt = f"txt={sum(1 for line in f if line.strip())}"
    else:
        txt = "txt=—"
    rec_ok = (RECORDINGS_DIR / f"{sid}.wav").exists()
    duration = c.get("duration") or "0"
    start = _parse_twilio_ts(c.get("start_time"))
    start_str = start.astimezone().strftime("%Y-%m-%d %H:%M") if start else "?"
    from_ = c.get("from_formatted") or c.get("from") or "?"
    return {
        "idx": idx,
        "sid": sid,
        "from": from_,
        "duration": f"{duration}s",
        "txt": txt,
        "rec": "rec✓" if rec_ok else "rec✗",
        "when": start_str,
    }


def _print_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no calls in window)")
        return
    print(
        f"{'#':>4}  {'CALL_SID':<16}  {'FROM':<18}  "
        f"{'DUR':>5}  {'TXT':<8}  {'REC':<5}  WHEN"
    )
    for r in rows:
        sid_short = (r["sid"][:14] + "…") if len(r["sid"]) > 15 else r["sid"]
        print(
            f"[{r['idx']:>2}]  {sid_short:<16}  {r['from']:<18}  "
            f"{r['duration']:>5}  {r['txt']:<8}  {r['rec']:<5}  "
            f"{r['when']}"
        )


def _fetch_rows(since: datetime | None) -> list[dict[str, Any]]:
    """Twilio fetch → sort → render rows. Slow (hits the API); used by
    cmd_list and the REPL pull verb."""
    calls = fetch_twilio_calls(since)
    calls.sort(
        key=lambda c: _parse_twilio_ts(c.get("start_time"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return [_row_render(i + 1, c) for i, c in enumerate(calls)]


def cmd_list(args: argparse.Namespace) -> int:
    since = parse_since(args.since)
    rows = _fetch_rows(since)
    _print_table(rows)
    if args.no_interactive or not sys.stdin.isatty() or not rows:
        return 0
    return _repl(rows, since=since, args=args)


def _repl(initial_rows: list[dict[str, Any]], *, since: datetime | None, args: argparse.Namespace) -> int:
    """Drive the row-numbered REPL.

    The Twilio fetch is expensive (HTTP, paginated), so we cache the row
    list and only re-fetch on ``pull``. ``_load_rows`` reads from the
    cache; ``_do_pull`` runs ``cmd_pull`` (downloads everything) and
    refreshes the cache so the post-pull re-render reflects new rec✓
    flags and txt counts.
    """
    cache: list[dict[str, Any]] = list(initial_rows)

    def _load_rows() -> list[ReplRow]:
        return [ReplRow(idx=r["idx"], key=r["sid"]) for r in cache]

    def _render(_rows: list[ReplRow]) -> None:
        _print_table(cache)

    def _do_show(sid: str, _extra: list[str]) -> bool:
        _do_show_impl(sid, compact=False)
        return False

    def _do_play(sid: str, _extra: list[str]) -> bool:
        _do_play_impl(sid)
        return False

    def _do_pull(extra: list[str]) -> bool:
        if extra:
            print("  usage: pull")
            return False
        # Run the full archive pull (transcripts + recordings) then
        # refresh the row cache so the post-render reflects the new
        # local state. Reuses the outer --since.
        cmd_pull(argparse.Namespace(since=args.since))
        cache[:] = _fetch_rows(since)
        return True

    verbs = [
        Verb("show", needs_row=True, usage="show <n>", handler=_do_show),
        Verb("play", needs_row=True, usage="play <n>", handler=_do_play),
        Verb("pull", needs_row=False, usage="pull", handler=_do_pull),
    ]
    return run_repl(load_rows=_load_rows, render=_render, verbs=verbs)


# -- show / play ------------------------------------------------------------


def _page(text: str) -> None:
    """Show text via $PAGER (or less); fall back to stdout if no tty / no pager."""
    if not text.endswith("\n"):
        text += "\n"
    if not sys.stdout.isatty():
        sys.stdout.write(text)
        return
    pager = os.environ.get("PAGER")
    if pager:
        cmd: list[str] | str = pager
        shell = True
    elif shutil.which("less"):
        # -R passes ANSI through, -F exits if one screen fits,
        # -X keeps output in scrollback instead of using the alt screen.
        cmd = ["less", "-R", "-F", "-X"]
        shell = False
    else:
        sys.stdout.write(text)
        return
    try:
        subprocess.run(cmd, input=text, text=True, check=False, shell=shell)
    except (BrokenPipeError, KeyboardInterrupt):
        pass


def _do_show_impl(call_sid: str, *, compact: bool) -> None:
    path = TRANSCRIPTS_DIR / f"{call_sid}.jsonl"
    if not path.exists():
        print(f"  no local transcript: {path}")
        return
    rows = list(_iter_rows(path))
    if not rows:
        print(f"  (empty transcript: {path})")
        return
    header = f"# {call_sid}  ({len(rows)} rows, {_fmt_ts(rows[0].get('ts', ''))})"
    body = "\n".join(_render_row(row, compact=compact) for row in rows)
    _page(header + "\n" + body + "\n")


def _do_play_impl(call_sid: str) -> None:
    path = RECORDINGS_DIR / f"{call_sid}.wav"
    if not path.exists():
        print(f"  no local recording: {path} (run `archive pull` first?)")
        return
    print(f"  playing {path}  (Ctrl-C to stop)")
    try:
        # afplay on macOS; otherwise hand off to the platform opener.
        for player in ("afplay", "open", "xdg-open"):
            try:
                subprocess.run([player, str(path)], check=False)
                break
            except FileNotFoundError:
                continue
    except KeyboardInterrupt:
        print()


def cmd_show(args: argparse.Namespace) -> int:
    _do_show_impl(args.call_sid, compact=args.compact)
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    _do_play_impl(args.call_sid)
    return 0


# -- main ------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_pull = sub.add_parser("pull", help="mirror missing transcripts/recordings into local")
    p_pull.add_argument("--since", help="duration (7d, 24h, 30m, 90s) or ISO date")
    p_pull.set_defaults(func=cmd_pull)

    p_list = sub.add_parser("list", help="show recent calls + REPL")
    p_list.add_argument("--since", help="duration or ISO date")
    p_list.add_argument("--no-interactive", action="store_true",
                        help="print table and exit; no REPL")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="print one call's transcript")
    p_show.add_argument("call_sid")
    p_show.add_argument("--compact", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_play = sub.add_parser("play", help="play one call's recording (afplay/open)")
    p_play.add_argument("call_sid")
    p_play.set_defaults(func=cmd_play)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
