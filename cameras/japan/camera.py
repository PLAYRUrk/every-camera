"""Hamamatsu camera over DCAM-API.

Ported from ``japan-camera/camera.py`` with three changes, all of which the
every-camera worker depends on:

* **``capture()`` cannot hang.** The original looped on
  ``wait_capevent_frameready`` with ``while True:`` and no bound, so a camera that
  stopped producing frames took the worker thread with it — the stop request would
  never be served and systemd would eventually SIGKILL the process mid-exposure.
  Here the wait is bounded and a genuine error gives up at once, returning ``None``,
  which the worker already treats as one failed frame.
* **Applied values are read back.** ``prop_setgetvalue`` returns what the camera
  actually took; the original discarded it, so ``current_binning`` could claim 8
  while the sensor did 4 — and ``BINNING`` in the header would say so too.
* **Nothing prints.** The console dashboard owns stdout; messages go through
  ``console_ui``.

The SDK is imported inside :meth:`__enter__`, never at module scope: see
``cameras/japan/dcamsdk/__init__.py`` for why that matters.

There is no cooling control anywhere in this module. The camera reports
``SENSORTEMPERATURE`` and has no setpoint to command, which is the main way it
differs from the PIXIS in ``cameras/asi/camera.py``.
"""
from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

import numpy as np

import console_ui

from . import dcamsdk
from .config import readout_text

if TYPE_CHECKING:
    from .config import CameraCfg

# How long ``capture()`` keeps waiting for a frame that is late but not refused.
# The budget is the exposure plus this many frame-timeout windows: a camera that
# is merely slow gets several chances, and one that has stopped answering is given
# up on in bounded time so a stop request is still served.
MAX_TIMEOUT_WAITS = 5

# Slack over the exposure itself, covering readout and transfer.
CAPTURE_MARGIN_S = 5.0


class HamamatsuCamera:
    def __init__(self, cfg: CameraCfg) -> None:
        self._cfg = cfg
        self._sdk = None
        self._dcam = None
        self.current_exposure: float | None = None
        self.current_binning: int = cfg.binning
        self.current_readout_speed: int = cfg.readout_speed

    # --- lifetime -----------------------------------------------------------
    def __enter__(self) -> HamamatsuCamera:
        sdk = dcamsdk.load()
        if not sdk.Dcamapi.init():
            raise RuntimeError(f"Dcamapi.init() failed: {sdk.Dcamapi.lasterr()}")
        dcam = sdk.Dcam()
        if not dcam.dev_open():
            # init() succeeded, so it has to be undone before this propagates —
            # otherwise a retry in the same process meets an already-initialised
            # library.
            sdk.Dcamapi.uninit()
            raise RuntimeError(f"dev_open() failed: {dcam.lasterr()}")
        self._sdk = sdk
        self._dcam = dcam
        self.set_readout_speed(self._cfg.readout_speed)
        self.set_binning(self._cfg.binning)
        return self

    def __exit__(self, *args) -> None:
        if self._dcam is not None:
            self._dcam.dev_close()
            self._dcam = None
        if self._sdk is not None:
            self._sdk.Dcamapi.uninit()

    # --- configuration ------------------------------------------------------
    def _apply(self, prop, value, label, cast):
        """Set one DCAM property and return what the camera actually took.

        A refused write leaves the cached value alone rather than recording an
        intention: the header and the frame name are built from these, and a value
        the sensor never applied would be a lie in the archive.
        """
        applied = self._dcam.prop_setgetvalue(prop, float(value))
        if applied is False:
            console_ui.warn(f"Could not set {label} to {value}: "
                            f"{self._dcam.lasterr()}")
            return None
        taken = cast(applied)
        if taken != cast(value):
            console_ui.warn(f"{label}: asked for {cast(value)}, camera applied "
                            f"{taken}")
        return taken

    def set_exposure(self, sec: float) -> None:
        taken = self._apply(self._sdk.DCAM_IDPROP.EXPOSURETIME, sec,
                            "exposure (s)", float)
        if taken is not None:
            self.current_exposure = taken

    def set_binning(self, b: int) -> None:
        taken = self._apply(self._sdk.DCAM_IDPROP.BINNING, b, "binning", int)
        if taken is not None:
            self.current_binning = taken

    def set_readout_speed(self, speed: int) -> None:
        taken = self._apply(self._sdk.DCAM_IDPROP.READOUTSPEED, speed,
                            "readout speed", int)
        if taken is not None:
            self.current_readout_speed = taken

    # --- readings -----------------------------------------------------------
    def temperature(self) -> float | None:
        """Sensor temperature in °C, or ``None`` when DCAM will not report it."""
        value = self._dcam.prop_getvalue(self._sdk.DCAM_IDPROP.SENSORTEMPERATURE)
        return None if value is False else float(value)

    def capture(self) -> np.ndarray | None:
        """Take one frame. Returns ``None`` on any failure, having said why."""
        if not self._dcam.buf_alloc(1):
            console_ui.error(f"buf_alloc failed: {self._dcam.lasterr()}")
            return None
        image = None
        try:
            if not self._dcam.cap_snapshot():
                console_ui.error(f"cap_snapshot failed: {self._dcam.lasterr()}")
                return None
            timeout_ms = self._cfg.frame_timeout_ms
            budget = ((self.current_exposure or 0.0) + CAPTURE_MARGIN_S
                      + MAX_TIMEOUT_WAITS * timeout_ms / 1000.0)
            deadline = monotonic() + budget
            while True:
                if self._dcam.wait_capevent_frameready(timeout_ms):
                    image = self._dcam.buf_getlastframedata()
                    if image is False:
                        console_ui.error(f"buf_getlastframedata failed: "
                                         f"{self._dcam.lasterr()}")
                        image = None
                    break
                error = self._dcam.lasterr()
                if error != self._sdk.DCAMERR.TIMEOUT:
                    # Not "still exposing" — the camera is refusing. Waiting
                    # longer cannot help, and the slot is better spent on the
                    # next frame.
                    console_ui.error(f"Frame wait failed: {error}")
                    break
                if monotonic() >= deadline:
                    console_ui.error(
                        f"No frame within {budget:.0f} s of starting a "
                        f"{self.current_exposure or 0:g} s exposure — giving up "
                        f"on this frame")
                    break
        finally:
            self._dcam.buf_release()
        return image

    # --- identification -----------------------------------------------------
    def info(self) -> dict:
        d = self._dcam
        s = self._sdk.DCAM_IDSTR
        return {
            "vendor": d.dev_getstring(s.VENDOR),
            "model": d.dev_getstring(s.MODEL),
            "bus": d.dev_getstring(s.BUS),
            "dcam_version": d.dev_getstring(s.DCAMAPIVERSION),
            "serial": d.dev_getstring(s.CAMERAID),
            "camera_version": d.dev_getstring(s.CAMERAVERSION),
            "driver_version": d.dev_getstring(s.DRIVERVERSION),
            "module_version": d.dev_getstring(s.MODULEVERSION),
        }

    def info_rows(self) -> list:
        """``(label, value)`` rows describing the camera, for the console block.

        Replaces the original's ``print_info()``: the same eight facts, handed to
        the dashboard instead of to stdout.
        """
        info = self.info()
        return [
            ("Model:", f"{info['vendor']} {info['model']}"),
            ("Serial:", info["serial"]),
            ("Bus:", info["bus"]),
            ("DCAM API:", info["dcam_version"]),
            ("Camera firmware:", info["camera_version"]),
            ("Driver:", info["driver_version"]),
            ("Module:", info["module_version"]),
            ("Readout speed:", _readout_text(self.current_readout_speed)),
        ]
