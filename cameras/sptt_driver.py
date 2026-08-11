"""
SPTT (CSDU-429) camera driver: firmware loading, capture, FITS output, workers.
Uses pyusb for camera control.
"""
import os
import sys
import json
import time
import struct
import threading
import argparse

import console_ui

import numpy as np

from datetime import datetime as dt
from pathlib import Path

import frame_archive
import intensity

from utils import (
    claim_instance_name, get_instance_name, get_local_ip, get_system_info,
    APP_DIR,
)
from worker_common import (
    WorkerMqtt, parse_command_params, publish_current_params,
    publish_schedule_state, serving_focus_hold,
    run_focus_iteration, MQTT_MAX_PAYLOAD_BYTES, announce_setup_mode,
    SETUP_STATUS, install_stop_handler, stop_signal_name,
)

from .sptt_load_firmware import (
    VID, PID_RAW, PID_CONFIGURED,
    find_libusb_backend, load_firmware_files, detach_kernel_driver,
    load_fx2_firmware, wait_for_configured_device, load_fpga_bitstream,
)
from .sptt_timing import (
    exposure_to_us, derive_period_us, exposure_mismatch, period_mismatch,
    describe_timing, PERIOD_READOUT_MARGIN_US, CMD_VALUE_MAX,
)

import usb.core
import usb.util

# ---------------------------------------------------------------------------
# Camera command IDs (CSDU-429 protocol)
# ---------------------------------------------------------------------------
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

USB_CMD_TIMEOUT = 10000
USB_READ_TIMEOUT = 10000

# Default capture seconds (can be overridden in config)
SPTT_CAPTURE_SECONDS = [0, 30]

# Argument default meaning "leave this setting as the camera already has it".
# configure() is called again whenever binning or encoding changes, and a plain
# default would silently revert the operator's period and trigger mode there.
KEEP = object()


class ExposureNotHonoured(RuntimeError):
    """The camera did not take the exposure or period it was given.

    Raised rather than logged because a silently shortened exposure corrupts
    every measurement taken under it while looking like a working night — the
    same reasoning as the ASI driver's refusal to clamp (cameras/asi/camera.py).
    """


def make_command(cmd_id, value=0):
    # Checked here rather than in the callers because this is where a value too
    # wide for the wire would be masked into something small and plausible.
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
    status_list = struct.unpack_from('=BBHHHIIBHHHHBIIHIHHBBIHBBB', array)
    return array[0], status_list


def read_raw_frame(size, ep_tr):
    chunks = []
    remaining = size
    while remaining > 0:
        length = min(512, remaining)
        chunks.append(_usb_read_retry(ep_tr, 512))
        remaining -= length
    return chunks


def decode_frame(raw_chunks, w, h, encoding, binning=0):
    """Unpack one raw USB transfer into a frame on the program's 0..65535 scale.

    The sensor digitises 12 bits (or 8, in the low-bandwidth encoding); this is
    the one place that knows it, so the shift up to full scale happens here and
    nothing downstream has to ask how deep a SPTT pixel is.
    """
    raw = []
    for buf in raw_chunks:
        raw.extend(buf)

    if encoding == ENCODING_12BPP:
        pixels = []
        for i in range(0, len(raw) - 2, 3):
            b0, b1, b2 = raw[i], raw[i+1], raw[i+2]
            pixels.append((b0 << 4) | (b2 & 0x0F))
            pixels.append((b1 << 4) | ((b2 >> 4) & 0x0F))
    else:
        pixels = raw

    dtype = np.uint16
    bits = 12 if encoding == ENCODING_12BPP else 8

    if binning > 0:
        arr = np.array(pixels[:w * h], dtype=dtype)
        if len(arr) < w * h:
            arr = np.pad(arr, (0, w * h - len(arr)))

        return intensity.to_full_scale(arr.reshape(h, w), bits)

    frame = [0] * (w * h)
    for i in range(h):
        src_row = i // 2 if i % 2 == 0 else h // 2 + i // 2
        src_off = src_row * w
        dst_off = i * w
        for j in range(w):
            idx = src_off + j
            if idx < len(pixels):
                frame[dst_off + j] = pixels[idx]

    return intensity.to_full_scale(
        np.array(frame, dtype=dtype).reshape(h, w), bits)


