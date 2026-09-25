"""Solar altitude for the sun-driven schedule.

astropy is imported lazily: it costs a couple of seconds to import and is only
needed when the schedule actually runs in ``sun`` mode.

Timestamps are converted to UTC before being handed to astropy. Both original
programs — asi-camera and the Hamamatsu japan-camera it descends from — formatted
a naive local time straight into ``Time(...)``, which astropy reads as UTC:
correct only on a station whose clock is already UTC, and hours wrong anywhere
else. Fixing it moves when a ``sun`` window opens on such a station, which is why
it is stated here rather than left to be discovered.

Earth-rotation data (IERS)
--------------------------
astropy's defaults are written for an interactive session on a networked
workstation, and they are wrong for an observing station:

* ``auto_download = True`` — the first solar calculation of the night goes to
  ``datacenter.iers.org`` and ``maia.usno.navy.mil``. Those two hosts being
  unreachable is the normal case, not the exceptional one: a station can have
  a perfectly good internet connection and still not reach them. What that
  costs is a ``remote_timeout`` stall per URL — ten seconds each by default —
  *inside the measurement loop*, which is enough to make a slot late.
* ``auto_max_age = 30.0`` with ``iers_degraded_accuracy = 'error'`` — once the
  bundled table's predictive values are more than a month old and the download
  has failed, astropy **raises** rather than warns. That propagates out of
  ``sun_angle``, out of the mode loop, and stops the run, so the station loses
  every night from a month after its astropy was installed. Observed at Uzur
  on 2026-09-25 as ``Measurement loop failed: interpolating from IERS_Auto
  using predictive values that are more than 30.0 days old``.

:func:`configure_iers` fixes both. What the first one costs is the UT1−UTC
correction, which is bounded by a leap second — under 0.9 s. The sun moves 15
arcseconds per second of time, so the worst case is about 0.004° of solar
altitude, against thresholds written in whole degrees (``sun_max_angle`` is
typically −10°). It is not a measurable difference; losing the night is.

``EVERY_CAMERA_IERS_AUTO=1`` in the environment (``env.sh`` / ``env.cmd``) lets
astropy go to the network again, for a station that can reach those hosts and
wants the fresher table — accepting the stall on the first calculation of each
run. It does **not** restore the exception: whether the download works or not,
a solar altitude is always returned. No configuration of this program can make
an unreachable web server stop a night's measurements.
"""
import os

from datetime import datetime

import console_ui

from .timeutil import to_utc as _to_utc

# Set once, on the first solar calculation of the process — astropy caches the
# table it opens, so this has to happen before anything asks for a time.
_iers_configured = False
_iers_offline = True


def configure_iers():
    """Stop Earth-rotation data from ever ending a night.

    Idempotent and silent after the first call. Returns True when the network
    is left alone, False when the operator asked for it back with
    ``EVERY_CAMERA_IERS_AUTO``. Either way the raising is switched off — see the
    module docstring for why, and for what the default trades away (0.004°).
    """
    global _iers_configured, _iers_offline
    if _iers_configured:
        return _iers_offline

    # Before the flag, not after: an astropy that fails to import here has
    # configured nothing, and the next call should try again rather than report
    # settings that were never applied.
    from astropy.utils import iers

    wanted = os.environ.get("EVERY_CAMERA_IERS_AUTO", "").strip().lower()
    offline = wanted not in ("1", "true", "yes", "on")

    iers.conf.auto_download = not offline
    # None disables the age check outright. This is the setting that turns a
    # failed download into a raised exception, and it has to go in both modes:
    # asking astropy to try the network is not the same as agreeing to lose the
    # night when the network says no.
    iers.conf.auto_max_age = None
    # Added in astropy 5.1. Older versions have neither the setting nor the
    # error it governs, so its absence is not a problem to report. 'warn' when
    # the operator asked for fresh data — they should hear that it is stale;
    # 'ignore' by default, where staleness is the expected steady state and a
    # warning per run would only be noise.
    if hasattr(iers.conf, "iers_degraded_accuracy"):
        iers.conf.iers_degraded_accuracy = "ignore" if offline else "warn"

    _iers_configured = True
    _iers_offline = offline

    # Once per run, so that a night log says which of the two it was. Silence
    # here would be worse than a line: the setting decides whether the run can
    # stall on a web server, and that is exactly what somebody reading the log
    # after a late frame wants to rule out.
    if offline:
        console_ui.log("Earth rotation: bundled IERS table, no network "
                       "(solar altitude within 0.01°)")
    else:
        console_ui.log("Earth rotation: EVERY_CAMERA_IERS_AUTO is set — the "
                       "first solar calculation may wait on the IERS servers")
    return offline


def sun_angle(t: datetime, lat: float, lon: float, elevation: float) -> float:
    """Solar altitude in degrees at time ``t`` for the given site."""
    configure_iers()

    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, get_sun
    from astropy.time import Time

    location = EarthLocation.from_geodetic(lon * u.degree, lat * u.degree,
                                           elevation * u.meter)
    obs_time = Time(_to_utc(t).strftime("%Y-%m-%d %H:%M:%S"), scale="utc")
    frame = AltAz(location=location, obstime=obs_time)
    return get_sun(obs_time).transform_to(frame).alt.value


def angle_fn(location):
    """Return ``f(datetime) -> degrees`` bound to a :class:`config.LocationCfg`."""
    def _angle(when):
        return sun_angle(when, location.lat, location.lon, location.elevation)
    return _angle
