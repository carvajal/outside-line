"""Shared Railway ssh + volume helpers for the operator scripts.

The repo has one Railway service with a persistent volume
mounted at ``/app/data`` (see ADR 0004). Operator CLIs sometimes need to
read or atomically write files on that volume from a local machine
without round-tripping through a deploy.

This module wraps ``railway ssh`` for that. It exposes:

* :func:`railway_ssh` — raw ``railway ssh <cmd>`` invocation (rc, stdout,
  stderr). Two-second SSH session overhead per call.
* :func:`railway_list` — ``ls`` a remote directory, return basenames.
* :func:`railway_cat` — read a remote file as text, or ``None`` on miss.
* :func:`railway_write_text` — atomically write text to a remote path
  via SSH stdin → ``cat > tmp && mv tmp final`` in one round-trip.
* :func:`railway_rm` — ``rm -f`` a single remote file.
* :func:`railway_available` — cached probe so the operator scripts can
  degrade gracefully when ``railway`` isn't authenticated/linked.

"""

from __future__ import annotations

import subprocess

# The volume mount path inside the Railway container — confirmed by
# docs/decisions/0004-railway-data-volume.md. Operator scripts should
# import this rather than hard-coding the string.
RAILWAY_DATA = "/app/data"


class RailwayError(RuntimeError):
    """Raised when a ``railway`` invocation we expected to work failed."""


def railway_ssh(cmd: str, *, stdin: str | None = None) -> tuple[int, str, str]:
    """Run ``railway ssh <cmd>`` and return ``(rc, stdout, stderr)``.

    ``stdin`` is piped into the remote command (text). Don't shell-quote
    ``cmd`` yourself — Railway's CLI hands it to a shell on the other side
    and a single string is what works in practice (matches the prior
    usage in ``scripts/archive.py``).
    """
    # When no stdin is supplied, route the child's stdin to /dev/null
    # so it can't inherit (and consume) the parent process's stdin. This
    # matters when an operator CLI is itself piped data — e.g.
    # `... | ./scripts/callers.py memory set +1...` — and a sync-layer
    # pull does railway_list/railway_cat before the script reads stdin.
    proc = subprocess.run(
        ["railway", "ssh", cmd],
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def railway_list(remote_dir: str) -> list[str]:
    """Return basenames under ``remote_dir``, or ``[]`` if it doesn't exist."""
    rc, out, _ = railway_ssh(f"ls {remote_dir} 2>/dev/null")
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def railway_cat(remote_path: str) -> str | None:
    """Return file content as text, or ``None`` on miss / error.

    Empty files come back as ``None`` too — there's no way for the
    operator CLIs to distinguish "absent" from "empty" through ``cat``
    output alone, and the callers treat both the same.
    """
    rc, out, _ = railway_ssh(f"cat {remote_path} 2>/dev/null")
    if rc != 0 or not out:
        return None
    return out


def railway_write_text(remote_path: str, content: str) -> None:
    """Atomically write ``content`` to ``remote_path`` on the volume.

    Pipes the bytes via SSH stdin into ``<path>.tmp.$$`` and renames it
    into place in a single shell invocation, so a crash mid-write can't
    leave the final path half-written. Raises :class:`RailwayError` if
    either the upload or the rename fails.
    """
    # $$ on the remote shell is the SSH-side PID, so concurrent operator
    # commands can't collide on the same temp name.
    remote_cmd = f"cat > {remote_path}.tmp.$$ && mv {remote_path}.tmp.$$ {remote_path}"
    rc, _, err = railway_ssh(remote_cmd, stdin=content)
    if rc != 0:
        raise RailwayError(
            f"railway write failed for {remote_path}: rc={rc} stderr={err.strip()!r}"
        )


def railway_rm(remote_path: str) -> None:
    """``rm -f`` a single file on the remote volume.

    Idempotent (``-f`` swallows "no such file"). Raises only if the SSH
    invocation itself fails (auth, connectivity).
    """
    rc, _, err = railway_ssh(f"rm -f {remote_path}")
    if rc != 0:
        raise RailwayError(
            f"railway rm failed for {remote_path}: rc={rc} stderr={err.strip()!r}"
        )


_availability_cache: bool | None = None


def railway_available(*, force: bool = False) -> bool:
    """Cached probe: is ``railway`` authenticated and linked to a project?

    Runs ``railway status`` once per process and caches the result. The
    sync wrapper calls this before attempting any remote work and falls
    back to local-only with a warning if it returns ``False``.
    """
    global _availability_cache
    if _availability_cache is not None and not force:
        return _availability_cache
    try:
        proc = subprocess.run(
            ["railway", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _availability_cache = False
        return False
    _availability_cache = proc.returncode == 0
    return _availability_cache
