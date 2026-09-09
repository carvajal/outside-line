#!/usr/bin/env -S uv run python
"""Warm the Telethon session for the agent Telegram account.

Run once. ``TelegramClient.start()`` asks for the OTP that lands in the
Telegram chat with the ``Telegram`` service account on the phone that
already has the account signed in (NOT SMS — Telegram detects the
active session and delivers in-app). If the account has 2FA enabled it
will then ask for the cloud password.

On success the script writes ``data/sessions/outside-line.session`` (gitignored,
persisted to the Railway volume per ADR 0004) and prints the top dialogs as
a read-access sanity check. Subsequent runs of any Telethon-using code reuse
the session file silently — no further prompts.

Usage:
    uv run python scripts/warm_account.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from telethon import TelegramClient  # noqa: E402

from outside_line.config import settings  # noqa: E402


async def main() -> int:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        print(
            "ERROR: TELEGRAM_API_ID / TELEGRAM_API_HASH missing from .env.",
            file=sys.stderr,
        )
        return 2

    stem = settings.telegram_session_path.removesuffix(".session")
    Path(stem).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        stem, settings.telegram_api_id, settings.telegram_api_hash
    )

    await client.start(phone=lambda: settings.telegram_phone_number)

    me = await client.get_me()
    print(f"✓ Logged in as {me.first_name} (id={me.id}, phone=+{me.phone})")
    print(f"  Session: {stem}.session")
    print()
    print("Recent dialogs (top 10):")
    count = 0
    async for d in client.iter_dialogs(limit=10):
        kind = d.entity.__class__.__name__
        print(f"  - {d.name or '(no name)'} [{kind}]")
        count += 1
    if count == 0:
        print("  (no dialogs yet — that's fine, the auth still worked)")

    print()
    print("🛑 NEXT: on the phone that holds the agent account, add the")
    print("   contacts' phone numbers to the address book (Contacts → +).")
    print("   Required for telegram_bridge.place_call to resolve them by phone.")

    await client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
