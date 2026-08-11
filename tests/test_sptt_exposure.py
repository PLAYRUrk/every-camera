"""The exposure the SPTT camera is told to use, and the period that lets it run.

The CSDU-429 takes the exposure and the frame period as two separate registers,
and in continuous mode a period shorter than the exposure does not slow the
camera down — it truncates the integration. This driver sent a fixed 75 000 us
period whatever the exposure was, so the default 0.88 s exposure was being cut
to 75 ms and every frame came out about twelve times too dark, with the request
faithfully recorded in the header and nothing anywhere reporting a problem.

These tests pin the three halves of the fix: the period is derived from the
exposure and follows it when it changes, what the camera reports back is
checked instead of assumed, and the frame already in flight when a setting
changes is thrown away rather than handed out as the new one.

``cameras.sptt_driver`` imports pyusb, which loads fine with no libusb behind
it, so only the three endpoints are faked here — none of this needs a camera.
"""
import struct
import sys

from datetime import datetime as dt
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.sptt_timing import (          # noqa: E402
    CMD_VALUE_MAX, EXPOSURE_MIN_US, LEGACY_PERIOD_US, PERIOD_READOUT_MARGIN_US,
    derive_period_us, exposure_mismatch, exposure_to_us, period_mismatch,
)


# ---------------------------------------------------------------------------
# The arithmetic, on its own
# ---------------------------------------------------------------------------
def test_the_period_leaves_room_for_the_whole_exposure():
    assert derive_period_us(0.88) == 880_000 + PERIOD_READOUT_MARGIN_US


def test_the_default_period_used_to_be_shorter_than_the_default_exposure():
    """The bug itself: 75 ms of period for 880 ms of exposure."""
    assert LEGACY_PERIOD_US < exposure_to_us(0.88)
    assert derive_period_us(0.88) > LEGACY_PERIOD_US
    assert derive_period_us(0.88) >= exposure_to_us(0.88)


def test_a_configured_period_is_honoured():
    assert derive_period_us(0.1, override=2_000_000) == 2_000_000


def test_a_configured_period_that_would_cut_the_exposure_is_raised():
    said = []
    period = derive_period_us(0.88, override=50_000, warn=said.append)
    assert period == 880_000 + PERIOD_READOUT_MARGIN_US
    assert said and "50000" in said[0] and "880000" in said[0]


def test_a_short_period_is_left_alone_outside_continuous_mode():
    said = []
    period = derive_period_us(0.88, override=50_000, allow_short=True,
                              warn=said.append)
    assert period == 50_000
    assert not said


def test_the_firmware_minimum_period_wins():
    assert derive_period_us(0.1, min_period_us=2_000_000) == 2_000_000


def test_a_period_too_wide_for_the_wire_is_clamped_not_wrapped():
    said = []
    # Wrapping would hand the camera a few milliseconds of period, which is
    # this bug at its worst — a period far shorter than the exposure.
    assert derive_period_us(1.0, override=CMD_VALUE_MAX + 10,
                            warn=said.append) == CMD_VALUE_MAX
    assert said
    assert derive_period_us(1.0, min_period_us=1 << 33) == CMD_VALUE_MAX


def test_make_command_refuses_a_value_that_would_wrap():
    from cameras.sptt_driver import CMD_SET_PERIOD, make_command

    assert len(make_command(CMD_SET_PERIOD, CMD_VALUE_MAX)) == 5
    with pytest.raises(ValueError):
        make_command(CMD_SET_PERIOD, 1 << 32)


def test_microseconds_are_rounded_not_truncated():
    # int(0.29 * 1_000_000) is 289999.
    assert exposure_to_us(0.29) == 290_000


def test_an_exposure_below_the_sensor_minimum_is_floored():
    assert exposure_to_us(1e-9) == EXPOSURE_MIN_US


def test_a_shortened_exposure_is_a_mismatch():
    assert exposure_mismatch(880_000, 75_000)


