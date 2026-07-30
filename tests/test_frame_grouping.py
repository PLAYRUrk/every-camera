"""Recovering the shape of a night from nothing but the file names.

The viewer groups an archive by day, and a japan archive by cycle and filter
below that. The day is easy: the name carries the stamp. The cycle is not — the
driver knows which iteration of the general cycle it is shooting
(``cameras/common/schedule.next_cycle_slot``) and writes it nowhere: not into the
name, not into the FITS header, not into ``/api/status``. So the period is
recovered from the one thing the archive does record — when each filter came
round again — and these tests pin that arithmetic down, including the cases where
the honest answer is "there is no cycle here".

Pure: no Qt, no camera, no astropy. That is the point of ``frame_grouping``.
"""
import sys

from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import frame_grouping as fg                                   # noqa: E402

NIGHT = datetime(2026, 7, 29, 11, 0, 0)      # 20:00 in Japan, in UTC

# For the frames whose name carries no stamp: built from a *local* naive datetime,
# because that is the clock ``datetime.fromtimestamp`` reads it back in, so the day
# it lands on is the same wherever these tests run.
LOCAL_NOON = datetime(2026, 7, 29, 12, 0, 0).timestamp()


def entry(name, mtime=None, size=4096):
    return {"name": name, "size": size, "mtime": mtime, "ext": ".fits"}


def japan_name(stamp, filter_num, dark=False):
    """The name ``cameras/japan/paths.frame_name`` would write."""
    return f"{stamp:%Y%m%dT%H%M%S}_{filter_num}{'_bg' if dark else ''}.fits"


def cycle_run(cycles, slots, period, start=NIGHT):
    """A ``time``-mode night: ``slots`` of ``(delta, filter)`` repeated ``cycles`` times."""
    frames = []
    for index in range(cycles):
        for delta, filter_num in slots:
            stamp = start + timedelta(seconds=index * period + delta)
            frames.append(entry(japan_name(stamp, filter_num)))
    return frames


# ---------------------------------------------------------------------------
# Timestamps and days
# ---------------------------------------------------------------------------
def test_the_japan_name_gives_the_utc_stamp():
    stamp, from_name = fg.frame_timestamp("20260729T140530_3.fits")
    assert stamp == datetime(2026, 7, 29, 14, 5, 30)
    assert from_name is True


def test_the_asi_name_gives_the_utc_stamp_too():
    """A different punctuation of the same convention (cameras/asi/paths.py)."""
    stamp, from_name = fg.frame_timestamp(
        "20260729_140530_TOR_ASI_5577_055000ms.fits")
    assert stamp == datetime(2026, 7, 29, 14, 5, 30)
    assert from_name is True


def test_a_name_without_a_stamp_falls_back_to_the_local_mtime():
    mtime = datetime(2026, 7, 29, 14, 5, 30).timestamp()
    stamp, from_name = fg.frame_timestamp("hand-copied.fits", mtime)
    assert stamp == datetime(2026, 7, 29, 14, 5, 30)
    assert from_name is False


def test_a_frame_with_neither_has_no_day_invented_for_it():
    assert fg.frame_timestamp("hand-copied.fits") == (None, False)
    assert fg.frame_day("hand-copied.fits") == fg.UNKNOWN_DAY


def test_the_name_wins_over_the_mtime():
    """A frame captured last night but copied today must not group under today."""
    copied_today = datetime(2026, 8, 1, 9, 0, 0).timestamp()
    assert fg.frame_day("20260729T140530_3.fits", copied_today) == "2026-07-29"


def test_sessions_come_newest_first_with_the_undatable_last():
    frames = [entry("20260728T120000_3.fits"), entry("20260729T120000_3.fits"),
              entry("loose.fits")]
    assert [s["label"] for s in fg.group_by_session(frames)] == [
        "2026-07-29", "2026-07-28", fg.UNKNOWN_DAY]


def test_frames_within_a_session_come_newest_first():
    frames = [entry("20260729T120000_3.fits"), entry("20260729T140000_5.fits")]
    (session,) = fg.group_by_session(frames)
    assert [f["name"] for f in session["frames"]] == ["20260729T140000_5.fits",
                                                      "20260729T120000_3.fits"]


