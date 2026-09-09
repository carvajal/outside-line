"""Shared helpers for ``scripts/dev_mode.py``.

Stdlib only. Pure-Python where reasonable; shells out to ``ngrok``, ``pkill``,
and ``./scripts/twilio.py`` where shelling out is the natural call.

Treat this module as an implementation detail of ``scripts/dev_mode.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
TWILIO_WRAPPER = REPO_ROOT / "scripts" / "twilio.py"

# Deployment-specific hosts, read from .env (they are per-install, not
# per-repo): DEV_NGROK_DOMAIN is a reserved ngrok domain for local mode,
# RAILWAY_APP_DOMAIN is the deployed app's public host. Commands that
# need one fail with a clear message when it's unset.
from _dotenv import env as _dotenv_env  # noqa: E402

_ENV = _dotenv_env()
NGROK_DOMAIN = _ENV.get("DEV_NGROK_DOMAIN", "")
RAILWAY_DOMAIN = _ENV.get("RAILWAY_APP_DOMAIN", "")
LOCAL_PORT = 8000

# Numbers whose Twilio "friendly name" starts with this string are managed
# by dev-mode (their voice webhook gets rotated between prod and local on
# each toggle). The trailing space prevents accidental matches on names
# like ``outside-line-experiment`` — only ``outside-line <role> ...``
# shapes match.
MANAGED_FRIENDLY_NAME_PREFIX = "outside-line "

NGROK_API_TUNNELS = "http://127.0.0.1:4040/api/tunnels"


def _require_domain(value: str, env_key: str) -> str:
    if not value:
        raise RuntimeError(
            f"{env_key} is not set in .env — add it (e.g. "
            f"{env_key}=your-domain.example) before running this command"
        )
    return value


def dev_url() -> str:
    return f"https://{_require_domain(NGROK_DOMAIN, 'DEV_NGROK_DOMAIN')}"


def prod_url() -> str:
    return f"https://{_require_domain(RAILWAY_DOMAIN, 'RAILWAY_APP_DOMAIN')}"


# ---------------------------------------------------------------------------
# .env mutation
# ---------------------------------------------------------------------------


def update_env(updates: dict[str, str | None]) -> dict[str, tuple[str | None, str | None]]:
    """Apply ``updates`` to ``.env``. ``None`` value deletes the key.

    Preserves comments + ordering of untouched lines. Atomic write (tmp + rename).
    Returns a dict mapping each changed key to ``(before, after)`` so callers
    can show a diff.
    """
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True) if ENV_FILE.exists() else []
    seen: set[str] = set()
    changes: dict[str, tuple[str | None, str | None]] = {}

    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(raw)
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key not in updates:
            out.append(raw)
            continue
        seen.add(key)
        new = updates[key]
        if new is None:
            changes[key] = (value, None)
            continue  # drop the line
        if new != value:
            changes[key] = (value, new)
        out.append(f"{key}={new}\n")

    for key, new in updates.items():
        if key in seen or new is None:
            continue
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append(f"{key}={new}\n")
        changes[key] = (None, new)

    tmp = ENV_FILE.with_suffix(ENV_FILE.suffix + ".tmp")
    tmp.write_text("".join(out), encoding="utf-8")
    os.replace(tmp, ENV_FILE)
    return changes


def env_value(key: str) -> str | None:
    """Read a single key from ``.env`` (returns ``None`` if absent)."""
    if not ENV_FILE.exists():
        return None
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key_, _, value = line.partition("=")
        if key_.strip() == key:
            return value.strip()
    return None


# ---------------------------------------------------------------------------
# ngrok lifecycle
# ---------------------------------------------------------------------------


def kill_ngrok() -> int:
    """Kill any running ``ngrok http`` process. Idempotent. Returns count killed."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "ngrok http"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except FileNotFoundError:
        return 0
    pids = [int(p) for p in out.split() if p.strip().isdigit()]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(0.5)
    return len(pids)


def _public_tunnel_url() -> str | None:
    try:
        with urllib.request.urlopen(NGROK_API_TUNNELS, timeout=1.0) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None
    for t in data.get("tunnels") or []:
        url = t.get("public_url") or ""
        if url.startswith("https://"):
            return url
    return None


