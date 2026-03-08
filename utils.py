"""
Shared utilities: config, schedule, encryption, instance naming, status files.
"""
import os
import re
import json
import socket
from datetime import datetime as dt
from dataclasses import dataclass
from pathlib import Path

try:
    from cryptography.fernet import Fernet
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_STATUS_DIR = str(Path.home() / ".every_camera" / "status")
HOME_CONFIG_FILE = str(Path.home() / ".every_camera" / "config.json")
LOCAL_CONFIG_FILE = os.path.join(APP_DIR, "config.json")
KEY_FILE = str(Path.home() / ".every_camera" / ".keyfile")
ENC_PREFIX = "ENC:"
ENCRYPTED_FIELDS = {"mqtt_password", "password"}

SCHEDULE_DT_FMT = "%Y-%m-%d %H:%M:%S"
SCHEDULE_LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*-\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})$'
)


# ---------------------------------------------------------------------------
# GUI availability
# ---------------------------------------------------------------------------
def can_use_gui():
    """Return True if a graphical display is available."""
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# ---------------------------------------------------------------------------
# Network / instance naming
# ---------------------------------------------------------------------------
def get_local_ip():
    """Get local IP address (the one used for default route)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_instance_name(camera_name):
    """Build unique instance name: {camera_name}_{last_IP_octet}."""
    ip = get_local_ip()
    last_octet = ip.split(".")[-1]
    return f"{camera_name}_{last_octet}"


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------
def _get_or_create_key():
    key_dir = os.path.dirname(KEY_FILE)
    os.makedirs(key_dir, exist_ok=True)
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as fh:
            return fh.read().strip()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as fh:
        fh.write(key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass
    return key


def encrypt_value(val):
    if not CRYPTO_AVAILABLE or not val:
        return val
    try:
        return ENC_PREFIX + Fernet(_get_or_create_key()).encrypt(val.encode()).decode()
    except Exception:
        return val


def decrypt_value(val):
    if not CRYPTO_AVAILABLE or not val or not str(val).startswith(ENC_PREFIX):
        return val
    try:
        return Fernet(_get_or_create_key()).decrypt(val[len(ENC_PREFIX):].encode()).decode()
    except Exception:
        return val


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "cannon": {
        "instance_name": "",
        "output_dir": "",
        "schedule_file": "",
        "capture_seconds": [0, 30],
        "camcfg_file": "",
    },
    "sptt": {
        "instance_name": "",
        "output_dir": "",
        "exposure": 880000,
        "gain": 100,
        "binning": 0,
        "encoding": 1,
        "target_temp": None,
        "firmware_dir": "",
    },
    "mqtt": {
        "enabled": False,
        "host": "broker.hivemq.com",
        "port": 1883,
        "user": "",
        "password": "",
        "prefix": "every_camera",
        "tls": False,
    },
    "status_dir": "",
}


def load_config(path=None):
    """Load config from JSON file. Returns merged config with defaults."""
    path = path or LOCAL_CONFIG_FILE
    cfg = _deep_copy(DEFAULT_CONFIG)
    try:
        with open(path) as fh:
            data = json.load(fh)
        _deep_merge(cfg, data)
    except FileNotFoundError:
        save_config(cfg, path)
    except Exception:
        pass
    # Decrypt sensitive fields
    mqtt = cfg.get("mqtt", {})
    for field in ENCRYPTED_FIELDS:
        if field in mqtt:
            mqtt[field] = decrypt_value(mqtt[field])
    return cfg


def save_config(cfg, path=None):
    """Save config to JSON file, encrypting sensitive fields."""
    path = path or LOCAL_CONFIG_FILE
    out = _deep_copy(cfg)
    mqtt = out.get("mqtt", {})
    for field in ENCRYPTED_FIELDS:
        if field in mqtt and mqtt[field]:
            mqtt[field] = encrypt_value(mqtt[field])
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(out, fh, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        print(f"[WARN] Could not save config: {exc}")


def _deep_copy(d):
    return json.loads(json.dumps(d))


def _deep_merge(base, override):
    """Recursively merge override into base dict."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------
@dataclass
class ScheduleEntry:
    start: dt
    end: dt


def parse_schedule_text(text):
    """Parse schedule text. Returns (list[ScheduleEntry], list[str errors])."""
    entries, errors = [], []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('-'):
            continue
        m = SCHEDULE_LINE_RE.match(line)
        if not m:
            errors.append(f"Line {lineno}: invalid format '{line}'")
            continue
        try:
            start = dt.strptime(m.group(1), SCHEDULE_DT_FMT)
            end = dt.strptime(m.group(2), SCHEDULE_DT_FMT)
            if end <= start:
                errors.append(f"Line {lineno}: end must be after start")
                continue
            entries.append(ScheduleEntry(start, end))
        except ValueError as e:
            errors.append(f"Line {lineno}: {e}")
    return entries, errors


def load_schedule_file(filepath):
    with open(filepath, "r") as f:
        return parse_schedule_text(f.read())


def save_schedule_file(filepath, entries):
    with open(filepath, "w") as f:
        f.write("# Measurement Schedule\n")
        f.write(f"# Format: {SCHEDULE_DT_FMT} - {SCHEDULE_DT_FMT}\n\n")
        for e in entries:
            f.write(f"{e.start.strftime(SCHEDULE_DT_FMT)} - {e.end.strftime(SCHEDULE_DT_FMT)}\n")


# ---------------------------------------------------------------------------
# Status file helpers
# ---------------------------------------------------------------------------
def write_status_file(path, data):
    """Atomically write JSON status file."""
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# System info for monitoring
# ---------------------------------------------------------------------------
def get_system_info(output_dir=None):
    """Collect system metrics for monitoring payload."""
    import platform
    info = {
        "hostname": platform.node(),
        "ip": get_local_ip(),
        "platform": platform.system(),
    }
    # CPU usage (non-blocking, quick)
    try:
        with open("/proc/loadavg") as f:
            info["load_avg_1m"] = float(f.read().split()[0])
    except Exception:
        pass
    # Memory
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    mem[parts[0][:-1]] = int(parts[1])
            if "MemTotal" in mem and "MemAvailable" in mem:
                info["mem_used_pct"] = round(
                    100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1
                )
    except Exception:
        pass
    # Disk free
    if output_dir and os.path.isdir(output_dir):
        try:
            st = os.statvfs(output_dir)
            info["disk_free_mb"] = round(st.f_bavail * st.f_frsize / (1024 * 1024))
        except Exception:
            pass
    return info
