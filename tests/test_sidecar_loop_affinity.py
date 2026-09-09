"""Regression: the sidecar must build its bridge UNDER the serve loop (ADR 0017).

PyTgCalls binds ``asyncio.get_event_loop()`` at construction (``pytgcalls.py:42``).
The first cut of the sidecar built the real bridge in ``run()`` *before*
``asyncio.run()`` existed, so every ntgcalls future was pinned to the wrong loop
and ``place_call`` raised ``RuntimeError: ... Future attached to a different
loop`` — the contact never rang. The echo path can't catch this (``EchoBridge``
touches no loop-bound C objects), so this test guards the ordering directly:
``_run`` must invoke the bridge factory while its own loop is running.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile

import pytest

from outside_line import telegram_sidecar as sc


@pytest.mark.asyncio
async def test_bridge_is_constructed_under_the_serve_loop() -> None:
    captured: dict[str, asyncio.AbstractEventLoop] = {}

    def make_bridge() -> sc.EchoBridge:
        # A real PyTgCalls would call get_event_loop() right here; capture the
        # running loop so we can prove it's the same one _run serves on.
        captured["loop"] = asyncio.get_running_loop()
        return sc.EchoBridge()

    socket_path = tempfile.mktemp(prefix="rc_loopaff_", suffix=".sock", dir="/tmp")
    task = asyncio.create_task(sc._run(make_bridge, socket_path))
    try:
        for _ in range(200):
            if "loop" in captured:
                break
            await asyncio.sleep(0.01)
        assert captured.get("loop") is asyncio.get_running_loop()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        with contextlib.suppress(FileNotFoundError):
            os.unlink(socket_path)
