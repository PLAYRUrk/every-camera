"""Focus mode must work before the operator has started the measurements.

In ``time`` mode both cameras sit in ``_wait_for_start`` until someone presses
Enter. That loop published status and applied parameter changes but never served
a focus frame, so an observer who opened focus_app against a camera in that state
saw a connected, streaming, permanently blank window — and the only way out was
to walk to the machine and press Enter. Worse, the shutter is shut at that point,
so even a served frame would have been black.

These tests pin down all three halves of the fix: a frame reaches the service
before Enter, the shutter is opened for the session and shut again when the
programme takes over, and a dark run keeps the shutter to itself.
"""
import sys
import threading
import time

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camera_service import CameraService                      # noqa: E402
from cameras import asi_driver, japan_driver                  # noqa: E402
from cameras.asi import config as asi_config                  # noqa: E402
from cameras.japan import config as japan_config, devices     # noqa: E402

pytest.importorskip("astropy.io.fits")

# Long enough for several TICKs of the wait loop, short enough to stay a test.
SETTLE = 3.0


def japan_worker(tmp_path, service, **overrides):
    cfg = {
        "output_dir": str(tmp_path),
        "mode": "time",
        "t_start": "20:00",
        "dark_frames": 1,
        "dead_time": 0.1,
        "wait_for_enter": True,
        "camera": {"backend": "sim", "binning": 8},
        "filter_wheel": {"port": "sim"},
        "location": {"lat": 51.81, "lon": 103.08, "elevation": 658},
        "schedule": [{"delta": 0, "filter": 1, "exposure": 0.05},
                     {"delta": 1, "filter": 3, "exposure": 0.05}],
    }
    cfg.update(overrides)
    conf = japan_config.from_dict(cfg)
    cam = japan_driver.JapanCamera(conf)
    cam.cam = devices.make_camera(conf)
    cam.wheel = devices.make_wheel(conf)
    cam.set_shutter(False)          # what JapanCamera.open() leaves behind
    return japan_driver.JapanWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path), service=service)


def asi_worker(tmp_path, service, **overrides):
    cfg = {
        "output_dir": str(tmp_path),
        "mode": "time",
        "t_start": "20:00",
        "dark_frames": 1,
        "dead_time": 0.1,
        "wait_for_enter": True,
        "camera": {"backend": "sim", "binning": 8},
        "cooling": {"enabled": False, "wait_on_start": False,
                    "warm_on_exit": False},
        "filter_wheel": {"port": "sim"},
        "location": {"lat": 51.81, "lon": 103.08, "elevation": 658},
        "schedule": [{"delta": 0, "filter": 1, "exposure": 0.05},
                     {"delta": 1, "filter": 3, "exposure": 0.05}],
    }
    cfg.update(overrides)
    conf = asi_config.from_dict(cfg)
    from cameras.asi import devices as asi_devices
    cam = asi_driver.AsiCamera(conf)
    cam.cam = asi_devices.make_camera(conf)
    cam.wheel = asi_devices.make_wheel(conf)
    cam.set_shutter(False)
    return asi_driver.AsiWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path), service=service)


