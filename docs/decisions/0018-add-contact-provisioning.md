# 0018 — Unified contact + directline provisioning

**Date:** 2026-07-08
**Status:** Accepted

## Context

Adding a new directline (ADR 0013) means running three operator CLIs in
sequence — `twilio.py` (buy + wire the DID), `directlines.py` (map DID →
contact), `contacts.py` (add the contact, which triggers the cache-reloading
redeploy) — plus knowing several non-obvious things:

- The directline resolves its contact at call time via
  `contacts.find_by_username`; if the contact doesn't exist the call silently
  falls through to the agent (`directline.unavailable`). A directline is useless
  without its contact.
- Contacts are cached in-process, so the contact add must trigger a redeploy.
  Directlines are read from disk per call and don't.
- The DID must carry the `outside-line directline (<Name>)` friendly name + both
  prod webhooks or it won't join dev-mode's prod/local sweep.
- A new name must not fuzzy-collide with an existing contact.
- A dense metro's primary area code is routinely out of Twilio inventory; the
  overlay is the code to fall back to.

This was hand-run each time — slow, and easy to get a piece wrong (forget the
contact, skip the redeploy).

## Decision

Ship `scripts/add_contact.py` as the single canonical way to provision a
contact + its directline DID. Everything that can be deterministic is; the
operator (or a wrapper driving the script) supplies exactly three judgments:
the contact's details, the inventory-gap fallback, and error recovery.

### search / provision split

- `search --area-code NNN` — read-only inventory probe. Emits the available
  numbers or `NO_INVENTORY`. This is where the caller handles a dry area code
  (fall back to an overlay code for the same metro).
- `provision --phone-number … --username … --first-name … [--aliases …]
  [--keywords …]` — the mutating, money step: collision guard → idempotent buy
  → directline map → contact add (fires the redeploy) → wait for the deploy →
  resolution proof. Emits a machine-readable JSON summary
  (`SUCCESS`/`PARTIAL`/`ERROR`/`COLLISION`) a wrapper can act on.

The split is a clean money-gate: the operator sees the searched number and
gives an explicit OK before `provision` spends anything. The script is
non-interactive by design — all answers arrive as flags — so it composes with
any driver (a human, a shell script, an assistant).

### Always a DID

`add_contact.py` always provisions a contact **and** a DID. Contact-only
additions (a contact reachable via the agent's `call_contact`, no dedicated
number) stay on `scripts/contacts.py add` — not worth a second mode.

### Reuse, not reimplementation

The executor shells the existing CLIs (exit-code + `-o json` driven) rather
than duplicating their logic, and imports `scripts/_dev_mode.py` for the prod
webhook URLs + managed-name convention, so a bought DID joins the dev-mode
sweep by construction. The raw CLIs remain the low-level primitive.

## Alternatives considered

- **In-process executor** (import the store classes instead of shelling the
  CLIs). Rejected: couples to script internals and needs a contacts-row
  refactor, for no real gain — the CLIs already encapsulate the writes + sync +
  redeploy.
- **No new script; run the three CLIs by hand.** Rejected: that's the status
  quo this decision removes — a judgment call at every step.
- **Deterministic inventory fallback inside the script.** Rejected as the
  primary path: the overlay/nearby choice is a judgment call; the script
  reports `NO_INVENTORY` and the caller decides.

## Verification

- `add_contact.py --help` / `search --help` / `provision --help` parse; `ruff`
  clean; one unit test (`tests/test_add_contact.py`) covers the CSV parse + the
  fuzzy-collision guard.
- `search` is read-only and free — safe to exercise against a dry and a
  non-dry area code.
- `provision` is validated against real provisioning runs — no live buy in
  automated testing (real money). Verification is ring-safe: local resolution
  proof + deploy + healthz, never a live POST that would ring the contact.
