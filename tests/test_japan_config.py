"""Reading the ``japan`` section of config.json.

Two jobs, and the second is the interesting one. The first is the ordinary
mapping of a dict onto typed objects. The second is that this camera shares its
schedule module with the ASI imager, whose vocabulary is strictly larger — a third
schedule mode, a per-slot gain, an explicit cycle length. Which of that this camera
accepts is decided here and nowhere else, so a config copied from the ASI camera
has to be *reported*, not half-honoured.

Nothing raises for a bad value: a station whose config has one typo should still
observe tonight, with the problem on the console.
"""
import sys

from datetime import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.japan import config as japan_config           # noqa: E402


def problems(conf):
    return " | ".join(conf.errors)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def test_an_empty_section_gives_working_defaults():
    conf = japan_config.from_dict({})
    assert conf.errors == []
    assert conf.camera.backend == "dcam"
    assert conf.camera.readout_speed == 2
    assert conf.camera.binning == 1
    assert conf.camera.frame_timeout_ms == 1000
    assert conf.filter_wheel.port == "/dev/ttyUSB0"
    assert conf.schedule.mode == "sun"
    assert conf.schedule.dark_frames == 3
    assert conf.schedule.entries == []
    assert conf.wait_for_enter is True


def test_a_missing_section_is_the_same_as_an_empty_one():
    assert japan_config.from_dict(None).camera.backend == "dcam"


def test_the_simulator_is_recognised_by_the_backend_name():
    conf = japan_config.from_dict({"camera": {"backend": "sim"}})
    assert conf.simulated is True
    assert japan_config.from_dict({}).simulated is False


def test_numbers_written_as_strings_are_still_read():
    """Hand-edited JSON, and what the setup wizard's text fields produce."""
    conf = japan_config.from_dict({
        "camera": {"binning": "4", "readout_speed": "1"},
        "location": {"lat": "53.3", "lon": "107.7", "elevation": "515"},
    })
    assert conf.errors == []
    assert conf.camera.binning == 4
    assert conf.camera.readout_speed == 1
    assert conf.location.lat == pytest.approx(53.3)
    assert conf.location.elevation == pytest.approx(515.0)


# ---------------------------------------------------------------------------
# This camera's own policy
# ---------------------------------------------------------------------------
def test_sun_cycle_is_reported_as_belonging_to_the_other_camera():
    """The likeliest mistake is a config copied from the ASI imager."""
    conf = japan_config.from_dict({"mode": "sun_cycle"})
    assert conf.schedule.mode == "sun"
    assert "sun_cycle" in problems(conf)
    assert "asi" in problems(conf)


def test_an_invented_mode_is_reported_without_blaming_the_other_camera():
    conf = japan_config.from_dict({"mode": "whenever"})
    assert conf.schedule.mode == "sun"
    assert "whenever" in problems(conf)
    assert "asi" not in problems(conf)


def test_a_third_readout_speed_is_refused():
    """DCAM READOUTSPEED is an enumeration, so there is nothing to clamp to."""
    conf = japan_config.from_dict({"camera": {"readout_speed": 3}})
    assert conf.camera.readout_speed == 2
    assert "readout_speed" in problems(conf)


@pytest.mark.parametrize("binning", [0, 9, -1])
def test_binning_outside_the_sensor_range_is_refused(binning):
    conf = japan_config.from_dict({"camera": {"binning": binning}})
    assert conf.camera.binning == 1
    assert "binning" in problems(conf)


def test_an_unknown_backend_falls_back_to_the_real_one():
    conf = japan_config.from_dict({"camera": {"backend": "picam"}})
    assert conf.camera.backend == "dcam"
    assert "backend" in problems(conf)


def test_a_nonsense_frame_timeout_is_refused():
    conf = japan_config.from_dict({"camera": {"frame_timeout_ms": 0}})
    assert conf.camera.frame_timeout_ms == 1000
    assert "frame_timeout_ms" in problems(conf)


def test_negative_dark_frames_become_none_rather_than_a_crash():
    conf = japan_config.from_dict({"dark_frames": -2})
    assert conf.schedule.dark_frames == 0
    assert "dark_frames" in problems(conf)


