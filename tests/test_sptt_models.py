"""The CSDU family: CSDU-429/423 (grey, interlaced) and CSDU-285C (colour).

The driver was written for the CSDU-429 only. The vendor's own distribution
(SPTT-CAM/samples) also ships the CSDU-285C: another USB ID, another firmware
script, a progressive-scan sensor and a Bayer mosaic. These tests pin what
can be checked without the camera.
"""
import sys

from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cameras import sptt_load_firmware as loader            # noqa: E402
from cameras.sptt_load_firmware import CSDU285C, CSDU429    # noqa: E402
from cameras.sptt_spt import extract_firmware, parse_spt    # noqa: E402
from cameras import sptt_driver                             # noqa: E402
from cameras.sptt_driver import (                           # noqa: E402
    ENCODING_8BPP, SpttCamera, _save_fits_minimal, decode_frame, debayer,
    frame_metadata, is_mosaic, bayer_pattern_setting, colour_offsets,
)

FIRMWARE = ROOT / "firmware"
SPT_429 = ROOT / "SPTT-CAM" / "C2-429.spt"


# ---------------------------------------------------------------------------
# Firmware scripts
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not SPT_429.is_file(), reason="SPTT-CAM submodule not checked out")
def test_the_429_script_gives_the_blobs_from_the_usb_capture():
    """The parser is right if it reproduces what was captured off the wire."""
    fx2_blocks, fpga = extract_firmware(SPT_429.read_bytes())
    assert [(a, len(b)) for a, b in fx2_blocks] == loader.FX2_BLOCKS
    assert b"".join(b for _, b in fx2_blocks) == (FIRMWARE / "fx2_firmware.bin").read_bytes()
    assert fpga == (FIRMWARE / "fpga_bitstream.bin").read_bytes()


def test_the_285c_firmware_ships_and_differs_from_the_429s():
    fx2_blocks, fpga = loader.load_firmware_files(model=CSDU285C)
    assert [a for a, _ in fx2_blocks] == [0x0000, 0x0FE0]
    assert sum(len(b) for _, b in fx2_blocks) == 6746
    assert len(fpga) == 54908
    assert fpga != (FIRMWARE / "fpga_bitstream.bin").read_bytes()


def test_cpucs_writes_are_left_to_the_loader():
    """The script holds/releases the 8051 itself; those are not firmware."""
    data = (FIRMWARE / CSDU285C.spt_name).read_bytes()
    cpucs = [r for r in parse_spt(data) if r.request == 0xA0 and r.value == 0xE600]
    assert [r.data for r in cpucs] == [b"\x01", b"\x00"]
    fx2_blocks, _ = extract_firmware(data)
    assert all(addr != 0xE600 for addr, _ in fx2_blocks)


def test_a_damaged_script_is_refused():
    with pytest.raises(ValueError):
        parse_spt(b"XXXX" + b"\x00" * 40)


def test_a_missing_285c_script_names_the_file(tmp_path):
    with pytest.raises(FileNotFoundError) as excinfo:
        loader.load_firmware_files(str(tmp_path), CSDU285C)
    assert "csdu285.spt" in str(excinfo.value)


def test_the_429_still_loads_from_the_old_blobs_without_a_script(tmp_path):
    (tmp_path / "fx2_firmware.bin").write_bytes(b"\x01" * 6936)
    (tmp_path / "fpga_bitstream.bin").write_bytes(b"\x02" * 100)
    fx2_blocks, fpga = loader.load_firmware_files(str(tmp_path), CSDU429)
    assert [(a, len(b)) for a, b in fx2_blocks] == loader.FX2_BLOCKS
    assert fpga == b"\x02" * 100


# ---------------------------------------------------------------------------
# Model detection and choosing one of several cameras
# ---------------------------------------------------------------------------
class FakeDev:
    def __init__(self, pid, bus, ports):
        self.idProduct, self.bus, self.port_numbers = pid, bus, ports
        self.address = 9


@pytest.fixture
def bus(monkeypatch):
    """A fake USB bus: append FakeDevs to the returned list."""
    devices = []

    def find(find_all=False, idVendor=None, idProduct=None, backend=None,
             custom_match=None):
        assert idVendor == loader.VID
        hits = [d for d in devices if d.idProduct == idProduct
                and (custom_match is None or custom_match(d))]
        return hits if find_all else (hits[0] if hits else None)
    monkeypatch.setattr(loader.usb.core, "find", find)
    monkeypatch.setattr(loader.usb.util, "dispose_resources", lambda dev: None)
    return devices


