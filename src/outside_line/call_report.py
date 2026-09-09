"""The call-processed report — one informational email per call, on call-end.

Triggered fire-and-forget from ``POST /twilio/voice/status`` (the single
universal call-ended signal — fires for blocked, connected, gate-hangup, and
crash-recovered calls alike; see ``docs/decisions/0022-call-processed-report.md``).
Reads the durable lifecycle journal (:mod:`outside_line.call_journal`) + the
transcript, reconstructs what happened deterministically, adds a one-shot LLM
topic/summary over the transcript (reusing the :mod:`outside_line.memory_updater`
pattern), and sends a clean HTML + plain-text email via Resend
(:func:`outside_line.alerts._post_resend`).

Exactly-once: an ``open(marker, "x")`` on the volume claims the send atomically,
so Twilio re-deliveries / racing callbacks can't double-email.

Fail-open everywhere: disabled/missing settings, a thin journal, an LLM error —
all just send what we have (or log + return). The report is enrichment; a
missing one never affects a call.
"""

from __future__ import annotations

import asyncio
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from . import alerts
from .call_journal import CallSummary, load_journal, summarize_lifecycle
from .call_journal import DEFAULT_DIR as CALLS_DIR
from .config import settings
from .log import get_logger
from .memory_updater import _format_transcript
from .transcripts import DEFAULT_DIR as TRANSCRIPTS_DIR

log = get_logger(__name__)

# Let the in-process teardown flush the final journal/transcript writes before
# we read them. Durations don't depend on this (they derive from segment
# starts), so a short settle is plenty; longer buys nothing — the caller-STT
# tail is lost with the OpenAI WS at teardown, not merely delayed (ADR 0024).
_SETTLE_S = 2.0

_TIMEOUT_S = 30.0

_PROMPT_TEMPLATE = """\
You are summarizing a phone call for the operator of a voice assistant named
{agent_name}. Below is the transcript of a call: "caller" is the person who
phoned in, "agent" is {agent_name}, "tool" lines are {agent_name}'s function
calls, "system" lines are internal notes. Write a concise, factual summary
FOR THE OPERATOR, in ENGLISH.

Return ONLY a JSON object (no code fences, no prose before or after) with
exactly these keys:
- "subject_topics": a 2-5 word lowercase phrase naming what the caller wanted
  or asked about, for an email subject line (e.g. "visa and weather",
  "wanted to reach Sam"). Empty string if nothing substantive was discussed.
- "summary_bullets": an array of 1-4 short strings, each one factual point
  about what happened or was discussed (past tense, terse, no fluff).
- "detail": a short plain-text paragraph (2-5 sentences) telling the fuller
  story for someone who wants to read more. Empty string if there's nothing
  beyond the bullets.

## Transcript
{transcript}
"""