# ---------------------------------------------------------------------------
# Camera class
# ---------------------------------------------------------------------------
class SpttCamera:
    """Manages USB connection and CSDU-429 camera operations."""

    def __init__(self, backend):
        self.backend = backend
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
        # Frame timing. period_us is what the camera was last told; the
        # override is the operator's sptt.period_us (None = derive it from the
        # exposure), and min_period_us is the floor the firmware reports for
        # the current binning and ROI.
        self.period_us = None
        self.period_override_us = None
        self.trigmode = 0
        self.min_period_us = 0
        # A setting changed, so the frame the sensor is part-way through began
        # under the old one and has to be thrown away before anything is read.
        self._params_dirty = False
        # When the integration behind the last frame was allowed to start, and
        # how long the frame took to arrive after that — the archived DATE-OBS
        # comes from the first of these.
        self.last_frame_started = None
        self.last_frame_lag = None
        self._armed_at = None

    def open(self):
        self.dev = usb.core.find(idVendor=VID, idProduct=PID_CONFIGURED, backend=self.backend)
        if not self.dev:
            raise RuntimeError("Configured camera not found!")
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            self.dev.reset()
            time.sleep(0.5)
            self.dev = usb.core.find(idVendor=VID, idProduct=PID_CONFIGURED, backend=self.backend)
            if not self.dev:
                raise RuntimeError("Device lost after reset!")
        except usb.core.USBError:
            pass
        self.dev.set_configuration()
        cfg = self.dev.get_active_configuration()
        self.ep_wr = cfg.interfaces()[0][0]
        self.ep_rd = cfg.interfaces()[0][1]
        self.ep_tr = cfg.interfaces()[0][2]

    def close(self):
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
        """Apply all camera parameters. Exposure is in seconds.

        ``period_us`` and ``trigmode`` default to :data:`KEEP` so that the
        reconfigure a binning change triggers inherits what the operator set;
        pass ``None`` for either to mean "derive it" / "continuous".
        """
        wr = self._write_cmd
        self.exposure = exposure
        self.gain = gain
        self.binning = binning
        self.encoding = encoding
        if period_us is not KEEP:
            self.period_override_us = period_us
        if trigmode is not KEEP:
            self.trigmode = 0 if trigmode is None else int(trigmode)
        self._params_dirty = True

        exposure_us = exposure_to_us(exposure)

        wr(CMD_SET_BINNING, binning)
        if roi_org is not None:
            h_org = roi_org[0] & ~1
            v_org = roi_org[1] & ~1
            wr(CMD_SET_ROI_ORG, (h_org << 16) | v_org)
        if roi_size is not None:
            h_size = roi_size[0] & ~3
            v_size = roi_size[1] & ~3
            wr(CMD_SET_ROI_SIZE, (h_size << 16) | v_size)

        wr(CMD_SET_ENCODING, encoding)
        wr(CMD_SET_EXP, exposure_us)
        wr(CMD_SET_GAIN, gain)
        if r_offset is not None:
            wr(CMD_SET_R_OFFSET, r_offset)
        if g_offset is not None:
            wr(CMD_SET_G_OFFSET, g_offset)
        wr(CMD_SET_TRIGMODE, self.trigmode)
        wr(CMD_SET_DRAFT, 1 if draft else 0)
        if target_temp is not None:
            wr(CMD_SET_TARGET_TEMP, target_temp & 0xFF)

        # Read back before the period is sent: the firmware's minimum period
        # depends on the frame size, so it only means anything once binning,
        # ROI and encoding are in force.
        _, sl = read_crb(self.ep_wr, self.ep_rd)
        self.w = sl[17]
        self.h = sl[18]
        self.min_period_us = sl[14]
        return self._apply_period()

    def _write_cmd(self, cmd_id, value=0):
        _usb_write_retry(self.ep_wr, make_command(cmd_id, value))

    def _apply_period(self, verify=True):
        """Send the frame period that matches the exposure now in force.

        The single place a period is computed or written. Every path that
        changes the exposure ends here, because a period left behind at its old
        value truncates the new exposure without saying so.
        """
        exposure_us = exposure_to_us(self.exposure)
        period = derive_period_us(
            self.exposure,
            min_period_us=self.min_period_us,
            override=self.period_override_us,
            # Outside continuous mode the period does not gate the integration,
            # so an operator asking for a short one gets it.
            allow_short=(self.trigmode != 0),
            warn=console_ui.warn)
        self._write_cmd(CMD_SET_PERIOD, period)
        self.period_us = period

        _, sl = read_crb(self.ep_wr, self.ep_rd)
        self.min_period_us = sl[14]
        if verify:
            self._verify_timing(exposure_us, sl)
        return sl

    def _verify_timing(self, requested_exposure_us, sl):
        """Check what the camera says it is doing against what it was told."""
        console_ui.log(describe_timing(requested_exposure_us, sl))
        problems = [p for p in (exposure_mismatch(requested_exposure_us, sl[5]),
                                period_mismatch(sl[5], sl[6])) if p]
        if problems:
            for problem in problems:
                console_ui.warn(problem)
            raise ExposureNotHonoured("; ".join(problems))

    def set_exposure(self, value):
        """Set exposure in seconds.

        The period follows it. Leaving the old, shorter one in place is what
        made a live exposure change do nothing visible to the frames.
        """
        exposure_us = exposure_to_us(value)
        self._write_cmd(CMD_SET_EXP, exposure_us)
        self.exposure = exposure_us / 1_000_000.0
        self._params_dirty = True
        self._apply_period()

    def set_gain(self, value):
        self.gain = value
        self._write_cmd(CMD_SET_GAIN, value)
        self._params_dirty = True

    def _flush_endpoints(self):
        for ep in (self.ep_rd, self.ep_tr):
            for _ in range(64):
                try:
                    ep.read(size_or_buffer=512, timeout=50)
                except usb.core.USBError:
                    break

    def start(self, retries=3):
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
                self.last_frame_started = dt.now()
                self._armed_at = time.monotonic()
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

    def stop(self):
        if self._running:
            try:
                _usb_write_retry(self.ep_wr, make_command(CMD_CAM_STOP))
            except usb.core.USBError:
                pass
            self._running = False

    def _frame_wait_budget(self):
        """How long one frame may take before something is wrong.

        Worst case the wait starts just after an integration did, so a whole
        period can pass before the next one even begins. The old
        ``exposure + 5`` was written when the period was always 75 ms.
        """
        period_s = (self.period_us or 0) / 1_000_000.0
        return max(period_s + self.exposure + 5.0, 3.0)

    def _rearm(self):
        """Drop whatever is queued and note when the next frame may start."""
        _usb_write_retry(self.ep_wr, make_command(CMD_FIFO_INIT))
        self.last_frame_started = dt.now()
        self._armed_at = time.monotonic()

    def _wait_for_fifo(self, budget):
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                sb, _ = read_crb(self.ep_wr, self.ep_rd)
            except usb.core.USBError:
                time.sleep(0.01)
                continue
            if not (sb & 0x08):
                return
            time.sleep(0.05)
        raise RuntimeError("FIFO timeout — no frame data")

    def _read_one_frame(self):
        # Update frame dimensions based on binning
        BINNING_SIZES = {0: (744, 576), 1: (372, 288), 3: (188, 144)}
        if self.binning in BINNING_SIZES:
            self.w, self.h = BINNING_SIZES[self.binning]

        w, h = self.w, self.h

        if self.encoding == ENCODING_12BPP:
            frame_size = w * h * 3 // 2
        else:
            frame_size = w * h

        _usb_write_retry(self.ep_wr, make_command(CMD_READ_PREPARE, frame_size))
        raw_chunks = read_raw_frame(frame_size, self.ep_tr)
        _usb_write_retry(self.ep_wr, make_command(CMD_FIFO_INIT))
        if self._armed_at is not None:
            self.last_frame_lag = time.monotonic() - self._armed_at
        return decode_frame(raw_chunks, w, h, self.encoding, self.binning)

    def grab_frame(self):
        """One frame, integrated under the settings currently in force.

        After a parameter change the sensor is already part-way through a frame
        that began under the old ones; it is discarded here so that no caller
        has to know the difference. This is why the fix reaches the Qt worker
        in gui_app.py without that copy being touched.
        """
        if self._params_dirty:
            self._rearm()
            self._wait_for_fifo(self._frame_wait_budget())
            self._read_one_frame()          # the in-flight, old-settings frame
            self._params_dirty = False

        self._wait_for_fifo(self._frame_wait_budget())
        return self._read_one_frame()

    def grab_fresh_frame(self):
        """A frame whose integration was allowed to start at this call.

        The camera free-runs, so between two scheduled shots the FIFO fills
        with frames up to half a minute old; archiving one of those under the
        tick's DATE-OBS records a time the frame was never taken at.
        """
        self._rearm()
        return self.grab_frame()

    def get_status(self):
        return read_crb(self.ep_wr, self.ep_rd)

    def get_status_info(self):
        """Get detailed status dict for monitoring."""
        try:
            sb, sl = read_crb(self.ep_wr, self.ep_rd)
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
                "frame_w": sl[17],
                "frame_h": sl[18],
                "fifo_cnt": sl[13],
                "temp_sink": sl[23],
                "temp_ccd": sl[24],
                "temp_target": sl[25],
            }
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Firmware loading
# ---------------------------------------------------------------------------
def ensure_firmware_loaded(backend):
    """Load firmware if device is not yet configured."""
    dev = usb.core.find(idVendor=VID, idProduct=PID_CONFIGURED, backend=backend)
    if dev:
        console_ui.log(f"Camera already configured (PID=0x{PID_CONFIGURED:04x}), skipping firmware load.")
        usb.util.dispose_resources(dev)
        return True

    dev_raw = usb.core.find(idVendor=VID, idProduct=PID_RAW, backend=backend)
    if not dev_raw:
        console_ui.error("No SPTT camera found.")
        return False

    console_ui.log("Loading firmware files…")
    fx2_data, fpga_data = load_firmware_files()

    console_ui.log(f"Found raw FX2 device: {VID:04x}:{PID_RAW:04x}")
    detach_kernel_driver(dev_raw)
    load_fx2_firmware(dev_raw, fx2_data)

    console_ui.log("Sending USB bus reset…")
    try:
        dev_raw.reset()
    except usb.core.USBError as e:
        console_ui.log(f"USB reset returned: {e} (expected)")
    usb.util.dispose_resources(dev_raw)
    del dev_raw

    dev = wait_for_configured_device(backend)
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

    dev_final = wait_for_configured_device(backend, timeout=10.0)
    if dev_final:
        console_ui.log("Firmware loaded successfully.")
        usb.util.dispose_resources(dev_final)
        return True

    console_ui.warn("Device not found after initialization.")
    return False


