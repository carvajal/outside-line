"""Contact lookup with ranked fuzzy match.

Source of truth is ``data/contacts.json`` (gitignored, operator-edited, PII).
``find_contacts("marco")`` returns the ranked list of candidates; the
singular ``find_contact`` is a thin wrapper around the top result. Both
fold accents + case via NFKD → ASCII → casefold; matching considers the
contact's ``first_name``, ``aliases``, and free-form ``keywords`` (e.g.
``["brother", "mom"]``) so the caller can say "call my brother" without
the persona needing to know the registered name.

Resolution is **username-only** (ADR 0011): ``telegram_username`` is the
identifier the Telegram bridge passes to ``Telethon.get_entity``.
Phone-based lookup was retired because contacts who hide their phone
number on Telegram aren't resolvable by phone even via
``ImportContactsRequest``.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Contact:
    first_name: str
    aliases: tuple[str, ...]
    telegram_username: str
    keywords: tuple[str, ...] = ()


_DEFAULT_PATH = Path("data/contacts.json")
_cached: list[Contact] | None = None

# Scoring rubric — see plan. Higher is better, contacts with score 0 are
# dropped. Per-token contribution; per-contact score is the sum of the
# best contribution per query token.
_SCORE_FIRST_NAME_EXACT = 100
_SCORE_ALIAS_EXACT = 90
_SCORE_KEYWORD_EXACT = 70
_SCORE_FIRST_NAME_PREFIX = 60
_SCORE_ALIAS_PREFIX = 50
_SCORE_FIRST_NAME_SUBSTRING = 35
_SCORE_KEYWORD_SUBSTRING = 25
_MIN_PREFIX_LEN = 2
_MIN_SUBSTRING_LEN = 3


def _fold(s: str) -> str:
    return (
        unicodedata.normalize("NFKD", s)
        .encode("ascii", "ignore")
        .decode()
        .casefold()
        .strip()
    )


def load_contacts(path: Path = _DEFAULT_PATH) -> list[Contact]:
    """Read contacts from JSON. Missing file → [] (fail-soft, logged).

    Entries lacking a ``telegram_username`` are skipped (with a warning)
    rather than failing the whole load — the contact directory should
    survive a partially-edited JSON file so the rest of the agent's call
    surface stays online.
    """
    if not path.exists():
        log.warning("contacts.file_missing", path=str(path))
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[Contact] = []
    for entry in raw.get("contacts", []):
        handle = entry.get("telegram_username")
        if not handle:
            log.warning(
                "contacts.entry_skipped",
                first_name=entry.get("first_name"),
                reason="missing telegram_username",
            )
            continue
        out.append(
            Contact(
                first_name=entry["first_name"],
                aliases=tuple(entry.get("aliases", [])),
                telegram_username=handle,
                keywords=tuple(entry.get("keywords", [])),
            )
        )
    return out


def _score_token(token: str, contact: Contact) -> int:
    """Best per-token score for one contact."""
    fn = _fold(contact.first_name)
    aliases = [_fold(a) for a in contact.aliases]
    keywords = [_fold(k) for k in contact.keywords]

    if token == fn:
        return _SCORE_FIRST_NAME_EXACT
    if token in aliases:
        return _SCORE_ALIAS_EXACT
    if token in keywords:
        return _SCORE_KEYWORD_EXACT
    if len(token) >= _MIN_PREFIX_LEN and fn.startswith(token):
        return _SCORE_FIRST_NAME_PREFIX
    if len(token) >= _MIN_PREFIX_LEN and any(a.startswith(token) for a in aliases):
        return _SCORE_ALIAS_PREFIX
    if len(token) >= _MIN_SUBSTRING_LEN and token in fn:
        return _SCORE_FIRST_NAME_SUBSTRING
    if len(token) >= _MIN_SUBSTRING_LEN and any(token in k for k in keywords):
        return _SCORE_KEYWORD_SUBSTRING
    return 0


def find_contacts(
    query: str,
    contacts: list[Contact] | None = None,
) -> list[Contact]:
    """Return contacts ranked by match quality against ``query``.

    Higher score is better; contacts with score 0 are dropped. Query is
    tokenized on whitespace; each token contributes the best per-contact
    score it can earn. Ties break on contacts.json order (stable sort).
    """
    if not query:
        return []
    pool = contacts if contacts is not None else _get_cached()
    if not pool:
        return []

    tokens = [tok for tok in (_fold(t) for t in query.split()) if tok]
    if not tokens:
        return []

    scored: list[tuple[int, int, Contact]] = []
    for idx, c in enumerate(pool):
        total = sum(_score_token(tok, c) for tok in tokens)
        if total > 0:
            scored.append((total, idx, c))

    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored]


def find_contact(
    first_name: str,
    contacts: list[Contact] | None = None,
) -> Contact | None:
    """Top-ranked match for ``first_name``, or None.

    Thin wrapper over :func:`find_contacts` for the historical exact-name
    call sites and the existing pytest. Exact-name matches always win
    (highest score), so the legacy 6 cases are unchanged in behavior.
    """
    hits = find_contacts(first_name, contacts)
    return hits[0] if hits else None


def find_by_username(
    telegram_username: str,
    contacts: list[Contact] | None = None,
) -> Contact | None:
    """Resolve a telegram ``@username`` to its Contact, or None.

    Used by the directline path to map a configured DID's username back
    to the same Contact shape the Telegram bridge expects. Case-insensitive
    match against ``Contact.telegram_username``; the leading ``@`` is
    stripped on both sides.
    """
    handle = telegram_username.lstrip("@").casefold().strip()
    if not handle:
        return None
    pool = contacts if contacts is not None else _get_cached()
    for c in pool:
        if c.telegram_username.lstrip("@").casefold() == handle:
            return c
    return None


def _get_cached() -> list[Contact]:
    global _cached
    if _cached is None:
        _cached = load_contacts()
    return _cached


def clear_cache() -> None:
    """Drop the module-level cache (used by scripts after editing the JSON)."""
    global _cached
    _cached = None
