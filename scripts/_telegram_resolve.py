"""Telegram username → user_id probe for the contacts CLI.

The username-only refactor (ADR 0011) replaced the phone-based contact
import path. Operators no longer need to land a phone number in the
agent account's Telegram address book; instead they verify a public
``@username`` resolves cleanly via :func:`resolve_username`, which calls
``client.get_entity("@handle")`` on the Railway container against the
live Telethon session at ``/app/data/sessions/outside-line.session``
(ADR 0008).

Also exposes :func:`railway_redeploy` so the CLI can trigger a fresh
deploy after a JSON mutation — the FastAPI process caches contacts at
module level (``outside_line.contacts._cached``) and only refreshes on
restart.
"""

from __future__ import annotations

import json
import subprocess

from _railway import railway_ssh


class TelegramResolveError(RuntimeError):
    """Raised when the on-container Telethon resolution call fails."""


_RESOLVE_SNIPPET = r"""
import asyncio, json, os, sys
from telethon import TelegramClient


async def _main():
    api_id = int(os.environ['TELEGRAM_API_ID'])
    api_hash = os.environ['TELEGRAM_API_HASH']
    session_path = os.environ.get(
        'TELEGRAM_SESSION_PATH', '/app/data/sessions/outside-line.session'
    )
    handle = sys.stdin.read().strip()
    if not handle:
        print(json.dumps({'error': 'no handle provided on stdin'}))
        return 2
    # Telethon appends the .session suffix itself — strip it if present.
    if session_path.endswith('.session'):
        session_path = session_path[:-len('.session')]

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print(json.dumps({'error': 'session not authorized'}))
            return 2
        try:
            entity = await client.get_entity(handle)
        except Exception as exc:
            print(json.dumps({'error': f'get_entity failed: {exc!r}'}))
            return 1
        out = {
            'user_id': getattr(entity, 'id', None),
            'username': getattr(entity, 'username', None),
            'first_name': getattr(entity, 'first_name', None),
            'last_name': getattr(entity, 'last_name', None),
        }
        print(json.dumps(out))
        return 0
    finally:
        await client.disconnect()


sys.exit(asyncio.run(_main()))
"""


def resolve_username(handle: str) -> dict:
    """Resolve ``handle`` (e.g. ``"@some_handle"``) via the agent's session.

    Runs on the Railway container via ``railway ssh``. Returns the
    parsed dict with ``user_id``, ``username``, ``first_name``,
    ``last_name``. Raises :class:`TelegramResolveError` on any failure
    of the on-container Python (no such user, session unauthorized,
    container Python error, etc.).
    """
    cmd = f"python -c {_shell_quote(_RESOLVE_SNIPPET)}"
    rc, out, err = railway_ssh(cmd, stdin=handle)
    if rc != 0:
        raise TelegramResolveError(
            f"on-container python exited rc={rc}: {err.strip() or out.strip()!r}"
        )
    last_json_line = ""
    for line in out.splitlines()[::-1]:
        line = line.strip()
        if line.startswith("{"):
            last_json_line = line
            break
    if not last_json_line:
        raise TelegramResolveError(
            f"unexpected output (no JSON line): {out.strip()!r}"
        )
    try:
        parsed = json.loads(last_json_line)
    except json.JSONDecodeError as exc:
        raise TelegramResolveError(
            f"could not parse JSON output: {exc} (line: {last_json_line!r})"
        )
    if "error" in parsed:
        raise TelegramResolveError(parsed["error"])
    return parsed


def railway_redeploy() -> int:
    """``railway redeploy --yes``. Returns the CLI exit code (0 = ok)."""
    proc = subprocess.run(
        ["railway", "redeploy", "--yes"],
        check=False,
    )
    return proc.returncode


def _shell_quote(s: str) -> str:
    """POSIX shell single-quote, escaping any embedded single quotes."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


__all__ = [
    "TelegramResolveError",
    "resolve_username",
    "railway_redeploy",
]
