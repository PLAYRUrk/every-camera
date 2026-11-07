"""Hamamatsu camera over DCAM-API.

``capture()`` cannot hang: a camera that stops producing frames must not take the
worker thread with it, so the wait is bounded and ends in ``None``, one failed frame.
Applied values are read back, so ``current_binning`` and ``BINNING`` say what the
sensor did. Nothing prints: the console dashboard owns stdout.

The SDK is imported inside :meth:`__enter__`, never at module scope.

Cooling is a switch (``SENSORCOOLER``) plus, on some models, a target
(``SENSORTEMPERATURETARGET``). Not every model exposes either, so
:meth:`HamamatsuCamera.cooler_supported` asks first; the reading ``SENSORTEMPERATURE``
is always there.
"""
from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

import numpy as np

import console_ui
import intensity

from . import dcamsdk
from .config import readout_text

if TYPE_CHECKING:
    from .config import CameraCfg

# The budget is the exposure plus this many frame-timeout windows: a slow camera gets
# several chances, and one that stopped answering is given up on, so a stop request is
# still served.
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

    def __enter__(self) -> HamamatsuCamera:
        sdk = dcamsdk.load()
        if not sdk.Dcamapi.init():
            raise RuntimeError(f"Dcamapi.init() failed: {sdk.Dcamapi.lasterr()}")
        dcam = sdk.Dcam()
        if not dcam.dev_open():
            # init() succeeded, so undo it: a retry in this process would meet an
            # initialised library.
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

    def _apply(self, prop, value, label, cast):
        """Set one DCAM property and return what the camera took.

        A refused write leaves the cached value alone: the header and the frame name
        are built from these.
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

    def _writable(self, prop) -> bool:
        attr = self._dcam.prop_getattr(prop)
        return attr is not False and attr.is_writable()

    def cooler_supported(self) -> bool:
        return self._writable(self._sdk.DCAM_IDPROP.SENSORCOOLER)

    def cooler_can_switch_off(self) -> bool:
        attr = self._dcam.prop_getattr(self._sdk.DCAM_IDPROP.SENSORCOOLER)
        if attr is False:
            return False
        return attr.valuemin <= self._sdk.DCAMPROP.SENSORCOOLER.OFF

    def set_cooler(self, on: bool) -> bool:
        """Switch the cooler; True once the camera has taken the new mode.

        A plain set comes first: some models answer the set-and-read-back of
        :meth:`_apply` with INVALIDPARAM for SENSORCOOLER. The set-get is still tried
        after it, for a model that only takes that one.
        """
        prop = self._sdk.DCAM_IDPROP.SENSORCOOLER
        mode = self._sdk.DCAMPROP.SENSORCOOLER
        value = mode.ON if on else mode.OFF
        word = "on" if on else "off"
        if not self._dcam.prop_setvalue(prop, float(value)):
            first = self._dcam.lasterr()
            if self._dcam.prop_setgetvalue(prop, float(value)) is False:
                console_ui.warn(f"Could not switch the sensor cooler {word}: "
                                f"{self._error_name(first)}"
                                f"{self._accepted_text(prop)}")
                return False
        now = self._dcam.prop_getvalue(prop)
        if now is not False and int(now) != int(value):
            console_ui.warn(f"Sensor cooler: asked for {word} ({int(value)}), "
                            f"camera reports {int(now)}")
            return False
        return True

    def _error_name(self, err) -> str:
        try:
            return f"{self._sdk.DCAMERR(err).name} ({int(err)})"
        except (ValueError, TypeError):
            return str(err)

    def _accepted_text(self, prop) -> str:
        attr = self._dcam.prop_getattr(prop)
        if attr is False:
            return ""
        return f" (camera accepts {attr.valuemin:g}..{attr.valuemax:g})"

    def set_target_temperature(self, celsius: float) -> float | None:
        """Move the cooling target; None when this model has no target to move."""
        prop = self._sdk.DCAM_IDPROP.SENSORTEMPERATURETARGET
        if not self._writable(prop):
            return None
        return self._apply(prop, celsius, "sensor temperature target (°C)", float)

    @property
    def target_temperature(self) -> float | None:
        value = self._dcam.prop_getvalue(
            self._sdk.DCAM_IDPROP.SENSORTEMPERATURETARGET)
        return None if value is False else float(value)

    def cooler_status(self) -> str | None:
        """``off``, ``ready``, ``busy``… as DCAM words it, or None if it will not."""
        value = self._dcam.prop_getvalue(self._sdk.DCAM_IDPROP.SENSORCOOLERSTATUS)
        if value is False:
            return None
        try:
            return self._sdk.DCAMPROP.SENSORCOOLERSTATUS(int(value)).name.lower()
        except ValueError:
            return str(int(value))

    def temperature(self) -> float | None:
        value = self._dcam.prop_getvalue(self._sdk.DCAM_IDPROP.SENSORTEMPERATURE)
        return None if value is False else float(value)

    def capture(self) -> np.ndarray | None:
        """Returns ``None`` on any failure, having said why."""
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
                    # The camera is refusing, not still exposing: waiting longer
                    # cannot help.
                    console_ui.error(f"Frame wait failed: {error}")
                    break
                if monotonic() >= deadline:
                    console_ui.error(
                        f"No frame within {budget:.0f} s of starting a "
                        f"{self.current_exposure or 0:g} s exposure — giving up "
                        f"on this frame")
                    break
        finally:
            if image is None:
                # A snapshot still running keeps the buffer busy, and every later
                # buf_alloc would then fail.
                stop = getattr(self._dcam, "cap_stop", None)
                if stop is not None:
                    try:
                        stop()
                    except Exception:
                        pass
            if not self._dcam.buf_release():
                console_ui.warn(f"buf_release failed: {self._dcam.lasterr()}")
        if image is None:
            return None
        # MONO16 passes through untouched; a MONO8 buffer is widened and shifted, so
        # the rest of the program never asks which pixel type produced a frame.
        return intensity.to_full_scale(image, 8 if image.dtype == np.uint8 else 16)

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
        info = self.info()
        return [
            ("Model:", f"{info['vendor']} {info['model']}"),
            ("Serial:", info["serial"]),
            ("Bus:", info["bus"]),
            ("DCAM API:", info["dcam_version"]),
            ("Camera firmware:", info["camera_version"]),
            ("Driver:", info["driver_version"]),
            ("Module:", info["module_version"]),
            ("Readout speed:", readout_text(self.current_readout_speed)),
        ]
