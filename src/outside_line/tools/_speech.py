"""Shared helpers for tools that produce text the agent reads aloud.

Anything that turns LLM output into something safe to splat into TTS
without it being spoken character-by-character lives here.
"""

from __future__ import annotations

import re

from ..log import get_logger

log = get_logger(__name__)

# ISO-639-1 → human-readable name for prompt building. Both tools take
# the caller's current language as a hint and hard-pin the reply to it.
LANG_NAMES: dict[str, str] = {"en": "English", "es": "Spanish"}

# Strip the inline citations OpenAI's tools keep appending despite
# instructions to the contrary: ` ([domain](https://...))`, ` (https://...)`,
# and bare URLs. Read aloud, any of these come out as
# "open paren h-t-t-p-s colon slash slash …".
_MD_CITATION_RE = re.compile(r"\s*\(\[[^\]]+\]\([^)]+\)\)")
_BARE_URL_PAREN_RE = re.compile(r"\s*\(https?://[^)]+\)")
_BARE_URL_RE = re.compile(r"https?://\S+")


def strip_citations(text: str) -> str:
    text = _MD_CITATION_RE.sub("", text)
    text = _BARE_URL_PAREN_RE.sub("", text)
    text = _BARE_URL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


# Routing-mechanic vocabulary that hints at how a call or message is
# technically routed. Tool output is read aloud, and the agent speaks in
# human terms ("I'll call them", "I'll pass the message") — mechanic
# vocabulary confuses callers. Banned from the agent's speech via the
# persona rule (see persona.py) and logged whenever a tool's output
# sneaks one through. The persona soft constraint is the primary
# enforcement; this is a passive guardrail for spotting regressions.
#
# Stems use \w* so -ing/-ed forms are caught. Multi-word phrases use
# \s+ to tolerate extra space.
BRIDGE_VOCAB_RE = re.compile(
    r"\b(?:"
    r"bridge\w*"                             # bridge / bridges / bridged / bridging
    r"|patch\s+(?:in|through)"
    r"|conference\s+in"
    r"|transfer\w*"                          # transfer / transferring / transferred
    r"|forward\w*"                           # forward / forwarding / forwarded
    r")\b",
    re.IGNORECASE,
)


def log_bridge_vocab_hits(text: str, tool: str) -> None:
    """Log a structured warning per bridge-vocab hit in ``text``.

    Passive guardrail: does not mutate ``text``. The persona soft
    constraint is the primary enforcement (see persona.py); a hit here
    means the model leaked routing-mechanic vocabulary into a tool
    output, which is a regression worth noticing on a per-call basis.
    """
    for m in BRIDGE_VOCAB_RE.finditer(text):
        log.warning(
            "tool.bridge_vocab.hit",
            tool=tool,
            term=m.group(0),
            text=text,
        )


# Spoken verbatim by the agent when a tool fails. Plain, warm register —
# matches the persona.
FAILURE_REPLY = "I couldn't look that up just now, sorry."
