"""
SPTT (CSDU-429) camera driver: firmware loading, capture, FITS output, workers.
Uses pyusb for camera control.
"""
import os
import sys
import json
import signal
import time
import struct
import threading
import argparse

import numpy as np

from datetime import datetime as dt
from pathlib import Path

from utils import (
    write_status_file, get_instance_name, get_system_info, APP_DIR,
)

from sptt_load_firmware import (
    VID, PID_RAW, PID_CONFIGURED,
    find_libusb_backend, load_firmware_files, detach_kernel_driver,
    load_fx2_firmware, wait_for_configured_device, load_fpga_bitstream,
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


def make_command(cmd_id, value=0):
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

    dtype = np.uint16 if encoding == ENCODING_12BPP else np.uint8

    if binning > 0:
        arr = np.array(pixels[:w * h], dtype=dtype)
        if len(arr) < w * h:
            arr = np.pad(arr, (0, w * h - len(arr)))
        
        return arr.reshape(h, w)

    frame = [0] * (w * h)
    for i in range(h):
        src_row = i // 2 if i % 2 == 0 else h // 2 + i // 2
        src_off = src_row * w
        dst_off = i * w
        for j in range(w):
            idx = src_off + j
            if idx < len(pixels):
                frame[dst_off + j] = pixels[idx]

    return np.array(frame, dtype=dtype).reshape(h, w)


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
                  r_offset=None, g_offset=None, trigmode=0, period=75000,
                  draft=False, roi_org=None, roi_size=None, target_temp=None):
        """Apply all camera parameters. Exposure is in seconds."""
        wr = self._write_cmd
        self.exposure = exposure
        self.gain = gain
        self.binning = binning
        self.encoding = encoding

        exposure_us = int(exposure * 1_000_000)

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
        wr(CMD_SET_TRIGMODE, trigmode)
        wr(CMD_SET_PERIOD, period)
        wr(CMD_SET_DRAFT, 1 if draft else 0)
        if target_temp is not None:
            wr(CMD_SET_TARGET_TEMP, target_temp & 0xFF)

        _, sl = read_crb(self.ep_wr, self.ep_rd)
        self.w = sl[17]
        self.h = sl[18]
        return sl

    def _write_cmd(self, cmd_id, value=0):
        _usb_write_retry(self.ep_wr, make_command(cmd_id, value))

    def set_exposure(self, value):
        """Set exposure in seconds."""
        self.exposure = value
        _usb_write_retry(self.ep_wr, make_command(CMD_SET_EXP, int(value * 1_000_000)))

    def set_gain(self, value):
        self.gain = value
        _usb_write_retry(self.ep_wr, make_command(CMD_SET_GAIN, value))

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
                time.sleep(0.1)
                sb, _ = read_crb(self.ep_wr, self.ep_rd)
                if sb & 0x01:
                    return
            except usb.core.USBError as e:
                if attempt < retries - 1:
                    print(f"  Start attempt {attempt+1} failed: {e}, retrying...")
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

    def grab_frame(self):
        # Wait up to exposure_time + 5s for frame data in FIFO
        max_wait = max(self.exposure + 5.0, 3.0)
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            try:
                sb, _ = read_crb(self.ep_wr, self.ep_rd)
            except usb.core.USBError:
                time.sleep(0.01)
                continue
            if not (sb & 0x08):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("FIFO timeout — no frame data")

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
        return decode_frame(raw_chunks, w, h, self.encoding, self.binning)

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
        print(f"Camera already configured (PID=0x{PID_CONFIGURED:04x}), skipping firmware load.")
        usb.util.dispose_resources(dev)
        return True

    dev_raw = usb.core.find(idVendor=VID, idProduct=PID_RAW, backend=backend)
    if not dev_raw:
        print("ERROR: No SPTT camera found!")
        return False

    print("Loading firmware files...")
    fx2_data, fpga_data = load_firmware_files()

    print(f"\nFound raw FX2 device: {VID:04x}:{PID_RAW:04x}")
    detach_kernel_driver(dev_raw)
    load_fx2_firmware(dev_raw, fx2_data)

    print("\nSending USB bus reset...")
    try:
        dev_raw.reset()
    except usb.core.USBError as e:
        print(f"  USB reset returned: {e} (expected)")
    usb.util.dispose_resources(dev_raw)
    del dev_raw

    dev = wait_for_configured_device(backend)
    if not dev:
        print("ERROR: Device not found after firmware load!")
        return False

    detach_kernel_driver(dev)
    dev.set_configuration()
    print()
    load_fpga_bitstream(dev, fpga_data)

    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass
    del dev

    print()
    dev_final = wait_for_configured_device(backend, timeout=10.0)
    if dev_final:
        print("Firmware loaded successfully.")
        usb.util.dispose_resources(dev_final)
        return True

    print("WARNING: Device not found after initialization.")
    return False


