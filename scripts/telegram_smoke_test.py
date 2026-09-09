#!/usr/bin/env -S uv run python
"""Smoke test: ring a contact's Telegram via 1:1 P2P.

Places a real outbound Telegram voice call via ``TelegramBridge.place_call``
(``pytgcalls.play(user_id, ExternalMedia.AUDIO, …)`` → ``phone.requestCall``),
holds the line silent for ``--hold`` seconds, hangs up. Success = the
contact's Telegram app rings like any other incoming call.

Audio is intentionally silence — this probes call setup only; the real
bidirectional bridge lives in ``telegram_bridge.py``.

Usage:
    uv run python scripts/telegram_smoke_test.py --to Sam            # 20 s hold
    uv run python scripts/telegram_smoke_test.py --to Sam --hold 5

Preconditions (validated explicitly with a clear error if missing):
    1. ``data/sessions/outside-line.session`` exists (run ``warm_account.py``).
    2. Target contact is in ``data/contacts.json``.
    3. Target contact resolves via their public ``@username`` (ADR 0011)
       or has been added to the agent account's address book.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pytgcalls import PyTgCalls  # noqa: E402

from outside_line.contacts import find_contact  # noqa: E402
from outside_line.telegram_bridge import TelegramBridge, make_telethon  # noqa: E402


async def main(target: str, hold_s: int) -> int:
    contact = find_contact(target)
    if contact is None:
        print(
            f"ERROR: no contact matches '{target}'. "
            f"Check data/contacts.json or try another name.",
            file=sys.stderr,
        )
        return 2

    telethon = make_telethon()
    call_py = PyTgCalls(telethon)
    bridge = TelegramBridge(telethon, call_py)

    user_id: int | None = None
    try:
        await bridge.start()
        print(
            f"→ Ringing {contact.first_name} ({contact.phone}) on Telegram. "
            f"Holding {hold_s}s then hanging up."
        )
        user_id = await bridge.place_call(contact, timeout_s=max(hold_s, 30))
        await asyncio.sleep(hold_s)
        return 0
    except Exception as exc:
        print(f"ERROR placing call: {exc!r}", file=sys.stderr)
        return 1
    finally:
        if user_id is not None:
            try:
                await bridge.hangup(user_id)
            except Exception as exc:
                print(f"warning: hangup failed: {exc!r}", file=sys.stderr)
        await bridge.stop()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--to",
        required=True,
        help="Contact first-name (or alias) to ring.",
    )
    p.add_argument(
        "--hold",
        type=int,
        default=20,
        help="Seconds to hold the ringing call before hangup. Default: 20.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    raise SystemExit(asyncio.run(main(args.to, args.hold)))
