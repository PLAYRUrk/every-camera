"""Synthetic camera with the same interface as :class:`camera.HamamatsuCamera`.

Selected with ``japan.camera.backend = "sim"``. It exists so the scheduler, the
filter sequencing, the dashboard and the FITS output can be exercised end to end
on a machine with no camera and no DCAM-API SDK installed. Exposures really do
take the requested time, so timing behaviour matches a real run.

The cooler can be switched, as ``SENSORCOOLER`` switches it on the real camera:
on, the sensor relaxes towards the target; off, towards the room. Either way it
wanders a little, which is what the dashboard and the CCD-TEMP header should be
shown.
"""
from __future__ import annotations

from time import monotonic, sleep
from typing import TYPE_CHECKING

import numpy as np

import console_ui

from .config import BINNING_MAX, BINNING_MIN, READOUT_SPEEDS, readout_text

if TYPE_CHECKING:
    from .config import CameraCfg

SENSOR_WIDTH = 2048
SENSOR_HEIGHT = 2048

# Where the sensor settles with the cooler on (unless a target is set) and off,
# and how fast it gets there. A camera starts with its cooler running, which is
# how the real one powers up.
SENSOR_TEMP_C = -10.0
AMBIENT_C = 20.0
COOLING_TAU_S = 60.0         # time constant of the approach either way
READY_WITHIN_C = 1.0         # "ready" once this close to the target
DRIFT_C = 0.4                # amplitude of the slow wander
DRIFT_PERIOD_S = 90.0        # one full swing


class SimCamera:
    def __init__(self, cfg: CameraCfg) -> None:
        self._cfg = cfg
        self._rng = np.random.default_rng(20260729)
        self._started = monotonic()
        self._sensor = (SENSOR_WIDTH, SENSOR_HEIGHT)

        self.current_exposure: float | None = None
        self.current_binning: int = cfg.binning
        self.current_readout_speed: int = cfg.readout_speed

        self.cooler_on = True
        self.target_temperature: float = SENSOR_TEMP_C
        self._temp = SENSOR_TEMP_C
        self._temp_at = monotonic()

    # --- lifetime -----------------------------------------------------------
    def __enter__(self) -> SimCamera:
        console_ui.log("Camera backend: SIMULATOR (no hardware in use)")
        return self

    def __exit__(self, *args) -> None:
        pass

    # --- configuration ------------------------------------------------------
    def set_exposure(self, sec: float) -> None:
        self.current_exposure = float(sec)

    def set_binning(self, b: int) -> None:
        value = int(b)
        if not BINNING_MIN <= value <= BINNING_MAX:
            clamped = min(max(value, BINNING_MIN), BINNING_MAX)
            console_ui.warn(f"Binning: this camera does not accept {value} "
                            f"(allowed: {BINNING_MIN}..{BINNING_MAX}) — "
                            f"using {clamped} instead")
            value = clamped
        self.current_binning = value

    def set_readout_speed(self, speed: int) -> None:
        value = int(speed)
        if value not in READOUT_SPEEDS:
            console_ui.warn(f"Readout speed: this camera does not accept {value} "
                            f"(allowed: 1 slow, 2 fast) — using 2 instead")
            value = 2
        self.current_readout_speed = value

    # --- cooling ------------------------------------------------------------
    def _settle(self) -> float:
        """Advance the sensor towards wherever the cooler is taking it."""
        now = monotonic()
        goal = self.target_temperature if self.cooler_on else AMBIENT_C
        self._temp = goal + (self._temp - goal) * float(
            np.exp(-(now - self._temp_at) / COOLING_TAU_S))
        self._temp_at = now
        return self._temp

    def cooler_supported(self) -> bool:
        return True

    def cooler_can_switch_off(self) -> bool:
        return True

    def set_cooler(self, on: bool) -> bool:
        self._settle()
        self.cooler_on = bool(on)
        return True

    def set_target_temperature(self, celsius: float) -> float:
        self._settle()
        self.target_temperature = float(celsius)
        return self.target_temperature

    def cooler_status(self) -> str:
        if not self.cooler_on:
            return "off"
        near = abs(self._settle() - self.target_temperature) <= READY_WITHIN_C
        return "ready" if near else "busy"

    # --- readings -----------------------------------------------------------
    def temperature(self) -> float | None:
        phase = (monotonic() - self._started) / DRIFT_PERIOD_S
        drift = DRIFT_C * np.sin(2.0 * np.pi * phase)
        return self._settle() + float(drift) + float(self._rng.normal(0.0, 0.02))

    def capture(self) -> np.ndarray | None:
        sleep(self.current_exposure or 0.0)
        w = self._sensor[0] // self.current_binning
        h = self._sensor[1] // self.current_binning
        # A soft all-sky-like disc plus read noise, scaled by exposure. The fast
        # readout is the noisier of the two, as it is on the real sensor.
        yy, xx = np.mgrid[0:h, 0:w]
        r = np.hypot(yy - h / 2, xx - w / 2) / (min(w, h) / 2)
        signal = 3000.0 * np.exp(-2.0 * r**2) * (self.current_exposure or 1.0) / 30.0
        read_noise = 25.0 if self.current_readout_speed == 2 else 8.0
        frame = signal + 600.0 + self._rng.normal(0.0, read_noise, (h, w))
        return np.clip(frame, 0, 65535).astype("<u2")

    # --- identification -----------------------------------------------------
    def info(self) -> dict:
        """The same keys the real ``info()`` returns, so nothing branches on backend."""
        return {
            "vendor": "Hamamatsu Photonics (simulated)",
            "model": "C11440-22CU (simulated)",
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
        return [
            ("Model:", f"{info['vendor']} {info['model']}"),
            ("Serial:", info["serial"]),
            ("Sensor:", f"{info['module_version']}  ({width}x{height})"),
            ("DCAM API:", info["dcam_version"]),
            ("Readout speed:", readout_text(self.current_readout_speed)),
        ]
