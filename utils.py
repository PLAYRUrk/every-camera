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

import console_ui

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_STATUS_DIR = str(Path.home() / ".every_camera" / "status")
LOCAL_CONFIG_FILE = os.path.join(APP_DIR, "config.json")

SCHEDULE_DT_FMT = "%Y-%m-%d %H:%M:%S"

# Minimum interval between status publications. Outside the schedule the
# workers loop twice a second; without this they would write the status file
# and publish a retained MQTT message twice a second, around the clock.
STATUS_MIN_INTERVAL = 5.0
SCHEDULE_LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*-\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})$'
)


# ---------------------------------------------------------------------------
# GUI availability
# ---------------------------------------------------------------------------
def can_use_gui():
    """Return True if a graphical display is available."""
    import sys
    if sys.platform in ("win32", "darwin"):
        return True
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


def sanitize_name(text, fallback="node"):
    """Reduce free text to something safe for topics, file names and locks."""
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_"
                   for ch in str(text or "")).strip("_")
    return safe or fallback


def get_instance_name(camera_name, cfg=None):
    """Build the default instance name: ``{camera_name}_{node_name}``.

    It used to be ``{camera_name}_{last_IP_octet}``, which collided in two
    common cases: two machines whose addresses end in the same octet (.5 in
    192.168.1.x and in 10.0.0.x), and two copies of the program on one machine.
    Since the name drives the MQTT topics, the log file and the preview file,
    a collision meant two cameras silently overwriting each other. The node
    name (config ``node_name``, hostname otherwise) does not have that problem.

    Same-machine duplicates are handled separately by :func:`claim_instance_name`.
    """
    return f"{camera_name}_{sanitize_name(get_node_name(cfg))}"


# ---------------------------------------------------------------------------
# Instance name reservation
# ---------------------------------------------------------------------------
INSTANCE_LOCK_DIR = str(Path.home() / ".every_camera" / "instances")
MAX_INSTANCE_ORDINAL = 64


