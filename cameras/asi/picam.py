"""Minimal ctypes binding to the Princeton Instruments PICAM library (Linux).

Covers exactly what the imager needs: opening a camera, reading/writing the
parameters used by the schedule, committing them to hardware, and acquiring
single readouts.

Everything here is transcribed from the vendor header shipped with the legacy
project, ``imagerd_rt/src/imagerd_rt/src/include/picam.h`` (and
``pil_platform.h`` for the native type sizes on ``PIL_LIN64``).
"""
from __future__ import annotations

import ctypes
import os
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char,
    c_char_p,
    c_double,
    c_int,
    c_longlong,
    c_void_p,
)
from enum import IntEnum

# --- native types (pil_platform.h, PIL_LIN64) -------------------------------
piint = c_int
piflt = c_double
pibln = c_int
pi64s = c_longlong
PicamHandle = c_void_p

NO_TIMEOUT = -1
"""Sentinel accepted by Picam_Acquire's ``readout_time_out``: wait forever."""


class PicamValueType(IntEnum):
    Integer = 1
    FloatingPoint = 2
    Boolean = 3
    Enumeration = 4
    Rois = 5
    LargeInteger = 6
    Pulse = 7
    Modulations = 8


class PicamConstraintType(IntEnum):
    NoneType = 1
    Range = 2
    Collection = 3
    Rois = 4
    Pulse = 5
    Modulations = 6


def _pi_v(value_type: PicamValueType, constraint_type: PicamConstraintType, n: int) -> int:
    """The PI_V macro from picam.h:634 that encodes a PicamParameter constant."""
    return (int(constraint_type) << 24) + (int(value_type) << 16) + n


_V = PicamValueType
_C = PicamConstraintType


class PicamParameter(IntEnum):
    ExposureTime = _pi_v(_V.FloatingPoint, _C.Range, 23)
    ShutterTimingMode = _pi_v(_V.Enumeration, _C.Collection, 24)
    ShutterOpeningDelay = _pi_v(_V.FloatingPoint, _C.Range, 46)
    ShutterClosingDelay = _pi_v(_V.FloatingPoint, _C.Range, 25)
    AdcSpeed = _pi_v(_V.FloatingPoint, _C.Collection, 33)
    AdcAnalogGain = _pi_v(_V.Enumeration, _C.Collection, 35)
    AdcQuality = _pi_v(_V.Enumeration, _C.Collection, 36)
    OutputSignal = _pi_v(_V.Enumeration, _C.Collection, 32)
    InvertOutputSignal = _pi_v(_V.Boolean, _C.Collection, 52)
    ReadoutControlMode = _pi_v(_V.Enumeration, _C.Collection, 26)
    KineticsWindowHeight = _pi_v(_V.Integer, _C.Range, 56)
    Rois = _pi_v(_V.Rois, _C.Rois, 37)
    ReadoutCount = _pi_v(_V.LargeInteger, _C.Range, 40)
    PixelFormat = _pi_v(_V.Enumeration, _C.Collection, 41)
    FrameSize = _pi_v(_V.Integer, _C.NoneType, 42)
    FrameStride = _pi_v(_V.Integer, _C.NoneType, 43)
    FramesPerReadout = _pi_v(_V.Integer, _C.NoneType, 44)
    ReadoutStride = _pi_v(_V.Integer, _C.NoneType, 45)
    PixelBitDepth = _pi_v(_V.Integer, _C.NoneType, 48)
    SensorTemperatureSetPoint = _pi_v(_V.FloatingPoint, _C.Range, 14)
    SensorTemperatureReading = _pi_v(_V.FloatingPoint, _C.NoneType, 15)
    SensorTemperatureStatus = _pi_v(_V.Enumeration, _C.NoneType, 16)


class PicamEnumeratedType(IntEnum):
    Error = 1
    Model = 2
    ComputerInterface = 3
    Parameter = 6
    AdcAnalogGain = 7
    OutputSignal = 11
    PixelFormat = 12
    ReadoutControlMode = 13
    SensorTemperatureStatus = 14
    ShutterTimingMode = 16
    AcquisitionErrorsMask = 25


