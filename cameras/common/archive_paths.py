"""Archive layout the processing program expects.

imagerd_rt built ``/data/%Y/%m/%d/<site>_<device>/ut%H/``. The last two levels are
dropped because one every-camera instance owns one output directory. Everything is
UTC, so a night splits across two day directories at UTC midnight.

In the japan ``sun_cycle`` mode ``site_id`` is ``japan.location.name`` and
``device_id`` is ``japan.name``.
"""
from __future__ import annotations

from pathlib import Path

from .timeutil import to_utc


def day_dir(root, timestamp) -> Path:
    utc = to_utc(timestamp)
    return Path(root) / f"{utc:%Y}" / f"{utc:%m}" / f"{utc:%d}"


# Wavelength field when the wheel position is unknown, as for the opening darks taken
# at home. An empty field would still have to be parsed; this cannot be mistaken for a
# wavelength.
NO_FILTER_TAG = "none"


def frame_name(timestamp, *, site_id, device_id, wavelength, exposure_sec,
               dark=False, preflight=False) -> str:
    """``YYYYMMDD_hhmmss_SITE_DEV_WAVE_EEEEEEms[_DARK][_pf].fits``.

    ``EEEEEE`` is milliseconds; ``_pf`` marks a frame of the automatic twilight stage.
    """
    utc = to_utc(timestamp)
    exposure_ms = int(round(float(exposure_sec) * 1000))
    name = (f"{utc:%Y%m%d_%H%M%S}_{site_id}_{device_id}_"
            f"{wavelength or NO_FILTER_TAG}_{exposure_ms:06d}ms")
    if dark:
        name += "_DARK"
    if preflight:
        name += "_pf"
    return name + ".fits"


def frame_path(root, timestamp, *, site_id, device_id, wavelength,
               exposure_sec, dark=False, preflight=False) -> Path:
    return day_dir(root, timestamp) / frame_name(
        timestamp, site_id=site_id, device_id=device_id, wavelength=wavelength,
        exposure_sec=exposure_sec, dark=dark, preflight=preflight)