class InstanceClaim:
    """A held reservation of one instance name. Release it when the run ends."""

    def __init__(self, name, fd=None, path=""):
        self.name = name
        self._fd = fd
        self._path = path

    def release(self):
        """Give the name back. Closing the descriptor is what drops the flock.

        The file itself stays: unlinking it would let a third process create a
        fresh file at the same path and lock *that* inode while a second process
        still holds the old one — two owners of one name. An empty leftover file
        costs nothing and is reused by the next run.
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def claim_instance_name(base_name, lock_dir=None):
    """Reserve ``base_name`` for this process. Returns an :class:`InstanceClaim`.

    While another *live* process on this machine holds the name, ``-2``, ``-3``
    … are appended, so several copies of every-camera can run from one
    identical config.json without sharing MQTT topics, log files or preview
    files. The reservation is a ``flock`` on a file in ``~/.every_camera/instances``:
    the kernel drops it when the process dies, so a crashed run frees its name
    without leaving anything to clean up.

    Platforms without ``fcntl`` (Windows, where only the observer tools run)
    get the name unchanged and no reservation.
    """
    base_name = sanitize_name(base_name, fallback="camera")
    try:
        import fcntl
    except ImportError:
        return InstanceClaim(base_name)

    directory = lock_dir or INSTANCE_LOCK_DIR
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        console_ui.warn(f"Cannot create {directory}: {exc}. "
                        f"Instance name {base_name} is not reserved.")
        return InstanceClaim(base_name)

    for ordinal in range(1, MAX_INSTANCE_ORDINAL + 1):
        name = base_name if ordinal == 1 else f"{base_name}-{ordinal}"
        path = os.path.join(directory, f"{name}.lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)          # held by a live process — try the next name
            continue
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        if ordinal > 1:
            console_ui.warn(f"Instance name '{base_name}' is already used by "
                            f"another process on this machine — running as '{name}'.")
        return InstanceClaim(name, fd, path)

    console_ui.warn(f"All {MAX_INSTANCE_ORDINAL} variants of '{base_name}' are "
                    f"taken; running unreserved.")
    return InstanceClaim(base_name)


def get_node_name(cfg=None):
    """Human-readable name of this machine, for the observer's programs.

    Scanning the LAN used to show bare IP addresses, which say nothing about
    which telescope hut a node is standing in. ``node_name`` in config.json fixes
    that; it describes the *machine*, not one camera, because a single node may
    run several cameras. The hostname is the fallback, so the name is never
    empty and an unconfigured node still looks sensible.
    """
    name = ""
    if isinstance(cfg, dict):
        name = str(cfg.get("node_name", "") or "").strip()
    if name:
        return name
    try:
        return socket.gethostname()
    except Exception:
        return "every-camera"


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
        "capture_seconds": [0, 30],
        "exposure": 0.88,
        "gain": 100,
        "binning": 0,
        "encoding": 1,
        "target_temp": None,
        "firmware_dir": "",
    },
    "infra": {
        "instance_name": "",
        "output_dir": "",
        "schedule_file": "",
        "capture_seconds": [0, 30],
        "exposure_us": 1000.0,
        "gain": 0,
        "roi": "1280x1024",
        "save_format": "tiff",
    },
    # ASI all-sky imager: Princeton PIXIS through PICAM + SmartMotor filter wheel.
    # The schedule lives here rather than in a separate file; ``schedule_file``
    # is only for stations that already keep a legacy asi-camera schedule.txt.
    "asi": {
        "instance_name": "",
        "output_dir": "",
        "mode": "sun",                  # "sun" or "time"
        "sun_max_angle": -10.0,         # sun mode: start below this solar altitude
        "t_start": "20:00",             # time mode: phase reference of the cycle
        "dark_frames": 3,
        "dead_time": 5.0,
        "wait_for_enter": True,         # time mode: wait for the operator to start
        "schedule_file": "",            # optional legacy schedule.txt
        "schedule": [],                 # sun:  {"filter":1,"exposure":55,"seconds":[0]}
                                        # time: {"delta":100,"filter":3,
                                        #        "exposure":25,"binning":1}
        "camera": {
            "backend": "picam",         # "picam" (real SDK) or "sim"
            "readout_speed": 2.0,
            "binning": 4,
            "gain": 1,                  # 1 Low, 2 Medium, 3 High
            "frame_timeout_ms": 30000,
            "readout_control_mode": 1,
            "shutter_timing_mode": 1,
            "output_signal": 4,
            "kinetics_window_height": 50,
            "demo": False,
            "demo_model": "Pixis1024F",
        },
        "cooling": {
            "enabled": True,
            "target_temp": -60.0,
            "tolerance": 3.0,
            "wait_on_start": True,
            "wait_timeout": 1800.0,
            "warm_on_exit": True,
            "warm_temp": 13.0,
            "warm_timeout": 900.0,
        },
        "filter_wheel": {
            "port": "/dev/ttyUSB0",     # "sim" selects the simulator
            "baudrate": 9600,
            "move_timeout": 8.0,
        },
        "location": {
            "lat": 0.0,
            "lon": 0.0,
            "elevation": 0.0,
        },
    },
    "sentry": {
        "instance_name": "",
        "imagerd_rt_dir": "/usr/local/imagerd_rt",
        "output_dir": "",
        "device_id": "ASI1",
        "site_id": "SITE",
        "zenith_angle_start": -10,
        "schedule_len_ms": 1440000,
        "imaging_mode": "schedule",
        "capture_seconds": [0, 30],
        "slots": [],
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
    "server": {
        "enabled": True,
        "bind": "0.0.0.0",
        # Preferred port. When it is taken — the normal case when several
        # cameras run from this same config.json — the server moves up to
        # port + port_search. Nothing is written back: the choice is per run.
        "port": 8765,
        "port_search": 20,      # 0 = use "port" or nothing, as before
        "discovery": True,
        "discovery_port": 45455,
        "max_list": 2000,
        "focus_ttl": 60,
    },
    # Friendly name of this machine, shown by viewer_app/focus_app/monitor
    # instead of a bare IP. Empty means "use the hostname".
    "node_name": "",
    "status_dir": "",
}


def load_config(path=None):
    """Load config from JSON file. Returns merged config with defaults.

    A malformed config used to be swallowed silently, so a single typo made
    every setting revert to defaults — and the GUI then wrote those defaults
    back over the file. Now the error is reported and the original is kept as
    a ``.bak`` copy before anything can overwrite it.
    """
    path = path or LOCAL_CONFIG_FILE
    cfg = _deep_copy(DEFAULT_CONFIG)
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value is not an object")
        _deep_merge(cfg, data)
    except FileNotFoundError:
        save_config(cfg, path)
    except (json.JSONDecodeError, ValueError) as exc:
        backup = path + ".bak"
        try:
            import shutil
            shutil.copy2(path, backup)
            hint = f"Original kept as {backup}."
        except Exception:
            hint = "Could not back it up."
        console_ui.error(f"Config {path} is not valid JSON: {exc}\n"
                         f"        Falling back to defaults. {hint}")
    except Exception as exc:
        console_ui.error(f"Could not read config {path}: {exc}. Using defaults.")
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
        console_ui.warn(f"Could not save config: {exc}")


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


def load_schedule_safe(filepath):
    """Load a schedule without ever raising. Returns ``(entries, errors, reason)``.

    ``reason`` is None when a usable schedule was loaded; otherwise it is a
    short explanation of why the camera cannot measure, and the caller is
    expected to start in setup mode rather than abort. Aborting used to be the
    only option, which meant a freshly installed camera could not even be
    focused until someone invented a schedule for it.

    ``errors`` holds per-line complaints from :func:`parse_schedule_text`; they
    are worth reporting even when enough valid lines remain.
    """
    if not str(filepath or "").strip():
        return [], [], "schedule_file is not configured"
    try:
        entries, errors = load_schedule_file(filepath)
    except FileNotFoundError:
        return [], [], f"schedule file not found: {filepath}"
    except IsADirectoryError:
        return [], [], f"schedule file is a directory: {filepath}"
    except UnicodeDecodeError:
        return [], [], f"schedule file is not readable text: {filepath}"
    except OSError as exc:
        return [], [], f"cannot read schedule file {filepath}: {exc}"
    if not entries:
        return [], errors, f"no valid intervals in {filepath}"
    return entries, errors, None


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
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def pid_alive(pid):
    """Return True if a process with this PID currently exists."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=5)
            return str(pid) in out.stdout
        except Exception:
            return True  # cannot tell — assume alive rather than hide a live node
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True      # exists, owned by another user
    except OSError:
        return True
    return True


