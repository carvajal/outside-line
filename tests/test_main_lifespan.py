"""Lifespan wiring for the sidecar flip (ADR 0017).

The real ntgcalls path needs Telegram creds + a live call to exercise (covered
by the manual real-call verification, not CI). Here we assert the no-creds skip
path is preserved: no sidecar is spawned and ``app.state.telegram_bridge`` is
None, so message-only functionality stays intact.
"""

from __future__ import annotations

import pytest

from outside_line import main as main_mod


@pytest.mark.asyncio
async def test_lifespan_skips_sidecar_without_creds(monkeypatch) -> None:
    monkeypatch.setattr(main_mod.settings, "telegram_api_id", None)
    async with main_mod.lifespan(main_mod.app):
        assert main_mod.app.state.telegram_bridge is None
        assert main_mod.app.state.telegram_supervisor is None