# ---------------------------------------------------------------------------
# FITS file writing
# ---------------------------------------------------------------------------
def frame_metadata(cam, when, status=None):
    """The FITS header for one archived SPTT frame.

    Records what the camera says it did as well as what it was asked for. The
    two used to be assumed equal, which is how a driver that cut every 0.88 s
    exposure down to 75 ms went on filing frames stamped ``EXPTIME = 0.88``.
    ``EXPTIME`` therefore now carries the hardware's own figure, since that is
    the number any photometry has to divide by, and ``EXPREQ`` keeps the
    request. Every key stays within the eight characters FITS allows.
    """
    if status is None:
        status = cam.get_status_info()
    metadata = {
        "DATE-OBS": (cam.last_frame_started or when).isoformat(),
        "INSTRUME": "CSDU-429",
        "EXPTIME": status.get("exposure_s", cam.exposure),
        "EXPREQ": cam.exposure,
        "PERIOD": int(status.get("period_us", cam.period_us or 0)),
        "MINPERD": int(status.get("min_period_us", cam.min_period_us or 0)),
        "TRIGMODE": int(cam.trigmode),
        "GAIN": cam.gain,
        "BINNING": cam.binning,
        "ENCODING": "12bit" if cam.encoding == ENCODING_12BPP else "8bit",
        # ENCODING says how the sensor digitised; this says what scale the
        # pixels in this file are actually on, which is the same for every
        # camera in this program.
        "ADCFULL": intensity.FULL_SCALE,
    }
    if cam.last_frame_lag is not None:
        metadata["FRAMELAG"] = round(cam.last_frame_lag, 3)
    if status:
        metadata["CCDTEMP"] = status.get("temp_ccd", 0)
        metadata["SINKTEMP"] = status.get("temp_sink", 0)
        metadata["TRGTEMP"] = status.get("temp_target", 0)
    return metadata


