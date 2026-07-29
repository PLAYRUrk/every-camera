"""What a Hamamatsu frame's header says — and, just as much, what it does not.

The Japan writer is built on the same core as the ASI one
(``cameras/common/fits.py``), which is the point: eighteen cards, one
implementation. The risk that creates is leakage — a PIXIS-only card, or the
sixteen imagerd_rt legacy records, appearing in a Hamamatsu frame because the
shared code grew a default. So these tests assert the exact card list, not just
that the values are right.

Four comments differ from the PIXIS wording, and those differences are real: this
camera's ``READSPD`` is DCAM's READOUTSPEED *setting*, 1 or 2, not a figure in
megahertz.
"""
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.japan.fits import write_fits                # noqa: E402

fits = pytest.importorskip("astropy.io.fits")

# Aware and eight hours ahead of UTC, like the station this camera runs at: it is
# the only way a test can tell "wrote UTC" from "wrote whatever it was handed".
IRKUTSK = timezone(timedelta(hours=8))
EVENING_LOCAL = datetime(2026, 7, 29, 1, 30, 0, tzinfo=IRKUTSK)

EXPECTED_CARDS = [
    ("SIMPLE", True, "conforms to FITS standard"),
    ("BITPIX", 16, "array data type"),
    ("NAXIS", 2, "number of array dimensions"),
    ("NAXIS1", 4, ""),
    ("NAXIS2", 4, ""),
    ("DATE-OBS", "2026-07-28T17:30:00.000", "UTC observation start time"),
    ("DATE-LOC", "2026-07-29T01:30:00.000", "local observation start time"),
    ("EXPTIME", 30.0, "[s] exposure duration"),
    ("BINNING", 1, "pixel binning NxN"),
    ("READSPD", 2, "readout speed setting"),
    ("FILTER", 3, "filter wheel position"),
    ("CCD-TEMP", -10.25, "[C] CCD sensor temperature"),
    ("IMAGETYP", "LIGHT", "frame type: LIGHT or DARK"),
    ("OBSMODE", "sun", "observation mode: sun, time, or dark"),
    ("INSTRUME", "C11440-22CU", "camera model"),
    ("VENDOR", "Hamamatsu Photonics", "camera manufacturer"),
    ("CAMSN", "S/N 000123", "camera serial number"),
    ("CAMVER", "1.20", "camera firmware version"),
    ("DRVVER", "4.0.1", "DCAM driver version"),
    ("DCAMVER", "4.10", "DCAM API version"),
    ("SITELAT", 53.324236, "[deg] observatory latitude"),
    ("SITELON", 107.741264, "[deg] observatory longitude"),
    ("SITEELEV", 515.0, "[m] observatory elevation"),
    ("BSCALE", 1, ""),
    ("BZERO", 32768, ""),
]

# Everything the ASI writer adds and this one must not.
FOREIGN_KEYWORDS = [
    "GAIN", "SETTEMP", "SKYMEAN", "SPLITNUM", "SPLITIDX",       # PIXIS / loops
    "BITDEPTH", "CCDGAIN", "CCDTEMP", "EXPOSURE", "ReadoutSpeed",
    "SEQNO", "SITEID", "DEVICEID", "LATITUDE", "Longitude",
    "FilterWavelength", "FilterPosition", "FilterDescription",
    "FWTEMP", "VERSION",                                        # imagerd_rt
]


def write_frame(path, **overrides):
    kwargs = dict(
        timestamp=EVENING_LOCAL, exposure_sec=30.0, binning=1, readout_speed=2,
        filter_num=3, ccd_temp=-10.25, image_type="LIGHT", obs_mode="sun",
        camera_vendor="Hamamatsu Photonics", camera_model="C11440-22CU",
        camera_sn="S/N 000123", camera_version="1.20", driver_version="4.0.1",
        dcam_version="4.10", lat=53.324236, lon=107.741264, elevation=515.0,
    )
    kwargs.update(overrides)
    write_fits(path, np.zeros((4, 4), dtype="<u2"), **kwargs)
    return fits.getheader(path)


def cards_of(header):
    return [(card.keyword, card.value, card.comment) for card in header.cards]


# ---------------------------------------------------------------------------
# The whole header, card for card
# ---------------------------------------------------------------------------
def test_the_header_is_exactly_the_eighteen_shared_cards(tmp_path):
    actual = cards_of(write_frame(tmp_path / "frame.fits"))
    assert [name for name, _, _ in actual] == \
           [name for name, _, _ in EXPECTED_CARDS]
    for (name, value, comment), (_, want, want_comment) in zip(actual,
                                                               EXPECTED_CARDS):
        if isinstance(want, float):
            assert value == pytest.approx(want), name
        else:
            assert value == want, name
        assert comment == want_comment, name


