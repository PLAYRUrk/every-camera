"""
Shared utilities: config, schedule, instance naming, status files.
"""
import os
import re
import json
import socket
from datetime import datetime as dt
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_STATUS_DIR = str(Path.home() / ".every_camera" / "status")
HOME_CONFIG_FILE = str(Path.home() / ".every_camera" / "config.json")
LOCAL_CONFIG_FILE = os.path.join(APP_DIR, "config.json")
LOCAL_MQTT_FILE = os.path.join(APP_DIR, "mqtt.json")

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
        "exposure": 0.88,
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
    return cfg


def save_config(cfg, path=None):
    """Save config to JSON file."""
    path = path or LOCAL_CONFIG_FILE
    out = _deep_copy(cfg)
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


# ---------------------------------------------------------------------------
# Interactive console configuration
# ---------------------------------------------------------------------------
def _ask(prompt, default=""):
    """Ask user for input with a default value."""
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def _ask_bool(prompt, default=False):
    """Ask user for yes/no."""
    suffix = " [Y/n]" if default else " [y/N]"
    val = input(f"{prompt}{suffix}: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "1", "true", "да")


def _ask_int(prompt, default=0):
    """Ask user for integer."""
    val = _ask(prompt, str(default))
    try:
        return int(val)
    except ValueError:
        return default


def _ask_float(prompt, default=0.0):
    """Ask user for float."""
    val = _ask(prompt, str(default))
    try:
        return float(val)
    except ValueError:
        return default


def configure_console_cannon(cfg, config_path=None):
    """Interactive configuration for Canon camera console mode."""
    cannon = cfg.get("cannon", {})
    print("\n--- Canon Camera Configuration ---\n")

    cannon["output_dir"] = _ask("Output directory for images", cannon.get("output_dir", ""))
    cannon["schedule_file"] = _ask("Schedule file path", cannon.get("schedule_file", ""))
    cannon["instance_name"] = _ask("Instance name (auto if empty)",
                                    cannon.get("instance_name", ""))
    secs_str = _ask("Capture seconds (comma-separated)",
                     ", ".join(str(s) for s in cannon.get("capture_seconds", [0, 30])))
    try:
        cannon["capture_seconds"] = [int(s.strip()) for s in secs_str.split(",") if s.strip()]
    except ValueError:
        cannon["capture_seconds"] = [0, 30]

    cfg["cannon"] = cannon
    _configure_mqtt(cfg)

    save_config(cfg, config_path)
    print("\nConfiguration saved.\n")


def configure_console_sptt(cfg, config_path=None):
    """Interactive configuration for SPTT camera console mode."""
    sptt = cfg.get("sptt", {})
    print("\n--- SPTT (CSDU-429) Camera Configuration ---\n")

    sptt["output_dir"] = _ask("Output directory for FITS files", sptt.get("output_dir", ""))
    sptt["instance_name"] = _ask("Instance name (auto if empty)",
                                  sptt.get("instance_name", ""))
    sptt["exposure"] = _ask_float("Exposure (seconds)", sptt.get("exposure", 0.88))
    sptt["gain"] = _ask_int("Gain (0-1023)", sptt.get("gain", 100))
    sptt["binning"] = _ask_int("Binning (0=1x1, 1=2x2, 3=4x4)", sptt.get("binning", 0))
    enc = _ask_int("Encoding (0=8bit, 1=12bit)", sptt.get("encoding", 1))
    sptt["encoding"] = enc if enc in (0, 1) else 1

    cfg["sptt"] = sptt
    _configure_mqtt(cfg)

    save_config(cfg, config_path)
    print("\nConfiguration saved.\n")


def _configure_mqtt(cfg):
    """Interactive MQTT configuration (shared)."""
    mqtt = cfg.get("mqtt", {})
    if _ask_bool("Configure MQTT?", mqtt.get("enabled", False)):
        mqtt["enabled"] = True
        mqtt["host"] = _ask("MQTT broker host", mqtt.get("host", "broker.hivemq.com"))
        mqtt["port"] = _ask_int("MQTT port", mqtt.get("port", 1883))
        mqtt["user"] = _ask("MQTT username (optional)", mqtt.get("user", ""))
        mqtt["password"] = _ask("MQTT password (optional)", mqtt.get("password", ""))
        mqtt["prefix"] = _ask("MQTT topic prefix", mqtt.get("prefix", "every_camera"))
        mqtt["tls"] = _ask_bool("Use TLS?", mqtt.get("tls", False))
    else:
        mqtt["enabled"] = False
    cfg["mqtt"] = mqtt
