"""SPTT cameras (CSDU-429/423, CSDU-285C) over pyusb."""
import os
import sys
import json
import time
import queue
import struct
import threading
import argparse

import alerts

import console_ui

import numpy as np

from datetime import datetime as dt, timedelta, timezone
from pathlib import Path

import frame_archive
import intensity

from utils import (
    claim_instance_name, get_instance_name, get_local_ip, get_system_info,
    APP_DIR, STATUS_MIN_INTERVAL, unique_path,
)
from worker_common import (
    WorkerBus, publish_current_params,
    publish_schedule_state, serving_focus_hold,
    run_focus_iteration, announce_setup_mode,
    SETUP_STATUS, install_stop_handler, stop_signal_name,
)

from .sptt_load_firmware import (
    VID, CSDU429, CSDU285C, MODELS, CameraLocation, explain_usb_error,
    find_device, list_cameras, select_camera,
    find_libusb_backend, load_firmware_files, detach_kernel_driver,
    load_fx2_firmware, wait_for_configured_device, load_fpga_bitstream,
)
from .common.sun_gate import SunSettingsError, gate_from_config
from .common.exposure import frame_stats
from .common.exposure_config import (
    PreflightCfg, OverexposureCfg, parse_preflight, parse_overexposure,
    check_threshold_above_target,
)
from .common.intensity_control import (
    IntensityController, TickSlot, intensity_cards, shoot_split,
)
from .sptt_timing import (
    exposure_to_us, derive_period_us, exposure_mismatch, period_mismatch,
    describe_timing, PERIOD_READOUT_MARGIN_US, CMD_VALUE_MAX,
)

import usb.core
import usb.util

CMD_GET_STATUS      = 0x00
CMD_SET_EXP         = 0x01
CMD_SET_GAIN        = 0x02
CMD_SET_R_OFFSET    = 0x03
CMD_SET_G_OFFSET    = 0x04
CMD_CAM_START       = 0x05
CMD_CAM_STOP        = 0x06
CMD_SET_TRIGMODE    = 0x07
CMD_SET_DRAFT       = 0x08
CMD_SET_PERIOD      = 0x09
CMD_SET_BINNING     = 0x0A
CMD_SET_ENCODING    = 0x0B
CMD_SET_ROI_ORG     = 0x0C
CMD_FIFO_INIT       = 0x0D
CMD_SET_ROI_SIZE    = 0x0E
CMD_SET_TARGET_TEMP = 0x0F
CMD_READ_PREPARE    = 0xF0

ENCODING_8BPP  = 0
ENCODING_12BPP = 1

TRIGMODE_SOFTSTART = 2

USB_CMD_TIMEOUT = 10000
USB_READ_TIMEOUT = 10000

# Dead time added to every frame, and uneven, since the next integration starts
# only when the read is done and the FIFO re-armed. The vendor's program polls at 5 ms.
FIFO_POLL_INTERVAL = 0.005
# A software-started frame cannot arrive before its exposure ends, so the polling only
# begins this long before then: a minute-long exposure is not 12 000 status requests.
POLL_LEAD_S = 0.3
# How often a long frame wait lets the worker publish its status (cam.on_wait).
WAIT_CALLBACK_INTERVAL = 2.0

STATUS_FIFO_EMPTY = 0x08
STATUS_FIFO_FULL = 0x10

# A multiple of 512, so a request never ends inside a packet. Large requests let
# libusb queue the packets: 512 at a time took longer than the frame period.
BULK_CHUNK = 64 * 1024

# When a frame takes longer to collect than the camera takes to produce the
# next one, the period is raised to the time it took times this, plus the pad.
TRANSFER_SLACK = 1.25
TRANSFER_PAD_US = 50_000
# The gap between two live frames that counts towards that: the focus loop's debayer,
# JPEG and publish. Capped so a stream paused for a scheduled capture does not count.
PROCESSING_CAP_S = 0.5

SPTT_CAPTURE_SECONDS = [0, 30]

# USB transfer of a frame before one was timed.
READ_ESTIMATE_S = 0.5
# Arming, the last FIFO poll and clock jitter: what a capture needs besides its frame.
SCHEDULE_MARGIN_S = 0.3
# Frames that may wait for the disk before a capture is refused.
WRITE_QUEUE_DEPTH = 4

# Named from the top-left 2x2 cell, as FITS BAYERPAT does. RGGB is the ICX285AQ's
# layout; sptt.bayer_pattern corrects a camera that reads out differently.
BAYER_PATTERNS = ("RGGB", "GRBG", "GBRG", "BGGR")
DEFAULT_BAYER_PATTERN = "RGGB"

# Peltier set-point range the vendor documents.
TARGET_TEMP_MIN = -30
TARGET_TEMP_MAX = 30

# "Leave this as the camera has it": configure() runs again on every binning or
# encoding change, and a plain default would revert the operator's period and trigger mode.
KEEP = object()


class FrameReadError(RuntimeError):
    """The camera stopped sending before the frame was complete."""


class FrameCancelled(Exception):
    """The wait for a frame was abandoned because the run is stopping."""


class ExposureNotHonoured(RuntimeError):
    """Raised, not logged: a silently shortened exposure corrupts every measurement
    while looking like a working night."""


def make_command(cmd_id, value=0):
    # Checked here because this is where a value too wide for the wire would be masked
    # into something small and plausible.
    if not 0 <= value <= CMD_VALUE_MAX:
        raise ValueError(f"Command 0x{cmd_id:02X}: value {value} does not fit "
                         "the four bytes on the wire")
    return bytes([
        cmd_id,
        value & 0xFF,
        (value >> 8) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 24) & 0xFF,
    ])


def target_temp_byte(target_temp):
    """The CCD set-point as the one signed byte the camera takes, or None.

    setup_app stores it from a float spin box.
    """
    if target_temp is None:
        return None
    value = int(round(float(target_temp)))
    if not TARGET_TEMP_MIN <= value <= TARGET_TEMP_MAX:
        raise ValueError(f"Target temperature {target_temp} °C is outside the "
                         f"camera's {TARGET_TEMP_MIN}..{TARGET_TEMP_MAX} °C")
    return value & 0xFF


def _usb_write_retry(ep, data, timeout=USB_CMD_TIMEOUT, retries=3, delay=0.3):
    for attempt in range(retries):
        try:
            ep.write(data, timeout=timeout)
            return
        except usb.core.USBError:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise


def _usb_read_retry(ep, size, timeout=USB_READ_TIMEOUT, retries=3, delay=0.3):
    for attempt in range(retries):
        try:
            return ep.read(size_or_buffer=size, timeout=timeout)
        except usb.core.USBError:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise


def read_crb(ep_wr, ep_rd):
    _usb_write_retry(ep_wr, make_command(CMD_GET_STATUS))
    array = _usb_read_retry(ep_rd, 64)
    status_list = struct.unpack_from('=BBHHHIIBHHHHBIIHIHHBBIHbbb', array)
    return array[0], status_list


