"""SPTT driver faults found by reading it against the vendor's SPTT-CAM code.

Each test pins one of them: a float cooling set-point that stopped the camera
from being configured, a frame size taken from a table instead of the
firmware, focus frames that ran into the scheduled capture, a setup mode that
never reached the console worker, local time in DATE-OBS, a worker that gave
up without the process saying so, and a setting change from another thread
that could land in the middle of a frame read.
"""
import sys

from datetime import datetime as dt, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_sptt_exposure import FakeFirmware, make_camera, written  # noqa: E402


# ---------------------------------------------------------------------------
# Cooling set-point
# ---------------------------------------------------------------------------
def test_a_float_set_point_from_setup_app_reaches_the_camera():
    from cameras.sptt_driver import CMD_SET_TARGET_TEMP

    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.configure(exposure=0.01, target_temp=-10.0)     # what setup_app saves
    assert written(fw, CMD_SET_TARGET_TEMP) == [(-10) & 0xFF]


def test_a_set_point_outside_the_peltier_range_is_refused_before_anything_is_sent():
    fw = FakeFirmware()
    cam = make_camera(fw)
    with pytest.raises(ValueError):
        cam.configure(exposure=0.01, target_temp=-60.0)
    assert not fw.commands


# ---------------------------------------------------------------------------
# Frame size
# ---------------------------------------------------------------------------
def test_the_frame_size_is_the_one_the_firmware_reports():
    from cameras.sptt_driver import CMD_READ_PREPARE, ENCODING_12BPP

    fw = FakeFirmware()
    fw.w, fw.h = 186, 144               # 4x4 binning; the old table said 188
    cam = make_camera(fw)
    cam.configure(exposure=0.01, binning=3, encoding=ENCODING_12BPP)
    cam.start()

    frame = cam.grab_frame()

    assert frame.shape == (144, 186)
    assert written(fw, CMD_READ_PREPARE)[-1] == 186 * 144 * 3 // 2


# ---------------------------------------------------------------------------
# Decoding: numpy must give exactly what the vendor's loops gave
# ---------------------------------------------------------------------------
def _vendor_decode(raw_chunks, w, h, encoding, binning=0):
    """SPTT-CAM/capture.py decode_frame, before any scaling."""
    raw = []
    for buf in raw_chunks:
        raw.extend(buf)
    if encoding == 1:
        pixels = []
        for i in range(0, len(raw) - 2, 3):
            b0, b1, b2 = raw[i], raw[i + 1], raw[i + 2]
            pixels.append((b0 << 4) | (b2 & 0x0F))
            pixels.append((b1 << 4) | ((b2 >> 4) & 0x0F))
    else:
        pixels = raw
    if binning > 0:
        arr = np.array(pixels[:w * h], dtype=np.uint16)
        if len(arr) < w * h:
            arr = np.pad(arr, (0, w * h - len(arr)))
        return arr.reshape(h, w)
    frame = [0] * (w * h)
    for i in range(h):
        src_row = i // 2 if i % 2 == 0 else h // 2 + i // 2
        for j in range(w):
            idx = src_row * w + j
            if idx < len(pixels):
                frame[i * w + j] = pixels[idx]
    return np.array(frame, dtype=np.uint16).reshape(h, w)


