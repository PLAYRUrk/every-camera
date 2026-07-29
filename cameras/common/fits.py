"""The FITS cards both imagers write, and the write itself.

The Hamamatsu program defined this header set; the PIXIS driver inherited it and
then added its own cards (``GAIN``, ``SETTEMP``, the intensity-control pair) and
the sixteen records imagerd_rt attached to every frame. What is left over — the
eighteen cards below — is genuinely the same for both instruments, down to the
comment text, so it is written in one place.

``DATE-OBS`` is the *start* of the exposure in UTC, as the FITS standard requires.
Both original programs passed a naive local timestamp under a comment claiming
UTC; converting here is a deliberate behaviour change, and ``DATE-LOC`` keeps the
local wall-clock time that the operator's night log is written in, so nothing is
lost by it.

Values are written through untouched. That is what lets one function serve a
``READSPD`` of ``2.0`` MHz for the PIXIS and an ``int`` speed setting of ``2`` for
the Hamamatsu without either camera's spelling leaking into the other's frames;
the differing comment text comes in through ``comments``.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np

from astropy.io import fits

from .timeutil import to_utc

# Written where a program had a reading it could not take. imagerd_rt's value,
# kept because the station's processing program tests for it.
UNKNOWN_TEMP = -999.0

CORE_COMMENTS = {
    "DATE-OBS": "UTC observation start time",
    "DATE-LOC": "local observation start time",
    "EXPTIME": "[s] exposure duration",
    "BINNING": "pixel binning NxN",
    "READSPD": "[MHz] ADC readout speed",
    "FILTER": "filter wheel position",
    "CCD-TEMP": "[C] CCD sensor temperature",
    "IMAGETYP": "frame type: LIGHT or DARK",
    "OBSMODE": "observation mode; see also SPLITNUM",
    "INSTRUME": "camera model",
    "VENDOR": "camera manufacturer",
    "CAMSN": "camera serial number",
    "CAMVER": "camera firmware version",
    "DRVVER": "camera driver version",
    "DCAMVER": "camera SDK (PICAM) version",
    "SITELAT": "[deg] observatory latitude",
    "SITELON": "[deg] observatory longitude",
    "SITEELEV": "[m] observatory elevation",
}


def utc_string(timestamp: datetime) -> str:
    """Format a timestamp as a FITS UTC string, assuming local time if naive."""
    return to_utc(timestamp).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def local_string(timestamp: datetime) -> str:
    """Format a timestamp as it reads on the station's own clock."""
    return timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def write_core_header(
    h,
    *,
    timestamp: datetime,
    exposure_sec: float,
    binning: int,
    readout_speed,
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
    date_loc: bool,
    comments: dict | None = None,
) -> None:
    """Append the shared cards to ``h``, in the order the archive has them.

    ``date_loc`` has no default on purpose: whether a camera writes the local
    timestamp is a property of its archive, and both callers say so out loud
    rather than inheriting whatever happened to be convenient here.
    """
    say = dict(CORE_COMMENTS, **(comments or {}))
    h["DATE-OBS"] = (utc_string(timestamp), say["DATE-OBS"])
    if date_loc:
        h["DATE-LOC"] = (local_string(timestamp), say["DATE-LOC"])
    h["EXPTIME"] = (exposure_sec, say["EXPTIME"])
    h["BINNING"] = (binning, say["BINNING"])
    h["READSPD"] = (readout_speed, say["READSPD"])
    h["FILTER"] = (filter_num, say["FILTER"])
    h["CCD-TEMP"] = (ccd_temp if ccd_temp is not None else UNKNOWN_TEMP,
                     say["CCD-TEMP"])
    h["IMAGETYP"] = (image_type, say["IMAGETYP"])
    h["OBSMODE"] = (obs_mode, say["OBSMODE"])
    h["INSTRUME"] = (camera_model, say["INSTRUME"])
    h["VENDOR"] = (camera_vendor, say["VENDOR"])
    h["CAMSN"] = (camera_sn, say["CAMSN"])
    h["CAMVER"] = (camera_version, say["CAMVER"])
    h["DRVVER"] = (driver_version, say["DRVVER"])
    h["DCAMVER"] = (dcam_version, say["DCAMVER"])
    h["SITELAT"] = (lat, say["SITELAT"])
    h["SITELON"] = (lon, say["SITELON"])
    h["SITEELEV"] = (elevation, say["SITEELEV"])


def write_image(path: Path, data: np.ndarray | None, header) -> None:
    """Write one frame, refusing to overwrite and refusing to file a failure.

    A capture that returned nothing must not become a file: an empty frame in the
    archive is worse than a gap, because it looks like data.
    """
    if data is None:
        raise ValueError(f"Cannot write FITS '{path}': image data is None "
                         f"(capture failed)")
    fits.writeto(str(path), data, header=header, overwrite=False)
