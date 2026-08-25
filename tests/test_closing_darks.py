"""When a stop is owed a set of closing dark frames, and when it is not.

Darks bracket a *session* of measurements. Two moments look like a run but are
not one, and both used to cost the operator a full dark series on the way out:

* nothing has been shot yet — the camera is waiting for the start signal, the
  sun, or the cycle anchor;
* the night is over and the next has not begun. In sun modes one run spans many
  nights, and the run-total counter never went back to zero, so a stop at noon —
  exactly when one updates a machine — shot darks for a night that ended hours
  before.

A dark series is minutes long on a cooled camera, and on the ASI the sensor
warm-up only starts afterwards, so this is the difference between a shutdown of
seconds and one of many minutes.

The third case is the opening darks themselves: a stop during them cancels the
measurements they were leading into, so finishing the series serves nothing.
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import asi_driver, japan_driver                      # noqa: E402
from cameras.asi import config as asi_config, devices             # noqa: E402
from cameras.japan import config as japan_config                  # noqa: E402
from cameras.japan import devices as japan_devices                # noqa: E402

# Cooling off: these tests are about counters and stop flags, and waiting for a
# simulated sensor to reach -60 C would dominate every one of them.
SIM = {"camera": {"backend": "sim"}, "filter_wheel": {"port": "sim"},
       "cooling": {"enabled": False, "wait_on_start": False,
                   "warm_on_exit": False},
       "schedule": [{"filter": 1, "exposure": 0.01, "seconds": [0]}],
       "dark_frames": 2}

JAPAN_SIM = {"camera": {"backend": "sim", "binning": 8},
             "filter_wheel": {"port": "sim"},
             "schedule": [{"filter": 1, "exposure": 0.01, "seconds": [0]}],
             "dark_frames": 2}


@pytest.fixture
def worker(tmp_path):
    cfg = asi_config.from_dict(SIM)
    cam = asi_driver.AsiCamera(cfg)
    cam.cam = devices.make_camera(cfg)
    cam.cam.__enter__()
    cam.wheel = devices.make_wheel(cfg)
    cam.wheel.__enter__()
    cam._info = cam.cam.info()
    return asi_driver.AsiWorkerConsole(
        cam=cam, cfg=cfg, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))


@pytest.fixture
def japan_worker(tmp_path):
    conf = japan_config.from_dict(dict(JAPAN_SIM, output_dir=str(tmp_path)))
    cam = japan_driver.JapanCamera(conf)
    cam.cam = japan_devices.make_camera(conf)
    cam.wheel = japan_devices.make_wheel(conf)
    return japan_driver.JapanWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))


# -- who is owed closing darks -----------------------------------------------
def test_a_stop_before_any_light_frame_owes_no_closing_darks(worker):
    assert worker._closing_darks_due() is False


def test_a_stop_during_a_session_owes_closing_darks(worker):
    worker._session_shots = 4
    assert worker._closing_darks_due() is True


def test_a_stop_between_nights_owes_no_closing_darks(worker):
    # The run has lights in it, but the session that took them is over: this is
    # the daytime stop that used to pay for a night that had already ended.
    worker._shots = 120
    worker._session_shots = 0
    assert worker._closing_darks_due() is False


def test_a_forced_quit_never_waits_for_darks(worker):
    worker._session_shots = 4
    worker._force_quit = True
    assert worker._closing_darks_due() is False


def test_setup_mode_owes_nothing(worker):
    worker.setup_mode = True
    worker._session_shots = 4
    assert worker._closing_darks_due() is False


def test_the_counter_follows_the_lights_not_the_darks(worker, tmp_path):
    import datetime

    worker._capture_one(datetime.datetime.now(), 0.01, image_type="LIGHT")
    assert (worker._shots, worker._session_shots) == (1, 1)
    worker._capture_one(datetime.datetime.now() + datetime.timedelta(seconds=5),
                        0.01, image_type="DARK", obs_mode="dark")
    assert (worker._shots, worker._session_shots) == (1, 1)
    assert worker._closing_darks_due() is True


# -- the opening darks give way to a stop ------------------------------------
def test_a_stop_cuts_the_opening_darks_short(worker):
    worker._stop_event.set()
    worker._capture_darks("initial", abort_on_stop=True)
    assert worker._darks == 0


def test_the_closing_darks_are_taken_after_a_stop(worker):
    # The whole point of them: the stop request is already set when they run.
    worker._stop_event.set()
    worker._capture_darks("final")
    assert worker._darks > 0


# -- the same reasoning on the other camera ----------------------------------
def test_the_japan_driver_answers_the_question_the_same_way(japan_worker):
    # One wheel, one kind of night, one answer. Japan used to shoot the closing
    # darks unconditionally, which is the half of this bug that had no guard at
    # all.
    assert japan_worker._closing_darks_due() is False
    japan_worker._session_shots = 3
    assert japan_worker._closing_darks_due() is True
    japan_worker._shots, japan_worker._session_shots = 120, 0
    assert japan_worker._closing_darks_due() is False


def test_a_stop_cuts_the_japan_opening_darks_short(japan_worker):
    japan_worker._stop_event.set()
    japan_worker._capture_darks("initial", abort_on_stop=True)
    assert japan_worker._darks == 0
