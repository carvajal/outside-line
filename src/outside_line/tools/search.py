"""`search_web` tool.

Called from the Realtime receive loop when the agent decides it needs current
information. Delegates to the Responses API with the hosted `web_search`
tool — the hosted variant is **not** safe to register directly on a
Realtime session (it hangs there). Returns a short spoken-style summary string that the
caller posts back as a `function_call_output` so the agent speaks it.

The worker is steered for two observed failure modes (ADR 0024):
freshness + source diversity for "same headlines every call," and an honest
no-invented-fare rule for "fake flight prices." Live fares are structurally
out of reach for web search — a real quote needs a fares API (see docs/roadmap.md).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from ..config import settings
from ..log import get_logger
from ._speech import FAILURE_REPLY, LANG_NAMES, log_bridge_vocab_hits, strip_citations

log = get_logger(__name__)

# `search_context_size: high` is the single biggest freshness lever: it
# feeds the model more candidate sources so it can find *newer* / *different*
# stories instead of the same wire headlines.
_SEARCH_CONTEXT_SIZE = "high"


def today_local() -> str:
    """Today's date in the agent's configured timezone
    (``settings.agent_timezone``), as an ISO `YYYY-MM-DD` string.

    The caller cares about "today" in the agent's local time, not the
    UTC of the deployment host. Shared by the search worker (freshness)
    and the agent's session instructions (so it never states a stale
    date)."""
    return datetime.now(ZoneInfo(settings.agent_timezone)).date().isoformat()


def _web_search_tool() -> dict[str, object]:
    """The hosted `web_search` tool object, tuned for freshness.

    No `filters.allowed_domains`: that is a hard *restriction* (results
    come only from the listed domains), so any topical allowlist would
    cripple weather / sports / general lookups. Source diversity is
    steered in the prompt instead — see `_build_instructions`."""
    return {
        "type": "web_search",
        "search_context_size": _SEARCH_CONTEXT_SIZE,
    }


# Build instructions per call so we can hard-pin the reply language and
# inject today's date — we can't rely on the model to detect the language
# from the query alone (the agent sometimes translates the caller's English
# question to Spanish before passing it as `query`, which then makes the
# search reply in Spanish). The caller passes the language explicitly via
# the tool's `language` parameter, sourced from the Realtime model's
# awareness of what the caller actually said.
def _build_instructions(language: str | None, recent_context: str | None) -> str:
    lang_name = LANG_NAMES.get((language or "").lower(), "")
    lang_clause = (
        f"HARD CONSTRAINT: Reply in {lang_name}. Do NOT translate to any "
        "other language regardless of what the query language is."
        if lang_name
        else "Reply in the same language as the query. Do NOT translate."
    )
    # Told-topics avoidance: the caller's memory (which may carry a
    # "News already discussed" section written by the post-call merge) is
    # passed in so repeat callers get *new* news, not the same three
    # stories. Only steers news queries; harmless on everything else.
    avoid_clause = (
        (
            "\nCONTEXT — this person has already been told the following in "
            "previous calls (may include a 'News already discussed' list). "
            "For NEWS/current-events queries, prioritize something NEWER or "
            "DIFFERENT and do NOT repeat these unless there is genuinely new "
            "development. Use this only to avoid repeats — do not read it "
            f"back:\n{recent_context.strip()}\n"
        )
        if recent_context and recent_context.strip()
        else ""
    )
    return (
        "You are a research helper feeding answers to a voice agent "
        f"named {settings.agent_name}, who is on a live phone call. "
        f"Today is {today_local()}. "
        "Search in the language(s) most likely to have good sources "
        "for the query. "
        f"{lang_clause}\n"
        # Freshness
        "FRESHNESS: For news, weather, prices, scores, or anything about "
        "current events, prioritize sources from the last 48 hours and "
        "discard stale articles; say how recent the info is. For timeless "
        "questions (facts, definitions, history), recency does not matter.\n"
        # Source diversity
        "SOURCES: For news, vary across multiple reputable sources — "
        "primary outlets plus wire services. Do NOT keep returning the "
        "same headlines — look across several sources and surface what "
        "is newest.\n"
        # Flight-price honesty (real fares need a fares API)
        "FLIGHT PRICES & FARES: Web search CANNOT see live booking systems, "
        "so NEVER invent a specific price or quote a figure as if it were a "
        "confirmed fare. If you find a dated approximate range, give it and "
        "clearly mark it approximate ('it was around X, worth confirming'). "
        "If you don't, say honestly there is no exact price right now. A "
        "made-up fare is worse than an honest 'I don't have an exact "
        "price'.\n"
        f"{avoid_clause}"
        # Output shape — richer than before so the agent has the salient facts to
        # work with; it re-condenses to 2-3 spoken sentences per the persona.
        "OUTPUT: Give the concrete facts that make the answer useful — "
        "dates, figures, names, airline/price when relevant — in spoken-"
        "style plain prose. Be concise but do not drop the key fact. About "
        "5 sentences max. Do NOT include markdown, URLs, domain names, "
        "citation links, parenthetical sources, or lists — your output is "
        "read aloud as-is, so any URL or bracket text would be spoken "
        "character-by-character."
    )


async def search_web(
    query: str,
    language: str | None = None,
    recent_context: str | None = None,
) -> str:
    """Look up `query` on the web and return a spoken-style summary in
    `language` (ISO-639-1 hint — "en" or "es"; pass-through anything
    else and let the model decide). `recent_context` is the caller's
    memory text, passed so news queries can skip already-told headlines.
    On timeout or any error, return a short apology the agent can speak
    verbatim — never raises.

    The cleaned output is logged at INFO so we can debug
    hallucinations after a real call (citations are pre-stripped so
    the raw payload differs only by URL noise — not worth the bytes)."""
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    instructions = _build_instructions(language, recent_context)
    try:
        async with asyncio.timeout(settings.search_web_timeout_s):
            resp = await client.responses.create(
                model=settings.openai_search_model,
                tools=[_web_search_tool()],
                instructions=instructions,
                input=query,
            )
    except TimeoutError:
        log.warning(
            "search_web.timeout",
            query=query,
            language=language,
            timeout_s=settings.search_web_timeout_s,
        )
        return FAILURE_REPLY
    except Exception:
        log.exception("search_web.failed", query=query, language=language)
        return FAILURE_REPLY

    raw = (getattr(resp, "output_text", "") or "").strip()
    text = strip_citations(raw)
    if not text:
        log.warning("search_web.empty_output", query=query, language=language)
        return FAILURE_REPLY
    log_bridge_vocab_hits(text, "search_web")
    log.info(
        "search_web.ok",
        query=query,
        language=language,
        had_recent_context=bool(recent_context and recent_context.strip()),
        output_len=len(text),
        stripped_chars=len(raw) - len(text),
        output=text,
    )
    return text
