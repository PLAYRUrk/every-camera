"""Where ASI frames land and what their headers say.

Both are contracts with the station's processing program, which was written
against imagerd_rt's output: it walks a UTC ``YYYY/MM/DD`` tree and reads the
sixteen metadata names that program attached to every frame. A rename here is a
silent data loss there, so the names and the layout are pinned down by tests
rather than left to whoever edits the writer next.
"""
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.asi import paths                          # noqa: E402
from cameras.asi.fits import write_fits                # noqa: E402
from cameras.asi.seqno import next_seqno               # noqa: E402
from cameras.asi.timeutil import to_utc                # noqa: E402

fits = pytest.importorskip("astropy.io.fits")

# An aware timestamp keeps the expected paths independent of the machine's zone.
NOON_UTC = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def test_frames_are_filed_under_a_utc_date_tree():
    path = paths.frame_path("/data/asi", NOON_UTC, site_id="TORY",
                            device_id="ASI0", wavelength="5577",
                            exposure_sec=55)
    assert path.parent == Path("/data/asi/2026/07/28")


def test_the_tree_follows_utc_not_the_local_day():
    """01:00 in Irkutsk on the 29th is still the 28th in UTC, as it was before."""
    late = datetime(2026, 7, 29, 1, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    path = paths.frame_path("/data/asi", late, site_id="TORY", device_id="ASI0",
                            wavelength="5577", exposure_sec=55)
    assert path.parent == Path("/data/asi/2026/07/28")
    assert path.name.startswith("20260728_170000_")


def test_the_file_name_is_the_one_imagerd_rt_wrote():
    name = paths.frame_name(NOON_UTC, site_id="TORY", device_id="ASI0",
                            wavelength="5577", exposure_sec=55)
    assert name == "20260728_120000_TORY_ASI0_5577_055000ms.fits"


def test_a_short_exposure_is_still_padded_to_six_digits():
    name = paths.frame_name(NOON_UTC, site_id="TORY", device_id="ASI0",
                            wavelength="OH__", exposure_sec=7)
    assert "_007000ms" in name


def test_darks_carry_the_legacy_suffix():
    name = paths.frame_name(NOON_UTC, site_id="TORY", device_id="ASI0",
                            wavelength="5577", exposure_sec=55, dark=True)
    assert name.endswith("_055000ms_DARK.fits")


def test_two_filters_in_the_same_second_do_not_collide():
    common = dict(site_id="TORY", device_id="ASI0", exposure_sec=55)
    first = paths.frame_name(NOON_UTC, wavelength="5577", **common)
    second = paths.frame_name(NOON_UTC, wavelength="6300", **common)
    assert first != second


# ---------------------------------------------------------------------------
# SEQNO
# ---------------------------------------------------------------------------
def test_seqno_starts_at_one_and_survives_a_restart(tmp_path):
    assert next_seqno(tmp_path) == 1
    assert next_seqno(tmp_path) == 2
    assert (tmp_path / "seqno.txt").read_text().strip() == "2"
    # A fresh process reads the file back rather than starting over.
    assert next_seqno(tmp_path) == 3


def test_seqno_continues_from_a_counter_copied_from_the_old_station(tmp_path):
    (tmp_path / "seqno.txt").write_text("184213\n")
    assert next_seqno(tmp_path) == 184214


def test_a_corrupt_counter_restarts_instead_of_raising(tmp_path):
    (tmp_path / "seqno.txt").write_text("not a number")
    assert next_seqno(tmp_path) == 1


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------
def write_frame(path, **overrides):
    kwargs = dict(
        timestamp=NOON_UTC, exposure_sec=55.0, binning=4, readout_speed=2.0,
        filter_num=1, ccd_temp=-60.123, image_type="LIGHT", obs_mode="sun_cycle",
        camera_vendor="Princeton Instruments", camera_model="Pixis2048B",
        camera_sn="SN1", camera_version="fw", driver_version="drv",
        dcam_version="picam", lat=51.810646, lon=103.075555, elevation=658.0,
        gain=3, set_temp=-60.0, bit_depth=16, seqno=42, site_id="TORY",
        device_id="ASI0", filter_wavelength="5577",
        filter_description="557.7nm x 2.0nm",
    )
    kwargs.update(overrides)
    write_fits(path, np.zeros((4, 4), dtype="<u2"), **kwargs)
    return fits.getheader(path)


def test_every_legacy_metadata_name_survives_a_round_trip(tmp_path):
    header = write_frame(tmp_path / "frame.fits")
    assert header["Binning"] == 4
    assert header["BitDepth"] == 16
    assert header["CCDGain"] == 3
    assert header["CCDTemp"] == pytest.approx(-60.12)
    assert header["Exposure"] == "55000.00 ms"
    assert header["ReadoutSpeed"] == "2 MHz"
    assert header["SEQNO"] == 42
    assert header["SiteID"] == "TORY"
    assert header["DeviceID"] == "ASI0"
    assert header["Latitude"] == pytest.approx(51.81)
    assert header["Longitude"] == pytest.approx(103.08)
    assert header["FilterWavelength"] == "5577"
    assert header["FilterPosition"] == 1
    assert header["FilterDescription"] == "557.7nm x 2.0nm"
    assert header["FWTemp"] == pytest.approx(-999.0)
    assert header["Version"] == "3.0"


def test_the_modern_header_set_is_untouched(tmp_path):
    header = write_frame(tmp_path / "frame.fits")
    assert header["DATE-OBS"] == "2026-07-28T12:00:00.000"
    assert header["EXPTIME"] == 55.0
    assert header["CCD-TEMP"] == pytest.approx(-60.123)   # unrounded, unlike CCDTemp
    assert header["IMAGETYP"] == "LIGHT"
    assert header["OBSMODE"] == "sun_cycle"
    assert header["SITELON"] == pytest.approx(103.075555)  # full precision kept


def test_a_100_khz_readout_keeps_the_legacy_spelling(tmp_path):
    header = write_frame(tmp_path / "slow.fits", readout_speed=0.1)
    assert header["ReadoutSpeed"] == "100 KHz"


def test_a_missing_wheel_temperature_is_recorded_not_omitted(tmp_path):
    header = write_frame(tmp_path / "frame.fits", fw_temp=None)
    assert header["FWTemp"] == pytest.approx(-999.0)


def test_a_wheel_that_reports_a_temperature_is_written_as_read(tmp_path):
    header = write_frame(tmp_path / "frame.fits", fw_temp=23.456)
    assert header["FWTemp"] == pytest.approx(23.46)


def test_an_unknown_filter_costs_the_header_its_tag_not_the_frame(tmp_path):
    header = write_frame(tmp_path / "frame.fits", filter_num=0,
                         filter_wavelength="", filter_description="")
    assert header["FilterPosition"] == 0
    assert header["FilterWavelength"] == ""


def test_writing_refuses_to_overwrite_an_existing_frame(tmp_path):
    path = tmp_path / "frame.fits"
    write_frame(path)
    with pytest.raises(OSError):
        write_frame(path)


def test_naive_timestamps_are_read_as_local_time():
    naive = datetime(2026, 7, 28, 12, 0, 0)
    assert to_utc(naive) == naive.astimezone().astimezone(timezone.utc)