# ---------------------------------------------------------------------------
# Sessions across midnight — the reason days are not calendar days
# ---------------------------------------------------------------------------
def night_across_midnight(hours=8, step=600, start=None):
    """A run from 22:00 to 06:00 the next morning, one frame every ``step`` s."""
    start = start or datetime(2026, 7, 29, 22, 0, 0)
    return [entry(japan_name(start + timedelta(seconds=i * step), 3))
            for i in range(int(hours * 3600 / step) + 1)]


def test_a_night_through_midnight_is_one_session_not_two():
    """The beginning of a run and its end must never land in different lists."""
    sessions = fg.group_by_session(night_across_midnight())
    assert len(sessions) == 1
    assert sessions[0]["label"] == "2026-07-29 → 2026-07-30"
    assert sessions[0]["dates"] == ["2026-07-29", "2026-07-30"]
    assert sessions[0]["start"] == datetime(2026, 7, 29, 22, 0, 0)
    assert sessions[0]["end"] == datetime(2026, 7, 30, 6, 0, 0)


def test_two_nights_are_two_sessions_even_though_they_share_a_date():
    """The 30th holds the morning of one night and the evening of the next."""
    frames = (night_across_midnight()
              + night_across_midnight(start=datetime(2026, 7, 30, 22, 0, 0)))
    sessions = fg.group_by_session(frames)
    assert [s["label"] for s in sessions] == ["2026-07-30 → 2026-07-31",
                                              "2026-07-29 → 2026-07-30"]


def test_a_night_that_stays_inside_one_date_is_labelled_with_that_date():
    """The Japanese station's own case: 20:00–05:00 JST is 11:00–20:00 the same UTC day."""
    (session,) = fg.group_by_session(
        night_across_midnight(start=datetime(2026, 7, 29, 11, 0, 0)))
    assert session["label"] == "2026-07-29"


def test_the_quiet_between_nights_is_what_splits_them():
    """A pause shorter than the gap is a cloud or a restart, not a new session."""
    frames = [entry(japan_name(NIGHT + timedelta(hours=h), 3))
              for h in (0, 1, 2, 7, 8)]        # a five-hour hole in one night
    assert len(fg.group_by_session(frames)) == 1
    assert len(fg.group_by_session(frames, gap=timedelta(hours=4))) == 2


# ---------------------------------------------------------------------------
# japan names
# ---------------------------------------------------------------------------
def test_a_light_frame_is_read_as_filter_and_time():
    parsed = fg.parse_japan_frame(entry("20260729T140530_3.fits"))
    assert parsed["filter"] == 3
    assert parsed["dark"] is False
    assert parsed["time"] == datetime(2026, 7, 29, 14, 5, 30)


def test_the_bg_suffix_marks_a_dark():
    assert fg.parse_japan_frame(entry("20260729T140530_3_bg.fits"))["dark"] is True


def test_a_wheel_at_home_is_filter_zero():
    parsed = fg.parse_japan_frame(entry("20260729T140530_0.fits"))
    assert parsed["filter"] == 0
    assert fg.japan_filter_label(0) == "Filter 0 — home / unknown"


def test_a_foreign_name_is_not_read_as_a_japan_frame():
    """An ASI frame in a shared directory must not be given a wheel position."""
    assert fg.parse_japan_frame(
        entry("20260729_140530_TOR_ASI_5577_055000ms.fits")) is None
    assert fg.parse_japan_frame(entry("screenshot.png")) is None


# ---------------------------------------------------------------------------
# The period
# ---------------------------------------------------------------------------
def parse_all(frames):
    return [p for p in (fg.parse_japan_frame(f) for f in frames) if p]


def test_the_readme_programme_is_recovered():
    """Slots 100;3 and 130;5 with dead_time 5 -> a 160 s cycle (README 913-921)."""
    frames = cycle_run(10, [(100, 3), (130, 5)], 160)
    assert fg.detect_cycle_period(parse_all(frames)) == 160.0