def _fmt_dur(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    return f"{round(seconds / 60)}m"


def _mask_phone(phone: str | None) -> str:
    """Short phone form for the subject line: ``+1 555…`` for NANP numbers."""
    if not phone:
        return "unknown"
    digits = phone.lstrip("+")
    if phone.startswith("+1") and len(digits) >= 4:
        return f"+1 {digits[1:4]}…"
    return f"{phone[:6]}…" if len(phone) > 7 else phone


def _who(summary: CallSummary, *, mask: bool) -> str:
    if summary.label:
        return summary.label
    return (
        _mask_phone(summary.from_number) if mask else (summary.from_number or "unknown")
    )


def _clip(text: str, limit: int = 78) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _line_label(summary: CallSummary) -> str:
    if summary.line == "directline":
        return (
            f"direct line — {summary.line_label}"
            if summary.line_label
            else "direct line"
        )
    return "main line"


# ---------------------------------------------------------------------------
# Subject.


def _build_subject(summary: CallSummary, topics: str) -> str:
    who = _who(summary, mask=True)
    if summary.blocked:
        return _clip(f"{settings.agent_name} · {who} · blocked ({_line_label(summary)})")

    tokens: list[str] = []
    if summary.talked_to_agent:
        tokens.append(f"{settings.agent_name} {_fmt_dur(summary.agent_s)}")
    for c in summary.bridged_contacts:
        label = "direct line → " if c.via == "directline" else ""
        tokens.append(f"{label}{c.name} {_fmt_dur(c.seconds)}")
    for c in summary.no_answer_contacts:
        tokens.append(f"{c.name} no answer")

    headline = " + ".join(tokens) if tokens else "call"
    # Topics ride along only when the whole call was the agent (no contact leg
    # to crowd the line); with a bridge, durations carry the signal.
    only_agent = (
        summary.talked_to_agent
        and not summary.bridged_contacts
        and not summary.no_answer_contacts
    )
    if only_agent and topics:
        headline = f"{headline} — {topics}"
    return _clip(f"{settings.agent_name} · {who} · {headline}")


# ---------------------------------------------------------------------------
# Lifecycle bullets (shared by HTML + text).


def _lifecycle_lines(summary: CallSummary) -> list[str]:
    if summary.blocked:
        return [f"Blocked — not on the allow-list ({_line_label(summary)})"]
    lines: list[str] = []
    for seg in summary.segments:
        if seg.kind == "agent":
            lines.append(f"Talked to {settings.agent_name} — {_fmt_dur(seg.seconds)}")
        elif seg.outcome == "bridged":
            via = " (direct line)" if seg.via == "directline" else ""
            lines.append(f"Connected to {seg.name} — {_fmt_dur(seg.seconds)}{via}")
        elif seg.outcome == "no_answer":
            lines.append(f"Called {seg.name} — no answer")
    if not lines:
        lines.append("Call received; no conversation captured")
    if not summary.clean_end and not summary.blocked:
        lines.append("⚠ Call ended abnormally (dropped or crashed mid-call)")
    return lines


def _commands(summary: CallSummary, call_sid: str | None) -> list[tuple[str, str]]:
    """Return (description, command) pairs for the actions block."""
    cmds: list[tuple[str, str]] = []
    phone = summary.from_number
    if summary.blocked and phone:
        cmds.append(
            (
                "Allow this number (reaches the agent via the gate)",
                f"./scripts/callers.py allow {phone} on",
            )
        )
        cmds.append(
            ("Also skip the gate every call", f"./scripts/callers.py skip-gate {phone} on")
        )
        cmds.append(
            (
                "Label it for the registry",
                f"./scripts/callers.py label {phone} 'Someone'",
            )
        )
    if call_sid:
        cmds.append(("Recording + transcript", f"./scripts/archive.py play {call_sid}"))
    return cmds


# ---------------------------------------------------------------------------
# Bodies.


def _build_text(
    summary: CallSummary,
    llm: dict[str, Any] | None,
    call_sid: str | None,
    started_at: str | None,
) -> str:
    who = _who(summary, mask=False)
    parts = [f"{who} — {_line_label(summary)}", ""]
    parts.append(f"When:     {started_at or '(unknown)'}")
    parts.append(f"Duration: {_fmt_dur(summary.total_s)}")
    parts.append(f"Phone:    {summary.from_number or '(unknown)'}")
    parts.append(f"Label:    {summary.label or '(none)'}")
    if summary.call_count:
        n = summary.call_count
        parts.append(f"History:  {n} call{'s' if n != 1 else ''} so far")
    parts.append("")
    parts.append("Lifecycle:")
    parts.extend(f"  - {line}" for line in _lifecycle_lines(summary))

    if llm:
        bullets = llm.get("summary_bullets") or []
        detail = (llm.get("detail") or "").strip()
        if bullets:
            parts.append("")
            parts.append("Summary:")
            parts.extend(f"  - {b}" for b in bullets)
        if detail:
            parts.append("")
            parts.append("Details:")
            parts.append(f"  {detail}")

    parts.append("")
    parts.append(f"Twilio:   {alerts._twilio_console_url(call_sid)}")
    cmds = _commands(summary, call_sid)
    if cmds:
        parts.append("")
        for desc, cmd in cmds:
            parts.append(f"{desc}:")
            parts.append(f"    {cmd}")
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _build_html(
    summary: CallSummary,
    llm: dict[str, Any] | None,
    call_sid: str | None,
    started_at: str | None,
) -> str:
    e = html.escape
    who = e(_who(summary, mask=False))

    facts = [
        ("Line", e(_line_label(summary))),
        ("When", e(started_at or "unknown")),
        ("Duration", e(_fmt_dur(summary.total_s))),
        ("Phone", e(summary.from_number or "unknown")),
    ]
    if summary.call_count:
        n = summary.call_count
        facts.append(("History", f"{n} call{'s' if n != 1 else ''} so far"))
    facts_html = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#666;">{k}</td>'
        f'<td style="padding:2px 0;"><strong>{v}</strong></td></tr>'
        for k, v in facts
    )

    lifecycle_html = "".join(
        f"<li>{e(line)}</li>" for line in _lifecycle_lines(summary)
    )

    sections = [
        f'<h2 style="margin:0 0 4px;font-size:18px;">{who}</h2>',
        f'<table style="border-collapse:collapse;font-size:14px;margin:0 0 16px;">{facts_html}</table>',
        '<h3 style="font-size:14px;margin:0 0 4px;">Lifecycle</h3>',
        f'<ul style="margin:0 0 16px;padding-left:20px;font-size:14px;">{lifecycle_html}</ul>',
    ]

    if llm:
        bullets = llm.get("summary_bullets") or []
        detail = (llm.get("detail") or "").strip()
        if bullets:
            items = "".join(f"<li>{e(str(b))}</li>" for b in bullets)
            sections.append('<h3 style="font-size:14px;margin:0 0 4px;">Summary</h3>')
            sections.append(
                f'<ul style="margin:0 0 16px;padding-left:20px;font-size:14px;">{items}</ul>'
            )
        if detail:
            sections.append('<h3 style="font-size:14px;margin:0 0 4px;">Details</h3>')
            sections.append(
                f'<p style="margin:0 0 16px;font-size:14px;color:#333;">{e(detail)}</p>'
            )

    console = alerts._twilio_console_url(call_sid)
    sections.append(
        f'<p style="font-size:13px;margin:0 0 12px;">'
        f'<a href="{e(console)}">Twilio call log</a></p>'
    )
    cmds = _commands(summary, call_sid)
    if cmds:
        blocks = "".join(
            f'<div style="margin:0 0 10px;font-size:13px;">{e(desc)}:'
            f'<pre style="margin:4px 0 0;padding:8px;background:#f5f5f5;'
            f'border-radius:4px;overflow:auto;font-size:12px;">{e(cmd)}</pre></div>'
            for desc, cmd in cmds
        )
        sections.append(blocks)

    body = "".join(sections)
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,'
        f'Arial,sans-serif;color:#111;max-width:640px;">{body}</div>'
    )