def test_a_slot_carrying_a_gain_is_reported_as_ignored():
    """This camera has no analog gain; silence would imply the setting took."""
    conf = japan_config.from_dict({
        "mode": "sun",
        "schedule": [{"filter": 1, "exposure": 30, "seconds": [0], "gain": 3}],
    })
    assert len(conf.schedule.entries) == 1
    assert "gain" in problems(conf)


def test_time_mode_without_a_start_time_gets_one_and_says_so():
    conf = japan_config.from_dict({"mode": "time"})
    assert conf.schedule.t_start == time(20, 0)
    assert "t_start" in problems(conf)


def test_a_start_time_is_read_with_or_without_seconds():
    assert japan_config.from_dict(
        {"mode": "time", "t_start": "20:15"}).schedule.t_start == time(20, 15)
    assert japan_config.from_dict(
        {"mode": "time", "t_start": "20:15:30"}).schedule.t_start == time(20, 15, 30)


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------
def test_sun_slots_come_through_with_their_seconds():
    conf = japan_config.from_dict({
        "mode": "sun",
        "camera": {"binning": 2},
        "schedule": [
            {"filter": 3, "exposure": 30, "seconds": [0, 30]},
            {"filter": 5, "exposure": 60, "seconds": 0},
        ],
    })
    assert conf.errors == []
    first, second = conf.schedule.entries
    assert (first.filter, first.exposure, first.seconds) == (3, 30.0, [0, 30])
    assert second.seconds == [0]              # a bare number is accepted
    assert first.binning == 2                 # inherited from the camera


def test_one_unusable_slot_is_dropped_and_the_rest_still_run():
    conf = japan_config.from_dict({
        "mode": "sun",
        "schedule": [
            {"filter": 9, "exposure": 30, "seconds": [0]},     # no such position
            {"filter": 3, "exposure": 30, "seconds": [0]},
        ],
    })
    assert [e.filter for e in conf.schedule.entries] == [3]
    assert "filter" in problems(conf)


def test_the_cycle_period_is_derived_from_the_last_slot():
    """japan-camera's own arithmetic: last delta + its exposure + dead time."""
    conf = japan_config.from_dict({
        "mode": "time", "t_start": "20:00", "dead_time": 5.0,
        "schedule": [
            {"delta": 100, "filter": 3, "exposure": 25, "binning": 1},
            {"delta": 130, "filter": 5, "exposure": 25, "binning": 2},
        ],
    })
    assert conf.errors == []
    assert conf.schedule.period == pytest.approx(160.0)


# ---------------------------------------------------------------------------
# A legacy schedule.txt
# ---------------------------------------------------------------------------
def test_a_legacy_sun_schedule_file_is_read(tmp_path):
    """japan-camera's own format, comma-separated: filter,exposure,seconds."""
    path = tmp_path / "schedule.txt"
    path.write_text("# a comment, then a blank line\n\n3,30,0:30\n5,60,0\n")
    conf = japan_config.from_dict({"mode": "sun", "schedule_file": str(path)})
    assert conf.errors == []
    assert [(e.filter, e.exposure, e.seconds)
            for e in conf.schedule.entries] == [(3, 30.0, [0, 30]),
                                                (5, 60.0, [0])]


def test_a_legacy_time_schedule_file_is_read(tmp_path):
    """Semicolon-separated in time mode: delta;filter;exposure;binning."""
    path = tmp_path / "schedule.txt"
    path.write_text("100;3;25;1\n130;5;25;2\n")
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "dead_time": 5.0,
                                   "schedule_file": str(path)})
    assert conf.errors == []
    assert [(e.delta, e.filter, e.binning)
            for e in conf.schedule.entries] == [(100.0, 3, 1), (130.0, 5, 2)]
    assert conf.schedule.period == pytest.approx(160.0)


def test_a_schedule_file_replaces_the_slots_in_the_config(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("1,10,0\n")
    conf = japan_config.from_dict({
        "mode": "sun", "schedule_file": str(path),
        "schedule": [{"filter": 6, "exposure": 99, "seconds": [15]}],
    })
    assert [e.filter for e in conf.schedule.entries] == [1]


def test_a_missing_schedule_file_is_reported_not_raised(tmp_path):
    conf = japan_config.from_dict({"mode": "sun",
                                   "schedule_file": str(tmp_path / "gone.txt")})
    assert conf.schedule.entries == []
    assert "gone.txt" in problems(conf)


def test_a_schedule_file_may_not_switch_the_camera_to_an_asi_mode(tmp_path):
    """A converted ASI schedule can carry globals; ``sun_cycle`` is not one to take."""
    path = tmp_path / "schedule.json"
    path.write_text('{"mode": "sun_cycle", "sun_max_angle": -12.0, '
                    '"slots": [{"delta": 0, "filter": 1, "exposure": 5}]}')
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "schedule_file": str(path)})
    assert conf.schedule.mode == "time"
    assert "sun_cycle" in problems(conf)