def test_a_cycle_whose_slots_straddle_it_is_recovered_too():
    """The ASI/Tory shape: 1428 + 7 + 5 = 1440 s, with the two slots far apart.

    The gap *inside* a cycle here is 1428 s and the gap *across* the boundary is
    12 s, so anything that looked for a large gap would cut the night up wrongly.
    """
    frames = cycle_run(8, [(0, 1), (1428, 2)], 1440)
    assert fg.detect_cycle_period(parse_all(frames)) == 1440.0


def test_the_shortest_period_wins_over_its_multiples():
    """320 s splits the same night just as evenly, and would hide half the cycles."""
    frames = cycle_run(12, [(100, 3), (130, 5)], 160)
    assert fg.detect_cycle_period(parse_all(frames)) == 160.0


def test_a_sun_mode_night_has_no_period_to_find():
    """Frames on given seconds of a minute, only while the sun is low enough."""
    stamps = [0, 30, 60, 90, 600, 630, 660, 1500, 1530, 4000, 4030]
    frames = [entry(japan_name(NIGHT + timedelta(seconds=s), 3)) for s in stamps]
    assert fg.detect_cycle_period(parse_all(frames)) is None


def test_a_real_sun_schedule_is_read_as_the_minute_it_repeats_on():
    """``sun`` mode does repeat — every minute — and the answer says so.

    The station's own sun schedule (japan-camera/schedule.txt) is filter 3 at
    seconds 0 and 30 and filter 5 at second 0, fired every minute the sun is low
    enough. That is a one-minute cycle, and calling it one is truthful; it is also
    useless to browse, which is part of why the switch is off by default and why
    the viewer says out loud which mode the camera is in.
    """
    frames = []
    for minute in range(30):
        base = NIGHT + timedelta(minutes=minute)
        frames.append(entry(japan_name(base, 3)))
        frames.append(entry(japan_name(base, 5)))
        frames.append(entry(japan_name(base + timedelta(seconds=30), 3)))
    assert fg.detect_cycle_period(parse_all(frames)) == 60.0


def test_one_cycle_is_not_enough_to_claim_a_period():
    frames = cycle_run(1, [(100, 3), (130, 5)], 160)
    assert fg.detect_cycle_period(parse_all(frames)) is None


def test_darks_do_not_disturb_the_period():
    """Three back-to-back darks would otherwise offer a 0-1 s candidate."""
    frames = cycle_run(10, [(100, 3), (130, 5)], 160)
    frames += [entry(japan_name(NIGHT - timedelta(seconds=10 - i), 0, dark=True))
               for i in range(3)]
    assert fg.detect_cycle_period(parse_all(frames)) == 160.0


# ---------------------------------------------------------------------------
# Cycles and filters
# ---------------------------------------------------------------------------
def test_a_night_splits_into_its_cycles_and_filters():
    frames = cycle_run(10, [(100, 3), (130, 5)], 160)
    (day,) = fg.group_japan_cycles(frames)
    assert day["label"] == "2026-07-29"
    assert day["period"] == 160.0
    assert day["period_auto"] is True
    assert len(day["cycles"]) == 10
    # Newest cycle first, like everything else in the viewer.
    assert [c["index"] for c in day["cycles"]] == list(range(10, 0, -1))
    for cycle in day["cycles"]:
        assert [group["filter"] for group in cycle["filters"]] == [3, 5]
        assert all(len(group["frames"]) == 1 for group in cycle["filters"])


def test_darks_are_kept_out_of_the_cycles():
    frames = cycle_run(10, [(100, 3), (130, 5)], 160)
    darks = [entry(japan_name(NIGHT - timedelta(seconds=10 - i), 0, dark=True))
             for i in range(3)]
    (day,) = fg.group_japan_cycles(frames + darks)
    assert len(day["darks"]) == 3
    assert len(day["cycles"]) == 10
    assert sum(c["count"] for c in day["cycles"]) == 20