# ---------------------------------------------------------------------------
# LLM.


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


async def _llm_summarize(rendered: str) -> dict[str, Any] | None:
    """One Responses-API call over the transcript → topics/bullets/detail.

    Fail-open: any error (timeout, malformed JSON, empty) returns ``None`` and
    the email still sends with the deterministic lifecycle skeleton.
    """
    if not settings.openai_api_key:
        return None
    prompt = _PROMPT_TEMPLATE.format(
        agent_name=settings.agent_name, transcript=rendered
    )
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        async with asyncio.timeout(_TIMEOUT_S):
            resp = await client.responses.create(
                model=settings.openai_search_model,
                input=prompt,
            )
    except Exception as exc:
        log.warning("call_report.llm.failed", error=repr(exc))
        return None
    raw = _strip_fences(getattr(resp, "output_text", "") or "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("call_report.llm.bad_json", sample=raw[:200])
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Entry point.


async def craft_and_send_call_report(
    call_sid: str,
    *,
    call_status: str | None = None,
    call_duration_s: int | None = None,
    from_number: str | None = None,
    to_number: str | None = None,
    calls_dir: Path = CALLS_DIR,
    transcripts_dir: Path = TRANSCRIPTS_DIR,
    settle_s: float = _SETTLE_S,
) -> None:
    """Build + send the call-processed email for one call. Never raises."""
    try:
        await _craft_and_send_inner(
            call_sid,
            call_status=call_status,
            call_duration_s=call_duration_s,
            from_number=from_number,
            to_number=to_number,
            calls_dir=calls_dir,
            transcripts_dir=transcripts_dir,
            settle_s=settle_s,
        )
    except Exception:
        log.exception("call_report.failed", call_sid=call_sid)


async def _craft_and_send_inner(
    call_sid: str,
    *,
    call_status: str | None,
    call_duration_s: int | None,
    from_number: str | None,
    to_number: str | None,
    calls_dir: Path,
    transcripts_dir: Path,
    settle_s: float,
) -> None:
    if not settings.caller_alerts_enabled:
        log.info("call_report.skipped", call_sid=call_sid, reason="disabled")
        return
    missing = alerts._missing_settings()
    if missing:
        log.info(
            "call_report.skipped",
            call_sid=call_sid,
            reason="missing_settings",
            missing_settings=missing,
        )
        return

    # Exactly-once: atomically claim the send. A racing/re-delivered callback
    # loses the race and returns here.
    marker = calls_dir / f"{call_sid}.emailed"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.open("x").close()
    except FileExistsError:
        log.info("call_report.skipped", call_sid=call_sid, reason="already_sent")
        return
    except Exception:
        log.exception("call_report.marker_failed", call_sid=call_sid)
        # Fall through — better a possible duplicate than a lost report.

    if settle_s > 0:
        await asyncio.sleep(settle_s)

    events = load_journal(call_sid, base_dir=calls_dir)
    summary = summarize_lifecycle(events, total_s=call_duration_s)
    # Backfill from the status-callback fields when the journal is thin.
    if not summary.from_number:
        summary.from_number = from_number
    if not summary.to_number:
        summary.to_number = to_number

    started_at = _started_at(events)

    llm: dict[str, Any] | None = None
    transcript_path = transcripts_dir / f"{call_sid}.jsonl"
    if transcript_path.exists():
        rendered, caller_count = _format_transcript(transcript_path)
        if rendered and caller_count > 0:
            llm = await _llm_summarize(rendered)

    topics = str((llm or {}).get("subject_topics") or "").strip()
    subject = _build_subject(summary, topics)
    payload = {
        "from": settings.caller_alerts_email_from,
        "to": [settings.caller_alerts_email_to],
        "subject": subject,
        "html": _build_html(summary, llm, call_sid, started_at),
        "text": _build_text(summary, llm, call_sid, started_at),
    }
    log.info(
        "call_report.sending",
        call_sid=call_sid,
        call_status=call_status,
        subject=subject,
        blocked=summary.blocked,
        agent_s=summary.agent_s,
        contacts=len(summary.bridged_contacts),
        had_summary=llm is not None,
    )
    await alerts._post_resend(payload, phone=summary.from_number or "", kind="report")


def _started_at(events: list[dict[str, Any]]) -> str | None:
    if not events:
        return None
    ts = events[0].get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ts
