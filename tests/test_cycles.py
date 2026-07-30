"""Reconstructing cycles from a flat archive, and the difference frame's arithmetic.

Nothing in a capture records which cycle it belongs to, so ``cycles.py`` rebuilds
that from the schedule the camera reports. Two things there are easy to get
quietly wrong and impossible to notice by eye: the anchor (frame names are UTC,
``t_start`` is local wall clock) and the refusals (a first cycle or an unfinished
one must produce nothing rather than half a difference). Both are pinned here,
along with the weights, which are the whole point of the feature.
"""
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cycles                                                  # noqa: E402

PERIOD = 600.0
SCHEDULE = {"mode": "time", "t_start": "20:00:00", "period": PERIOD,
            "dead_time": 5.0, "entries": []}


def name_for(local_moment, filter_num, dark=False):
    """The name the japan driver would file this local instant under."""
    utc = local_moment.astimezone(timezone.utc)
    return f"{utc:%Y%m%dT%H%M%S}_{filter_num}{'_bg' if dark else ''}.fits"


def anchor(days_ago=1):
    return (datetime.now().astimezone()
            .replace(hour=20, minute=0, second=0, microsecond=0)
            - timedelta(days=days_ago))


def archive(n_cycles=3, filters=(1, 2), offsets=(0, 300), start=None):
    """A listing shaped like ``/api/frames``: ``n_cycles`` of ``filters``."""
    start = start or anchor()
    frames = []
    for index in range(n_cycles):
        base = start + timedelta(seconds=index * PERIOD)
        for filter_num in filters:
            for offset in offsets:
                when = base + timedelta(seconds=offset + filter_num)
                frames.append({"name": name_for(when, filter_num),
                               "size": 1024, "mtime": when.timestamp()})
    return frames


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def test_a_light_frame_name_is_read():
    moment, filter_num, dark = cycles.parse_japan_name("20260729T121030_3.fits")
    assert (filter_num, dark) == (3, False)
    assert moment == datetime(2026, 7, 29, 12, 10, 30, tzinfo=timezone.utc)


def test_a_dark_is_recognised_as_one():
    _moment, filter_num, dark = cycles.parse_japan_name("20260729T121030_1_bg.fits")
    assert (filter_num, dark) == (1, True)


def test_filter_zero_is_a_real_position():
    # paths.NO_FILTER: the wheel at home, or a move that never confirmed.
    assert cycles.parse_japan_name("20260729T121030_0.fits")[1] == 0


@pytest.mark.parametrize("name", [
    "", "not-a-frame.fits", "20260729T121030.fits", "20260729_1.fits",
    "20260729T121030_1.tiff", "20261399T121030_1.fits",
    "../../etc/passwd", "20260729T121030_1.fits.bak",
])
def test_anything_else_is_not_a_japan_frame(name):
    assert cycles.parse_japan_name(name) is None


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def test_frames_land_in_the_cycles_the_schedule_implies():
    grouped = cycles.group_cycles(archive(), SCHEDULE)
    assert grouped["reason"] == ""
    assert grouped["period"] == PERIOD
    assert [c["index"] for c in grouped["cycles"]] == [0, 1, 2]
    assert all(sorted(c["filters"]) == ["1", "2"] for c in grouped["cycles"])
    assert all(len(c["filters"]["1"]) == 2 for c in grouped["cycles"])


def test_each_cycle_lists_its_frames_in_time_order():
    grouped = cycles.group_cycles(archive(), SCHEDULE)
    for cycle in grouped["cycles"]:
        for frames in cycle["filters"].values():
            times = [f["time"] for f in frames]
            assert times == sorted(times)


def test_darks_belong_to_no_cycle():
    frames = archive()
    dark_at = anchor() + timedelta(seconds=120)
    frames.append({"name": name_for(dark_at, 1, dark=True), "size": 1,
                   "mtime": dark_at.timestamp()})
    grouped = cycles.group_cycles(frames, SCHEDULE)
    assert len(grouped["cycles"][0]["filters"]["1"]) == 2


def test_foreign_names_are_ignored_rather_than_guessed_at():
    frames = archive() + [{"name": "20260729_120000_TORY_ASI1_5577_055000ms.fits",
                           "size": 1, "mtime": 0}]
    assert len(cycles.group_cycles(frames, SCHEDULE)["cycles"]) == 3


