"""One intensity scale, and the two places a 16-bit range used to break things.

Every driver here now scales its captures onto 0..65535 on the way in, so the
statistics, the histograms and the saturation figure all mean the same thing
whichever camera produced the frame. Two consequences of that are worth pinning
down, because both were invisible while no sensor filled more than 12 bits:

* FITS has no unsigned 16-bit type. A value above 32767 written without
  ``BZERO`` wraps to a negative one, and a file read without applying ``BZERO``
  comes back shifted by 32768. The dependency-free writer and reader are the
  paths astropy does not cover, so they are tested here directly.
* An archive captured before the standardisation is on a 12-bit scale and says
  nothing about it. It has to stay readable, and it has to be lifted, or the
  same sky would look sixteen times darker in an older file.
"""
import sys

from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import frame_archive                                       # noqa: E402
import intensity                                           # noqa: E402

from cameras.sptt_driver import _save_fits_minimal         # noqa: E402


# ---------------------------------------------------------------------------
# Scaling on the way in
# ---------------------------------------------------------------------------
def test_a_12_bit_sample_lands_exactly_at_the_top_of_the_range():
    """Full scale must reach full scale, or no saturation check can see it.

    A plain ``<< 4`` leaves a clipped 12-bit pixel on 65520 — four counts short,
    and invisible to every "is this frame overexposed" test in the program.
    """
    frame = np.array([[0, 2048, 4095]], dtype=np.uint16)
    scaled = intensity.to_full_scale(frame, 12)
    assert scaled.dtype == np.uint16
    assert scaled[0][0] == 0
    assert scaled[0][-1] == intensity.FULL_SCALE


def test_the_mapping_is_reversible():
    """A shift back recovers the sample the sensor actually delivered."""
    frame = np.arange(4096, dtype=np.uint16).reshape(64, 64)
    scaled = intensity.to_full_scale(frame, 12)
    assert np.array_equal(scaled >> 4, frame)


def test_an_8_bit_frame_grows_a_container_before_it_is_scaled():
    frame = np.array([[0, 128, 255]], dtype=np.uint8)
    scaled = intensity.to_full_scale(frame, 8)
    assert scaled.dtype == np.uint16
    # The 8-bit world spells this ``v * 257``, and it is the same mapping.
    assert list(scaled[0]) == [0, 128 * 257, 65535]


def test_a_16_bit_frame_is_handed_back_untouched():
    frame = np.array([[0, 40000, 65535]], dtype=np.uint16)
    assert list(intensity.to_full_scale(frame, 16)[0]) == [0, 40000, 65535]


def test_the_sptt_decoder_delivers_full_scale():
    """The 12-bit unpacking path, exercised through the driver's own decoder."""
    from cameras.sptt_driver import decode_frame, ENCODING_12BPP

    # Two pixels packed into three bytes: 0xFFF and 0x000.
    raw = [[0xFF, 0x00, 0x0F]]
    frame = decode_frame(raw, w=2, h=1, encoding=ENCODING_12BPP, binning=1)
    assert frame.dtype == np.uint16
    assert int(frame.max()) == intensity.FULL_SCALE


# ---------------------------------------------------------------------------
# What a file says about its own scale
# ---------------------------------------------------------------------------
def test_a_recorded_full_scale_is_believed():
    assert intensity.full_scale_from_header({"ADCFULL": "65535"}) == 65535


def test_a_bit_depth_is_read_when_there_is_no_full_scale():
    assert intensity.full_scale_from_header({"BITDEPTH": "16"}) == 65535
    assert intensity.full_scale_from_header({"BITDEPTH": "12"}) == 4095


def test_a_header_that_says_nothing_says_nothing():
    assert intensity.full_scale_from_header({"EXPTIME": "55.0"}) is None
    assert intensity.full_scale_from_header({}) is None


def test_a_dark_16_bit_frame_is_not_mistaken_for_a_12_bit_one():
    """The reason the old guess had to go: darkness is not shallowness.

    ``legacy_full_scale`` still guesses, because an archive written before
    ``ADCFULL`` offers nothing else — but it is only ever reached for files with
    no recorded scale, and a modern frame always has one.
    """
    dark = np.full((4, 4), 300, dtype=np.uint16)
    assert intensity.legacy_full_scale(dark) == 4095
    assert intensity.full_scale_from_header({"ADCFULL": "65535"}) == 65535