def save_fits(filepath, frame, metadata=None):
    """Save frame as FITS file with metadata in header."""
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
    """Minimal FITS writer without astropy."""
    import struct as st

    h, w = frame.shape
    bitpix = 16 if frame.dtype in (np.uint16, np.int16) else 8
    # FITS has no unsigned 16-bit type: the values go out as signed shorts with
    # an offset, which is what astropy writes for a uint16 array too. Without
    # this the frame silently wrapped into negative numbers above 32767 —
    # harmless while the sensor filled only 12 bits, wrong now that it fills 16.
    unsigned = bitpix == 16 and frame.dtype == np.uint16

    # Build header
    cards = []
    cards.append(f"SIMPLE  =                    T / FITS standard")
    cards.append(f"BITPIX  = {bitpix:>20d} / bits per pixel")
    cards.append(f"NAXIS   =                    2 / number of axes")
    cards.append(f"NAXIS1  = {w:>20d} / width")
    cards.append(f"NAXIS2  = {h:>20d} / height")
    if unsigned:
        cards.append(f"BZERO   = {32768:>20d} / offset for unsigned 16-bit data")
        cards.append(f"BSCALE  = {1:>20d} / linear scaling factor")

    if metadata:
        for key, value in metadata.items():
            fits_key = key[:8].upper().ljust(8)
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

    # Pad header to multiple of 2880 bytes
    header_str = ""
    for card in cards:
        header_str += card.ljust(80)
    while len(header_str) % 2880 != 0:
        header_str += " " * 80

    # Write
    if bitpix == 16:
        values = (frame.astype(np.int32) - 32768) if unsigned else frame
        data = values.astype(">i2").tobytes()
    else:
        data = frame.astype(np.uint8).tobytes()

    # Pad data to multiple of 2880
    pad_len = (2880 - len(data) % 2880) % 2880
    data += b'\x00' * pad_len

    with open(filepath, "wb") as f:
        f.write(header_str.encode("ascii"))
        f.write(data)


