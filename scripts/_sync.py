"""Local↔Railway mirror wrapper used by the operator CLIs.

Single shared abstraction so ``scripts/callers.py`` and ``scripts/contacts.py``
can keep the same one-file (or one-directory) of state in sync between
the local checkout and the Railway volume mounted at ``/app/data``. Read
commands auto-pull the latest authoritative state before running; write
commands push back atomically.

The shape:

* :class:`SyncSpec` — declares ``kind=file|dir``, the remote path, and the
  local path. Operator scripts build one of these at startup.
* :func:`with_sync` — runs ``pull → fn(...) → push`` (push only when
  ``mutating=True``). Falls back to local-only with a one-line warning
  if ``--no-remote`` is set or ``railway`` isn't available.

Snapshot-in / mutate / snapshot-out trades a tiny race window for code
simplicity — production's ``record_call`` could write the file between
our pull and push and lose the update. Acceptable at a-few-calls-per-day
volume; documented in ``docs/decisions/0006-operator-cli-railway-sync.md``.

The push semantics for the two kinds:

* ``kind="file"`` — atomically replace ``spec.remote`` with the bytes of
  ``spec.local`` (single SSH round-trip via ``cat > tmp && mv``).
* ``kind="dir"`` — diff the local and remote directories: upload any
  file that's new locally or whose bytes differ, and remove from the
  remote any file that vanished locally. The diff is computed against
  the snapshot we just pulled, so a parallel ``record_call``-style write
  on the remote during the same window can be clobbered.

Read commands skip the push, so a remote that got bumped between our
pull and our local rendering will lose the bump — but the next mutating
command will re-pull and pick it back up.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from _railway import (
    RailwayError,
    railway_available,
    railway_cat,
    railway_list,
    railway_rm,
    railway_ssh,
    railway_write_text,
)


SyncKind = Literal["file", "dir"]


@dataclass(frozen=True)
class SyncSpec:
    """Where one CLI's state lives, locally and on Railway.

    ``kind="file"`` covers single-file stores like ``data/callers.json``.
    ``kind="dir"`` covers per-record directories, e.g. the caller-memory
    store at ``data/memory/`` (one ``<phone>.md`` per caller). The
    ``dir_extensions`` tuple selects which files are in scope on push and
    delete — dotfiles are always ignored, and files outside the
    allow-list are left alone in both directions.
    """

    kind: SyncKind
    remote: str  # absolute path on the Railway volume, e.g. /app/data/callers.json
    local: Path  # canonical local path on disk
    # File extensions in scope for dir-mode push/delete. The memory store
    # passes ``(".md",)``. Use lowercase including the leading dot.
    dir_extensions: tuple[str, ...] = (".json",)


def _warn(msg: str) -> None:
    sys.stderr.write(f"{msg}\n")


# -- pull ------------------------------------------------------------------


def _pull_file(spec: SyncSpec) -> None:
    """Replace ``spec.local`` with the bytes of ``spec.remote`` (or empty)."""
    remote_bytes = railway_cat(spec.remote)
    if remote_bytes is None:
        # Remote missing/empty — leave the local copy alone if it exists,
        # or create an empty one so the store can read it cleanly.
        if not spec.local.exists():
            spec.local.parent.mkdir(parents=True, exist_ok=True)
            spec.local.write_text("{}\n", encoding="utf-8")
        return
    # Validate JSON up front so we never mirror garbage onto the local
    # copy (and never push that garbage back in the next mutating call).
    try:
        json.loads(remote_bytes)
    except json.JSONDecodeError as exc:
        raise RailwayError(
            f"remote {spec.remote} is not valid JSON ({exc}); "
            f"inspect with: railway ssh \"cat {spec.remote}\""
        )
    spec.local.parent.mkdir(parents=True, exist_ok=True)
    tmp = spec.local.with_suffix(spec.local.suffix + ".sync.tmp")
    tmp.write_text(remote_bytes, encoding="utf-8")
    os.replace(tmp, spec.local)


def _pull_dir(spec: SyncSpec) -> dict[str, str]:
    """Mirror ``spec.remote`` directory contents into ``spec.local``.

    Adds any file present remotely but missing locally. Replaces any
    file whose bytes differ. Removes any local file whose name no
    longer exists remotely.

    Returns the ``{filename: remote_bytes}`` snapshot it just pulled so
    :func:`_push_dir` can diff against it without re-fetching every
    file (saves N SSH round-trips on the post-mutation push).
    """
    spec.local.mkdir(parents=True, exist_ok=True)
    remote_files = set(railway_list(spec.remote))
    local_files = {p.name for p in spec.local.iterdir() if p.is_file()}
    snapshot: dict[str, str] = {}

    # Add or update.
    for fname in remote_files:
        local_path = spec.local / fname
        content = railway_cat(f"{spec.remote}/{fname}")
        if content is None:
            continue
        snapshot[fname] = content
        if local_path.exists() and local_path.read_text(encoding="utf-8") == content:
            continue
        tmp = local_path.with_suffix(local_path.suffix + ".sync.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, local_path)

    # Remove files that no longer exist remotely.
    for fname in local_files - remote_files:
        # Defensive: never touch dotfiles or anything outside the spec's
        # allow-listed extensions (someone might `cp` a backup into the
        # dir, or the memory dir might share space with future kinds).
        if fname.startswith(".") or not _in_scope(fname, spec.dir_extensions):
            continue
        (spec.local / fname).unlink()

    return snapshot


def _in_scope(fname: str, extensions: tuple[str, ...]) -> bool:
    """True if ``fname`` ends with one of ``extensions`` (case-insensitive)."""
    fl = fname.lower()
    return any(fl.endswith(ext) for ext in extensions)


# -- push ------------------------------------------------------------------


def _push_file(spec: SyncSpec) -> None:
    """Upload ``spec.local`` to ``spec.remote`` atomically."""
    if not spec.local.exists():
        # Nothing to push (mutating fn might have just deleted everything).
        return
    railway_write_text(spec.remote, spec.local.read_text(encoding="utf-8"))


def _push_dir(spec: SyncSpec, *, snapshot: dict[str, str]) -> None:
    """Push local dir contents to remote; remove remote files that vanished.

    ``snapshot`` is the ``{filename: remote_bytes}`` we captured in
    :func:`_pull_dir`. We diff against it instead of re-fetching each
    file, so a no-op push costs zero SSH round-trips and a real push
    costs one ``railway_write_text`` per changed file.
    """
    if not spec.local.exists():
        return
    post_local = {p.name for p in spec.local.iterdir() if p.is_file()}

    # Upload anything that's new locally or whose bytes differ from pull.
    pending_writes: list[tuple[str, str]] = []
    for fname in post_local:
        if fname.startswith(".") or not _in_scope(fname, spec.dir_extensions):
            continue
        local_path = spec.local / fname
        local_content = local_path.read_text(encoding="utf-8")
        if snapshot.get(fname) == local_content:
            continue
        pending_writes.append((fname, local_content))

    if pending_writes:
        # Ensure the remote dir exists before writing — the prod app
        # creates it lazily, so on a fresh volume it may not be there.
        rc, _, err = railway_ssh(f"mkdir -p {spec.remote}")
        if rc != 0:
            raise RailwayError(
                f"railway mkdir -p failed for {spec.remote}: rc={rc} stderr={err.strip()!r}"
            )
        for fname, content in pending_writes:
            railway_write_text(f"{spec.remote}/{fname}", content)

    # Remove from remote anything our mutation deleted locally.
    pre_remote = set(snapshot)
    for fname in pre_remote - post_local:
        if fname.startswith(".") or not _in_scope(fname, spec.dir_extensions):
            continue
        railway_rm(f"{spec.remote}/{fname}")


# -- public entrypoint -----------------------------------------------------


def with_sync(
    spec: SyncSpec,
    *,
    no_remote: bool,
    mutating: bool,
    fn: Callable[[], int],
) -> int:
    """Pull, run ``fn``, optionally push. Returns ``fn``'s exit code.

    Falls back to running ``fn`` against the local file untouched when
    ``no_remote`` is True or ``railway_available()`` returns False. Prints
    a one-line ``[remote sync skipped]`` notice in the implicit-skip case
    so the operator can spot drift; stays silent when they explicitly
    opted out with ``--no-remote``.
    """
    if no_remote:
        return fn()
    if not railway_available():
        _warn("[remote sync skipped] railway CLI unavailable; running against local only")
        return fn()

    snapshot: dict[str, str] = {}
    try:
        if spec.kind == "file":
            _pull_file(spec)
        else:
            snapshot = _pull_dir(spec)
    except RailwayError as exc:
        _warn(f"error: pull failed: {exc}")
        return 1

    rc = fn()

    if mutating and rc == 0:
        try:
            if spec.kind == "file":
                _push_file(spec)
            else:
                _push_dir(spec, snapshot=snapshot)
        except RailwayError as exc:
            _warn(f"error: push failed (local was updated, remote is stale): {exc}")
            return 1

    return rc


# -- tiny round-trip self-check helper for the implementation step --------


def _selfcheck_file_roundtrip(spec: SyncSpec) -> bool:
    """No-op fn → confirm local hash unchanged after a sync round-trip."""
    if not spec.local.exists():
        return False
    before = spec.local.read_bytes()
    rc = with_sync(spec, no_remote=False, mutating=True, fn=lambda: 0)
    if rc != 0:
        return False
    return spec.local.read_bytes() == before


__all__ = [
    "SyncSpec",
    "with_sync",
    "_selfcheck_file_roundtrip",
]
