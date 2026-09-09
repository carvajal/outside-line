# Contributing

## Orientation

1. Skim [`docs/high-level-architecture.md`](./docs/high-level-architecture.md)
   for the subsystem map.
2. Check [`docs/roadmap.md`](./docs/roadmap.md) for the active backlog and
   [`docs/decisions/INDEX.md`](./docs/decisions/INDEX.md) for the ADR ledger.

## Conventions

- **The package is `outside_line`** (underscore — a valid Python module
  name). The repo and directories are `outside-line` (hyphen).
- **Never commit secrets.** Secrets live in `.env` (local, gitignored)
  and the host's variable store. `data/*` is gitignored and holds PII
  (contacts, callers, memory, the Telegram session) — keep it that way.
- **New dependencies need a decision record.** Add a short ADR under
  [`docs/decisions/`](./docs/decisions/) justifying anything new in
  `pyproject.toml`. The same goes for protocol choices and security
  trade-offs.
- **Style:** ruff defaults, type hints everywhere, `async`/`await` for
  every network call. Match the comment density and idiom of the
  surrounding code.
- **Docs stay in sync.** If a change touches something the docs
  describe (a config knob, a tool, an artifact on the data volume),
  update the affected doc in the same PR — the hub doc's config table
  and disk-layout table are the usual suspects.

## Before you open a PR

```bash
uv run ruff check
uv run pytest -q
```

Both must be green — CI runs exactly these. Keep commits small and
semantic: one distinct change per commit, message focused on the *why*.