def test_no_pixis_or_imagerd_rt_card_leaks_in(tmp_path):
    """The shared core must not hand this camera the other one's headers."""
    header = write_frame(tmp_path / "frame.fits")
    present = [name for name in FOREIGN_KEYWORDS if name in header]
    assert present == [], f"foreign cards in a Hamamatsu frame: {present}"


def test_nothing_becomes_a_hierarch_card(tmp_path):
    """Only the legacy ASI names are long enough to need one; none are here."""
    header = write_frame(tmp_path / "frame.fits")
    assert [k for k in header.keys() if len(k) > 8] == []


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def test_date_obs_is_utc_not_the_clock_on_the_wall(tmp_path):
    """01:30 in Irkutsk is 17:30 the previous day in UTC.

    japan-camera wrote the local time here under a comment claiming UTC. This is
    the assertion that says the mislabel is gone.
    """
    header = write_frame(tmp_path / "frame.fits")
    assert header["DATE-OBS"] == "2026-07-28T17:30:00.000"


def test_date_loc_keeps_the_local_time_the_night_log_is_written_in(tmp_path):
    header = write_frame(tmp_path / "frame.fits")
    assert header["DATE-LOC"] == "2026-07-29T01:30:00.000"


def test_a_naive_timestamp_is_read_as_local_time(tmp_path):
    """What ``datetime.now()`` hands the driver, converted the same way."""
    naive = datetime(2026, 7, 29, 1, 30, 0)
    header = write_frame(tmp_path / "frame.fits", timestamp=naive)
    expected = naive.astimezone().astimezone(timezone.utc)
    assert header["DATE-OBS"] == expected.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    assert header["DATE-LOC"] == "2026-07-29T01:30:00.000"


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
def test_readout_speed_stays_an_integer_setting(tmp_path):
    """Not a float, and not "2 MHz": DCAM's READOUTSPEED is an enumeration."""
    header = write_frame(tmp_path / "slow.fits", readout_speed=1)
    assert header["READSPD"] == 1
    assert isinstance(header["READSPD"], int)


def test_a_sensor_that_will_not_report_is_recorded_not_omitted(tmp_path):
    """The processing program tests for -999, so the card has to be there."""
    header = write_frame(tmp_path / "frame.fits", ccd_temp=None)
    assert header["CCD-TEMP"] == pytest.approx(-999.0)


def test_a_dark_says_so_in_both_places(tmp_path):
    header = write_frame(tmp_path / "dark.fits", image_type="DARK",
                         obs_mode="dark")
    assert header["IMAGETYP"] == "DARK"
    assert header["OBSMODE"] == "dark"


def test_an_unknown_filter_is_written_as_zero_not_left_blank(tmp_path):
    """What ``JapanCamera.filter_number`` produces for an unconfirmed move."""
    header = write_frame(tmp_path / "frame.fits", filter_num=0)
    assert header["FILTER"] == 0


def test_site_coordinates_keep_their_full_precision(tmp_path):
    """The ASI writer rounds them for its legacy cards; nothing rounds here."""
    header = write_frame(tmp_path / "frame.fits")
    assert header["SITELAT"] == pytest.approx(53.324236)
    assert header["SITELON"] == pytest.approx(107.741264)


def test_the_pixels_survive_as_sixteen_bit_unsigned(tmp_path):
    path = tmp_path / "frame.fits"
    data = np.arange(16, dtype="<u2").reshape(4, 4) * 4000
    write_fits(
        path, data, timestamp=EVENING_LOCAL, exposure_sec=30.0, binning=1,
        readout_speed=2, filter_num=3, ccd_temp=-10.0, image_type="LIGHT",
        obs_mode="sun", camera_vendor="v", camera_model="m", camera_sn="s",
        camera_version="c", driver_version="d", dcam_version="a",
        lat=0.0, lon=0.0, elevation=0.0,
    )
    read = fits.getdata(path)
    assert read.dtype.kind == "u" and read.dtype.itemsize == 2
    assert np.array_equal(read, data)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_writing_refuses_to_overwrite_an_existing_frame(tmp_path):
    path = tmp_path / "frame.fits"
    write_frame(path)
    with pytest.raises(OSError):
        write_frame(path)


def test_a_failed_capture_is_refused_rather_than_filed_as_an_empty_frame(tmp_path):
    """An empty frame in the archive is worse than a gap: it looks like data."""
    path = tmp_path / "none.fits"
    with pytest.raises(ValueError, match="image data is None"):
        write_fits(
            path, None, timestamp=EVENING_LOCAL, exposure_sec=30.0, binning=1,
            readout_speed=2, filter_num=3, ccd_temp=-10.0, image_type="LIGHT",
            obs_mode="sun", camera_vendor="v", camera_model="m", camera_sn="s",
            camera_version="c", driver_version="d", dcam_version="a",
            lat=0.0, lon=0.0, elevation=0.0,
        )
    assert not path.exists()
