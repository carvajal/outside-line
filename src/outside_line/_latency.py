"""Tiny ring-buffer + percentile helpers for bridge-phase latency telemetry.

Imported by ``twilio_handler`` and ``telegram_bridge`` to collect per-frame
latency / interval / stopwatch samples and emit p50 / p95 / max once per
second during BRIDGE. No numpy — kept import-light because both call
sites are on the audio hot path.

Three shapes:

- ``LatencyBuffer`` — raw ms samples (``.add(ms)``). The primitive.
- ``StopwatchBuffer`` — ``.record(start_ns)`` computes ``monotonic_ns() -
  start_ns`` in ms and stores. For per-call durations.
- ``IntervalBuffer`` — ``.tick(now_ns)`` computes the gap since the previous
  tick in ms. First tick is a no-op (just anchors). For inter-arrival jitter.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field


class LatencyBuffer:
    """Fixed-capacity ring buffer of latency samples (ms) with p50/p95/max."""

    __slots__ = ("_samples",)

    def __init__(self, max_samples: int = 1000) -> None:
        self._samples: deque[float] = deque(maxlen=max_samples)

    def add(self, ms: float) -> None:
        self._samples.append(float(ms))

    def extend(self, ms_iter: Iterable[float]) -> None:
        for ms in ms_iter:
            self._samples.append(float(ms))

    def count(self) -> int:
        return len(self._samples)

    def p50(self) -> float:
        return self._percentile(0.50)

    def p95(self) -> float:
        return self._percentile(0.95)

    def max(self) -> float:
        if not self._samples:
            return 0.0
        return max(self._samples)

    def reset(self) -> None:
        self._samples.clear()

    def _percentile(self, q: float) -> float:
        n = len(self._samples)
        if n == 0:
            return 0.0
        ordered = sorted(self._samples)
        idx = int(q * (n - 1))
        return ordered[idx]


class StopwatchBuffer:
    """Per-call duration samples in ms, sourced from ``time.monotonic_ns()``.

    Wraps ``LatencyBuffer``. Use ``record(start_ns)`` at the end of the
    measured region — it computes ``(monotonic_ns() - start_ns) / 1e6``
    and stores. ``add_ms`` is also exposed for the rare site that already
    has a duration in ms.
    """

    __slots__ = ("_buf",)

    def __init__(self, max_samples: int = 1000) -> None:
        self._buf = LatencyBuffer(max_samples=max_samples)

    def record(self, start_ns: int) -> None:
        self._buf.add((time.monotonic_ns() - start_ns) / 1e6)

    def add_ms(self, ms: float) -> None:
        self._buf.add(ms)

    def count(self) -> int:
        return self._buf.count()

    def p50(self) -> float:
        return self._buf.p50()

    def p95(self) -> float:
        return self._buf.p95()

    def max(self) -> float:
        return self._buf.max()

    def reset(self) -> None:
        self._buf.reset()


class IntervalBuffer:
    """Gap-between-ticks samples in ms.

    Designed for inter-arrival jitter — call ``tick(now_ns)`` on every
    event of interest. The first tick anchors and adds nothing; each
    subsequent tick stores ``(now_ns - last_ns) / 1e6``. ``reset()``
    clears both the samples and the anchor so the next ``tick()`` is
    again a no-op.
    """

    __slots__ = ("_buf", "_last_ns")

    def __init__(self, max_samples: int = 1000) -> None:
        self._buf = LatencyBuffer(max_samples=max_samples)
        self._last_ns: int | None = None

    def tick(self, now_ns: int) -> None:
        if self._last_ns is not None:
            self._buf.add((now_ns - self._last_ns) / 1e6)
        self._last_ns = now_ns

    def count(self) -> int:
        return self._buf.count()

    def p50(self) -> float:
        return self._buf.p50()

    def p95(self) -> float:
        return self._buf.p95()

    def max(self) -> float:
        return self._buf.max()

    def reset(self) -> None:
        self._buf.reset()
        self._last_ns = None


@dataclass
class BridgeMetrics:
    """Per-bridge-phase metric state shared between twilio_handler and
    telegram_bridge. Owned by ``_run_bridge_phase``; threaded through
    ``TelegramBridge.register_callbacks(..., bridge_metrics=...)`` so
    ``_on_stream_frame`` can record callback cadence + on the first
    frame emit the cold-start ``phase.bridge.setup_timeline`` log.

    Lives here (not in twilio_handler) to avoid a circular import:
    telegram_bridge does not depend on twilio_handler, and twilio_handler
    already depends on telegram_bridge.

    Stamps in ns (``time.monotonic_ns()``):
      - ``ring_started_ns`` — copied from the WS-lifetime ctx at bridge
        construction, snapshot of when ``phase.ring.started`` fired.
      - ``bridge_started_ns`` — at bridge phase construction.
      - ``first_frame_ns`` — set by ``_on_stream_frame`` on first call.
      - ``first_send_ns`` — set by ``pump_to_telegram`` after the first
        successful ``send_to_contact`` pair.
    """

    ring_started_ns: int = 0
    bridge_started_ns: int = 0
    first_frame_ns: int = 0
    first_send_ns: int = 0
    tg_callback_interval: IntervalBuffer = field(default_factory=IntervalBuffer)
    tg_frames_per_callback: StopwatchBuffer = field(default_factory=StopwatchBuffer)
    tg_bytes_per_callback: StopwatchBuffer = field(default_factory=StopwatchBuffer)
    ws_send_to_twilio: StopwatchBuffer = field(default_factory=StopwatchBuffer)
    pcm_to_mulaw: StopwatchBuffer = field(default_factory=StopwatchBuffer)
    tg_callbacks_total: int = 0