@pytest.mark.parametrize("pid, model, configured", [
    (0xE429, CSDU429, True), (0xF429, CSDU429, False),
    (0xC285, CSDU285C, True), (0xD285, CSDU285C, False),
])
def test_the_model_is_told_from_the_usb_id(bus, pid, model, configured):
    bus.append(FakeDev(pid, 1, (2,)))
    assert loader.select_camera(None) == loader.CameraLocation(model, "1-2", configured)


def test_the_port_is_named_as_linux_names_it():
    assert loader.usb_port(FakeDev(0xE429, 3, (1, 4, 2))) == "3-1.4.2"


def test_no_camera_is_an_error_not_a_guess(bus):
    with pytest.raises(LookupError, match="No SPTT camera"):
        loader.select_camera(None)


def test_two_cameras_and_no_port_refuse_to_pick_one(bus):
    """"The first" could be either after a reboot, or the same for two runs."""
    bus.extend([FakeDev(0xE429, 1, (2,)), FakeDev(0xD285, 1, (3,))])
    with pytest.raises(LookupError) as excinfo:
        loader.select_camera(None)
    message = str(excinfo.value)
    assert "1-2" in message and "1-3" in message and "usb_port" in message


def test_the_configured_port_picks_its_camera(bus):
    bus.extend([FakeDev(0xE429, 1, (2,)), FakeDev(0xE429, 1, (3,))])
    assert loader.select_camera(None, "1-3").port == "1-3"
    with pytest.raises(LookupError, match="No SPTT camera on USB port 1-4"):
        loader.select_camera(None, "1-4")


def test_the_firmware_wait_stays_on_its_own_port(bus, monkeypatch):
    """After a firmware load, another camera already up must not be taken
    for the one that is re-enumerating."""
    monkeypatch.setattr(loader.time, "sleep", lambda s: None)
    bus.append(FakeDev(0xE429, 1, (2,)))           # the other camera, up
    assert loader.wait_for_configured_device(None, timeout=0.01, port="1-3") is None
    mine = FakeDev(0xE429, 1, (3,))
    bus.append(mine)
    assert loader.wait_for_configured_device(None, timeout=1, port="1-3") is mine


def test_each_camera_object_opens_its_own_device(bus):
    other, mine = FakeDev(0xE429, 1, (2,)), FakeDev(0xE429, 1, (3,))
    bus.extend([other, mine])
    assert sptt_driver.find_device(None, 0xE429, "1-3") is mine
    cam = SpttCamera(None, CSDU429, port="1-3")
    assert cam.port == "1-3"


def test_the_usb_port_setting():
    from cameras.sptt_driver import usb_port_setting
    assert usb_port_setting({}) is None
    assert usb_port_setting({"usb_port": "  "}) is None
    assert usb_port_setting({"usb_port": " 1-2.3 "}) == "1-2.3"


@pytest.mark.parametrize("value, expected", [
    ("sptt", "sptt"), ("sptt-north", "sptt"), ("asi", "asi")])
def test_a_labelled_type_is_its_camera_type(value, expected):
    """every-camera@sptt-north passes "sptt-north" as --type."""
    import main
    assert main.camera_type(value) == expected


def test_an_unknown_type_is_still_refused():
    import argparse
    import main
    with pytest.raises(argparse.ArgumentTypeError):
        main.camera_type("spt-north")


# ---------------------------------------------------------------------------
# Frame decoding
# ---------------------------------------------------------------------------
def _rows_frame(w, h):
    """8-bit raw frame whose every pixel holds its row number."""
    return [bytes(np.repeat(np.arange(h, dtype=np.uint8), w))]


def test_the_429_frame_is_woven_from_two_fields():
    frame = decode_frame(_rows_frame(4, 6), 4, 6, ENCODING_8BPP, 0, interlaced=True)
    # Raw rows 0,1,2 are the even field, 3,4,5 the odd one.
    assert list(frame[:, 0] >> 8) == [0, 3, 1, 4, 2, 5]


def test_the_285c_frame_is_read_as_it_comes():
    """Weaving a progressive frame would interleave its top and bottom halves."""
    frame = decode_frame(_rows_frame(4, 6), 4, 6, ENCODING_8BPP, 0, interlaced=False)
    assert list(frame[:, 0] >> 8) == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("model", [CSDU429, CSDU285C])
