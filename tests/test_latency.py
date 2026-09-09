"""LatencyBuffer / StopwatchBuffer / IntervalBuffer behavior.

The percentile + interval math is the only part of bridge-phase telemetry
worth a unit test — everything else is plumbing verified by a real
bridge call.
"""

from __future__ import annotations

import time

from outside_line._latency import IntervalBuffer, LatencyBuffer, StopwatchBuffer


def test_empty_returns_zero() -> None:
    buf = LatencyBuffer()
    assert buf.count() == 0
    assert buf.p50() == 0.0
    assert buf.p95() == 0.0
    assert buf.max() == 0.0


def test_single_sample_p50_eq_p95() -> None:
    buf = LatencyBuffer()
    buf.add(42.0)
    assert buf.count() == 1
    assert buf.p50() == 42.0
    assert buf.p95() == 42.0
    assert buf.max() == 42.0


def test_known_sequence_p50_lt_p95() -> None:
    buf = LatencyBuffer()
    buf.extend(float(i) for i in range(1, 101))  # 1..100
    # nearest-rank: idx = floor(q * (n-1)) → q=0.5 → idx=49 → val=50;
    # q=0.95 → idx=94 → val=95.
    assert buf.p50() == 50.0
    assert buf.p95() == 95.0
    assert buf.max() == 100.0
    assert buf.count() == 100


def test_reset_clears_samples() -> None:
    buf = LatencyBuffer()
    buf.extend([10.0, 20.0, 30.0])
    buf.reset()
    assert buf.count() == 0
    assert buf.p50() == 0.0
    assert buf.max() == 0.0


def test_ring_buffer_drops_oldest_at_maxlen() -> None:
    buf = LatencyBuffer(max_samples=3)
    buf.extend([1.0, 2.0, 3.0, 4.0, 5.0])
    # Oldest two dropped; only 3,4,5 remain.
    assert buf.count() == 3
    # idx = floor(0.5 * 2) = 1 → second of [3,4,5] = 4.
    assert buf.p50() == 4.0
    # idx = floor(0.95 * 2) = 1 → also 4 with n=3.
    assert buf.p95() == 4.0
    # max should reflect only what's still in the ring.
    assert buf.max() == 5.0


def test_max_respects_ring_rotation_with_descending_input() -> None:
    """The biggest value can fall out of the window — max() must reflect
    only what's currently in the deque, not the all-time peak."""
    buf = LatencyBuffer(max_samples=3)
    buf.extend([100.0, 50.0, 40.0, 30.0, 20.0])
    # 100, 50 dropped; remaining [40, 30, 20] → max is 40.
    assert buf.max() == 40.0


def test_stopwatch_records_real_duration_ms() -> None:
    sw = StopwatchBuffer()
    start = time.monotonic_ns()
    # Spin briefly to guarantee a nonzero elapsed.
    while time.monotonic_ns() - start < 1_000_000:  # >= 1 ms
        pass
    sw.record(start)
    assert sw.count() == 1
    # >= 1 ms by construction; allow generous upper bound for slow CI.
    assert sw.p50() >= 1.0
    assert sw.p50() < 500.0


def test_stopwatch_add_ms_and_reset() -> None:
    sw = StopwatchBuffer()
    sw.add_ms(2.5)
    sw.add_ms(5.0)
    sw.add_ms(7.5)
    assert sw.count() == 3
    # n=3 nearest-rank: idx = floor(0.5 * 2) = 1 → middle = 5.0.
    assert sw.p50() == 5.0
    # idx = floor(0.95 * 2) = 1 → also 5.0 with n=3 (coarse).
    assert sw.p95() == 5.0
    assert sw.max() == 7.5
    sw.reset()
    assert sw.count() == 0
    assert sw.max() == 0.0


def test_interval_first_tick_is_noop() -> None:
    iv = IntervalBuffer()
    iv.tick(1_000_000_000)
    # Anchor only; no sample yet.
    assert iv.count() == 0
    assert iv.p50() == 0.0


def test_interval_subsequent_ticks_record_gap_ms() -> None:
    iv = IntervalBuffer()
    iv.tick(0)
    iv.tick(20_000_000)  # +20 ms
    iv.tick(40_000_000)  # +20 ms
    iv.tick(100_000_000)  # +60 ms
    assert iv.count() == 3
    # Sorted gaps: [20, 20, 60]; idx = floor(0.5 * 2) = 1 → 20.
    assert iv.p50() == 20.0
    # idx = floor(0.95 * 2) = 1 → 20 with n=3 (nearest-rank coarse).
    assert iv.p95() == 20.0
    assert iv.max() == 60.0


def test_interval_reset_clears_anchor_and_samples() -> None:
    iv = IntervalBuffer()
    iv.tick(0)
    iv.tick(20_000_000)
    iv.reset()
    assert iv.count() == 0
    # The next tick must be a fresh anchor (no-op), not a gap from the
    # pre-reset anchor.
    iv.tick(100_000_000)
    assert iv.count() == 0
    iv.tick(120_000_000)
    assert iv.count() == 1
    assert iv.p50() == 20.0