def test_the_anchor_is_t_start_on_the_frames_own_night():
    """A frame after midnight keeps counting from the evening that started the run.

    The cycle itself falls on the next day — it is the *anchor* that stays on the
    previous evening, and the index that goes on rising through midnight instead
    of restarting at 0.
    """
    start = anchor()
    late = start + timedelta(hours=5)          # 01:00 the next day
    grouped = cycles.group_cycles(
        [{"name": name_for(late, 1), "size": 1, "mtime": late.timestamp()}],
        SCHEDULE)
    cycle = grouped["cycles"][0]
    assert cycle["index"] == int(5 * 3600 / PERIOD)
    assert datetime.fromisoformat(cycle["start"]) == start + timedelta(
        seconds=cycle["index"] * PERIOD)


def test_a_frame_before_t_start_belongs_to_the_previous_evening():
    start = anchor()
    early = start - timedelta(hours=1)         # 19:00, before the anchor
    grouped = cycles.group_cycles(
        [{"name": name_for(early, 1), "size": 1, "mtime": early.timestamp()}],
        SCHEDULE)
    assert grouped["cycles"][0]["index"] == int(23 * 3600 / PERIOD)


def test_a_camera_without_a_schedule_says_so_instead_of_showing_nothing():
    grouped = cycles.group_cycles(archive(), {})
    assert grouped["cycles"] == []
    assert "does not report a schedule" in grouped["reason"]


def test_sun_mode_groups_by_the_minute():
    schedule = {"mode": "sun", "t_start": None, "period": 0.0}
    start = anchor()
    frames = [{"name": name_for(start + timedelta(seconds=s), 1), "size": 1,
               "mtime": 0} for s in (0, 30, 60, 90)]
    grouped = cycles.group_cycles(frames, schedule)
    assert grouped["period"] == 60.0
    assert len(grouped["cycles"]) == 2


def test_the_cycle_being_shot_now_is_not_complete():
    now = datetime.now().astimezone()
    start = now - timedelta(seconds=PERIOD / 3)
    frames = [{"name": name_for(start + timedelta(seconds=1), 1), "size": 1,
               "mtime": 0}]
    schedule = dict(SCHEDULE, t_start=start.strftime("%H:%M:%S"))
    grouped = cycles.group_cycles(frames, schedule, now=now)
    assert grouped["cycles"][-1]["complete"] is False


# ---------------------------------------------------------------------------
# Choosing the three frames
# ---------------------------------------------------------------------------
def test_the_three_frames_are_first_previous_last():
    grouped = cycles.group_cycles(archive(), SCHEDULE)
    ids = [c["id"] for c in grouped["cycles"]]
    first, previous, last, reason = cycles.pick_composite_frames(grouped, ids[1], 1)
    assert reason == ""
    assert first["name"] == grouped["cycles"][1]["filters"]["1"][0]["name"]
    assert last["name"] == grouped["cycles"][1]["filters"]["1"][-1]["name"]
    assert previous["name"] == grouped["cycles"][0]["filters"]["1"][-1]["name"]


def test_the_first_cycle_of_a_night_has_nothing_to_subtract():
    grouped = cycles.group_cycles(archive(), SCHEDULE)
    *frames, reason = cycles.pick_composite_frames(
        grouped, grouped["cycles"][0]["id"], 1)
    assert frames == [None, None, None]
    assert "no previous cycle" in reason


def test_an_unfinished_cycle_has_no_last_frame_yet():
    now = datetime.now().astimezone()
    start = now - timedelta(seconds=PERIOD + PERIOD / 3)
    schedule = dict(SCHEDULE, t_start=start.strftime("%H:%M:%S"))
    grouped = cycles.group_cycles(archive(n_cycles=2, start=start, offsets=(0, 60)),
                                  schedule, now=now)
    *frames, reason = cycles.pick_composite_frames(
        grouped, grouped["cycles"][-1]["id"], 1)
    assert frames == [None, None, None]
    assert "still being shot" in reason