class PicamConstraintCategory(IntEnum):
    Capable = 1
    Required = 2
    Recommended = 3


class PicamAdcAnalogGain(IntEnum):
    Low = 1
    Medium = 2
    High = 3


class PicamOutputSignal(IntEnum):
    NotReadingOut = 1
    ShutterOpen = 2
    Busy = 3
    AlwaysLow = 4
    AlwaysHigh = 5
    Acquiring = 6
    ShiftingUnderMask = 7
    Exposing = 8
    EffectivelyExposing = 9
    ReadingOut = 10
    WaitingForTrigger = 11


class PicamReadoutControlMode(IntEnum):
    FullFrame = 1
    FrameTransfer = 2
    Kinetics = 3
    SpectraKinetics = 4
    Interline = 5
    Dif = 6


class PicamShutterTimingMode(IntEnum):
    Normal = 1
    AlwaysClosed = 2
    AlwaysOpen = 3
    OpenBeforeTrigger = 4


class PicamSensorTemperatureStatus(IntEnum):
    Unlocked = 1
    Locked = 2


class PicamComputerInterface(IntEnum):
    Usb2 = 1
    IEEE1394A = 2
    GigabitEthernet = 3


# Only the demo models that make sense for this imager; the full PicamModel
# enumeration has a few hundred entries and PICAM resolves names for us.
DEMO_MODELS = {
    "Pixis1024F": 10,
    "Pixis1024B": 11,
    "Pixis1024BR": 13,
    "Pixis1024BUV": 12,
    "Pixis2048F": 21,
    "Pixis2048B": 22,
    "Pixis2048BR": 67,
    "Pixis512F": 44,
    "Pixis512B": 45,
    "Pixis400F": 38,
    "Pixis400B": 40,
}

STRING_SIZE_SENSOR_NAME = 64
STRING_SIZE_SERIAL_NUMBER = 64
STRING_SIZE_FIRMWARE_NAME = 64
STRING_SIZE_FIRMWARE_DETAIL = 256


# --- structures (picam.h) ---------------------------------------------------
class PicamCameraID(Structure):
    _fields_ = [
        ("model", piint),
        ("computer_interface", piint),
        ("sensor_name", c_char * STRING_SIZE_SENSOR_NAME),
        ("serial_number", c_char * STRING_SIZE_SERIAL_NUMBER),
    ]


class PicamFirmwareDetail(Structure):
    _fields_ = [
        ("name", c_char * STRING_SIZE_FIRMWARE_NAME),
        ("detail", c_char * STRING_SIZE_FIRMWARE_DETAIL),
    ]


class PicamRoi(Structure):
    _fields_ = [
        ("x", piint),
        ("width", piint),
        ("x_binning", piint),
        ("y", piint),
        ("height", piint),
        ("y_binning", piint),
    ]


class PicamRois(Structure):
    _fields_ = [
        ("roi_array", POINTER(PicamRoi)),
        ("roi_count", piint),
    ]


class PicamAvailableData(Structure):
    _fields_ = [
        ("initial_readout", c_void_p),
        ("readout_count", pi64s),
    ]


class PicamAcquisitionStatus(Structure):
    _fields_ = [
        ("running", pibln),
        ("errors", piint),
        ("readout_rate", piflt),
    ]


class PicamRangeConstraint(Structure):
    _fields_ = [
        ("scope", piint),
        ("severity", piint),
        ("empty_set", pibln),
        ("minimum", piflt),
        ("maximum", piflt),
        ("increment", piflt),
        ("excluded_values_array", POINTER(piflt)),
        ("excluded_values_count", piint),
        ("outlying_values_array", POINTER(piflt)),
        ("outlying_values_count", piint),
    ]