# ---------------------------------------------------------------------------
# Console worker
# ---------------------------------------------------------------------------
class SpttWorkerConsole(threading.Thread):
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, cam, output_dir, instance_name, status_dir,
                 capture_seconds=None, mqtt_publisher=None,
                 mqtt_prefix="every_camera", service=None, node_name="",
                 setup_mode=False):
        super().__init__(daemon=True)
        self.cam = cam
        self.output_dir = output_dir
        self.instance_name = instance_name
        self.status_dir = status_dir
        self.capture_seconds = sorted(capture_seconds or SPTT_CAPTURE_SECONDS)
        self.setup_mode = bool(setup_mode)
        self._service = service
        self._bus = WorkerMqtt("sptt", instance_name, status_dir,
                               mqtt_publisher, mqtt_prefix, service=service,
                               node_name=node_name)
        self._stop_event = threading.Event()
        self._shots = 0
        self._errors = 0
        self._last_shot = None
        self._last_frame = None
        # Whether the last loop pass ran under a focus hold, so the transitions
        # in and out of it are logged once rather than ten times a second.
        self._held = False
        self._pending_capture = None
        self._pending_capture_lock = threading.Lock()
        self._pending_capture_event = threading.Event()

    def request_stop(self):
        self._stop_event.set()

    def _encode_jpeg(self, frame):
        """Encode a frame, downscaling until it fits the broker payload cap."""
        if frame.ndim != 2:
            raise ValueError(f"Unexpected frame shape: {frame.shape}")
        return frame_archive.to_jpeg_capped(frame, MQTT_MAX_PAYLOAD_BYTES)

    def _publish_frame_ok(self, jpeg_bytes, w, h, ts_iso,
                          on_demand=False, params=None):
        self._bus.publish_frame_jpeg(jpeg_bytes, ts_iso, on_demand=on_demand,
                                     params=params,
                                     extra={"width": w, "height": h})

    def _publish_frame_error(self, status, error, ts_iso=None, on_demand=False):
        self._bus.publish_error(status, error, ts_iso=ts_iso,
                                on_demand=on_demand)

    def _apply_params(self, params):
        applied = {}
        errors = []
        try:
            if "exposure" in params:
                self.cam.set_exposure(float(params["exposure"]))
                applied["exposure"] = float(params["exposure"])
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
                    # configure() refuses a timing the camera did not take, so
                    # the restart has to happen either way — a rejected change
                    # must not leave the schedule with a stopped camera.
                    self.cam.start()
                applied["binning"] = binning
                applied["encoding"] = "12bit" if encoding == ENCODING_12BPP else "8bit"
        except Exception as e:
            errors.append(str(e))
        return applied, errors

    def _handle_pending_capture(self):
        with self._pending_capture_lock:
            params = self._pending_capture
            self._pending_capture = None
        self._pending_capture_event.clear()
        if params is None:
            return
        console_ui.log("On-demand SPTT capture starting")
        self._bus.publish_note("capturing", f"Applying params: {params}")
        try:
            applied, errors = self._apply_params(params)
            for err in errors:
                console_ui.warn(f"Param apply: {err}")
            # Whoever asked is waiting for a frame that reflects what they
            # asked for, not the one already in flight.
            frame = self.cam.grab_fresh_frame()
            now = dt.now()
            self._push_live_frame(frame, now)
            jpeg_bytes, w, h = self._encode_jpeg(frame)
            self._publish_frame_ok(
                jpeg_bytes, w, h, now.isoformat(),
                on_demand=True, params=applied)
            console_ui.log("On-demand frame sent via MQTT")
        except Exception as e:
            self._publish_frame_error("error", f"Capture failed: {e}",
                                      on_demand=True)
            console_ui.error(f"On-demand capture error: {e}")

    def _on_mqtt_command(self, topic, payload):
        """Handle incoming MQTT commands (get_frame, capture_frame)."""
        console_ui.log(f"MQTT cmd received: {topic} "
                       f"({len(payload) if payload else 0} bytes)")
        if not self._bus.enabled:
            return
        if topic.endswith("/cmd/get_frame"):
            frame = self._last_frame
            ts_iso = self._last_shot.isoformat() if self._last_shot else None
            if frame is None:
                self._publish_frame_error("no_frame", "No frame captured yet")
                console_ui.warn("Frame requested but no frame available yet")
                return
            try:
                jpeg_bytes, w, h = self._encode_jpeg(frame)
            except Exception as e:
                self._publish_frame_error("error", str(e), ts_iso)
                console_ui.error(f"Frame encode error: {e}")
                return
            self._publish_frame_ok(jpeg_bytes, w, h, ts_iso)
            console_ui.log("Frame sent via MQTT")
            return

        if topic.endswith("/cmd/capture_frame"):
            params, err = parse_command_params(payload)
            if err:
                self._publish_frame_error("bad_request", err, on_demand=True)
                return
            with self._pending_capture_lock:
                self._pending_capture = params
            self._pending_capture_event.set()
            self._bus.publish_note("accepted",
                                   f"Request queued with params: {params}")
            console_ui.log(f"On-demand capture queued with params: {params}")
            return
        console_ui.log(f"Unknown command: {topic}")

    # ------------------------------------------------------------------
    # Live view for the LAN focus tool
    # ------------------------------------------------------------------
    def _push_live_frame(self, frame, ts):
        if self._service is not None:
            try:
                self._service.publish_frame(frame, ts)
            except Exception:
                pass

    def _current_params(self):
        return {
            "exposure": self.cam.exposure,
            "gain": self.cam.gain,
            "binning": self.cam.binning,
            "encoding": "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit",
        }

    def _focus_holding(self):
        """True while a focus session has asked the schedule to stand down."""
        return self._service is not None and self._service.focus_hold()

    def _handle_focus(self, now):
        """Grab one extra frame for the focus tool.

        Held sessions get every tick, the same as setup mode: the operator has
        agreed to interrupt the measurements, so there is no capture second to
        keep clear any more.
        """
        if self._service is None or not self._service.focus_active():
            return
        if (now.second in self.capture_seconds and not self.setup_mode
                and not self._focus_holding()):
            return
        req_id, params = self._service.take_param_request()
        if params:
            applied, errors = self._apply_params(params)
            self._service.complete_param_request(req_id, applied, errors)
            self._service.set_current_params(self._current_params())
        # Only the frame right after a change is worth waiting a full period
        # for; grab_frame already drops the contaminated one either way, so the
        # rest of the focus loop keeps its rate.
        grab = self.cam.grab_fresh_frame if params else self.cam.grab_frame
        run_focus_iteration(
            self._service, grab,
            on_error=lambda exc, stopped: console_ui.warn(
                          f"Focus frame failed: {exc}"
                          f"{' — focus mode disabled' if stopped else ''}"))

    def run(self):
        last_fired = (-1, -1)
        consecutive_errors = 0
        self._bus.prepare_status_dir()
        self._bus.subscribe(self._on_mqtt_command)
        if self._service is not None:
            self._service.set_current_params(self._current_params())

        if self.setup_mode:
            console_ui.log("SPTT ready for focusing (setup mode) — nothing is archived")
            self._save_status(SETUP_STATUS, force=True)
        else:
            console_ui.log("SPTT measurement started (captures at :00 and :30)")
            self._save_status("running", force=True)

        # Start continuous capture
        try:
            self.cam.start()
        except Exception as exc:
            console_ui.error(f"Failed to start camera: {exc}")
            self._save_status("error", force=True)
            self._bus.shutdown()
            return

        while not self._stop_event.is_set():
            # Handle on-demand capture requests (outside schedule)
            if self._pending_capture_event.is_set():
                self._handle_pending_capture()

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
            if now.second in self.capture_seconds and fire_key != last_fired:
                last_fired = fire_key
                ok = self._capture_one(now)
                if ok:
                    consecutive_errors = 0
                    self._shots += 1
                    self._last_shot = now
                    self._save_status("running", force=True)
                else:
                    consecutive_errors += 1
                    self._errors += 1
                    self._save_status("error", force=True)
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        console_ui.error(f"{consecutive_errors} consecutive errors, stopping")
                        break
            elif now.second not in self.capture_seconds:
                last_fired = (-1, -1)
                self._handle_focus(now)

            self._save_status("running")
            self._stop_event.wait(0.1)

        self.cam.stop()
        self._save_status("stopped", force=True)
        self._bus.shutdown()
        console_ui.log("SPTT measurement stopped")

    def _capture_one(self, now):
        timestamp = now.strftime("%Y%m%dT%H%M%S")
        filepath = os.path.join(self.output_dir, f"{timestamp}.fit")
        try:
            # A frame started at the tick, not whichever one has been sitting
            # in the FIFO since the last capture half a minute ago.
            frame = self.cam.grab_fresh_frame()
            metadata = frame_metadata(self.cam, now)

            save_fits(filepath, frame, metadata)
            self._last_frame = frame
            self._push_live_frame(frame, now)
            console_ui.log(f"Frame saved: {os.path.basename(filepath)} "
                           f"({frame.shape[1]}x{frame.shape[0]}, "
                           f"exp={metadata['EXPTIME']}s, "
                           f"period={metadata['PERIOD']}us, "
                           f"gain={self.cam.gain})")
            return True
        except Exception as exc:
            console_ui.error(f"Capture error: {exc}")
            return False

    def _save_status(self, status, force=False):
        # focus_app only sees the values published from here, so they are
        # refreshed on the status cadence rather than left as they were at
        # startup — see worker_common.publish_current_params.
        publish_current_params(self._service, self._current_params)
        cam_status = {}
        try:
            cam_status = self.cam.get_status_info()
        except Exception:
            pass

        payload = {
            "instance_name": self.instance_name,
            "camera_type": "sptt",
            "pid": os.getpid(),
            "status": status,
            "output_dir": self.output_dir,
            "shots_taken": self._shots,
            "last_shot": self._last_shot.isoformat() if self._last_shot else None,
            "errors": self._errors,
            "setup_mode": self.setup_mode,
            "frame_size": f"{self.cam.w}x{self.cam.h}",
            "exposure_s": self.cam.exposure,
            "period_us": self.cam.period_us,
            "min_period_us": self.cam.min_period_us,
            "trigmode": self.cam.trigmode,
            "gain": self.cam.gain,
            "binning": self.cam.binning,
            "encoding": "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit",
            "capture_seconds": self.capture_seconds,
            "last_update": dt.now().isoformat(),
        }
        payload.update({f"cam_{k}": v for k, v in cam_status.items()})
        try:
            system = get_system_info(self.output_dir)
            payload["system"] = system
            console_ui.update(disk_free_mb=system.get("disk_free_mb"))
        except Exception:
            pass
        console_ui.update(status=status, frames=self._shots, errors=self._errors,
                          output_dir=self.output_dir)
        console_ui.set_section("camera", [
            ("Exposure / gain:", f"{self.cam.exposure} s  ·  {self.cam.gain}"),
            # On screen because a period shorter than the exposure is what
            # silently truncated every frame this camera ever took.
            ("Frame period:", f"{self.cam.period_us} us  "
                              f"(min {self.cam.min_period_us})"),
            ("Frame size:", f"{self.cam.w}x{self.cam.h}"),
            ("Encoding:", "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit"),
            ("CCD temperature:", str(cam_status.get("temp_ccd", "n/a"))),
        ])
        self._bus.publish_status(payload, force=force)


