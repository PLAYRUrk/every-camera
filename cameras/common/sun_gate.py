"""Night window for cameras that otherwise shoot around the clock (SPTT).

The process does not end at dawn: it waits for the next night, so a service with no
restarts measures every night. A failed solar calculation opens the window: extra
daytime frames cost disk, a closed window costs the night.

With ``start_angle`` the window opens earlier, in a ``preflight`` stage that lasts until
the sun reaches ``max_angle``. The stage is evening-only, as on the ASI imager: the
morning ends the night at ``max_angle``.
"""
import time

from datetime import timedelta
from types import SimpleNamespace

import console_ui

from .schedule import sun_crossing_time
from .sun import angle_fn

MODES = ("always", "sun")
CHECK_SECONDS = 30.0


class SunSettingsError(ValueError):
    pass


# How far ahead the altitude is probed to tell a setting sun from a rising one.
TREND_PROBE = timedelta(minutes=5)


def gate_from_config(section, start_angle=None):
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
    return SunGate(angle_fn(location), max_angle, start_angle=start_angle)


class SunGate:
    def __init__(self, angle, max_angle, check_seconds=CHECK_SECONDS, clock=time.monotonic,
                 start_angle=None):
        self._angle_fn = angle
        self.max_angle = float(max_angle)
        self.start_angle = None if start_angle is None else float(start_angle)
        self.check_seconds = check_seconds
        self._clock = clock
        self._checked_at = None
        self._open = True
        self._failing = False
        self._reported = False
        # Set when tonight's main stage began, cleared once the sun is above
        # start_angle: a morning between the two angles is not a second preflight.
        self._night_done = False
        self.angle = None
        self.opens_at = None
        self.stage = "main"
        # Counts the openings, so a worker can tell a new night from the same one.
        self.night = 0

    @property
    def open_angle(self):
        return self.max_angle if self.start_angle is None else self.start_angle

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
            # The stage stays: one failed calculation must not end tonight's preflight.
            self._set(True, now, self.stage)
            return True
        self._failing = False
        is_open, stage = self._decide(now)
        self._set(is_open, now, stage)
        return self._open

    def _decide(self, now):
        if self.angle <= self.max_angle:
            self._night_done = True
            return True, "main"
        if self.start_angle is None:
            return False, "main"
        if self.angle > self.start_angle:
            self._night_done = False
            return False, "main"
        if self._night_done:
            return False, "main"
        if self.stage == "preflight" and self._open:
            return True, "preflight"
        if self._rising(now):
            # Started in the morning twilight: tonight's preflight is long past.
            self._night_done = True
            return False, "main"
        return True, "preflight"

    def _rising(self, now):
        try:
            return float(self._angle_fn(now + TREND_PROBE)) > self.angle
        except Exception:
            return False

    def _set(self, is_open, now, stage):
        was_open, first = self._open, not self._reported
        was_stage = self.stage
        self._open = is_open
        self.stage = stage
        self._reported = True
        if is_open:
            self.opens_at = None
            if first or not was_open:
                self.night += 1
                console_ui.log(f"Sun at {self._shown()} — "
                               f"{'preflight stage' if stage == 'preflight' else 'captures'}"
                               f" run")
            elif stage != was_stage:
                console_ui.log(f"Sun at {self._shown()} — "
                               f"{'preflight stage' if stage == 'preflight' else 'main captures'}")
            return
        if first or was_open:
            try:
                # A morning already below start_angle would read as "opens now".
                self.opens_at = (None if self.angle is not None
                                 and self.angle <= self.open_angle
                                 else sun_crossing_time(self._angle_fn,
                                                        self.open_angle, now))
            except Exception:
                self.opens_at = None
            console_ui.log(f"Sun at {self._shown()} — captures wait for the night"
                           f"{self._when()}")
        elif (self.opens_at is None and self.angle is not None
              and self.angle > self.open_angle):
            # Closed in the morning between the two angles: tonight is known only once
            # the sun is up.
            try:
                self.opens_at = sun_crossing_time(self._angle_fn, self.open_angle, now)
            except Exception:
                self.opens_at = None

    def _shown(self):
        angle = "?" if self.angle is None else f"{self.angle:+.1f}°"
        if self.start_angle is None:
            return f"{angle} (threshold {self.max_angle:g}°)"
        return (f"{angle} (preflight {self.start_angle:g}°, "
                f"threshold {self.max_angle:g}°)")

    def _when(self):
        return f", ~{self.opens_at.strftime('%H:%M')}" if self.opens_at else ""

    def describe(self):
        if self._open:
            what = "preflight" if self.stage == "preflight" else "capturing"
            return f"sun {self._shown()}, {what}"
        return f"sun {self._shown()}, waiting{self._when()}"
