"""Unit tests for the empty-transcript nudge in `RealtimeSession`.

When whisper-1 returns "" (VAD committed on silence/noise), the session
must inject a system message telling the agent to ask for a repeat instead of
improvising. Persona rule is the primary lever; this nudge is the
belt-and-suspenders safety net for the pure-silence case.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from outside_line.realtime_agent import RealtimeSession


def _make_session_with_fake_conn() -> tuple[RealtimeSession, AsyncMock]:
    """Construct a RealtimeSession bypassing __aenter__ and wire a fake _conn."""
    rt = RealtimeSession(on_audio_out=AsyncMock())
    create_mock = AsyncMock()
    rt._conn = SimpleNamespace(  # type: ignore[assignment]
        conversation=SimpleNamespace(item=SimpleNamespace(create=create_mock))
    )
    return rt, create_mock


def test_nudge_creates_system_item() -> None:
    rt, create_mock = _make_session_with_fake_conn()
    asyncio.run(rt._nudge_repeat_on_empty())
    create_mock.assert_awaited_once()
    kwargs = create_mock.await_args.kwargs
    item = kwargs["item"]
    assert item["type"] == "message"
    assert item["role"] == "system"
    text = item["content"][0]["text"]
    assert "Sorry, could you repeat that?" in text


def test_nudge_is_no_op_when_conn_missing() -> None:
    """Defensive: if the WS already closed, the helper must not raise."""
    rt = RealtimeSession(on_audio_out=AsyncMock())
    rt._conn = None
    asyncio.run(rt._nudge_repeat_on_empty())  # should not raise


def test_nudge_swallows_create_errors() -> None:
    """A failing conversation.item.create must not propagate."""
    rt, create_mock = _make_session_with_fake_conn()
    create_mock.side_effect = RuntimeError("ws gone")
    asyncio.run(rt._nudge_repeat_on_empty())  # should not raise