def cleanup_stale_status_files(status_dir):
    """Remove ``{pid}.json`` status files left behind by dead processes.

    Without this, a crashed or SIGKILLed worker stays in the monitor's local
    list forever, since only a clean shutdown removes its own file.
    """
    removed = 0
    if not status_dir or not os.path.isdir(status_dir):
        return removed
    try:
        names = os.listdir(status_dir)
    except OSError:
        return removed
    for name in names:
        stem, ext = os.path.splitext(name)
        # Console workers write "{pid}.json"; the GUI, which can run several
        # cameras in one process, writes "{pid}_{camera}.json".
        pid_part = stem.split("_", 1)[0]
        if ext != ".json" or not pid_part.isdigit():
            continue
        if pid_alive(pid_part):
            continue
        try:
            os.remove(os.path.join(status_dir, name))
            removed += 1
        except OSError:
            pass
    return removed


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


def configure_console_infra(cfg, config_path=None):
    """Interactive configuration for Infra camera console mode."""
    infra = cfg.get("infra", {})
    print("\n--- Infra Camera (SW1300 SWIR) Configuration ---\n")

    infra["output_dir"] = _ask("Output directory for images", infra.get("output_dir", ""))
    infra["schedule_file"] = _ask("Schedule file path", infra.get("schedule_file", ""))
    infra["instance_name"] = _ask("Instance name (auto if empty)",
                                   infra.get("instance_name", ""))
    secs_str = _ask("Capture seconds (comma-separated)",
                     ", ".join(str(s) for s in infra.get("capture_seconds", [0, 30])))
    try:
        infra["capture_seconds"] = [int(s.strip()) for s in secs_str.split(",") if s.strip()]
    except ValueError:
        infra["capture_seconds"] = [0, 30]

    infra["exposure_us"] = _ask_float("Exposure (microseconds)", infra.get("exposure_us", 1000.0))
    infra["gain"] = _ask_int("Gain (0-120)", infra.get("gain", 0))
    roi = _ask("ROI (1280x256 or 1280x1024)", infra.get("roi", "1280x1024"))
    infra["roi"] = roi if roi in ("1280x256", "1280x1024") else "1280x1024"
    fmt = _ask("Save format (tiff, png or fits)", infra.get("save_format", "tiff"))
    infra["save_format"] = fmt if fmt in ("tiff", "png", "fits") else "tiff"

    cfg["infra"] = infra
    _configure_mqtt(cfg)

    save_config(cfg, config_path)
    print("\nConfiguration saved.\n")


