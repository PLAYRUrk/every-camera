"""Schedule parsing and cycle timing for the ASI driver — no camera needed.

The cycle arithmetic is the subtlest part of the driver: a frame that lands a
second late is a frame taken with the wrong filter in the next slot. These tests
came over from the standalone asi-camera program together with the code, and are
extended to cover the config.json schedule that replaced its schedule.txt.
"""
import sys

from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.asi import config as asi_config          # noqa: E402
from cameras.asi import schedule as sched             # noqa: E402


def entry(delta, filter_num=1, exposure=25.0, binning=1):
    return sched.Entry(filter=filter_num, exposure=exposure, delta=delta,
                       binning=binning)


# ---------------------------------------------------------------------------
# Cycle period
# ---------------------------------------------------------------------------
def test_period_comes_from_the_largest_delta_not_the_file_order():
    entries = [entry(130, exposure=25.0), entry(100, exposure=55.0)]
    assert sched.cycle_period(entries, dead_time=5.0) == 160.0


def test_period_includes_the_dead_time():
    assert sched.cycle_period([entry(0, exposure=10.0)], dead_time=2.5) == 12.5


# ---------------------------------------------------------------------------
# next_cycle_slot
# ---------------------------------------------------------------------------
def test_before_t_start_the_iteration_is_negative():
    t_start = datetime(2026, 7, 27, 20, 0, 0)
    entries = [entry(0), entry(60)]
    period = sched.cycle_period(entries, 5.0)
    slot, chosen, iteration = sched.next_cycle_slot(
        t_start, period, entries, t_start - timedelta(minutes=5))
    assert iteration < 0
    assert slot < t_start


def test_at_t_start_the_first_entry_is_next():
    t_start = datetime(2026, 7, 27, 20, 0, 0)
    entries = [entry(0, filter_num=3), entry(60, filter_num=5)]
    period = sched.cycle_period(entries, 5.0)
    slot, chosen, iteration = sched.next_cycle_slot(
        t_start, period, entries, t_start - timedelta(seconds=1))
    assert slot == t_start
    assert chosen.filter == 3
    assert iteration == 0


def test_the_slot_advances_within_an_iteration():
    t_start = datetime(2026, 7, 27, 20, 0, 0)
    entries = [entry(0, filter_num=3), entry(60, filter_num=5)]
    period = sched.cycle_period(entries, 5.0)
    slot, chosen, iteration = sched.next_cycle_slot(
        t_start, period, entries, t_start + timedelta(seconds=1))
    assert slot == t_start + timedelta(seconds=60)
    assert chosen.filter == 5
    assert iteration == 0


def test_the_slot_rolls_into_the_next_iteration():
    t_start = datetime(2026, 7, 27, 20, 0, 0)
    entries = [entry(0), entry(60)]
    period = sched.cycle_period(entries, 5.0)      # 60 + 25 + 5 = 90
    slot, _, iteration = sched.next_cycle_slot(
        t_start, period, entries, t_start + timedelta(seconds=61))
    assert iteration == 1
    assert slot == t_start + timedelta(seconds=period)


