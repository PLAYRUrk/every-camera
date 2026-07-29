"""Remote parameter editing for the Japan camera, as focus_app drives it.

The form focus_app draws comes from ``PARAM_SCHEMAS["japan"]``; what the worker
will actually apply comes from ``_apply_params``. When those two disagree the
symptom is a field that looks editable and silently does nothing, so both ends are
checked here — and so is the refusal path, because a value the camera cannot take
must be reported rather than half-applied.

Two whole controls are absent by design: this camera has no analog gain, and its
sensor temperature is a reading with no setpoint behind it.
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import japan_driver                             # noqa: E402
from cameras.japan import config as japan_config, devices    # noqa: E402
from camera_service import PARAM_SCHEMAS                     # noqa: E402


@pytest.fixture
def worker(tmp_path):
    conf = japan_config.from_dict({
        "output_dir": str(tmp_path),
        "camera": {"backend": "sim", "binning": 1},
        "filter_wheel": {"port": "sim"},
        "schedule": [{"filter": 1, "exposure": 0.05, "seconds": [0]}],
    })
    cam = japan_driver.JapanCamera(conf)
    cam.cam = devices.make_camera(conf)
    cam.wheel = devices.make_wheel(conf)
    cam.wheel.__enter__()          # homes the simulated wheel, as open() would
    return japan_driver.JapanWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------
def test_the_schema_offers_no_gain_and_no_setpoint():
    names = {field["name"] for field in PARAM_SCHEMAS["japan"]}
    assert names == {"exposure", "binning", "readout_speed", "filter", "shutter"}


def test_home_is_shown_but_never_offered_as_a_destination():
    """The wheel parks there; the schedule always names a real filter."""
    field = next(f for f in PARAM_SCHEMAS["japan"] if f["name"] == "filter")
    assert [state["value"] for state in field["states"]] == [0]
    assert [choice["value"] for choice in field["choices"]] == [1, 2, 3, 4, 5, 6]


def test_the_readout_speeds_are_the_two_the_camera_has():
    field = next(f for f in PARAM_SCHEMAS["japan"]
                 if f["name"] == "readout_speed")
    assert [choice["value"] for choice in field["choices"]] == [1, 2]


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------
def test_every_parameter_in_the_schema_can_actually_be_applied(worker):
    applied, errors = worker._apply_params({
        "exposure": 12.5, "binning": 4, "readout_speed": 1, "filter": 3,
        "shutter": True,
    })
    assert errors == []
    assert applied == {"exposure": 12.5, "binning": 4, "readout_speed": 1,
                       "filter": 3, "shutter": True}
    assert worker.cam.current_exposure == pytest.approx(12.5)
    assert worker.cam.current_binning == 4
    assert worker.cam.current_readout_speed == 1
    assert worker.cam.current_filter == 3
    assert worker.cam.shutter_open is True


def test_the_shutter_accepts_the_words_a_person_would_type(worker):
    for value, expected in (("closed", False), ("open", True), (0, False),
                            ("yes", True)):
        applied, errors = worker._apply_params({"shutter": value})
        assert errors == [], value
        assert applied["shutter"] is expected, value


def test_an_ambiguous_shutter_value_is_refused_rather_than_guessed(worker):
    worker.cam.set_shutter(True)
    applied, errors = worker._apply_params({"shutter": "maybe"})
    assert applied == {}
    assert errors and "shutter" in errors[0]
    assert worker.cam.shutter_open is True      # unchanged


# ---------------------------------------------------------------------------
# Refusing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("params, unchanged", [
    ({"readout_speed": 3}, "current_readout_speed"),
    ({"binning": 0}, "current_binning"),
    ({"binning": 9}, "current_binning"),
    ({"filter": 0}, "current_filter"),
    ({"filter": 7}, "current_filter"),
])
def test_a_value_this_camera_cannot_take_is_refused(worker, params, unchanged):
    before = getattr(worker.cam, unchanged)
    applied, errors = worker._apply_params(params)
    assert applied == {}
    assert errors, params
    assert getattr(worker.cam, unchanged) == before


def test_a_parameter_from_the_other_camera_is_named_not_ignored(worker):
    """``gain`` and ``target_temp`` are ASI controls; silence would imply they took."""
    applied, errors = worker._apply_params({"gain": 2, "target_temp": -60.0})
    assert applied == {}
    assert len(errors) == 2
    assert any("gain" in e for e in errors)
    assert any("target_temp" in e for e in errors)


def test_one_bad_field_does_not_discard_the_good_ones(worker):
    """A form is submitted whole; a typo in one row must not lose the others."""
    applied, errors = worker._apply_params({"exposure": 5.0, "binning": 99})
    assert applied == {"exposure": 5.0}
    assert len(errors) == 1
    assert worker.cam.current_exposure == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Reporting back
# ---------------------------------------------------------------------------
def test_what_is_published_is_what_the_camera_holds(worker):
    worker._apply_params({"exposure": 7.0, "binning": 2, "readout_speed": 1,
                          "filter": 5, "shutter": False})
    assert worker._current_params() == {
        "exposure": 7.0, "binning": 2, "readout_speed": 1, "filter": 5,
        "shutter": False,
    }


def test_a_just_started_camera_reports_home_rather_than_a_filter(worker):
    """0 is a place the wheel is; showing 1 would be a guess dressed as a reading."""
    assert worker._current_params()["filter"] == 0
    assert worker.cam.filter_number == 0