def test_a_schedule_file_may_still_set_what_this_camera_understands(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text('{"sun_max_angle": -12.5, "dark_frames": 5, '
                    '"slots": [{"filter": 1, "exposure": 5, "seconds": [0]}]}')
    conf = japan_config.from_dict({"mode": "sun", "schedule_file": str(path)})
    assert conf.schedule.sun_max_angle == pytest.approx(-12.5)
    assert conf.schedule.dark_frames == 5


def test_the_station_identity_in_a_schedule_file_is_ignored(tmp_path):
    """``site_id`` and ``device_id`` belong to the ASI archive, not this one."""
    path = tmp_path / "schedule.json"
    path.write_text('{"site_id": "TORY", "device_id": "ASI0", '
                    '"slots": [{"delta": 0, "filter": 1, "exposure": 5, '
                    '"binning": 1}]}')
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "dead_time": 5.0,
                                   "schedule_file": str(path)})
    assert conf.schedule.period == pytest.approx(10.0)
    assert not hasattr(conf, "site_id")


# ---------------------------------------------------------------------------
# The cycle period, stated where the cycle is written
# ---------------------------------------------------------------------------
def test_the_period_is_derived_from_the_slots_by_default(tmp_path):
    conf = japan_config.from_dict({
        "mode": "time", "t_start": "20:00", "dead_time": 5.0,
        "schedule": [{"delta": 0, "filter": 1, "exposure": 5},
                     {"delta": 100, "filter": 3, "exposure": 25}]})
    assert conf.schedule.period == pytest.approx(130.0)


def test_a_text_schedule_may_state_its_own_period(tmp_path):
    """The point of the header: swapping schedules must not mean editing config.

    The second slot sits near the end of the cycle so that this stays a test of
    the header and nothing else. Left at Δ100 it also described a 1440 s cycle
    with twenty-two idle minutes in it, which ``period_mismatch`` now — rightly —
    has something to say about; the stated period still differs from the derived
    1410 s, so it is no weaker a test of which of the two wins.
    """
    path = tmp_path / "schedule.txt"
    path.write_text("period = 1440\n"
                    "# delta(s);filter;exposure(s);binning\n"
                    "0;1;55;1\n1380;3;25;1\n")
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "dead_time": 5.0,
                                   "schedule_file": str(path)})
    assert problems(conf) == ""
    assert conf.schedule.period == pytest.approx(1440.0)
    assert len(conf.schedule.entries) == 2


def test_a_json_schedule_may_state_its_own_period(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text('{"schedule_len": 900, '
                    '"slots": [{"delta": 0, "filter": 1, "exposure": 5}]}')
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "dead_time": 5.0,
                                   "schedule_file": str(path)})
    assert conf.schedule.period == pytest.approx(900.0)


def test_the_schedule_file_period_beats_the_one_in_config(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("period = 1440\n0;1;55;1\n")
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "schedule_len": 60,
                                   "schedule_file": str(path)})
    assert conf.schedule.period == pytest.approx(1440.0)


def test_config_may_still_set_the_period_without_a_file():
    conf = japan_config.from_dict({
        "mode": "time", "t_start": "20:00", "schedule_len": 720,
        "schedule": [{"delta": 0, "filter": 1, "exposure": 5}]})
    assert conf.schedule.period == pytest.approx(720.0)


@pytest.mark.parametrize("value", ["nonsense", "-30", "0"])
def test_an_unusable_period_is_reported_and_derived_instead(tmp_path, value):
    path = tmp_path / "schedule.txt"
    path.write_text(f"period = {value}\n0;1;5;1\n")
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "dead_time": 5.0,
                                   "schedule_file": str(path)})
    assert conf.schedule.period == pytest.approx(10.0)
    if value != "0":
        # "0" is indistinguishable from "unset" and passes quietly.
        assert "period" in problems(conf)


