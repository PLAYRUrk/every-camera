"""Synthetic camera with the same interface as :class:`camera.PixisCamera`.

Selected with ``[camera] backend = sim``. It exists so the scheduler, the filter
sequencing, the dashboard and the FITS output can be exercised end to end on a
machine with no camera and no PICAM SDK installed. Exposures really do take the
requested time, so timing behaviour matches a real run.
"""
from __future__ import annotations

from time import monotonic, sleep
from typing import TYPE_CHECKING

import numpy as np

import console_ui

from .picam import FloatRange, clamp_to_constraint, describe_constraint

if TYPE_CHECKING:
    from .config import CameraCfg, CoolingCfg

SENSOR_WIDTH = 2048
SENSOR_HEIGHT = 2048
AMBIENT_C = 22.0
COOLING_RATE_C_PER_S = 4.0  # fast enough to keep dry runs short
# A plausible PIXIS cooling range, so dry runs exercise the same clamping and
# warnings a real camera would produce for an impossible setpoint.
SETPOINT_LIMITS = FloatRange(minimum=-70.0, maximum=25.0, increment=0.1)


class SimCamera:
    def __init__(self, cfg: CameraCfg, cooling: CoolingCfg | None = None) -> None:
        self._cfg = cfg
        self._cooling = cooling
        self._rng = np.random.default_rng(20260726)
        self._temp = AMBIENT_C
        self._temp_updated = monotonic()
        self._setpoint = cooling.target_temp if cooling and cooling.enabled else AMBIENT_C
        self._sensor = (SENSOR_WIDTH, SENSOR_HEIGHT)

        self.current_exposure: float | None = None
        self.current_binning: int = cfg.binning
        self.current_readout_speed: float = cfg.readout_speed
        self.current_gain: int = cfg.gain

    # --- lifetime -----------------------------------------------------------
    def __enter__(self) -> SimCamera:
        console_ui.log("Camera backend: SIMULATOR (no hardware in use)")
        if self._cooling and self._cooling.enabled:
            self.set_target_temperature(self._cooling.target_temp)
        if self._cooling and self._cooling.enabled and self._cooling.wait_on_start:
            self.wait_for_temperature(self._cooling.wait_timeout)
        return self

    def __exit__(self, *args) -> None:
        if self._cooling and self._cooling.enabled and self._cooling.warm_on_exit:
            self.warm_up()

    # --- configuration ------------------------------------------------------
    def set_exposure(self, sec: float) -> None:
        self.current_exposure = sec

    def set_binning(self, b: int) -> None:
        self.current_binning = b

    def set_gain(self, gain: int) -> None:
        self.current_gain = gain

    # --- cooling ------------------------------------------------------------
    @property
    def target_temperature(self) -> float | None:
        if not (self._cooling and self._cooling.enabled):
            return None
        return self._setpoint

    def temperature_limits(self) -> FloatRange:
        return SETPOINT_LIMITS

    def set_target_temperature(self, celsius: float) -> float:
        self._advance_temperature()
        effective, changed = clamp_to_constraint(SETPOINT_LIMITS, celsius)
        if changed:
            console_ui.warn(
                f"Sensor setpoint (°C): this camera does not accept {float(celsius):g} "
                f"(allowed: {describe_constraint(SETPOINT_LIMITS)}) — "
                f"using {effective:g} instead"
            )
        self._setpoint = effective
        if self._cooling:
            self._cooling.target_temp = effective
        return effective

    def _advance_temperature(self) -> None:
        now = monotonic()
        step = (now - self._temp_updated) * COOLING_RATE_C_PER_S
        self._temp_updated = now
        if self._temp < self._setpoint:
            self._temp = min(self._setpoint, self._temp + step)
        else:
            self._temp = max(self._setpoint, self._temp - step)

    def temperature_locked(self) -> bool:
        self._advance_temperature()
        return abs(self._temp - self._setpoint) < 0.2

    def _wait_temperature(self, target: float, timeout: float, label: str) -> bool:
        tolerance = self._cooling.tolerance if self._cooling else 3.0
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            temp = self.temperature()
            if self.temperature_locked() or abs(temp - target) <= tolerance:
                console_ui.log(f"{label}: {temp:.2f} C (target {target:.2f} C) — reached.")
                return True
            sleep(0.5)
        console_ui.warn(f"{label}: timed out after {timeout:.0f} s, continuing anyway.")
        return False

    def wait_for_temperature(self, timeout: float | None = None) -> bool:
        if not (self._cooling and self._cooling.enabled):
            return True
        timeout = self._cooling.wait_timeout if timeout is None else timeout
        return self._wait_temperature(self._setpoint, timeout, "Cooling")

    def warm_up(self) -> bool:
        if not (self._cooling and self._cooling.enabled):
            return True
        console_ui.log(f"Warming sensor to {self._cooling.warm_temp:.1f} C before shutdown...")
        warm_temp = self.set_target_temperature(self._cooling.warm_temp)
        return self._wait_temperature(
            warm_temp, self._cooling.warm_timeout, "Warm-up"
        )

    # --- readings -----------------------------------------------------------
    def temperature(self) -> float | None:
        self._advance_temperature()
        return self._temp + float(self._rng.normal(0.0, 0.05))

    def capture(self) -> np.ndarray | None:
        sleep(self.current_exposure or 0.0)
        w = self._sensor[0] // self.current_binning
        h = self._sensor[1] // self.current_binning
        # A soft all-sky-like disc plus read noise, scaled by exposure and gain.
        yy, xx = np.mgrid[0:h, 0:w]
        r = np.hypot(yy - h / 2, xx - w / 2) / (min(w, h) / 2)
        signal = 3000.0 * np.exp(-2.0 * r**2) * (self.current_exposure or 1.0) / 30.0
        frame = signal * self.current_gain + 600.0 + self._rng.normal(0.0, 25.0, (h, w))
        return np.clip(frame, 0, 65535).astype("<u2")

    # --- identification -----------------------------------------------------
    def info(self) -> dict:
        return {
            "vendor": "Princeton Instruments (simulated)",
            "model": "Pixis2048B (simulated)",
            "bus": "none",
            "dcam_version": "sim",
            "serial": "SIMULATOR",
            "camera_version": "sim",
            "driver_version": "sim",
            "module_version": "simulated sensor",
        }

    def info_rows(self) -> list:
        """``(label, value)`` rows describing the camera, for the console block."""
        info = self.info()
        width, height = self._sensor
        rows = [
            ("Model:", f"{info['vendor']} {info['model']}"),
            ("Serial:", info["serial"]),
            ("Sensor:", f"{info['module_version']}  ({width}x{height})"),
            ("ADC speed:", f"{self.current_readout_speed} MHz"),
        ]
        if self.target_temperature is not None:
            rows.append(("Temp setpoint:", f"{self.target_temperature:.1f} °C"))
            rows.append(("Setpoint range:", describe_constraint(SETPOINT_LIMITS)))
        return rows
