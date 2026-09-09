"""Smoke tests for the shared REPL driver in ``scripts/_repl.py``.

The driver is hard to integration-test (it blocks on stdin), but we can
monkeypatch ``builtins.input`` to feed canned lines and assert on the
observable side effects: how often ``render`` was called, what state
``load_rows`` returned, and what ``on_exit`` got.

Locked behavior:
    * mutating handler (returns True) triggers exactly one re-render
    * non-mutating handler (returns False) does NOT re-render
    * unknown verb / bad row / non-numeric row print and continue
    * row-needing verb refuses without a row index
    * ``on_exit(any_mutation)`` fires once with the right flag
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/_repl.py isn't part of the installable package — it lives next
# to the operator CLIs. Add the scripts dir to sys.path so the test can
# import it directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _repl import ReplRow, Verb, run_repl


def _driver(
    monkeypatch,
    *,
    lines: list[str],
    rows_versions: list[list[ReplRow]],
    verbs: list[Verb],
    on_exit_log: list[bool] | None = None,
) -> dict:
    """Drive ``run_repl`` once with canned input.

    ``rows_versions`` is a queue of row-lists that ``load_rows`` will pop
    one per call — lets a test simulate "before" + "after" snapshots.
    """
    input_iter = iter(lines)
    render_calls: list[list[ReplRow]] = []

    def fake_input(_prompt: str = "") -> str:
        try:
            return next(input_iter)
        except StopIteration as exc:
            raise EOFError from exc

    monkeypatch.setattr("builtins.input", fake_input)

    versions = list(rows_versions)

    def load_rows() -> list[ReplRow]:
        if len(versions) > 1:
            return versions.pop(0)
        return versions[0]

    def render(rows: list[ReplRow]) -> None:
        render_calls.append(list(rows))

    def on_exit(any_mutation: bool) -> None:
        if on_exit_log is not None:
            on_exit_log.append(any_mutation)

    rc = run_repl(
        load_rows=load_rows,
        render=render,
        verbs=verbs,
        on_exit=on_exit if on_exit_log is not None else None,
    )
    return {"rc": rc, "render_calls": render_calls}


def test_mutating_handler_triggers_one_rerender(monkeypatch, capsys) -> None:
    """A handler returning True re-renders once; a non-mutating one doesn't."""
    handler_calls: list[tuple[str, list[str]]] = []

    def add_handler(extra: list[str]) -> bool:
        handler_calls.append(("add", extra))
        return True

    def show_handler(key: str, extra: list[str]) -> bool:
        handler_calls.append(("show", [key, *extra]))
        return False

    on_exit_log: list[bool] = []
    rows_before = [ReplRow(idx=1, key="alice")]
    rows_after = [ReplRow(idx=1, key="alice"), ReplRow(idx=2, key="bob")]
    out = _driver(
        monkeypatch,
        lines=["add bob", "show 2", "q"],
        rows_versions=[rows_before, rows_after],
        verbs=[
            Verb("add", needs_row=False, usage="add <name>", handler=add_handler),
            Verb("show", needs_row=True, usage="show <n>", handler=show_handler),
        ],
        on_exit_log=on_exit_log,
    )
    assert out["rc"] == 0
    # First render is the post-mutation one (entry doesn't render — the
    # CLI's cmd_list already printed the initial table).
    assert len(out["render_calls"]) == 1
    assert [r.key for r in out["render_calls"][0]] == ["alice", "bob"]
    assert handler_calls == [("add", ["bob"]), ("show", ["bob"])]
    assert on_exit_log == [True]


def test_non_mutating_session_reports_no_mutation(monkeypatch) -> None:
    """If no handler returns True, on_exit gets False and no re-render fires."""

    def show_handler(key: str, _extra: list[str]) -> bool:
        return False

    on_exit_log: list[bool] = []
    out = _driver(
        monkeypatch,
        lines=["show 1", "q"],
        rows_versions=[[ReplRow(idx=1, key="alice")]],
        verbs=[Verb("show", needs_row=True, usage="show <n>", handler=show_handler)],
        on_exit_log=on_exit_log,
    )
    assert out["render_calls"] == []
    assert on_exit_log == [False]