# ---------------------------------------------------------------------------
# FITS file writing
# ---------------------------------------------------------------------------
def save_fits(filepath, frame, metadata=None):
    """Save frame as FITS file with metadata in header."""
    try:
        from astropy.io import fits
    except ImportError:
        print("[WARN] astropy not installed, falling back to raw FITS")
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

    # Build header
    cards = []
    cards.append(f"SIMPLE  =                    T / FITS standard")
    cards.append(f"BITPIX  = {bitpix:>20d} / bits per pixel")
    cards.append(f"NAXIS   =                    2 / number of axes")
    cards.append(f"NAXIS1  = {w:>20d} / width")
    cards.append(f"NAXIS2  = {h:>20d} / height")

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
        data = frame.astype(np.int16).byteswap().tobytes()
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
                 capture_seconds=None, mqtt_publisher=None, mqtt_prefix="every_camera"):
        super().__init__(daemon=True)
        self.cam = cam
        self.output_dir = output_dir
        self.instance_name = instance_name
        self.status_dir = status_dir
        self.capture_seconds = sorted(capture_seconds or SPTT_CAPTURE_SECONDS)
        self._mqtt = mqtt_publisher
        self._mqtt_prefix = mqtt_prefix
        self._mqtt_topic = f"{mqtt_prefix}/{instance_name}/status"
        self._stop_event = threading.Event()
        self._shots = 0
        self._errors = 0
        self._last_shot = None
        self._last_frame = None
        self._status_path = os.path.join(status_dir, f"{os.getpid()}.json")

    def request_stop(self):
        self._stop_event.set()

    def _on_mqtt_command(self, topic, payload):
        """Handle incoming MQTT commands (e.g. get_frame)."""
        if topic.endswith("/cmd/get_frame") and self._last_frame is not None:
            import base64
            import io
            from PIL import Image

            frame = self._last_frame
            # Normalize to 8-bit for JPEG
            if frame.dtype == np.uint16:
                display = (frame.astype(np.float32) / frame.max() * 255).astype(np.uint8)
            else:
                display = frame
            img = Image.fromarray(display, mode="L")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            jpeg_data = buf.getvalue()

            frame_topic = f"{self._mqtt_prefix}/{self.instance_name}/frame"
            frame_payload = json.dumps({
                "camera_type": "sptt",
                "instance_name": self.instance_name,
                "format": "jpeg",
                "data": base64.b64encode(jpeg_data).decode(),
                "timestamp": self._last_shot.isoformat() if self._last_shot else None,
            })
            self._mqtt.publish(frame_topic, frame_payload, retain=False)
            print("[INFO] Frame sent via MQTT")

    def run(self):
        last_fired = (-1, -1)
        consecutive_errors = 0
        os.makedirs(self.status_dir, exist_ok=True)

        # Subscribe to command topic
        if self._mqtt:
            cmd_topic = f"{self._mqtt_prefix}/{self.instance_name}/cmd/#"
            self._mqtt.subscribe_commands(cmd_topic, self._on_mqtt_command)

        print("[INFO] SPTT measurement started (captures at :00 and :30)")
        self._save_status("running")

        # Start continuous capture
        try:
            self.cam.start()
        except Exception as exc:
            print(f"[ERROR] Failed to start camera: {exc}")
            self._save_status("error")
            return

        while not self._stop_event.is_set():
            now = dt.now()

            fire_key = (now.minute, now.second)
            if now.second in self.capture_seconds and fire_key != last_fired:
                last_fired = fire_key
                ok = self._capture_one(now)
                if ok:
                    consecutive_errors = 0
                    self._shots += 1
                    self._last_shot = now
                    self._save_status("running")
                else:
                    consecutive_errors += 1
                    self._errors += 1
                    self._save_status("error")
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        print(f"[ERROR] {consecutive_errors} consecutive errors, stopping")
                        break
            elif now.second not in self.capture_seconds:
                last_fired = (-1, -1)

            self._stop_event.wait(0.1)

        self.cam.stop()
        self._save_status("stopped")
        self._delete_status()
        print("[INFO] SPTT measurement stopped")

    def _capture_one(self, now):
        timestamp = now.strftime("%Y%m%dT%H%M%S")
        filepath = os.path.join(self.output_dir, f"{timestamp}.fit")
        try:
            frame = self.cam.grab_frame()

            # Build FITS metadata
            cam_status = self.cam.get_status_info()
            metadata = {
                "DATE-OBS": now.isoformat(),
                "INSTRUME": "CSDU-429",
                "EXPTIME": self.cam.exposure,
                "GAIN": self.cam.gain,
                "BINNING": self.cam.binning,
                "ENCODING": "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit",
            }
            if cam_status:
                metadata["CCDTEMP"] = cam_status.get("temp_ccd", 0)
                metadata["SINKTEMP"] = cam_status.get("temp_sink", 0)
                metadata["TRGTEMP"] = cam_status.get("temp_target", 0)

            save_fits(filepath, frame, metadata)
            self._last_frame = frame
            print(f"[INFO] Frame saved: {os.path.basename(filepath)} "
                  f"({frame.shape[1]}x{frame.shape[0]}, "
                  f"exp={self.cam.exposure}s, gain={self.cam.gain})")
            return True
        except Exception as exc:
            print(f"[ERROR] Capture error: {exc}")
            return False

    def _save_status(self, status):
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
            "frame_size": f"{self.cam.w}x{self.cam.h}",
            "exposure_s": self.cam.exposure,
            "gain": self.cam.gain,
            "binning": self.cam.binning,
            "encoding": "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit",
            "capture_seconds": self.capture_seconds,
            "last_update": dt.now().isoformat(),
        }
        payload.update({f"cam_{k}": v for k, v in cam_status.items()})
        try:
            payload["system"] = get_system_info(self.output_dir)
        except Exception:
            pass

        try:
            write_status_file(self._status_path, payload)
        except Exception:
            pass
        if self._mqtt:
            try:
                self._mqtt.publish(self._mqtt_topic, json.dumps(payload), retain=True)
            except Exception:
                pass

    def _delete_status(self):
        try:
            os.remove(self._status_path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Console entry point
# ---------------------------------------------------------------------------
def run_console_sptt(config_path=None):
    """Run SPTT camera measurement in console mode."""
    from utils import load_config
    from mqtt_client import create_console_publisher

    cfg = load_config(config_path)
    sptt_cfg = cfg.get("sptt", {})
    mqtt_cfg = cfg.get("mqtt", {})

    instance_name = sptt_cfg.get("instance_name") or get_instance_name("SPTT")
    output_dir = sptt_cfg.get("output_dir", "")
    status_dir = cfg.get("status_dir") or str(Path.home() / ".every_camera" / "status")
    exposure = sptt_cfg.get("exposure", 0.88)
    gain = sptt_cfg.get("gain", 100)
    binning = sptt_cfg.get("binning", 0)
    encoding = sptt_cfg.get("encoding", ENCODING_12BPP)
    target_temp = sptt_cfg.get("target_temp")
    capture_seconds = sptt_cfg.get("capture_seconds", SPTT_CAPTURE_SECONDS)

    print("=" * 60)
    print("  Every Camera — SPTT (CSDU-429) Console Mode")
    print(f"  Instance  : {instance_name}")
    print(f"  Exposure  : {exposure} s")
    print(f"  Gain      : {gain}")
    print(f"  Binning   : {binning}")
    print(f"  Encoding  : {'12bit' if encoding == ENCODING_12BPP else '8bit'}")
    print(f"  Capture at: {capture_seconds} seconds of each minute")
    print("=" * 60)

    if not output_dir:
        print("[INFO] Configuration incomplete. Starting setup wizard...")
        from utils import configure_console_sptt
        configure_console_sptt(cfg, config_path)
        sptt_cfg = cfg.get("sptt", {})
        instance_name = sptt_cfg.get("instance_name") or get_instance_name("SPTT")
        output_dir = sptt_cfg.get("output_dir", "")
        exposure = sptt_cfg.get("exposure", 0.88)
        gain = sptt_cfg.get("gain", 100)
        binning = sptt_cfg.get("binning", 0)
        encoding = sptt_cfg.get("encoding", ENCODING_12BPP)
        target_temp = sptt_cfg.get("target_temp")
        capture_seconds = sptt_cfg.get("capture_seconds", SPTT_CAPTURE_SECONDS)
        print(f"  Instance  : {instance_name}")
        print(f"  Exposure  : {exposure} s")
        print(f"  Gain      : {gain}")

    if not output_dir:
        print("[ERROR] output_dir is required.")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(status_dir, exist_ok=True)

    # Load firmware and connect
    print("[INFO] Initializing SPTT camera...")
    backend = find_libusb_backend()
    if not ensure_firmware_loaded(backend):
        print("[ERROR] Failed to initialize camera.")
        sys.exit(1)
    time.sleep(1)

    cam = SpttCamera(backend)
    try:
        cam.open()
        sl = cam.configure(
            exposure=exposure, gain=gain, binning=binning, encoding=encoding,
            target_temp=target_temp,
        )
        print(f"[INFO] Camera ready: {cam.w}x{cam.h}")
    except Exception as exc:
        print(f"[ERROR] Failed to configure camera: {exc}")
        sys.exit(1)

    # MQTT
    mqtt_pub = create_console_publisher(mqtt_cfg)

    worker = SpttWorkerConsole(
        cam=cam,
        output_dir=output_dir,
        instance_name=instance_name,
        status_dir=status_dir,
        capture_seconds=capture_seconds,
        mqtt_publisher=mqtt_pub,
        mqtt_prefix=mqtt_cfg.get("prefix", "every_camera"),
    )

    def _sigint(sig, frame):
        print("\n[INFO] Stopping (Ctrl+C)...")
        worker.request_stop()
    signal.signal(signal.SIGINT, _sigint)

    print("[INFO] Starting. Press Ctrl+C to stop.")
    worker.start()
    worker.join()

    cam.close()
    if mqtt_pub:
        mqtt_pub.disconnect_broker()
    print("[INFO] Done.")
