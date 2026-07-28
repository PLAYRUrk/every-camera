"""The ASI shutter as a remotely settable parameter.

The shutter lives on the filter-wheel controller and used to be moved only by
the observing programme: shut for darks, opened once measurements began. A
camera parked in setup mode therefore sat behind a closed shutter, and
focus_app.py — which exists to look through it — had no way to open it. These
tests pin down the parameter that fixed that: how values are read, what is
refused, and that the state travels back to the client.
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camera_service import PARAM_SCHEMAS                    # noqa: E402
from cameras import asi_driver                              # noqa: E402
from cameras.asi import config as asi_config, devices        # noqa: E402

SIM = {"camera": {"backend": "sim"}, "filter_wheel": {"port": "sim"}}


@pytest.fixture
def cam():
    """An :class:`AsiCamera` on both simulators — no hardware, no cooling wait."""
    cfg = asi_config.from_dict(SIM)
    camera = asi_driver.AsiCamera(cfg)
    camera.cam = devices.make_camera(cfg)
    camera.wheel = devices.make_wheel(cfg)
    return camera


@pytest.fixture
def worker(cam, tmp_path):
    return asi_driver.AsiWorkerConsole(
        cam=cam, cfg=asi_config.from_dict(SIM), output_dir=str(tmp_path),
        instance_name="test", status_dir=str(tmp_path))


# -- reading a value ---------------------------------------------------------
@pytest.mark.parametrize("value", [True, 1, "true", "on", "open", "Opened"])
def test_words_for_open_are_all_read_as_open(value):
    assert asi_driver._as_bool(value) is True


@pytest.mark.parametrize("value", [False, 0, "false", "off", "closed", "SHUT"])
def test_words_for_closed_are_all_read_as_closed(value):
    # "closed" is a non-empty string: plain bool() would open the shutter on it.
    assert asi_driver._as_bool(value) is False


@pytest.mark.parametrize("value", ["maybe", "", "1.5 volts", None])
def test_an_ambiguous_value_is_refused_rather_than_guessed(value):
    with pytest.raises(ValueError):
        asi_driver._as_bool(value)


# -- the camera facade -------------------------------------------------------
def test_the_shutter_state_is_unknown_until_it_is_commanded():
    # The real controller never reports its state, so the wheel starts with
    # none to report and focus_app is shown nothing rather than a guess.
    from cameras.asi.filterwheel import FilterWheel

    assert FilterWheel("/dev/null", 9600).shutter_open is None


def test_commanding_the_shutter_records_what_it_was_told(cam):
    cam.set_shutter(True)
    assert cam.shutter_open is True
    cam.set_shutter(False)
    assert cam.shutter_open is False


# -- the parameter -----------------------------------------------------------
def test_applying_the_shutter_moves_it_and_reports_it(worker, cam):
    applied, errors = worker._apply_params({"shutter": True})
    assert errors == []
    assert applied == {"shutter": True}
    assert cam.shutter_open is True


def test_a_worded_value_from_mqtt_applies_too(worker, cam):
    cam.set_shutter(True)
    applied, errors = worker._apply_params({"shutter": "closed"})
    assert errors == []
    assert applied == {"shutter": False}
    assert cam.shutter_open is False


def test_a_bad_value_is_reported_and_leaves_the_shutter_alone(worker, cam):
    cam.set_shutter(True)
    applied, errors = worker._apply_params({"shutter": "ajar"})
    assert applied == {}
    assert errors and "shutter" in errors[0]
    assert cam.shutter_open is True      # a rejected request changes nothing


def test_the_current_state_is_published_to_the_client(worker, cam):
    cam.set_shutter(True)
    assert worker._current_params()["shutter"] is True


# -- the wheel's position ----------------------------------------------------
def test_a_homed_wheel_reports_home_not_unknown(cam):
    # The complaint this fixes: a camera just started, wheel homed and idle,
    # showed "filter: unknown" as though the position had been lost.
    cam.wheel.__enter__()
    assert cam.current_filter == 0
    assert asi_driver._filter_text(cam.current_filter) == "home"


def test_a_position_that_was_never_confirmed_stays_unknown():
    from cameras.asi.filterwheel import FilterWheel

    wheel = FilterWheel("/dev/null", 9600)
    assert wheel.current_filter is None
    assert asi_driver._filter_text(wheel.current_filter) == "unknown"


def test_an_unknown_position_is_still_written_to_the_archive_as_zero(cam):
    # File names and FITS headers have always used 0 here; growing a second
    # spelling would break tooling that reads the archive.
    assert cam.current_filter is None
    assert cam.filter_number == 0


def test_home_is_a_position_the_form_can_ask_for(worker, cam):
    cam.wheel.select(3)
    applied, errors = worker._apply_params({"filter": 0})
    assert errors == []
    assert applied == {"filter": 0}
    assert cam.current_filter == 0


def test_the_schema_offers_home_as_a_filter_choice():
    field = next(f for f in PARAM_SCHEMAS["asi"] if f["name"] == "filter")
    assert [c["value"] for c in field["choices"]][0] == 0


def test_the_schema_offers_the_shutter_as_a_switch():
    # focus_app renders whatever the camera describes; without this field there
    # is no shutter control in the app at all.
    field = next(f for f in PARAM_SCHEMAS["asi"] if f["name"] == "shutter")
    assert field["type"] == "bool"
