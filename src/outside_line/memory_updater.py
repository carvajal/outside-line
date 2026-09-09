"""Post-call LLM-merge for the per-caller memory file.

Fires as a background task from :mod:`outside_line.twilio_handler` after
the WS closes. Reads the call transcript at
``data/transcripts/<call_sid>.jsonl``, splices it with the existing
memory at ``data/memory/<phone>.md``, asks a small Responses-API model
whether the memory should change, and writes the new file back if so.

Cost is sub-cent per call (small model, ~500 input tokens median).
Runs after the call ends — never on the call's critical path.

Fail-open everywhere: a missing transcript, a too-short call, an
upstream API error, a malformed response, all just log and return.
Memory is enrichment; missing it never breaks future calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from openai import AsyncOpenAI

from .caller_memory import load_memory, save_memory
from .config import settings
from .log import get_logger

log = get_logger(__name__)

# Sentinel returned by the model to mean "no update needed". Compared
# with startswith so trailing whitespace / punctuation doesn't defeat
# the no-op path.
_UNCHANGED = "UNCHANGED"

# Hard ceiling on the merge call. The merge is small and self-contained
# — if it takes longer than this something is wrong upstream.
_TIMEOUT_S = 30.0

# Cap the transcript bytes we send. A 30-minute call could in principle
# write more than this, but in practice calls are short; truncating to
# the tail (most recent) protects token budget without losing signal.
_MAX_TRANSCRIPT_CHARS = 12000

_PROMPT_TEMPLATE = """\
You maintain a short per-caller memory note for a voice agent named
{agent_name}. The memory captures STABLE facts about the caller —
identity, location, family relationships, communication style,
recurring concerns, important dates. It is read by {agent_name} at the
start of every future call, to greet and treat the person with
continuity.

Rules:
- Keep the memory under 25 lines of plain markdown.
- Preserve existing facts unless directly contradicted in the call.
- Capture stable facts only — what someone *is* and what matters to
  them long-term. NOT one-off topics, one-off questions, or chitchat.
- ONE exception to "no one-off topics": if the call discussed news or
  current events ({agent_name} looked something up on the web), record the
  topics briefly WITH the date under a "## News already discussed"
  section — e.g. "- 2026-07-04: citywide blackout; election results".
  Keep only the ~8 most recent lines there, dropping older ones. This
  section exists so the NEXT call surfaces new news instead of the same
  headlines; it is fed back into the web-search step, not read aloud.
- Prefer concise bullets ("- Location: Springfield, IL") over prose.
- Write in the language of the existing memory if one exists; default
  to the language the call was in.

If this call did NOT surface any new stable facts worth remembering,
respond with the single token: {unchanged}

Otherwise, respond with the FULL updated memory note, ready to be
written to disk as-is. Do not add commentary, do not wrap in code
fences, do not preface with "Here is the updated memory:".

## Existing memory

{existing}

## Transcript of the call that just ended

{transcript}
"""


def _format_transcript(transcript_path: Path) -> tuple[str, int]:
    """Return ``(rendered, caller_utterance_count)``.

    ``rendered`` is a plain-text view of the JSONL — one line per row,
    ``role: text`` shape — truncated from the head if it exceeds the
    char cap (tail-preserve so the freshest context survives).
    """
    lines: list[str] = []
    caller_count = 0
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                role = row.get("role")
                text = row.get("text") or ""
                if role == "caller":
                    caller_count += 1
                if role in ("caller", "agent", "tool", "system"):
                    lines.append(f"{role}: {text}")
    except FileNotFoundError:
        return "", 0

    rendered = "\n".join(lines)
    if len(rendered) > _MAX_TRANSCRIPT_CHARS:
        rendered = "[…truncated…]\n" + rendered[-_MAX_TRANSCRIPT_CHARS:]
    return rendered, caller_count


async def maybe_update_memory(
    phone: str, call_sid: str, transcript_path: Path
) -> None:
    """Possibly rewrite ``data/memory/<phone>.md`` based on the transcript.

    Always safe to call from ``asyncio.create_task``: never raises,
    never blocks longer than ``_TIMEOUT_S``, logs every outcome
    (skipped / unchanged / completed / failed) so the operator can
    audit from the application logs.
    """
    try:
        await _maybe_update_memory_inner(phone, call_sid, transcript_path)
    except Exception:
        log.exception(
            "memory.update.failed",
            phone=phone,
            call_sid=call_sid,
        )


async def _maybe_update_memory_inner(
    phone: str, call_sid: str, transcript_path: Path
) -> None:
    if not transcript_path.exists():
        log.info(
            "memory.update.skipped_no_transcript",
            phone=phone,
            call_sid=call_sid,
            transcript_path=str(transcript_path),
        )
        return

    rendered, caller_count = _format_transcript(transcript_path)
    if caller_count < settings.caller_memory_min_utterances:
        log.info(
            "memory.update.skipped_short",
            phone=phone,
            call_sid=call_sid,
            caller_utterances=caller_count,
            min_required=settings.caller_memory_min_utterances,
        )
        return

    existing = load_memory(phone) or "(none)"
    prompt = _PROMPT_TEMPLATE.format(
        agent_name=settings.agent_name,
        unchanged=_UNCHANGED,
        existing=existing,
        transcript=rendered,
    )

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        async with asyncio.timeout(_TIMEOUT_S):
            resp = await client.responses.create(
                model=settings.caller_memory_model,
                input=prompt,
            )
    except TimeoutError:
        log.warning(
            "memory.update.timeout",
            phone=phone,
            call_sid=call_sid,
            timeout_s=_TIMEOUT_S,
        )
        return

    raw = (getattr(resp, "output_text", "") or "").strip()
    if not raw:
        log.warning(
            "memory.update.empty_output",
            phone=phone,
            call_sid=call_sid,
        )
        return

    if raw.startswith(_UNCHANGED):
        log.info(
            "memory.update.unchanged",
            phone=phone,
            call_sid=call_sid,
            caller_utterances=caller_count,
        )
        return

    save_memory(phone, raw)
    log.info(
        "memory.update.completed",
        phone=phone,
        call_sid=call_sid,
        caller_utterances=caller_count,
        bytes_written=len(raw),
        had_existing=(existing != "(none)"),
    )
