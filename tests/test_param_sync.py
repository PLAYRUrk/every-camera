"""What focus_app is told the camera is set to.

A camera changes its own settings while nobody is asking it to: a schedule
moves the exposure and the filter between frames, the ASI shutter opens after
the pre-darks. The drivers used to publish these values once at startup and
again only after a change *they had been asked for*, so an observer who
connected later — or who watched the schedule run — was shown the state the
process had started with. These tests cover the refresh that fixed it.
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camera_service import CameraService                      # noqa: E402
from worker_common import publish_current_params              # noqa: E402
from cameras import asi_driver                                # noqa: E402
from cameras.asi import config as asi_config, devices         # noqa: E402

SIM = {"camera": {"backend": "sim"}, "filter_wheel": {"port": "sim"}}


@pytest.fixture
def service():
    return CameraService("asi", "test", "")


@pytest.fixture
def worker(service, tmp_path):
    cfg = asi_config.from_dict(SIM)
    cam = asi_driver.AsiCamera(cfg)
    cam.cam = devices.make_camera(cfg)
    cam.wheel = devices.make_wheel(cfg)
    return asi_driver.AsiWorkerConsole(
        cam=cam, cfg=cfg, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path), service=service)


# -- the shared helper -------------------------------------------------------
def test_the_snapshot_reaches_the_service(service):
    publish_current_params(service, lambda: {"gain": 3})
    assert service.params()["current"] == {"gain": 3}


def test_a_camera_that_cannot_answer_does_not_break_the_status_tick(service):
    def _broken():
        raise RuntimeError("camera busy")

    publish_current_params(service, _broken)          # warns, does not raise
    assert service.params()["current"] == {}


def test_no_service_is_not_an_error():
    publish_current_params(None, lambda: {"gain": 3})


# -- the driver --------------------------------------------------------------
def test_a_change_the_schedule_made_reaches_the_client(worker, service):
    # Nobody asked focus_app for this: it is what _capture_darks and the two
    # schedule modes do on their own.
    worker.cam.set_shutter(True)
    worker.cam.set_exposure(7.5)
    worker._save_status("running")

    current = service.params()["current"]
    assert current["shutter"] is True
    assert current["exposure"] == 7.5


def test_the_state_keeps_up_rather_than_freezing_at_the_first_reading(worker,
                                                                     service):
    worker.cam.set_shutter(True)
    worker._save_status("running")
    worker.cam.set_shutter(False)
    worker._save_status("running")
    assert service.params()["current"]["shutter"] is False


def test_a_client_connecting_late_is_told_the_current_filter(worker, service):
    worker.cam.select_filter(4)
    worker._save_status("running")
    assert service.params()["current"]["filter"] == 4