# ---------------------------------------------------------------------------
# Console entry point
# ---------------------------------------------------------------------------
def run_preview_sptt(cam, instance_name):
    """Continuously grab frames and overwrite preview_{instance_name}.png at max FPS."""
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
                frame = cam.grab_frame()
                if frame.dtype == np.uint16:
                    fmax = int(frame.max())
                    if fmax > 0:
                        display = (frame.astype(np.float32) / fmax * 255).astype(np.uint8)
                    else:
                        display = frame.astype(np.uint8)
                else:
                    display = frame
                img = Image.fromarray(display, mode="L")
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


def run_console_sptt(config_path=None, preview=False, verbose=False,
                     setup_mode=False):
    """Run SPTT camera measurement in console mode."""
    from utils import load_config, get_node_name
    from mqtt_client import create_console_publisher

    cfg = load_config(config_path)
    sptt_cfg = cfg.get("sptt", {})
    mqtt_cfg = cfg.get("mqtt", {})

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
    capture_seconds = sptt_cfg.get("capture_seconds", SPTT_CAPTURE_SECONDS)

    dash = console_ui.start_dashboard("sptt", instance_name, verbose=verbose)
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
        _run_console_sptt(cfg, sptt_cfg, mqtt_cfg, config_path, preview, dash,
                          instance_name, node_name, output_dir, status_dir,
                          exposure, gain, binning, encoding, target_temp,
                          capture_seconds, setup_mode, period_us, trigmode)
    finally:
        dash.stop()
        claim.release()


