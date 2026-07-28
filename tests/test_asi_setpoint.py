"""How the PIXIS class reacts to values the camera will not take.

The camera at the observatory rejected a cooling setpoint that had worked for
years on the previous one, and PICAM's answer ("Invalid Parameter Value") named
neither the parameter nor an acceptable value. These tests pin the behaviour
that replaced that: ask the camera what it allows, nudge the value into range
with a warning, and turn a rejection into a message that says what to fix.

No PICAM library and no camera: the SDK calls are stubbed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.asi import picam  # noqa: E402
from cameras.asi.camera import PixisCamera  # noqa: E402
from cameras.asi.picam import PicamParameter as P  # noqa: E402

SETPOINT_RANGE = picam.FloatRange(minimum=-55.0, maximum=25.0, increment=0.1)
GAIN_VALUES = [1.0, 2.0, 3.0]


@pytest.fixture
def camera(monkeypatch):
    """A PixisCamera with the SDK replaced by recorders."""
    cam = PixisCamera.__new__(PixisCamera)
    cam._handle = object()
    cam.writes = []

    limits = {P.SensorTemperatureSetPoint: SETPOINT_RANGE, P.AdcAnalogGain: GAIN_VALUES}
    monkeypatch.setattr(picam, "constraint",
                        lambda handle, parameter: limits.get(parameter))
    monkeypatch.setattr(picam, "set_float",
                        lambda handle, parameter, value: cam.writes.append((parameter, value)))
    monkeypatch.setattr(picam, "set_int",
                        lambda handle, parameter, value: cam.writes.append((parameter, value)))
    # Naming a parameter goes through the SDK, which is not installed here.
    monkeypatch.setattr(picam, "enum_string",
                        lambda enum_type, value: f"parameter {int(value)}")
    return cam


def test_an_acceptable_setpoint_is_written_unchanged(camera):
    assert camera._set_checked(P.SensorTemperatureSetPoint, -50.0, "Setpoint") == -50.0
    assert camera.writes == [(P.SensorTemperatureSetPoint, -50.0)]


def test_an_impossible_setpoint_is_clamped_and_warned_about(camera, capsys):
    """-60 C on a camera that stops at -55: the exact failure that started this."""
    result = camera._set_checked(P.SensorTemperatureSetPoint, -60.0, "Sensor setpoint")

    assert result == pytest.approx(-55.0)
    assert camera.writes == [(P.SensorTemperatureSetPoint, pytest.approx(-55.0))]
    warning = capsys.readouterr().out
    assert "-60" in warning and "-55" in warning


def test_enumerated_parameters_are_written_as_integers(camera):
    """Modes go through set_int, not set_float."""
    assert camera._set_checked(P.AdcAnalogGain, 3, "Analog gain") == 3
    assert camera.writes == [(P.AdcAnalogGain, 3)]


def test_an_unsupported_mode_is_never_swapped_for_a_neighbour(camera, monkeypatch):
    """Nearest-number is meaningless for a mode: gain 5 is not "almost" gain 3."""
    monkeypatch.setattr(picam, "enum_string",
                        lambda enum_type, value: "AdcAnalogGain")

    with pytest.raises(RuntimeError, match="does not accept 5"):
        camera._set_checked(P.AdcAnalogGain, 5, "Analog gain")
    assert camera.writes == []


def test_an_unconstrained_parameter_passes_the_value_through(camera):
    assert camera._set_checked(P.ExposureTime, 55_000.0, "Exposure (ms)") == 55_000.0
    assert camera.writes == [(P.ExposureTime, 55_000.0)]


def test_exposure_is_never_silently_shortened(camera, monkeypatch):
    """A quietly shortened exposure would corrupt the measurement — stop instead."""
    too_short = picam.FloatRange(minimum=0.0, maximum=1000.0, increment=0.0)
    monkeypatch.setattr(picam, "constraint", lambda handle, parameter: too_short)

    with pytest.raises(RuntimeError, match="does not accept 55000"):
        camera._set_checked(P.ExposureTime, 55_000.0, "Exposure (ms)", clamp=False)
    assert camera.writes == []


def test_a_rejected_value_reports_what_was_allowed(camera, monkeypatch):
    def refuse(handle, parameter, value):
        raise picam.PicamError("Picam_SetParameterFloatingPointValue", 2)

    monkeypatch.setattr(picam, "set_float", refuse)
    monkeypatch.setattr(picam, "enum_string",
                        lambda enum_type, value: "SensorTemperatureSetPoint")
    monkeypatch.setattr(picam, "error_string", lambda code: "Invalid Parameter Value")

    with pytest.raises(RuntimeError) as exc:
        camera._set_checked(P.SensorTemperatureSetPoint, -50.0, "Sensor setpoint")

    message = str(exc.value)
    assert "Sensor setpoint" in message
    assert "-55" in message and "25" in message  # what it would have accepted
