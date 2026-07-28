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


# ---------------------------------------------------------------------------
# sun_cycle: the cycle imagerd_rt started by the sun rather than by the clock
# ---------------------------------------------------------------------------
def test_sun_cycle_slots_parse_like_time_slots():
    entries, errors = sched.entries_from_config(
        [{"delta": 0, "filter": 1, "exposure": 55, "binning": 4},
         {"delta": 60, "filter": 2, "exposure": 55, "binning": 4}], "sun_cycle")
    assert errors == []
    assert [e.delta for e in entries] == [0.0, 60.0]


def test_sun_cycle_needs_no_t_start():
    conf = asi_config.from_dict({
        "mode": "sun_cycle", "sun_max_angle": -12.0,
        "schedule": [{"delta": 0, "filter": 1, "exposure": 5}],
    })
    assert conf.errors == []
    assert conf.schedule.t_start is None
    assert conf.schedule.sun_max_angle == -12.0


def test_per_slot_gain_and_readout_are_read():
    entries, errors = sched.entries_from_config(
        [{"delta": 0, "filter": 1, "exposure": 55, "gain": 3, "readout": 5}],
        "sun_cycle")
    assert errors == []
    assert entries[0].gain == 3
    assert entries[0].readout == 5.0


def test_legacy_slot_key_names_still_work():
    """A hand-converted schedule may keep imagerd_rt's own key spellings."""
    entries, errors = sched.entries_from_config(
        [{"delta": 0, "filter_num": 2, "exposure_sec": 7,
          "ccd_gain": 1, "prep_time": 2}], "sun_cycle")
    assert errors == []
    assert (entries[0].filter, entries[0].gain, entries[0].readout) == (2, 1, 2.0)


@pytest.mark.parametrize("bad", [{"gain": 0}, {"gain": 7}, {"readout": -1}])
def test_out_of_range_gain_or_readout_is_reported_and_dropped(bad):
    slot = {"delta": 0, "filter": 1, "exposure": 5}
    slot.update(bad)
    entries, errors = sched.entries_from_config([slot], "sun_cycle")
    assert errors                       # reported…
    assert len(entries) == 1            # …but the slot itself survives
    assert entries[0].gain is None or entries[0].readout is None


def test_period_prefers_the_slots_own_readout_over_the_dead_time():
    entries = [sched.Entry(filter=3, exposure=7.0, delta=1428.0, readout=5.0)]
    assert sched.cycle_period(entries, dead_time=999.0) == 1440.0


def test_explicit_schedule_len_wins_over_the_derived_period():
    conf = asi_config.from_dict({
        "mode": "sun_cycle", "schedule_len": 1440,
        "schedule": [{"delta": 0, "filter": 1, "exposure": 5}],
    })
    assert conf.errors == []
    assert conf.schedule.period == 1440.0


def test_a_useless_schedule_len_falls_back_to_the_slots():
    conf = asi_config.from_dict({
        "mode": "sun_cycle", "schedule_len": -3,
        "dead_time": 5.0,
        "schedule": [{"delta": 0, "filter": 1, "exposure": 10}],
    })
    assert any("schedule_len" in e for e in conf.errors)
    assert conf.schedule.period == 15.0


@pytest.mark.parametrize("t,expected", [
    (datetime(2026, 7, 27, 20, 0, 0), datetime(2026, 7, 27, 20, 0, 0)),
    (datetime(2026, 7, 27, 20, 0, 1), datetime(2026, 7, 27, 20, 1, 0)),
    (datetime(2026, 7, 27, 20, 59, 30), datetime(2026, 7, 27, 21, 0, 0)),
])
def test_next_minute_boundary(t, expected):
    assert sched.next_minute_boundary(t) == expected


def test_unique_dark_settings_carries_the_gain_of_the_first_matching_slot():
    entries = [sched.Entry(filter=1, exposure=55.0, delta=0, binning=4, gain=3,
                           readout=5.0),
               sched.Entry(filter=2, exposure=55.0, delta=60, binning=4, gain=1),
               sched.Entry(filter=3, exposure=7.0, delta=900, binning=4, gain=2,
                           readout=5.0)]
    assert sched.unique_dark_settings(entries) == [
        (55.0, 4, 3, 5.0), (7.0, 4, 2, 5.0)]


