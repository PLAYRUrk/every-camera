"""What ``/api/stats`` says about the frame being taken right now.

The archive branch of that endpoint has always been able to describe a frame in
full: it opens the file and reads the FITS header out of it. The live branch had
only what the driver thought to publish alongside the pixels — an image type, a
filter, an exposure — so the viewer's info panel, which is built out of that
header, went blank the moment an observer ticked "Follow live". It was describing
the frame it could see least about.

The driver knows the answer, because it has just written the file. It now hands
over the path, and the live branch reads the same header out of the same function
the archive branch uses. What must not happen is that path going out with the
reply: it is a fact about the camera's disk, not about the frame.

No camera hardware is involved — a CameraService is published to directly.
"""
import json
import socket
import sys
import urllib.request

from datetime import datetime as dt
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import frame_server                                    # noqa: E402

from camera_service import CameraService               # noqa: E402
from cameras.japan.fits import write_fits              # noqa: E402


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def archive(tmp_path):
    """A written frame, and the pixels that went into it."""
    image = (np.arange(32 * 32, dtype=np.uint16) % 4096).reshape(32, 32)
    path = tmp_path / "20260731T203000_3.fits"
    write_fits(path, image, timestamp=dt(2026, 7, 31, 20, 30), exposure_sec=55.0,
               binning=1, readout_speed=2, filter_num=3, ccd_temp=-12.5,
               image_type="LIGHT", obs_mode="time", camera_vendor="Hamamatsu",
               camera_model="ORCA", camera_sn="SN1", camera_version="1",
               driver_version="1", dcam_version="4",
               lat=53.3, lon=107.7, elevation=515)
    return tmp_path, path, image


def _live_stats(out_dir, meta, image):
    """Publish one live frame and ask the server to describe it."""
    service = CameraService("japan", "test", str(out_dir))
    service.publish_frame(image, dt(2026, 7, 31, 20, 30), meta)
    server = frame_server.start_frame_server(
        {"enabled": True, "bind": "127.0.0.1", "port": _free_port(),
         "discovery": False}, service)
    assert server is not None
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{server.port}/api/stats", timeout=10) as response:
            return json.load(response)
    finally:
        server.stop()


def test_a_live_frame_is_described_by_the_file_it_was_written_to(archive):
    out_dir, path, image = archive
    payload = _live_stats(out_dir, {"image_type": "LIGHT", "filter": "3",
                                    "exposure": 55.0, "name": path.name,
                                    "path": str(path)}, image)

    # Header cards arrive as the strings JSON carries them in, which is what the
    # archive branch has always sent and what the viewer's table renders.
    header = payload["metadata"]["fits_header"]
    assert float(header["EXPTIME"]) == pytest.approx(55.0)
    assert str(header["FILTER"]) == "3"
    assert float(header["CCD-TEMP"]) == pytest.approx(-12.5)
    # The file's own facts come over too, so the panel reads the same for a
    # followed frame as for that frame opened from the archive a minute later.
    assert payload["metadata"]["size"] > 0
    assert payload["metadata"]["mtime"] > 0


def test_the_path_of_the_file_never_leaves_the_camera(archive):
    out_dir, path, image = archive
    payload = _live_stats(out_dir, {"name": path.name, "path": str(path)}, image)
    assert "path" not in payload["metadata"]
    assert "path" not in json.dumps(payload)
    # The name does go out: it is what names the frame in the viewer.
    assert payload["metadata"]["name"] == path.name


def test_what_the_driver_published_wins_over_the_file(archive):
    """The live facts describe the frame in hand; the file is only consulted."""
    out_dir, path, image = archive
    payload = _live_stats(out_dir, {"image_type": "ON-DEMAND", "name": path.name,
                                    "path": str(path)}, image)
    assert payload["metadata"]["image_type"] == "ON-DEMAND"


def test_a_frame_with_no_file_behind_it_still_answers(archive):
    """Setup mode writes nothing, and a live frame must still have statistics."""
    out_dir, _path, image = archive
    payload = _live_stats(out_dir, {"image_type": "LIGHT"}, image)
    assert "fits_header" not in payload["metadata"]
    assert payload["stats"]["max"] == pytest.approx(float(image.max()))


def test_a_path_outside_the_archive_is_not_read(archive, tmp_path):
    """Whatever that file is, it is not this camera's frame."""
    out_dir, path, image = archive
    elsewhere = tmp_path.parent / "not-the-archive.fits"
    elsewhere.write_bytes(path.read_bytes())
    try:
        payload = _live_stats(out_dir, {"path": str(elsewhere)}, image)
        assert "fits_header" not in payload["metadata"]
        assert "path" not in payload["metadata"]
    finally:
        elsewhere.unlink()
