"""
Canon camera driver: connection, configuration, capture, workers.
Uses gphoto2-cffi for camera control.
"""
import os
import io
import re
import sys
import json
import signal
import subprocess
import threading
import configparser as cp

import numpy as np

from datetime import datetime as dt
from time import sleep
from pathlib import Path

from utils import (
    ScheduleEntry, load_schedule_file, parse_schedule_text,
    write_status_file, get_instance_name, get_system_info,
    APP_DIR,
)

# ---------------------------------------------------------------------------
# gphoto2-cffi import with monkey-patching
# ---------------------------------------------------------------------------
import gphoto2cffi as gp
import gphoto2cffi.backend as _gp_backend
import gphoto2cffi.util as _gp_util
import gphoto2cffi.gphoto2 as _gp_main


def _patched_get_string(cfunc, *args):
    cstr = _gp_util.get_ctype("const char**", cfunc, *args)
    return _gp_backend.ffi.string(cstr).decode(errors='replace') if cstr else None


_gp_util.get_string = _patched_get_string
_gp_main.get_string = _patched_get_string


# Suppress UnicodeDecodeError spam from gphoto2cffi
class _GphotoLogFilter:
    def __init__(self, stream):
        self._stream = stream
        self._suppressing = False

    def write(self, text):
        if 'Exception ignored' in text and '_logging_callback' in text:
            self._suppressing = True
        if self._suppressing:
            if 'UnicodeDecodeError' in text:
                self._suppressing = False
            return len(text)
        return self._stream.write(text)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


sys.stderr = _GphotoLogFilter(sys.stderr)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SKIP_SECTIONS = {"status", "actions", ""}


