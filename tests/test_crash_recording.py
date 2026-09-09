"""Fixed-clip playback to the caller (_play_ulaw_clip) and the contact
(_play_contact_disclosure).

On a sidecar SIGSEGV the caller hears one fixed clip, then the call is
torn down. The playback strides the μ-law clip in 160 B / 20 ms frames and must
bail immediately if the caller has already hung up (``ws_closed`` set), so a
dead leg isn't written to.
"""

from __future__ import annotations

import asyncio

import pytest

from outside_line import twilio_handler as twh


def _collect_player(clip: bytes, *, closed: bool) -> list[bytes]:
    sent: list[bytes] = []

    async def _send(payload: bytes) -> None:
        sent.append(payload)

    ws_closed = asyncio.Event()
    if closed:
        ws_closed.set()

    async def _run() -> None:
        # Skip the real playout-drain wait so the test doesn't sleep.
        await twh._play_ulaw_clip(_send, ws_closed, clip=clip)

    asyncio.run(_run())
    return sent


def test_plays_all_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(twh, "_CRASH_PLAYOUT_TAIL_S", 0.0)
    clip = bytes(400)  # 2×160 + 80 → 3 frames (last one short)
    sent = _collect_player(clip, closed=False)
    assert len(sent) == 3
    assert [len(f) for f in sent] == [160, 160, 80]
    assert b"".join(sent) == clip


def test_bails_when_ws_already_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(twh, "_CRASH_PLAYOUT_TAIL_S", 0.0)
    sent = _collect_player(bytes(320), closed=True)
    assert sent == []  # dead caller leg is never written to


def test_empty_clip_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(twh, "_CRASH_PLAYOUT_TAIL_S", 0.0)
    assert _collect_player(b"", closed=False) == []


# -- contact-leg recording disclosure ---------------------------------------


def _collect_disclosure(
    monkeypatch: pytest.MonkeyPatch,
    clip: bytes,
    *,
    closed: bool,
) -> list[bytes]:
    monkeypatch.setattr(twh.settings, "recordings_enabled", True)
    monkeypatch.setattr(twh.settings, "recording_disclosure_enabled", True)
    monkeypatch.setattr(twh, "RECORDING_DISCLOSURE_PCM48_BYTES", clip)
    sent: list[bytes] = []

    class _FakeBridge:
        async def send_to_contact(self, user_id: int, chunk: bytes) -> None:
            sent.append(chunk)

    ws_closed = asyncio.Event()
    if closed:
        ws_closed.set()
    asyncio.run(twh._play_contact_disclosure(_FakeBridge(), 42, ws_closed))
    return sent


def test_contact_disclosure_plays_all_10ms_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clip = bytes(960 * 4)  # 4 exact 10 ms PCM48 chunks
    sent = _collect_disclosure(monkeypatch, clip, closed=False)
    assert len(sent) == 4
    assert all(len(c) == 960 for c in sent)


def test_contact_disclosure_bails_when_ws_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _collect_disclosure(monkeypatch, bytes(960 * 4), closed=True)
    assert sent == []


def test_contact_disclosure_noop_when_recordings_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(twh.settings, "recordings_enabled", False)
    monkeypatch.setattr(
        twh, "RECORDING_DISCLOSURE_PCM48_BYTES", bytes(960 * 4)
    )

    class _Boom:
        async def send_to_contact(self, user_id: int, chunk: bytes) -> None:
            raise AssertionError("must not send when recordings are off")

    asyncio.run(twh._play_contact_disclosure(_Boom(), 42, asyncio.Event()))
