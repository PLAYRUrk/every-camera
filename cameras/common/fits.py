"""The FITS cards both imagers write, and the write itself.

``DATE-OBS`` is the start of the exposure in UTC, as the FITS standard requires;
``DATE-LOC`` keeps the local wall-clock time of the operator's night log.

Values are written untouched, so ``READSPD`` can be ``2.0`` MHz on the PIXIS and a
speed setting of ``2`` on the Hamamatsu; differing comment text comes in through
``comments``.

``instrument=False`` leaves out what describes the camera and its software: the
Hamamatsu's ``sun_cycle`` frames carry one ``NAME`` card instead.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import warnings

import numpy as np

from astropy.io import fits
from astropy.io.fits.verify import VerifyWarning

import intensity

from .timeutil import to_utc

# The long legacy names deliberately become HIERARCH cards; astropy would warn for
# each one on every frame.
warnings.filterwarnings("ignore", category=VerifyWarning,
                        message=r"Keyword name .* is greater than 8 characters")

INSTRUMENT_CARDS = ("INSTRUME", "VENDOR", "CAMSN", "CAMVER", "DRVVER", "DCAMVER")

# imagerd_rt's value for a reading it could not take; the processing program tests for
# it.
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
    "ADCFULL": "[ADU] intensity full scale",
}


def utc_string(timestamp: datetime) -> str:
    return to_utc(timestamp).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def local_string(timestamp: datetime) -> str:
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
    instrument: bool = True,
) -> None:
    """Append the shared cards to ``h``, in the order the archive has them.

    ``date_loc`` has no default on purpose: whether a camera writes it is a property
    of its archive.
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
    if instrument:
        h["INSTRUME"] = (camera_model, say["INSTRUME"])
        h["VENDOR"] = (camera_vendor, say["VENDOR"])
        h["CAMSN"] = (camera_sn, say["CAMSN"])
        h["CAMVER"] = (camera_version, say["CAMVER"])
        h["DRVVER"] = (driver_version, say["DRVVER"])
        h["DCAMVER"] = (dcam_version, say["DCAMVER"])
    h["SITELAT"] = (lat, say["SITELAT"])
    h["SITELON"] = (lon, say["SITELON"])
    h["SITEELEV"] = (elevation, say["SITEELEV"])
    # Written by every camera, so a reader never guesses the scale from the data and a
    # frame from before the program standardised on 16 bits stays distinguishable.
    h["ADCFULL"] = (intensity.FULL_SCALE, say["ADCFULL"])


def write_legacy_keys(h, *, binning, bit_depth, gain, ccd_temp, exposure_sec,
                      readout_speed_text, seqno, site_id, device_id, lat, lon,
                      filter_num, filter_wavelength, filter_description,
                      fw_temp, legacy_version, instrument=True) -> None:
    """Append imagerd_rt's sixteen metadata records, in its own order.

    Values keep the original formatting (``Exposure`` as a string in ms, coordinates
    to two decimals): the processing program parses what the old archive contains.
    ``readout_speed_text`` arrives worded because the two cameras word it differently.
    """
    # ``Binning`` uppercases onto the BINNING card written above, with the same value.
    h["Binning"] = (binning, "pixel binning NxN")
    if instrument and bit_depth is not None:
        h["BitDepth"] = (bit_depth, "sensor bit depth")
    if instrument and gain is not None:
        h["CCDGain"] = (gain, "ADC analog gain: 1 Low, 2 Medium, 3 High")
    h["CCDTemp"] = (round(ccd_temp if ccd_temp is not None else UNKNOWN_TEMP, 2),
                    "[C] CCD sensor temperature")
    h["Exposure"] = (f"{float(exposure_sec) * 1000:.2f} ms", "exposure duration")
    h["ReadoutSpeed"] = (readout_speed_text, "ADC readout speed")
    if seqno is not None:
        h["SEQNO"] = (seqno, "archive frame sequence number")
    h["SiteID"] = (site_id, "station identifier")
    if instrument:
        h["DeviceID"] = (device_id, "imager identifier")
    h["Latitude"] = (round(float(lat), 2), "[deg] observatory latitude")
    h["Longitude"] = (round(float(lon), 2), "[deg] observatory longitude")
    h["FilterWavelength"] = (filter_wavelength, "filter wavelength tag")
    h["FilterPosition"] = (filter_num, "filter wheel position")
    h["FilterDescription"] = (filter_description, "filter description")
    h["FWTemp"] = (round(fw_temp if fw_temp is not None else UNKNOWN_TEMP, 2),
                   "[C] filter wheel temperature")
    if instrument:
        h["Version"] = (legacy_version, "imagerd_rt metadata version")


def write_image(path: Path, data: np.ndarray | None, header) -> None:
    """Refuses to overwrite. A failed capture must not become a file: an empty frame
    looks like data.
    """
    if data is None:
        raise ValueError(f"Cannot write FITS '{path}': image data is None "
                         f"(capture failed)")
    fits.writeto(str(path), data, header=header, overwrite=False)