# ---------------------------------------------------------------------------
# Camera USB release
# ---------------------------------------------------------------------------
def release_camera_usb():
    """Release camera from file manager (gvfs) so gphoto2 can claim it."""
    released = False
    try:
        result = subprocess.run(['gio', 'mount', '-l'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            m = re.search(r'(gphoto2://[^\s]+)', line)
            if m:
                subprocess.run(['gio', 'mount', '-u', m.group(1)], capture_output=True, timeout=5)
                released = True
    except Exception:
        pass

    for proc in ['gvfsd-gphoto2', 'gvfs-gphoto2-volume-monitor',
                 'gvfsd-mtp', 'gvfs-mtp-volume-monitor']:
        r = subprocess.run(['pkill', '-f', proc], capture_output=True)
        if r.returncode == 0:
            released = True

    try:
        det = subprocess.run(['gphoto2', '--auto-detect'],
                             capture_output=True, text=True, timeout=5)
        for line in det.stdout.splitlines():
            m = re.search(r'usb:(\d+),(\d+)', line)
            if m:
                usb_path = f'/dev/bus/usb/{m.group(1)}/{m.group(2)}'
                r = subprocess.run(['fuser', '-k', usb_path], capture_output=True)
                if r.returncode == 0:
                    released = True
    except Exception:
        pass

    if released:
        sleep(2)


# ---------------------------------------------------------------------------
# Camera model detection
# ---------------------------------------------------------------------------
def get_model_from_autodetect():
    try:
        result = subprocess.run(['gphoto2', '--auto-detect'],
                                capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            if 'usb:' in line:
                model = re.sub(r'\s+usb:\S+\s*$', '', line).strip()
                if model:
                    return model
    except Exception:
        pass
    return None


def detect_model(config):
    model_name = None
    for status_key in ("model", "cameramodel"):
        if "status" in config and status_key in config.get("status", {}):
            val = str(config["status"][status_key].value)
            if val and not val.isdigit():
                model_name = val
                break
    if not model_name:
        model_name = get_model_from_autodetect() or "Unknown"
    return model_name


# ---------------------------------------------------------------------------
# Camera config helpers
# ---------------------------------------------------------------------------
def camcfg_path(model_name):
    safe_name = model_name.replace(" ", "_")
    return os.path.join(APP_DIR, f"camcfg_{safe_name}.ini")


def generate_camcfg(config, model_name):
    lines = [
        f"# Auto-generated config for {model_name}",
        "# Change this config only if you know what you do!",
        "", "[info]", f"cam = {model_name}", "",
    ]
    for section in config:
        if section in SKIP_SECTIONS:
            continue
        section_lines = []
        for key in config[section]:
            widget = config[section][key]
            try:
                choices = widget._read_choices()
                if len(choices) < 2:
                    continue
                value = widget.value
                section_lines.append(f"# Possible values of {key}: {choices}")
                section_lines.append(f"{key} = {value}")
            except Exception:
                continue
        if section_lines:
            lines.append(f"[{section}]")
            lines.extend(section_lines)
            lines.append("")
    filepath = camcfg_path(model_name)
    with open(filepath, "w") as f:
        f.write("\n".join(lines))
    print(f"[INFO] Generated {filepath}")


def apply_camcfg(config, model_name):
    """Apply camera config from .ini file. Generate if missing."""
    cfg_path = camcfg_path(model_name)
    if not os.path.exists(cfg_path):
        print(f"[INFO] Generating {os.path.basename(cfg_path)}...")
        generate_camcfg(config, model_name)
    camcfg = cp.ConfigParser()
    camcfg.read(cfg_path)
    for section in camcfg.sections():
        if section == "info":
            continue
        for key in camcfg[section]:
            try:
                config[section][key].set(camcfg[section][key])
            except Exception:
                pass


def get_adjustable_params(config):
    params = []
    for section in config:
        if section in SKIP_SECTIONS:
            continue
        for key in config[section]:
            widget = config[section][key]
            try:
                choices = widget._read_choices()
                if len(choices) > 1:
                    current = widget.value
                    index = choices.index(current) if current in choices else 0
                    params.append({
                        "section": section, "key": key,
                        "choices": choices, "index": index,
                    })
            except Exception:
                pass
    return params


def get_camera_settings_info(config):
    """Extract current camera settings for monitoring payload."""
    info = {}
    for key in ("iso", "shutterspeed", "aperture", "imageformat",
                "whitebalance", "autoexposuremode"):
        for section in config:
            if section in SKIP_SECTIONS:
                continue
            if key in config[section]:
                try:
                    info[key] = str(config[section][key].value)
                except Exception:
                    pass
                break
    return info


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def capture_image(cam):
    """Capture image bytes, with fallback for cameras without capturetarget."""
    from gphoto2cffi.backend import lib as _gp_lib, ffi as _gp_ffi
    try:
        return cam.capture()
    except Exception:
        cam_file_path = _gp_ffi.new("CameraFilePath*")
        _gp_lib.gp_camera_capture(cam._cam, _gp_lib.GP_CAPTURE_IMAGE,
                                  cam_file_path, cam._ctx)
        folder = _gp_ffi.string(cam_file_path.folder).decode(errors='replace')
        name = _gp_ffi.string(cam_file_path.name).decode(errors='replace')
        cam_file = _gp_ffi.new("CameraFile**")
        _gp_lib.gp_file_new(cam_file)
        _gp_lib.gp_camera_file_get(cam._cam, folder.encode(), name.encode(),
                                   _gp_lib.GP_FILE_TYPE_NORMAL,
                                   cam_file[0], cam._ctx)
        data_p = _gp_ffi.new("const char**")
        size_p = _gp_ffi.new("unsigned long*")
        _gp_lib.gp_file_get_data_and_size(cam_file[0], data_p, size_p)
        img_data = _gp_ffi.buffer(data_p[0], size_p[0])[:]
        _gp_lib.gp_camera_file_delete(cam._cam, folder.encode(), name.encode(), cam._ctx)
        _gp_lib.gp_file_free(cam_file[0])
        return img_data


# ---------------------------------------------------------------------------
# Console worker
# ---------------------------------------------------------------------------
class CannonWorkerConsole(threading.Thread):
    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, cam, config, schedule, output_dir, instance_name,
                 status_dir, capture_seconds, mqtt_publisher=None, mqtt_prefix="every_camera"):
        super().__init__(daemon=True)
        self.cam = cam
        self.config = config
        self.schedule = schedule
        self.output_dir = output_dir
        self.instance_name = instance_name
        self.status_dir = status_dir
        self.capture_seconds = sorted(capture_seconds)
        self._mqtt = mqtt_publisher
        self._mqtt_prefix = mqtt_prefix
        self._mqtt_topic = f"{mqtt_prefix}/{instance_name}/status"
        self._stop_event = threading.Event()
        self._shots = 0
        self._errors = 0
        self._last_shot = None
        self._last_frame_data = None
        self._active_until = None
        self._status_path = os.path.join(status_dir, f"{os.getpid()}.json")

    def request_stop(self):
        self._stop_event.set()

    def _on_mqtt_command(self, topic, payload):
        """Handle incoming MQTT commands (e.g. get_frame)."""
        if topic.endswith("/cmd/get_frame") and self._last_frame_data:
            import base64
            frame_topic = f"{self._mqtt_prefix}/{self.instance_name}/frame"
            frame_payload = json.dumps({
                "camera_type": "cannon",
                "instance_name": self.instance_name,
                "format": "jpeg",
                "data": base64.b64encode(self._last_frame_data).decode(),
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

        print("[INFO] Cannon measurement started")
        self._save_status("running")

        while not self._stop_event.is_set():
            now = dt.now()

            # Find active schedule interval
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

            # Fire at configured seconds, but only once per (minute, second)
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
        print("[INFO] Cannon measurement stopped")

    def _capture_one(self, now):
        timestamp = now.strftime("%Y%m%dT%H%M%S")
        filepath = os.path.join(self.output_dir, f"{timestamp}.jpeg")
        try:
            img_data = capture_image(self.cam)
            with open(filepath, "wb") as fh:
                fh.write(img_data)
            self._last_frame_data = img_data
            print(f"[INFO] Shot saved: {os.path.basename(filepath)}")
            return True
        except Exception as exc:
            print(f"[ERROR] Capture error: {exc}")
            return False

    def _save_status(self, status):
        cam_info = {}
        try:
            cam_info = get_camera_settings_info(self.config)
        except Exception:
            pass

        payload = {
            "instance_name": self.instance_name,
            "camera_type": "cannon",
            "pid": os.getpid(),
            "status": status,
            "output_dir": self.output_dir,
            "shots_taken": self._shots,
            "last_shot": self._last_shot.isoformat() if self._last_shot else None,
            "active_until": self._active_until.isoformat() if self._active_until else None,
            "errors": self._errors,
            "capture_seconds": self.capture_seconds,
            "last_update": dt.now().isoformat(),
        }
        payload.update(cam_info)
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
def run_console_cannon(config_path=None):
    """Run Canon camera measurement in console mode."""
    from utils import load_config
    from mqtt_client import create_console_publisher

    cfg = load_config(config_path)
    cannon_cfg = cfg.get("cannon", {})
    mqtt_cfg = cfg.get("mqtt", {})

    instance_name = cannon_cfg.get("instance_name") or get_instance_name("Cannon")
    output_dir = cannon_cfg.get("output_dir", "")
    status_dir = cfg.get("status_dir") or str(Path.home() / ".every_camera" / "status")
    schedule_file = cannon_cfg.get("schedule_file", "")
    capture_seconds = cannon_cfg.get("capture_seconds", [0, 30])

    print("=" * 60)
    print("  Every Camera — Canon Console Mode")
    print(f"  Instance      : {instance_name}")
    print(f"  Capture at    : {capture_seconds} seconds of each minute")
    print("=" * 60)

    if not output_dir or not schedule_file:
        print("[INFO] Configuration incomplete. Starting setup wizard...")
        from utils import configure_console_cannon
        configure_console_cannon(cfg, config_path)
        cannon_cfg = cfg.get("cannon", {})
        instance_name = cannon_cfg.get("instance_name") or get_instance_name("Cannon")
        output_dir = cannon_cfg.get("output_dir", "")
        schedule_file = cannon_cfg.get("schedule_file", "")
        capture_seconds = cannon_cfg.get("capture_seconds", [0, 30])
        print(f"  Instance      : {instance_name}")
        print(f"  Capture at    : {capture_seconds} seconds of each minute")

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
    print("[INFO] Releasing camera USB...")
    release_camera_usb()
    try:
        cam = gp.Camera()
    except Exception as exc:
        print(f"[ERROR] Failed to connect camera: {exc}")
        sys.exit(1)

    config = cam._get_config()
    model_name = detect_model(config)
    print(f"[INFO] Connected: {model_name}")

    # Apply camera config
    apply_camcfg(config, model_name)

    # MQTT
    mqtt_pub = create_console_publisher(mqtt_cfg)

    worker = CannonWorkerConsole(
        cam=cam,
        config=config,
        schedule=entries,
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

    if mqtt_pub:
        mqtt_pub.disconnect_broker()
    print("[INFO] Done.")