def test_cycle_dark_duration_covers_every_frame_plus_a_margin():
    entries = [sched.Entry(filter=1, exposure=55.0, delta=0, readout=5.0),
               sched.Entry(filter=3, exposure=7.0, delta=900, readout=5.0)]
    # 3 x (55+5) + 3 x (7+5) = 216 s of exposing and reading out.
    assert sched.estimate_cycle_dark_duration(entries, 3, 5.0) > 216.0


def test_cycle_dark_duration_falls_back_to_the_dead_time():
    entries = [sched.Entry(filter=1, exposure=10.0, delta=0)]
    with_gap = sched.estimate_cycle_dark_duration(entries, 1, 20.0)
    without = sched.estimate_cycle_dark_duration(entries, 1, 0.0)
    assert with_gap > without


# ---------------------------------------------------------------------------
# Converted imagerd_rt schedules (JSON)
# ---------------------------------------------------------------------------
def test_json_schedule_carries_its_globals_into_the_config(tmp_path):
    path = tmp_path / "converted.json"
    path.write_text(
        '{"mode": "sun_cycle", "sun_max_angle": -12.0, "schedule_len": 300,'
        ' "site_id": "TORY", "device_id": "ASI0",'
        ' "slots": [{"delta": 0, "filter": 1, "exposure": 55, "gain": 3,'
        '            "readout": 5, "binning": 4}]}')
    conf = asi_config.from_dict({
        "mode": "sun", "sun_max_angle": -6.0,
        "schedule_file": str(path),
    })
    assert conf.errors == []
    assert conf.schedule.mode == "sun_cycle"
    assert conf.schedule.sun_max_angle == -12.0
    assert conf.schedule.period == 300.0
    assert conf.station.site_id == "TORY"
    assert conf.station.device_id == "ASI0"
    assert conf.schedule.entries[0].gain == 3


def test_a_json_schedule_may_be_a_bare_list(tmp_path):
    path = tmp_path / "slots.json"
    path.write_text('[{"delta": 0, "filter": 1, "exposure": 5}]')
    conf = asi_config.from_dict({"mode": "sun_cycle", "schedule_file": str(path)})
    assert conf.errors == []
    assert len(conf.schedule.entries) == 1


def test_broken_json_is_reported_not_raised(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json at all")
    conf = asi_config.from_dict({"mode": "sun_cycle", "schedule_file": str(path)})
    assert conf.schedule.entries == []
    assert any("JSON" in e for e in conf.errors)


def test_a_json_schedule_cannot_redefine_the_output_directory(tmp_path):
    path = tmp_path / "sneaky.json"
    path.write_text('{"output_dir": "/somewhere/else", "slots": []}')
    conf = asi_config.from_dict({"output_dir": "/data/asi", "mode": "sun_cycle",
                                 "schedule_file": str(path)})
    assert conf.output_dir == "/data/asi"


def test_the_converted_tory_schedule_reproduces_the_original_cycle():
    """The station's own schedule.conf, converted: 36 slots closing at 1440 s."""
    repo = Path(__file__).resolve().parent.parent
    conf = asi_config.from_dict({
        "mode": "sun",
        "schedule_file": str(repo / "schedules" / "asi_tory_1440.json"),
        "camera": {"binning": 4},
    })
    assert conf.errors == []
    assert conf.schedule.mode == "sun_cycle"
    assert len(conf.schedule.entries) == 36
    assert conf.schedule.period == 1440.0
    last = max(conf.schedule.entries, key=lambda e: e.delta)
    assert (last.delta, last.exposure, last.readout) == (1428.0, 7.0, 5.0)
    # Every slot carries the gain and binning the original had.
    assert {e.gain for e in conf.schedule.entries} == {3}
    assert {e.binning for e in conf.schedule.entries} == {4}


# ---------------------------------------------------------------------------
# Filter table
# ---------------------------------------------------------------------------
def test_filters_default_to_the_tory_wheel():
    conf = asi_config.from_dict({})
    assert conf.filter_info(1).wavelength == "5577"
    assert conf.filter_info(3).wavelength == "OH__"


def test_an_unknown_wheel_position_yields_blank_filter_details():
    conf = asi_config.from_dict({})
    info = conf.filter_info(0)
    assert (info.wavelength, info.description) == ("", "")


def test_a_station_may_replace_the_filter_table():
    conf = asi_config.from_dict({
        "filters": [{"slot": 1, "wavelength": "4278", "description": "427.8nm"}],
    })
    assert conf.filter_info(1).wavelength == "4278"
    assert conf.filter_info(2).wavelength == ""
