"""Princeton Instruments PIXIS camera, driven through the PICAM SDK.

The public surface mirrors the Hamamatsu class of the reference program
(``sample/japan-camera/camera.py``) so that ``runner.py`` and ``fits_writer.py``
are camera-agnostic: context manager, ``current_exposure`` / ``current_binning``
/ ``current_readout_speed``, ``set_exposure``, ``set_binning``, ``temperature``,
``capture``, ``info``, ``print_info``.

The configuration order follows the legacy C implementation that has been
running this instrument for years — ``imagerd_rt/src/imagerd_rt/src/pixis.c``,
functions ``Configure_Static()`` and ``Configure_Dynamic()``.
"""
from __future__ import annotations

import ctypes
from time import monotonic, sleep
from typing import TYPE_CHECKING

import numpy as np

import console_ui

from . import picam
from .picam import PicamParameter as P

if TYPE_CHECKING:
    from .config import CameraCfg, CoolingCfg


class PixisCamera:
    def __init__(self, cfg: CameraCfg, cooling: CoolingCfg | None = None) -> None:
        self._cfg = cfg
        self._cooling = cooling
        self._handle: picam.PicamHandle | None = None
        self._demo_id: picam.PicamCameraID | None = None
        self._id: picam.PicamCameraID | None = None
        self._sensor: tuple[int, int] = (0, 0)  # full sensor (width, height)
        self._setpoint_limits = None  # cooling setpoints this camera accepts
        self._bit_depth: int | None = None  # known once the camera is configured

        self.current_exposure: float | None = None
        self.current_binning: int = cfg.binning
        self.current_readout_speed: float = cfg.readout_speed
        self.current_gain: int = cfg.gain

    # --- lifetime -----------------------------------------------------------
    def __enter__(self) -> PixisCamera:
        picam.initialize_library()
        try:
            self._open()
            self._configure_static()
            if self._cooling and self._cooling.enabled and self._cooling.wait_on_start:
                self.wait_for_temperature(self._cooling.wait_timeout)
        except Exception:
            self._shutdown()
            raise
        return self

    def __exit__(self, *args) -> None:
        try:
            if (
                self._handle is not None
                and self._cooling
                and self._cooling.enabled
                and self._cooling.warm_on_exit
            ):
                self.warm_up()
        finally:
            self._shutdown()

    def _open(self) -> None:
        if self._cfg.demo:
            model = picam.DEMO_MODELS.get(self._cfg.demo_model)
            if model is None:
                raise ValueError(
                    f"Unknown demo_model {self._cfg.demo_model!r}; "
                    f"known models: {', '.join(sorted(picam.DEMO_MODELS))}"
                )
            self._demo_id = picam.connect_demo_camera(model, "0008675309")
            self._handle = picam.open_camera(self._demo_id)
            console_ui.log(f"PICAM demo camera connected: {self._cfg.demo_model}")
        else:
            # Unlike the legacy code, a missing camera is an error rather than a
            # silent fall-back to a demo camera: set demo = true to ask for one.
            self._handle = picam.open_first_camera()
        self._id = picam.get_camera_id(self._handle)
        self._sensor = picam.sensor_size(self._handle)
        self._setpoint_limits = picam.constraint(
            self._handle, P.SensorTemperatureSetPoint
        )

    def _shutdown(self) -> None:
        if self._handle is not None:
            try:
                picam.close_camera(self._handle)
            finally:
                self._handle = None
        if self._demo_id is not None:
            try:
                picam.disconnect_demo_camera(self._demo_id)
            finally:
                self._demo_id = None
        picam.uninitialize_library()

    # --- configuration ------------------------------------------------------
    def _commit(self) -> None:
        failed = picam.commit(self._handle)
        if failed:
            names = ", ".join(
                picam.enum_string(picam.PicamEnumeratedType.Parameter, p) for p in failed
            )
            raise RuntimeError(f"PICAM rejected these parameters: {names}")

    def _set_checked(self, parameter, value, label: str, *, clamp: bool = True):
        """Write one parameter after asking the camera what it accepts.

        Every model has its own limits — a setpoint one PIXIS cools to is out of
        range on the next — and PICAM answers a bad value with a bare "Invalid
        Parameter Value" naming neither the parameter nor what would have
        worked. So the constraint is read first. A measured quantity outside it
        (setpoint, ADC speed) is nudged to the nearest value the camera allows,
        with a warning: losing the night over one number in config.json is worse
        than cooling three degrees less deeply. A mode is not nudged — the
        numerically nearest shutter mode is a *different* mode, and quietly
        swapping one for another would spoil the data far more thoroughly than
        stopping does. Returns the value actually written.
        """
        limits = picam.constraint(self._handle, parameter)
        kind = picam.value_type(parameter)
        is_mode = kind in (picam.PicamValueType.Enumeration,
                           picam.PicamValueType.Boolean)
        effective, changed = picam.clamp_to_constraint(limits, float(value))
        if changed:
            if is_mode or not clamp:
                raise RuntimeError(self._rejected(parameter, label, value, limits))
            console_ui.warn(
                f"{label}: this camera does not accept {float(value):g} "
                f"(allowed: {picam.describe_constraint(limits)}) — "
                f"using {effective:g} instead"
            )
        if kind is not picam.PicamValueType.FloatingPoint:
            effective = int(round(effective))
        try:
            if kind is picam.PicamValueType.FloatingPoint:
                picam.set_float(self._handle, parameter, effective)
            else:
                picam.set_int(self._handle, parameter, effective)
        except picam.PicamError as exc:
            raise RuntimeError(
                self._rejected(parameter, label, effective, limits, exc)
            ) from exc
        return effective

    @staticmethod
    def _rejected(parameter, label: str, value, limits, exc=None) -> str:
        """Why a value did not go in, in terms of what to put in config.json."""
        name = picam.enum_string(picam.PicamEnumeratedType.Parameter, int(parameter))
        text = (f"{label}: this camera does not accept {float(value):g} — allowed: "
                f"{picam.describe_constraint(limits)} ({name})")
        return f"{text} — {exc}" if exc is not None else text

    def _configure_static(self) -> None:
        """Parameters that stay put for the whole session (pixis.c:Configure_Static)."""
        cfg = self._cfg
        if self._cooling and self._cooling.enabled:
            self._cooling.target_temp = self._set_checked(
                P.SensorTemperatureSetPoint, self._cooling.target_temp,
                "Sensor setpoint (°C)")
        self.current_readout_speed = self._set_checked(
            P.AdcSpeed, cfg.readout_speed, "ADC speed (MHz)")
        readout_mode = self._set_checked(
            P.ReadoutControlMode, cfg.readout_control_mode, "Readout control mode")
        self._set_checked(P.OutputSignal, cfg.output_signal, "Output signal")
        self._set_checked(
            P.ShutterTimingMode, cfg.shutter_timing_mode, "Shutter timing mode")
        if readout_mode == 3:  # Kinetics
            self._set_checked(
                P.KineticsWindowHeight, cfg.kinetics_window_height,
                "Kinetics window height")
        self.current_gain = self._set_checked(P.AdcAnalogGain, cfg.gain, "Analog gain")
        self._set_full_frame_roi(cfg.binning)
        self._commit()

        # Kept: the legacy ``BitDepth`` header records it on every frame.
        self._bit_depth = bit_depth = picam.get_int(self._handle, P.PixelBitDepth)
        if bit_depth > 16:
            raise RuntimeError(
                f"This camera reports {bit_depth}-bit pixels; the FITS writer here "
                "assumes 16-bit readouts."
            )

    def _set_full_frame_roi(self, binning: int) -> None:
        """Full-sensor ROI with symmetric binning."""
        width, height = self._sensor
        rois = picam.get_rois(self._handle)
        try:
            if rois.contents.roi_count != 1:
                raise RuntimeError(
                    f"Expected exactly one region of interest, got {rois.contents.roi_count}"
                )
            roi = rois.contents.roi_array[0]
            roi.x, roi.y = 0, 0
            roi.width, roi.height = width, height
            roi.x_binning = roi.y_binning = binning
            picam.set_rois(self._handle, rois)
        finally:
            picam.destroy_rois(rois)
        self.current_binning = binning

    def _frame_shape(self) -> tuple[int, int]:
        """(height, width) of the readout as PICAM currently has it configured.

        Read back rather than derived from sensor/binning: PICAM may clamp a
        requested ROI or binning to what the hardware actually supports.
        """
        rois = picam.get_rois(self._handle)
        try:
            roi = rois.contents.roi_array[0]
            return roi.height // roi.y_binning, roi.width // roi.x_binning
        finally:
            picam.destroy_rois(rois)

    def set_exposure(self, sec: float) -> None:
        # PICAM takes the exposure in milliseconds; the schedule speaks seconds.
        # Never clamped: a silently shortened exposure would quietly corrupt the
        # measurement, so an unsupported one has to be an error.
        self._set_checked(
            P.ExposureTime, sec * 1000.0, "Exposure (ms)", clamp=False)
        self._commit()
        self.current_exposure = sec

    def set_binning(self, b: int) -> None:
        self._set_full_frame_roi(b)
        self._commit()

    def set_gain(self, gain: int) -> None:
        self.current_gain = self._set_checked(P.AdcAnalogGain, gain, "Analog gain")
        self._commit()

    # --- cooling ------------------------------------------------------------
    @property
    def target_temperature(self) -> float | None:
        if not (self._cooling and self._cooling.enabled):
            return None
        return self._cooling.target_temp

    def temperature_limits(self) -> picam.FloatRange | list[float] | None:
        """Setpoints this camera accepts, as read from it (None if unknown)."""
        return self._setpoint_limits

    def set_target_temperature(self, celsius: float) -> float:
        """Move the cooling setpoint; returns the value the camera took."""
        effective = self._set_checked(
            P.SensorTemperatureSetPoint, celsius, "Sensor setpoint (°C)")
        self._commit()
        if self._cooling:
            self._cooling.target_temp = effective
        return effective

    def temperature_locked(self) -> bool:
        """True when the sensor reports its temperature is locked to the setpoint."""
        try:
            status = picam.get_int(self._handle, P.SensorTemperatureStatus)
        except picam.PicamError:
            return False
        return status == int(picam.PicamSensorTemperatureStatus.Locked)

    def _wait_temperature(self, target: float, timeout: float, label: str) -> bool:
        """Poll until the sensor locks on ``target`` (or lands within tolerance)."""
        tolerance = self._cooling.tolerance if self._cooling else 3.0
        deadline = monotonic() + timeout
        last_report = 0.0
        while monotonic() < deadline:
            temp = self.temperature()
            if temp is not None:
                if self.temperature_locked() or abs(temp - target) <= tolerance:
                    console_ui.log(f"{label}: {temp:.2f} C (target {target:.2f} C) — reached.")
                    return True
                if monotonic() - last_report >= 15.0:
                    console_ui.log(f"{label}: {temp:.2f} C -> {target:.2f} C ...")
                    last_report = monotonic()
            sleep(2.0)
        console_ui.warn(f"{label}: timed out after {timeout:.0f} s, continuing anyway.")
        return False

    def wait_for_temperature(self, timeout: float | None = None) -> bool:
        if not (self._cooling and self._cooling.enabled):
            return True
        timeout = self._cooling.wait_timeout if timeout is None else timeout
        return self._wait_temperature(self._cooling.target_temp, timeout, "Cooling")

    def warm_up(self) -> bool:
        """Raise the setpoint to a safe value and wait before powering down."""
        if not (self._cooling and self._cooling.enabled):
            return True
        console_ui.log(f"Warming sensor to {self._cooling.warm_temp:.1f} C before shutdown...")
        warm_temp = self.set_target_temperature(self._cooling.warm_temp)
        return self._wait_temperature(
            warm_temp, self._cooling.warm_timeout, "Warm-up"
        )

    # --- readings -----------------------------------------------------------
    def temperature(self) -> float | None:
        try:
            return picam.read_float(self._handle, P.SensorTemperatureReading)
        except picam.PicamError:
            return None

    def capture(self) -> np.ndarray | None:
        exposure_ms = (self.current_exposure or 0.0) * 1000.0
        timeout_ms = int(exposure_ms) + self._cfg.frame_timeout_ms
        try:
            frame_size = picam.get_int(self._handle, P.FrameSize)
            h, w = self._frame_shape()
            address, readouts, errors = picam.acquire(self._handle, 1, timeout_ms)
        except picam.PicamError as exc:
            console_ui.error(f"capture failed: {exc}")
            return None
        if errors:
            mask = picam.enum_string(
                picam.PicamEnumeratedType.AcquisitionErrorsMask, errors
            )
            console_ui.error(f"acquisition reported: {mask}")
            return None
        if readouts < 1 or not address:
            console_ui.error("no image data returned")
            return None

        expected = w * h * 2  # 16 bits per pixel
        if frame_size < expected:
            console_ui.error(
                f"frame size {frame_size} B is smaller than the expected "
                f"{expected} B for {w}x{h}"
            )
            return None
        # The PICAM buffer is reused by the next acquisition — copy it out now.
        buf = (ctypes.c_ubyte * expected).from_address(address)
        return np.frombuffer(bytes(buf), dtype="<u2").reshape(h, w).copy()

    # --- identification -----------------------------------------------------
    def info(self) -> dict:
        """Same dictionary keys as the reference program, filled from PICAM."""
        cid = self._id
        model = picam.enum_string(picam.PicamEnumeratedType.Model, cid.model)
        bus = picam.enum_string(
            picam.PicamEnumeratedType.ComputerInterface, cid.computer_interface
        )
        try:
            firmware = "; ".join(f"{n}={d}" for n, d in picam.firmware_details(cid))
        except picam.PicamError:
            firmware = "n/a"
        version = picam.library_version()
        return {
            "vendor": "Princeton Instruments",
            "model": model,
            "bus": bus,
            "dcam_version": version,     # PICAM library version
            "serial": cid.serial_number.decode(errors="replace"),
            "camera_version": firmware or "n/a",
            "driver_version": version,
            "module_version": cid.sensor_name.decode(errors="replace"),
            "bit_depth": self._bit_depth,
        }

    def info_rows(self) -> list:
        """``(label, value)`` rows describing the camera, for the console block."""
        info = self.info()
        width, height = self._sensor
        rows = [
            ("Model:", f"{info['vendor']} {info['model']}"),
            ("Bus / PICAM:", f"{info['bus']}  ·  {info['dcam_version']}"),
            ("Serial:", info["serial"]),
            ("Sensor:", f"{info['module_version']}  ({width}x{height})"),
            ("Firmware:", info["camera_version"]),
            ("ADC speed:", f"{self.current_readout_speed} MHz"),
        ]
        if self.target_temperature is not None:
            rows.append(("Temp setpoint:", f"{self.target_temperature:.1f} °C"))
        if self._setpoint_limits is not None:
            rows.append(("Setpoint range:",
                         picam.describe_constraint(self._setpoint_limits)))
        return rows
