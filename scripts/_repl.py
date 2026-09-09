"""Row-numbered REPL for operator CLIs.

After a script prints a numbered table, this helper drops into a loop
where the operator can act on rows by their index:

    > show 3
    > allow 1 on
    > add Sam +15551230100 --alias sammy
    > q

The caller supplies a ``load_rows`` callable that returns the current
list of :class:`ReplRow` objects, a ``render`` callable that prints the
row-numbered table, and a list of :class:`Verb` objects describing the
commands the operator can type. Each verb declares whether it needs a
row index (``needs_row``), a one-line ``usage`` string for the banner,
and a ``handler`` that returns ``True`` when it mutated state — in
which case the driver reloads rows and re-renders before the next
prompt.

Exits on ``q``, ``quit``, ``exit``, EOF, or Ctrl-C. After exit, the
optional ``on_exit(any_mutation)`` callback fires with a single bool
indicating whether *any* handler in the session returned ``True`` —
used by ``contacts.py`` to batch a single ``railway redeploy`` per
session instead of one per mutation.

Handler exceptions are caught and printed so a single bad row never
kills the loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplRow:
    idx: int  # 1-based; matches what the operator typed
    key: str  # natural key handed to row-needing verbs (phone, sid, first_name, …)


# Handlers for ``needs_row=True`` verbs take ``(row_key, extra_tokens)``.
# Handlers for ``needs_row=False`` verbs take ``(extra_tokens,)``.
# Either way they return ``True`` if they mutated state (driver re-renders).
RowHandler = Callable[[str, list[str]], bool]
RowlessHandler = Callable[[list[str]], bool]


@dataclass(frozen=True)
class Verb:
    name: str
    needs_row: bool
    usage: str
    handler: RowHandler | RowlessHandler


def run_repl(
    *,
    load_rows: Callable[[], list[ReplRow]],
    render: Callable[[list[ReplRow]], None],
    verbs: list[Verb],
    prompt: str = "> ",
    on_exit: Callable[[bool], None] | None = None,
) -> int:
    """Block on stdin, dispatch verbs, re-render after mutations.

    ``load_rows`` is called once on entry (after the caller's own
    initial render) and again after every mutating handler so row
    indices stay in sync with the data. Returns 0 on normal exit.
    """
    verbs_by_name = {v.name: v for v in verbs}
    any_mutation = False

    rows = load_rows()
    by_idx = {r.idx: r for r in rows}

    print()
    _print_banner(verbs)

    while True:
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("q", "quit", "exit"):
            break
        parts = line.split()
        verb_name = parts[0]
        verb = verbs_by_name.get(verb_name)
        if verb is None:
            print(
                f"  unknown command {verb_name!r}; try: "
                f"{', '.join(sorted(verbs_by_name))}, q"
            )
            continue

        try:
            if verb.needs_row:
                if len(parts) < 2:
                    print(f"  usage: {verb.usage}")
                    continue
                try:
                    n = int(parts[1])
                except ValueError:
                    print(f"  {parts[1]!r} is not a row number")
                    continue
                row = by_idx.get(n)
                if row is None:
                    print(f"  no row [{n}]")
                    continue
                mutated = bool(verb.handler(row.key, parts[2:]))  # type: ignore[call-arg]
            else:
                mutated = bool(verb.handler(parts[1:]))  # type: ignore[call-arg]
        except Exception as exc:
            print(f"  {verb_name} failed: {exc!r}")
            mutated = False

        if mutated:
            any_mutation = True
            rows = load_rows()
            by_idx = {r.idx: r for r in rows}
            print()
            render(rows)

    if on_exit is not None:
        try:
            on_exit(any_mutation)
        except Exception as exc:
            print(f"  on_exit failed: {exc!r}")
    return 0


def _print_banner(verbs: list[Verb]) -> None:
    """Print a ``commands: …  q`` line built from each verb's usage."""
    parts = [v.usage for v in verbs]
    parts.append("q")
    print("commands:  " + "   ".join(parts))
