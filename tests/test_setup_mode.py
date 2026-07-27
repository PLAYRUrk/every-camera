"""Schedule loading must never abort the program — it must fall back to setup mode.

A camera whose schedule file is missing, empty or corrupt used to exit(1) on
startup. That made a freshly installed camera impossible to focus: focus_app.py
needs the driver running to get live frames from it. ``load_schedule_safe`` is
the decision point, so these tests pin down every way a schedule can be unusable
and check that each one is *reported*, not raised.
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import load_schedule_safe, save_schedule_file, ScheduleEntry  # noqa: E402

from datetime import datetime  # noqa: E402


def test_an_unset_schedule_path_asks_for_setup_mode():
    entries, errors, reason = load_schedule_safe("")
    assert entries == []
    assert reason and "not configured" in reason


def test_a_missing_file_is_reported_not_raised(tmp_path):
    entries, errors, reason = load_schedule_safe(tmp_path / "nope.txt")
    assert entries == []
    assert reason and "not found" in reason


def test_an_empty_file_asks_for_setup_mode(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("")
    entries, errors, reason = load_schedule_safe(path)
    assert entries == []
    assert reason and "no valid intervals" in reason


def test_a_comments_only_file_asks_for_setup_mode(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("# Measurement Schedule\n# nothing here yet\n\n")
    entries, errors, reason = load_schedule_safe(path)
    assert entries == []
    assert reason is not None


def test_a_directory_in_place_of_a_file_is_reported(tmp_path):
    entries, errors, reason = load_schedule_safe(tmp_path)
    assert entries == []
    assert reason and "directory" in reason


def test_binary_rubbish_is_reported_instead_of_a_traceback(tmp_path):
    # The old code called open().read() unguarded, so a truncated or binary
    # file crashed the driver with a UnicodeDecodeError traceback.
    path = tmp_path / "schedule.txt"
    path.write_bytes(bytes(range(256)) * 4)
    entries, errors, reason = load_schedule_safe(path)
    assert entries == []
    assert reason and "readable text" in reason


def test_malformed_lines_are_warnings_while_valid_ones_still_load(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("2026-03-08 12:00:00 - 2026-03-08 23:59:00\n"
                    "this line is nonsense\n")
    entries, errors, reason = load_schedule_safe(path)
    assert reason is None            # one good interval is a working schedule
    assert len(entries) == 1
    assert any("nonsense" in e for e in errors)


def test_a_file_of_only_bad_lines_asks_for_setup_mode(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("nonsense\n2026-13-45 99:99:99 - x\n")
    entries, errors, reason = load_schedule_safe(path)
    assert entries == []
    assert errors                    # each bad line is still reported
    assert reason is not None


def test_a_usable_schedule_reports_no_reason(tmp_path):
    path = tmp_path / "schedule.txt"
    save_schedule_file(path, [ScheduleEntry(datetime(2026, 3, 8, 12, 0, 0),
                                            datetime(2026, 3, 8, 23, 59, 0))])
    entries, errors, reason = load_schedule_safe(path)
    assert reason is None
    assert errors == []
    assert len(entries) == 1