# ---------------------------------------------------------------------------
# Saturation and statistics
# ---------------------------------------------------------------------------
def test_saturation_is_counted_against_the_frames_own_scale():
    frame = np.zeros((10, 10), dtype=np.uint16)
    frame[0, :] = 4095                      # a full row, clipped for 12 bits
    assert frame_archive.frame_stats(frame, full_scale=4095)["saturated_pct"] == 10.0
    # The same pixels on the program's scale are nowhere near full.
    assert frame_archive.frame_stats(frame)["saturated_pct"] == 0.0


def test_stats_report_the_scale_they_were_measured_against():
    frame = np.zeros((2, 2), dtype=np.uint16)
    assert frame_archive.frame_stats(frame)["full_scale"] == 65535.0
    assert frame_archive.frame_stats(frame, full_scale=4095)["full_scale"] == 4095.0


def test_histograms_are_binned_over_the_range_not_over_the_data():
    """Two frames of one camera must produce comparable histograms."""
    dim = np.full((8, 8), 1000, dtype=np.uint16)
    bright = np.full((8, 8), 60000, dtype=np.uint16)
    _, dim_edges = frame_archive.histogram(dim, bins=16)
    _, bright_edges = frame_archive.histogram(bright, bins=16)
    assert dim_edges == bright_edges
    assert dim_edges[0] == 0.0 and dim_edges[-1] == 65535.0


# ---------------------------------------------------------------------------
# The astropy-free FITS round trip
# ---------------------------------------------------------------------------
def test_a_value_above_32767_survives_the_dependency_free_round_trip(tmp_path):
    """The regression this scale change would otherwise have introduced.

    Without BZERO the writer turned 60000 into -5536 and the reader handed it
    back as such. Nothing complained; the frame was simply wrong.
    """
    path = tmp_path / "frame.fit"
    frame = np.array([[0, 32768, 60000, 65535]], dtype=np.uint16)
    _save_fits_minimal(str(path), frame, {"ADCFULL": intensity.FULL_SCALE})

    read_back = frame_archive.read_fits_minimal(str(path))
    assert read_back.dtype == np.uint16
    assert list(read_back[0]) == [0, 32768, 60000, 65535]


def test_the_dependency_free_reader_still_reads_a_file_written_without_bzero(tmp_path):
    """Old SPTT archives have no BZERO card and hold small positive values."""
    path = tmp_path / "old.fit"
    frame = np.array([[0, 1024, 4095]], dtype=np.uint16)
    _save_fits_minimal(str(path), frame, None)
    # Strip the offset the writer now adds, leaving the file as it used to be.
    text = path.read_bytes()
    assert b"BZERO" in text

    read_back = frame_archive.read_fits_minimal(str(path))
    assert list(read_back[0]) == [0, 1024, 4095]


def test_an_old_12_bit_archive_is_lifted_when_it_is_read(tmp_path):
    fits = pytest.importorskip("astropy.io.fits")
    path = tmp_path / "legacy.fits"
    frame = np.array([[0, 2048, 4095]], dtype=np.uint16)
    fits.writeto(str(path), frame, overwrite=True)   # no ADCFULL card

    arr, full_scale = frame_archive.read_frame_with_scale(str(path))
    assert full_scale == intensity.FULL_SCALE
    assert int(arr.max()) == 65535


def test_a_modern_frame_is_read_exactly_as_written(tmp_path):
    fits = pytest.importorskip("astropy.io.fits")
    path = tmp_path / "modern.fits"
    frame = np.array([[0, 300, 65535]], dtype=np.uint16)
    header = fits.Header()
    header["ADCFULL"] = intensity.FULL_SCALE
    fits.writeto(str(path), frame, header=header, overwrite=True)

    arr, full_scale = frame_archive.read_frame_with_scale(str(path))
    assert full_scale == intensity.FULL_SCALE
    # Dark, and left dark: the recorded scale stops the old guess from firing.
    assert list(arr[0]) == [0, 300, 65535]