def test_the_camera_tells_decode_frame_which_sensor_it_has(monkeypatch, model):
    monkeypatch.setattr(sptt_driver, "_usb_write_retry", lambda *a, **k: None)
    monkeypatch.setattr(sptt_driver, "read_raw_frame",
                        lambda size, ep: [b"\x00" * size])
    cam = SpttCamera(backend=None, model=model)
    cam.w, cam.h = 4, 2
    assert cam._read_one_frame()[-1] is model.interlaced


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------
CELL = {"R": 1000, "G": 2000, "B": 3000}


def _flat_mosaic(pattern, h=8, w=10):
    """The mosaic a uniformly coloured scene leaves under ``pattern``."""
    m = np.empty((h, w), dtype=np.uint16)
    for (dy, dx), c in zip(((0, 0), (0, 1), (1, 0), (1, 1)), pattern):
        m[dy::2, dx::2] = CELL[c]
    return m


@pytest.mark.parametrize("pattern", ["RGGB", "GRBG", "GBRG", "BGGR"])
def test_debayer_recovers_a_flat_colour_under_every_layout(pattern):
    cube = debayer(_flat_mosaic(pattern), pattern)
    assert cube.shape == (3, 8, 10) and cube.dtype == np.uint16
    for i, c in enumerate("RGB"):
        assert np.all(cube[i] == CELL[c]), c


def test_the_wrong_layout_swaps_the_colours():
    """What the operator would see on site, and what sptt.bayer_pattern fixes."""
    cube = debayer(_flat_mosaic("RGGB"), "BGGR")
    assert cube[0, 4, 4] == CELL["B"] and cube[2, 4, 4] == CELL["R"]


def test_an_unknown_layout_is_refused():
    with pytest.raises(ValueError):
        debayer(_flat_mosaic("RGGB"), "RGBG")
    with pytest.raises(ValueError):
        bayer_pattern_setting({"bayer_pattern": "rgb"})
    assert bayer_pattern_setting({"bayer_pattern": "grbg"}) == "GRBG"
    assert bayer_pattern_setting({}) == "RGGB"


def test_only_an_unbinned_colour_frame_is_a_mosaic():
    assert is_mosaic(CSDU285C, 0)
    assert not is_mosaic(CSDU285C, 1)
    assert not is_mosaic(CSDU429, 0)


def test_focus_and_live_view_get_colour():
    """Setup mode, focus_app and the LAN view take colour as (H, W, 3)."""
    cam = SpttCamera(None, CSDU285C)
    cam.binning = 0
    shown = cam.for_display(_flat_mosaic("RGGB"))
    assert shown.shape == (8, 10, 3)
    assert [int(v) for v in shown[4, 4]] == [CELL["R"], CELL["G"], CELL["B"]]


def test_the_focus_measures_and_the_stream_take_the_colour_frame():
    import frame_archive
    cam = SpttCamera(None, CSDU285C)
    shown = cam.for_display(_flat_mosaic("RGGB"))
    assert frame_archive.to_jpeg_bytes(shown)[:2] == bytes([0xFF, 0xD8])
    assert frame_archive.sharpness(shown) == 0.0          # flat colour, no detail
    assert frame_archive.frame_stats(shown)["shape"] == [8, 10, 3]


def test_grey_and_binned_frames_are_shown_as_they_are():
    frame = np.arange(6, dtype=np.uint16).reshape(2, 3)
    assert SpttCamera(None, CSDU429).for_display(frame) is frame
    binned = SpttCamera(None, CSDU285C)
    binned.binning = 1
    assert binned.for_display(frame) is frame


def test_colour_offsets_are_sent_only_when_configured():
    assert colour_offsets({}) == {"r_offset": None, "g_offset": None}
    assert colour_offsets({"r_offset": 0, "g_offset": 30.0}) == \
        {"r_offset": 0, "g_offset": 30}


# ---------------------------------------------------------------------------
# FITS
# ---------------------------------------------------------------------------
def _cam(model, binning=0):
    cam = SpttCamera(backend=None, model=model)
    cam.binning = binning
    return cam


def test_the_header_names_the_actual_camera():
    status = {"exposure_s": 0.5}
    assert frame_metadata(_cam(CSDU429), sptt_driver.dt.now(), status)["INSTRUME"] == "CSDU-429"
    meta = frame_metadata(_cam(CSDU285C), sptt_driver.dt.now(), status)
    assert meta["INSTRUME"] == "CSDU-285C"


