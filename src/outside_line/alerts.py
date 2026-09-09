"""Resend transactional-email transport for the call-processed report.

The operator gets one informational email per inbound call, sent when the call
ENDS — see :mod:`outside_line.call_report` for the crafter and
``docs/decisions/0022-call-processed-report.md`` for why the trigger is the
terminal ``/twilio/voice/status`` callback. This module is just the thin
Resend HTTPS transport + the config guard that report shares.

Railway blocks outbound SMTP on the Hobby tier, so Resend's HTTPS API is the
only transactional-email path (``docs/decisions/0005-caller-alerts-smtp.md``).

Every failure mode is fail-open: missing settings, network errors, non-2xx
responses, timeouts all log and return; they never raise. A missing email
never affects the call path.
"""

from __future__ import annotations

import httpx

from .config import settings
from .log import get_logger

log = get_logger(__name__)

# Resend's transactional-email endpoint. Auth is a Bearer API key.
_RESEND_ENDPOINT = "https://api.resend.com/emails"

# Hard upper bound on the HTTP send. Resend usually responds in well under a
# second; anything over this is a sign the path is broken and not worth
# retrying inside the (already fire-and-forget) report task.
_HTTP_TIMEOUT_S = 15.0


def _missing_settings() -> list[str]:
    """Return names of blank required settings (empty list = good to go)."""
    missing = []
    if not settings.caller_alerts_email_to:
        missing.append("CALLER_ALERTS_EMAIL_TO")
    if not settings.caller_alerts_resend_api_key:
        missing.append("CALLER_ALERTS_RESEND_API_KEY")
    return missing


def _twilio_console_url(call_sid: str | None) -> str:
    if not call_sid:
        return "(no CallSid)"
    return f"https://console.twilio.com/us1/monitor/logs/calls/{call_sid}"


async def _post_resend(payload: dict[str, object], *, phone: str, kind: str) -> None:
    """POST an email payload to Resend with consistent fail-open logging.

    ``payload`` carries ``from``/``to``/``subject`` and any of ``text``/``html``.
    ``kind`` is logged so the operator can tell which path fired.
    """
    headers = {
        "Authorization": f"Bearer {settings.caller_alerts_resend_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            resp = await client.post(_RESEND_ENDPOINT, headers=headers, json=payload)
    except Exception as exc:
        log.warning(
            "caller.alert.failed",
            phone=phone,
            kind=kind,
            reason="exception",
            error=repr(exc),
        )
        return

    if resp.status_code >= 400:
        log.warning(
            "caller.alert.failed",
            phone=phone,
            kind=kind,
            reason="http_status",
            status=resp.status_code,
            body=resp.text[:500],
        )
        return

    message_id: str | None = None
    try:
        message_id = resp.json().get("id")
    except Exception:
        pass
    log.info(
        "caller.alert.sent",
        phone=phone,
        kind=kind,
        recipient=settings.caller_alerts_email_to,
        message_id=message_id,
    )