def wait_for_start(worker, monkeypatch, seconds=SETTLE):
    """Run ``_wait_for_start`` on a thread, pretending a terminal is attached.

    ``input()`` is replaced by a block, so the loop runs exactly as it does
    while a real operator has not yet pressed Enter.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    released = threading.Event()
    monkeypatch.setattr("builtins.input", lambda *a: released.wait(30))

    done = threading.Event()
    thread = threading.Thread(
        target=lambda: (worker._wait_for_start(), done.set()), daemon=True)
    thread.start()
    return thread, released, done


# ---------------------------------------------------------------------------
# The bug itself
# ---------------------------------------------------------------------------
def test_japan_serves_a_focus_frame_before_enter(tmp_path, monkeypatch):
    service = CameraService("japan", "test", str(tmp_path))
    worker = japan_worker(tmp_path, service)
    service.request_focus(30)

    thread, released, done = wait_for_start(worker, monkeypatch)
    try:
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline and service.latest()[0] is None:
            time.sleep(0.05)
        frame, ts, _meta, counter = service.latest()
        assert frame is not None, "no live frame arrived while waiting for Enter"
        assert counter >= 1
        # And the shutter was opened for it, or the frame would be a dark.
        assert worker.cam.shutter_open is True
    finally:
        released.set()
        worker.request_stop()
        thread.join(timeout=10)


def test_asi_serves_a_focus_frame_before_enter(tmp_path, monkeypatch):
    service = CameraService("asi", "test", str(tmp_path))
    worker = asi_worker(tmp_path, service)
    service.request_focus(30)

    thread, released, done = wait_for_start(worker, monkeypatch)
    try:
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline and service.latest()[0] is None:
            time.sleep(0.05)
        assert service.latest()[0] is not None, \
            "no live frame arrived while waiting for Enter"
        assert worker.cam.shutter_open is True
    finally:
        released.set()
        worker.request_stop()
        thread.join(timeout=10)


def test_parameters_are_still_served_while_waiting(tmp_path, monkeypatch):
    """The half that already worked must keep working."""
    service = CameraService("japan", "test", str(tmp_path))
    worker = japan_worker(tmp_path, service)
    req_id = service.request_params({"exposure": 0.25})

    thread, released, done = wait_for_start(worker, monkeypatch)
    try:
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline:
            result = service.param_result(req_id)
            if result and result.get("done"):
                break
            time.sleep(0.05)
        assert service.param_result(req_id).get("applied") == {"exposure": 0.25}
    finally:
        released.set()
        worker.request_stop()
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# The shutter, which the fix borrows and must give back
# ---------------------------------------------------------------------------
def test_the_shutter_is_shut_again_when_the_session_ends(tmp_path, monkeypatch):
    service = CameraService("japan", "test", str(tmp_path))
    worker = japan_worker(tmp_path, service)
    service.request_focus(30)

    thread, released, done = wait_for_start(worker, monkeypatch)
    try:
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline and not worker.cam.shutter_open:
            time.sleep(0.05)
        assert worker.cam.shutter_open is True

        service.stop_focus()
        deadline = time.monotonic() + SETTLE
        while time.monotonic() < deadline and worker.cam.shutter_open:
            time.sleep(0.05)
        assert worker.cam.shutter_open is False
    finally:
        released.set()
        worker.request_stop()
        thread.join(timeout=10)


def test_pressing_enter_hands_the_shutter_back(tmp_path, monkeypatch):
    """The pre-darks are about to need the shutter shut."""
    service = CameraService("japan", "test", str(tmp_path))
    worker = japan_worker(tmp_path, service)
    service.request_focus(30)

    thread, released, done = wait_for_start(worker, monkeypatch)
    deadline = time.monotonic() + SETTLE
    while time.monotonic() < deadline and not worker.cam.shutter_open:
        time.sleep(0.05)
    assert worker.cam.shutter_open is True

    released.set()                          # the operator presses Enter
    assert done.wait(10), "_wait_for_start never returned"
    assert worker.cam.shutter_open is False
    thread.join(timeout=10)


def test_a_shutter_closed_by_hand_stays_closed(tmp_path):
    """focus_app has a shutter control; opening for focus must not fight it."""
    service = CameraService("japan", "test", str(tmp_path))
    worker = japan_worker(tmp_path, service, mode="sun")
    service.request_focus(30)

    worker._shutter_for_focus()
    assert worker.cam.shutter_open is True          # opened for the session

    service.request_params({"shutter": False})
    worker._serve_params()
    assert worker.cam.shutter_open is False

    for _ in range(5):
        worker._shutter_for_focus()
    assert worker.cam.shutter_open is False, "focus reopened a hand-closed shutter"

    # The session ending releases the operator's claim on it.
    service.stop_focus()
    worker._shutter_for_focus()
    service.request_focus(30)
    worker._shutter_for_focus()
    assert worker.cam.shutter_open is True


def test_a_dark_run_keeps_the_shutter_shut(tmp_path):
    """Focus must not reopen the shutter halfway through the darks."""
    service = CameraService("japan", "test", str(tmp_path))
    worker = japan_worker(tmp_path, service)
    service.request_focus(30)

    shutter_states = []
    original = worker._capture_one

    def watched(*args, **kwargs):
        # Serving focus between darks is exactly what would ruin them.
        worker._service_tick(None, last_status=0.0)
        shutter_states.append(worker.cam.shutter_open)
        return original(*args, **kwargs)

    worker._capture_one = watched
    worker._capture_darks("initial")

    assert shutter_states, "no dark frames were taken"
    assert not any(shutter_states), "the shutter was open during a dark run"
    assert worker._darks_running is False
    # Frames were filed as darks, not lights.
    assert all(p.name.endswith("_bg.fits") for p in tmp_path.glob("*.fits"))


def test_a_refused_focus_frame_says_why(tmp_path):
    """Silence is what made a starved focus session look like a broken camera."""
    service = CameraService("japan", "test", str(tmp_path))
    worker = japan_worker(tmp_path, service)
    service.request_focus(30)
    worker.cam.set_exposure(30.0)

    worker._serve_focus(slack=2.0)          # far too little room
    assert service.latest()[0] is None
    note = service.status().get("focus_note", "")
    assert "2 s" in note and "live frame" in note

    # And a served frame clears it again.
    worker.cam.set_exposure(0.05)
    worker._serve_focus(slack=60.0)
    assert service.latest()[0] is not None
    assert not service.status().get("focus_note")
