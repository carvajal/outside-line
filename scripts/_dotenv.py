"""Minimal ``.env`` loader shared by the repo's operator scripts.

Parses ``KEY=value`` per line, ``#`` comments, optional ``export`` prefix,
optional surrounding single/double quotes. Lines that don't match are
ignored (matches what ``set -a; source .env`` tolerates for our use).
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"


def load_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def env() -> dict[str, str]:
    """Return the merged ``.env`` + ``os.environ`` dict (env wins)."""
    return {**load_dotenv(), **os.environ}