def test_a_raw_colour_frame_carries_its_bayer_layout():
    cam = _cam(CSDU285C)
    cam.bayer_pattern = "GRBG"
    meta = frame_metadata(cam, sptt_driver.dt.now(), {"exposure_s": 0.5})
    assert meta["BAYERPAT"] == "GRBG"
    assert meta["COLORTYP"] == "BAYER"
    assert all(len(k) <= 8 for k in meta)


def test_a_demosaiced_frame_does_not_invite_a_second_demosaic():
    meta = frame_metadata(_cam(CSDU285C), sptt_driver.dt.now(),
                          {"exposure_s": 0.5}, debayered=True)
    assert meta["COLORTYP"] == "RGB"
    assert "BAYERPAT" not in meta


def test_binned_or_grey_frames_have_no_colour_keys():
    for cam in (_cam(CSDU285C, binning=1), _cam(CSDU429)):
        meta = frame_metadata(cam, sptt_driver.dt.now(), {"exposure_s": 0.5})
        assert "BAYERPAT" not in meta and "COLORTYP" not in meta


def test_the_minimal_writer_stores_an_rgb_cube(tmp_path):
    cube = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5) * 1000
    path = tmp_path / "rgb.fit"
    _save_fits_minimal(str(path), cube, {"COLORTYP": "RGB"})
    raw = path.read_bytes()
    header = raw[:2880].decode("ascii")
    cards = {header[i:i + 8].strip(): header[i + 10:i + 30].strip()
             for i in range(0, 2880, 80)}
    assert (cards["NAXIS"], cards["NAXIS1"], cards["NAXIS2"], cards["NAXIS3"]) == \
        ("3", "5", "4", "3")
    data = np.frombuffer(raw[2880:2880 + cube.size * 2], dtype=">i2")
    assert np.array_equal((data.astype(np.int32) + 32768).reshape(cube.shape), cube)


def test_astropy_reads_the_cube_back(tmp_path):
    fits = pytest.importorskip("astropy.io.fits")
    from cameras.sptt_driver import save_fits
    cube = np.random.default_rng(1).integers(0, 65535, (3, 4, 5), dtype=np.uint16)
    path = tmp_path / "rgb.fit"
    save_fits(str(path), cube, {"COLORTYP": "RGB"})
    with fits.open(path) as hdul:
        assert np.array_equal(hdul[0].data, cube)
        assert hdul[0].header["COLORTYP"] == "RGB"


@pytest.mark.parametrize("writer", ["astropy", "minimal"])
def test_the_archive_shows_a_saved_rgb_frame_in_colour(tmp_path, writer):
    """The cube is (3, H, W) on disk; the viewer and the LAN archive take
    colour as (H, W, 3) and used to refuse the file outright."""
    import frame_archive
    from cameras.sptt_driver import save_fits
    cube = debayer(_flat_mosaic("RGGB"), "RGGB")
    path = tmp_path / "rgb.fit"
    if writer == "astropy":
        pytest.importorskip("astropy")
        save_fits(str(path), cube, {"COLORTYP": "RGB", "ADCFULL": 65535})
    else:
        _save_fits_minimal(str(path), cube, {"COLORTYP": "RGB", "ADCFULL": 65535})
    arr, _ = frame_archive.read_frame_with_scale(str(path))
    assert arr.shape == (8, 10, 3)
    assert [int(arr[4, 4, i]) for i in range(3)] == [CELL["R"], CELL["G"], CELL["B"]]
    assert frame_archive.read_fits_minimal(str(path)).shape == (3, 8, 10)
    assert frame_archive.to_jpeg_bytes(arr)[:2] == b"\xff\xd8"


# ---------------------------------------------------------------------------
# Running without root
# ---------------------------------------------------------------------------
def test_the_udev_rules_cover_every_id_of_every_model():
    """The old README rule named Cypress's 04b4, which no CSDU camera uses."""
    import re
    rules = (ROOT / loader.UDEV_RULES).read_text(encoding="utf-8")
    pairs = set(re.findall(r'idVendor\}=="([0-9a-f]{4})".*idProduct\}=="([0-9a-f]{4})"', rules))
    for model in loader.MODELS:
        for pid in (model.pid_raw, model.pid_configured):
            assert (f"{loader.VID:04x}", f"{pid:04x}") in pairs, model.name


def test_a_permission_error_says_how_to_fix_it():
    import usb.core
    message = loader.explain_usb_error(
        usb.core.USBError("Access denied (insufficient permissions)", errno=13))
    assert loader.UDEV_RULES in message and "udevadm" in message
    other = usb.core.USBError("Pipe error", errno=32)
    assert loader.explain_usb_error(other) == str(other)
