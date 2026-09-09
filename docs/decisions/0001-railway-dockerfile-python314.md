# 0001 — Railway build: Dockerfile instead of Nixpacks (Python 3.14)

**Date:** 2026-05-29
**Status:** Accepted

## Context

The initial `railway.json` specified the **Nixpacks** builder. The first
Railway deploy failed at `uv sync` with:

```
error: No interpreter found for Python 3.14 in managed installations or system path
```

Railway's Nixpacks builder pins **uv 0.4.30** (Oct 2024), which predates Python
3.14 and cannot download/provide a 3.14 interpreter. The project requires
Python 3.14 (`requires-python >=3.14` in `pyproject.toml`), so Nixpacks can't
build it as-is.

## Decision

Build from a **Dockerfile** instead:

- Base image `python:3.14-slim-bookworm` (interpreter guaranteed).
- Install `uv==0.11.17` via pip (matches the local toolchain).
- `UV_PYTHON_DOWNLOADS=never` so uv uses the system 3.14 interpreter.
- `uv sync --no-dev --frozen` against the committed `uv.lock`.
- Launch via the Dockerfile `CMD` (`sh -c` wrapper) so `${PORT}` expands at
  runtime. The `railway.json` `startCommand` was removed — when set, Railway ran
  it without a shell and passed the literal string `$PORT` to uvicorn
  (`'$PORT' is not a valid integer`).

`railway.json` now sets `"builder": "DOCKERFILE"`.

## Consequences

- Build is reproducible and pinned (no reliance on Nixpacks' uv version).
- If we later need system packages (e.g. for audio), they go in the
  Dockerfile via `apt-get`, which is more controllable than Nixpacks anyway.