def test_unknown_verb_keeps_loop_alive(monkeypatch, capsys) -> None:
    """Typing a verb that isn't registered prints help and continues."""
    out = _driver(
        monkeypatch,
        lines=["bogus 1", "q"],
        rows_versions=[[ReplRow(idx=1, key="alice")]],
        verbs=[
            Verb(
                "show",
                needs_row=True,
                usage="show <n>",
                handler=lambda _k, _e: False,
            )
        ],
    )
    captured = capsys.readouterr()
    assert "unknown command" in captured.out
    assert out["rc"] == 0


def test_row_needing_verb_refuses_non_numeric(monkeypatch, capsys) -> None:
    """`forget abc` (non-numeric row index) errors and continues."""
    out = _driver(
        monkeypatch,
        lines=["forget abc", "q"],
        rows_versions=[[ReplRow(idx=1, key="alice")]],
        verbs=[
            Verb(
                "forget",
                needs_row=True,
                usage="forget <n>",
                handler=lambda _k, _e: True,
            )
        ],
    )
    captured = capsys.readouterr()
    assert "not a row number" in captured.out
    assert out["render_calls"] == []
    assert out["rc"] == 0


def test_row_needing_verb_refuses_missing_row(monkeypatch, capsys) -> None:
    """`show 42` when row 42 doesn't exist prints `no row` and continues."""
    out = _driver(
        monkeypatch,
        lines=["show 42", "q"],
        rows_versions=[[ReplRow(idx=1, key="alice")]],
        verbs=[
            Verb(
                "show",
                needs_row=True,
                usage="show <n>",
                handler=lambda _k, _e: False,
            )
        ],
    )
    captured = capsys.readouterr()
    assert "no row [42]" in captured.out
    assert out["render_calls"] == []


def test_handler_exception_is_caught(monkeypatch, capsys) -> None:
    """A handler raising must not kill the REPL."""

    def boom(_key: str, _extra: list[str]) -> bool:
        raise RuntimeError("kaboom")

    out = _driver(
        monkeypatch,
        lines=["explode 1", "q"],
        rows_versions=[[ReplRow(idx=1, key="alice")]],
        verbs=[Verb("explode", needs_row=True, usage="explode <n>", handler=boom)],
    )
    captured = capsys.readouterr()
    assert "explode failed" in captured.out
    assert "kaboom" in captured.out
    assert out["rc"] == 0


def test_eof_exits_cleanly(monkeypatch) -> None:
    """End-of-input (no `q`) exits with 0 and fires on_exit."""
    on_exit_log: list[bool] = []
    out = _driver(
        monkeypatch,
        lines=[],  # immediate EOFError
        rows_versions=[[]],
        verbs=[
            Verb(
                "show",
                needs_row=True,
                usage="show <n>",
                handler=lambda _k, _e: False,
            )
        ],
        on_exit_log=on_exit_log,
    )
    assert out["rc"] == 0
    assert on_exit_log == [False]


def test_rowless_verb_accepts_trailing_args(monkeypatch) -> None:
    """`add foo bar` passes `[foo, bar]` to the rowless handler."""
    seen_extras: list[list[str]] = []

    def add_handler(extra: list[str]) -> bool:
        seen_extras.append(extra)
        return False

    _driver(
        monkeypatch,
        lines=["add Sam +15551230100", "q"],
        rows_versions=[[]],
        verbs=[
            Verb(
                "add",
                needs_row=False,
                usage="add <name> <phone>",
                handler=add_handler,
            )
        ],
    )
    assert seen_extras == [["Sam", "+15551230100"]]
