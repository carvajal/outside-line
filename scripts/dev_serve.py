#!/usr/bin/env python3
"""Run the dev uvicorn server in the background under a supervisor.

The public CLI is unchanged from the pre-supervisor shape:

    ./scripts/dev_serve.py            # start (or restart) on port 8000
    ./scripts/dev_serve.py --stop     # stop
    ./scripts/dev_serve.py --tail     # follow the log (Ctrl-C to exit)
    ./scripts/dev_serve.py --status   # is it running? log size? respawns?

What's new is the layer between operator and uvicorn:

* The entry script spawns a detached *supervisor* (this same file, run
  with the suppressed ``--supervise`` flag), which in turn spawns
  uvicorn with ``--reload``. The entry script then blocks on
  ``/healthz`` and returns once the worker is serving — same UX as
  before.
* The supervisor polls ``/healthz`` and, after K consecutive failures
  (the signal-killed-worker case ``--reload`` doesn't cover), kills the
  whole uvicorn process group and respawns it. ``[supervisor]`` lines
  in ``.uvicorn.log`` mark every spawn / respawn / give-up.
* uvicorn's own ``--reload`` still owns file-change restarts; the
  supervisor never reacts to those because the *parent* uvicorn
  process (the reloader) stays alive across ``.py`` edits.
* A crash-loop guard (``RESPAWN_MAX`` respawns in ``RESPAWN_WINDOW_S``)
  exits the supervisor with a loud log line so the operator notices
  via ``--status`` instead of an invisible loop.

``.uvicorn.log`` is gitignored. ``.uvicorn.supervisor.state`` (a tiny
JSON pidfile + counters) is also gitignored — see ``.gitignore``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = REPO_ROOT / ".uvicorn.log"
STATE_FILE = REPO_ROOT / ".uvicorn.supervisor.state"
PORT = 8000
PROC_MATCH = "uvicorn outside_line.main:app"
SUPERVISOR_MATCH = "dev_serve.py --supervise"
HEALTHZ_URL = f"http://127.0.0.1:{PORT}/healthz"

# Supervisor tunables. Inline so a future operator can tweak without
# digging through layers of config.
FIRST_BOOT_TIMEOUT_S = 15.0
PROBE_INTERVAL_S = 2.0
PROBE_TIMEOUT_S = 1.0
PROBE_FAIL_K = 3            # consecutive healthz fails before respawn
RESPAWN_MAX = 5             # respawns allowed within RESPAWN_WINDOW_S
RESPAWN_WINDOW_S = 60.0
CHILD_TERM_GRACE_S = 2.0    # SIGTERM grace before SIGKILL


# ---------------------------------------------------------------------------
# pgrep helpers (used both by entry script and supervisor)
# ---------------------------------------------------------------------------


def _find_pids(proc_match: str) -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", proc_match],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except FileNotFoundError:
        return []
    pids = [int(p) for p in out.split() if p.strip().isdigit()]
    # When the supervisor invokes pgrep for the worker pattern, the
    # supervisor process itself doesn't match (different argv) — no need
    # to filter self.
    return pids


def _kill_pids(pids: list[int], sig: int = signal.SIGTERM) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _kill_match(proc_match: str) -> int:
    """SIGTERM every process matching ``proc_match``; wait briefly for exit.

    Returns the number of PIDs we attempted to kill.
    """
    pids = _find_pids(proc_match)
    if not pids:
        return 0
    _kill_pids(pids, signal.SIGTERM)
    for _ in range(20):
        if not _find_pids(proc_match):
            break
        time.sleep(0.1)
    # Anything still alive after 2 s gets SIGKILL.
    leftover = _find_pids(proc_match)
    if leftover:
        _kill_pids(leftover, signal.SIGKILL)
        for _ in range(10):
            if not _find_pids(proc_match):
                break
            time.sleep(0.1)
    return len(pids)


# ---------------------------------------------------------------------------
# Healthz probe
# ---------------------------------------------------------------------------


def _healthz_ok(timeout: float = PROBE_TIMEOUT_S) -> bool:
    try:
        with urllib.request.urlopen(HEALTHZ_URL, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


def _wait_for_healthz(timeout: float = FIRST_BOOT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthz_ok():
            return True
        time.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# State file (so --status can report respawn count + last_exit_reason)
# ---------------------------------------------------------------------------


def _state_read() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _state_write(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _state_reset(started_at: float) -> None:
    _state_write({
        "started_at": started_at,
        "respawns": 0,
        "last_exit_reason": None,
    })


# ---------------------------------------------------------------------------
# Entry script (operator-facing)
# ---------------------------------------------------------------------------


def cmd_start() -> int:
    """Operator-facing entry. Detaches a supervisor + blocks on healthz."""
    sup_killed = _kill_match(SUPERVISOR_MATCH)
    worker_killed = _kill_match(PROC_MATCH)
    if sup_killed or worker_killed:
        print(
            f"killed {sup_killed} prior supervisor + {worker_killed} prior "
            f"uvicorn instance(s)"
        )

    # Truncate log for the operator-initiated session.
    LOG_FILE.write_text("", encoding="utf-8")
    _state_reset(started_at=time.time())

    log_fh = open(LOG_FILE, "ab", buffering=0)
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--supervise"],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=REPO_ROOT,
    )

    if not _wait_for_healthz(timeout=FIRST_BOOT_TIMEOUT_S):
        sys.stderr.write(
            f"error: uvicorn didn't pass /healthz within "
            f"{FIRST_BOOT_TIMEOUT_S:.0f}s; check {LOG_FILE}\n"
        )
        return 1

    sup_pids = _find_pids(SUPERVISOR_MATCH)
    worker_pids = _find_pids(PROC_MATCH)
    print(
        f"uvicorn running on :{PORT} (supervisor pid: {sup_pids}, "
        f"worker pids: {worker_pids})"
    )
    print(f"  logs: {LOG_FILE}")
    print(f"  tail: ./scripts/dev_serve.py --tail   (or: tail -f {LOG_FILE.name})")
    return 0


def cmd_stop() -> int:
    sup_killed = _kill_match(SUPERVISOR_MATCH)
    worker_killed = _kill_match(PROC_MATCH)
    print(
        f"killed {sup_killed} supervisor + {worker_killed} uvicorn process(es)"
    )
    return 0


def cmd_tail() -> int:
    if not LOG_FILE.exists():
        sys.stderr.write(f"no log yet at {LOG_FILE} (start the server first)\n")
        return 1
    os.execvp("tail", ["tail", "-f", str(LOG_FILE)])


def cmd_status() -> int:
    sup_pids = _find_pids(SUPERVISOR_MATCH)
    worker_pids = _find_pids(PROC_MATCH)
    size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    state = _state_read()
    respawns = state.get("respawns", 0)
    last_reason = state.get("last_exit_reason")

    if sup_pids and worker_pids:
        head = "RUNNING"
    elif worker_pids and not sup_pids:
        head = "RUNNING (no supervisor — orphaned worker)"
    else:
        head = "not running"

    print(f"uvicorn: {head}")
    print(f"  supervisor pids: {sup_pids or '[]'}")
    print(f"  worker pids:     {worker_pids or '[]'}")
    print(f"  healthz:         {'200' if _healthz_ok() else 'down'}")
    print(f"  respawns:        {respawns}")
    print(f"  last_exit:       {last_reason}")
    print(f"  log:             {LOG_FILE} ({size} bytes)")
    return 0


# ---------------------------------------------------------------------------
# Supervisor (detached child of the entry script)
# ---------------------------------------------------------------------------


def _sup_log(msg: str) -> None:
    """Write one ``[supervisor]`` line directly to stdout (== .uvicorn.log).

    Bypasses any buffering — supervisor lines need to be visible to the
    operator's ``tail -f`` immediately.
    """
    sys.stdout.write(f"[supervisor] {msg}\n")
    sys.stdout.flush()


def _spawn_child() -> subprocess.Popen[bytes]:
    """Spawn the uvicorn reloader process (== child of the supervisor).

    ``start_new_session=True`` puts the child in its own session/process
    group so ``os.killpg(child.pid, SIGTERM)`` targets only the uvicorn
    tree without blowing back on the supervisor itself.

    ``--reload-exclude data/sessions/*`` keeps Telethon's SQLite session
    writes (every Telegram update touches the file) from triggering a
    worker restart mid-call. Without this, a call where the contact
    answered would race the reload between ``voice()`` returning TwiML
    and Twilio opening the Media Stream WS — Twilio sees the WS open
    fail and plays its "We're sorry, an application error has occurred"
    fallback to the caller.
    """
    return subprocess.Popen(
        [
            "uv", "run", "uvicorn",
            "outside_line.main:app",
            "--reload",
            "--reload-exclude", "data/sessions/*",
            "--port", str(PORT),
            "--log-level", "info",
        ],
        stdout=sys.stdout.fileno(),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=REPO_ROOT,
        start_new_session=True,
    )


def _kill_child_tree(child: subprocess.Popen[bytes]) -> None:
    """SIGTERM the child's process group, then SIGKILL after grace."""
    try:
        pgid = os.getpgid(child.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + CHILD_TERM_GRACE_S
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
    # Belt-and-suspenders: anything still matching the worker pattern
    # (orphaned worker grandchild) gets a direct SIGKILL.
    leftover = _find_pids(PROC_MATCH)
    if leftover:
        _kill_pids(leftover, signal.SIGKILL)


def _bump_respawn_counter() -> int:
    state = _state_read()
    state["respawns"] = int(state.get("respawns", 0)) + 1
    _state_write(state)
    return state["respawns"]


def _record_exit(reason: str) -> None:
    state = _state_read()
    state["last_exit_reason"] = reason
    _state_write(state)


def cmd_supervise() -> int:
    """Long-running supervisor loop. Detached from the operator's shell."""
    _sup_log(f"started (pid={os.getpid()}, port={PORT})")

    # Forward SIGTERM/SIGINT to the child + exit cleanly.
    child_ref: dict[str, subprocess.Popen[bytes] | None] = {"c": None}

    def _on_term(signum: int, _frame: object) -> None:
        _sup_log(f"received signal {signum}, stopping child + exiting")
        child = child_ref["c"]
        if child is not None and child.poll() is None:
            _kill_child_tree(child)
        _record_exit("signalled")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    respawn_times: list[float] = []
    child = _spawn_child()
    child_ref["c"] = child

    if not _wait_for_healthz(timeout=FIRST_BOOT_TIMEOUT_S):
        _sup_log(
            f"first healthz never went green within {FIRST_BOOT_TIMEOUT_S:.0f}s "
            f"— giving up so the operator notices via --status"
        )
        if child.poll() is None:
            _kill_child_tree(child)
        _record_exit("first_boot_timeout")
        return 1

    _sup_log("first healthz green")

    consecutive_fails = 0
    while True:
        # Detect outright child exit (rare — uvicorn's reloader doesn't
        # usually exit on its own).
        rc = child.poll()
        if rc is not None:
            _sup_log(f"child exited (rc={rc}), respawning")
            consecutive_fails = 0  # fresh start
            respawn_times.append(time.monotonic())
            n = _bump_respawn_counter()
            if _is_crash_loop(respawn_times):
                _sup_log(
                    f"crash loop ({RESPAWN_MAX} respawns in "
                    f"{RESPAWN_WINDOW_S:.0f}s), giving up"
                )
                _record_exit("crash_loop")
                return 1
            child = _spawn_child()
            child_ref["c"] = child
            if not _wait_for_healthz(timeout=FIRST_BOOT_TIMEOUT_S):
                _sup_log(
                    f"post-respawn healthz never went green (respawn #{n})"
                )
                # Treat as another crash in the loop count.
                respawn_times.append(time.monotonic())
                if _is_crash_loop(respawn_times):
                    _sup_log(
                        f"crash loop ({RESPAWN_MAX} respawns in "
                        f"{RESPAWN_WINDOW_S:.0f}s), giving up"
                    )
                    if child.poll() is None:
                        _kill_child_tree(child)
                    _record_exit("crash_loop")
                    return 1
            else:
                _sup_log(f"respawn #{n} healthz green")
            continue

        if _healthz_ok():
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            if consecutive_fails >= PROBE_FAIL_K:
                _sup_log(
                    f"worker unresponsive ({PROBE_FAIL_K}x healthz fail), "
                    f"respawning"
                )
                _kill_child_tree(child)
                respawn_times.append(time.monotonic())
                n = _bump_respawn_counter()
                if _is_crash_loop(respawn_times):
                    _sup_log(
                        f"crash loop ({RESPAWN_MAX} respawns in "
                        f"{RESPAWN_WINDOW_S:.0f}s), giving up"
                    )
                    _record_exit("crash_loop")
                    return 1
                child = _spawn_child()
                child_ref["c"] = child
                if not _wait_for_healthz(timeout=FIRST_BOOT_TIMEOUT_S):
                    _sup_log(
                        f"post-respawn healthz never went green (respawn #{n})"
                    )
                    respawn_times.append(time.monotonic())
                    if _is_crash_loop(respawn_times):
                        _sup_log(
                            f"crash loop ({RESPAWN_MAX} respawns in "
                            f"{RESPAWN_WINDOW_S:.0f}s), giving up"
                        )
                        if child.poll() is None:
                            _kill_child_tree(child)
                        _record_exit("crash_loop")
                        return 1
                else:
                    _sup_log(f"respawn #{n} healthz green")
                consecutive_fails = 0

        time.sleep(PROBE_INTERVAL_S)


def _is_crash_loop(respawn_times: list[float]) -> bool:
    """True if the last RESPAWN_MAX respawns all fit inside RESPAWN_WINDOW_S."""
    if len(respawn_times) < RESPAWN_MAX:
        return False
    window = respawn_times[-RESPAWN_MAX:]
    return (window[-1] - window[0]) <= RESPAWN_WINDOW_S


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Manage the dev uvicorn server (logs to .uvicorn.log).",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--stop", action="store_true", help="stop the server")
    g.add_argument("--tail", action="store_true", help="follow the log file")
    g.add_argument(
        "--status", action="store_true",
        help="report running state + respawns + log size",
    )
    # Internal: spawned by cmd_start as a detached child. Suppressed in --help
    # so the operator surface stays "start / stop / tail / status".
    g.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.stop:
        return cmd_stop()
    if args.tail:
        return cmd_tail()
    if args.status:
        return cmd_status()
    if args.supervise:
        return cmd_supervise()
    return cmd_start()


if __name__ == "__main__":
    raise SystemExit(main())
