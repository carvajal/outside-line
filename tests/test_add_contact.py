"""Unit tests for the pure logic in ``scripts/add_contact.py``.

Coverage is deliberately narrow: the CSV flag parsing and the
fuzzy-collision guard are the self-contained, non-obvious bits. The rest is a thin shell over the
operator CLIs and is validated by a real ``provision`` run.
"""

from __future__ import annotations

import sys
from pathlib import Path

# add_contact.py lives next to the operator CLIs, not in the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from add_contact import _collision_hits, _parse_csv

from outside_line.contacts import Contact


def test_parse_csv_trims_dedups_drops_empties() -> None:
    assert _parse_csv("Sam Reyes, Cousin of Ana ,,Sam Reyes") == [
        "Sam Reyes",
        "Cousin of Ana",
    ]
    assert _parse_csv("") == []
    assert _parse_csv(None) == []
    assert _parse_csv("  solo  ") == ["solo"]


# A stand-in contacts pool — names that look close enough to a new
# addition to exercise the collision guard.
_POOL = [
    Contact(
        first_name="Walter",
        aliases=("walter",),
        telegram_username="@walter_example",
        keywords=("nephew",),
    ),
    Contact(
        first_name="Roberto",
        aliases=("roberto", "rober"),
        telegram_username="@roberto_example",
        keywords=("reyes",),
    ),
]


def test_collision_clear_for_distinct_name() -> None:
    # The real case that motivated this guard: a visually-similar name
    # ("Wanda" vs "Walter") must NOT fuzzy-match — no shared 3-char
    # prefix, no substring, no exact token.
    assert _collision_hits("Wanda", ["Wanda Diaz"], _POOL) == []


def test_collision_flags_existing_name() -> None:
    assert "Roberto" in _collision_hits("Roberto", [], _POOL)