class PicamRoisConstraint(Structure):
    _fields_ = [
        ("scope", piint),
        ("severity", piint),
        ("empty_set", pibln),
        ("rules", piint),
        ("maximum_roi_count", piint),
        ("x_constraint", PicamRangeConstraint),
        ("width_constraint", PicamRangeConstraint),
        ("x_binning_limits_array", POINTER(piint)),
        ("x_binning_limits_count", piint),
        ("y_constraint", PicamRangeConstraint),
        ("height_constraint", PicamRangeConstraint),
        ("y_binning_limits_array", POINTER(piint)),
        ("y_binning_limits_count", piint),
    ]


class PicamError(RuntimeError):
    """A PICAM call returned a non-zero PicamError code."""

    def __init__(self, function: str, code: int) -> None:
        self.function = function
        self.code = code
        super().__init__(f"{function} failed: {error_string(code)} (code {code})")


_LIB_CANDIDATES = (
    "libpicam.so",
    "/usr/local/lib/libpicam.so",
    "/opt/PrincetonInstruments/picam/runtime/libpicam.so",
    "/usr/local/lib/picam/libpicam.so",
)

_lib: ctypes.CDLL | None = None


def _load() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    candidates = []
    env = os.environ.get("PICAM_LIB")
    if env:
        candidates.append(env)
    candidates.extend(_LIB_CANDIDATES)
    errors = []
    for name in candidates:
        try:
            _lib = ctypes.CDLL(name)
        except OSError as exc:
            errors.append(f"  {name}: {exc}")
            continue
        _declare(_lib)
        return _lib
    raise OSError(
        "Cannot load the PICAM runtime (libpicam.so). Install the Princeton "
        "Instruments PICAM SDK for Linux, or point PICAM_LIB at the library.\n"
        "Tried:\n" + "\n".join(errors)
    )


def _declare(lib: ctypes.CDLL) -> None:
    """Attach argtypes/restype to every function this module calls."""
    sigs = {
        "Picam_InitializeLibrary": ([], piint),
        "Picam_UninitializeLibrary": ([], piint),
        "Picam_GetVersion": (
            [POINTER(piint), POINTER(piint), POINTER(piint), POINTER(piint)],
            piint,
        ),
        "Picam_DestroyString": ([c_char_p], piint),
        "Picam_GetEnumerationString": ([piint, piint, POINTER(c_char_p)], piint),
        "Picam_OpenFirstCamera": ([POINTER(PicamHandle)], piint),
        "Picam_OpenCamera": ([POINTER(PicamCameraID), POINTER(PicamHandle)], piint),
        "Picam_CloseCamera": ([PicamHandle], piint),
        "Picam_GetCameraID": ([PicamHandle, POINTER(PicamCameraID)], piint),
        "Picam_ConnectDemoCamera": ([piint, c_char_p, POINTER(PicamCameraID)], piint),
        "Picam_DisconnectDemoCamera": ([POINTER(PicamCameraID)], piint),
        "Picam_GetFirmwareDetails": (
            [POINTER(PicamCameraID), POINTER(POINTER(PicamFirmwareDetail)), POINTER(piint)],
            piint,
        ),
        "Picam_DestroyFirmwareDetails": ([POINTER(PicamFirmwareDetail)], piint),
        "Picam_GetParameterIntegerValue": ([PicamHandle, piint, POINTER(piint)], piint),
        "Picam_SetParameterIntegerValue": ([PicamHandle, piint, piint], piint),
        "Picam_GetParameterFloatingPointValue": ([PicamHandle, piint, POINTER(piflt)], piint),
        "Picam_SetParameterFloatingPointValue": ([PicamHandle, piint, piflt], piint),
        "Picam_ReadParameterFloatingPointValue": ([PicamHandle, piint, POINTER(piflt)], piint),
        "Picam_GetParameterRoisValue": ([PicamHandle, piint, POINTER(POINTER(PicamRois))], piint),
        "Picam_SetParameterRoisValue": ([PicamHandle, piint, POINTER(PicamRois)], piint),
        "Picam_DestroyRois": ([POINTER(PicamRois)], piint),
        "Picam_GetParameterRoisConstraint": (
            [PicamHandle, piint, piint, POINTER(POINTER(PicamRoisConstraint))],
            piint,
        ),
        "Picam_DestroyRoisConstraints": ([POINTER(PicamRoisConstraint)], piint),
        "Picam_AreParametersCommitted": ([PicamHandle, POINTER(pibln)], piint),
        "Picam_CommitParameters": (
            [PicamHandle, POINTER(POINTER(piint)), POINTER(piint)],
            piint,
        ),
        "Picam_DestroyParameters": ([POINTER(piint)], piint),
        "Picam_Acquire": (
            [PicamHandle, pi64s, piint, POINTER(PicamAvailableData), POINTER(piint)],
            piint,
        ),
        "Picam_StopAcquisition": ([PicamHandle], piint),
        "Picam_IsAcquisitionRunning": ([PicamHandle, POINTER(pibln)], piint),
    }
    for name, (argtypes, restype) in sigs.items():
        try:
            fn = getattr(lib, name)
        except AttributeError as exc:
            raise OSError(
                f"The PICAM library at {lib._name} does not export {name}; "
                "it is probably too old for this program."
            ) from exc
        fn.argtypes = argtypes
        fn.restype = restype


