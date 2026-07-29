"""Where a Hamamatsu frame lands, and the collision that used to end a dark run.

The layout is japan-camera's own — flat, one file per capture, ``_bg`` for a dark —
with one change: the timestamp is UTC. That half is easy to state and easy to test.

The other half is a bug this port had to fix. The name resolves to one second and
``fits.writeto`` refuses to overwrite, so two captures inside the same second at
the same filter produce the same path. In ``time`` mode the dark run is
back-to-back with no second-sync, so with any exposure under a second and more
than one dark frame that happened *every* time: the second dark raised, and the
rest of the closing darks were never taken.
"""
import sys
import threading

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import japan_driver                            # noqa: E402
from cameras.japan import config as japan_config, devices, paths  # noqa: E402

pytest.importorskip("astropy.io.fits")

NOON_UTC = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def test_the_file_name_is_the_one_japan_camera_wrote():
    assert paths.frame_name(NOON_UTC, 3) == "20260728T120000_3.fits"


def test_a_dark_carries_the_bg_suffix():
    assert paths.frame_name(NOON_UTC, 3, dark=True) == "20260728T120000_3_bg.fits"


def test_the_stamp_is_utc_not_the_local_day():
    """01:00 in Irkutsk on the 29th is 17:00 on the 28th in UTC."""
    late = datetime(2026, 7, 29, 1, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert paths.frame_name(late, 5) == "20260728T170000_5.fits"


def test_an_unknown_wheel_position_is_written_as_zero():
    """A move that never confirmed still has to produce a filename."""
    assert paths.frame_name(NOON_UTC, None) == "20260728T120000_0.fits"


def test_frames_are_filed_flat_with_no_date_tree():
    """Unlike the ASI archive: one directory per night, no YYYY/MM/DD below it."""
    path = paths.frame_path("/data/japan/20260728", NOON_UTC, 3)
    assert path == Path("/data/japan/20260728/20260728T120000_3.fits")


def test_two_filters_in_the_same_second_do_not_collide():
    assert paths.frame_name(NOON_UTC, 3) != paths.frame_name(NOON_UTC, 5)


# ---------------------------------------------------------------------------
# The collision backstop
# ---------------------------------------------------------------------------
def make_config(tmp_path, **overrides):
    cfg = {
        "output_dir": str(tmp_path),
        "mode": "time",
        "t_start": "20:00",
        "dark_frames": 3,
        "dead_time": 0.1,
        "wait_for_enter": False,
        "camera": {"backend": "sim", "binning": 8},
        "filter_wheel": {"port": "sim"},
        "location": {"lat": 51.81, "lon": 103.08, "elevation": 658},
        "schedule": [
            {"delta": 0.0, "filter": 1, "exposure": 0.05, "binning": 8},
            {"delta": 1.0, "filter": 3, "exposure": 0.05, "binning": 8},
        ],
    }
    cfg.update(overrides)
    return japan_config.from_dict(cfg)


@pytest.fixture
def worker(tmp_path):
    """A worker on both simulators, wired to write into ``tmp_path``."""
    conf = make_config(tmp_path)
    cam = japan_driver.JapanCamera(conf)
    cam.cam = devices.make_camera(conf)
    cam.wheel = devices.make_wheel(conf)
    return japan_driver.JapanWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))


def test_the_name_is_nudged_forward_until_it_is_free(worker, tmp_path):
    (tmp_path / "20260728T120000_0.fits").write_text("taken")
    (tmp_path / "20260728T120001_0.fits").write_text("taken")
    path = worker._unique_frame_path(NOON_UTC)
    assert path.name == "20260728T120002_0.fits"


def test_a_hopelessly_full_second_gives_up_rather_than_looping(worker, tmp_path):
    """None costs one frame and one logged error; a raise would cost the run."""
    for offset in range(japan_driver.JapanWorkerConsole.NAME_ATTEMPTS):
        stamp = NOON_UTC + timedelta(seconds=offset)
        (tmp_path / paths.frame_name(stamp, 0)).write_text("taken")
    assert worker._unique_frame_path(NOON_UTC) is None


def test_lights_and_darks_of_the_same_second_are_different_names(worker, tmp_path):
    """The ``_bg`` suffix means a dark never has to be nudged past a light."""
    light = worker._unique_frame_path(NOON_UTC, dark=False)
    dark = worker._unique_frame_path(NOON_UTC, dark=True)
    assert light.name == "20260728T120000_0.fits"
    assert dark.name == "20260728T120000_0_bg.fits"


def test_a_run_of_short_darks_produces_one_file_each(worker, tmp_path):
    """The regression. Three 0.05 s darks land in the same second; in
    japan-camera the second one raised and the rest were lost."""
    worker._capture_darks("final")
    darks = sorted(p.name for p in tmp_path.glob("*_bg.fits"))
    assert len(darks) == 3, darks
    assert len(set(darks)) == 3
    assert worker._errors == 0
    assert worker._darks == 3


def test_the_closing_darks_survive_a_stop_request(worker, tmp_path):
    """A stop must not cut the darks short — only a second, forced one does."""
    worker.request_stop()
    worker._capture_darks("final")
    assert len(list(tmp_path.glob("*_bg.fits"))) == 3


def test_a_forced_quit_abandons_the_darks(worker, tmp_path):
    """The operator's escape hatch: the second Ctrl+C means now, not soon."""
    worker.request_stop(force=True)
    worker._capture_darks("final")
    assert list(tmp_path.glob("*_bg.fits")) == []


def test_darks_are_taken_once_per_unique_exposure(tmp_path):
    """Two slots, two exposures, three frames each."""
    conf = make_config(tmp_path, dark_frames=3, schedule=[
        {"delta": 0.0, "filter": 1, "exposure": 0.05, "binning": 8},
        {"delta": 1.0, "filter": 3, "exposure": 0.10, "binning": 8},
        {"delta": 2.0, "filter": 5, "exposure": 0.05, "binning": 8},
    ])
    cam = japan_driver.JapanCamera(conf)
    cam.cam = devices.make_camera(conf)
    cam.wheel = devices.make_wheel(conf)
    w = japan_driver.JapanWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))
    w._capture_darks("initial")
    assert w._darks == 6           # 3 frames x 2 unique exposures, not 3 slots
    assert w._errors == 0


def test_no_seqno_counter_is_left_behind(worker, tmp_path):
    """That file belongs to the ASI archive; finding one here means copied code."""
    worker._capture_darks("final")
    assert not (tmp_path / "seqno.txt").exists()