def test_the_firmware_clock_granularity_is_not_a_mismatch():
    assert exposure_mismatch(880_000, 879_994) is None


def test_a_period_shorter_than_the_exposure_is_a_mismatch():
    assert period_mismatch(880_000, 75_000)
    assert period_mismatch(880_000, 955_000) is None


# ---------------------------------------------------------------------------
# A fake CSDU-429
# ---------------------------------------------------------------------------
class FakeFirmware:
    """Just enough camera to answer commands and hand out numbered frames."""

    #: The layout read_crb unpacks out of the 64-byte status buffer.
    FORMAT = '=BBHHHIIBHHHHBIIHIHHBBIHBBB'

    def __init__(self, max_exposure_us=None, fixed_period_us=None,
                 min_period_us=0, frames=8):
        self.exposure_us = 0
        self.period_us = 0
        self.min_period_us = min_period_us
        self.gain = 0
        self.binning = 0
        self.encoding = 0
        self.trigmode = 0
        self.w, self.h = 744, 576
        # A firmware that refuses what it is asked for, so the checks have
        # something to catch.
        self.max_exposure_us = max_exposure_us
        self.fixed_period_us = fixed_period_us
        self.running = False
        self.commands = []          # (cmd_id, value), in order
        self.fifo = list(range(1, frames + 1))
        self.frames_read = []       # the tags of the frames handed out
        self.pending = b""          # the frame being streamed out, in chunks

    # --- the endpoints ---------------------------------------------------
    @property
    def ep_wr(self):
        return _FakeWriteEndpoint(self)

    @property
    def ep_rd(self):
        return _FakeStatusEndpoint(self)

    @property
    def ep_tr(self):
        return _FakeTransferEndpoint(self)

    # --- behaviour -------------------------------------------------------
    def command(self, cmd_id, value):
        from cameras import sptt_driver as d

        self.commands.append((cmd_id, value))
        if cmd_id == d.CMD_SET_EXP:
            self.exposure_us = (min(value, self.max_exposure_us)
                                if self.max_exposure_us else value)
        elif cmd_id == d.CMD_SET_PERIOD:
            self.period_us = self.fixed_period_us or value
        elif cmd_id == d.CMD_SET_GAIN:
            self.gain = value
        elif cmd_id == d.CMD_SET_BINNING:
            self.binning = value
        elif cmd_id == d.CMD_SET_ENCODING:
            self.encoding = value
        elif cmd_id == d.CMD_SET_TRIGMODE:
            self.trigmode = value
        elif cmd_id == d.CMD_CAM_START:
            self.running = True
        elif cmd_id == d.CMD_CAM_STOP:
            self.running = False
        elif cmd_id == d.CMD_FIFO_INIT:
            # Everything queued is dropped; the camera starts a new frame.
            self.fifo = [max(self.frames_read + self.fifo + [0]) + 1]
            self.pending = b""
        elif cmd_id == d.CMD_READ_PREPARE:
            # One whole frame is now on its way out, however many 512-byte
            # chunks the driver takes to collect it.
            self.pending = self.take_frame(value)

    def status_bytes(self):
        # Bit 3 clear means the FIFO holds something.
        status = 0x01 if self.running else 0x00
        if not self.fifo:
            status |= 0x08
        values = [status, 0, self.gain, 0, 0, self.exposure_us, self.period_us,
                  self.binning, 0, 0, 0, 0, 0, len(self.fifo),
                  self.min_period_us, 1023, 0, self.w, self.h, 0, 0, 0, 0,
                  0, 0, 0]
        return struct.pack(self.FORMAT, *values).ljust(64, b"\x00")

    def take_frame(self, size):
        tag = self.fifo.pop(0) if self.fifo else 0
        self.frames_read.append(tag)
        # The tag is written into every byte, so the decoded frame identifies
        # which queued frame it came from.
        return bytes([tag & 0xFF]) * size


