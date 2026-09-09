"""Focused smoke tests for the callers store + E.164 normalizer.

The upsert + atomic-write + skip-gate roundtrip have enough
off-the-happy-path branches that a real-call test would be the wrong
tool. Everything else is validated by curl + real-call checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from outside_line.callers import CallersStore, normalize_e164


# -- normalizer ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+15551230199", "+15551230199"),
        ("+1 (555) 123-0100", "+15551230100"),
        ("15551230199", "+15551230199"),
        ("5551230199", "+15551230199"),  # bare US 10-digit
        ("555 123 0199", "+15551230199"),
        ("(555) 123-0199", "+15551230199"),
        ("+44 20 7946 0958", "+442079460958"),  # UK
        ("+1234567", None),  # too short for E.164 (<8 digits)
        ("12345", None),
        ("", None),
        (None, None),
        ("not a phone", None),
        ("+12 abc 345", None),  # non-digit after +
    ],
)
def test_normalize_e164(raw: str | None, expected: str | None) -> None:
    assert normalize_e164(raw) == expected


# -- store: record_call --------------------------------------------------


def test_record_call_first_time_creates_entry(tmp_path: Path) -> None:
    store = CallersStore(tmp_path / "callers.json")
    entry, first = store.record_call("+15551230199", call_sid="CAtest1")
    assert first is True
    assert entry["call_count"] == 1
    assert entry["skip_gate"] is False
    assert entry["label"] is None
    assert entry["first_seen"] == entry["last_seen"]


def test_record_call_repeat_bumps_count(tmp_path: Path) -> None:
    store = CallersStore(tmp_path / "callers.json")
    store.record_call("+15551230199")
    entry, first = store.record_call("+15551230199")
    assert first is False
    assert entry["call_count"] == 2
    # last_seen advanced (or at least didn't go backward); first_seen pinned.
    assert entry["last_seen"] >= entry["first_seen"]


def test_record_call_multiple_phones_independent(tmp_path: Path) -> None:
    store = CallersStore(tmp_path / "callers.json")
    store.record_call("+15551230199")
    store.record_call("+15551230101")
    _, first_again = store.record_call("+15551230101")
    assert first_again is False
    data = json.loads((tmp_path / "callers.json").read_text())
    assert set(data) == {"+15551230199", "+15551230101"}


def test_record_call_serial_writes_are_atomic_and_consistent(
    tmp_path: Path,
) -> None:
    p = tmp_path / "callers.json"
    store = CallersStore(p)
    for _ in range(5):
        store.record_call("+15551230199")
    # File must be valid JSON and counters must be exact.
    data = json.loads(p.read_text())
    assert data["+15551230199"]["call_count"] == 5
    # No leftover temp file.
    assert not (tmp_path / "callers.json.tmp").exists()


def test_record_call_corrupt_file_falls_back_to_empty(tmp_path: Path) -> None:
    p = tmp_path / "callers.json"
    p.write_text("{this is not json", encoding="utf-8")
    store = CallersStore(p)
    entry, first = store.record_call("+15551230199")
    # Treated as empty: insert is a "first time".
    assert first is True
    assert entry["call_count"] == 1
    # File is healed on the save.
    data = json.loads(p.read_text())
    assert "+15551230199" in data


# -- store: skip-gate + CLI helpers --------------------------------------


def test_skips_gate_default_false(tmp_path: Path) -> None:
    store = CallersStore(tmp_path / "callers.json")
    store.record_call("+15551230199")
    assert store.skips_gate("+15551230199") is False


def test_skips_gate_missing_file_returns_false(tmp_path: Path) -> None:
    # Fail-open: a missing store is not a gate-skip list.
    store = CallersStore(tmp_path / "callers.json")
    assert store.skips_gate("+19999999999") is False


def test_set_skip_gate_toggles(tmp_path: Path) -> None:
    store = CallersStore(tmp_path / "callers.json")
    store.record_call("+15551230199")
    store.set_skip_gate("+15551230199", True)
    assert store.skips_gate("+15551230199") is True
    store.set_skip_gate("+15551230199", False)
    assert store.skips_gate("+15551230199") is False


def test_legacy_bypass_key_read_and_migrated(tmp_path: Path) -> None:
    """Entries persisted before the rename still skip the gate, and any
    write migrates them to the new key (the live store is a persisted
    volume — reads must honor the legacy shape)."""
    p = tmp_path / "callers.json"
    p.write_text(
        json.dumps({"+15551230199": {"allowed": True, "bypass": True}}),
        encoding="utf-8",
    )
    store = CallersStore(p)
    assert store.skips_gate("+15551230199") is True
    # A write (any write) converges the file on skip_gate.
    store.set_skip_gate("+15551230199", False)
    data = json.loads(p.read_text())
    entry = data["+15551230199"]
    assert "bypass" not in entry
    assert entry["skip_gate"] is False
    assert store.skips_gate("+15551230199") is False


def test_add_creates_entry_without_recording_a_call(tmp_path: Path) -> None:
    store = CallersStore(tmp_path / "callers.json")
    entry = store.add("+15551230199", label="Marco cell")
    assert entry["label"] == "Marco cell"
    assert entry["call_count"] == 0
    assert store.skips_gate("+15551230199") is False


def test_add_existing_only_updates_label(tmp_path: Path) -> None:
    store = CallersStore(tmp_path / "callers.json")
    store.record_call("+15551230199")  # call_count -> 1
    store.add("+15551230199", label="Renamed")
    entry = store.get("+15551230199")
    assert entry is not None
    assert entry["label"] == "Renamed"
    assert entry["call_count"] == 1  # unchanged


def test_set_label_and_forget(tmp_path: Path) -> None:
    store = CallersStore(tmp_path / "callers.json")
    store.record_call("+15551230199")
    store.set_label("+15551230199", "Operator cell")
    assert store.get("+15551230199")["label"] == "Operator cell"
    assert store.forget("+15551230199") is True
    assert store.get("+15551230199") is None
    # Idempotent forget.
    assert store.forget("+15551230199") is False


def test_list_all_sorted_by_last_seen_desc(tmp_path: Path) -> None:
    store = CallersStore(tmp_path / "callers.json")
    store.record_call("+15551230111")
    store.record_call("+15551230122")
    store.record_call("+15551230111")  # bumps +1111111's last_seen
    rows = store.list_all()
    assert [phone for phone, _ in rows][0] == "+15551230111"
