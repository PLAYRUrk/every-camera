"""FITS output for the ASI imager, with the instrument's established header set.

``DATE-OBS`` is the *start* of the exposure, written in UTC as the FITS standard
requires. The asi-camera original passed a naive local timestamp under a header
comment claiming UTC; converting here is a deliberate behaviour change, so that
frames from this station can be compared with any other instrument's.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from astropy.io import fits


def _utc_string(timestamp: datetime) -> str:
    """Format a timestamp as a FITS UTC string, assuming local time if naive."""
    if timestamp.tzinfo is None:
        timestamp = timestamp.astimezone()
    return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def write_fits(
    path: Path,
    data: np.ndarray | None,
    *,
    timestamp: datetime,
    exposure_sec: float,
    binning: int,
    readout_speed: float,
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
    gain: int | None = None,
    set_temp: float | None = None,
) -> None:
    if data is None:
        raise ValueError(f"Cannot write FITS '{path}': image data is None (capture failed)")
    hdu = fits.PrimaryHDU(data)
    h = hdu.header
    h["DATE-OBS"] = (_utc_string(timestamp), "UTC observation start time")
    h["DATE-LOC"] = (timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
                     "local observation start time")
    h["EXPTIME"] = (exposure_sec, "[s] exposure duration")
    h["BINNING"] = (binning, "pixel binning NxN")
    h["READSPD"] = (readout_speed, "[MHz] ADC readout speed")
    h["FILTER"] = (filter_num, "filter wheel position")
    h["CCD-TEMP"] = (ccd_temp if ccd_temp is not None else -999.0, "[C] CCD sensor temperature")
    h["IMAGETYP"] = (image_type, "frame type: LIGHT or DARK")
    h["OBSMODE"] = (obs_mode, "observation mode: sun, time, or dark")
    h["INSTRUME"] = (camera_model, "camera model")
    h["VENDOR"] = (camera_vendor, "camera manufacturer")
    h["CAMSN"] = (camera_sn, "camera serial number")
    h["CAMVER"] = (camera_version, "camera firmware version")
    h["DRVVER"] = (driver_version, "camera driver version")
    h["DCAMVER"] = (dcam_version, "camera SDK (PICAM) version")
    h["SITELAT"] = (lat, "[deg] observatory latitude")
    h["SITELON"] = (lon, "[deg] observatory longitude")
    h["SITEELEV"] = (elevation, "[m] observatory elevation")
    # PIXIS-specific additions the Hamamatsu camera had no equivalent for.
    if gain is not None:
        h["GAIN"] = (gain, "ADC analog gain: 1 Low, 2 Medium, 3 High")
    if set_temp is not None:
        h["SETTEMP"] = (set_temp, "[C] sensor temperature setpoint")
    fits.writeto(str(path), data, header=h, overwrite=False)