class _FakeWriteEndpoint:
    def __init__(self, fw):
        self.fw = fw

    def write(self, data, timeout=None):
        cmd_id = data[0]
        value = data[1] | (data[2] << 8) | (data[3] << 16) | (data[4] << 24)
        self.fw.command(cmd_id, value)


class _FakeStatusEndpoint:
    def __init__(self, fw):
        self.fw = fw

    def read(self, size_or_buffer=64, timeout=None):
        return self.fw.status_bytes()


class _FakeTransferEndpoint:
    def __init__(self, fw):
        self.fw = fw

    def read(self, size_or_buffer=512, timeout=None):
        chunk, self.fw.pending = self.fw.pending[:512], self.fw.pending[512:]
        return chunk.ljust(512, b"\x00")


def make_camera(fw, exposure=0.88):
    """A SpttCamera wired to ``fw``, with the endpoints already bound."""
    from cameras.sptt_driver import SpttCamera

    cam = SpttCamera(backend=None)
    cam.ep_wr, cam.ep_rd, cam.ep_tr = fw.ep_wr, fw.ep_rd, fw.ep_tr
    cam.exposure = exposure
    return cam


def written(fw, cmd_id):
    return [value for cid, value in fw.commands if cid == cmd_id]


# ---------------------------------------------------------------------------
# The camera against it
# ---------------------------------------------------------------------------
def test_configure_sends_a_period_that_fits_the_exposure():
    from cameras.sptt_driver import CMD_SET_PERIOD

    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.configure(exposure=0.88)

    periods = written(fw, CMD_SET_PERIOD)
    assert periods and periods[-1] >= exposure_to_us(0.88)
    assert periods[-1] != LEGACY_PERIOD_US


def test_the_period_follows_a_live_exposure_change():
    """The live setter used to write the exposure and leave the period alone."""
    from cameras.sptt_driver import CMD_SET_EXP, CMD_SET_PERIOD

    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.configure(exposure=0.1)
    fw.commands.clear()

    cam.set_exposure(5.0)

    ids = [cid for cid, _ in fw.commands]
    assert CMD_SET_EXP in ids and CMD_SET_PERIOD in ids
    assert ids.index(CMD_SET_EXP) < ids.index(CMD_SET_PERIOD)
    assert written(fw, CMD_SET_PERIOD)[-1] >= exposure_to_us(5.0)


def test_the_firmware_minimum_period_is_respected():
    from cameras.sptt_driver import CMD_SET_PERIOD

    fw = FakeFirmware(min_period_us=2_000_000)
    cam = make_camera(fw)
    cam.configure(exposure=0.1)

    assert cam.min_period_us == 2_000_000
    assert written(fw, CMD_SET_PERIOD)[-1] >= 2_000_000


def test_an_exposure_the_camera_shortened_is_refused():
    from cameras.sptt_driver import ExposureNotHonoured

    fw = FakeFirmware(max_exposure_us=100_000)
    cam = make_camera(fw)
    with pytest.raises(ExposureNotHonoured) as excinfo:
        cam.configure(exposure=0.88)
    assert "0.88" in str(excinfo.value) and "0.1" in str(excinfo.value)


def test_a_period_shorter_than_the_exposure_is_refused():
    """Even when the exposure register itself reads back correctly."""
    from cameras.sptt_driver import ExposureNotHonoured

    fw = FakeFirmware(fixed_period_us=LEGACY_PERIOD_US)
    cam = make_camera(fw)
    with pytest.raises(ExposureNotHonoured) as excinfo:
        cam.configure(exposure=0.88)
    assert "truncated" in str(excinfo.value)


def test_an_operator_period_reaches_the_camera():
    from cameras.sptt_driver import CMD_SET_PERIOD

    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.configure(exposure=0.1, period_us=3_000_000)
    assert written(fw, CMD_SET_PERIOD)[-1] == 3_000_000


