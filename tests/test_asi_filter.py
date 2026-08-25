"""What the ASI driver does when the filter wheel does not get where it was sent.

The failure this covers is a quiet one. The wheel reports that it could not
confirm a position, the frame is still taken, and it goes into the archive named
``..._none_055000ms.fits`` with ``FILTER = 0`` in its header — indistinguishable
at a glance from a dark taken at home. Nothing counted it, nothing published it,
and the run carried on filing frames that way until somebody read the archive a
day later.

So: the move reports its outcome, the worker counts it, and the name still says
``none`` — that marker is deliberate and worth keeping greppable.
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import asi_driver                                # noqa: E402
from cameras.asi import config as asi_config, devices, paths  # noqa: E402
from cameras.asi.schedule import Entry                        # noqa: E402

SIM = {"camera": {"backend": "sim"}, "filter_wheel": {"port": "sim"}}


@pytest.fixture
def cam():
    """An :class:`AsiCamera` on both simulators — no hardware, no cooling wait."""
    cfg = asi_config.from_dict(SIM)
    camera = asi_driver.AsiCamera(cfg)
    camera.cam = devices.make_camera(cfg)
    camera.wheel = devices.make_wheel(cfg)
    camera.wheel.__enter__()
    return camera


@pytest.fixture
def worker(cam, tmp_path):
    return asi_driver.AsiWorkerConsole(
        cam=cam, cfg=asi_config.from_dict(SIM), output_dir=str(tmp_path),
        instance_name="test", status_dir=str(tmp_path))


def slot(number=3):
    return Entry(filter=number, exposure=0.01, seconds=[0])


# -- the camera facade -------------------------------------------------------
def test_preparing_a_slot_reports_that_the_filter_arrived(cam):
    assert cam.prepare(slot(3)) is True
    assert cam.current_filter == 3


def test_preparing_a_slot_reports_a_filter_that_did_not_arrive(cam):
    cam.wheel.fail_selects = 1
    assert cam.prepare(slot(3)) is False
    assert cam.current_filter is None


def test_a_slot_that_names_no_filter_leaves_the_wheel_alone(cam):
    cam.prepare(slot(3))
    assert cam.prepare(Entry(filter=None, exposure=0.01, seconds=[0])) is True
    assert cam.current_filter == 3


# -- what the worker does with that ------------------------------------------
def test_a_failed_filter_move_is_counted_rather_than_silently_filed(worker):
    worker.cam.wheel.fail_selects = 1
    # True: the slot is still shot. A gap in the series costs more than a frame
    # labelled with what the wheel actually reports.
    assert worker._prepare_entry(slot(3)) is True
    assert worker._errors == 1


def test_a_filter_move_that_worked_costs_nothing(worker):
    assert worker._prepare_entry(slot(3)) is True
    assert worker._errors == 0


def test_a_preparation_that_broke_outright_drops_the_slot(worker, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("/dev/ttyUSB0 went away")

    monkeypatch.setattr(worker.cam, "prepare", explode)
    assert worker._prepare_entry(slot(3)) is False
    assert worker._errors == 1


# -- the marker left in the archive ------------------------------------------
def test_a_frame_with_an_unknown_filter_is_named_so_it_can_be_found(tmp_path):
    # ``none`` is the deliberate stand-in for a wavelength tag, and a night that
    # went wrong is found by grepping for it.
    name = paths.frame_name(__import__("datetime").datetime(2026, 7, 28, 20, 30),
                            site_id="TORY", device_id="ASI0", wavelength="",
                            exposure_sec=55)
    assert "_none_" in name
