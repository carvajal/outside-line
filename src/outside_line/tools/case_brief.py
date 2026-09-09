"""`case_brief_lookup` tool.

Answers questions about the legal case described in the operator-
authored case brief. Grounded in two sources, fetched on every call:

* The curated brief at ``settings.case_brief_path`` — operator-
  authored markdown, gitignored, lives on the deployment volume.
* The live CourtListener Atom feed at
  ``settings.case_brief_docket_feed_url`` (optional — blank skips the
  feed) — public, no auth, structured XML with one ``<entry>`` per
  docket filing.

The two grounded sources are spliced into a Responses-API ``instructions``
prompt; the caller's question goes into ``input``. **No tools** registered
on the Responses call — we provide all the data ourselves, so the model
isn't free-roaming the web. Returns a 2–3 sentence spoken-style summary
or a short apology. Never raises.

Why a dedicated tool instead of tilting ``search_web``: docs/decisions/
0007-case-brief-lookup-tool.md.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import httpx
from openai import AsyncOpenAI

from ..config import settings
from ..log import get_logger
from ._speech import FAILURE_REPLY, LANG_NAMES, log_bridge_vocab_hits, strip_citations

log = get_logger(__name__)

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# 5 s feed timeout — half the total tool budget. On miss we fall back
# to brief-only rather than dead-air the call.
_FEED_TIMEOUT_S = 5.0

# Cap how many feed entries we splice into the prompt. The feed returns
# at most a few dozen; the most-recent N is plenty for "what's new"
# questions and keeps the prompt cheap. Entries past this are silently
# dropped (they're already in the brief's table anyway).
_FEED_MAX_ENTRIES = 20

# Stable User-Agent so CourtListener can identify the source if they
# ever care to throttle (they ask nicely for one in their docs).
_FEED_USER_AGENT = "outside-line/0.1"

# Title shape from CourtListener: "Entry #N in <case caption>, <docket>".
# Pull the N so we can say "entry #11" rather than reading the whole title.
_ENTRY_NUM_RE = re.compile(r"Entry\s*#(\d+)", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class _DocketEntry:
    """One row from the CourtListener Atom feed."""

    entry_no: str          # "11" or "" if the title is a minute-entry shape
    date: str              # "2026-06-01"
    title: str             # raw <title>, e.g. "Entry #11 in ..."
    summary: str           # raw <summary>, e.g. "Order to Show Cause"


# Module-level brief cache. Read once at first call; subsequent calls
# reuse. A process restart picks up edits — fine for the Railway-deploy
# cadence we have, where the brief changes hours-to-days, not minutes.
_brief_cache: str | None = None


def _load_brief() -> str | None:
    """Read the brief from disk, cached. Returns None if the file is
    missing — the tool then degrades gracefully rather than fabricating
    grounded content."""
    global _brief_cache
    if _brief_cache is not None:
        return _brief_cache
    path: Path = settings.case_brief_path
    try:
        _brief_cache = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("case_brief_lookup.brief_missing", brief_path=str(path))
        return None
    except OSError:
        log.exception("case_brief_lookup.brief_read_failed", brief_path=str(path))
        return None
    return _brief_cache


async def _fetch_feed_entries() -> list[_DocketEntry]:
    """Pull the Atom feed and parse it into ``_DocketEntry`` rows. Empty
    list on any error — the tool can still answer from the brief alone."""
    url = settings.case_brief_docket_feed_url
    if not url:
        return []
    try:
        async with httpx.AsyncClient(
            timeout=_FEED_TIMEOUT_S, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers={"User-Agent": _FEED_USER_AGENT})
    except httpx.HTTPError:
        log.exception("case_brief_lookup.feed_unreachable", url=url)
        return []
    if resp.status_code != 200:
        log.warning(
            "case_brief_lookup.feed_bad_status",
            url=url,
            status=resp.status_code,
        )
        return []
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        log.exception("case_brief_lookup.feed_parse_failed", url=url)
        return []

    entries: list[_DocketEntry] = []
    for e in root.findall("a:entry", _ATOM_NS):
        title = (e.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
        summary = (
            e.findtext("a:summary", default="", namespaces=_ATOM_NS) or ""
        ).strip()
        published = (
            e.findtext("a:published", default="", namespaces=_ATOM_NS) or ""
        ).strip()
        m = _ENTRY_NUM_RE.search(title)
        entries.append(
            _DocketEntry(
                entry_no=m.group(1) if m else "",
                date=published[:10],
                title=title,
                summary=summary,
            )
        )
    return entries[:_FEED_MAX_ENTRIES]


def _format_feed_entries(entries: list[_DocketEntry]) -> str:
    """Render feed rows as a markdown-ish list for the Responses-API
    prompt. Format favors the entry # + summary, which is the actionable
    bit for "what's new" questions."""
    if not entries:
        return "(no live docket entries available — fall back to the brief above)"
    lines: list[str] = []
    for e in entries:
        label = f"Entry #{e.entry_no}" if e.entry_no else "Minute entry"
        date = e.date or "unknown date"
        summary = e.summary or e.title or "(no summary)"
        lines.append(f"- {date} · {label}: {summary}")
    return "\n".join(lines)


def _build_instructions(
    brief: str | None, feed_entries: list[_DocketEntry], language: str | None
) -> str:
    """Assemble the grounded prompt: header + brief + live docket."""
    lang_name = LANG_NAMES.get((language or "").lower(), "")
    lang_clause = (
        f"HARD CONSTRAINT: Reply in {lang_name}. Do NOT translate to any "
        "other language regardless of what the question language is."
        if lang_name
        else "Reply in the same language as the question. Do NOT translate."
    )
    brief_section = brief if brief else (
        "(The case brief is unavailable. Tell the caller plainly that "
        "you don't have the case file in front of you right now. Do NOT "
        "tell them to call a lawyer.)"
    )
    return (
        "You are a research helper feeding answers to a voice agent "
        f"named {settings.agent_name}, who is on a live phone call. The "
        "caller is asking about a legal case described in the brief "
        "below. Ground your answer ONLY in the brief and the live docket "
        "entries — do NOT invent facts, do NOT speculate. If the question "
        "is about something the brief does not cover, say so plainly "
        "('I don't have that right now') and stop there. Do NOT recommend "
        "calling a lawyer, do NOT say 'I'm not a lawyer', do NOT add any "
        "disclaimer about not being qualified — the caller already knows "
        f"{settings.agent_name} is a friend, not a lawyer, and that "
        "preamble is wasted speech.\n"
        f"{lang_clause}\n"
        "Keep the answer to 2–3 short sentences, spoken-style, plain prose. "
        "Do NOT include markdown, URLs, domain names, citation links, "
        "parenthetical sources, or lists — your output is read aloud as-is, "
        "so any URL or bracket text would be spoken character-by-character. "
        "If the live docket has an entry the brief's table does not list, "
        "treat that as news and mention it.\n\n"
        "# CASE BRIEF\n\n"
        f"{brief_section}\n\n"
        "# LIVE DOCKET ENTRIES (fresh from CourtListener, most-recent first)\n\n"
        f"{_format_feed_entries(feed_entries)}\n"
    )


async def case_brief_lookup(question: str, language: str | None = None) -> str:
    """Answer a question about the legal case in 2–3 spoken-style
    sentences in ``language`` (ISO-639-1 hint — ``"en"`` or ``"es"``).

    Pulls the brief from disk + the CourtListener Atom feed, splices both
    into a Responses-API prompt, and returns the model's reply. On
    timeout, missing brief, or any other failure returns a short apology
    The agent can speak verbatim — never raises.
    """
    brief = _load_brief()
    feed_entries = await _fetch_feed_entries()
    instructions = _build_instructions(brief, feed_entries, language)

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        async with asyncio.timeout(settings.case_brief_lookup_timeout_s):
            resp = await client.responses.create(
                model=settings.openai_search_model,
                instructions=instructions,
                input=question,
            )
    except TimeoutError:
        log.warning(
            "case_brief_lookup.timeout",
            question=question,
            language=language,
            timeout_s=settings.case_brief_lookup_timeout_s,
        )
        return FAILURE_REPLY
    except Exception:
        log.exception(
            "case_brief_lookup.failed", question=question, language=language
        )
        return FAILURE_REPLY

    raw = (getattr(resp, "output_text", "") or "").strip()
    text = strip_citations(raw)
    if not text:
        log.warning(
            "case_brief_lookup.empty_output",
            question=question,
            language=language,
        )
        return FAILURE_REPLY
    log_bridge_vocab_hits(text, "case_brief_lookup")
    log.info(
        "case_brief_lookup.ok",
        question=question,
        language=language,
        feed_entries_count=len(feed_entries),
        brief_available=brief is not None,
        output_len=len(text),
        stripped_chars=len(raw) - len(text),
        output=text,
    )
    return text
