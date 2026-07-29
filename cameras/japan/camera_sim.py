"""Synthetic camera with the same interface as :class:`camera.HamamatsuCamera`.

Selected with ``japan.camera.backend = "sim"``. It exists so the scheduler, the
filter sequencing, the dashboard and the FITS output can be exercised end to end
on a machine with no camera and no DCAM-API SDK installed. Exposures really do
take the requested time, so timing behaviour matches a real run.

The sensor temperature drifts on its own and cannot be commanded, exactly as on
the real camera: DCAM reports ``SENSORTEMPERATURE`` and this instrument offers no
setpoint to control it.
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

# A plausible reading for a Hamamatsu sCMOS with its own cooling running: settled
# a little below zero and wandering slowly, which is what the dashboard and the
# CCD-TEMP header should be shown.
SENSOR_TEMP_C = -10.0
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

    # --- readings -----------------------------------------------------------
    def temperature(self) -> float | None:
        phase = (monotonic() - self._started) / DRIFT_PERIOD_S
        drift = DRIFT_C * np.sin(2.0 * np.pi * phase)
        return SENSOR_TEMP_C + float(drift) + float(self._rng.normal(0.0, 0.02))

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