def test_a_reconfigure_keeps_the_operator_period():
    """Changing the binning must not revert the period to its default."""
    from cameras.sptt_driver import CMD_SET_PERIOD

    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.configure(exposure=0.1, period_us=3_000_000)
    cam.configure(exposure=0.1, binning=1)      # what _apply_params does
    assert written(fw, CMD_SET_PERIOD)[-1] == 3_000_000


def test_the_first_frame_after_a_parameter_change_is_thrown_away():
    from cameras.sptt_driver import ENCODING_8BPP

    fw = FakeFirmware()
    cam = make_camera(fw)
    # 8-bit so the frame tag survives into the pixels unmangled.
    cam.configure(exposure=0.01, encoding=ENCODING_8BPP)
    cam.start()
    fw.fifo = [1, 2, 3]

    cam.set_exposure(0.02)
    frame = cam.grab_frame()

    # Two frames left the FIFO, and the caller got the second one.
    assert len(fw.frames_read) == 2
    assert int(frame.flat[0]) >> 8 == fw.frames_read[-1]

    before = len(fw.frames_read)
    cam.grab_frame()
    assert len(fw.frames_read) == before + 1     # no further discard


def test_a_scheduled_capture_starts_its_own_frame():
    from cameras.sptt_driver import CMD_FIFO_INIT

    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.configure(exposure=0.01)
    cam.start()
    fw.commands.clear()

    started_after = dt.now()
    cam.grab_fresh_frame()

    assert CMD_FIFO_INIT in [cid for cid, _ in fw.commands]
    assert cam.last_frame_started >= started_after
    assert cam.last_frame_lag is not None


def test_the_wait_covers_a_long_period():
    fw = FakeFirmware()
    cam = make_camera(fw)
    cam.exposure = 30.0
    cam.period_us = 30_075_000
    # The old budget was exposure + 5 s, sized when the period was 75 ms.
    assert cam._frame_wait_budget() > 60.0


def test_the_header_records_what_the_camera_did():
    from cameras.sptt_driver import frame_metadata

    fw = FakeFirmware(max_exposure_us=100_000)
    cam = make_camera(fw)
    try:
        cam.configure(exposure=0.88)
    except Exception:
        pass                                    # the refusal is tested above

    metadata = frame_metadata(cam, dt.now())
    assert metadata["EXPREQ"] == 0.88           # what was asked for
    assert metadata["EXPTIME"] == 0.1           # what the camera reports
    assert metadata["PERIOD"] >= 0
    assert "MINPERD" in metadata

    keys = [k[:8].upper() for k in metadata]
    assert all(len(k) <= 8 for k in keys)
    assert len(set(keys)) == len(keys)          # nothing collides after truncation


def test_a_refused_change_does_not_leave_the_camera_stopped():
    """The schedule has to keep running on the last timing that worked."""
    from cameras.sptt_driver import SpttWorkerConsole

    fw = FakeFirmware(fixed_period_us=LEGACY_PERIOD_US)
    cam = make_camera(fw)
    cam.start()

    worker = SpttWorkerConsole.__new__(SpttWorkerConsole)   # no MQTT, no thread
    worker.cam = cam
    applied, errors = worker._apply_params({"binning": 1})

    assert errors                     # the refusal was reported, not swallowed
    assert fw.running
    assert cam._running


def test_the_gui_copy_does_not_reimplement_the_timing():
    """One source of truth: gui_app drives the driver, it does not re-derive.

    Read as text because PyQt5 need not be installed to check this.
    """
    source = (Path(__file__).resolve().parent.parent / "gui_app.py").read_text(
        encoding="utf-8")
    assert "CMD_SET_PERIOD" not in source
    assert "derive_period_us" not in source
    assert "grab_fresh_frame" in source


def test_an_existing_config_gains_the_new_keys_without_a_migration(tmp_path):
    import json

    import utils

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"sptt": {"output_dir": "/data"}}),
                    encoding="utf-8")

    cfg = utils.load_config(str(path))
    assert cfg["sptt"]["output_dir"] == "/data"
    assert cfg["sptt"]["period_us"] is None
    assert cfg["sptt"]["trigmode"] is None