def test_a_gap_in_the_archive_does_not_promote_an_older_cycle():
    """Cycle 0 must not stand in as "previous" for cycle 2."""
    frames = [f for f in archive(n_cycles=3)
              if f["name"] not in {x["name"] for x in archive(n_cycles=3)[4:8]}]
    grouped = cycles.group_cycles(frames, SCHEDULE)
    assert [c["index"] for c in grouped["cycles"]] == [0, 2]
    *chosen, reason = cycles.pick_composite_frames(
        grouped, grouped["cycles"][-1]["id"], 1)
    assert chosen == [None, None, None]
    assert "previous cycle is missing" in reason


def test_a_filter_absent_from_the_previous_cycle_is_refused():
    frames = archive(n_cycles=2, filters=(1,))
    extra = anchor() + timedelta(seconds=PERIOD + 5)
    frames.append({"name": name_for(extra, 4), "size": 1, "mtime": 0})
    grouped = cycles.group_cycles(frames, SCHEDULE)
    *chosen, reason = cycles.pick_composite_frames(
        grouped, grouped["cycles"][1]["id"], 4)
    assert chosen == [None, None, None]
    assert "previous cycle" in reason


def test_an_unknown_cycle_is_refused_rather_than_guessed():
    grouped = cycles.group_cycles(archive(), SCHEDULE)
    *chosen, reason = cycles.pick_composite_frames(grouped, 1, 1)
    assert chosen == [None, None, None]
    assert "not in this archive" in reason


# ---------------------------------------------------------------------------
# Weights and arithmetic
# ---------------------------------------------------------------------------
def moments(gap0, gap1):
    t_a = datetime(2026, 7, 29, 20, 30, tzinfo=timezone.utc)
    return t_a, t_a - timedelta(seconds=gap0), t_a + timedelta(seconds=gap1)


@pytest.mark.parametrize("gap, expected", [
    (0.0, 1.0),             # taken at the same instant — full weight
    (300.0, 0.5),           # half the window — half the weight
    (600.0, 0.0),           # exactly the window — nothing
    (5000.0, 0.0),          # beyond it: clamped, never negative
])
def test_a_weight_falls_linearly_to_zero_across_the_window(gap, expected):
    t_a, t_b, t_d = moments(gap, gap)
    assert cycles.composite_weights(t_a, t_b, t_d, 600.0) == (expected, expected)


def test_the_two_weights_are_independent():
    t_a, t_b, t_d = moments(150.0, 450.0)
    assert cycles.composite_weights(t_a, t_b, t_d, 600.0) == (0.75, 0.25)


def test_a_window_must_be_positive():
    t_a, t_b, t_d = moments(10.0, 10.0)
    with pytest.raises(ValueError):
        cycles.composite_weights(t_a, t_b, t_d, 0.0)


def test_the_difference_is_first_minus_the_weighted_mean():
    a = np.full((4, 4), 100, dtype=np.uint16)
    b = np.full((4, 4), 40, dtype=np.uint16)
    d = np.full((4, 4), 60, dtype=np.uint16)
    result = cycles.composite_image(a, b, d, 1.0, 1.0)
    assert np.allclose(result, 100 - (40 + 60) / 2)             # 50
    assert np.allclose(cycles.composite_image(a, b, d, 0.5, 0.0), 100 - 20 / 2)


def test_the_result_keeps_its_negative_half():
    a = np.zeros((2, 2), dtype=np.uint16)
    b = np.full((2, 2), 100, dtype=np.uint16)
    result = cycles.composite_image(a, b, b, 1.0, 1.0)
    assert result.dtype == np.float32
    assert np.allclose(result, -100.0)


def test_weights_of_zero_leave_the_frame_alone():
    a = np.arange(16, dtype=np.uint16).reshape(4, 4)
    other = np.full((4, 4), 999, dtype=np.uint16)
    assert np.allclose(cycles.composite_image(a, other, other, 0.0, 0.0), a)


def test_frames_of_different_shapes_are_refused_with_a_reason():
    a = np.zeros((4, 4), dtype=np.uint16)
    b = np.zeros((2, 2), dtype=np.uint16)
    with pytest.raises(ValueError, match="binning"):
        cycles.composite_image(a, b, a, 1.0, 1.0)
