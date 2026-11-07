"""FITS output for the Hamamatsu imager.

:func:`write_fits` (``sun``, ``time``) writes only the shared cards: ``GAIN`` and
``SETTEMP`` have no value here, no loop drives the intensity cards, and the imagerd_rt
records belong to the ASI archive.

:func:`write_sun_cycle_fits` files into that archive: the ASI header set minus every
card naming the camera or software, plus ``NAME``.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np

from astropy.io import fits

from ..common.fits import (  # noqa: F401
    UNKNOWN_TEMP, write_core_header, write_image, write_legacy_keys,
)

# READSPD carries DCAM's READOUTSPEED setting, an enumeration of 1 or 2, so the shared
# "[MHz]" comment would be wrong.
JAPAN_COMMENTS = {
    "READSPD": "readout speed setting",
    "OBSMODE": "observation mode: sun, time, or dark",
    "DRVVER": "DCAM driver version",
    "DCAMVER": "DCAM API version",
}

# The sun/time header above keeps its wording, which the archive already has.
SUN_CYCLE_COMMENTS = dict(JAPAN_COMMENTS,
                          OBSMODE="observation mode: sun_cycle or dark")


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
        # Keeps the station's wall-clock time recoverable: DATE-OBS is UTC.
        date_loc=True,
        comments=JAPAN_COMMENTS,
    )
    write_image(path, data, hdu.header)


def write_sun_cycle_fits(
    path: Path,
    data: np.ndarray | None,
    *,
    timestamp: datetime,
    exposure_sec: float,
    binning: int,
    readout_speed: int,
    readout_speed_text: str,
    filter_num: int,
    ccd_temp: float | None,
    image_type: str,
    obs_mode: str,
    name: str,
    site_name: str,
    lat: float,
    lon: float,
    elevation: float,
    seqno: int | None = None,
    filter_wavelength: str = "",
    filter_description: str = "",
    fw_temp: float | None = None,
    sky_mean: float | None = None,
    split_count: int = 1,
    split_index: int = 1,
) -> None:
    """The ASI header set without the instrument cards, plus ``NAME``.

    ``readout_speed_text`` is worded for this camera (``2 (fast)``): ``2 MHz`` would
    put a PIXIS figure into a Hamamatsu frame.
    """
    hdu = fits.PrimaryHDU(data)
    h = hdu.header
    write_core_header(
        h,
        timestamp=timestamp,
        exposure_sec=exposure_sec,
        binning=binning,
        readout_speed=readout_speed,
        filter_num=filter_num,
        ccd_temp=ccd_temp,
        image_type=image_type,
        obs_mode=obs_mode,
        camera_vendor="",
        camera_model="",
        camera_sn="",
        camera_version="",
        driver_version="",
        dcam_version="",
        lat=lat,
        lon=lon,
        elevation=elevation,
        date_loc=True,
        comments=SUN_CYCLE_COMMENTS,
        instrument=False,
    )
    h["NAME"] = (name, "instrument name")
    # SKYMEAN goes on every frame: it costs one pass over the array and answers the
    # first question asked of an archived frame.
    if sky_mean is not None:
        h["SKYMEAN"] = (round(float(sky_mean), 2), "[ADU] mean frame intensity")
    if split_count and split_count > 1:
        h["SPLITNUM"] = (int(split_count), "sub-frames this slot was divided into")
        h["SPLITIDX"] = (int(split_index), "index of this sub-frame, 1-based")
    write_legacy_keys(
        h,
        binning=binning,
        bit_depth=None,
        gain=None,
        ccd_temp=ccd_temp,
        exposure_sec=exposure_sec,
        readout_speed_text=readout_speed_text,
        seqno=seqno,
        site_id=site_name,
        device_id="",
        lat=lat,
        lon=lon,
        filter_num=filter_num,
        filter_wavelength=filter_wavelength,
        filter_description=filter_description,
        fw_temp=fw_temp,
        legacy_version="",
        instrument=False,
    )
    write_image(path, data, h)