def configure_console_sentry(cfg, config_path=None):
    """Interactive configuration for Sentry (imagerd_rt) camera console mode."""
    sentry = cfg.get("sentry", {})
    print("\n--- Sentry (imagerd_rt) Camera Configuration ---\n")

    sentry["imagerd_rt_dir"] = _ask(
        "imagerd_rt installation directory",
        sentry.get("imagerd_rt_dir", "/usr/local/imagerd_rt"),
    )
    sentry["output_dir"] = _ask(
        "Output directory for archived FITS files (optional)",
        sentry.get("output_dir", ""),
    )
    sentry["instance_name"] = _ask(
        "Instance name (auto if empty)",
        sentry.get("instance_name", ""),
    )
    sentry["device_id"] = _ask("Camera device ID", sentry.get("device_id", "ASI1"))
    sentry["site_id"] = _ask("Site ID", sentry.get("site_id", "SITE"))
    sentry["zenith_angle_start"] = _ask_float(
        "Solar zenith angle to start imaging (degrees, negative = below horizon)",
        sentry.get("zenith_angle_start", -10),
    )
    sentry["schedule_len_ms"] = _ask_int(
        "Schedule cycle length (ms)", sentry.get("schedule_len_ms", 1440000)
    )
    cur_mode = sentry.get("imaging_mode", "schedule")
    if isinstance(cur_mode, int):
        cur_mode = "schedule" if cur_mode == 0 else "rapid"
    mode_in = _ask("Imaging mode (schedule / rapid)", cur_mode)
    sentry["imaging_mode"] = mode_in if mode_in in ("schedule", "rapid") else "schedule"

    secs_str = _ask(
        "Capture seconds for MQTT publish (comma-separated)",
        ", ".join(str(s) for s in sentry.get("capture_seconds", [0, 30])),
    )
    try:
        sentry["capture_seconds"] = [int(s.strip()) for s in secs_str.split(",") if s.strip()]
    except ValueError:
        sentry["capture_seconds"] = [0, 30]

    # Slot editor
    if _ask_bool("Configure imaging slots now?", bool(sentry.get("slots"))):
        slots = []
        while True:
            print(f"\n  Slot {len(slots) + 1}:")
            slot = {
                "filter_num":  _ask_int("    Filter number", 1),
                "start_ms":    _ask_int("    Start time in cycle (ms)", 0),
                "exposure_ms": _ask_int("    Exposure length (ms)", 55000),
                "binning":     _ask_int("    Binning (1/2/4)", 2),
                "gain":        _ask_int("    CCD gain (1-3)", 3),
                "readout_ms":  _ask_int("    Filter wheel + readout overhead (ms)", 5000),
            }
            slots.append(slot)
            if not _ask_bool("  Add another slot?", False):
                break
        sentry["slots"] = slots

    cfg["sentry"] = sentry
    _configure_mqtt(cfg)

    save_config(cfg, config_path)
    print("\nConfiguration saved.\n")


