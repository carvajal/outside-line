"""``message_contact`` realtime tool: dictate a short Telegram text to a contact.

Pure Telethon under the hood — no audio, no pytgcalls. The Realtime
model composes the message body in the caller's voice (first person);
this helper just ships the body to a contact the dispatcher already
resolved via ``contacts.find_contacts``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..contacts import Contact
from ..log import get_logger

if TYPE_CHECKING:
    # Annotation-only — the runtime object is a TelegramBridgeClient (ADR 0017),
    # so this tool doesn't drag ntgcalls into the FastAPI process.
    from ..telegram_bridge import TelegramBridge

log = get_logger(__name__)


async def send_message(
    contact: Contact,
    message: str,
    bridge: TelegramBridge,
) -> str:
    """Send ``message`` to ``contact`` over Telegram. Returns a spoken reply."""
    body = (message or "").strip()
    if not body:
        log.info("tool.message_contact.empty_body", contact=contact.first_name)
        return "I didn't quite catch what you wanted to send. Could you repeat it?"

    try:
        await bridge.send_text(contact, body)
    except Exception as exc:
        log.warning(
            "tool.message_contact.send_failed",
            contact=contact.first_name,
            error=repr(exc),
        )
        return "Sorry, I couldn't send the message just now."

    return f"I sent {contact.first_name} the message."