def test_an_unknown_header_is_reported_and_the_slots_still_load(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("output_dir = /tmp/somewhere\n0;1;5;1\n")
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "schedule_file": str(path)})
    assert "output_dir" in problems(conf)
    assert len(conf.schedule.entries) == 1


def test_a_text_schedule_may_set_the_mode_below_its_slots(tmp_path):
    """The mode decides how a slot line is punctuated, so it is read first."""
    path = tmp_path / "schedule.txt"
    path.write_text("1,55,0:30\n2,55,15\nmode = sun\n")
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "schedule_file": str(path)})
    assert conf.schedule.mode == "sun"
    assert [e.seconds for e in conf.schedule.entries] == [[0, 30], [15]]


# ---------------------------------------------------------------------------
# A stated period against the slots that describe it
# ---------------------------------------------------------------------------
def _twelve_minute_cycle():
    """Twelve slots a minute apart: Δ660 + 55 s + 5 s dead time = 720 s."""
    return [{"delta": delta, "filter": (delta // 60) % 6 + 1, "exposure": 55}
            for delta in range(0, 661, 60)]


def test_a_period_twice_the_slots_is_reported():
    """The mistake that looked like a driver that had hung.

    A cycle stated at 1440 s whose slots close at 720 s leaves the whole second
    half of every cycle with no slot in it. Phase-locked to ``t_start``, the next
    slot a run started less than one such period early can find is then the first
    slot of the following cycle — which is ``t_start`` itself, so the camera sits
    there doing nothing until the appointed hour and never says why.
    """
    conf = japan_config.from_dict({
        "mode": "time", "t_start": "20:00", "dead_time": 5.0,
        "schedule_len": 1440, "schedule": _twelve_minute_cycle()})
    assert "1440" in problems(conf) and "720" in problems(conf)
    # Reported, not corrected: a quiet tail may be deliberate.
    assert conf.schedule.period == pytest.approx(1440.0)


def test_a_period_shorter_than_the_slots_is_reported_too():
    conf = japan_config.from_dict({
        "mode": "time", "t_start": "20:00", "dead_time": 5.0,
        "schedule_len": 400, "schedule": _twelve_minute_cycle()})
    assert "overruns" in problems(conf)


def test_a_period_that_matches_its_slots_is_silent():
    conf = japan_config.from_dict({
        "mode": "time", "t_start": "20:00", "dead_time": 5.0,
        "schedule_len": 720, "schedule": _twelve_minute_cycle()})
    assert conf.errors == []


def test_a_derived_period_cannot_disagree_with_itself():
    conf = japan_config.from_dict({
        "mode": "time", "t_start": "20:00", "dead_time": 5.0,
        "schedule": _twelve_minute_cycle()})
    assert conf.errors == []
    assert conf.schedule.period == pytest.approx(720.0)


def test_sun_mode_says_nothing_about_a_period_it_does_not_use():
    conf = japan_config.from_dict({
        "mode": "sun", "schedule_len": 1440,
        "schedule": [{"filter": 1, "exposure": 55, "seconds": [0]}]})
    assert conf.errors == []


def test_the_disagreement_is_found_in_a_schedule_file_too(tmp_path):
    path = tmp_path / "schedule.txt"
    path.write_text("period = 1440\n0;1;55;1\n660;6;55;1\n")
    conf = japan_config.from_dict({"mode": "time", "t_start": "20:00",
                                   "dead_time": 5.0,
                                   "schedule_file": str(path)})
    assert "720" in problems(conf)


def test_an_ordinary_tail_at_the_end_of_a_cycle_is_not_worth_a_word():
    """Closing a minute after the last slot is a schedule, not a mistake."""
    conf = japan_config.from_dict({
        "mode": "time", "t_start": "20:00", "dead_time": 5.0,
        "schedule_len": 780, "schedule": _twelve_minute_cycle()})
    assert conf.errors == []


def test_one_slot_per_cycle_is_all_tail_and_still_fine():
    """A programme of one frame per cycle has no inner gap to be judged against."""
    conf = japan_config.from_dict({
        "mode": "time", "t_start": "20:00", "dead_time": 5.0,
        "schedule_len": 1440,
        "schedule": [{"delta": 0, "filter": 1, "exposure": 55}]})
    assert conf.errors == []