def configure_console_asi(cfg, config_path=None):
    """Interactive configuration for the ASI all-sky imager console mode."""
    asi = cfg.get("asi", _deep_copy(DEFAULT_CONFIG["asi"]))
    camera = asi.get("camera", _deep_copy(DEFAULT_CONFIG["asi"]["camera"]))
    cooling = asi.get("cooling", _deep_copy(DEFAULT_CONFIG["asi"]["cooling"]))
    wheel = asi.get("filter_wheel", _deep_copy(DEFAULT_CONFIG["asi"]["filter_wheel"]))
    location = asi.get("location", _deep_copy(DEFAULT_CONFIG["asi"]["location"]))
    print("\n--- ASI (Princeton PIXIS) Camera Configuration ---\n")

    asi["output_dir"] = _ask("Output directory for FITS files", asi.get("output_dir", ""))
    asi["instance_name"] = _ask("Instance name (auto if empty)",
                                asi.get("instance_name", ""))

    backend = _ask("Camera backend (picam / sim)", camera.get("backend", "picam"))
    camera["backend"] = backend if backend in ("picam", "sim") else "picam"
    camera["binning"] = _ask_int("Binning (1/2/4/8)", camera.get("binning", 4))
    camera["gain"] = _ask_int("Analog gain (1=Low, 2=Medium, 3=High)",
                              camera.get("gain", 1))
    camera["readout_speed"] = _ask_float("ADC readout speed (MHz)",
                                         camera.get("readout_speed", 2.0))

    cooling["enabled"] = _ask_bool("Use the cooler?", cooling.get("enabled", True))
    if cooling["enabled"]:
        cooling["target_temp"] = _ask_float("Sensor setpoint (°C)",
                                            cooling.get("target_temp", -60.0))
        cooling["wait_on_start"] = _ask_bool("Wait for the setpoint before measuring?",
                                             cooling.get("wait_on_start", True))
        cooling["warm_on_exit"] = _ask_bool("Warm the sensor up on exit?",
                                            cooling.get("warm_on_exit", True))

    wheel["port"] = _ask("Filter wheel serial port ('sim' for the simulator)",
                         wheel.get("port", "/dev/ttyUSB0"))
    if wheel["port"].strip().lower() != "sim":
        wheel["baudrate"] = _ask_int("Filter wheel baudrate", wheel.get("baudrate", 9600))

    location["lat"] = _ask_float("Site latitude (degrees)", location.get("lat", 0.0))
    location["lon"] = _ask_float("Site longitude (degrees)", location.get("lon", 0.0))
    location["elevation"] = _ask_float("Site elevation (m)",
                                       location.get("elevation", 0.0))

    mode = _ask("Schedule mode (sun / time)", asi.get("mode", "sun"))
    asi["mode"] = mode if mode in ("sun", "time") else "sun"
    if asi["mode"] == "sun":
        asi["sun_max_angle"] = _ask_float(
            "Start when the solar altitude drops below (degrees)",
            asi.get("sun_max_angle", -10.0))
    else:
        asi["t_start"] = _ask("Cycle phase reference T_start (HH:MM)",
                              asi.get("t_start", "20:00"))
    asi["dark_frames"] = _ask_int("Dark frames per exposure",
                                  asi.get("dark_frames", 3))
    asi["dead_time"] = _ask_float("Dead time between cycles (s)",
                                  asi.get("dead_time", 5.0))

    schedule_file = _ask("Legacy schedule.txt path (empty = use the slots below)",
                         asi.get("schedule_file", ""))
    asi["schedule_file"] = schedule_file
    if not schedule_file and _ask_bool("Enter the schedule now?",
                                       not asi.get("schedule")):
        slots = []
        while True:
            print(f"\n  Slot {len(slots) + 1}:")
            slot = {
                "filter": _ask_int("    Filter (1-6)", 1),
                "exposure": _ask_float("    Exposure (s)", 55.0),
            }
            if asi["mode"] == "time":
                slot["delta"] = _ask_float("    Offset from the cycle start (s)", 0.0)
                slot["binning"] = _ask_int("    Binning", camera.get("binning", 4))
            else:
                secs = _ask("    Capture seconds within the minute (comma-separated)",
                            "0")
                try:
                    slot["seconds"] = [int(s.strip()) for s in secs.split(",")
                                       if s.strip()]
                except ValueError:
                    slot["seconds"] = [0]
            slots.append(slot)
            if not _ask_bool("  Add another slot?", False):
                break
        asi["schedule"] = slots

    asi["camera"] = camera
    asi["cooling"] = cooling
    asi["filter_wheel"] = wheel
    asi["location"] = location
    cfg["asi"] = asi
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
    _configure_server(cfg)


def _configure_server(cfg):
    """Interactive LAN frame-server configuration (shared)."""
    srv = cfg.get("server", _deep_copy(DEFAULT_CONFIG["server"]))
    print("\n  The LAN frame server lets viewer_app.py and focus_app.py browse")
    print("  captured frames and watch a live stream over the local network.")
    # Asked here because it is what the observer sees when scanning the network.
    cfg["node_name"] = _ask(
        "Name of this machine, shown to observers instead of its IP",
        cfg.get("node_name", "") or socket.gethostname())
    if _ask_bool("Enable LAN frame server?", srv.get("enabled", True)):
        srv["enabled"] = True
        srv["port"] = _ask_int("HTTP port", srv.get("port", 8765))
        # A busy port is normal when several cameras share one config.json, so
        # the server moves up instead of giving up. Nothing is written back.
        srv["port_search"] = _ask_int(
            "If that port is busy, how many above it to try (0 = none)",
            srv.get("port_search", 20))
        srv["bind"] = _ask("Bind address (0.0.0.0 = whole LAN)",
                           srv.get("bind", "0.0.0.0"))
        srv["discovery"] = _ask_bool("Answer UDP discovery probes?",
                                     srv.get("discovery", True))
        if srv["discovery"]:
            srv["discovery_port"] = _ask_int("Discovery UDP port",
                                             srv.get("discovery_port", 45455))
    else:
        srv["enabled"] = False
    cfg["server"] = srv