def start_ngrok(domain: str, port: int, timeout: float = 10.0) -> str:
    """Launch ``ngrok http --domain=<domain> <port>`` detached, wait for ready.

    Returns the public URL the agent saw on the local API.
    """
    if shutil.which("ngrok") is None:
        raise RuntimeError(
            "ngrok binary not on PATH — install with: brew install ngrok"
        )
    log_path = REPO_ROOT / ".ngrok.log"
    log_fh = open(log_path, "ab", buffering=0)
    subprocess.Popen(
        ["ngrok", "http", f"--domain={domain}", str(port), "--log=stdout"],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=REPO_ROOT,
    )

    expected = f"https://{domain}"
    deadline = time.monotonic() + timeout
    last_seen: str | None = None
    while time.monotonic() < deadline:
        url = _public_tunnel_url()
        if url == expected:
            return url
        last_seen = url
        time.sleep(0.3)
    raise RuntimeError(
        f"ngrok didn't expose {expected} within {timeout:.0f}s "
        f"(last seen on local API: {last_seen!r}; check {log_path})"
    )


# ---------------------------------------------------------------------------
# Twilio webhook
# ---------------------------------------------------------------------------


def _run_twilio(args: list[str]) -> str:
    cmd = [str(TWILIO_WRAPPER), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"twilio CLI failed: {' '.join(cmd)}")
    return proc.stdout


@dataclass(frozen=True)
class PhoneNumberRow:
    """One row of the account's incoming-phone-numbers list."""

    sid: str                          # ``PN…`` — needed for the low-level
                                      # ``api:core:…:update`` command which
                                      # is keyed on SID, not E.164
    phone_number: str                 # E.164, e.g. ``+15551230100``
    friendly_name: str                # e.g. ``outside-line mainline (Springfield, IL)``
    voice_url: str | None             # current voice webhook (``None`` if unset)
    status_callback: str | None       # current call-lifecycle webhook
    status_callback_method: str | None  # ``POST`` / ``GET`` (Twilio default: POST)

    @property
    def is_managed(self) -> bool:
        return self.friendly_name.startswith(MANAGED_FRIENDLY_NAME_PREFIX)


def _parse_optional(raw: str) -> str | None:
    """Coerce Twilio TSV cells: ``"null"`` and ``""`` both mean unset."""
    raw = raw.strip()
    return None if raw in ("", "null") else raw


