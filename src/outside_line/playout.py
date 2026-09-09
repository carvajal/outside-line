"""Playout tracking for the Twilio media stream (ADR 0020).

Answers the one question the floor gate needs and OpenAI cannot: *when has
the caller actually finished hearing the agent?* OpenAI's ``response.done``
fires at generation-complete, but Twilio buffers outbound audio and plays
it at 1× — on long answers the audible tail outlives the response by tens
of seconds (measured 16–27 s on real calls with dual-channel
recordings). ``RealtimeSession._agent_has_floor`` consults
``is_draining()`` so the input gate stays closed until playback truly ends.

Two mechanisms, deliberately layered:

- **Primary — Twilio ``mark`` echo (event-based, fail-closed).** After a
  response's last audio chunk we send a ``mark`` frame; Twilio echoes it
  back once the audio queued before it has *played*. A delayed echo only
  means extra muteness, never a leak.
- **Backstop — byte-count playout clock (fail-open, bounded).** μ-law is
  exactly ``ULAW_BYTES_PER_S`` bytes per second of playback, so counting
  forwarded bytes predicts the drain deadline with no model-speed term at
  all. If an echo hasn't arrived by ``deadline + grace`` it is provably
  lost and the floor opens anyway. The per-echo ``echo_delta_ms`` log line
  tracks how the two mechanisms agree in the wild.

One instance per media WebSocket — it outlives the per-phase
``RealtimeSession`` rebuilds (RING/BRIDGE reconnects), so each AGENT phase
entry calls ``reset()``.

**Strip-away path** (kept deliberately shallow): to retire this — true
barge-in, or a full-duplex realtime model — delete this module, the one
``is_draining()`` term in ``_agent_has_floor``, and the mark plumbing in
``twilio_handler``; nothing else knows it exists.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from .log import get_logger

log = get_logger(__name__)

# Twilio Media Streams outbound audio is 8 kHz μ-law: 1 byte per sample,
# 8 000 bytes per second of playback. Every deadline computation below
# hangs off this constant — if the outbound format ever changes (e.g.
# L16/16k in the <Stream> TwiML), this constant AND the mark-vs-clock
# telemetry baselines must change with it.
ULAW_BYTES_PER_S = 8000

SendMarkFn = Callable[[str], Awaitable[None]]


class PlayoutTracker:
    """Tracks Twilio-side playback of the agent's audio; see module docstring."""

    def __init__(
        self,
        *,
        send_mark: SendMarkFn,
        grace_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send_mark = send_mark
        self._grace_s = grace_s
        self._clock = clock
        # Predicted moment (clock time) Twilio's playout buffer drains.
        self._deadline: float = clock()
        # Outbound bytes since the last armed mark — zero means the next
        # response_ended() has no audio of its own and must not arm.
        self._bytes_since_mark: int = 0
        # Armed marks: name -> deadline snapshot at arm time. Non-empty
        # (and not expired) == the caller is still hearing the agent.
        self._pending: dict[str, float] = {}
        # Tombstones for grace-expired marks so a late echo still yields
        # its delta telemetry instead of logging as "unknown".
        self._expired: dict[str, float] = {}
        # Monotonic serial so mark names stay unique even if OpenAI ever
        # hands us duplicate/absent response ids.
        self._seq: int = 0

    def note_sent(self, n_bytes: int) -> None:
        """Book one outbound μ-law chunk. Called per chunk by the
        ``send_to_twilio`` closure, before the WS send."""
        now = self._clock()
        # max(now, ·): if the buffer already drained (deltas slower than
        # real time, or idle gap), playback of this chunk starts now, not
        # at the stale deadline. This keeps the clock exact regardless of
        # how fast the model generates.
        self._deadline = max(now, self._deadline) + n_bytes / ULAW_BYTES_PER_S
        self._bytes_since_mark += n_bytes

    async def response_ended(self, response_id: str | None) -> None:
        """Arm a mark for the response whose audio just finished
        generating. No-op when the response produced no audio (tool
        hand-offs with ``speak=False``, cancelled/failed responses).
        Never raises — this runs inside the Realtime receive loop.
        """
        if self._bytes_since_mark == 0:
            return
        self._seq += 1
        name = f"resp-end-{self._seq}-{response_id or 'noid'}"
        audio_s = self._bytes_since_mark / ULAW_BYTES_PER_S
        self._bytes_since_mark = 0
        # Record BEFORE sending: a failed send degrades to clock+grace
        # release (fail-closed), never to an instantly-open gate.
        self._pending[name] = self._deadline
        try:
            await self._send_mark(name)
        except Exception:
            log.exception("playout.mark.send_failed", mark_name=name)
            return
        log.info(
            "playout.mark.sent",
            mark_name=name,
            response_audio_s=round(audio_s, 2),
            drain_eta_s=round(self._deadline - self._clock(), 2),
            pending_marks=len(self._pending),
        )

    def mark_echoed(self, name: str) -> None:
        """Twilio confirmed playback reached this mark. Idempotent:
        duplicate, expired, or foreign names are no-ops with a log."""
        now = self._clock()
        snapshot = self._pending.pop(name, None)
        if snapshot is not None:
            log.info(
                "playout.mark.echo",
                mark_name=name,
                echo_delta_ms=int((now - snapshot) * 1000),
                pending_marks=len(self._pending),
            )
            return
        snapshot = self._expired.pop(name, None)
        if snapshot is not None:
            log.warning(
                "playout.mark.echo_after_expiry",
                mark_name=name,
                echo_delta_ms=int((now - snapshot) * 1000),
            )
            return
        log.info("playout.mark.unknown", mark_name=name)

    def is_draining(self) -> bool:
        """True while the caller is still hearing already-sent audio.
        Expires marks whose echo is provably lost (clock + grace)."""
        now = self._clock()
        for name, snapshot in list(self._pending.items()):
            if now > snapshot + self._grace_s:
                del self._pending[name]
                self._expired[name] = snapshot
                log.warning(
                    "playout.mark.lost",
                    mark_name=name,
                    overdue_ms=int((now - snapshot - self._grace_s) * 1000),
                )
        return bool(self._pending)

    @property
    def pending_marks(self) -> int:
        return len(self._pending)

    def reset(self) -> None:
        """Forget all playout state. Called on AGENT phase entry (RING /
        BRIDGE audio flows through the same closure and would inflate the
        clock) and after a Twilio ``clear`` (buffer discarded; Twilio
        echoes any pending marks, which then hit the idempotent path)."""
        if self._pending or self._expired:
            log.info(
                "playout.reset",
                pending_marks=len(self._pending),
                tombstones=len(self._expired),
            )
        self._pending.clear()
        self._expired.clear()
        self._deadline = self._clock()
        self._bytes_since_mark = 0
