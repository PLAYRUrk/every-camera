"""
Infra camera driver: Tanho SW1300 SWIR camera (THCAMSW1300, Sony IMX990).
USB: Cypress FX3, VID=0xaa55, PID=0x8866.
12-bit ADC, 16-bit output, 1280x1024.

Provides:
  - TanhoCamera: low-level USB camera control
  - InfraCaptureThread: continuous frame capture (QThread for live preview)
  - InfraWorkerConsole: schedule-based capture (threading.Thread for headless)
  - run_console_infra(): console entry point
"""
import os
import io
import sys
import json
import signal
import ctypes
import threading
import time

import numpy as np

from datetime import datetime as dt
from pathlib import Path

from utils import (
    ScheduleEntry, load_schedule_file, parse_schedule_text,
    write_status_file, get_instance_name, get_system_info,
    APP_DIR,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RAW_HALF_WIDTH = 640
BYTES_PER_PIXEL = 2

ROI_MODES = {
    "1280x256": (1280, 256),
    "1280x1024": (1280, 1024),
}
DEFAULT_ROI = "1280x1024"

FRAME_WIDTH = 1280
FRAME_HEIGHT = 1024
ADC_MAX = 4094

CMD_PACKET_SIZE = 32
CMD_FOOTER_POS = 31
CMD_FOOTER_VAL = 0x15

CMD_EXPOSURE = 0xFF
CMD_EXPOSURE_ALT = 0xF5
CMD_GAIN = 0xFE
CMD_ROI = 0xD0

USB_EP_IN = 0x81
USB_CHUNK_SIZE = 0x4000
USB_TIMEOUT = 200
SYNC_MARKER = b'\x55\xFF\xAA\xCC'
SYNC_HEADER_SIZE = 0x200

MAX_GRAB_RETRIES = 3

INFRA_CAPTURE_SECONDS = [0, 30]


# ---------------------------------------------------------------------------
# Library discovery
# ---------------------------------------------------------------------------
def _find_library() -> str:
    """Find libTanhoAPI.so relative to this module."""
    base = Path(__file__).resolve().parent
    # Prefer the real file first (symlinks may not work on all systems)
    candidates = [
        base / "infra_lib" / "libTanhoAPI.so.1.0.0",
        base / "infra_lib" / "libTanhoAPI.so",
    ]
    for path in candidates:
        if path.is_file() and not path.is_symlink() or (
            path.is_symlink() and path.resolve().is_file()
        ):
            return str(path)
    raise FileNotFoundError(
        "libTanhoAPI.so not found. Check infra_lib/ directory:\n"
        + "\n".join(f"  - {p}" for p in candidates)
    )


def _raw_size_for_roi(width: int, height: int) -> tuple:
    raw_w = width // 2
    raw_h = height * 2
    return raw_w, raw_h


# ---------------------------------------------------------------------------
# Camera class
# ---------------------------------------------------------------------------
class TanhoCamera:
    """Low-level wrapper for TanhoAPI SDK (SW1300 SWIR camera)."""

    def __init__(self):
        self._lib = None
        self._libusb = None
        self._connected = False
        self._roi_width = 1280
        self._roi_height = 1024
        self._exposure_us = 1000.0
        self._gain = 0
        self._usb_buffer = None
        self._usb_chunk = None
        self._transferred = None
        self._num_chunks = 0
        self._usb_buf_size = 0
        self._frame_size = 0
        self._raw_w = 0
        self._raw_h = 0
        self._devh_ref = None

    def _load_library(self):
        lib_path = _find_library()
        self._lib = ctypes.CDLL(lib_path)

        self._driver_init = self._lib._ZN8TanhoAPI19TanhoCam_DriverInitEj
        self._driver_init.argtypes = [ctypes.c_uint]
        self._driver_init.restype = ctypes.c_int

        self._open_driver = self._lib._ZN8TanhoAPI19TanhoCam_OpenDriverEv
        self._open_driver.argtypes = []
        self._open_driver.restype = ctypes.c_int

        self._close_driver = self._lib._ZN8TanhoAPI20TanhoCam_CloseDriverEv
        self._close_driver.argtypes = []
        self._close_driver.restype = ctypes.c_int

        self._execute_cmd = self._lib._ZN8TanhoAPI19TanhoCam_ExecuteCmdEPh
        self._execute_cmd.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        self._execute_cmd.restype = ctypes.c_int

        self._driver_start = self._lib._Z20TanhoCam_DriverStartv
        self._driver_start.argtypes = []
        self._driver_start.restype = ctypes.c_int

        self._driver_stop = self._lib._Z19TanhoCam_DriverStopv
        self._driver_stop.argtypes = []
        self._driver_stop.restype = ctypes.c_int

        self._libusb = ctypes.CDLL("libusb-1.0.so.0")
        self._bulk_transfer = self._libusb.libusb_bulk_transfer
        self._bulk_transfer.argtypes = [
            ctypes.c_void_p, ctypes.c_ubyte,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
        ]
        self._bulk_transfer.restype = ctypes.c_int

    def connect(self) -> bool:
        if self._connected:
            return True
        if self._lib is None:
            self._load_library()

        result = self._driver_init(1)
        if result != 0:
            raise RuntimeError(f"TanhoCam_DriverInit error: {result}")

        result = self._open_driver()
        if not result:
            raise RuntimeError(
                "Failed to open camera. Check:\n"
                "  1. Camera connected via USB 3.0\n"
                "  2. Access rights (udev rule or sudo)"
            )

        self._driver_start()
        self._devh_ref = ctypes.c_void_p.in_dll(self._lib, 'devh')
        self._connected = True
        self.set_roi(1280, 1024)
        self._flush_usb()
        return True

    def disconnect(self):
        if self._connected and self._lib is not None:
            self._driver_stop()
            self._close_driver()
            self._connected = False
            self._usb_buffer = None
            self._usb_chunk = None
            self._devh_ref = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def roi_width(self) -> int:
        return self._roi_width

    @property
    def roi_height(self) -> int:
        return self._roi_height

    @property
    def exposure_us(self) -> float:
        return self._exposure_us

    @property
    def gain(self) -> int:
        return self._gain

    def _allocate_buffer(self):
        raw_w, raw_h = _raw_size_for_roi(self._roi_width, self._roi_height)
        self._raw_w = raw_w
        self._raw_h = raw_h
        self._frame_size = self._roi_width * self._roi_height * BYTES_PER_PIXEL
        chunks_per_frame = (self._frame_size + USB_CHUNK_SIZE - 1) // USB_CHUNK_SIZE
        self._num_chunks = chunks_per_frame * 3
        self._usb_buf_size = self._num_chunks * USB_CHUNK_SIZE
        self._usb_buffer = (ctypes.c_ubyte * self._usb_buf_size)()
        self._usb_chunk = (ctypes.c_ubyte * USB_CHUNK_SIZE)()
        self._transferred = ctypes.c_int(0)

    def _flush_usb(self):
        if not self._devh_ref:
            return
        devh = self._devh_ref.value
        if not devh:
            return
        flush_chunk = (ctypes.c_ubyte * USB_CHUNK_SIZE)()
        transferred = ctypes.c_int(0)
        for _ in range(20):
            ret = self._bulk_transfer(
                devh, USB_EP_IN, flush_chunk, USB_CHUNK_SIZE,
                ctypes.byref(transferred), 10
            )
            if ret != 0:
                break

    def _read_frame_usb(self) -> bytes:
        devh = self._devh_ref.value
        if not devh:
            raise RuntimeError("USB device handle not initialized")

        buf_addr = ctypes.addressof(self._usb_buffer)

        for i in range(self._num_chunks):
            self._bulk_transfer(
                devh, USB_EP_IN, self._usb_chunk, USB_CHUNK_SIZE,
                ctypes.byref(self._transferred), USB_TIMEOUT
            )
            ctypes.memmove(
                buf_addr + i * USB_CHUNK_SIZE,
                self._usb_chunk,
                USB_CHUNK_SIZE
            )

        buf_bytes = ctypes.string_at(buf_addr, self._usb_buf_size)
        pos = buf_bytes.find(SYNC_MARKER)
        if pos < 0:
            return None

        data_start = pos + SYNC_HEADER_SIZE
        data_end = data_start + self._frame_size
        if data_end > self._usb_buf_size:
            return None

        return buf_bytes[data_start:data_end]

    def grab_frame(self) -> np.ndarray:
        """Grab one frame, deinterlace. Returns uint16 (roi_height, roi_width)."""
        if not self._connected:
            raise RuntimeError("Camera not connected")

        for attempt in range(MAX_GRAB_RETRIES):
            frame_bytes = self._read_frame_usb()
            if frame_bytes is not None:
                break
        else:
            raise RuntimeError(
                f"Failed to grab frame after {MAX_GRAB_RETRIES} attempts "
                "(sync marker not found)"
            )

        raw_16 = np.frombuffer(frame_bytes, dtype=np.uint16).reshape(
            self._raw_h, self._raw_w
        )

        frame = np.empty((self._roi_height, self._roi_width), dtype=np.uint16)
        frame[:, :self._raw_w] = raw_16[0::2]
        frame[:, self._raw_w:] = raw_16[1::2]

        return frame

    def set_exposure(self, microseconds: float):
        """Set exposure in microseconds."""
        if not self._connected:
            return
        self._exposure_us = microseconds
        ticks = int(microseconds * 20)
        b = ticks.to_bytes(4, 'little')

        cmd1 = self._make_cmd_packet(CMD_EXPOSURE)
        cmd1[4] = b[1]; cmd1[5] = b[0]
        cmd1[6] = b[3]; cmd1[7] = b[2]
        self._execute_raw_cmd(cmd1)

        cmd2 = self._make_cmd_packet(CMD_EXPOSURE_ALT)
        cmd2[4] = b[1]; cmd2[5] = b[0]
        cmd2[6] = b[3]; cmd2[7] = b[2]
        self._execute_raw_cmd(cmd2)

    def set_gain(self, gain: int):
        """Set gain (0-120)."""
        if not self._connected:
            return
        self._gain = gain
        cmd = self._make_cmd_packet(CMD_GAIN)
        cmd[4] = 0x00
        cmd[5] = max(1, gain) & 0xFF
        self._execute_raw_cmd(cmd)

    def set_roi(self, width: int = 1280, height: int = 1024):
        if not self._connected:
            return
        cmd = self._make_cmd_packet(CMD_ROI)
        cmd[4] = 0x00
        cmd[5] = 0x00
        cmd[6] = (width // 8) & 0xFF
        cmd[7] = (height // 8) & 0xFF
        self._execute_raw_cmd(cmd)
        self._roi_width = width
        self._roi_height = height
        self._allocate_buffer()

    def _execute_raw_cmd(self, data):
        buf = (ctypes.c_ubyte * CMD_PACKET_SIZE)(*data[:CMD_PACKET_SIZE])
        self._execute_cmd(buf)

    @staticmethod
    def _make_cmd_packet(cmd_code: int) -> bytearray:
        packet = bytearray(CMD_PACKET_SIZE)
        packet[0] = 0x00
        packet[1] = 0x06
        packet[2] = 0x00
        packet[3] = cmd_code
        packet[CMD_FOOTER_POS] = CMD_FOOTER_VAL
        return packet

    def __del__(self):
        self.disconnect()


# ---------------------------------------------------------------------------
# Image saving
# ---------------------------------------------------------------------------
def save_fits(filepath, frame_16, exposure_us=None, gain=None, roi=None):
    """Save 16-bit frame as FITS with metadata header."""
    from astropy.io import fits
    hdu = fits.PrimaryHDU(data=frame_16)
    hdr = hdu.header
    hdr['INSTRUME'] = ('THCAMSW1300', 'Camera model')
    hdr['SENSOR'] = ('IMX990-AABA-C', 'Sensor model')
    hdr['BITPIX'] = (16, 'Bits per pixel')
    hdr['DATE-OBS'] = (dt.utcnow().isoformat(), 'Observation date (UTC)')
    if exposure_us is not None:
        hdr['EXPTIME'] = (exposure_us / 1e6, 'Exposure time (seconds)')
        hdr['EXPTUS'] = (exposure_us, 'Exposure time (microseconds)')
    if gain is not None:
        hdr['GAIN'] = (gain, 'Camera gain')
    if roi is not None:
        hdr['ROI'] = (roi, 'Region of interest')
    hdu.writeto(filepath, overwrite=True)


def save_tiff(filepath, frame_16):
    """Save 16-bit frame as TIFF using cv2 or PIL."""
    try:
        import cv2
        cv2.imwrite(filepath, frame_16)
        return
    except ImportError:
        pass
    try:
        from PIL import Image
        img = Image.fromarray(frame_16)
        img.save(filepath)
    except ImportError:
        raise RuntimeError("Install opencv-python or Pillow to save TIFF images")


def save_png(filepath, frame_16):
    """Save 16-bit frame as 8-bit PNG (normalized)."""
    fmin, fmax = frame_16.min(), frame_16.max()
    if fmax > fmin:
        img_8 = ((frame_16 - fmin) / (fmax - fmin) * 255).astype(np.uint8)
    else:
        img_8 = np.zeros_like(frame_16, dtype=np.uint8)
    try:
        import cv2
        cv2.imwrite(filepath, img_8)
        return
    except ImportError:
        pass
    try:
        from PIL import Image
        img = Image.fromarray(img_8, mode="L")
        img.save(filepath)
    except ImportError:
        raise RuntimeError("Install opencv-python or Pillow to save PNG images")


def frame_to_jpeg_bytes(frame_16):
    """Convert 16-bit frame to JPEG bytes for MQTT transmission."""
    clipped = np.clip(frame_16 * (255.0 / ADC_MAX), 0, 255).astype(np.uint8)
    try:
        import cv2
        _, buf = cv2.imencode('.jpg', clipped, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes()
    except ImportError:
        pass
    from PIL import Image
    img = Image.fromarray(clipped, mode="L")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Console worker
# ---------------------------------------------------------------------------
class InfraWorkerConsole(threading.Thread):
    """Schedule-based capture worker for console/headless mode."""
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, cam, schedule, output_dir, instance_name,
                 status_dir, capture_seconds, save_format="tiff",
                 mqtt_publisher=None, mqtt_prefix="every_camera"):
        super().__init__(daemon=True)
        self.cam = cam
        self.schedule = schedule
        self.output_dir = output_dir
        self.instance_name = instance_name
        self.status_dir = status_dir
        self.capture_seconds = sorted(capture_seconds)
        self.save_format = save_format
        self._mqtt = mqtt_publisher
        self._mqtt_prefix = mqtt_prefix
        self._mqtt_topic = f"{mqtt_prefix}/{instance_name}/status"
        self._stop_event = threading.Event()
        self._shots = 0
        self._errors = 0
        self._last_shot = None
        self._last_frame = None
        self._active_until = None
        self._status_path = os.path.join(status_dir, f"{os.getpid()}.json")
        self._pending_capture = None
        self._pending_capture_lock = threading.Lock()

    def request_stop(self):
        self._stop_event.set()

    _MQTT_MAX_PAYLOAD_BYTES = 240_000

    def _publish_ok(self, jpeg_bytes, ts_iso, on_demand=False, params=None):
        import base64
        frame_topic = f"{self._mqtt_prefix}/{self.instance_name}/frame"
        body = {
            "camera_type": "infra",
            "instance_name": self.instance_name,
            "status": "ok",
            "format": "jpeg",
            "data": base64.b64encode(jpeg_bytes).decode(),
            "timestamp": ts_iso,
            "on_demand": on_demand,
        }
        if params:
            body["params"] = params
        payload = json.dumps(body)
        if len(payload) > self._MQTT_MAX_PAYLOAD_BYTES:
            self._publish_error(
                "too_large",
                f"Frame payload {len(payload)} bytes exceeds broker limit",
                ts_iso=ts_iso, on_demand=on_demand)
            print(f"[WARN] Frame too large ({len(payload)} bytes)")
            return
        self._mqtt.publish(frame_topic, payload, retain=False)

    def _publish_error(self, status, error, ts_iso=None, on_demand=False):
        frame_topic = f"{self._mqtt_prefix}/{self.instance_name}/frame"
        self._mqtt.publish(frame_topic, json.dumps({
            "camera_type": "infra",
            "instance_name": self.instance_name,
            "status": status,
            "error": error,
            "timestamp": ts_iso,
            "on_demand": on_demand,
        }), retain=False)

    def _apply_params(self, params):
        applied = {}
        errors = []
        try:
            if "exposure_us" in params:
                self.cam.set_exposure(float(params["exposure_us"]))
                applied["exposure_us"] = float(params["exposure_us"])
            elif "exposure" in params:
                exp_us = float(params["exposure"]) * 1_000_000
                self.cam.set_exposure(exp_us)
                applied["exposure_us"] = exp_us
            if "gain" in params:
                self.cam.set_gain(int(params["gain"]))
                applied["gain"] = int(params["gain"])
            if "roi_width" in params or "roi_height" in params:
                w = int(params.get("roi_width", self.cam.roi_width))
                h = int(params.get("roi_height", self.cam.roi_height))
                self.cam.set_roi(width=w, height=h)
                applied["roi_width"] = w
                applied["roi_height"] = h
        except Exception as e:
            errors.append(str(e))
        return applied, errors

    def _handle_pending_capture(self):
        with self._pending_capture_lock:
            params = self._pending_capture
            self._pending_capture = None
        if params is None:
            return
        print("[INFO] On-demand Infra capture starting", flush=True)
        self._publish_status("capturing", f"Applying params: {params}")
        try:
            applied, errors = self._apply_params(params)
            for err in errors:
                print(f"[WARN] Param apply: {err}")
            frame = self.cam.grab_frame()
            now = dt.now()
            jpeg_bytes = frame_to_jpeg_bytes(frame)
            self._publish_ok(jpeg_bytes, now.isoformat(),
                             on_demand=True, params=applied)
            print("[INFO] On-demand frame sent via MQTT")
        except Exception as e:
            self._publish_error("error", f"Capture failed: {e}", on_demand=True)
            print(f"[ERROR] On-demand capture error: {e}")

    def _on_mqtt_command(self, topic, payload):
        print(f"[infra:{self.instance_name}] MQTT cmd received: {topic} "
              f"({len(payload) if payload else 0} bytes)", flush=True)
        if not self._mqtt:
            return
        if topic.endswith("/cmd/get_frame"):
            if self._last_frame is None:
                self._publish_error("no_frame", "No frame captured yet")
                print("[WARN] Frame requested but no frame available yet")
                return
            ts = self._last_shot.isoformat() if self._last_shot else None
            try:
                jpeg_data = frame_to_jpeg_bytes(self._last_frame)
            except Exception as e:
                self._publish_error("error", str(e), ts)
                print(f"[ERROR] Frame encode error: {e}")
                return
            self._publish_ok(jpeg_data, ts)
            print("[INFO] Frame sent via MQTT")
            return
        if topic.endswith("/cmd/capture_frame"):
            try:
                params = json.loads(payload) if payload else {}
            except json.JSONDecodeError as e:
                self._publish_error("bad_request", f"Invalid JSON: {e}",
                                    on_demand=True)
                return
            if not isinstance(params, dict):
                params = {}
            with self._pending_capture_lock:
                self._pending_capture = params
            self._publish_status("accepted", f"Request queued with params: {params}")
            print(f"[INFO] On-demand capture queued with params: {params}",
                  flush=True)
            return
        print(f"[infra:{self.instance_name}] Unknown command: {topic}", flush=True)

    def _publish_status(self, status, note=""):
        if not self._mqtt:
            return
        frame_topic = f"{self._mqtt_prefix}/{self.instance_name}/frame"
        self._mqtt.publish(frame_topic, json.dumps({
            "camera_type": "infra",
            "instance_name": self.instance_name,
            "status": status,
            "note": note,
            "timestamp": dt.now().isoformat(),
            "on_demand": True,
        }), retain=False)

    def run(self):
        last_fired = (-1, -1)
        consecutive_errors = 0
        os.makedirs(self.status_dir, exist_ok=True)

        if self._mqtt:
            cmd_topic = f"{self._mqtt_prefix}/{self.instance_name}/cmd/#"
            self._mqtt.subscribe_commands(cmd_topic, self._on_mqtt_command)
            print(f"[infra:{self.instance_name}] Subscribed to commands: "
                  f"{cmd_topic}", flush=True)
        else:
            print(f"[infra:{self.instance_name}] No MQTT — remote commands disabled",
                  flush=True)

        print("[INFO] Infra camera measurement started")
        self._save_status("running")

        while not self._stop_event.is_set():
            # Handle on-demand capture requests (outside schedule)
            if self._pending_capture is not None:
                self._handle_pending_capture()

            now = dt.now()

            active_end = None
            for entry in self.schedule:
                if entry.start <= now <= entry.end:
                    active_end = entry.end
                    break

            if active_end is None:
                self._save_status("waiting")
                self._stop_event.wait(0.5)
                continue

            self._active_until = active_end

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

        self._save_status("stopped")
        self._delete_status()
        print("[INFO] Infra camera measurement stopped")

    def _capture_one(self, now):
        timestamp = now.strftime("%Y%m%dT%H%M%S")
        ext_map = {"tiff": "tiff", "png": "png", "fits": "fits"}
        ext = ext_map.get(self.save_format, "tiff")
        filepath = os.path.join(self.output_dir, f"{timestamp}.{ext}")
        try:
            frame = self.cam.grab_frame()
            if self.save_format == "fits":
                roi_str = f"{self.cam.roi_width}x{self.cam.roi_height}"
                save_fits(filepath, frame,
                          exposure_us=self.cam.exposure_us,
                          gain=self.cam.gain,
                          roi=roi_str)
            elif self.save_format == "tiff":
                save_tiff(filepath, frame)
            else:
                save_png(filepath, frame)
            self._last_frame = frame
            print(f"[INFO] Shot saved: {os.path.basename(filepath)}")
            return True
        except Exception as exc:
            print(f"[ERROR] Capture error: {exc}")
            return False

    def _save_status(self, status):
        payload = {
            "instance_name": self.instance_name,
            "camera_type": "infra",
            "pid": os.getpid(),
            "status": status,
            "output_dir": self.output_dir,
            "shots_taken": self._shots,
            "last_shot": self._last_shot.isoformat() if self._last_shot else None,
            "active_until": self._active_until.isoformat() if self._active_until else None,
            "errors": self._errors,
            "capture_seconds": self.capture_seconds,
            "exposure_us": self.cam.exposure_us,
            "gain": self.cam.gain,
            "roi": f"{self.cam.roi_width}x{self.cam.roi_height}",
            "last_update": dt.now().isoformat(),
        }
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
def run_preview_infra(cam, instance_name):
    """Continuously grab frames and overwrite preview_{instance_name}.png at max FPS."""
    preview_path = os.path.join(APP_DIR, f"preview_{instance_name}.png")
    tmp_path = preview_path
    print(f"[INFO] Preview mode: writing {preview_path} (Ctrl+C to stop)")

    stop = threading.Event()

    def _sigint(sig, frame):
        print("\n[INFO] Stopping preview...")
        stop.set()
    signal.signal(signal.SIGINT, _sigint)

    frames = 0
    t0 = dt.now()
    while not stop.is_set():
        try:
            frame = cam.grab_frame()
            #save_png(tmp_path, frame)
            save_fits(tmp_path, frame)
            os.replace(tmp_path, preview_path)
            frames += 1
            if frames % 10 == 0:
                elapsed = (dt.now() - t0).total_seconds()
                fps = frames / elapsed if elapsed > 0 else 0
                print(f"[INFO] Preview: {frames} frames, {fps:.1f} FPS")
        except Exception as exc:
            print(f"[ERROR] Preview frame error: {exc}")
            time.sleep(0.1)


def run_console_infra(config_path=None, preview=False):
    """Run Infra camera measurement in console mode."""
    from utils import load_config
    from mqtt_client import create_console_publisher

    cfg = load_config(config_path)
    infra_cfg = cfg.get("infra", {})
    mqtt_cfg = cfg.get("mqtt", {})

    instance_name = infra_cfg.get("instance_name") or get_instance_name("Infra")
    output_dir = infra_cfg.get("output_dir", "")
    status_dir = cfg.get("status_dir") or str(Path.home() / ".every_camera" / "status")
    schedule_file = infra_cfg.get("schedule_file", "")
    capture_seconds = infra_cfg.get("capture_seconds", INFRA_CAPTURE_SECONDS)
    exposure_us = infra_cfg.get("exposure_us", 1000.0)
    gain_val = infra_cfg.get("gain", 0)
    roi = infra_cfg.get("roi", DEFAULT_ROI)
    save_format = infra_cfg.get("save_format", "tiff")

    print("=" * 60)
    print("  Every Camera -- Infra (SW1300 SWIR) Console Mode" + ("  [PREVIEW]" if preview else ""))
    print(f"  Instance      : {instance_name}")
    if not preview:
        print(f"  Capture at    : {capture_seconds} seconds of each minute")
    print(f"  Exposure      : {exposure_us} us")
    print(f"  Gain          : {gain_val}")
    print(f"  ROI           : {roi}")
    if not preview:
        print(f"  Save format   : {save_format}")
    print("=" * 60)

    if preview:
        print("[INFO] Connecting to Infra camera...")
        cam = TanhoCamera()
        try:
            cam.connect()
        except Exception as exc:
            print(f"[ERROR] Failed to connect camera: {exc}")
            sys.exit(1)
        cam.set_exposure(exposure_us)
        cam.set_gain(gain_val)
        if roi in ROI_MODES:
            w, h = ROI_MODES[roi]
            cam.set_roi(w, h)
        print(f"[INFO] Connected: {cam.roi_width}x{cam.roi_height}")
        try:
            run_preview_infra(cam, instance_name)
        finally:
            cam.disconnect()
        print("[INFO] Done.")
        return

    if not output_dir or not schedule_file:
        print("[INFO] Configuration incomplete. Starting setup wizard...")
        from utils import configure_console_infra
        configure_console_infra(cfg, config_path)
        infra_cfg = cfg.get("infra", {})
        instance_name = infra_cfg.get("instance_name") or get_instance_name("Infra")
        output_dir = infra_cfg.get("output_dir", "")
        schedule_file = infra_cfg.get("schedule_file", "")
        capture_seconds = infra_cfg.get("capture_seconds", INFRA_CAPTURE_SECONDS)
        exposure_us = infra_cfg.get("exposure_us", 1000.0)
        gain_val = infra_cfg.get("gain", 0)
        roi = infra_cfg.get("roi", DEFAULT_ROI)
        save_format = infra_cfg.get("save_format", "tiff")

    if not output_dir:
        print("[ERROR] output_dir is required.")
        sys.exit(1)
    if not schedule_file:
        print("[ERROR] schedule_file is required.")
        sys.exit(1)
    if not os.path.exists(schedule_file):
        print(f"[ERROR] Schedule file not found: {schedule_file}")
        sys.exit(1)

    entries, errors = load_schedule_file(schedule_file)
    for err in errors:
        print(f"[WARN] Schedule: {err}")
    if not entries:
        print("[ERROR] No valid schedule entries found.")
        sys.exit(1)

    print(f"[INFO] Loaded {len(entries)} schedule intervals")
    print(f"[INFO] Output directory: {output_dir}")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(status_dir, exist_ok=True)

    # Connect camera
    print("[INFO] Connecting to Infra camera...")
    cam = TanhoCamera()
    try:
        cam.connect()
    except Exception as exc:
        print(f"[ERROR] Failed to connect camera: {exc}")
        sys.exit(1)

    # Apply settings
    cam.set_exposure(exposure_us)
    cam.set_gain(gain_val)
    if roi in ROI_MODES:
        w, h = ROI_MODES[roi]
        cam.set_roi(w, h)
    print(f"[INFO] Connected: {cam.roi_width}x{cam.roi_height}")

    # MQTT
    mqtt_pub = create_console_publisher(mqtt_cfg)

    worker = InfraWorkerConsole(
        cam=cam,
        schedule=entries,
        output_dir=output_dir,
        instance_name=instance_name,
        status_dir=status_dir,
        capture_seconds=capture_seconds,
        save_format=save_format,
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

    cam.disconnect()
    if mqtt_pub:
        mqtt_pub.disconnect_broker()
    print("[INFO] Done.")