def read_raw_frame(size, ep_tr):
    """The endpoint may return less than asked for: count what each read returned.

    A read is not retried: a timed-out transfer loses what it had received, so a
    retry would shift every later byte of the frame.
    """
    chunks = []
    remaining = size
    while remaining > 0:
        want = min(BULK_CHUNK, -(-remaining // 512) * 512)
        try:
            chunk = ep_tr.read(size_or_buffer=want, timeout=USB_READ_TIMEOUT)
        except usb.core.USBError as exc:
            raise FrameReadError(f"Camera sent {size - remaining} of {size} frame "
                                 f"bytes: {exc}") from exc
        if not len(chunk):
            break                       # decode_frame leaves the rest black
        chunks.append(chunk)
        remaining -= len(chunk)
    return chunks


def _byte_view(buf):
    try:
        return np.frombuffer(buf, dtype=np.uint8)      # pyusb's array, without a copy
    except TypeError:
        return np.asarray(buf, dtype=np.uint8)


def decode_frame(raw_chunks, w, h, encoding, binning=0, interlaced=True):
    """Unpack one raw USB transfer into a frame on the program's 0..65535 scale.

    The one place that knows the sensor's depth: 12 bits, or 8 in the low-bandwidth
    encoding. Done in numpy: per-pixel Python loops take about a second on a full
    744x576 frame, which is dead time in the focus loop and delays a stop.
    """
    raw = (np.concatenate([_byte_view(buf) for buf in raw_chunks])
           if raw_chunks else np.empty(0, dtype=np.uint8))

    if encoding == ENCODING_12BPP:
        # Three bytes carry two pixels: the high eight bits of each, then
        # both low nibbles packed into the third byte.
        groups = raw[:len(raw) // 3 * 3].reshape(-1, 3).astype(np.uint16)
        pixels = np.empty(groups.shape[0] * 2, dtype=np.uint16)
        pixels[0::2] = (groups[:, 0] << 4) | (groups[:, 2] & 0x0F)
        pixels[1::2] = (groups[:, 1] << 4) | (groups[:, 2] >> 4)
        bits = 12
    else:
        pixels = raw.astype(np.uint16)
        bits = 8

    # A short transfer leaves the missing pixels black, as the vendor's does.
    arr = np.zeros(w * h, dtype=np.uint16)
    n = min(len(pixels), w * h)
    arr[:n] = pixels[:n]
    arr = arr.reshape(h, w)

    if binning > 0 or not interlaced:
        # Binning combines the CCD fields, so the data is already progressive;
        # a progressive-scan sensor (CSDU-285C) never had fields to begin with.
        return intensity.to_full_scale(arr, bits)

    # Interlaced CCD: the first field holds the even lines, the second the odd.
    rows = np.arange(h)
    src_rows = np.where(rows % 2 == 0, rows // 2, h // 2 + rows // 2)
    return intensity.to_full_scale(arr[src_rows], bits)


def is_mosaic(model, binning):
    """Binning adds neighbouring colours together on the chip, so a binned colour frame
    is grey.
    """
    return model.color and binning == 0


def debayer(mosaic, pattern=DEFAULT_BAYER_PATTERN):
    """Bilinear demosaic: a (3, H, W) R, G, B cube of ``mosaic``'s dtype.

    numpy only, so the SPTT node needs nothing beyond what it already installs.
    """
    pattern = pattern.upper()
    if pattern not in BAYER_PATTERNS:
        raise ValueError(f"Unknown Bayer pattern {pattern!r}; "
                         f"expected one of {', '.join(BAYER_PATTERNS)}")
    h, w = mosaic.shape
    src = mosaic.astype(np.float32)
    kernel = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], dtype=np.float32)

    def blur(a):
        p = np.pad(a, 1, mode="reflect")
        out = np.zeros_like(a)
        for dy in range(3):
            for dx in range(3):
                out += kernel[dy, dx] * p[dy:dy + h, dx:dx + w]
        return out

    cells = {(0, 0): pattern[0], (0, 1): pattern[1],
             (1, 0): pattern[2], (1, 1): pattern[3]}
    cube = np.empty((3, h, w), dtype=mosaic.dtype)
    for i, colour in enumerate("RGB"):
        mask = np.zeros((h, w), dtype=np.float32)
        for (dy, dx), c in cells.items():
            if c == colour:
                mask[dy::2, dx::2] = 1.0
        plane = blur(src * mask) / blur(mask)
        plane = np.where(mask > 0, src, plane)
        cube[i] = np.clip(np.rint(plane), 0, np.iinfo(mosaic.dtype).max)
    return cube


def seconds_to_next_capture(now, capture_seconds):
    sec = now.second + now.microsecond / 1_000_000
    ahead = [s - sec for s in capture_seconds if s > sec]
    return min(ahead) if ahead else 60 - sec + min(capture_seconds)


def next_capture_at(now, capture_seconds):
    """The first capture tick strictly after ``now``, as a whole-second datetime."""
    tick = now + timedelta(seconds=seconds_to_next_capture(now, capture_seconds))
    return (tick + timedelta(microseconds=500_000)).replace(microsecond=0)


def idle_wait_s(now, capture_seconds, step=0.1):
    """A loop pause that never sleeps past the next tick, so a capture starts on it."""
    return max(0.0, min(step, seconds_to_next_capture(now, capture_seconds)))


def missed_ticks(started, finished, capture_seconds):
    """Ticks after ``started`` whose whole second went by before ``finished``.

    A capture that ends inside a tick's second still fires it, late by under a second.
    """
    missed = []
    tick = next_capture_at(started, capture_seconds)
    while tick + timedelta(seconds=1) <= finished:
        missed.append(tick)
        tick = next_capture_at(tick, capture_seconds)
    return missed


def capture_gap_s(capture_seconds):
    """The shortest interval between two captures, across the minute boundary too."""
    secs = sorted({int(s) % 60 for s in capture_seconds})
    if len(secs) < 2:
        return 60.0
    return float(min([b - a for a, b in zip(secs, secs[1:])]
                     + [60 - secs[-1] + secs[0]]))


def schedule_problem(cam, capture_seconds):
    """A warning when a capture outlasts the gap to the next one, else None.

    A capture that runs into the next tick's second makes the schedule skip it.
    """
    gap = capture_gap_s(capture_seconds)
    need = cam.capture_time_estimate() + SCHEDULE_MARGIN_S
    if need <= gap:
        return None
    longest = max(0.0, gap - (need - cam.exposure))
    message = (f"SPTT exposure {cam.exposure:g} s does not fit the schedule: a capture "
               f"takes about {need:.1f} s and the captures are {gap:g} s apart, so "
               f"frames will be missed. The longest exposure that fits is about "
               f"{longest:.1f} s")
    if not cam.single_shot:
        message += ("; in continuous mode a capture may wait a whole frame period, "
                    "sptt.trigmode 2 starts each frame at its tick")
    return message + "."


def focus_grab(cam, grab):
    """A focus frame for display, or None when the run is stopping: not a focus error."""
    try:
        return cam.for_display(grab())
    except FrameCancelled:
        return None


def focus_frame_fits(now, capture_seconds, cam):
    """A focus frame begun at :59.3 would run past :00 and the scheduled frame would never
    be taken, so ``now`` not being a capture second is not enough.
    """
    if now.second in capture_seconds:
        return False
    return seconds_to_next_capture(now, capture_seconds) > cam.frame_time_estimate()


class SpttCamera:
    def __init__(self, backend, model=CSDU429, port=None):
        self.backend = backend
        self.model = model
        # None: the only camera. Two cameras on one machine must never open the same device.
        self.port = port
        # The ROI origin is always even (see _configure), so a ROI never shifts the mosaic.
        self.bayer_pattern = DEFAULT_BAYER_PATTERN
        self.dev = None
        self.ep_wr = None
        self.ep_rd = None
        self.ep_tr = None
        self.w = 0
        self.h = 0
        self.encoding = ENCODING_8BPP
        self.binning = 0
        self.exposure = 0.88
        self.gain = 100
        self._running = False
        # period_us is what the camera was last told; the override is sptt.period_us
        # (None: derive from the exposure); min_period_us is the firmware's floor for the
        # current binning and ROI.
        self.period_us = None
        self.period_override_us = None
        self.trigmode = 0
        self.min_period_us = 0
        # The frame in progress began under an old setting and has to be dropped.
        self._params_dirty = False
        # When the integration behind the last frame was allowed to start, for DATE-OBS,
        # and how long the frame took to arrive after that.
        self.last_frame_started = None
        self.last_frame_lag = None
        # USB transfer time of the last frame. Longer than the period means the camera
        # outruns the reads.
        self.last_read_s = None
        self._slow_read_warned_at = None
        # transfer_floor_us: the shortest period at which frames can be collected here,
        # learnt from the transfers; 0 until one was too slow. _processing_s: in a live
        # stream, the time between the end of one read and the request for the next.
        self.transfer_floor_us = 0
        self._read_done_at = None
        self._processing_s = 0.0
        self._armed_at = None
        # Lets a frame wait tell that a change from another thread overtook it.
        self._settings_gen = 0
        # Set by a stopping worker: a frame wait can last a whole exposure.
        self.cancel_event = threading.Event()
        # Called from long frame waits, so a worker blocked in one still reports in.
        self.on_wait = None
        # One USB conversation at a time: the GUI changes settings from its own thread, and
        # a status request interleaved with a frame readout gets the wrong bytes back.
        self._lock = threading.RLock()

    def open(self):
        pid = self.model.pid_configured
        self.dev = find_device(self.backend, pid, self.port)
        if not self.dev:
            where = f" on USB port {self.port}" if self.port else ""
            raise RuntimeError(f"Configured {self.model.name} camera not found{where}!")
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            self.dev.reset()
            time.sleep(0.5)
            self.dev = find_device(self.backend, pid, self.port)
            if not self.dev:
                raise RuntimeError("Device lost after reset!")
        except usb.core.USBError:
            pass
        try:
            self.dev.set_configuration()
        except usb.core.USBError as exc:
            raise RuntimeError(explain_usb_error(exc)) from exc
        cfg = self.dev.get_active_configuration()
        self.ep_wr = cfg.interfaces()[0][0]
        self.ep_rd = cfg.interfaces()[0][1]
        self.ep_tr = cfg.interfaces()[0][2]

    def close(self):
        with self._lock:
            if self.dev:
                try:
                    self.stop()
                except Exception:
                    pass
                try:
                    usb.util.dispose_resources(self.dev)
                except Exception:
                    pass
                self.dev = None

    def configure(self, exposure=0.88, gain=100, binning=0, encoding=ENCODING_12BPP,
                  r_offset=None, g_offset=None, trigmode=KEEP, period_us=KEEP,
                  draft=False, roi_org=None, roi_size=None, target_temp=None):
        """Exposure is in seconds.

        ``period_us`` and ``trigmode`` default to KEEP, so a reconfigure after a binning
        change inherits what the operator set; None means "derive it" / "continuous".
        """
        with self._lock:
            return self._configure(exposure, gain, binning, encoding, r_offset,
                                   g_offset, trigmode, period_us, draft,
                                   roi_org, roi_size, target_temp)

    def _configure(self, exposure, gain, binning, encoding, r_offset, g_offset,
                   trigmode, period_us, draft, roi_org, roi_size, target_temp):
        # Before anything is sent, so a bad value does not leave the camera half reconfigured.
        temp_byte = target_temp_byte(target_temp)
        wr = self._write_cmd
        if binning and not self.model.has_binning:
            console_ui.warn(f"{self.model.name} ignores binning: using full "
                            "resolution.")
            binning = 0
        if roi_size is None and not self.model.has_binning:
            # The camera keeps an ROI across runs, and it decides the frame size here.
            roi_org = (0, 0)
            roi_size = self.model.nominal_sizes[0]
        self.exposure = exposure
        self.gain = gain
        self.binning = binning
        self.encoding = encoding
        if period_us is not KEEP:
            self.period_override_us = period_us
        if trigmode is not KEEP:
            self.trigmode = 0 if trigmode is None else int(trigmode)
        self._mark_dirty()

        exposure_us = exposure_to_us(exposure)

        wr(CMD_SET_BINNING, binning)
        if roi_org is not None:
            h_org = roi_org[0] & ~1
            v_org = roi_org[1] & ~1
            wr(CMD_SET_ROI_ORG, (v_org << 16) | h_org)
        if roi_size is not None:
            h_size = roi_size[0] & ~3
            v_size = roi_size[1] & ~3
            wr(CMD_SET_ROI_SIZE, (v_size << 16) | h_size)

        wr(CMD_SET_ENCODING, encoding)
        wr(CMD_SET_EXP, exposure_us)
        wr(CMD_SET_GAIN, gain)
        if r_offset is not None:
            wr(CMD_SET_R_OFFSET, r_offset)
        if g_offset is not None:
            wr(CMD_SET_G_OFFSET, g_offset)
        wr(CMD_SET_TRIGMODE, self.hardware_trigmode)
        wr(CMD_SET_DRAFT, 1 if draft else 0)
        if temp_byte is not None:
            wr(CMD_SET_TARGET_TEMP, temp_byte)

        # Before the period is sent: the firmware's minimum period depends on the frame
        # size, so it means nothing until binning, ROI and encoding are in force.
        _, sl = read_crb(self.ep_wr, self.ep_rd)
        self._set_frame_size(*self._frame_size(sl))
        self.min_period_us = sl[14]
        return self._apply_period()

    def _frame_size(self, sl):
        return (sl[17], sl[18]) if self.model.has_binning else (sl[10], sl[11])

    @property
    def single_shot(self):
        # An operator's external trigger mode is left alone. Software start is taken
        # one frame at a time on any model: each frame needs its own CAM_START.
        if self.trigmode == TRIGMODE_SOFTSTART:
            return True
        return self.model.single_shot and self.trigmode == 0

    @property
    def hardware_trigmode(self):
        return TRIGMODE_SOFTSTART if self.single_shot else self.trigmode

    def _write_cmd(self, cmd_id, value=0):
        _usb_write_retry(self.ep_wr, make_command(cmd_id, value))

    def _mark_dirty(self):
        self._params_dirty = True
        self._settings_gen += 1

    def _set_frame_size(self, w, h):
        """Trust the firmware's size: a table keyed on binning is wrong under an ROI.

        The size decides how many bytes are asked for, and a wrong one leaves the rest of
        the frame in the FIFO to be read as the next one.
        """
        expected = self.model.nominal_sizes.get(self.binning)
        if not w or not h:
            if not expected:
                raise RuntimeError(f"Camera reports an empty frame ({w}x{h})")
            console_ui.warn(f"Camera reports an empty frame ({w}x{h}); "
                            f"assuming {expected[0]}x{expected[1]}")
            w, h = expected
        elif expected and (w, h) != expected and (w, h) != (self.w, self.h):
            console_ui.log(f"SPTT frame size {w}x{h} at binning {self.binning} "
                           f"(full sensor would be {expected[0]}x{expected[1]})")
        if (int(w), int(h)) != (self.w, self.h):
            # A different frame takes a different time to collect; learn anew.
            self.transfer_floor_us = 0
        self.w, self.h = int(w), int(h)

    def _apply_period(self, verify=True):
        """The single place a period is computed or written.

        Every exposure change ends here: a period left at its old value truncates the new
        exposure without saying so.
        """
        exposure_us = exposure_to_us(self.exposure)
        period = derive_period_us(
            self.exposure,
            min_period_us=max(self.min_period_us or 0, self.transfer_floor_us),
            override=self.period_override_us,
            # Outside continuous mode the period does not gate the integration,
            # so an operator asking for a short one gets it.
            allow_short=(self.trigmode != 0 or self.single_shot),
            warn=console_ui.warn)
        self._write_cmd(CMD_SET_PERIOD, period)
        self.period_us = period

        _, sl = read_crb(self.ep_wr, self.ep_rd)
        self.min_period_us = sl[14]
        if verify:
            self._verify_timing(exposure_us, sl,
                                short_period_ok=(self.trigmode != 0 or self.single_shot))
        return sl

    def _verify_timing(self, requested_exposure_us, sl, short_period_ok=False):
        """``short_period_ok``: outside continuous mode the period does not gate the
        integration, so a short one was chosen on purpose and is not a fault."""
        console_ui.log(describe_timing(requested_exposure_us, sl))
        checks = [exposure_mismatch(requested_exposure_us, sl[5])]
        if not short_period_ok:
            checks.append(period_mismatch(sl[5], sl[6]))
        problems = [p for p in checks if p]
        if problems:
            for problem in problems:
                console_ui.warn(problem)
            raise ExposureNotHonoured("; ".join(problems))

    def set_exposure(self, value):
        """Exposure is in seconds. The period has to follow it, or the frames do not change."""
        exposure_us = exposure_to_us(value)
        with self._lock:
            self._write_cmd(CMD_SET_EXP, exposure_us)
            self.exposure = exposure_us / 1_000_000.0
            self._mark_dirty()
            self._apply_period()

    def set_gain(self, value):
        with self._lock:
            self.gain = value
            self._write_cmd(CMD_SET_GAIN, value)
            self._mark_dirty()

    def _flush_endpoints(self):
        for ep in (self.ep_rd, self.ep_tr):
            for _ in range(64):
                try:
                    ep.read(size_or_buffer=512, timeout=50)
                except usb.core.USBError:
                    break

    def start(self, retries=3):
        with self._lock:
            return self._start(retries)

    def _start(self, retries):
        if self.single_shot:
            self._abort_shot()
            self._running = True
            self._params_dirty = False
            return
        for attempt in range(retries):
            try:
                try:
                    _usb_write_retry(self.ep_wr, make_command(CMD_CAM_STOP),
                                     timeout=USB_CMD_TIMEOUT, retries=1)
                except usb.core.USBError:
                    pass
                time.sleep(0.1)
                self._flush_endpoints()
                _usb_write_retry(self.ep_wr, make_command(CMD_FIFO_INIT))
                time.sleep(0.05)
                _usb_write_retry(self.ep_wr, make_command(CMD_CAM_START))
                self._running = True
                # The run begins here, so the first frame out of it was
                # integrated under the current settings by construction.
                self._params_dirty = False
                self.last_frame_started = dt.now(timezone.utc)
                self._armed_at = time.monotonic()
                self._read_done_at = None
                time.sleep(0.1)
                sb, _ = read_crb(self.ep_wr, self.ep_rd)
                if sb & 0x01:
                    return
            except usb.core.USBError as e:
                if attempt < retries - 1:
                    console_ui.warn(f"Start attempt {attempt + 1} failed: {e}, retrying…")
                    time.sleep(0.5 * (attempt + 1))
                else:
                    raise RuntimeError(f"Failed to start camera after {retries} attempts: {e}")

    def _abort_shot(self):
        try:
            _usb_write_retry(self.ep_wr, make_command(CMD_CAM_STOP), retries=1)
        except usb.core.USBError:
            pass
        time.sleep(0.05)
        self._flush_endpoints()

    def stop(self):
        with self._lock:
            if self._running:
                try:
                    _usb_write_retry(self.ep_wr, make_command(CMD_CAM_STOP))
                except usb.core.USBError:
                    pass
                self._running = False

    def _frame_wait_budget(self):
        """Worst case the wait starts just after an integration did, so a whole period can
        pass before the next one begins.
        """
        period_s = (self.period_us or 0) / 1_000_000.0
        return max(period_s + self.exposure + 5.0, 3.0)

    def frame_time_estimate(self):
        """A grab's usual duration: up to a period for the frame under way, another for the
        one dropped after a setting change, and the readout.

        Keeps focus frames clear of a scheduled capture; the timeout is _frame_wait_budget.
        """
        period_us = (self.period_us
                     or exposure_to_us(self.exposure) + PERIOD_READOUT_MARGIN_US)
        if self.single_shot:
            return self.exposure + 1.0
        return 2 * period_us / 1_000_000.0 + 1.0

    def capture_time_estimate(self):
        """From a scheduled tick to the frame read, settings unchanged.

        A software-started frame takes its exposure; a free-running one may first have
        to wait out the period under way.
        """
        read_s = self.last_read_s if self.last_read_s is not None else READ_ESTIMATE_S
        if self.single_shot:
            return self.exposure + read_s
        period_us = (self.period_us
                     or exposure_to_us(self.exposure) + PERIOD_READOUT_MARGIN_US)
        return period_us / 1_000_000.0 + read_s

    def _trigger(self):
        self._read_done_at = None
        self._params_dirty = False
        _usb_write_retry(self.ep_wr, make_command(CMD_FIFO_INIT))
        _usb_write_retry(self.ep_wr, make_command(CMD_CAM_START))
        self.last_frame_started = dt.now(timezone.utc)
        self._armed_at = time.monotonic()

    def _rearm(self):
        self._read_done_at = None
        _usb_write_retry(self.ep_wr, make_command(CMD_FIFO_INIT))
        self.last_frame_started = dt.now(timezone.utc)
        self._armed_at = time.monotonic()

    def _wait_for_fifo(self, budget):
        # The lock is taken per poll, not for the whole wait: a wait can last
        # a full exposure, and the GUI must be able to change a setting in it.
        start = time.monotonic()
        deadline = start + budget
        # Only a software start is known to begin the integration at _armed_at; a
        # free-running camera may already be streaming into the FIFO.
        quiet_until = ((self._armed_at or start) + self.exposure - POLL_LEAD_S
                       if self.single_shot else start)
        next_report = start + WAIT_CALLBACK_INTERVAL
        while True:
            now = time.monotonic()
            if self.cancel_event.is_set():
                raise FrameCancelled("Frame wait abandoned: the run is stopping")
            if now >= deadline:
                raise RuntimeError("FIFO timeout — no frame data")
            if self.on_wait is not None and now >= next_report:
                next_report = now + WAIT_CALLBACK_INTERVAL
                try:
                    self.on_wait()
                except Exception:
                    pass
            if now < quiet_until:
                self.cancel_event.wait(min(quiet_until, next_report, deadline) - now)
                continue
            try:
                with self._lock:
                    sb, _ = read_crb(self.ep_wr, self.ep_rd)
            except usb.core.USBError:
                time.sleep(0.01)
                continue
            if not (sb & STATUS_FIFO_EMPTY):
                return sb
            time.sleep(FIFO_POLL_INTERVAL)

    def _read_one_frame(self):
        """Returns the arguments for decode_frame: decoding happens outside the USB lock."""
        w, h = self.w, self.h

        if self.encoding == ENCODING_12BPP:
            frame_size = w * h * 3 // 2
        else:
            frame_size = w * h

        t0 = time.monotonic()
        _usb_write_retry(self.ep_wr, make_command(CMD_READ_PREPARE, frame_size))
        raw_chunks = read_raw_frame(frame_size, self.ep_tr)
        self.last_read_s = time.monotonic() - t0
        if self.single_shot:
            self._read_done_at = time.monotonic()
            if self._armed_at is not None:
                self.last_frame_lag = self._read_done_at - self._armed_at
            return (raw_chunks, w, h, self.encoding, self.binning,
                    self.model.interlaced)
        self._check_read_time()
        # Reset the FIFO only if it is empty. Otherwise it holds the next frame from its
        # first byte, and with three bytes to two pixels a cut head turns every later
        # frame into noise. Left alone it is read whole next time.
        sb, _ = read_crb(self.ep_wr, self.ep_rd)
        if sb & STATUS_FIFO_EMPTY:
            _usb_write_retry(self.ep_wr, make_command(CMD_FIFO_INIT))
        self._read_done_at = time.monotonic()
        if self._armed_at is not None:
            self.last_frame_lag = time.monotonic() - self._armed_at
        return (raw_chunks, w, h, self.encoding, self.binning,
                self.model.interlaced)

    def _keep_up_with_transfer(self):
        """Slow the camera to the pace its frames can be collected at; True if it did.

        In continuous mode the next frame starts one period after the last, read or not,
        and bytes that do not fit are lost at the join: frames shift sideways, then decode
        as noise. So when collecting and handling a frame outlasts the period, the period
        is raised and the run restarted, which puts the FIFO back in step. A period the
        operator set, or a trigger mode, only gets the warning.
        """
        if self.last_read_s is None or not self.period_us:
            return False
        busy_s = self.last_read_s + min(self._processing_s, PROCESSING_CAP_S)
        if busy_s * 1e6 <= self.period_us:
            return False
        if self.trigmode != 0 or self.period_override_us is not None:
            self._check_read_time()
            return False
        self.transfer_floor_us = int(busy_s * 1e6 * TRANSFER_SLACK) + TRANSFER_PAD_US
        old_period = self.period_us
        self._apply_period(verify=False)
        console_ui.warn(
            f"SPTT frames take {busy_s:.2f} s to collect"
            f"{f' and handle' if self._processing_s else ''}, more than the "
            f"{old_period / 1e6:.3f} s frame period — the camera was outrunning "
            f"the reads. Frame period raised to {self.period_us / 1e6:.3f} s; "
            "restarting the run to get back in step.")
        self._start(1)
        return True

    def _check_read_time(self):
        period_s = (self.period_us or 0) / 1_000_000.0
        if not period_s or self.last_read_s <= period_s:
            return
        now = time.monotonic()
        if self._slow_read_warned_at and now - self._slow_read_warned_at < 60:
            return
        self._slow_read_warned_at = now
        console_ui.warn(f"SPTT frame transfer took {self.last_read_s:.2f} s, longer "
                        f"than the {period_s:.3f} s frame period: the camera "
                        "outruns the USB reads, so live frames are dropped. "
                        "A longer exposure or binning gives the reads time.")

    def grab_frame(self):
        """One frame, integrated under the settings currently in force.

        After a change the sensor is part-way through a frame under the old ones; it is
        discarded here so no caller has to know. A change from another thread during the
        wait starts the wait over the same way.
        """
        if self.single_shot:
            return self._grab_single()
        while True:
            with self._lock:
                # The live loop's own work, which the camera does not wait for either.
                self._processing_s = (time.monotonic() - self._read_done_at
                                      if self._read_done_at is not None else 0.0)
                discard = self._params_dirty
                if discard:
                    self._rearm()
                    self._params_dirty = False
                gen = self._settings_gen
            sb = self._wait_for_fifo(self._frame_wait_budget())
            with self._lock:
                if self._settings_gen != gen:
                    continue                # overtaken by a change
                if sb & STATUS_FIFO_FULL:
                    # The FIFO ran over, so nothing in it can be trusted to start at a frame.
                    # A restart puts it back in step: the first frame after one is always whole.
                    console_ui.warn("SPTT FIFO overflowed — restarting the run")
                    self._start(1)
                    continue
                raw = self._read_one_frame()
                # The frame just read is whole; what the camera does next may
                # not be. Slowing it down restarts the run for the next one.
                self._keep_up_with_transfer()
            if not discard:                 # else it was the old-settings frame
                return decode_frame(*raw)

    def _grab_single(self):
        """One software-started frame: its integration begins here and nothing can
        run ahead of the read, so a slow host costs time and not data.
        """
        retried = False
        while True:
            with self._lock:
                self._trigger()
                gen = self._settings_gen
            self._wait_for_fifo(self._frame_wait_budget())
            with self._lock:
                if self._settings_gen != gen:
                    self._abort_shot()
                    continue
                try:
                    raw = self._read_one_frame()
                except FrameReadError as exc:
                    self._abort_shot()
                    if retried:
                        raise
                    retried = True
                    console_ui.warn(f"{exc}; taking the frame again")
                    continue
            return decode_frame(*raw)

    def grab_fresh_frame(self):
        """A frame whose integration was allowed to start at this call.

        The camera free-runs, so between shots the FIFO fills with frames up to half a
        minute old, which would be archived under a DATE-OBS they were not taken at.
        """
        if self.single_shot:
            return self._grab_single()
        with self._lock:
            self._rearm()
        return self.grab_frame()

    def is_mosaic(self):
        return is_mosaic(self.model, self.binning)

    def for_display(self, frame):
        """A raw mosaic demosaiced to an (H, W, 3) RGB image, the form the live view,
        focus_app and the previews take colour in; anything else unchanged."""
        if not self.is_mosaic():
            return frame
        return np.moveaxis(self.to_rgb(frame), 0, -1)

    def to_rgb(self, frame):
        return debayer(frame, self.bayer_pattern)

    def get_status(self):
        with self._lock:
            return read_crb(self.ep_wr, self.ep_rd)

    def get_status_info(self):
        try:
            sb, sl = self.get_status()
            frame_w, frame_h = self._frame_size(sl)
            return {
                "running": bool(sb & 1),
                "exposing": bool(sb & 2),
                "busy": bool(sb & 4),
                "fifo_empty": bool(sb & 8),
                "fifo_full": bool(sb & 16),
                "gain": sl[2],
                "r_offset": sl[3],
                "g_offset": sl[4],
                "exposure_s": sl[5] / 1_000_000.0,
                "period_us": sl[6],
                "min_period_us": sl[14],
                "binning": sl[7],
                "roi_org_h": sl[8],
                "roi_org_v": sl[9],
                "roi_size_h": sl[10],
                "roi_size_v": sl[11],
                "frame_w": frame_w,
                "frame_h": frame_h,
                "fifo_cnt": sl[13],
                "read_s": self.last_read_s,
                "transfer_floor_us": self.transfer_floor_us,
                "temp_sink": sl[23],
                "temp_ccd": sl[24],
                "temp_target": sl[25],
            }
        except Exception:
            return {}


def ensure_firmware_loaded(backend, firmware_dir=None, usb_port=None):
    """Return the camera's CameraLocation once it is up, else None.

    ``firmware_dir`` empty means ``firmware/`` next to the program. ``usb_port`` picks
    the camera when the machine has several.
    """
    try:
        found = select_camera(backend, usb_port)
    except LookupError as exc:
        console_ui.error(str(exc))
        return None
    model, port = found.model, found.port
    ready = found._replace(configured=True)

    if found.configured:
        console_ui.log(f"{model.name} on USB port {port} already configured "
                       f"(PID=0x{model.pid_configured:04x}), skipping firmware load.")
        return ready

    dev_raw = find_device(backend, model.pid_raw, port)
    if not dev_raw:
        console_ui.error(f"{model.name} disappeared from USB port {port}.")
        return None

    console_ui.log(f"Loading {model.name} firmware files…")
    try:
        fx2_blocks, fpga_data = load_firmware_files(firmware_dir, model)
    except (FileNotFoundError, ValueError) as exc:
        # As with "no camera found": the caller decides, and the operator gets the path
        # that was actually searched.
        console_ui.error(str(exc))
        return None

    try:
        if _load_firmware(backend, model, port, dev_raw, fx2_blocks, fpga_data):
            return ready
        return None
    except usb.core.USBError as exc:
        console_ui.error(explain_usb_error(exc))
        return None
    except RuntimeError as exc:
        console_ui.error(f"SPTT firmware load failed: {exc}")
        return None


def _load_firmware(backend, model, port, dev_raw, fx2_blocks, fpga_data):
    console_ui.log(f"Found raw FX2 device: {VID:04x}:{model.pid_raw:04x}")
    detach_kernel_driver(dev_raw)
    load_fx2_firmware(dev_raw, fx2_blocks)

    console_ui.log("Sending USB bus reset…")
    try:
        dev_raw.reset()
    except usb.core.USBError as e:
        console_ui.log(f"USB reset returned: {e} (expected)")
    usb.util.dispose_resources(dev_raw)
    del dev_raw

    dev = wait_for_configured_device(backend, model=model, port=port)
    if not dev:
        console_ui.error("Device not found after firmware load.")
        return False

    detach_kernel_driver(dev)
    dev.set_configuration()
    load_fpga_bitstream(dev, fpga_data)

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    del dev

    dev_final = wait_for_configured_device(backend, timeout=10.0, model=model,
                                           port=port)
    if dev_final:
        console_ui.log(f"{model.name} firmware loaded successfully.")
        usb.util.dispose_resources(dev_final)
        return True

    console_ui.warn("Device not found after initialization.")
    return False


def intensity_settings(section):
    """``(preflight, overexposure, errors)``; a stage that cannot run as configured is
    switched off and reported. Preflight needs ``mode: "sun"``: it starts at a solar
    altitude.
    """
    section = section or {}
    errors = []
    mode = str(section.get("mode") or "always").strip().lower()
    try:
        sun_max_angle = float(section.get("sun_max_angle", -10.0))
    except (TypeError, ValueError):
        sun_max_angle = -10.0
    preflight = parse_preflight(section.get("preflight") or {}, mode, sun_max_angle,
                                errors, prefix="sptt", modes=("sun",))
    overexposure = parse_overexposure(section.get("overexposure") or {}, errors,
                                      prefix="sptt")
    check_threshold_above_target(preflight, overexposure, errors, prefix="sptt")
    return preflight, overexposure, errors


def frame_metadata(cam, when, status=None, debayered=False):
    """The FITS header for one archived SPTT frame.

    EXPTIME is the hardware's own figure, the one photometry has to divide by; EXPREQ
    keeps the request. A raw colour frame gives its mosaic layout in BAYERPAT, read by
    DS9, Siril and astropy-based tools. Keys stay within the eight characters of FITS.
    """
    if status is None:
        status = cam.get_status_info()
    # DATE-OBS in UTC, as every other camera in this program writes it; a
    # naive ``when`` is the station's local time. DATE-LOC keeps that one.
    started = (cam.last_frame_started or when).astimezone(timezone.utc)
    metadata = {
        "DATE-OBS": started.replace(tzinfo=None).isoformat(),
        "TIMESYS": "UTC",
        "DATE-LOC": started.astimezone().replace(tzinfo=None).isoformat(),
        "INSTRUME": cam.model.name,
        "EXPTIME": status.get("exposure_s", cam.exposure),
        "EXPREQ": cam.exposure,
        "PERIOD": int(status.get("period_us", cam.period_us or 0)),
        "MINPERD": int(status.get("min_period_us", cam.min_period_us or 0)),
        "TRIGMODE": int(cam.hardware_trigmode),
        "GAIN": cam.gain,
        "BINNING": cam.binning,
        "ENCODING": "12bit" if cam.encoding == ENCODING_12BPP else "8bit",
        # ENCODING is the sensor's digitisation; this is the scale of the pixels in this file.
        "ADCFULL": intensity.FULL_SCALE,
    }
    if is_mosaic(cam.model, cam.binning):
        if debayered:
            # No BAYERPAT here: viewers that see one demosaic again.
            metadata["COLORTYP"] = "RGB"
            metadata["DEBAYER"] = f"bilinear {cam.bayer_pattern}"
        else:
            metadata["COLORTYP"] = "BAYER"
            metadata["BAYERPAT"] = cam.bayer_pattern
            metadata["XBAYROFF"] = 0
            metadata["YBAYROFF"] = 0
    if cam.last_frame_lag is not None:
        metadata["FRAMELAG"] = round(cam.last_frame_lag, 3)
    if cam.last_read_s is not None:
        # The USB transfer alone; compared with PERIOD it says whether the
        # reads keep up with the camera.
        metadata["READTIME"] = round(cam.last_read_s, 3)
    if status:
        metadata["CCDTEMP"] = status.get("temp_ccd", 0)
        metadata["SINKTEMP"] = status.get("temp_sink", 0)
        metadata["TRGTEMP"] = status.get("temp_target", 0)
    return metadata


def save_fits(filepath, frame, metadata=None):
    try:
        from astropy.io import fits
    except ImportError:
        console_ui.warn("astropy not installed, falling back to raw FITS")
        _save_fits_minimal(filepath, frame, metadata)
        return

    hdu = fits.PrimaryHDU(frame)
    hdr = hdu.header

    if metadata:
        for key, value in metadata.items():
            # FITS header keys are max 8 chars
            fits_key = key[:8].upper()
            try:
                hdr[fits_key] = value
            except Exception:
                pass

    hdu.writeto(filepath, overwrite=True)


def _save_fits_minimal(filepath, frame, metadata=None):
    import struct as st

    # A demosaiced colour frame is a (3, H, W) cube; FITS lists axes fastest
    # first, so NAXIS1 is the width and NAXIS3 the colour.
    axes = frame.shape[::-1]
    bitpix = 16 if frame.dtype in (np.uint16, np.int16) else 8
    # FITS has no unsigned 16-bit type: values go out as signed shorts with an offset,
    # which is what astropy writes for a uint16 array too.
    unsigned = bitpix == 16 and frame.dtype == np.uint16

    cards = []
    cards.append(f"SIMPLE  =                    T / FITS standard")
    cards.append(f"BITPIX  = {bitpix:>20d} / bits per pixel")
    cards.append(f"NAXIS   = {len(axes):>20d} / number of axes")
    for n, (size, what) in enumerate(zip(axes, ("width", "height", "colour")), 1):
        cards.append(f"NAXIS{n}  = {size:>20d} / {what}")
    if unsigned:
        cards.append(f"BZERO   = {32768:>20d} / offset for unsigned 16-bit data")
        cards.append(f"BSCALE  = {1:>20d} / linear scaling factor")

    if metadata:
        for key, value in metadata.items():
            fits_key = key[:8].upper().ljust(8)
            if isinstance(value, tuple):
                value = value[0]
            if isinstance(value, bool):
                val_str = "T" if value else "F"
                cards.append(f"{fits_key}= {val_str:>20s}")
            elif isinstance(value, int):
                cards.append(f"{fits_key}= {value:>20d}")
            elif isinstance(value, float):
                cards.append(f"{fits_key}= {value:>20.6f}")
            elif isinstance(value, str):
                val_str = f"'{value[:68]}'"
                cards.append(f"{fits_key}= {val_str:<20s}")

    cards.append(f"END")

    header_str = ""
    for card in cards:
        header_str += card.ljust(80)
    while len(header_str) % 2880 != 0:
        header_str += " " * 80

    if bitpix == 16:
        values = (frame.astype(np.int32) - 32768) if unsigned else frame
        data = values.astype(">i2").tobytes()
    else:
        data = frame.astype(np.uint8).tobytes()

    pad_len = (2880 - len(data) % 2880) % 2880
    data += b'\x00' * pad_len

    with open(filepath, "wb") as f:
        f.write(header_str.encode("ascii"))
        f.write(data)


class FrameWriter(threading.Thread):
    """Archives frames off the capture thread, so a slow disk delays a file and not the
    next exposure.

    ``results`` gets ``(filepath, error, summary)`` per frame; the worker counts them.
    """

    def __init__(self, cam, service=None, depth=WRITE_QUEUE_DEPTH):
        super().__init__(daemon=True, name="sptt-writer")
        self.cam = cam
        self._service = service
        self._jobs = queue.Queue(maxsize=depth)
        self.results = queue.Queue()

    def submit(self, filepath, frame, metadata, debayered, when, summary=""):
        try:
            self._jobs.put_nowait((filepath, frame, metadata, debayered, when, summary))
        except queue.Full:
            raise RuntimeError(f"{self._jobs.maxsize} frames are still waiting for "
                               "the disk; this one is dropped") from None

    def run(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            filepath, frame, metadata, debayered, when, summary = job
            # Before the write: a focus frame taken meanwhile must not be overtaken
            # by this older one.
            if self._service is not None:
                try:
                    # Demosaiced: the focus measures would read a mosaic's colour
                    # checkerboard as fine detail.
                    self._service.publish_frame(self.cam.for_display(frame), when)
                except Exception:
                    pass
            try:
                save_fits(filepath, self.cam.to_rgb(frame) if debayered else frame,
                          metadata)
                self.results.put((filepath, None, summary))
            except Exception as exc:
                self.results.put((filepath, exc, summary))

    def close(self, timeout=60.0):
        """Writes what is queued, then ends the thread."""
        if not self.is_alive():
            return
        try:
            self._jobs.put(None, timeout=timeout)
        except queue.Full:
            return
        self.join(timeout)

    def take_results(self):
        while True:
            try:
                yield self.results.get_nowait()
            except queue.Empty:
                return


class SpttWorkerConsole(threading.Thread):
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, cam, output_dir, instance_name, status_dir,
                 capture_seconds=None, service=None, node_name="",
                 setup_mode=False, debayer=False, sun_gate=None,
                 preflight=None, overexposure=None):
        super().__init__(daemon=True)
        self.cam = cam
        # None: captures around the clock.
        self.sun_gate = sun_gate
        # Archive the colour camera's frames as R, G, B cubes instead of the raw mosaic.
        self.debayer = bool(debayer)
        self.output_dir = output_dir
        self.instance_name = instance_name
        self.status_dir = status_dir
        self.capture_seconds = sorted(capture_seconds or SPTT_CAPTURE_SECONDS)
        self.setup_mode = bool(setup_mode)
        self._service = service
        self._bus = WorkerBus("sptt", instance_name, status_dir,
                              service=service, node_name=node_name)
        self._stop_event = threading.Event()
        # Set when the run gave up on its own, so the process can exit non-zero
        # and systemd's Restart=on-failure brings the camera back.
        self.failed = False
        self._shots = 0
        self._errors = 0
        self._missed = 0
        self._consecutive_errors = 0
        self._last_shot = None
        self._last_frame = None
        self._writer = None
        # Whether a schedule check has already used a timed transfer.
        self._schedule_checked_read = False
        self._last_status = "running"
        # Whether the last pass ran under a focus hold: entering and leaving it are
        # logged once, not ten times a second.
        self._held = False
        # Refreshed on the status cadence, not on every loop pass: see _camera_status.
        self._cam_status = {}
        self._cam_status_at = None
        self._system_info = None
        # The exposure the operator set, which focus_app and the status report. A split
        # or preflight exposure stays on the camera between ticks: in continuous mode
        # every change costs a dropped frame, so it moves only when the plan does.
        self._base_exposure = None
        self._last_mean = None
        self._last_saturation = 0.0
        self._night = None
        self._intensity = IntensityController(
            preflight or PreflightCfg(), overexposure or OverexposureCfg(),
            budget=self._preflight_budget, dead_time=self._dead_time)

    def request_stop(self):
        self._stop_event.set()
        cancel = getattr(self.cam, "cancel_event", None)
        if cancel is not None:
            cancel.set()

    def _check_schedule(self):
        if self.setup_mode:
            return
        self._schedule_checked_read = self.cam.last_read_s is not None
        problem = schedule_problem(self.cam, self.capture_seconds)
        if problem:
            console_ui.warn(problem)

    def _apply_params(self, params):
        applied = {}
        errors = []
        try:
            if "exposure" in params:
                self.cam.set_exposure(float(params["exposure"]))
                self._base_exposure = float(params["exposure"])
                applied["exposure"] = float(params["exposure"])
                self._check_schedule()
            if "gain" in params:
                self.cam.set_gain(int(params["gain"]))
                applied["gain"] = int(params["gain"])
            if "binning" in params or "encoding" in params:
                binning = int(params.get("binning", self.cam.binning))
                enc_param = params.get("encoding")
                if enc_param in ("12bit", "12", 12):
                    encoding = ENCODING_12BPP
                elif enc_param in ("8bit", "8", 8):
                    encoding = ENCODING_8BPP
                else:
                    encoding = self.cam.encoding
                self.cam.stop()
                try:
                    self.cam.configure(
                        exposure=self.cam.exposure,
                        gain=self.cam.gain,
                        binning=binning,
                        encoding=encoding,
                    )
                finally:
                    # configure() refuses a timing the camera did not take, and a rejected change
                    # must not leave the schedule with a stopped camera.
                    self.cam.start()
                applied["binning"] = binning
                applied["encoding"] = "12bit" if encoding == ENCODING_12BPP else "8bit"
        except Exception as e:
            errors.append(str(e))
        return applied, errors

    def _current_params(self):
        return {
            "exposure": (self.cam.exposure if self._base_exposure is None
                         else self._base_exposure),
            "gain": self.cam.gain,
            "binning": self.cam.binning,
            "encoding": "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit",
        }

    def _focus_holding(self):
        return self._service is not None and self._service.focus_hold()

    def _handle_focus(self, now):
        """Held sessions get every tick, like setup mode: the operator agreed to interrupt
        the measurements.
        """
        if self._service is None or not self._service.focus_active():
            return
        if (not self.setup_mode and not self._focus_holding()
                and not focus_frame_fits(now, self.capture_seconds, self.cam)):
            return
        req_id, params = self._service.take_param_request()
        if params:
            applied, errors = self._apply_params(params)
            self._service.complete_param_request(req_id, applied, errors)
            self._service.set_current_params(self._current_params())
        # Only the frame right after a change is worth a full period: grab_frame drops
        # the contaminated one anyway, so the focus loop otherwise keeps its rate.
        grab = self.cam.grab_fresh_frame if params else self.cam.grab_frame
        run_focus_iteration(
            self._service, lambda: focus_grab(self.cam, grab),
            on_error=lambda exc, stopped: console_ui.warn(
                          f"Focus frame failed: {exc}"
                          f"{' — focus mode disabled' if stopped else ''}"))

    def run(self):
        self._bus.prepare_status_dir()
        if self._service is not None:
            self._service.set_current_params(self._current_params())

        if self.setup_mode:
            console_ui.log("SPTT ready for focusing (setup mode) — nothing is archived")
            self._save_status(SETUP_STATUS, force=True)
        else:
            console_ui.log("SPTT measurement started (captures at :00 and :30"
                           f"{', sun permitting' if self.sun_gate else ''})")
            self._save_status("running", force=True)

        cancel = getattr(self.cam, "cancel_event", None)
        if cancel is not None and not self._stop_event.is_set():
            cancel.clear()
        try:
            self.cam.start()
        except Exception as exc:
            console_ui.error(f"Failed to start camera: {exc}")
            self.failed = True
            self._save_status("error", force=True)
            self._bus.shutdown(failed=f"Failed to start camera: {exc}")
            return

        self._base_exposure = float(self.cam.exposure)
        self._intensity.begin_session()
        self._writer = FrameWriter(self.cam, self._service)
        self._writer.start()
        if hasattr(self.cam, "on_wait"):
            self.cam.on_wait = lambda: self._save_status(self._last_status)
        if hasattr(self.cam, "capture_time_estimate"):
            self._check_schedule()

        if self.sun_gate is not None:
            # The first altitude imports astropy, seconds: not on a capture second.
            self.sun_gate.is_open(dt.now())

        try:
            self._loop()
        finally:
            if hasattr(self.cam, "on_wait"):
                # The camera outlives the run in the GUI, whose preview would keep
                # publishing this finished worker's status.
                self.cam.on_wait = None
            self.cam.stop()
            self._writer.close()
            self._collect_saved()

        self._save_status("error" if self.failed else "stopped", force=True)
        self._bus.shutdown(failed="consecutive capture errors" if self.failed else None)
        console_ui.log("SPTT measurement stopped")

    def _loop(self):
        last_fired = (-1, -1)
        while not self._stop_event.is_set():
            self._collect_saved()
            if self._consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                console_ui.error(f"{self._consecutive_errors} consecutive errors, "
                                 "stopping")
                self.failed = True
                return
            now = dt.now()

            fire_key = (now.minute, now.second)
            holding = self._focus_holding()
            if holding and not self._held:
                console_ui.log("Focus session took the camera — captures at "
                               ":00 and :30 are paused until it ends.")
            elif self._held and not holding:
                console_ui.log("Focus session ended — captures resume.")
                last_fired = (-1, -1)   # the second we stopped in is not owed a frame
            self._held = holding
            serving_focus_hold(self._service, holding)
            publish_schedule_state(self._service,
                                   not self.setup_mode and not holding)
            if self.setup_mode or holding:
                # No schedule to honour: focus gets every tick, nothing is saved.
                self._handle_focus(now)
                self._save_status(SETUP_STATUS)
                self._stop_event.wait(0.1)
                continue
            if self.sun_gate is not None and not self.sun_gate.is_open(
                    now, refresh=now.second not in self.capture_seconds):
                last_fired = (-1, -1)
                self._handle_focus(now)
                self._save_status("waiting")
                self._stop_event.wait(0.1)
                continue
            self._follow_sun_stage()
            if now.second in self.capture_seconds and fire_key != last_fired:
                last_fired = fire_key
                ok = self._capture_tick(now)
                if ok is None:
                    continue                # stopping
                if ok:
                    self._last_shot = now
                    self._save_status("running", force=True)
                else:
                    self._consecutive_errors += 1
                    self._errors += 1
                    self._save_status("error", force=True)
                self._note_missed(now, dt.now())
                if (ok and not self._schedule_checked_read
                        and hasattr(self.cam, "capture_time_estimate")):
                    self._check_schedule()          # now with a timed transfer
                continue
            elif now.second not in self.capture_seconds:
                last_fired = (-1, -1)
                self._handle_focus(now)

            self._save_status("running")
            self._stop_event.wait(idle_wait_s(dt.now(), self.capture_seconds))

    def _note_missed(self, started, finished):
        ticks = missed_ticks(started, finished, self.capture_seconds)
        if not ticks:
            return
        self._missed += len(ticks)
        took = (finished - started).total_seconds()
        console_ui.warn(f"Missed the {', '.join(f'{t:%H:%M:%S}' for t in ticks)} "
                        f"capture{'s' if len(ticks) > 1 else ''}: the one before "
                        f"took {took:.1f} s")

    def _collect_saved(self):
        if self._writer is None:
            return
        for filepath, error, summary in self._writer.take_results():
            if error is None:
                self._shots += 1
                self._consecutive_errors = 0
                console_ui.log(f"Frame saved: {os.path.basename(filepath)} {summary}")
            else:
                self._errors += 1
                self._consecutive_errors += 1
                console_ui.error(f"Could not save {os.path.basename(filepath)}: {error}")
                self._save_status("error", force=True)

    def _follow_sun_stage(self):
        """A new night starts from the scheduled exposure; the sun reaching
        ``sun_max_angle`` ends the automatic stage.
        """
        gate = self.sun_gate
        if gate is None:
            return
        if gate.night != self._night:
            self._night = gate.night
            self._intensity.begin_session(preflight=gate.stage == "preflight")
            if self._intensity.stage == "preflight":
                console_ui.log(f"Preflight stage: holding "
                               f"{self._intensity.preflight.target_mean:.0f} ADU until "
                               f"the sun reaches {gate.max_angle:g}°")
        elif gate.stage == "main" and self._intensity.stage == "preflight":
            self._intensity.enter_main(f"Sun at {gate.max_angle:g}° — scheduled "
                                       f"exposure {self._base_exposure:g} s")

    def _dead_time(self):
        """From the end of one exposure to the start of the next one asked for at once."""
        estimate = getattr(self.cam, "capture_time_estimate", None)
        if estimate is None:
            return READ_ESTIMATE_S
        return max(float(estimate()) - float(self.cam.exposure), 0.0)

    def _preflight_budget(self, entry):
        """The longest exposure that still leaves the next tick its start. A free-running
        camera drops the frame under way when the exposure changes, which preflight
        does on most ticks.
        """
        dropped = 0.0
        if not getattr(self.cam, "single_shot", True):
            dropped = (getattr(self.cam, "period_us", 0) or 0) / 1e6
        return max(capture_gap_s(self.capture_seconds) - self._dead_time()
                   - SCHEDULE_MARGIN_S - dropped, 0.0)

    def _obs_mode(self):
        if self.sun_gate is None:
            return "always"
        return self._intensity.obs_mode("sun", "sun_auto")

    def _use_exposure(self, seconds):
        if abs(float(self.cam.exposure) - float(seconds)) < 1e-9:
            return True
        try:
            self.cam.set_exposure(float(seconds))
            return True
        except Exception as exc:
            console_ui.warn(f"Exposure {seconds:g} s refused: {exc}")
            return False

    def _capture_tick(self, now):
        """One scheduled tick: a frame, or the sub-frames the split guard asks for. True,
        False on an error, None when the run is stopping.
        """
        entry = TickSlot(exposure=self._base_exposure)
        exposure, frames = self._intensity.plan(entry)
        if not self._use_exposure(exposure):
            exposure, frames = self._base_exposure, 1
            self._intensity.idle()
            if not self._use_exposure(exposure):
                return False
        obs_mode = self._obs_mode()
        outcome = {"stopping": False}

        def capture(index):
            result = self._capture_one(now if index == 1 else dt.now(), obs_mode,
                                       frames, index)
            if result is None:
                outcome["stopping"] = True
                return False, None
            return result, self._last_mean

        ok, mean = shoot_split(frames, capture,
                               lambda: self._stop_event.is_set() or outcome["stopping"])
        if outcome["stopping"] and not ok:
            return None
        self._intensity.feed(entry, exposure, mean, self._last_saturation)
        return ok

    def _capture_one(self, now, obs_mode="always", split_count=1, split_index=1):
        """True once the frame is queued for the disk, False on an error, None when the
        run is stopping.
        """
        timestamp = now.strftime("%Y%m%dT%H%M%S")
        suffix = "_pf" if obs_mode == "sun_auto" else ""
        filepath = unique_path(os.path.join(self.output_dir,
                                            f"{timestamp}{suffix}.fit"))
        try:
            # Started at the tick, not one sitting in the FIFO since the last capture.
            frame = self.cam.grab_fresh_frame()
            debayered = self.debayer and self.cam.is_mosaic()
            self._last_mean, self._last_saturation = frame_stats(frame)
            metadata = frame_metadata(self.cam, now, debayered=debayered)
            metadata["OBSMODE"] = (obs_mode, "always, sun, or sun_auto (preflight)")
            metadata.update(intensity_cards(self._last_mean, split_count, split_index))
            self._last_frame = frame
            part = f", {split_index}/{split_count}" if split_count > 1 else ""
            mean = ("" if self._last_mean is None
                    else f", mean={self._last_mean:.0f}")
            self._writer.submit(filepath, frame, metadata, debayered, now,
                                f"({frame.shape[1]}x{frame.shape[0]}"
                                f"{', RGB' if debayered else ''}, "
                                f"exp={metadata['EXPTIME']}s, "
                                f"period={metadata['PERIOD']}us, "
                                f"gain={self.cam.gain}{mean}{part})")
            return True
        except FrameCancelled:
            return None
        except Exception as exc:
            console_ui.error(f"Capture error: {exc}")
            return False

    def _camera_status(self, force=False):
        """The camera's own status block, at the cadence the status goes out.

        The loop calls _save_status ten times a second. Asking the camera each time puts
        control transfers on the endpoint the readout uses, which shows as dead time that
        varies while the exposure does not.
        """
        now = time.monotonic()
        if (not force and self._cam_status_at is not None
                and now - self._cam_status_at < STATUS_MIN_INTERVAL):
            return self._cam_status
        try:
            self._cam_status = self.cam.get_status_info()
        except Exception:
            self._cam_status = {}
        try:
            self._system_info = get_system_info(self.output_dir)
            console_ui.update(
                disk_free_mb=self._system_info.get("disk_free_mb"))
        except Exception:
            pass
        self._cam_status_at = now
        return self._cam_status

    def _save_status(self, status, force=False):
        self._last_status = status
        # focus_app only sees the values published here, so they follow the status cadence.
        publish_current_params(self._service, self._current_params)
        cam_status = self._camera_status(force)

        payload = {
            "instance_name": self.instance_name,
            "camera_type": "sptt",
            "camera_model": self.cam.model.name,
            "usb_port": self.cam.port,
            "pid": os.getpid(),
            "status": status,
            "output_dir": self.output_dir,
            "shots_taken": self._shots,
            "last_shot": self._last_shot.isoformat() if self._last_shot else None,
            "errors": self._errors,
            "missed": self._missed,
            "setup_mode": self.setup_mode,
            "frame_size": f"{self.cam.w}x{self.cam.h}",
            "exposure_s": self._current_params()["exposure"],
            "period_us": self.cam.period_us,
            "min_period_us": self.cam.min_period_us,
            "trigmode": self.cam.hardware_trigmode,
            "gain": self.cam.gain,
            "binning": self.cam.binning,
            "encoding": "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit",
            "capture_seconds": self.capture_seconds,
            **self._intensity.status_fields(self._last_mean),
            "last_update": dt.now().isoformat(),
        }
        payload.update({f"cam_{k}": v for k, v in cam_status.items()})
        # Cached like the camera status: this stats the output filesystem, which is not
        # free on a network share.
        if self._system_info is not None:
            payload["system"] = self._system_info
        if self.sun_gate is not None:
            payload["sun_altitude"] = self.sun_gate.angle
            payload["sun_max_angle"] = self.sun_gate.max_angle
            console_ui.update(schedule=self.sun_gate.describe())

        console_ui.update(status=status, frames=self._shots, errors=self._errors,
                          missed=self._missed, output_dir=self.output_dir)
        rows = [
            ("Exposure / gain:",
             f"{self._current_params()['exposure']} s  ·  {self.cam.gain}"),
            # On screen because a period shorter than the exposure silently truncates frames.
            ("Frame period:", f"{self.cam.period_us} us  "
                              f"(min {self.cam.min_period_us})"),
            ("Frame size:", f"{self.cam.w}x{self.cam.h}"),
            ("Encoding:", "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit"),
            ("CCD temperature:", str(cam_status.get("temp_ccd", "n/a"))),
            ("Missed captures:", str(self._missed)),
        ]
        control = self._intensity.row(self._last_mean)
        if control:
            rows.append(("Intensity control:", control))
        console_ui.set_section("camera", rows)
        self._bus.publish_status(payload, force=force)


def run_preview_sptt(cam, instance_name):
    from PIL import Image

    preview_path = os.path.join(APP_DIR, f"preview_{instance_name}.png")
    tmp_path = preview_path + ".tmp"
    console_ui.log(f"Preview mode: writing {preview_path} (Ctrl+C to stop)")

    stop = threading.Event()

    def _stop(sig, frame):
        console_ui.log(f"{stop_signal_name(sig)} — stopping preview…")
        stop.set()
    install_stop_handler(_stop)

    cam.start()
    frames = 0
    t0 = dt.now()
    try:
        while not stop.is_set():
            try:
                frame = cam.for_display(cam.grab_frame())
                if frame.dtype == np.uint16:
                    fmax = int(frame.max())
                    if fmax > 0:
                        display = (frame.astype(np.float32) / fmax * 255).astype(np.uint8)
                    else:
                        display = frame.astype(np.uint8)
                else:
                    display = frame
                img = Image.fromarray(display, mode="RGB" if display.ndim == 3 else "L")
                img.save(tmp_path, format="PNG")
                os.replace(tmp_path, preview_path)
                frames += 1
                if frames % 10 == 0:
                    elapsed = (dt.now() - t0).total_seconds()
                    fps = frames / elapsed if elapsed > 0 else 0
                    console_ui.log(f"Preview: {frames} frames, {fps:.1f} FPS")
            except Exception as exc:
                console_ui.error(f"Preview frame error: {exc}")
                time.sleep(0.1)
    finally:
        cam.stop()


def bayer_pattern_setting(sptt_cfg):
    pattern = str(sptt_cfg.get("bayer_pattern") or DEFAULT_BAYER_PATTERN).upper()
    if pattern not in BAYER_PATTERNS:
        raise ValueError(f"sptt.bayer_pattern {pattern!r} is not one of "
                         f"{', '.join(BAYER_PATTERNS)}")
    return pattern


def usb_port_setting(sptt_cfg):
    """Empty means the only camera on this machine."""
    return str(sptt_cfg.get("usb_port") or "").strip() or None


def run_probe_sptt(config_path=None):
    """Opens nothing and loads no firmware, so it is safe while other runs use their
    cameras. The port column is what goes into sptt.usb_port.
    """
    from utils import load_config
    configured = usb_port_setting(load_config(config_path).get("sptt", {}))
    cameras = list_cameras(find_libusb_backend())
    if not cameras:
        print("No SPTT camera found.")
        return
    print(f"{'USB port':<12} {'Model':<10} State")
    for cam in cameras:
        state = "firmware loaded" if cam.configured else "no firmware yet"
        mark = "   <- sptt.usb_port of this config" if cam.port == configured else ""
        print(f"{cam.port:<12} {cam.model.name:<10} {state}{mark}")
    if len(cameras) > 1 and not configured:
        print("\nSeveral cameras: give each run its own config with "
              "sptt.usb_port set to one of the ports above.")


def colour_offsets(sptt_cfg):
    """Unset means the camera keeps its own; on a grey camera they are simply unused."""
    return {key: None if sptt_cfg.get(key) is None else int(sptt_cfg[key])
            for key in ("r_offset", "g_offset")}


def make_camera(backend, found, sptt_cfg):
    cam = SpttCamera(backend, found.model, found.port)
    cam.bayer_pattern = bayer_pattern_setting(sptt_cfg)
    return cam


def run_console_sptt(config_path=None, preview=False, verbose=False,
                     setup_mode=False):
    from utils import load_config, get_node_name

    cfg = load_config(config_path)
    sptt_cfg = cfg.get("sptt", {})

    node_name = get_node_name(cfg)
    claim = claim_instance_name(sptt_cfg.get("instance_name")
                                or get_instance_name("SPTT", cfg))
    instance_name = claim.name
    output_dir = sptt_cfg.get("output_dir", "")
    status_dir = cfg.get("status_dir") or str(Path.home() / ".every_camera" / "status")
    exposure = sptt_cfg.get("exposure", 0.88)
    gain = sptt_cfg.get("gain", 100)
    binning = sptt_cfg.get("binning", 0)
    encoding = sptt_cfg.get("encoding", ENCODING_12BPP)
    target_temp = sptt_cfg.get("target_temp")
    period_us = sptt_cfg.get("period_us")
    trigmode = sptt_cfg.get("trigmode")
    firmware_dir = sptt_cfg.get("firmware_dir", "")
    capture_seconds = sptt_cfg.get("capture_seconds", SPTT_CAPTURE_SECONDS)

    dash = console_ui.start_dashboard("sptt", instance_name, verbose=verbose)
    # Before the camera is opened, so a camera that will not open still raises an alert.
    # The crash handler goes first: it alone can speak for a process killed outright.
    alert_status_path = os.path.join(status_dir, f"{os.getpid()}.json")
    alerts.install_crash_handler(alert_status_path)
    alerts.start(cfg.get("alerts", {}), node_name, instance_name, "sptt",
                 log_path=console_ui.default_log_path("sptt", instance_name),
                 status_path=alert_status_path)

    dash.update(status="starting", node_name=node_name, output_dir=output_dir,
                frames=0, errors=0,
                schedule=f"capture at {capture_seconds} s of each minute")
    dash.set_section("device", [
        ("Exposure:", f"{exposure} s"),
        ("Frame period:", f"{period_us} us" if period_us else
                          f"auto ({PERIOD_READOUT_MARGIN_US} us over exposure)"),
        ("Gain:", str(gain)),
        ("Binning:", str(binning)),
        ("Encoding:", "12bit" if encoding == ENCODING_12BPP else "8bit"),
    ])
    try:
        _run_console_sptt(cfg, sptt_cfg, config_path, preview, dash,
                          instance_name, node_name, output_dir, status_dir,
                          exposure, gain, binning, encoding, target_temp,
                          capture_seconds, setup_mode, period_us, trigmode,
                          firmware_dir)
    finally:
        dash.stop()
        claim.release()


def _run_console_sptt(cfg, sptt_cfg, config_path, preview, dash,
                      instance_name, node_name, output_dir, status_dir,
                      exposure, gain, binning, encoding, target_temp,
                      capture_seconds, setup_mode=False, period_us=None,
                      trigmode=None, firmware_dir=""):
    if preview:
        console_ui.log("Initializing SPTT camera...")
        backend = find_libusb_backend()
        found = ensure_firmware_loaded(backend, firmware_dir,
                                       usb_port_setting(sptt_cfg))
        if not found:
            console_ui.error("Failed to initialize camera.")
            sys.exit(1)
        time.sleep(1)
        try:
            cam = make_camera(backend, found, sptt_cfg)
            cam.open()
            cam.configure(exposure=exposure, gain=gain, binning=binning,
                          encoding=encoding, target_temp=target_temp,
                          period_us=period_us, trigmode=trigmode,
                          **colour_offsets(sptt_cfg))
            console_ui.log(f"Camera ready: {cam.w}x{cam.h}")
        except Exception as exc:
            console_ui.error(f"Failed to configure camera: {exc}")
            sys.exit(1)
        try:
            run_preview_sptt(cam, instance_name)
        finally:
            cam.close()
        console_ui.log("Done.")
        return

    if not output_dir:
        console_ui.log("Configuration incomplete. Starting setup wizard...")
        from utils import configure_console_sptt
        with dash.suspended():          # the wizard needs a plain terminal
            configure_console_sptt(cfg, config_path)
        sptt_cfg = cfg.get("sptt", {})
        output_dir = sptt_cfg.get("output_dir", "")
        exposure = sptt_cfg.get("exposure", 0.88)
        gain = sptt_cfg.get("gain", 100)
        binning = sptt_cfg.get("binning", 0)
        encoding = sptt_cfg.get("encoding", ENCODING_12BPP)
        target_temp = sptt_cfg.get("target_temp")
        period_us = sptt_cfg.get("period_us")
        trigmode = sptt_cfg.get("trigmode")
        firmware_dir = sptt_cfg.get("firmware_dir", "")
        capture_seconds = sptt_cfg.get("capture_seconds", SPTT_CAPTURE_SECONDS)
        dash.update(output_dir=output_dir,
                    schedule=f"capture at {capture_seconds} s of each minute")

    if not output_dir:
        console_ui.error("output_dir is required.")
        sys.exit(1)

    preflight, overexposure, problems = intensity_settings(sptt_cfg)
    for problem in problems:
        console_ui.warn(problem)
    try:
        sun_gate = None if setup_mode else gate_from_config(
            sptt_cfg,
            start_angle=preflight.sun_start_angle if preflight.enabled else None)
    except SunSettingsError as exc:
        console_ui.error(f"sptt: {exc}")
        sys.exit(1)

    if setup_mode:
        announce_setup_mode(None)
        dash.update(schedule="setup mode — no scheduled captures")

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(status_dir, exist_ok=True)

    console_ui.log("Initializing SPTT camera...")
    backend = find_libusb_backend()
    found = ensure_firmware_loaded(backend, firmware_dir,
                                   usb_port_setting(sptt_cfg))
    if not found:
        console_ui.error("Failed to initialize camera.")
        sys.exit(1)
    time.sleep(1)

    try:
        cam = make_camera(backend, found, sptt_cfg)
        cam.open()
        sl = cam.configure(
            exposure=exposure, gain=gain, binning=binning, encoding=encoding,
            target_temp=target_temp, period_us=period_us, trigmode=trigmode,
            **colour_offsets(sptt_cfg),
        )
        # configure() has already refused anything the camera did not take.
        console_ui.log(f"{found.model.name} (USB port {found.port}) ready: "
                       f"{cam.w}x{cam.h} — "
                       f"exposure {sl[5] / 1e6:.3f} s, period {sl[6]} us "
                       f"(firmware minimum {sl[14]} us)")
    except Exception as exc:
        console_ui.error(f"Failed to configure camera: {exc}")
        sys.exit(1)


    # A frame server failure is never fatal.
    from camera_service import CameraService
    from frame_server import start_frame_server
    service = CameraService("sptt", instance_name, output_dir,
                            node_name=node_name, setup_mode=setup_mode)
    server = start_frame_server(cfg.get("server", {}), service)
    if server:
        dash.update(server_url=server.url)
        if setup_mode:
            console_ui.log(f"Focus this camera with: python focus_app.py "
                           f"--host {get_local_ip()} --port {server.port}")

    worker = SpttWorkerConsole(
        cam=cam,
        output_dir=output_dir,
        instance_name=instance_name,
        status_dir=status_dir,
        capture_seconds=capture_seconds,
        service=service,
        node_name=node_name,
        setup_mode=setup_mode,
        debayer=sptt_cfg.get("debayer", False),
        sun_gate=sun_gate,
        preflight=preflight,
        overexposure=overexposure,
    )

    def _stop(sig, frame):
        console_ui.log(f"{stop_signal_name(sig)} — stopping…")
        worker.request_stop()
    install_stop_handler(_stop)

    console_ui.log("Starting. Press Ctrl+C to stop.")
    try:
        worker.start()
        while worker.is_alive():
            worker.join(timeout=0.5)
    finally:
        cam.close()
        if server:
            server.stop()
    if worker.failed:
        # Non-zero so that systemd's Restart=on-failure restarts a camera that gave up.
        console_ui.error("SPTT measurement gave up — exiting with an error.")
        sys.exit(1)
    console_ui.log("Done.")
