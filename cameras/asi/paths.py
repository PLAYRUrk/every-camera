"""Where a frame is filed, in the layout the processing program expects.

imagerd_rt built ``/data/%Y/%m/%d/<site>_<device>/ut%H/`` and a file name
carrying the site, device, filter wavelength and exposure
(``lib_capture.c:227-345``). Everything there is UTC — ``gmtime()`` at
``lib_capture.c:66`` — so a night is split across two day directories at UTC
midnight, and the archive is kept that way here rather than quietly re-based to
local time.

The station/device subdirectory and the ``ut%H`` level are dropped: one
every-camera instance owns one output directory, so those two levels only ever
had one value each.
"""
from __future__ import annotations

from pathlib import Path

from .timeutil import to_utc


def day_dir(root, timestamp) -> Path:
    """``<root>/YYYY/MM/DD`` for the UTC day ``timestamp`` falls in."""
    utc = to_utc(timestamp)
    return Path(root) / f"{utc:%Y}" / f"{utc:%m}" / f"{utc:%d}"


# Stands in for the wavelength tag when the wheel position is unknown — at
# startup it is parked at home, and the opening darks are taken there. imagerd_rt
# never met the case because it drove the wheel through the schedule for its
# darks too; a name with an empty field would still have to be parsed, so the
# field is filled with something that cannot be mistaken for a wavelength.
NO_FILTER_TAG = "none"


def frame_name(timestamp, *, site_id, device_id, wavelength, exposure_sec,
               dark=False, preflight=False) -> str:
    """The legacy file name: ``YYYYMMDD_hhmmss_SITE_DEV_WAVE_EEEEEEms[_DARK][_pf].fits``.

    The exposure is milliseconds zero-padded to six digits, as the original
    wrote it — 55 s becomes ``055000ms`` and 7 s becomes ``007000ms``.

    ``preflight`` tags a frame shot during the automatic twilight stage, so it
    can be told apart from the main programme's frames without opening it.
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
    """Full path of one frame; the directory is not created here."""
    return day_dir(root, timestamp) / frame_name(
        timestamp, site_id=site_id, device_id=device_id, wavelength=wavelength,
        exposure_sec=exposure_sec, dark=dark, preflight=preflight)