def test_the_first_cycle_is_numbered_one():
    frames = cycle_run(3, [(0, 3), (30, 5)], 160)
    (day,) = fg.group_japan_cycles(frames)
    assert day["cycles"][-1]["index"] == 1
    assert day["cycles"][-1]["first"] == NIGHT


def test_a_cycle_that_produced_nothing_leaves_a_gap_in_the_numbering():
    """A missing cycle is information — it is not renumbered away."""
    frames = []
    for index in (0, 1, 3):               # the third cycle produced nothing
        for delta, filter_num in ((100, 3), (130, 5)):
            stamp = NIGHT + timedelta(seconds=index * 160 + delta)
            frames.append(entry(japan_name(stamp, filter_num)))
    (day,) = fg.group_japan_cycles(frames)
    assert day["period"] == 160.0
    assert [c["index"] for c in day["cycles"]] == [4, 2, 1]


def test_a_set_period_overrides_the_guess():
    frames = cycle_run(10, [(100, 3), (130, 5)], 160)
    (day,) = fg.group_japan_cycles(frames, period=320.0)
    assert day["period"] == 320.0
    assert day["period_auto"] is False
    assert len(day["cycles"]) == 5


def test_a_set_anchor_moves_the_boundaries_and_the_numbering():
    """An anchor before the first frame numbers the early cycles from further back."""
    frames = cycle_run(3, [(0, 3), (30, 5)], 160)
    (day,) = fg.group_japan_cycles(frames, period=160.0,
                                   anchor=time(10, 57, 20))     # 160 s earlier
    assert [c["index"] for c in day["cycles"]] == [4, 3, 2]


def test_two_nights_do_not_share_a_cycle():
    frames = cycle_run(4, [(100, 3), (130, 5)], 160)
    frames += cycle_run(4, [(100, 3), (130, 5)], 160,
                        start=NIGHT - timedelta(days=1))
    days = fg.group_japan_cycles(frames)
    assert [day["label"] for day in days] == ["2026-07-29", "2026-07-28"]
    assert all(len(day["cycles"]) == 4 for day in days)


def test_cycle_numbers_keep_rising_through_midnight():
    """One night, one rising sequence — the count does not restart at the date."""
    frames = cycle_run(5, [(100, 3), (130, 5)], 160,
                       start=datetime(2026, 7, 29, 23, 50, 0))
    (session,) = fg.group_japan_cycles(frames)
    assert session["label"] == "2026-07-29 → 2026-07-30"
    assert [c["index"] for c in session["cycles"]] == [5, 4, 3, 2, 1]
    assert session["cycles"][0]["first"].day == 30      # the last cycle is past midnight


def test_an_anchor_before_midnight_serves_a_session_that_starts_after_it():
    """The evening's t_start still numbers a run whose first frame is after 00:00."""
    frames = cycle_run(4, [(0, 3), (30, 5)], 160,
                       start=datetime(2026, 7, 30, 0, 10, 0))
    (session,) = fg.group_japan_cycles(frames, period=160.0,
                                       anchor=time(23, 50, 0))
    # 20 minutes from 23:50 to 00:10 is 7.5 cycles of 160 s.
    assert [c["index"] for c in session["cycles"]] == [11, 10, 9, 8]


def test_a_night_with_no_cycle_still_groups_by_filter():
    """The fallback the viewer draws when the period cannot be found."""
    stamps = [0, 30, 600, 630, 1500, 4000]
    frames = [entry(japan_name(NIGHT + timedelta(seconds=s), 3 if i % 2 else 5))
              for i, s in enumerate(stamps)]
    (day,) = fg.group_japan_cycles(frames)
    assert day["period"] is None
    assert day["cycles"] == []
    assert [group["filter"] for group in day["filters"]] == [3, 5]


def test_files_that_are_not_japan_frames_are_kept_aside():
    frames = cycle_run(4, [(100, 3), (130, 5)], 160) + [entry("notes.txt",
                                                             LOCAL_NOON)]
    (day,) = fg.group_japan_cycles(frames)
    assert [f["name"] for f in day["unparsed"]] == ["notes.txt"]
    assert sum(c["count"] for c in day["cycles"]) == 8
