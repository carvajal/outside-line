# 0007 — `case_brief_lookup` tool (CourtListener-grounded answers)

**Date:** 2026-06-02
**Status:** Accepted

## Context

The agent currently has one custom function tool — `search_web` — which
delegates to OpenAI's Responses API with the hosted `web_search` tool.
It's the right shape for
weather, news, prices: open-ended, current, the model picks sources.

It's the **wrong** shape for case-specific questions ("what's happening
with my case?", "Who's the judge?", "what comes next?"). Two reasons:

1. **Grounding.** The operator-authored brief at
   `data/case_brief/brief.md` is the source of truth for parties,
   lawyers, next deadlines, and standing instructions from counsel
   that the caller should hear verbatim. `search_web` would
   bypass the brief and pull from whatever news articles happened to
   rank for the query — speculative, often outdated, and silent on the
   counsel-issued instructions that the brief carries verbatim.
2. **Live freshness.** The brief is dated and can fall behind the
   docket; "what's new" needs the CourtListener docket *itself*, not
   a Google-mediated guess at it. The HTML page is behind CloudFront's
   WAF (HTTP 202 + `x-amzn-waf-action: challenge` on curl). The REST
   API needs an authenticated key. But the public **Atom feed** at
   `/docket/<id>/feed/` is open, no login, structured XML — exactly
   what we want.

## Decision

Add a sibling custom function tool: **`case_brief_lookup(question,
language)`**. On every call:

1. Read the curated brief from `data/case_brief/brief.md` (cached after
   first read). If the file is missing, degrade gracefully — never
   fabricate grounded content.
2. Fetch the CourtListener Atom feed at the configured
   `/docket/<id>/feed/` URL with a tight 5 s timeout (skipped when the
   URL is blank). Parse with stdlib `xml.etree.ElementTree`. On any
   feed failure, fall back to brief-only.
3. Call the Responses API (same `OPENAI_SEARCH_MODEL` as `search_web`)
   with **no tools registered** — we provide
   all the data ourselves via `instructions`. The prompt header pins
   the reply language, caps length at 2–3 sentences, forbids URLs and
   markdown, and instructs the model to ground only in the brief and
   the live entries.
4. Strip citations + collapse whitespace (shared helper in
   `tools/_speech.py`). Log raw + cleaned at INFO. Return the string.
   Never raise.

The Realtime agent registers both `search_web` and `case_brief_lookup`
simultaneously. The model picks based on the tool descriptions: case
questions hit `case_brief_lookup`, everything else stays on
`search_web`. The `turn_id` / transcript / barge-in machinery in
`realtime_agent.py` covers the new tool automatically — only a
dispatcher branch had to be added.

## Why not register `web_search` on the Responses call

Adding `tools=[{"type": "web_search"}]` to the Responses call would
let the helper model dig further into the docket. Tested against the
CourtListener HTML page directly: WAF-blocked. Tested via the model's
general web search: returns news-article paraphrases that mix
speculation in with fact. The Atom feed gives us the exact filings list
deterministically; mixing it with web_search adds noise without adding
signal at this volume.

## Why a separate tool, not a flag on `search_web`

A flag (`is_about_case=True`) would have to be set by the Realtime
model, which means the routing decision lives in the persona prompt
either way. A separate tool moves that decision into the tool
description where OpenAI's function-routing logic actually consumes
it. Cleaner, and the implementations diverge enough (different data
sources, different prompt shape, different timeout budget) that
collapsing them would just be a shared shell over two distinct bodies.

## Where the brief lives

`data/case_brief/brief.md`, **gitignored** under the project's
`data/*` deny-all rule — case material never belongs in git. Matches
the existing pattern — `callers.json`, `transcripts/`, `recordings/`
all under `data/`. A fictional sample lives at
`docs/examples/case-brief.example.md`.

On Railway, the volume mounts at `/app/data`, so
`/app/data/case_brief/brief.md` is the prod path. The brief is pushed
once via `scripts/_railway.py`'s `railway_write_text` helper;
subsequent edits to the brief are pushed the same way — **no
`railway up` needed** because the volume is independent of the deploy.

## Knobs (in `config.py`)

| Setting                          | Default                                                                | Notes                              |
| -------------------------------- | ---------------------------------------------------------------------- | ---------------------------------- |
| `case_brief_path`                | `Path("data/case_brief/brief.md")`                                     | Override for an alternate brief.   |
| `case_brief_docket_feed_url`     | `""` (blank = brief-only)                                              | e.g. a CourtListener feed URL.     |
| `case_brief_lookup_timeout_s`    | `10.0`                                                                 | Total tool budget (read+feed+LLM). |

Reuses `openai_search_model` from the `search_web` tool — both are
Responses-API consumers and there's no reason to fork the model knob.

## What this is **not**

- Not a generic multi-case framework. One case, one folder, one feed
  URL. If a second case ever materializes, promote to
  `data/cases/<label>/` and parameterize the tool. Until then, keep it
  flat.
- Not an operator CLI for the brief. Editing is `vi
  data/case_brief/brief.md` + a two-line `railway_write_text` push —
  not worth a CLI for one file.
- Not a feed cache. ~1 call/day; the feed fetch is cheap.

## Validation

- Atom feed reachable: `curl <feed URL>` returned 200 with 12
  `<entry>` elements (one past the brief's table cutoff — a new "Order
  to Show Cause" entry surfaced as news).
- Tool standalone: `case_brief_lookup("what happened this week?", "en")`
  returns a 2–3 sentence summary that surfaces the new entry as news
  and re-mentions the pending deadline from the brief.
- Realtime registration: `_CASE_BRIEF_LOOKUP_TOOL` appears alongside
  `_SEARCH_WEB_TOOL` in the session config.
- End-to-end: validated on a real call.
