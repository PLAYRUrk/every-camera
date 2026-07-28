"""The sun-triggered general cycle, run end to end against the simulators.

The point of ``sun_cycle`` is a negative one: between the moment the program
starts and the moment the sun reaches ``sun_max_angle``, the only thing allowed
to touch the shutter is the pre-dark run. A regression here does not crash — it
quietly fills the archive with twilight frames the processing program will treat
as data. So these tests watch the *order* of events, not just the outcome.

Time is not faked wholesale: the clock runs, and the schedule is squeezed into a
few seconds so a whole night fits inside a test.
"""
import sys
import threading

from datetime import datetime as dt, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import asi_driver                              # noqa: E402
from cameras.asi import config as asi_config, devices        # noqa: E402

pytest.importorskip("astropy.io.fits")


def make_config(tmp_path, **overrides):
    cfg = {
        "output_dir": str(tmp_path),
        "mode": "sun_cycle",
        "sun_max_angle": -10.0,
        "dark_frames": 1,
        "dead_time": 0.1,
        "site_id": "TORY",
        "device_id": "ASI0",
        "camera": {"backend": "sim", "binning": 4},
        "filter_wheel": {"port": "sim"},
        "location": {"lat": 51.81, "lon": 103.08, "elevation": 658},
        "schedule": [
            # Slots a second apart: close enough for a quick test, far enough
            # that two frames never share a second and collide on a file name.
            {"delta": 0.0, "filter": 1, "exposure": 0.05, "binning": 4,
             "gain": 3, "readout": 0.5},
            {"delta": 1.0, "filter": 3, "exposure": 0.05, "binning": 4,
             "gain": 1, "readout": 0.5},
        ],
    }
    cfg.update(overrides)
    return asi_config.from_dict(cfg)


@pytest.fixture
def worker(tmp_path):
    """A worker on both simulators, wired to write into ``tmp_path``."""
    conf = make_config(tmp_path)
    cam = asi_driver.AsiCamera(conf)
    cam.cam = devices.make_camera(conf)
    cam.wheel = devices.make_wheel(conf)
    w = asi_driver.AsiWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))
    return w


@pytest.fixture
def prompt_anchor(monkeypatch):
    """Drop the whole-minute anchor so a test does not wait up to a minute for it.

    The anchor itself is pinned by ``test_the_cycle_is_anchored_to_a_whole
    minute``; every other test here is about what happens on either side of it.
    """
    monkeypatch.setattr(asi_driver.asi_schedule, "next_minute_boundary",
                        lambda t: t)


def run_cycle(worker, angle_fn, seconds):
    """Run ``_run_sun_cycle_mode`` with a scripted sun, stopping after ``seconds``."""
    worker._sun_angle_fn = lambda: angle_fn
    timer = threading.Timer(seconds, worker.request_stop)
    timer.start()
    try:
        worker._run_sun_cycle_mode()
    finally:
        timer.cancel()


def saved(tmp_path):
    return sorted(p.name for p in tmp_path.rglob("*.fits"))


# ---------------------------------------------------------------------------
# Nothing happens before the sun is low enough
# ---------------------------------------------------------------------------
def test_no_frame_is_taken_while_the_sun_is_still_up(worker, tmp_path):
    """Daylight: the window is hours away, so not even the darks have started."""
    run_cycle(worker, lambda when: 30.0, seconds=1.5)
    assert saved(tmp_path) == []
    assert worker._phase == "waiting for pre-darks"


def test_the_darks_come_before_any_light_frame(worker, tmp_path, prompt_anchor):
    """A window opening shortly: darks first, then lights — never the reverse."""
    opens = dt.now() + timedelta(seconds=2.5)

    def angle(when):
        return -20.0 if when >= opens else 5.0

    run_cycle(worker, angle, seconds=8.0)
    names = saved(tmp_path)
    assert names, "the run produced no frames at all"
    darks = [n for n in names if n.endswith("_DARK.fits")]
    lights = [n for n in names if not n.endswith("_DARK.fits")]
    assert darks, "the pre-darks never ran"
    assert lights, "the cycle never started"
    assert max(darks) < min(lights), "a light frame was taken before the darks"


# ---------------------------------------------------------------------------
# Once it is dark, the cycle runs
# ---------------------------------------------------------------------------
def test_the_cycle_is_anchored_to_a_whole_minute(worker, tmp_path, monkeypatch):
    """imagerd_rt only ever armed its cycle on a minute boundary; so do we."""
    real = asi_driver.asi_schedule.next_minute_boundary
    asked = []

    def spy(t):
        asked.append(t)
        return t                     # identity, so the test does not wait it out

    monkeypatch.setattr(asi_driver.asi_schedule, "next_minute_boundary", spy)
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    assert asked, "the cycle was anchored without consulting the minute boundary"
    anchor = real(asked[0])
    assert (anchor.second, anchor.microsecond) == (0, 0)
    assert anchor >= asked[0]


def test_frames_land_in_a_utc_date_tree_with_legacy_names(worker, tmp_path,
                                                          prompt_anchor):
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    files = list(tmp_path.rglob("*.fits"))
    assert files, "the run produced no frames at all"
    for path in files:
        year, month, day = path.relative_to(tmp_path).parts[:3]
        assert (len(year), len(month), len(day)) == (4, 2, 2)
        assert "_TORY_ASI0_" in path.name
        assert path.name.endswith("ms.fits") or path.name.endswith("_DARK.fits")


def test_the_darks_use_the_schedules_gain(worker, tmp_path, prompt_anchor):
    """A dark is only subtractable at the gain its light frames were taken at."""
    from astropy.io import fits

    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    darks = [p for p in tmp_path.rglob("*_DARK.fits")]
    assert darks
    # Both slots share an exposure, so one dark set is shot, at the first
    # slot's gain of 3.
    assert {fits.getheader(p)["CCDGain"] for p in darks} == {3}


def test_the_run_stops_when_the_sun_comes_back_up(worker, tmp_path,
                                                  prompt_anchor):
    """Sunrise ends the session by itself, without waiting for a stop request."""
    closes = dt.now() + timedelta(seconds=3.0)

    def angle(when):
        return 5.0 if when >= closes else -20.0

    started = dt.now()
    # No stop timer: if sunrise is not honoured, this blocks and the test fails
    # on the suite's own timeout rather than passing quietly.
    worker._sun_angle_fn = lambda: angle
    worker._run_sun_cycle_mode()
    assert dt.now() - started < timedelta(seconds=15)
    assert saved(tmp_path), "the session ended without taking anything"


def test_a_seqno_counter_is_kept_beside_the_archive(worker, tmp_path,
                                                   prompt_anchor):
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    counter = tmp_path / "seqno.txt"
    assert counter.exists()
    assert worker._errors == 0, "the run hit write errors; SEQNO would run ahead"
    assert int(counter.read_text()) == len(saved(tmp_path))
