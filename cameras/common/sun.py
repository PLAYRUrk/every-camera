"""Solar altitude. astropy is imported lazily: it costs seconds and only sun modes need
it.

Timestamps reach astropy in UTC: a naive local time would be read as UTC and be hours
wrong on a station not on UTC.

astropy's IERS defaults must never stop a night. The first calculation would try
``datacenter.iers.org`` and ``maia.usno.navy.mil``, normally unreachable from a
station, stalling ``remote_timeout`` per URL inside the measurement loop. Once the
bundled table's predictions are a month old, a failed download raises and ends the
run.

:func:`configure_iers` turns both off. That costs the UT1-UTC correction, under 0.9 s:
about 0.004° of solar altitude, against thresholds in whole degrees.

``EVERY_CAMERA_IERS_AUTO=1`` lets astropy try the network again, accepting that stall.
It does not restore the exception: no configuration can make an unreachable web server
stop a night.
"""
import os

from datetime import datetime

import console_ui

from .timeutil import to_utc as _to_utc

# Set on the first solar calculation: astropy caches the table it opens, so this must
# happen before anything asks for a time.
_iers_configured = False
_iers_offline = True


def configure_iers():
    """Stop Earth-rotation data from ever ending a night.

    Idempotent. Returns True when the network is left alone, False when
    ``EVERY_CAMERA_IERS_AUTO`` asks for it; the raising is off either way.
    """
    global _iers_configured, _iers_offline
    if _iers_configured:
        return _iers_offline

    # Before the flag: an astropy that fails to import has configured nothing, and the
    # next call should try again.
    from astropy.utils import iers

    wanted = os.environ.get("EVERY_CAMERA_IERS_AUTO", "").strip().lower()
    offline = wanted not in ("1", "true", "yes", "on")

    iers.conf.auto_download = not offline
    # None disables the age check, which is what turns a failed download into an
    # exception. Both modes need it: trying the network must not risk the night.
    iers.conf.auto_max_age = None
    # astropy >= 5.1; older versions have neither the setting nor the error it
    # governs. 'ignore' offline, where staleness is the steady state; 'warn' when
    # fresh data was asked for.
    if hasattr(iers.conf, "iers_degraded_accuracy"):
        iers.conf.iers_degraded_accuracy = "ignore" if offline else "warn"

    _iers_configured = True
    _iers_offline = offline

    # Once per run: the setting decides whether the run can stall on a web server,
    # which is what someone reading the log after a late frame wants to rule out.
    if offline:
        console_ui.log("Earth rotation: bundled IERS table, no network "
                       "(solar altitude within 0.01°)")
    else:
        console_ui.log("Earth rotation: EVERY_CAMERA_IERS_AUTO is set — the "
                       "first solar calculation may wait on the IERS servers")
    return offline


def sun_angle(t: datetime, lat: float, lon: float, elevation: float) -> float:
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
    def _angle(when):
        return sun_angle(when, location.lat, location.lon, location.elevation)
    return _angle
