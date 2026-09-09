# 0011 — Username-only contact resolution

**Date:** 2026-06-08
**Status:** Accepted

## Context

The `call_contact` and `message_contact` Realtime tools resolved a
`Contact` to its Telegram identity by calling
`Telethon.get_entity(contact.phone)` in two places —
`telegram_bridge.place_call` and `telegram_bridge.send_text`. The
contact's E.164 phone was the load-bearing identifier.

Phone-based resolution has a hard correctness hole: **contacts who set
"Who can see my phone number" to Nobody or Contacts on Telegram are not
resolvable by phone** unless the agent account already has them in its address
book. A prior workaround (a `scripts/_telegram_import.py` helper, since
removed — see Removed below) ran `contacts.ImportContactsRequest` on the Railway container to
forcibly land a (phone, first_name) pair — does not fix this case
either: for privacy-locked phones, Telegram returns
`result.imported = []` and `get_entity(phone)` still raises
`ValueError("Cannot find any entity corresponding to '<phone>'")`.

The symptom on the call surface today is misleading: `_run_ring_phase`
catches the `ValueError` in its broad `except Exception` and reports
"{name} didn't pick up" to the caller, when in fact the agent never rang
anyone. Worse, this failure is unrecoverable from the operator side —
no `./scripts/contacts.py sync` invocation can un-privacy a hidden
phone.

`@username`-based resolution sidesteps the problem entirely. Telegram
usernames are public by design; `client.get_entity("@some_handle")` works
regardless of phone privacy, address-book membership, or whether the
contact ever interacted with the agent account. The `Contact.telegram_username`
field had been on the dataclass since the disambiguation refactor but
nothing in `src/` ever consumed it — it was a stashed annotation.

## Decision

**Identify every contact by Telegram `@username`. Drop the `phone`
field entirely.** Both resolution sites in `telegram_bridge.py` pass
`contact.telegram_username` to `Telethon.get_entity`. The `phone` field
is removed from `Contact`, from `data/contacts.json`, and from the
operator CLI; the on-container `ImportContactsRequest` machinery is
deleted.

## Consequences

### Removed

- `Contact.phone` field (`src/outside_line/contacts.py`). Loader skips any
  entry without a `telegram_username` and logs `contacts.entry_skipped`
  so partial-edit footguns are visible without killing the whole load.
- `scripts/_telegram_import.py` and `telegram_import_contact()`. The
  `--no-telegram-sync` top-level CLI flag is gone with it.
- `_post_mutation()`'s Telegram-import leg (`scripts/contacts.py`).
  What remains is just the redeploy step, renamed `_maybe_redeploy`.
- `--phone` / `--no-telegram-username` flags on `update`.
- Any contact row whose `telegram_username` is `null` (re-add via
  `./scripts/contacts.py add <Name> @their_handle` once the handle is
  known).

### Added / repurposed

- `scripts/_telegram_resolve.py` — replaces `_telegram_import.py`. Same
  shape (railway-ssh wrapper around a `python -c` snippet) but the
  snippet calls `get_entity(handle)` instead of `ImportContactsRequest`.
  `railway_redeploy()` moves here so the import surface for the CLI
  stays in one file.
- `cmd_sync` repurposed from "re-run phone import" to "verify the
  `@username` resolves on the container". Pure probe — never mutates
  the JSON, never redeploys. Output: `resolved @handle → user_id=N
  (username=… first_name=…)`. Lets the operator confirm reachability
  without a real call.
- Validation in `scripts/contacts.py`: `--username` / positional
  `username` must match `@[A-Za-z][A-Za-z0-9_]{4,31}` (Telegram's rule:
  5–32 chars, letter first, then letters / digits / underscores).

### Untouched

- ADR 0008 (modality = 1:1 P2P) is unaffected. The choice of
  `pytgcalls.play(user_id)` is independent of how `user_id` gets
  resolved.
- Persona / tool descriptions (`persona.py`, `realtime_agent.py` tool
  defs) — already transport-agnostic; the model only sees
  `first_name`. The hard product constraint ("NEVER name Telegram /
  MTProto / phone in voice or tool descriptions") is intact.
- The post-mutation **redeploy** still fires on every CLI mutation
  outside the REPL; the REPL batches one redeploy at exit via
  `on_exit`.

## Verification

- `tests/test_telegram_bridge.py` gains
  `test_place_call_resolves_by_username` and
  `test_send_text_resolves_by_username` — both monkey-patch
  `Telethon.get_entity` and assert it was awaited with
  `contact.telegram_username`. Regression guards for any future drift
  back toward phone-based resolution.
- `tests/test_contacts.py::test_contact_has_no_phone_field` asserts
  the dataclass shape is durable.
- CLI offline smoke (`./scripts/contacts.py --no-remote --no-redeploy
  add Maria @maria_handle …`) round-trips without ever touching
  Telegram.
- Validated end-to-end on a real call: "call Marco for me" rings the
  contact's Telegram via `get_entity("@marco_example")`, audio bridges,
  hangup reconnects the agent; "send Marco a message" sends a DM the
  same way.

## Notes

- Operators adding a new contact must now know the recipient's
  `@username`. Telegram surfaces it on every user's profile (Settings
  → Username for the recipient to set their own); the CLI rejects
  inputs that don't match the format.
