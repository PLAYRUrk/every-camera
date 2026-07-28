"""Checks on the PICAM ctypes binding that need no library and no camera.

Every expectation here is transcribed from the vendor header (``picam.h``). If
the binding ever drifts from it these fail here, rather than the camera silently
misbehaving at the observatory.
"""
import ctypes
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.asi import picam  # noqa: E402
from cameras.asi.picam import PicamParameter as P  # noqa: E402

# (value_type, constraint_type, id) triples straight out of picam.h
HEADER_PARAMETERS = {
    "ExposureTime": (2, 2, 23),
    "ShutterTimingMode": (4, 3, 24),
    "ShutterOpeningDelay": (2, 2, 46),
    "ShutterClosingDelay": (2, 2, 25),
    "AdcSpeed": (2, 3, 33),
    "AdcAnalogGain": (4, 3, 35),
    "AdcQuality": (4, 3, 36),
    "OutputSignal": (4, 3, 32),
    "InvertOutputSignal": (3, 3, 52),
    "ReadoutControlMode": (4, 3, 26),
    "KineticsWindowHeight": (1, 2, 56),
    "Rois": (5, 4, 37),
    "ReadoutCount": (6, 2, 40),
    "PixelFormat": (4, 3, 41),
    "FrameSize": (1, 1, 42),
    "FrameStride": (1, 1, 43),
    "FramesPerReadout": (1, 1, 44),
    "ReadoutStride": (1, 1, 45),
    "PixelBitDepth": (1, 1, 48),
    "SensorTemperatureSetPoint": (2, 2, 14),
    "SensorTemperatureReading": (2, 1, 15),
    "SensorTemperatureStatus": (4, 1, 16),
}


@pytest.mark.parametrize("name,triple", sorted(HEADER_PARAMETERS.items()))
def test_parameter_constants_match_the_header(name, triple):
    value_type, constraint_type, number = triple
    expected = (constraint_type << 24) + (value_type << 16) + number
    assert int(getattr(P, name)) == expected


def test_every_declared_parameter_is_covered_by_this_test():
    assert set(P.__members__) == set(HEADER_PARAMETERS)


# Sizes produced by g++ for picam.h on x86-64 Linux (PIL_LIN64).
C_STRUCT_SIZES = {
    "PicamCameraID": 136,
    "PicamRoi": 24,
    "PicamRois": 16,
    "PicamAvailableData": 16,
    "PicamFirmwareDetail": 320,
    "PicamRangeConstraint": 72,
    "PicamCollectionConstraint": 24,
    "PicamRoisConstraint": 344,
    "PicamAcquisitionStatus": 16,
}


@pytest.mark.parametrize("name,size", sorted(C_STRUCT_SIZES.items()))
def test_struct_sizes_match_the_c_layout(name, size):
    assert ctypes.sizeof(getattr(picam, name)) == size


def test_struct_field_offsets_match_the_c_layout():
    """Offsets of the fields the binding actually dereferences."""
    assert picam.PicamCameraID.serial_number.offset == 72
    assert picam.PicamAvailableData.readout_count.offset == 8
    assert picam.PicamCollectionConstraint.values_array.offset == 8
    assert picam.PicamRangeConstraint.excluded_values_array.offset == 40
    assert picam.PicamRoisConstraint.width_constraint.offset == 96
    assert picam.PicamRoisConstraint.y_constraint.offset == 184
    assert picam.PicamRoisConstraint.height_constraint.offset == 256


def test_roi_field_order_matches_the_header():
    assert [f[0] for f in picam.PicamRoi._fields_] == [
        "x", "width", "x_binning", "y", "height", "y_binning",
    ]


def test_enumeration_values_match_the_header():
    assert picam.PicamAdcAnalogGain.High == 3
    assert picam.PicamOutputSignal.AlwaysLow == 4
    assert picam.PicamOutputSignal.Exposing == 8
    assert picam.PicamReadoutControlMode.FullFrame == 1
    assert picam.PicamReadoutControlMode.Kinetics == 3
    assert picam.PicamShutterTimingMode.Normal == 1
    assert picam.PicamSensorTemperatureStatus.Locked == 2
    assert picam.PicamConstraintCategory.Required == 2


