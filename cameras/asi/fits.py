"""FITS output for the ASI imager, with the instrument's established header set.

``DATE-OBS`` is the *start* of the exposure, written in UTC as the FITS standard
requires. The asi-camera original passed a naive local timestamp under a header
comment claiming UTC; converting here is a deliberate behaviour change, so that
frames from this station can be compared with any other instrument's.

Two header sets are written, on purpose:

* the every-camera set (``EXPTIME``, ``BINNING``, ``CCD-TEMP``, …), and
* the sixteen keys imagerd_rt attached to every frame, spelled exactly as it
  spelled them.

imagerd_rt did not write FITS at all — it saved 16-bit PNG and put its metadata
in PNG ``tEXt`` records, built by ``Build_Image()`` in ``lib_capture.c:445-540``.
Those record names are what the station's processing program reads, so they are
reproduced verbatim, in the original order, including the ones that merely
duplicate a modern keyword (``Binning``) and the ones whose value is a formatted
string rather than a number (``Exposure``, ``ReadoutSpeed``). Names longer than
eight characters become HIERARCH cards, which keeps the spelling intact;
astropy reads them back under the same name.
"""
from __future__ import annotations
import warnings

from datetime import datetime
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.io.fits.verify import VerifyWarning

from .timeutil import to_utc

# The long legacy names deliberately become HIERARCH cards. astropy warns once
# per card, which would be five lines of noise for every frame of every night;
# the choice is made knowingly, so the warning is silenced here rather than
# left to whoever reads the console.
warnings.filterwarnings("ignore", category=VerifyWarning,
                        message=r"Keyword name .* is greater than 8 characters")

# imagerd_rt's own version string (``SOFTWARE_VERSION``, imagerd_rt.h:95). The
# processing program has always seen "3.0" here.
LEGACY_VERSION = "3.0"

# Written where the original had a reading it could not take. The filter wheel
# controller used here has no temperature sensor at all.
UNKNOWN_TEMP = -999.0


def _utc_string(timestamp: datetime) -> str:
    """Format a timestamp as a FITS UTC string, assuming local time if naive."""
    return to_utc(timestamp).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _readout_speed_text(readout_speed: float) -> str:
    """The two spellings imagerd_rt used for ADC speed (``lib_capture.c:480-485``)."""
    if abs(float(readout_speed) - 2.0) < 1e-9:
        return "2 MHz"
    if abs(float(readout_speed) - 0.1) < 1e-9:
        return "100 KHz"
    return f"{float(readout_speed):g} MHz"


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
    bit_depth: int | None = None,
    seqno: int | None = None,
    site_id: str = "",
    device_id: str = "",
    filter_wavelength: str = "",
    filter_description: str = "",
    fw_temp: float | None = None,
    legacy_version: str = LEGACY_VERSION,
    sky_mean: float | None = None,
    split_count: int = 1,
    split_index: int = 1,
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
    h["CCD-TEMP"] = (ccd_temp if ccd_temp is not None else UNKNOWN_TEMP,
                     "[C] CCD sensor temperature")
    h["IMAGETYP"] = (image_type, "frame type: LIGHT or DARK")
    # ``sun_cycle_auto`` is the preflight stage: the sun_cycle schedule shot with
    # automatically chosen exposures, before the programme proper starts.
    h["OBSMODE"] = (obs_mode, "observation mode; see also SPLITNUM")
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
    # What the intensity-control loops measured and decided. SKYMEAN is written
    # for every frame because it costs one pass over the array and answers the
    # first question anyone asks of an archived frame; the SPLIT* pair appears
    # only when a slot was actually divided.
    if sky_mean is not None:
        h["SKYMEAN"] = (round(float(sky_mean), 2), "[ADU] mean frame intensity")
    if split_count and split_count > 1:
        h["SPLITNUM"] = (int(split_count), "sub-frames this slot was divided into")
        h["SPLITIDX"] = (int(split_index), "index of this sub-frame, 1-based")

    _write_legacy_keys(
        h,
        binning=binning,
        bit_depth=bit_depth,
        gain=gain,
        ccd_temp=ccd_temp,
        exposure_sec=exposure_sec,
        readout_speed=readout_speed,
        seqno=seqno,
        site_id=site_id,
        device_id=device_id,
        lat=lat,
        lon=lon,
        filter_num=filter_num,
        filter_wavelength=filter_wavelength,
        filter_description=filter_description,
        fw_temp=fw_temp,
        legacy_version=legacy_version,
    )
    fits.writeto(str(path), data, header=h, overwrite=False)


def _write_legacy_keys(h, *, binning, bit_depth, gain, ccd_temp, exposure_sec,
                       readout_speed, seqno, site_id, device_id, lat, lon,
                       filter_num, filter_wavelength, filter_description,
                       fw_temp, legacy_version) -> None:
    """Append imagerd_rt's sixteen metadata records, in its own order.

    Values keep the original formatting, down to ``Exposure`` being a string in
    milliseconds and the coordinates being rounded to two decimals: the
    processing program parses what the old archive contains, not what would be
    tidier here.
    """
    # ``Binning`` uppercases onto the BINNING card written above — same value,
    # same meaning, so the duplicate spelling costs nothing.
    h["Binning"] = (binning, "pixel binning NxN")
    if bit_depth is not None:
        h["BitDepth"] = (bit_depth, "sensor bit depth")
    if gain is not None:
        h["CCDGain"] = (gain, "ADC analog gain: 1 Low, 2 Medium, 3 High")
    h["CCDTemp"] = (round(ccd_temp if ccd_temp is not None else UNKNOWN_TEMP, 2),
                    "[C] CCD sensor temperature")
    h["Exposure"] = (f"{float(exposure_sec) * 1000:.2f} ms", "exposure duration")
    h["ReadoutSpeed"] = (_readout_speed_text(readout_speed), "ADC readout speed")
    if seqno is not None:
        h["SEQNO"] = (seqno, "archive frame sequence number")
    h["SiteID"] = (site_id, "station identifier")
    h["DeviceID"] = (device_id, "imager identifier")
    h["Latitude"] = (round(float(lat), 2), "[deg] observatory latitude")
    h["Longitude"] = (round(float(lon), 2), "[deg] observatory longitude")
    h["FilterWavelength"] = (filter_wavelength, "filter wavelength tag")
    h["FilterPosition"] = (filter_num, "filter wheel position")
    h["FilterDescription"] = (filter_description, "filter description")
    h["FWTemp"] = (round(fw_temp if fw_temp is not None else UNKNOWN_TEMP, 2),
                   "[C] filter wheel temperature")
    h["Version"] = (legacy_version, "imagerd_rt metadata version")
