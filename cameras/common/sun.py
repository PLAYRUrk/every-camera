"""Solar altitude for the sun-driven schedule.

astropy is imported lazily: it costs a couple of seconds to import and is only
needed when the schedule actually runs in ``sun`` mode.

Timestamps are converted to UTC before being handed to astropy. Both original
programs — asi-camera and the Hamamatsu japan-camera it descends from — formatted
a naive local time straight into ``Time(...)``, which astropy reads as UTC:
correct only on a station whose clock is already UTC, and hours wrong anywhere
else. Fixing it moves when a ``sun`` window opens on such a station, which is why
it is stated here rather than left to be discovered.
"""
from datetime import datetime

from .timeutil import to_utc as _to_utc


def sun_angle(t: datetime, lat: float, lon: float, elevation: float) -> float:
    """Solar altitude in degrees at time ``t`` for the given site."""
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