@pytest.mark.parametrize("encoding", [0, 1])
@pytest.mark.parametrize("binning", [0, 1])
@pytest.mark.parametrize("w,h,short", [(40, 30, 0), (37, 21, 0), (40, 30, 700)])
def test_decoding_matches_the_vendor(encoding, binning, w, h, short):
    import intensity
    from cameras.sptt_driver import decode_frame

    rng = np.random.default_rng(w * h + encoding + binning + short)
    size = (w * h * 3 // 2 if encoding else w * h) - short
    data = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
    chunks = [data[i:i + 512] for i in range(0, len(data), 512)]

    expected = intensity.to_full_scale(
        _vendor_decode(chunks, w, h, encoding, binning), 12 if encoding else 8)
    assert np.array_equal(decode_frame(chunks, w, h, encoding, binning), expected)


# ---------------------------------------------------------------------------
# Focus frames and the schedule
# ---------------------------------------------------------------------------
def test_a_focus_frame_is_not_started_when_it_would_run_into_a_capture():
    from cameras.sptt_driver import focus_frame_fits

    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.configure(exposure=0.88)

    def at(second, micro=0):
        return dt(2026, 10, 28, 12, 0, second, micro)

    assert focus_frame_fits(at(10), [0, 30], cam)
    assert not focus_frame_fits(at(59, 300_000), [0, 30], cam)   # the old miss
    assert not focus_frame_fits(at(28), [0, 30], cam)
    assert not focus_frame_fits(at(30), [0, 30], cam)            # the tick itself


def test_seconds_to_next_capture_wraps_the_minute():
    from cameras.sptt_driver import seconds_to_next_capture

    assert seconds_to_next_capture(dt(2026, 1, 1, 0, 0, 45), [0, 30]) == 15
    assert seconds_to_next_capture(dt(2026, 1, 1, 0, 0, 10), [0, 30]) == 20


# ---------------------------------------------------------------------------
# Console entry point
# ---------------------------------------------------------------------------
def _console_entry_source():
    """The text of _run_console_sptt, read from the file.

    Not inspect.getsource: other tests swap module functions for stubs, and
    then that returns the stub's source instead.
    """
    text = (Path(__file__).resolve().parent.parent / "cameras" /
            "sptt_driver.py").read_text(encoding="utf-8")
    return text[text.index("def _run_console_sptt("):]


def test_setup_mode_reaches_the_console_worker():
    """ASI and Japan pass it; SPTT archived frames during a focus session."""
    source = _console_entry_source()
    call = source[source.index("SpttWorkerConsole("):]
    call = call[:call.index(")")]
    assert "setup_mode=setup_mode" in call


def test_preview_loads_firmware_from_the_configured_directory():
    source = _console_entry_source()
    assert "ensure_firmware_loaded(backend)" not in source


def test_a_worker_that_cannot_start_the_camera_reports_failure(tmp_path):
    from cameras.sptt_driver import SpttWorkerConsole

    from cameras.sptt_load_firmware import CSDU429

    class DeadCamera:
        model = CSDU429
        port = None
        exposure, gain, binning, encoding = 0.88, 100, 0, 1
        w = h = 0
        period_us = min_period_us = trigmode = 0

        def start(self):
            raise RuntimeError("no camera")

        def stop(self):
            pass

        def get_status_info(self):
            return {}

    worker = SpttWorkerConsole(DeadCamera(), str(tmp_path), "SPTT-test",
                               str(tmp_path / "status"))
    worker.run()
    assert worker.failed


# ---------------------------------------------------------------------------
# Header time
# ---------------------------------------------------------------------------
def test_date_obs_is_utc():
    from cameras.sptt_driver import frame_metadata

    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.configure(exposure=0.01)
    cam.last_frame_started = dt(2026, 10, 28, 3, 4, 5, tzinfo=timezone.utc)

    metadata = frame_metadata(cam, dt.now())
    assert metadata["DATE-OBS"] == "2026-10-28T03:04:05"
    assert metadata["TIMESYS"] == "UTC"
    assert "DATE-LOC" in metadata


# ---------------------------------------------------------------------------
# Another thread changing a setting mid-wait
# ---------------------------------------------------------------------------
def test_a_change_made_while_waiting_for_a_frame_is_not_read_under_the_old_one():
    from cameras.sptt_driver import CMD_READ_PREPARE, CMD_SET_GAIN, ENCODING_8BPP

    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.configure(exposure=0.01, encoding=ENCODING_8BPP)
    cam.start()
    fw.commands.clear()

    real_wait = cam._wait_for_fifo
    calls = []

    def wait_with_interruption(budget):
        if not calls:
            cam.set_gain(200)           # the GUI thread, as the wait runs
        calls.append(budget)
        return real_wait(budget)

    cam._wait_for_fifo = wait_with_interruption
    cam.grab_frame()

    ids = [cid for cid, _ in fw.commands]
    assert ids.index(CMD_SET_GAIN) < ids.index(CMD_READ_PREPARE)
    # The frame integrating across the change was dropped, the next one kept.
    assert len(written(fw, CMD_READ_PREPARE)) == 2