def list_phone_numbers() -> list[PhoneNumberRow]:
    """Every incoming phone number on the account, sorted by E.164.

    One Twilio CLI call. Returns both managed and unmanaged rows — callers
    filter via ``r.is_managed``. See ``managed_numbers()`` /
    ``unmanaged_numbers()`` for the common splits.
    """
    out = _run_twilio(
        [
            "phone-numbers:list",
            "--properties=sid,phoneNumber,friendlyName,voiceUrl,"
            "statusCallback,statusCallbackMethod",
            "-o",
            "tsv",
        ]
    )
    rows: list[PhoneNumberRow] = []
    for line in out.splitlines()[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        rows.append(
            PhoneNumberRow(
                sid=parts[0].strip(),
                phone_number=parts[1].strip(),
                friendly_name=parts[2].strip(),
                voice_url=_parse_optional(parts[3]),
                status_callback=_parse_optional(parts[4]),
                status_callback_method=_parse_optional(parts[5]),
            )
        )
    return sorted(rows, key=lambda r: r.phone_number)


def managed_numbers() -> list[PhoneNumberRow]:
    """Numbers dev-mode rotates between prod and local (prefix-matched)."""
    return [r for r in list_phone_numbers() if r.is_managed]


def unmanaged_numbers() -> list[PhoneNumberRow]:
    """Numbers on the account that don't follow the prefix convention.

    Surfaced by ``status`` so the operator knows they're being skipped.
    """
    return [r for r in list_phone_numbers() if not r.is_managed]


def current_twilio_voice_urls() -> dict[str, str | None]:
    """``{phone_number: voice_url}`` for every managed number."""
    return {r.phone_number: r.voice_url for r in managed_numbers()}


def current_twilio_status_callbacks() -> dict[str, str | None]:
    """``{phone_number: status_callback}`` for every managed number."""
    return {r.phone_number: r.status_callback for r in managed_numbers()}


def set_twilio_voice_webhook(url: str) -> dict[str, str | None]:
    """Repoint every managed number's voice webhook to ``url``.

    Fail-fast on the first update error. After all updates land, re-reads
    every managed number and raises if any drifted from ``url``. Re-running
    after a partial-failure is idempotent (Twilio no-ops same-value updates).

    Returns ``{phone_number: voice_url}`` post-update.
    """
    rows = managed_numbers()
    if not rows:
        raise RuntimeError(
            "no managed numbers found — need friendlyName starting with "
            f"{MANAGED_FRIENDLY_NAME_PREFIX!r}. Rename via "
            "`./scripts/twilio.py phone-numbers:update <DID> "
            "--friendly-name 'outside-line <role> (<location>)'`"
        )
    for r in rows:
        _run_twilio(
            [
                "phone-numbers:update",
                r.phone_number,
                f"--voice-url={url}",
                "--voice-method=POST",
                "-o",
                "json",
            ]
        )
    after = current_twilio_voice_urls()
    target = normalize_voice_url(url)
    bad = {p: u for p, u in after.items() if normalize_voice_url(u) != target}
    if bad:
        raise RuntimeError(
            f"twilio webhook read-back mismatch — wrote {url!r}, read {bad!r}"
        )
    return after


def set_twilio_status_callback(url: str, *, method: str = "POST") -> dict[str, str | None]:
    """Repoint every managed number's call-lifecycle webhook to ``url``.

    Uses the low-level ``api:core:incoming-phone-numbers:update`` command
    because the high-level ``phone-numbers:update`` doesn't expose
    ``--status-callback`` / ``--status-callback-method`` flags. The
    low-level command is keyed on the ``PN…`` SID (carried on
    ``PhoneNumberRow.sid``).

    Read-back semantics match ``set_twilio_voice_webhook``.
    """
    rows = managed_numbers()
    if not rows:
        raise RuntimeError(
            "no managed numbers found — need friendlyName starting with "
            f"{MANAGED_FRIENDLY_NAME_PREFIX!r}."
        )
    for r in rows:
        _run_twilio(
            [
                "api:core:incoming-phone-numbers:update",
                f"--sid={r.sid}",
                f"--status-callback={url}",
                f"--status-callback-method={method}",
                "-o",
                "json",
            ]
        )
    after = current_twilio_status_callbacks()
    target = normalize_voice_url(url)
    bad = {p: u for p, u in after.items() if normalize_voice_url(u) != target}
    if bad:
        raise RuntimeError(
            f"twilio statusCallback read-back mismatch — wrote {url!r}, read {bad!r}"
        )
    return after


# ---------------------------------------------------------------------------
# Mode model (used by scripts/dev_mode.py)
# ---------------------------------------------------------------------------

MODES = ("prod", "local")


def desired_voice_url(mode: str) -> str:
    """Canonical Twilio voice webhook URL for a given mode.

    The gate length is server-side (``DTMF_GATE_SECONDS``), not URL-encoded,
    so the URL is identical for both prod and local — only the host differs.
    """
    if mode == "prod":
        return f"{prod_url()}/twilio/voice"
    if mode == "local":
        return f"{dev_url()}/twilio/voice"
    raise ValueError(f"unknown mode: {mode!r}")


def desired_status_callback_url(mode: str) -> str:
    """Canonical Twilio call-lifecycle webhook URL for a given mode.

    Same host as ``desired_voice_url`` — both endpoints live on the same
    FastAPI app. Pointing them at different hosts would always be a bug
    (the statusCallback teardown helper would target a session registered
    on a different process), so we sweep them in lockstep.
    """
    if mode == "prod":
        return f"{prod_url()}/twilio/voice/status"
    if mode == "local":
        return f"{dev_url()}/twilio/voice/status"
    raise ValueError(f"unknown mode: {mode!r}")


def parse_voice_url(url: str | None) -> str | None:
    """Return the lowercased hostname from a Twilio voice URL, or None."""
    if not url:
        return None
    return (urlparse(url).hostname or "").lower() or None


def host_to_mode(url: str | None) -> str:
    """Map a single voice URL's host to a mode label.

    Returns ``prod`` (Railway), ``local`` (ngrok), or ``unknown`` (URL is
    ``None`` or the host matches neither).
    """
    host = parse_voice_url(url)
    if RAILWAY_DOMAIN and host == RAILWAY_DOMAIN:
        return "prod"
    if NGROK_DOMAIN and host == NGROK_DOMAIN:
        return "local"
    return "unknown"


def detect_mode() -> str:
    """Infer the mode from BOTH webhook URLs across every managed number.

    Considers ``voice_url`` and ``status_callback`` per DID. Returns
    ``prod`` / ``local`` only if every URL (voice + statusCallback ×
    every managed DID) agrees on the host. ``mixed`` if some point at
    Railway and others at ngrok. ``unknown`` if any URL is unset or
    matches neither host — that surfaces both first-time bootstrap
    (statusCallback was historically null) and config drift, both of
    which the operator should resolve before further toggles.
    """
    rows = managed_numbers()
    if not rows:
        return "unknown"
    modes: set[str] = set()
    for r in rows:
        modes.add(host_to_mode(r.voice_url))
        modes.add(host_to_mode(r.status_callback))
    if "unknown" in modes:
        return "unknown"
    if len(modes) == 1:
        return modes.pop()
    return "mixed"


def normalize_voice_url(url: str | None) -> str | None:
    """Re-emit a voice URL in canonical form so idempotency compares cleanly.

    Strips trailing slashes from the path and drops any query string (legacy
    ``?pause=N`` URLs are normalized away).
    """
    if not url:
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


# ---------------------------------------------------------------------------
# Railway env vars + redeploy
# ---------------------------------------------------------------------------


def _run_railway(args: list[str]) -> str:
    proc = subprocess.run(
        ["railway", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"railway CLI failed: railway {' '.join(args)}")
    return proc.stdout


def railway_get_var(key: str) -> str | None:
    """Return the current Railway value for ``key`` (None if absent)."""
    out = _run_railway(["variable", "list", "--kv"])
    prefix = f"{key}="
    for line in out.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def railway_set_var(key: str, value: str) -> None:
    """Set a Railway env var without triggering a redeploy.

    ``--skip-deploys`` is load-bearing: without it Railway auto-redeploys on
    every variable write, which would silently break the "only redeploy when
    leaving local-test" rule the dev-mode script enforces.
    """
    _run_railway(
        ["variable", "set", f"{key}={value}", "--skip-deploys", "--json"]
    )


HEALTHZ_TIMEOUT_SEC = 90
HEALTHZ_POLL_INTERVAL_SEC = 3


def poll_healthz(url: str, timeout: float = HEALTHZ_TIMEOUT_SEC) -> bool:
    """Poll ``url`` until it returns 200 or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(HEALTHZ_POLL_INTERVAL_SEC)
    return False


def railway_up_and_wait() -> None:
    """``railway up --detach`` + poll ``/healthz`` on the prod URL. Raises on failure."""
    proc = subprocess.run(
        ["railway", "up", "--detach"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError("railway up --detach failed")
    sys.stdout.write(proc.stdout)
    if not poll_healthz(f"{prod_url()}/healthz"):
        raise RuntimeError(
            f"Railway /healthz didn't go green in {HEALTHZ_TIMEOUT_SEC}s"
        )


# ---------------------------------------------------------------------------
# Local uvicorn lifecycle (small wrappers around scripts/dev_serve.py)
# ---------------------------------------------------------------------------

DEV_SERVE = REPO_ROOT / "scripts" / "dev_serve.py"


def dev_serve_start() -> None:
    subprocess.run([str(DEV_SERVE)], check=True, cwd=REPO_ROOT)


def dev_serve_stop() -> None:
    subprocess.run([str(DEV_SERVE), "--stop"], check=True, cwd=REPO_ROOT)


def dev_serve_running() -> bool:
    """True if ``dev_serve.py --status`` reports a running uvicorn."""
    proc = subprocess.run(
        [str(DEV_SERVE), "--status"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return "RUNNING" in proc.stdout