def test_demo_models_are_pixis_model_ids():
    assert picam.DEMO_MODELS["Pixis1024F"] == 10
    assert picam.DEMO_MODELS["Pixis1024B"] == 11
    assert picam.DEMO_MODELS["Pixis2048B"] == 22


def test_parameter_constants_decode_back_to_their_types():
    """value_type/constraint_type undo PI_V, and the driver dispatches on them."""
    assert picam.value_type(P.SensorTemperatureSetPoint) is picam.PicamValueType.FloatingPoint
    assert picam.constraint_type(P.SensorTemperatureSetPoint) is picam.PicamConstraintType.Range
    assert picam.value_type(P.AdcAnalogGain) is picam.PicamValueType.Enumeration
    assert picam.constraint_type(P.AdcSpeed) is picam.PicamConstraintType.Collection
    assert picam.constraint_type(P.PixelBitDepth) is picam.PicamConstraintType.NoneType


# Clamping is pure arithmetic on what the camera reported, so it is checked here
# rather than at the observatory. The range mirrors a PIXIS cooling constraint.
SETPOINT = picam.FloatRange(minimum=-55.0, maximum=25.0, increment=0.1)


@pytest.mark.parametrize("value,expected", [
    (-20.0, -20.0),    # inside the range: untouched
    (-55.0, -55.0),    # exactly at the bottom
    (25.0, 25.0),      # exactly at the top
])
def test_allowed_setpoints_are_left_alone(value, expected):
    result, changed = picam.clamp_to_constraint(SETPOINT, value)
    assert result == pytest.approx(expected)
    assert changed is False


def test_setpoint_below_the_range_is_pulled_up_to_the_minimum():
    """The failure this whole path exists for: -60 C on a camera that stops at -55."""
    result, changed = picam.clamp_to_constraint(SETPOINT, -60.0)
    assert result == pytest.approx(-55.0)
    assert changed is True


def test_setpoint_above_the_range_is_pulled_down_to_the_maximum():
    result, changed = picam.clamp_to_constraint(SETPOINT, 40.0)
    assert result == pytest.approx(25.0)
    assert changed is True


def test_values_snap_to_the_increment():
    result, changed = picam.clamp_to_constraint(SETPOINT, -20.04)
    assert result == pytest.approx(-20.0)
    assert changed is True


def test_rounding_up_never_overshoots_the_maximum():
    limits = picam.FloatRange(minimum=0.0, maximum=9.5, increment=1.0)
    result, _ = picam.clamp_to_constraint(limits, 9.4)
    assert result == pytest.approx(9.0)


def test_excluded_values_are_stepped_over():
    limits = picam.FloatRange(minimum=0.0, maximum=10.0, increment=1.0,
                              excluded=(5.0,))
    result, changed = picam.clamp_to_constraint(limits, 5.0)
    assert result == pytest.approx(4.0)
    assert changed is True


def test_a_collection_picks_the_nearest_member():
    speeds = [0.1, 2.0]
    assert picam.clamp_to_constraint(speeds, 2.0) == (2.0, False)
    result, changed = picam.clamp_to_constraint(speeds, 1.6)
    assert result == pytest.approx(2.0)
    assert changed is True


def test_an_unconstrained_parameter_passes_the_value_through():
    assert picam.clamp_to_constraint(None, -273.0) == (-273.0, False)
    assert picam.clamp_to_constraint([], 7.0) == (7.0, False)


def test_describe_constraint_names_what_is_allowed():
    assert picam.describe_constraint(SETPOINT) == "-55 … 25, step 0.1"
    assert picam.describe_constraint([0.1, 2.0]) == "0.1, 2"
    assert picam.describe_constraint(None) == "unconstrained"
    long = picam.describe_constraint([float(i) for i in range(20)])
    assert long.endswith("(20 values)")


def test_missing_library_reports_a_useful_error(monkeypatch):
    monkeypatch.setattr(picam, "_lib", None)
    monkeypatch.setattr(picam, "_LIB_CANDIDATES", ("/nonexistent/libpicam.so",))
    monkeypatch.delenv("PICAM_LIB", raising=False)
    with pytest.raises(OSError, match="PICAM SDK"):
        picam._load()