def _run_console_sptt(cfg, sptt_cfg, mqtt_cfg, config_path, preview, dash,
                      instance_name, node_name, output_dir, status_dir,
                      exposure, gain, binning, encoding, target_temp,
                      capture_seconds, setup_mode=False, period_us=None,
                      trigmode=None):
    """Body of :func:`run_console_sptt`, with the dashboard already running."""
    from mqtt_client import create_console_publisher

    if preview:
        console_ui.log("Initializing SPTT camera...")
        backend = find_libusb_backend()
        if not ensure_firmware_loaded(backend):
            console_ui.error("Failed to initialize camera.")
            sys.exit(1)
        time.sleep(1)
        cam = SpttCamera(backend)
        try:
            cam.open()
            cam.configure(exposure=exposure, gain=gain, binning=binning,
                          encoding=encoding, target_temp=target_temp,
                          period_us=period_us, trigmode=trigmode)
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
        capture_seconds = sptt_cfg.get("capture_seconds", SPTT_CAPTURE_SECONDS)
        dash.update(output_dir=output_dir,
                    schedule=f"capture at {capture_seconds} s of each minute")

    if not output_dir:
        console_ui.error("output_dir is required.")
        sys.exit(1)

    if setup_mode:
        announce_setup_mode(None)
        dash.update(schedule="setup mode — no scheduled captures")

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(status_dir, exist_ok=True)

    # Load firmware and connect
    console_ui.log("Initializing SPTT camera...")
    backend = find_libusb_backend()
    if not ensure_firmware_loaded(backend):
        console_ui.error("Failed to initialize camera.")
        sys.exit(1)
    time.sleep(1)

    cam = SpttCamera(backend)
    try:
        cam.open()
        sl = cam.configure(
            exposure=exposure, gain=gain, binning=binning, encoding=encoding,
            target_temp=target_temp, period_us=period_us, trigmode=trigmode,
        )
        # configure() has already refused anything the camera did not take, so
        # this line is the confirmation rather than the check.
        console_ui.log(f"Camera ready: {cam.w}x{cam.h} — "
                       f"exposure {sl[5] / 1e6:.3f} s, period {sl[6]} us "
                       f"(firmware minimum {sl[14]} us)")
    except Exception as exc:
        console_ui.error(f"Failed to configure camera: {exc}")
        sys.exit(1)

    # MQTT
    mqtt_pub = create_console_publisher(mqtt_cfg, instance_name, "sptt")
    if mqtt_pub:
        dash.update(mqtt=f"{mqtt_cfg.get('host', '?')} — connected")

    # LAN frame server (archive browsing + live view). Never fatal.
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
        mqtt_publisher=mqtt_pub,
        mqtt_prefix=mqtt_cfg.get("prefix", "every_camera"),
        service=service,
        node_name=node_name,
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
        if mqtt_pub:
            try:
                mqtt_pub.disconnect_broker()
            except Exception:
                pass
    console_ui.log("Done.")
