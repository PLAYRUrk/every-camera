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

``instrument=False`` leaves out every card that describes the camera and its
software (``INSTRUME``, ``VENDOR``, ``CAMSN``, ``CAMVER``, ``DRVVER``,
``DCAMVER``). The Hamamatsu imager's ``sun_cycle`` frames are written that way:
they carry the instrument's name in one ``NAME`` card, set in config.json, and
nothing about which camera took them.

:func:`write_legacy_keys` appends the sixteen records imagerd_rt attached to
every frame. The ASI imager writes all of them; ``sun_cycle`` frames of the
Hamamatsu imager write them with ``instrument=False``, which drops the four
that describe the camera or the program (``BitDepth``, ``CCDGain``,
``DeviceID``, ``Version``).
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

# The long legacy names deliberately become HIERARCH cards. astropy warns once
# per card, which would be five lines of noise for every frame of every night;
# the choice is made knowingly, so the warning is silenced here rather than
# left to whoever reads the console. Only the legacy writer produces such cards.
warnings.filterwarnings("ignore", category=VerifyWarning,
                        message=r"Keyword name .* is greater than 8 characters")

# The cards that say which camera and which software took a frame. Left out of
# the header when ``write_core_header`` is called with ``instrument=False``.
INSTRUMENT_CARDS = ("INSTRUME", "VENDOR", "CAMSN", "CAMVER", "DRVVER", "DCAMVER")

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
    "ADCFULL": "[ADU] intensity full scale",
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
    instrument: bool = True,
) -> None:
    """Append the shared cards to ``h``, in the order the archive has them.

    ``date_loc`` has no default on purpose: whether a camera writes the local
    timestamp is a property of its archive, and both callers say so out loud
    rather than inheriting whatever happened to be convenient here.

    ``instrument=False`` skips :data:`INSTRUMENT_CARDS`; the values passed for
    them are then simply not used.
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
    # The scale the pixel values are on. Written by every camera here, so a
    # reader never has to guess it from the data — and so a frame captured
    # before the program standardised on 16 bits stays distinguishable. The
    # PIXIS's own ``BitDepth`` card says the same thing in imagerd_rt's words
    # and stays where it is, in the ASI writer.
    h["ADCFULL"] = (intensity.FULL_SCALE, say["ADCFULL"])


def write_legacy_keys(h, *, binning, bit_depth, gain, ccd_temp, exposure_sec,
                      readout_speed_text, seqno, site_id, device_id, lat, lon,
                      filter_num, filter_wavelength, filter_description,
                      fw_temp, legacy_version, instrument=True) -> None:
    """Append imagerd_rt's sixteen metadata records, in its own order.

    Values keep the original formatting, down to ``Exposure`` being a string in
    milliseconds and the coordinates being rounded to two decimals: the
    processing program parses what the old archive contains, not what would be
    tidier here. ``readout_speed_text`` arrives already worded, because the two
    cameras word it differently (``2 MHz`` against ``2 (fast)``).

    ``instrument=False`` drops the records that describe the camera or the
    program rather than the frame: ``BitDepth``, ``CCDGain``, ``DeviceID`` and
    ``Version``.
    """
    # ``Binning`` uppercases onto the BINNING card written above — same value,
    # same meaning, so the duplicate spelling costs nothing.
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
    """Write one frame, refusing to overwrite and refusing to file a failure.

    A capture that returned nothing must not become a file: an empty frame in the
    archive is worse than a gap, because it looks like data.
    """
    if data is None:
        raise ValueError(f"Cannot write FITS '{path}': image data is None "
                         f"(capture failed)")
    fits.writeto(str(path), data, header=header, overwrite=False)
