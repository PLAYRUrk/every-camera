"""FITS output for the ASI imager, with the instrument's established header set.

Three header sets are written, on purpose:

* the eighteen cards both imagers share, from ``cameras/common/fits.py`` — that
  module also explains why ``DATE-OBS`` is UTC and ``DATE-LOC`` exists;
* what only the PIXIS has (``GAIN``, ``SETTEMP``) plus what this driver's
  intensity-control loops decided (``SKYMEAN``, ``SPLITNUM``/``SPLITIDX``);
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

from datetime import datetime
from pathlib import Path

import numpy as np
from astropy.io import fits

# The legacy records themselves are written by ``write_legacy_keys``, shared with
# the Hamamatsu imager's ``sun_cycle`` frames; importing it also silences
# astropy's HIERARCH warning for them.
from ..common.fits import (  # noqa: F401
    UNKNOWN_TEMP, utc_string as _utc_string, write_core_header, write_image,
    write_legacy_keys,
)

# imagerd_rt's own version string (``SOFTWARE_VERSION``, imagerd_rt.h:95). The
# processing program has always seen "3.0" here.
LEGACY_VERSION = "3.0"


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
    hdu = fits.PrimaryHDU(data)
    h = hdu.header
    # ``OBSMODE`` may read ``sun_cycle_auto``: the preflight stage, the sun_cycle
    # schedule shot with automatically chosen exposures, before the programme
    # proper starts.
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
        camera_vendor=camera_vendor,
        camera_model=camera_model,
        camera_sn=camera_sn,
        camera_version=camera_version,
        driver_version=driver_version,
        dcam_version=dcam_version,
        lat=lat,
        lon=lon,
        elevation=elevation,
        date_loc=True,
    )
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

    write_legacy_keys(
        h,
        binning=binning,
        bit_depth=bit_depth,
        gain=gain,
        ccd_temp=ccd_temp,
        exposure_sec=exposure_sec,
        readout_speed_text=_readout_speed_text(readout_speed),
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
    write_image(path, data, h)