def error_string(code: int) -> str:
    """Human-readable name of a PicamError code (falls back to the number)."""
    if code == 0:
        return "None"
    try:
        lib = _load()
        s = c_char_p()
        if lib.Picam_GetEnumerationString(
            int(PicamEnumeratedType.Error), int(code), byref(s)
        ) == 0:
            text = s.value.decode(errors="replace") if s.value else str(code)
            lib.Picam_DestroyString(s)
            return text
    except Exception:
        pass
    return str(code)


def enum_string(enum_type: PicamEnumeratedType, value: int) -> str:
    """Human-readable name of any PICAM enumeration value."""
    lib = _load()
    s = c_char_p()
    if lib.Picam_GetEnumerationString(int(enum_type), int(value), byref(s)) != 0:
        return str(value)
    text = s.value.decode(errors="replace") if s.value else str(value)
    lib.Picam_DestroyString(s)
    return text


def _check(name: str, code: int) -> None:
    if code != 0:
        raise PicamError(name, code)


def _call(name: str, *args) -> None:
    _check(name, getattr(_load(), name)(*args))


# --- library lifetime -------------------------------------------------------
def initialize_library() -> None:
    _call("Picam_InitializeLibrary")


def uninitialize_library() -> None:
    _call("Picam_UninitializeLibrary")


def library_version() -> str:
    major, minor, distribution, released = (piint() for _ in range(4))
    _call(
        "Picam_GetVersion",
        byref(major),
        byref(minor),
        byref(distribution),
        byref(released),
    )
    return f"{major.value}.{minor.value}.{distribution.value}.{released.value}"


# --- camera access ----------------------------------------------------------
def open_first_camera() -> PicamHandle:
    handle = PicamHandle()
    _call("Picam_OpenFirstCamera", byref(handle))
    return handle


def connect_demo_camera(model: int, serial_number: str) -> PicamCameraID:
    cid = PicamCameraID()
    _call("Picam_ConnectDemoCamera", int(model), serial_number.encode(), byref(cid))
    return cid


def disconnect_demo_camera(cid: PicamCameraID) -> None:
    _call("Picam_DisconnectDemoCamera", byref(cid))


def open_camera(cid: PicamCameraID) -> PicamHandle:
    handle = PicamHandle()
    _call("Picam_OpenCamera", byref(cid), byref(handle))
    return handle


def close_camera(handle: PicamHandle) -> None:
    _call("Picam_CloseCamera", handle)


def get_camera_id(handle: PicamHandle) -> PicamCameraID:
    cid = PicamCameraID()
    _call("Picam_GetCameraID", handle, byref(cid))
    return cid


def firmware_details(cid: PicamCameraID) -> list[tuple[str, str]]:
    array = POINTER(PicamFirmwareDetail)()
    count = piint()
    _call("Picam_GetFirmwareDetails", byref(cid), byref(array), byref(count))
    try:
        return [
            (
                array[i].name.decode(errors="replace"),
                array[i].detail.decode(errors="replace"),
            )
            for i in range(count.value)
        ]
    finally:
        _load().Picam_DestroyFirmwareDetails(array)


