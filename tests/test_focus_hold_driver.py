"""A focus hold really does stop the captures — and really does let them go.

``test_focus_hold.py`` covers the state machine; this covers the half that
matters to a night's data: a worker running its schedule against the simulators,
held mid-run and released again. Two things have to be true, and neither is
visible from the service object alone — that nothing is archived while the hold
lasts, and that the schedule picks itself up afterwards rather than stopping for
good or shooting the slot it slept through.

Time is not faked: the schedule is squeezed into a couple of seconds, the same
way ``test_japan_driver.py`` does it.
"""
import sys
import threading

from datetime import datetime as dt, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camera_service import CameraService                      # noqa: E402
from cameras import japan_driver                              # noqa: E402
from cameras.japan import config as japan_config, devices     # noqa: E402

pytest.importorskip("astropy.io.fits")


def make_worker(tmp_path, service):
    conf = japan_config.from_dict({
        "output_dir": str(tmp_path),
        "mode": "time",
        "t_start": (dt.now() - timedelta(seconds=10)).strftime("%H:%M:%S"),
        "dark_frames": 0,
        "dead_time": 1.0,
        "wait_for_enter": False,
        "camera": {"backend": "sim", "binning": 8},
        "filter_wheel": {"port": "sim"},
        "location": {"lat": 51.81, "lon": 103.08, "elevation": 658},
        "schedule": [
            {"delta": 0.0, "filter": 1, "exposure": 0.05, "binning": 8},
            {"delta": 1.0, "filter": 3, "exposure": 0.05, "binning": 8},
        ],
    })
    cam = japan_driver.JapanCamera(conf)
    cam.cam = devices.make_camera(conf)
    cam.wheel = devices.make_wheel(conf)
    return japan_driver.JapanWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path), service=service)


def lights(tmp_path):
    return sorted(p.name for p in tmp_path.rglob("*.fits")
                  if not p.name.endswith("_bg.fits"))


def run_for(worker, seconds, during=None):
    """Run the time-mode loop for ``seconds``, calling ``during`` alongside it."""
    stop = threading.Timer(seconds, worker.request_stop)
    stop.start()
    helper = threading.Thread(target=during) if during else None
    if helper:
        helper.start()
    try:
        worker._run_time_mode()
    finally:
        stop.cancel()
        if helper:
            helper.join(timeout=5)


def test_nothing_is_archived_while_the_camera_is_held(tmp_path):
    service = CameraService("japan", "test", str(tmp_path))
    worker = make_worker(tmp_path, service)
    # Held from the outset: the loop must reach its wait, see the hold, and
    # stay there.
    service.request_focus(60, hold=True)

    run_for(worker, 2.5)

    assert lights(tmp_path) == [], "frames were archived during a focus hold"
    assert worker._phase == "paused for focusing"


def test_the_schedule_resumes_once_the_hold_is_released(tmp_path):
    service = CameraService("japan", "test", str(tmp_path))
    worker = make_worker(tmp_path, service)
    service.request_focus(60, hold=True)

    def release():
        import time
        time.sleep(1.2)
        service.stop_focus()

    run_for(worker, 4.0, during=release)

    assert lights(tmp_path), "the schedule never resumed after the hold ended"


def test_an_expiring_session_releases_the_camera_on_its_own(tmp_path):
    """A focus tool that dies must not take the night with it."""
    import time

    service = CameraService("japan", "test", str(tmp_path))
    worker = make_worker(tmp_path, service)
    service.request_focus(1, hold=True)      # nobody will renew this

    def expire():
        time.sleep(1.2)
        # The deadline is monotonic; move it rather than waiting it out.
        service._focus_deadline = time.monotonic() - 0.1

    run_for(worker, 4.0, during=expire)

    assert lights(tmp_path), "an expired hold left the camera stopped"


def test_the_worker_reports_the_hold_and_takes_it_back(tmp_path):
    service = CameraService("japan", "test", str(tmp_path))
    worker = make_worker(tmp_path, service)
    service.request_focus(60, hold=True)
    seen = []

    def watch():
        import time
        for _ in range(20):
            time.sleep(0.1)
            if service.status()["hold_effective"]:
                seen.append(True)
                break
        service.stop_focus()

    run_for(worker, 3.5, during=watch)

    assert seen, "the worker never confirmed it had paused"
    assert service.status()["hold_effective"] is False
    assert service.info()["schedule_active"] is not None
