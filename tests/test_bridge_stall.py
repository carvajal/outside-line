"""Unit tests for the bridge stall detector (`_stall_step`).

The detector tears a bridge down only when it is silent in BOTH
directions — a live contact leg means the call is up even if the
caller's inbound media paused.
"""

from __future__ import annotations

from outside_line.twilio_handler import _stall_step


def test_partial_streak_does_not_fire() -> None:
    """Two both-legs-silent windows then activity must reset the streak."""
    streak = 0
    streak, fired = _stall_step(0, 0, 0, streak, 3)
    assert (streak, fired) == (1, False)
    streak, fired = _stall_step(0, 0, 0, streak, 3)
    assert (streak, fired) == (2, False)
    # Frames flowing again — streak resets.
    streak, fired = _stall_step(50, 50, 100, streak, 3)
    assert (streak, fired) == (0, False)


def test_three_consecutive_silent_windows_fire() -> None:
    """Three consecutive all-silent windows at threshold=3 fire on the third."""
    streak = 0
    streak, fired = _stall_step(0, 0, 0, streak, 3)
    assert fired is False
    streak, fired = _stall_step(0, 0, 0, streak, 3)
    assert fired is False
    streak, fired = _stall_step(0, 0, 0, streak, 3)
    assert (streak, fired) == (3, True)


def test_threshold_zero_disables_firing() -> None:
    """`threshold=0` (operator opt-out) never fires even on many silent windows."""
    streak = 0
    for _ in range(100):
        streak, fired = _stall_step(0, 0, 0, streak, 0)
        assert fired is False
    assert streak == 100  # streak still tracked, just never trips


def test_any_leg_nonzero_resets() -> None:
    """Any one of the three signals being non-zero resets the streak."""
    assert _stall_step(0, 5, 0, 2, 3) == (0, False)  # caller inbound only
    assert _stall_step(5, 0, 0, 2, 3) == (0, False)  # forwarded to contact only
    # contact leg only (observed on a real call: caller inbound paused, contact still
    # streaming) — must reset, must NOT tear down.
    assert _stall_step(0, 0, 100, 2, 3) == (0, False)


def test_live_contact_leg_never_tears_down() -> None:
    """Regression for an observed false positive: caller inbound silent for many windows while
    the contact leg streams ~100 fps must never trip the detector."""
    streak = 0
    for _ in range(30):
        streak, fired = _stall_step(0, 0, 100, streak, 10)
        assert fired is False
        assert streak == 0


def test_both_legs_dead_fires_at_threshold_10() -> None:
    """A genuine both-way-dead bridge trips after 10 windows at threshold=10."""
    streak = 0
    for _ in range(9):
        streak, fired = _stall_step(0, 0, 0, streak, 10)
        assert fired is False
    streak, fired = _stall_step(0, 0, 0, streak, 10)
    assert (streak, fired) == (10, True)