# --- parameters -------------------------------------------------------------
def get_int(handle: PicamHandle, parameter: PicamParameter) -> int:
    value = piint()
    _call("Picam_GetParameterIntegerValue", handle, int(parameter), byref(value))
    return value.value


def set_int(handle: PicamHandle, parameter: PicamParameter, value: int) -> None:
    _call("Picam_SetParameterIntegerValue", handle, int(parameter), int(value))


def get_float(handle: PicamHandle, parameter: PicamParameter) -> float:
    value = piflt()
    _call("Picam_GetParameterFloatingPointValue", handle, int(parameter), byref(value))
    return value.value


def set_float(handle: PicamHandle, parameter: PicamParameter, value: float) -> None:
    _call("Picam_SetParameterFloatingPointValue", handle, int(parameter), float(value))


def read_float(handle: PicamHandle, parameter: PicamParameter) -> float:
    """Read a live value from hardware (as opposed to the cached parameter)."""
    value = piflt()
    _call("Picam_ReadParameterFloatingPointValue", handle, int(parameter), byref(value))
    return value.value


def get_rois(handle: PicamHandle) -> POINTER(PicamRois):
    """Return the current ROI set. The caller must pass it to destroy_rois()."""
    rois = POINTER(PicamRois)()
    _call("Picam_GetParameterRoisValue", handle, int(PicamParameter.Rois), byref(rois))
    return rois


def set_rois(handle: PicamHandle, rois: POINTER(PicamRois)) -> None:
    _call("Picam_SetParameterRoisValue", handle, int(PicamParameter.Rois), rois)


def destroy_rois(rois) -> None:
    _load().Picam_DestroyRois(rois)


def sensor_size(handle: PicamHandle) -> tuple[int, int]:
    """Full sensor (width, height) from the required ROI constraint."""
    constraint = POINTER(PicamRoisConstraint)()
    _call(
        "Picam_GetParameterRoisConstraint",
        handle,
        int(PicamParameter.Rois),
        int(PicamConstraintCategory.Required),
        byref(constraint),
    )
    try:
        return (
            int(constraint.contents.width_constraint.maximum),
            int(constraint.contents.height_constraint.maximum),
        )
    finally:
        _load().Picam_DestroyRoisConstraints(constraint)


def commit(handle: PicamHandle) -> list[int]:
    """Apply pending parameter changes; returns the parameters that failed."""
    failed = POINTER(piint)()
    count = piint()
    _call("Picam_CommitParameters", handle, byref(failed), byref(count))
    try:
        return [failed[i] for i in range(count.value)]
    finally:
        _load().Picam_DestroyParameters(failed)


def are_parameters_committed(handle: PicamHandle) -> bool:
    committed = pibln()
    _call("Picam_AreParametersCommitted", handle, byref(committed))
    return bool(committed.value)


# --- acquisition ------------------------------------------------------------
def acquire(handle: PicamHandle, readout_count: int, timeout_ms: int) -> tuple[int, int, int]:
    """Acquire ``readout_count`` readouts.

    Returns ``(buffer_address, readouts_returned, acquisition_errors_mask)``.
    The buffer belongs to PICAM and is reused by the next acquisition, so the
    caller must copy out of it before acquiring again.
    """
    data = PicamAvailableData()
    errors = piint()
    _call(
        "Picam_Acquire",
        handle,
        pi64s(int(readout_count)),
        piint(int(timeout_ms)),
        byref(data),
        byref(errors),
    )
    return int(data.initial_readout or 0), int(data.readout_count), int(errors.value)


def stop_acquisition(handle: PicamHandle) -> None:
    _call("Picam_StopAcquisition", handle)


def is_acquisition_running(handle: PicamHandle) -> bool:
    running = pibln()
    _call("Picam_IsAcquisitionRunning", handle, byref(running))
    return bool(running.value)
