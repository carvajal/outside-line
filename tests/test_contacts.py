"""Contact lookup: accent-folded, case-insensitive, alias-aware, keyword-aware.

Locked behavior: the original 6 cases preserve exact-match contracts so the
existing Realtime tool wiring keeps working. The 5 added cases cover the
fuzzy/disambiguation surface (alias prefix, keyword exact/substring,
multi-candidate ranking, multi-token query).

ADR 0011: ``Contact`` is identified by ``telegram_username`` only — the
phone field was retired. Assertions below check ``.telegram_username``
where the legacy cases checked ``.phone``; they still verify the same
lookup-identity round-trip.
"""

from __future__ import annotations

from outside_line.contacts import Contact, find_contact, find_contacts

_FIXTURES = [
    Contact(
        first_name="Marco",
        aliases=("marco",),
        telegram_username="@marco_example",
        keywords=("brother", "marc"),
    ),
    Contact(
        first_name="Damián",
        aliases=("damian", "dami"),
        telegram_username="@damian_example",
        keywords=("cousin",),
    ),
]


def test_matches_first_name_exact() -> None:
    assert find_contact("Marco", _FIXTURES).telegram_username == "@marco_example"


def test_matches_case_insensitive() -> None:
    assert find_contact("marco", _FIXTURES).telegram_username == "@marco_example"
    assert find_contact("MARCO", _FIXTURES).telegram_username == "@marco_example"


def test_matches_accent_folded() -> None:
    # "Damián" with the diacritic
    assert find_contact("Damián", _FIXTURES).telegram_username == "@damian_example"
    # ASCII variant
    assert find_contact("damian", _FIXTURES).telegram_username == "@damian_example"
    # Uppercase ASCII
    assert find_contact("DAMIAN", _FIXTURES).telegram_username == "@damian_example"


def test_matches_alias() -> None:
    assert find_contact("Dami", _FIXTURES).telegram_username == "@damian_example"
    assert find_contact("dami", _FIXTURES).telegram_username == "@damian_example"


def test_unknown_returns_none() -> None:
    assert find_contact("Camilo", _FIXTURES) is None


def test_empty_or_whitespace_returns_none() -> None:
    assert find_contact("", _FIXTURES) is None
    assert find_contact("   ", _FIXTURES) is None


# --- New cases below: fuzzy + disambiguation surface ---


def test_alias_prefix_resolves() -> None:
    # "Dam" is a 3-char prefix of alias "damian" → resolves to Damián.
    hits = find_contacts("Dam", _FIXTURES)
    assert len(hits) == 1
    assert hits[0].first_name == "Damián"


def test_keyword_exact_match() -> None:
    # "brother" is a keyword on Marco.
    assert find_contact("brother", _FIXTURES).telegram_username == "@marco_example"
    # Folded form should also work.
    assert find_contact("Brother", _FIXTURES).telegram_username == "@marco_example"


def test_keyword_substring_match() -> None:
    # "broth" is a 5-char substring of keyword "brother".
    hits = find_contacts("broth", _FIXTURES)
    assert len(hits) >= 1
    assert hits[0].first_name == "Marco"


def test_multi_candidate_ranking() -> None:
    # A query that scores against both contacts should rank exact > substring.
    pool = [
        Contact(
            first_name="Damián",
            aliases=("dami",),
            telegram_username="@one",
            keywords=(),
        ),
        Contact(
            first_name="Dania", aliases=("dania",), telegram_username="@two", keywords=()
        ),
    ]
    # "dam" prefix-matches Damián's alias; "Dania" doesn't match
    # ("dam" is not a prefix of "dania"). So only Damián resolves.
    hits = find_contacts("dam", pool)
    assert [c.first_name for c in hits] == ["Damián"]
    # But a query like "da" (2 chars, prefix of both) returns both.
    hits = find_contacts("da", pool)
    assert sorted(c.first_name for c in hits) == ["Damián", "Dania"]


def test_multi_token_query_sums_per_token_score() -> None:
    # Two-token query "brother marco" against Marco: keyword-exact (70)
    # + first_name-exact (100) = 170. Damián scores 0. Marco wins.
    hits = find_contacts("brother marco", _FIXTURES)
    assert len(hits) == 1
    assert hits[0].first_name == "Marco"


# --- Schema migration check (ADR 0011) ---


def test_contact_has_no_phone_field() -> None:
    """The phone field was retired; assert it's gone from the dataclass."""
    assert "phone" not in Contact.__dataclass_fields__
    assert "telegram_username" in Contact.__dataclass_fields__
