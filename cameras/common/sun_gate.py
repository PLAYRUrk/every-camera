"""Night window for cameras that otherwise shoot around the clock (SPTT).

The process does not end at dawn: it waits for the next night, so a service with no
restarts measures every night. A failed solar calculation opens the window: extra
daytime frames cost disk, a closed window costs the night.
"""
import time

from types import SimpleNamespace

import console_ui

from .schedule import sun_crossing_time
from .sun import angle_fn

MODES = ("always", "sun")
CHECK_SECONDS = 30.0


class SunSettingsError(ValueError):
    pass


def gate_from_config(section):
    """None for ``mode: "always"``; raises :class:`SunSettingsError` on a sun mode that
    would compute a window for the wrong place.
    """
    section = section or {}
    mode = str(section.get("mode") or "always").strip().lower()
    if mode not in MODES:
        raise SunSettingsError(f"mode must be one of {', '.join(MODES)}, not {mode!r}")
    if mode == "always":
        return None
    loc = section.get("location") or {}
    try:
        location = SimpleNamespace(lat=float(loc.get("lat") or 0.0),
                                   lon=float(loc.get("lon") or 0.0),
                                   elevation=float(loc.get("elevation") or 0.0))
        max_angle = float(section.get("sun_max_angle", -10.0))
    except (TypeError, ValueError) as exc:
        raise SunSettingsError(f"bad sun settings: {exc}") from exc
    if location.lat == 0.0 and location.lon == 0.0:
        raise SunSettingsError("mode \"sun\" needs location.lat and location.lon")
    return SunGate(angle_fn(location), max_angle)


class SunGate:
    def __init__(self, angle, max_angle, check_seconds=CHECK_SECONDS, clock=time.monotonic):
        self._angle_fn = angle
        self.max_angle = float(max_angle)
        self.check_seconds = check_seconds
        self._clock = clock
        self._checked_at = None
        self._open = True
        self._failing = False
        self._reported = False
        self.angle = None
        self.opens_at = None

    def is_open(self, now, refresh=True):
        """``refresh=False`` returns the cached answer: astropy is not called on a
        capture second.
        """
        stamp = self._clock()
        if self._checked_at is not None and (
                not refresh or stamp - self._checked_at < self.check_seconds):
            return self._open
        self._checked_at = stamp
        try:
            self.angle = float(self._angle_fn(now))
        except Exception as exc:
            if not self._failing:
                console_ui.warn(f"Sun altitude unavailable ({exc}) — capturing as if "
                                f"it were night")
            self._failing = True
            self.angle = None
            self._set(True, now)
            return True
        self._failing = False
        self._set(self.angle <= self.max_angle, now)
        return self._open

    def _set(self, is_open, now):
        was_open, first = self._open, not self._reported
        self._open = is_open
        self._reported = True
        if is_open:
            self.opens_at = None
            if first or not was_open:
                console_ui.log(f"Sun at {self._shown()} — captures run")
            return
        if first or was_open:
            try:
                self.opens_at = sun_crossing_time(self._angle_fn, self.max_angle, now)
            except Exception:
                self.opens_at = None
            console_ui.log(f"Sun at {self._shown()} — captures wait for the night"
                           f"{self._when()}")

    def _shown(self):
        angle = "?" if self.angle is None else f"{self.angle:+.1f}°"
        return f"{angle} (threshold {self.max_angle:g}°)"

    def _when(self):
        return f", ~{self.opens_at.strftime('%H:%M')}" if self.opens_at else ""

    def describe(self):
        if self._open:
            return f"sun {self._shown()}, capturing"
        return f"sun {self._shown()}, waiting{self._when()}"
