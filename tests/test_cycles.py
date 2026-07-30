"""The per-filter difference frame: which three frames, and the arithmetic.

``frame_grouping`` decides which cycle and filter a frame belongs to (and is
tested in ``test_frame_grouping.py``); this is what is built on top of one of its
filter groups. Two things here are easy to get quietly wrong and impossible to
notice by eye: the refusals — a first cycle or an unfinished one must produce
nothing rather than half a difference — and the ordering, because
``frame_grouping`` hands back cycles and frames newest first.
"""
import sys

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cycles                                                  # noqa: E402
from frame_grouping import group_japan_cycles                  # noqa: E402

PERIOD = 600.0
# Naive, like every timestamp frame_grouping produces: the names are UTC
# and nothing in this stack ever mixes them with a local clock.
ANCHOR = datetime(2026, 7, 29, 12, 0, 0)


def name_for(moment, filter_num, dark=False):
    return f"{moment:%Y%m%dT%H%M%S}_{filter_num}{'_bg' if dark else ''}.fits"


def archive(n_cycles=3, filters=(1, 2), offsets=(0, 300)):
    """A listing shaped like ``/api/frames``: ``n_cycles`` of ``filters``."""
    frames = []
    for index in range(n_cycles):
        base = ANCHOR + timedelta(seconds=index * PERIOD)
        for filter_num in filters:
            for offset in offsets:
                when = base + timedelta(seconds=offset + filter_num)
                frames.append({"name": name_for(when, filter_num),
                               "size": 1024, "mtime": when.timestamp()})
    return frames


def grouped(frames=None, **kwargs):
    """One session, cut on the exact period — no guessing in these tests."""
    sessions = group_japan_cycles(frames if frames is not None else archive(),
                                  period=PERIOD, anchor=ANCHOR, **kwargs)
    assert len(sessions) == 1, "the fixture is meant to be a single night"
    return sessions[0]


# ---------------------------------------------------------------------------
# Choosing the three frames
# ---------------------------------------------------------------------------
def test_the_three_frames_are_first_previous_last():
    session = grouped()
    first, previous, last, reason = cycles.pick_composite_frames(session, 2, 1)
    assert reason == ""
    # frame_grouping hands frames back newest first, so "first" is the tail.
    here = next(g for c in session["cycles"] if c["index"] == 2
                for g in c["filters"] if g["filter"] == 1)["frames"]
    there = next(g for c in session["cycles"] if c["index"] == 1
                 for g in c["filters"] if g["filter"] == 1)["frames"]
    assert first["name"] == here[-1]["name"]
    assert last["name"] == here[0]["name"]
    assert previous["name"] == there[0]["name"]


def test_the_first_cycle_of_a_night_has_nothing_to_subtract():
    *frames, reason = cycles.pick_composite_frames(grouped(), 1, 1)
    assert frames == [None, None, None]
    assert "no previous cycle" in reason


def test_the_newest_cycle_is_still_being_shot():
    session = grouped()
    newest = max(c["index"] for c in session["cycles"])
    *frames, reason = cycles.pick_composite_frames(session, newest, 1)
    assert frames == [None, None, None]
    assert "still being shot" in reason


def test_a_gap_in_the_archive_does_not_promote_an_older_cycle():
    """Cycle 1 must not stand in as "previous" for cycle 3."""
    # Cycle 2 is missing entirely — a night that lost a slot, or an archive
    # copied in pieces.
    gap_start = (ANCHOR + timedelta(seconds=PERIOD)).timestamp()
    keep = [f for f in archive(n_cycles=4)
            if not gap_start <= f["mtime"] < gap_start + PERIOD]
    session = grouped(keep)
    assert [c["index"] for c in session["cycles"]] == [4, 3, 1]
    *frames, reason = cycles.pick_composite_frames(session, 3, 1)
    assert frames == [None, None, None]
    assert "no previous cycle" in reason


def test_a_filter_absent_from_this_cycle_is_refused():
    *frames, reason = cycles.pick_composite_frames(grouped(), 2, 5)
    assert frames == [None, None, None]
    assert "not shot in cycle 2" in reason


def test_a_filter_absent_from_the_previous_cycle_is_refused():
    frames = archive(n_cycles=3, filters=(1,))
    for index in (1, 2):
        when = ANCHOR + timedelta(seconds=index * PERIOD + 10)
        frames.append({"name": name_for(when, 4), "size": 1,
                       "mtime": when.timestamp()})
    *chosen, reason = cycles.pick_composite_frames(grouped(frames), 2, 4)
    assert chosen == [None, None, None]
    assert "previous cycle" in reason


def test_an_unknown_cycle_is_refused_rather_than_guessed():
    *frames, reason = cycles.pick_composite_frames(grouped(), 99, 1)
    assert frames == [None, None, None]
    assert "not in this archive" in reason


def test_a_night_with_no_cycles_is_refused():
    *frames, reason = cycles.pick_composite_frames({"cycles": []}, 1, 1)
    assert frames == [None, None, None]
    assert "no cycles" in reason


def test_darks_never_reach_the_difference():
    frames = archive()
    dark_at = ANCHOR + timedelta(seconds=PERIOD + 120)
    frames.append({"name": name_for(dark_at, 1, dark=True), "size": 1,
                   "mtime": dark_at.timestamp()})
    first, _previous, last, reason = cycles.pick_composite_frames(
        grouped(frames), 2, 1)
    assert reason == ""
    assert not first["name"].endswith("_bg.fits")
    assert not last["name"].endswith("_bg.fits")


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------
def moments(gap0, gap1):
    t_first = datetime(2026, 7, 29, 20, 30)
    return (t_first, t_first - timedelta(seconds=gap0),
            t_first + timedelta(seconds=gap1))


@pytest.mark.parametrize("gap, expected", [
    (0.0, 1.0),             # taken at the same instant — full weight
    (300.0, 0.5),           # half the window — half the weight
    (600.0, 0.0),           # exactly the window — nothing
    (5000.0, 0.0),          # beyond it: clamped, never negative
])
def test_a_weight_falls_linearly_to_zero_across_the_window(gap, expected):
    assert cycles.composite_weights(*moments(gap, gap), 600.0) == (expected,
                                                                   expected)


def test_the_two_weights_are_independent():
    assert cycles.composite_weights(*moments(150.0, 450.0), 600.0) == (0.75, 0.25)


def test_a_window_must_be_positive():
    with pytest.raises(ValueError):
        cycles.composite_weights(*moments(10.0, 10.0), 0.0)


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------
def test_the_difference_is_first_minus_the_weighted_mean():
    a = np.full((4, 4), 100, dtype=np.uint16)
    b = np.full((4, 4), 40, dtype=np.uint16)
    d = np.full((4, 4), 60, dtype=np.uint16)
    assert np.allclose(cycles.composite_image(a, b, d, 1.0, 1.0),
                       100 - (40 + 60) / 2)                     # 50
    assert np.allclose(cycles.composite_image(a, b, d, 0.5, 0.0),
                       100 - 20 / 2)


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