def test_a_late_start_enters_at_the_current_phase():
    """Starting hours late must not restart the cycle from zero."""
    t_start = datetime(2026, 7, 27, 20, 0, 0)
    entries = [entry(0), entry(60)]
    period = sched.cycle_period(entries, 5.0)
    now = t_start + timedelta(hours=3, seconds=13)
    slot, _, iteration = sched.next_cycle_slot(t_start, period, entries, now)
    assert slot > now
    assert (slot - t_start).total_seconds() % period in (0.0, 60.0)
    assert iteration == int((now - t_start).total_seconds() // period)


def test_slots_are_strictly_monotonic_over_many_iterations():
    t_start = datetime(2026, 7, 27, 20, 0, 0)
    entries = [entry(0), entry(37), entry(90)]
    period = sched.cycle_period(entries, 5.0)
    now = t_start
    previous = None
    for _ in range(50):
        slot, _, _ = sched.next_cycle_slot(t_start, period, entries, now)
        if previous is not None:
            assert slot > previous
        previous = slot
        now = slot            # as the worker does: capture, then ask again


# ---------------------------------------------------------------------------
# Dark frames
# ---------------------------------------------------------------------------
def test_unique_exposures_keeps_one_binning_per_exposure_in_order():
    entries = [entry(0, exposure=25.0, binning=1),
               entry(30, exposure=25.0, binning=4),
               entry(60, exposure=55.0, binning=2)]
    assert sched.unique_exposures(entries) == [(25.0, 1), (55.0, 2)]


def test_dark_duration_grows_with_the_frame_count():
    entries = [sched.Entry(filter=1, exposure=55.0, seconds=[0, 30])]
    one = sched.estimate_dark_duration(entries, 1)
    three = sched.estimate_dark_duration(entries, 3)
    assert three > one > 0


# ---------------------------------------------------------------------------
# Second-of-minute slots
# ---------------------------------------------------------------------------
def test_next_second_slot_picks_the_next_second_in_this_minute():
    now = datetime(2026, 7, 27, 20, 0, 5, 400000)
    assert sched.next_second_slot([0, 30], now) == datetime(2026, 7, 27, 20, 0, 30)


def test_next_second_slot_rolls_over_to_the_next_minute():
    now = datetime(2026, 7, 27, 20, 0, 45)
    assert sched.next_second_slot([0, 30], now) == datetime(2026, 7, 27, 20, 1, 0)


# ---------------------------------------------------------------------------
# Sun crossing
# ---------------------------------------------------------------------------
def test_sun_crossing_returns_now_when_it_is_already_dark():
    now = datetime(2026, 7, 27, 22, 0, 0)
    assert sched.sun_crossing_time(lambda t: -20.0, -10.0, now) == now


def test_sun_crossing_finds_the_moment_the_threshold_is_passed():
    now = datetime(2026, 7, 27, 18, 0, 0)
    dusk = now + timedelta(hours=2)

    def angle(when):
        # Falls one degree every ten minutes; crosses -10° exactly at dusk.
        return -10.0 + (dusk - when).total_seconds() / 600.0

    crossing = sched.sun_crossing_time(angle, -10.0, now)
    assert abs((crossing - dusk).total_seconds()) < 1.0


def test_sun_crossing_gives_up_after_a_day():
    now = datetime(2026, 7, 27, 12, 0, 0)
    crossing = sched.sun_crossing_time(lambda t: 45.0, -10.0, now)
    assert crossing == now + timedelta(hours=24)


# ---------------------------------------------------------------------------
# Parsing: config.json slots
# ---------------------------------------------------------------------------
def test_time_slots_are_read_from_config():
    entries, errors = sched.entries_from_config(
        [{"delta": 100, "filter": 3, "exposure": 25, "binning": 1},
         {"delta": 130, "filter": 5, "exposure": 25, "binning": 2}], "time")
    assert errors == []
    assert [e.delta for e in entries] == [100.0, 130.0]
    assert [e.binning for e in entries] == [1, 2]


def test_sun_slots_default_to_second_zero():
    entries, errors = sched.entries_from_config(
        [{"filter": 1, "exposure": 55}], "sun")
    assert errors == []
    assert entries[0].seconds == [0]


def test_a_bad_slot_is_reported_and_skipped_not_fatal():
    entries, errors = sched.entries_from_config(
        [{"filter": 9, "exposure": 55, "seconds": [0]},        # filter out of range
         {"filter": 2, "exposure": -1, "seconds": [0]},        # exposure not positive
         {"filter": 2, "exposure": 55, "seconds": [61]},       # second out of range
         {"filter": 2, "exposure": 55, "seconds": [0, 30]}], "sun")
    assert len(entries) == 1
    assert entries[0].seconds == [0, 30]
    # Every rejected slot is named, so a typo can be found in the log.
    assert {e.split(":")[0] for e in errors} == {"Slot 1", "Slot 2", "Slot 3"}


def test_seconds_are_deduplicated_and_sorted():
    entries, _ = sched.entries_from_config(
        [{"filter": 1, "exposure": 5, "seconds": [30, 0, 30]}], "sun")
    assert entries[0].seconds == [0, 30]


# ---------------------------------------------------------------------------
# Parsing: legacy schedule.txt
# ---------------------------------------------------------------------------
def test_legacy_sun_file_is_understood():
    entries, errors = sched.parse_schedule_text("# comment\n1,55,0:30\n2,55,15\n",
                                                "sun")
    assert errors == []
    assert [(e.filter, e.exposure, e.seconds) for e in entries] == [
        (1, 55.0, [0, 30]), (2, 55.0, [15])]


def test_legacy_time_file_is_understood():
    entries, errors = sched.parse_schedule_text("100;3;25;1\n130;5;25;2\n", "time")
    assert errors == []
    assert [(e.delta, e.filter, e.binning) for e in entries] == [
        (100.0, 3, 1), (130.0, 5, 2)]
    assert sched.cycle_period(entries, 5.0) == 160.0


def test_a_malformed_legacy_line_is_reported():
    entries, errors = sched.parse_schedule_text("1,55\nnot a line\n1,55,0\n", "sun")
    assert len(entries) == 1
    assert len(errors) == 2


def test_legacy_round_trip_through_text():
    entries, _ = sched.parse_schedule_text("100;3;25;1\n130;5;25;2\n", "time")
    again, errors = sched.parse_schedule_text(
        sched.schedule_to_text(entries, "time"), "time")
    assert errors == []
    assert [e.as_dict() for e in again] == [e.as_dict() for e in entries]


# ---------------------------------------------------------------------------
# Config assembly
# ---------------------------------------------------------------------------
def test_config_defaults_fill_in_for_an_empty_section():
    conf = asi_config.from_dict({})
    assert conf.camera.backend == "picam"
    assert conf.cooling.enabled is True
    assert conf.schedule.mode == "sun"


def test_unknown_backend_is_reported_and_falls_back():
    conf = asi_config.from_dict({"camera": {"backend": "nonsense"}})
    assert conf.camera.backend == "picam"
    assert any("backend" in e for e in conf.errors)


def test_unknown_mode_is_reported_and_falls_back():
    conf = asi_config.from_dict({"mode": "whenever"})
    assert conf.schedule.mode == "sun"
    assert any("mode" in e for e in conf.errors)


def test_time_mode_without_t_start_is_reported_and_defaulted():
    conf = asi_config.from_dict({"mode": "time", "t_start": "",
                                 "schedule": [{"delta": 0, "filter": 1,
                                               "exposure": 5}]})
    assert conf.schedule.t_start == time(20, 0)
    assert any("t_start" in e for e in conf.errors)


@pytest.mark.parametrize("text,expected", [
    ("20:00", time(20, 0)),
    ("20:00:30", time(20, 0, 30)),
    ("", None),
    ("nonsense", None),
])
def test_t_start_parsing(text, expected):
    assert asi_config.parse_time(text, None) == expected


def test_schedule_file_overrides_the_slot_list(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("1,55,0:30\n")
    conf = asi_config.from_dict({
        "mode": "sun",
        "schedule_file": str(path),
        "schedule": [{"filter": 6, "exposure": 1, "seconds": [10]}],
    })
    assert conf.errors == []
    assert [e.filter for e in conf.schedule.entries] == [1]


def test_a_missing_schedule_file_is_reported_not_raised():
    conf = asi_config.from_dict({"mode": "sun",
                                 "schedule_file": "/nonexistent/schedule.txt"})
    assert conf.schedule.entries == []
    assert any("schedule_file" in e for e in conf.errors)


def test_slot_binning_defaults_to_the_camera_binning():
    conf = asi_config.from_dict({
        "mode": "time", "t_start": "20:00",
        "camera": {"binning": 4},
        "schedule": [{"delta": 0, "filter": 1, "exposure": 5}],
    })
    assert conf.schedule.entries[0].binning == 4
