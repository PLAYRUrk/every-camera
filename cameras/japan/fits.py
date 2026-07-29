"""FITS output for the Hamamatsu imager: the shared core, and nothing else.

Every card this writer produces is one of the eighteen in
``cameras/common/fits.py`` — this header set is where those eighteen came from.
Four comments differ from the PIXIS wording, because they describe a different
instrument, and they are the whole of the difference.

What is deliberately absent: ``GAIN`` and ``SETTEMP`` (no analog gain, no cooling
setpoint — the sensor temperature is a reading only), the intensity-control cards
``SKYMEAN``/``SPLITNUM``/``SPLITIDX`` (no loops here choose an exposure), and the
sixteen imagerd_rt legacy records, which belong to the ASI station's archive and
would be noise in a Hamamatsu frame.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np

from astropy.io import fits

from ..common.fits import UNKNOWN_TEMP, write_core_header, write_image  # noqa: F401

# The Hamamatsu spellings. READSPD carries DCAM's READOUTSPEED setting — an
# enumeration value, 1 or 2 — not the megahertz figure the PIXIS reports, so the
# shared "[MHz]" comment would be actively wrong here.
JAPAN_COMMENTS = {
    "READSPD": "readout speed setting",
    "OBSMODE": "observation mode: sun, time, or dark",
    "DRVVER": "DCAM driver version",
    "DCAMVER": "DCAM API version",
}


def write_fits(
    path: Path,
    data: np.ndarray | None,
    *,
    timestamp: datetime,
    exposure_sec: float,
    binning: int,
    readout_speed: int,
    filter_num: int,
    ccd_temp: float | None,
    image_type: str,
    obs_mode: str,
    camera_vendor: str,
    camera_model: str,
    camera_sn: str,
    camera_version: str,
    driver_version: str,
    dcam_version: str,
    lat: float,
    lon: float,
    elevation: float,
) -> None:
    hdu = fits.PrimaryHDU(data)
    write_core_header(
        hdu.header,
        timestamp=timestamp,
        exposure_sec=exposure_sec,
        binning=binning,
        readout_speed=readout_speed,
        filter_num=filter_num,
        ccd_temp=ccd_temp,
        image_type=image_type,
        obs_mode=obs_mode,
        camera_vendor=camera_vendor,
        camera_model=camera_model,
        camera_sn=camera_sn,
        camera_version=camera_version,
        driver_version=driver_version,
        dcam_version=dcam_version,
        lat=lat,
        lon=lon,
        elevation=elevation,
        # The old program's DATE-OBS held local time under a comment claiming
        # UTC. DATE-OBS is now genuinely UTC, and this card is what keeps the
        # station's own wall-clock time recoverable from the frame.
        date_loc=True,
        comments=JAPAN_COMMENTS,
    )
    write_image(path, data, hdu.header)
