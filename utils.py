import os
import re
import json
import shutil
import socket
from datetime import datetime as dt, timezone
from dataclasses import dataclass
from pathlib import Path

import console_ui

APP_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_STATUS_DIR = str(Path.home() / ".every_camera" / "status")
LOCAL_CONFIG_FILE = os.path.join(APP_DIR, "config.json")

SCHEDULE_DT_FMT = "%Y-%m-%d %H:%M:%S"

# Outside the schedule the workers loop twice a second; without this they would
# write the status file that often, around the clock.
STATUS_MIN_INTERVAL = 5.0
SCHEDULE_LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*-\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})$'
)


def can_use_gui():
    import sys
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def get_local_ip():
    """The address used for the default route."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def sanitize_name(text, fallback="node"):
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_"
                   for ch in str(text or "")).strip("_")
    return safe or fallback


def get_instance_name(camera_name, cfg=None):
    """The name drives the log and preview files, so it must not collide across
    machines.

    Same-machine duplicates are handled by :func:`claim_instance_name`.
    """
    return f"{camera_name}_{sanitize_name(get_node_name(cfg))}"


INSTANCE_LOCK_DIR = str(Path.home() / ".every_camera" / "instances")
MAX_INSTANCE_ORDINAL = 64


class InstanceClaim:
    def __init__(self, name, fd=None, path=""):
        self.name = name
        self._fd = fd
        self._path = path

    def release(self):
        """Closing the descriptor drops the lock, as process death does on both
        platforms: a crashed run must not leave its name reserved.

        The file stays: unlinking it would let a third process lock a fresh inode at the
        same path while a second still holds the old one, two owners of one name.
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


def _lock_file_exclusive(fd):
    """False when someone holds the lock. ``NotImplementedError`` where neither module
    exists tells the caller to run without a reservation rather than not run.

    ``msvcrt.locking`` is a *mandatory* byte-range lock, and the file is created empty,
    so it takes offset 0, length 1, whether or not that byte exists yet.
    """
    try:
        import fcntl
    except ImportError:
        pass
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    try:
        import msvcrt
    except ImportError:
        raise NotImplementedError("no file locking on this platform")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _same_file(fd, path):
    """A lock taken on an inode another process has just unlinked reserves nothing."""
    try:
        held, current = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _replace_foreign_lock(path):
    """A lock file left by another user — a run as root before ``User=`` was set — opens
    read-only only, but the directory still lets its owner replace it.

    True: replaced, try again. False: a live process holds it. None: cannot tell.
    """
    if os.name != "posix":
        return None
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        if not _lock_file_exclusive(fd):
            return False
        os.unlink(path)
        return True
    except OSError as exc:
        console_ui.warn(f"Cannot replace {path}, left by another user: {exc}. "
                        f"Run: sudo chown -R $(id -un): "
                        f"{os.path.dirname(os.path.dirname(path))}")
        return None
    finally:
        os.close(fd)


def _open_lock_file(path, attempts=3):
    """A locked descriptor of ``path``, or None when the name is taken or unusable."""
    for _ in range(attempts):
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except PermissionError:
            if _replace_foreign_lock(path):
                continue
            return None
        except OSError:
            return None
        try:
            locked = _lock_file_exclusive(fd)
        except NotImplementedError:
            os.close(fd)
            raise
        if locked and _same_file(fd, path):
            return fd
        os.close(fd)
        if not locked:
            return None
    return None


def claim_instance_name(base_name, lock_dir=None):
    """Appends ``-2``, ``-3`` ... while a *live* process holds the name, so several
    copies can run from one identical config.json without sharing log or preview files.

    The kernel drops the lock when the process dies, so a crashed run leaves nothing to
    clean up. A platform with no file locking gets the name unchanged, with a warning:
    two copies would share status, log and preview file names.
    """
    base_name = sanitize_name(base_name, fallback="camera")
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
            fd = _open_lock_file(path)
        except NotImplementedError:
            console_ui.warn(f"This platform offers no file locking; instance "
                            f"name '{base_name}' is not reserved. Run one copy "
                            f"at a time, or set distinct instance names.")
            return InstanceClaim(base_name)
        if fd is None:
            continue              # held by a live process — try the next name
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
    """Describes the *machine*, not one camera: a node may run several. The hostname is
    the fallback, so the name is never empty.
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


DEFAULT_CONFIG = {
    "cannon": {
        "instance_name": "",
        "output_dir": "",
        "schedule_file": "",
        "capture_seconds": [0, 30],
        "camcfg_file": "",
        "shutterspeed": "",
    },
    "sptt": {
        "instance_name": "",
        "output_dir": "",
        "capture_seconds": [0, 30],
        "exposure": 0.88,
        # None: the frame period follows the exposure. A period shorter than
        # the exposure truncates the integration instead of slowing the camera.
        "period_us": None,
        "trigmode": None,
        "gain": 100,
        "binning": 0,
        "encoding": 1,
        "target_temp": None,
        "firmware_dir": "",
        # Which camera, by physical USB port ("1-2.3"), when the machine has
        # several; empty = the only one. --type sptt --probe lists them.
        "usb_port": "",
        # Colour models only.
        "debayer": False,               # save RGB instead of the raw mosaic
        "bayer_pattern": "RGGB",
        "r_offset": None,               # None: the camera keeps its own
        "g_offset": None,
        # "always": around the clock; "sun": only while the sun is at or below
        # sun_max_angle. The process waits for the next night instead of exiting.
        "mode": "always",
        "sun_max_angle": -10.0,
        "location": {
            "lat": 0.0,
            "lon": 0.0,
            "elevation": 0.0,
        },
        # mode "sun" only: open the window at sun_start_angle and hold a mean frame
        # intensity until the sun reaches sun_max_angle. Same keys as asi.preflight.
        "preflight": {
            "enabled": False,
            "sun_start_angle": -6.0,    # must be ABOVE sun_max_angle
            "target_mean": 20000.0,     # mean frame intensity to hold, ADU 0..65535
            "tolerance": 0.15,          # deadband, as a fraction of the target
            "min_exposure": 0.05,       # shortest exposure the loop may pick, s
            "max_exposure": None,       # None = no longer than "exposure"
            "max_step": 4.0,            # largest exposure change per measurement
            "bias": 0.0,                # pedestal, ADU: SPTT takes no darks
        },
        # Divide a tick's frame into shorter sub-frames within exposure + readout
        # when it comes back over the threshold. Same keys as asi.overexposure.
        "overexposure": {
            "enabled": False,
            "threshold": 55000.0,       # mean ADU above which the frame divides
            "release": 0.85,            # hysteresis on the way back to one frame
            "max_splits": 4,
            "min_exposure": 0.05,       # shortest sub-frame, s
            "margin": 0.5,              # slack kept inside the frame budget, s
            "min_frame_gap": 1.0,       # sub-frames stay this far apart, s
            "bias": 0.0,                # pedestal, ADU: these cameras take no darks
        },
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
        # Same as sptt.overexposure; a frame interval measured on the stream is the
        # dead time, so millisecond exposures cannot be divided.
        "overexposure": {
            "enabled": False,
            "threshold": 55000.0,       # mean ADU above which the frame divides
            "release": 0.85,            # hysteresis on the way back to one frame
            "max_splits": 4,
            "min_exposure": 0.05,       # shortest sub-frame, s
            "margin": 0.5,              # slack kept inside the frame budget, s
            "min_frame_gap": 1.0,       # sub-frames stay this far apart, s
            "bias": 0.0,                # pedestal, ADU: these cameras take no darks
        },
    },
    # ASI all-sky imager: Princeton PIXIS through PICAM + SmartMotor filter wheel.
    # The schedule lives here rather than in a separate file; ``schedule_file``
    # is only for stations that already keep a legacy asi-camera schedule.txt.
    "asi": {
        "instance_name": "",
        "output_dir": "",
        "mode": "sun",                  # "sun", "time" or "sun_cycle"
        "sun_max_angle": -10.0,         # sun/sun_cycle: start below this altitude
        "t_start": "20:00",             # cycle phase reference: local time in
                                        # time mode, UTC in sun_cycle mode
        "schedule_len": None,           # cycle length, s (None = derive from slots)
        "dark_frames": 3,
        "dead_time": 5.0,
        # sun_cycle only: shoot the bright twilight between two solar setpoints,
        # on the main schedule but with the exposure chosen automatically to
        # hold a mean frame intensity. Off by default.
        "preflight": {
            "enabled": False,
            "sun_start_angle": -6.0,    # first setpoint; must be ABOVE sun_max_angle
            "target_mean": 20000.0,     # mean frame intensity to hold, ADU 0..65535
            "tolerance": 0.15,          # deadband, as a fraction of the target
            "min_exposure": 0.05,       # shortest exposure the loop may pick, s
            "max_exposure": None,       # None = no longer than the slot's own
            "max_step": 4.0,            # largest exposure change per measurement
            "bias": 0.0,                # pedestal, ADU; used when there are no darks
            "binning": None,            # None: each slot keeps its schedule binning
        },
        # Cycle modes: divide a slot's frame into shorter sub-frames when it
        # comes back over the threshold, so a filter cannot saturate. Off by
        # default.
        "overexposure": {
            "enabled": False,
            "threshold": 55000.0,       # mean ADU above which the slot divides
            "release": 0.85,            # hysteresis on the way back to one frame
            "max_splits": 4,
            "min_exposure": 0.05,       # shortest sub-frame, s
            "margin": 0.5,              # slack kept inside the slot budget, s
            "min_frame_gap": 1.0,       # sub-frames stay this far apart, s
            "bias": 0.0,                # pedestal, ADU; used when there are no darks
        },
        "wait_for_enter": True,         # time mode: wait for the operator to start
        "schedule_file": "",            # legacy schedule.txt or converted .json
        "schedule": [],                 # sun:   {"filter":1,"exposure":55,"seconds":[0]}
                                        # cycle: {"delta":100,"filter":3,"exposure":25,
                                        #         "binning":1,"gain":3,"readout":5}
        # Written into every frame's name and its legacy FITS headers, exactly
        # as imagerd_rt wrote them.
        "site_id": "",
        "device_id": "",
        "legacy_version": "3.0",        # the "Version" header the archive carries
        "fw_temp": None,                # no sensor on this wheel; None -> -999.0
        "filters": [                    # wavelength tags used in names and headers
            {"slot": 1, "wavelength": "5577", "description": "557.7nm x 2.0nm"},
            {"slot": 2, "wavelength": "6300", "description": "630.0nm x 2.0nm"},
            {"slot": 3, "wavelength": "OH__",
             "description": "Broadband OH with 18 nm notch at 865.0nm"},
            {"slot": 4, "wavelength": "8400", "description": "840.0nm x 1.8nm"},
            {"slot": 5, "wavelength": "8465", "description": "846.5nm x 1.8nm"},
            {"slot": 6, "wavelength": "8570", "description": "857.0nm x 1.8nm"},
        ],
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
        # Filter wheel temperature controller, read only. "auto" finds it among the
        # USB-serial ports, "sim" is the simulator, "" turns polling off.
        "omega": {
            "port": "auto",
            "baudrate": 9600,
            "poll_seconds": 10.0,
        },
        "location": {
            "lat": 0.0,
            "lon": 0.0,
            "elevation": 0.0,
        },
    },
    # Japan all-sky imager: Hamamatsu through DCAM-API + the same SmartMotor filter
    # wheel the ASI camera uses. No analog gain. Its sun_cycle mode
    # runs the ASI imager's night, intensity-control loops and all.
    "japan": {
        "instance_name": "",
        "output_dir": "",               # sun/time: frames land here flat, one dir
                                        # per night; sun_cycle: YYYY/MM/DD tree
        "name": "",                     # instrument name: the NAME card and the
                                        # sun_cycle file name
        "mode": "sun",                  # "sun", "time" or "sun_cycle"
        "sun_max_angle": -10.0,         # sun/sun_cycle: start below this altitude
        "t_start": "20:00",             # cycle phase reference: local time in
                                        # time mode, UTC in sun_cycle mode
        "dark_frames": 3,
        "dead_time": 5.0,
        "schedule_len": None,           # cycle modes: period, s; None -> derived.
                                        # A schedule file may state its own with a
                                        # "period = 1440" header, which is the place
                                        # it stays in step with the slots below it.
        "wait_for_enter": True,         # time mode: wait for the operator to start
        "schedule_file": "",            # legacy japan-camera schedule.txt, or .json
        "schedule": [],                 # sun:  {"filter":3,"exposure":30,"seconds":[0,30]}
                                        # time/sun_cycle: {"delta":100,"filter":3,
                                        #        "exposure":25,"binning":1}
        # sun_cycle only: shoot the bright twilight between two solar setpoints,
        # on the main schedule but with the exposure chosen automatically to
        # hold a mean frame intensity. Off by default. Same keys as asi.preflight.
        "preflight": {
            "enabled": False,
            "sun_start_angle": -6.0,    # first setpoint; must be ABOVE sun_max_angle
            "target_mean": 20000.0,     # mean frame intensity to hold, ADU 0..65535
            "tolerance": 0.15,          # deadband, as a fraction of the target
            "min_exposure": 0.05,       # shortest exposure the loop may pick, s
            "max_exposure": None,       # None = no longer than the slot's own
            "max_step": 4.0,            # largest exposure change per measurement
            "bias": 0.0,                # pedestal, ADU; used when there are no darks
            "binning": None,            # None: each slot keeps its schedule binning
        },
        # sun_cycle only — unlike asi.overexposure, which also drives the time
        # mode: divide a slot's frame into shorter sub-frames when it comes back
        # over the threshold, so a filter cannot saturate. Off by default.
        "overexposure": {
            "enabled": False,
            "threshold": 55000.0,       # mean ADU above which the slot divides
            "release": 0.85,            # hysteresis on the way back to one frame
            "max_splits": 4,
            "min_exposure": 0.05,       # shortest sub-frame, s
            "margin": 0.5,              # slack kept inside the slot budget, s
            "min_frame_gap": 1.0,       # sub-frames stay this far apart, s
            "bias": 0.0,                # pedestal, ADU; used when there are no darks
        },
        "camera": {
            "backend": "dcam",          # "dcam" (real SDK) or "sim"
            "readout_speed": 2,         # 1 slow / low noise, 2 fast
            "binning": 1,
            "frame_timeout_ms": 1000,   # one DCAM frame-ready wait, not the exposure
        },
        "cooling": {
            "enabled": False,
            "lead_minutes": 30.0,       # switched on this long before the window
            "target_temp": None,        # °C; None keeps the camera's own target
            "tolerance": 3.0,           # °C off target that still counts as cold
            "off_between_nights": True,
            "off_on_exit": True,
        },
        "filter_wheel": {
            "port": "/dev/ttyUSB0",     # "sim" selects the simulator
            "baudrate": 9600,
            "move_timeout": 8.0,
        },
        "location": {
            "name": "",                 # observing site: sun_cycle file names, SiteID
            "lat": 0.0,
            "lon": 0.0,
            "elevation": 0.0,           # height above sea level, m
        },
        # Wheel table for sun_cycle names and headers; empty = the Tory wheel
        # (cameras/common/filters.py). {"slot":1,"wavelength":"5577",
        # "description":"557.7nm x 2.0nm"}
        "filters": [],
    },
    # Alert mail. Only "to" has to be filled in: the sender's credentials live in
    # ~/.every_camera/mail_account.json. See mailer.py.
    "alerts": {
        "enabled": True,
        # Empty means nothing is sent, which is said once in the log.
        "to": [],
        # Ordinary errors are collected and sent together this often. Faults that end a
        # night do not wait for it.
        "digest_minutes": 30,
        # How long before the same fault is reported again: a wheel failing every forty
        # seconds all night must stay one letter, not a thousand.
        "cooldown_minutes": 60,
        "disk_free_min_mb": 5000,      # 0 disables the check
        "status_mail_minutes": 0,      # >0 mails a status summary that often
        # Per station per day. One mailbox serves every camera, so one station stuck in
        # a repeating fault would otherwise spend everyone's allowance before dawn.
        "max_per_day": 50,
        "log_tail_lines": 50,
        # "smtp": {...} may be added here for a station that needs its own mailbox; it
        # is used only when there is no mail_account.json. Absent by default: a password
        # per station is a password in six more places.
    },
    "server": {
        "enabled": True,
        "bind": "0.0.0.0",
        # Preferred port. When taken, the normal case with several cameras on this same
        # config.json, the server moves up to port + port_search. Nothing is written
        # back.
        "port": 8765,
        "port_search": 20,      # 0 = use "port" or nothing
        "discovery": True,
        "discovery_port": 45455,
        "max_list": 5000,
        "focus_ttl": 60,
        # Cloudflare quick tunnel: a new trycloudflare.com address every run, mailed by
        # the sentinel. Read-only from outside unless the request carries the token.
        "tunnel": {
            "enabled": True,
            "to": [],                   # empty = alerts.to
            "mail_minutes": 180,        # resend period; 0 = only when it changes
            "cloudflared": "",          # empty = PATH, ~/.every_camera/bin, download
            "auto_download": True,
            "protocol": "http2",        # QUIC is often blocked; "auto" tries it first
        },
    },
    # Friendly name of this machine, shown by viewer_app/focus_app/monitor
    # instead of a bare IP. Empty means "use the hostname".
    "node_name": "",
    "status_dir": "",
}


def _default_wheel_port():
    """Imported here: observer tools import ``utils`` to find their config and carry no
    ``cameras`` requirements. The module pulls in pyserial only when a port is opened.
    """
    try:
        from cameras.common.filterwheel import default_port
    except ImportError:
        return "COM3" if os.name == "nt" else "/dev/ttyUSB0"
    return default_port()


# The literals above read the way a Linux station does. On Windows they are a port
# that cannot exist, so both are replaced; the wizard and the setup GUI ask the same
# function.
for _camera_with_a_wheel in ("asi", "japan"):
    DEFAULT_CONFIG[_camera_with_a_wheel]["filter_wheel"]["port"] = \
        _default_wheel_port()
del _camera_with_a_wheel


def _drop_retired_keys(cfg):
    """An installed config.json is never overwritten, so the MQTT block would sit there
    looking like something that could be switched on. Dropped on load, written back on
    the next save.
    """
    cfg.pop("mqtt", None)
    # Never implemented; status_mail_minutes is the periodic report.
    alerts = cfg.get("alerts")
    if isinstance(alerts, dict):
        alerts.pop("daily_summary", None)
    return cfg


def _restore_tracked_config(path):
    """Bring a missing config.json back from git. Exclusive open: never overwrites."""
    import subprocess
    try:
        data = subprocess.run(["git", "-C", APP_DIR, "show", "HEAD:config.json"],
                              capture_output=True, timeout=10, check=True).stdout
        with open(path, "xb") as fh:
            fh.write(data)
    except Exception:
        pass


class LoadedConfig(dict):
    """A config as loaded. ``load_error`` says why the file could not be used: the
    result is then defaults, and must not be saved over the file."""
    load_error = None
    source_path = None


def _read_config_text(path):
    # Files written here are ASCII; one edited by hand on a Windows station may be
    # in the ANSI code page.
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        import locale
        return raw.decode(locale.getpreferredencoding(False))


def _backup_path(path):
    backup = path + ".bak"
    if os.path.exists(backup):
        backup = f"{path}.bak-{dt.now().strftime('%Y%m%d-%H%M%S')}"
    return backup


def load_config(path=None):
    """Returns the file merged over the defaults.

    A malformed file is reported and kept as a backup: every setting reverts to
    defaults, and ``load_error`` tells the GUI not to write those back over it.
    """
    path = path or LOCAL_CONFIG_FILE
    cfg = _deep_copy(DEFAULT_CONFIG)
    error = None
    if path == LOCAL_CONFIG_FILE and not os.path.exists(path):
        _restore_tracked_config(path)
    try:
        data = json.loads(_read_config_text(path))
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value is not an object")
        _deep_merge(cfg, data)
    except FileNotFoundError:
        save_config(cfg, path)
    except (json.JSONDecodeError, ValueError) as exc:
        error = f"not valid JSON: {exc}"
        backup = _backup_path(path)
        try:
            import shutil
            shutil.copy2(path, backup)
            hint = f"Original kept as {backup}."
        except Exception:
            hint = "Could not back it up."
        console_ui.error(f"Config {path} is not valid JSON: {exc}\n"
                         f"        Falling back to defaults. {hint}")
    except Exception as exc:
        error = f"could not be read: {exc}"
        console_ui.error(f"Could not read config {path}: {exc}. Using defaults.")
    result = LoadedConfig(_drop_retired_keys(cfg))
    result.load_error = error
    result.source_path = path
    return result


def save_config(cfg, path=None):
    """Atomic: a unique temp file, flushed to disk, then renamed over the config.
    Returns False, having said why, if nothing was written."""
    import tempfile
    path = path or LOCAL_CONFIG_FILE
    out = _deep_copy(cfg)
    tmp = None
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                                   suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp makes the file 0600; the config keeps the mode it had.
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            mode = 0o644
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except Exception as exc:
        console_ui.warn(f"Could not save config: {exc}")
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def _deep_copy(d):
    return json.loads(json.dumps(d))


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


@dataclass
class ScheduleEntry:
    start: dt
    end: dt


def parse_schedule_text(text):
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
    with open(filepath, "r", encoding="utf-8") as f:
        return parse_schedule_text(f.read())


def unique_path(path):
    """``path``, or ``name_2.ext`` and on: names come from local time, which repeats an
    hour when daylight saving ends, and an archived frame must never be overwritten."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}_{n}{ext}"):
        n += 1
    return f"{stem}_{n}{ext}"


def schedule_now():
    """Interval schedule files hold naive UTC times."""
    return dt.now(timezone.utc).replace(tzinfo=None)


def schedule_to_local(moment):
    """A schedule time as naive local time, for status files and the dashboard."""
    if moment is None:
        return None
    return moment.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def load_schedule_safe(filepath):
    """Never raises. Returns ``(entries, errors, reason)``.

    ``reason`` says why the camera cannot measure; the caller starts in setup mode
    rather than abort. ``errors`` holds per-line complaints, worth reporting even when
    enough valid lines remain.
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


def write_status_file(path, data):
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def pid_alive(pid):
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
    """Only a clean shutdown removes a worker's own file, so a crashed one would stay in
    the monitor's list forever.
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


def _windows_mem_used_pct():
    """The Windows counterpart of ``/proc/meminfo``."""
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return float(status.dwMemoryLoad)


def get_system_info(output_dir=None):
    """Every metric is optional: a key this platform cannot produce is left out, not
    faked.
    """
    import platform
    info = {
        "hostname": platform.node(),
        "ip": get_local_ip(),
        "platform": platform.system(),
    }
    # POSIX only: Windows keeps no load average, and sampling CPU time over an interval
    # would make this call block.
    try:
        with open("/proc/loadavg") as f:
            info["load_avg_1m"] = float(f.read().split()[0])
    except Exception:
        pass
    try:
        if os.name == "nt":
            info["mem_used_pct"] = _windows_mem_used_pct()
        else:
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
    # ``shutil.disk_usage``, not ``os.statvfs``: that does not exist on Windows.
    if output_dir and os.path.isdir(output_dir):
        try:
            info["disk_free_mb"] = round(
                shutil.disk_usage(output_dir).free / (1024 * 1024))
        except Exception:
            pass
    return info


def _ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def _ask_bool(prompt, default=False):
    suffix = " [Y/n]" if default else " [y/N]"
    val = input(f"{prompt}{suffix}: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "1", "true", "да")


def _ask_int(prompt, default=0):
    val = _ask(prompt, str(default))
    try:
        return int(val)
    except ValueError:
        return default


def _ask_float(prompt, default=0.0):
    val = _ask(prompt, str(default))
    try:
        return float(val)
    except ValueError:
        return default


def _ask_clearable(prompt, default=""):
    """Enter keeps the current value; "-" clears it."""
    val = _ask(f"{prompt}, '-' = none", default or "").strip()
    return "" if val == "-" else val


def _ask_optional_int(prompt, default=None):
    """Enter keeps the current value; "-" means "let the program work it out"."""
    val = _ask_clearable(prompt, "" if default is None else str(default))
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return default


def configure_console_cannon(cfg, config_path=None):
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

    print("\n  Exposure is written exactly as the camera names it — '1/125',")
    print("  '10.3', '30'. Leave it empty to keep whatever the camera settings")
    print("  .ini holds; a value the camera does not offer is reported at")
    print("  startup together with the list it does offer.")
    cannon["shutterspeed"] = _ask("Shutter speed (empty = from camera cfg file)",
                                   cannon.get("shutterspeed", ""))
    cannon["camcfg_file"] = _ask("Camera cfg file (empty = camcfg_<Model>.ini "
                                 "next to the app)",
                                 cannon.get("camcfg_file", ""))

    cfg["cannon"] = cannon
    _configure_server(cfg)

    save_config(cfg, config_path)
    print("\nConfiguration saved.\n")


def _ask_preflight(name, section):
    """Keeps the keys it does not ask about: a station that tuned them by hand must not
    lose them to a pass through the wizard.
    """
    preflight = _deep_copy(DEFAULT_CONFIG[name]["preflight"])
    preflight.update(section.get("preflight") or {})
    preflight["enabled"] = _ask_bool(
        "Start earlier, in the bright twilight, with automatic exposure?",
        preflight.get("enabled", False))
    if preflight["enabled"]:
        preflight["sun_start_angle"] = _ask_float(
            f"  Start the automatic stage below (degrees, above "
            f"{section.get('sun_max_angle', -10.0):g})",
            preflight.get("sun_start_angle", -6.0))
        preflight["target_mean"] = _ask_float(
            "  Mean frame intensity to hold (ADU, 0-65535)",
            preflight.get("target_mean", 20000.0))
        preflight["min_exposure"] = _ask_float(
            "  Shortest exposure the loop may pick (s)",
            preflight.get("min_exposure", 0.05))
    section["preflight"] = preflight


def _ask_overexposure(name, section):
    guard = _deep_copy(DEFAULT_CONFIG[name]["overexposure"])
    guard.update(section.get("overexposure") or {})
    guard["enabled"] = _ask_bool(
        "Split a frame into shorter ones when it over-exposes?",
        guard.get("enabled", False))
    if guard["enabled"]:
        guard["threshold"] = _ask_float(
            "  Mean intensity above which the frame splits (ADU, 0-65535)",
            guard.get("threshold", 55000.0))
        guard["max_splits"] = _ask_int(
            "  Most sub-frames one frame may be divided into",
            guard.get("max_splits", 4))
    section["overexposure"] = guard


def configure_console_sptt(cfg, config_path=None):
    sptt = cfg.get("sptt", {})
    print("\n--- SPTT (CSDU-429/423/285C) Camera Configuration ---\n")

    sptt["output_dir"] = _ask("Output directory for FITS files", sptt.get("output_dir", ""))
    sptt["instance_name"] = _ask("Instance name (auto if empty)",
                                  sptt.get("instance_name", ""))
    print("\n  With several SPTT cameras on this machine, each run needs its own")
    print("  config and the USB port of its camera (python main.py --type sptt")
    print("  --probe lists them). Leave empty for the only camera.")
    sptt["usb_port"] = _ask("USB port (empty = the only camera)",
                            sptt.get("usb_port", ""))
    sptt["exposure"] = _ask_float("Exposure (seconds)", sptt.get("exposure", 0.88))

    print("\n  The frame period is a separate register, and one shorter than")
    print("  the exposure truncates the integration instead of slowing the")
    print("  camera down. Answer '-' and it follows the exposure.")
    sptt["period_us"] = _ask_optional_int(
        "Frame period, us (none = auto)", sptt.get("period_us"))
    sptt["trigmode"] = _ask_optional_int(
        "Trigger mode (none = 0, continuous)", sptt.get("trigmode"))

    sptt["gain"] = _ask_int("Gain (0-1023)", sptt.get("gain", 100))
    sptt["binning"] = _ask_int("Binning (0=1x1, 1=2x2, 3=4x4)", sptt.get("binning", 0))
    enc = _ask_int("Encoding (0=8bit, 1=12bit)", sptt.get("encoding", 1))
    sptt["encoding"] = enc if enc in (0, 1) else 1

    # The model comes from the USB ID; these only matter on a CSDU-285C.
    print("\n  Colour camera (CSDU-285C) only — a grey camera ignores these.")
    sptt["debayer"] = _ask_bool("Save RGB instead of raw Bayer",
                                bool(sptt.get("debayer", False)))
    pattern = _ask("Bayer pattern (RGGB/GRBG/GBRG/BGGR)",
                   sptt.get("bayer_pattern") or "RGGB").upper()
    sptt["bayer_pattern"] = (pattern if pattern in ("RGGB", "GRBG", "GBRG", "BGGR")
                             else "RGGB")

    print("\n  Sun mode: scheduled frames wait while the sun is above the threshold;")
    print("  the program keeps running and starts again the next night.")
    if _ask_bool("Capture only while the sun is low?", sptt.get("mode") == "sun"):
        sptt["mode"] = "sun"
        sptt["sun_max_angle"] = _ask_float("Sun altitude at or below (degrees)",
                                           sptt.get("sun_max_angle", -10.0))
        location = sptt.get("location") or {}
        location["lat"] = _ask_float("Site latitude (degrees)", location.get("lat", 0.0))
        location["lon"] = _ask_float("Site longitude (degrees)", location.get("lon", 0.0))
        location["elevation"] = _ask_float("Site elevation (m)",
                                           location.get("elevation", 0.0))
        sptt["location"] = location
        _ask_preflight("sptt", sptt)
    else:
        sptt["mode"] = "always"
    _ask_overexposure("sptt", sptt)

    cfg["sptt"] = sptt
    _configure_server(cfg)

    save_config(cfg, config_path)
    print("\nConfiguration saved.\n")


def configure_console_infra(cfg, config_path=None):
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
    _ask_overexposure("infra", infra)

    cfg["infra"] = infra
    _configure_server(cfg)

    save_config(cfg, config_path)
    print("\nConfiguration saved.\n")


def configure_console_asi(cfg, config_path=None):
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
                         wheel.get("port") or _default_wheel_port())
    if wheel["port"].strip().lower() != "sim":
        wheel["baudrate"] = _ask_int("Filter wheel baudrate", wheel.get("baudrate", 9600))

    location["lat"] = _ask_float("Site latitude (degrees)", location.get("lat", 0.0))
    location["lon"] = _ask_float("Site longitude (degrees)", location.get("lon", 0.0))
    location["elevation"] = _ask_float("Site elevation (m)",
                                       location.get("elevation", 0.0))

    # Both end up in the file name and in the legacy FITS headers, so the
    # processing program keys off them.
    asi["site_id"] = _ask("Site ID (4 characters, e.g. TORY)", asi.get("site_id", ""))
    asi["device_id"] = _ask("Device ID (e.g. ASI0)", asi.get("device_id", ""))

    mode = _ask("Schedule mode (sun / time / sun_cycle)", asi.get("mode", "sun"))
    asi["mode"] = mode if mode in ("sun", "time", "sun_cycle") else "sun"
    if asi["mode"] in ("sun", "sun_cycle"):
        asi["sun_max_angle"] = _ask_float(
            "Start when the solar altitude drops below (degrees)",
            asi.get("sun_max_angle", -10.0))
    if asi["mode"] == "time":
        asi["t_start"] = _ask("Cycle phase reference T_start (HH:MM)",
                              asi.get("t_start", "20:00"))
    elif asi["mode"] == "sun_cycle":
        # UTC, so stations in different places and time zones share one phase.
        asi["t_start"] = _ask_clearable("Cycle phase reference T_start, UTC (HH:MM; "
                                        "none = first whole minute of the window)",
                                        asi.get("t_start", "") or "")
    if asi["mode"] in ("time", "sun_cycle"):
        asi["schedule_len"] = _ask_float(
            "Cycle length (s, 0 = derive from the slots)",
            asi.get("schedule_len") or 0.0) or None
    asi["dark_frames"] = _ask_int("Dark frames per exposure",
                                  asi.get("dark_frames", 3))
    asi["dead_time"] = _ask_float("Dead time between cycles (s)",
                                  asi.get("dead_time", 5.0))

    # Both intensity loops keep the keys they are not asked about, so a station that
    # tuned them by hand does not lose them to a pass through the wizard.
    if asi["mode"] == "sun_cycle":
        preflight = _deep_copy(DEFAULT_CONFIG["asi"]["preflight"])
        preflight.update(asi.get("preflight") or {})
        preflight["enabled"] = _ask_bool(
            "Shoot the bright twilight first, with automatic exposure?",
            preflight.get("enabled", False))
        if preflight["enabled"]:
            preflight["sun_start_angle"] = _ask_float(
                f"  Start the automatic stage below (degrees, above "
                f"{asi['sun_max_angle']:g})",
                preflight.get("sun_start_angle", -6.0))
            preflight["target_mean"] = _ask_float(
                "  Mean frame intensity to hold (ADU, 0-65535)",
                preflight.get("target_mean", 20000.0))
            preflight["min_exposure"] = _ask_float(
                "  Shortest exposure the loop may pick (s)",
                preflight.get("min_exposure", 0.05))
        asi["preflight"] = preflight

    if asi["mode"] in ("time", "sun_cycle"):
        guard = _deep_copy(DEFAULT_CONFIG["asi"]["overexposure"])
        guard.update(asi.get("overexposure") or {})
        guard["enabled"] = _ask_bool(
            "Split a slot's frame in two when it over-exposes?",
            guard.get("enabled", False))
        if guard["enabled"]:
            guard["threshold"] = _ask_float(
                "  Mean intensity above which the slot splits (ADU, 0-65535)",
                guard.get("threshold", 55000.0))
            guard["max_splits"] = _ask_int(
                "  Most sub-frames one slot may be divided into",
                guard.get("max_splits", 4))
        asi["overexposure"] = guard

    schedule_file = _ask("Schedule file path, .txt or .json "
                         "(empty = use the slots below)",
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
            if asi["mode"] in ("time", "sun_cycle"):
                slot["delta"] = _ask_float("    Offset from the cycle start (s)", 0.0)
                slot["binning"] = _ask_int("    Binning", camera.get("binning", 4))
                slot["gain"] = _ask_int("    Analog gain (1=Low, 2=Medium, 3=High)",
                                        camera.get("gain", 1))
                slot["readout"] = _ask_float(
                    "    Readout/filter-move time after the frame (s)",
                    asi.get("dead_time", 5.0))
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
    _configure_server(cfg)

    save_config(cfg, config_path)
    print("\nConfiguration saved.\n")


def configure_console_japan(cfg, config_path=None):
    japan = cfg.get("japan", _deep_copy(DEFAULT_CONFIG["japan"]))
    camera = japan.get("camera", _deep_copy(DEFAULT_CONFIG["japan"]["camera"]))
    wheel = japan.get("filter_wheel",
                      _deep_copy(DEFAULT_CONFIG["japan"]["filter_wheel"]))
    location = japan.get("location", _deep_copy(DEFAULT_CONFIG["japan"]["location"]))
    print("\n--- Japan (Hamamatsu DCAM) Camera Configuration ---\n")

    print("In sun and time mode frames are written flat into this directory, so a "
          "directory per night is the usual arrangement; sun_cycle files them "
          "into a YYYY/MM/DD tree under it.")
    japan["output_dir"] = _ask("Output directory for FITS files",
                               japan.get("output_dir", ""))
    japan["instance_name"] = _ask("Instance name (auto if empty)",
                                  japan.get("instance_name", ""))

    backend = _ask("Camera backend (dcam / sim)", camera.get("backend", "dcam"))
    camera["backend"] = backend if backend in ("dcam", "sim") else "dcam"
    camera["binning"] = _ask_int("Binning (1-8)", camera.get("binning", 1))
    camera["readout_speed"] = _ask_int("Readout speed (1=slow/min noise, 2=fast)",
                                       camera.get("readout_speed", 2))
    # There is no cooling section: this camera reports a sensor temperature and
    # offers no setpoint to command.

    wheel["port"] = _ask("Filter wheel serial port ('sim' for the simulator)",
                         wheel.get("port") or _default_wheel_port())
    if wheel["port"].strip().lower() != "sim":
        wheel["baudrate"] = _ask_int("Filter wheel baudrate",
                                     wheel.get("baudrate", 9600))

    location["name"] = _ask("Site name (e.g. TORY)", location.get("name", ""))
    location["lat"] = _ask_float("Site latitude (degrees)", location.get("lat", 0.0))
    location["lon"] = _ask_float("Site longitude (degrees)", location.get("lon", 0.0))
    location["elevation"] = _ask_float("Site elevation above sea level (m)",
                                       location.get("elevation", 0.0))
    # Written into the NAME card, and with the site name into sun_cycle names.
    japan["name"] = _ask("Instrument name (e.g. HAMA1)", japan.get("name", ""))

    mode = _ask("Schedule mode (sun / time / sun_cycle)", japan.get("mode", "sun"))
    japan["mode"] = mode if mode in ("sun", "time", "sun_cycle") else "sun"
    if japan["mode"] in ("sun", "sun_cycle"):
        japan["sun_max_angle"] = _ask_float(
            "Start when the solar altitude drops below (degrees)",
            japan.get("sun_max_angle", -10.0))
    if japan["mode"] == "sun_cycle":
        # UTC, so stations in different places and time zones share one phase.
        japan["t_start"] = _ask_clearable("Cycle phase reference T_start, UTC (HH:MM; "
                                          "none = first whole minute of the window)",
                                          japan.get("t_start", "") or "")
        period = _ask_float("Cycle period in seconds "
                            "(0 = derive it from the last slot)",
                            float(japan.get("schedule_len") or 0.0))
        japan["schedule_len"] = period if period > 0 else None
    elif japan["mode"] == "time":
        japan["t_start"] = _ask("Cycle phase reference T_start (HH:MM)",
                                japan.get("t_start", "20:00"))
        # A schedule file may say this itself with a "period = 1440" header, the better
        # place: it cannot then outlive its slots.
        period = _ask_float("Cycle period in seconds "
                            "(0 = derive it from the last slot)",
                            float(japan.get("schedule_len") or 0.0))
        japan["schedule_len"] = period if period > 0 else None
        japan["wait_for_enter"] = _ask_bool(
            "Wait for Enter before starting? (no = start unattended)",
            japan.get("wait_for_enter", True))
    japan["dark_frames"] = _ask_int("Dark frames per exposure",
                                    japan.get("dark_frames", 3))
    japan["dead_time"] = _ask_float(
        "Dead time reserved between cycles for filter/binning changes (s)",
        japan.get("dead_time", 5.0))

    # Both intensity loops belong to sun_cycle on this camera, and keep the keys they
    # are not asked about, so hand-tuned values survive a pass through the wizard.
    if japan["mode"] == "sun_cycle":
        preflight = _deep_copy(DEFAULT_CONFIG["japan"]["preflight"])
        preflight.update(japan.get("preflight") or {})
        preflight["enabled"] = _ask_bool(
            "Shoot the bright twilight first, with automatic exposure?",
            preflight.get("enabled", False))
        if preflight["enabled"]:
            preflight["sun_start_angle"] = _ask_float(
                f"  Start the automatic stage below (degrees, above "
                f"{japan['sun_max_angle']:g})",
                preflight.get("sun_start_angle", -6.0))
            preflight["target_mean"] = _ask_float(
                "  Mean frame intensity to hold (ADU, 0-65535)",
                preflight.get("target_mean", 20000.0))
            preflight["min_exposure"] = _ask_float(
                "  Shortest exposure the loop may pick (s)",
                preflight.get("min_exposure", 0.05))
        japan["preflight"] = preflight

        guard = _deep_copy(DEFAULT_CONFIG["japan"]["overexposure"])
        guard.update(japan.get("overexposure") or {})
        guard["enabled"] = _ask_bool(
            "Split a slot's frame in two when it over-exposes?",
            guard.get("enabled", False))
        if guard["enabled"]:
            guard["threshold"] = _ask_float(
                "  Mean intensity above which the slot splits (ADU, 0-65535)",
                guard.get("threshold", 55000.0))
            guard["max_splits"] = _ask_int(
                "  Most sub-frames one slot may be divided into",
                guard.get("max_splits", 4))
        japan["overexposure"] = guard

    schedule_file = _ask("Schedule file path, .txt or .json "
                         "(empty = use the slots below)",
                         japan.get("schedule_file", ""))
    japan["schedule_file"] = schedule_file
    if not schedule_file and _ask_bool("Enter the schedule now?",
                                       not japan.get("schedule")):
        slots = []
        while True:
            print(f"\n  Slot {len(slots) + 1}:")
            slot = {
                "filter": _ask_int("    Filter (1-6)", 1),
                "exposure": _ask_float("    Exposure (s)", 30.0),
            }
            if japan["mode"] in ("time", "sun_cycle"):
                slot["delta"] = _ask_float("    Offset from the cycle start (s)", 0.0)
                slot["binning"] = _ask_int("    Binning", camera.get("binning", 1))
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
        japan["schedule"] = slots

    japan["camera"] = camera
    japan["filter_wheel"] = wheel
    japan["location"] = location
    cfg["japan"] = japan
    _configure_server(cfg)

    save_config(cfg, config_path)
    print("\nConfiguration saved.\n")


def _configure_server(cfg):
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
        tun = srv.get("tunnel") or _deep_copy(DEFAULT_CONFIG["server"]["tunnel"])
        print("\n  Access from outside: a Cloudflare quick tunnel, its address mailed")
        print("  by sentinel.py. Read-only from outside without the mailed token.")
        tun["enabled"] = _ask_bool("Enable access from outside?",
                                   tun.get("enabled", True))
        if tun["enabled"]:
            answer = _ask("Who gets the address (comma-separated; blank = alerts.to)",
                          ", ".join(tun.get("to") or []))
            tun["to"] = [a.strip() for a in answer.split(",") if a.strip()]
            tun["mail_minutes"] = _ask_int("Resend the address every N minutes "
                                           "(0 = only when it changes)",
                                           tun.get("mail_minutes", 180))
        srv["tunnel"] = tun
    else:
        srv["enabled"] = False
    cfg["server"] = srv
    _configure_alerts(cfg)


def _configure_alerts(cfg):
    """Chained off the server wizard, so every camera's setup ends by asking who to
    tell.

    The sender's mailbox is not asked for: it arrives with the installation.
    """
    alerts_cfg = cfg.get("alerts", _deep_copy(DEFAULT_CONFIG["alerts"]))
    print("\n  Alert mail: a letter when a camera stops, crashes, hangs, loses")
    print("  its filter controller, or runs out of disk.")

    try:
        import mailer
        print(f"  Sender: {mailer.account_summary(mailer.resolve_account(alerts_cfg))}")
    except Exception:
        pass

    current = ", ".join(alerts_cfg.get("to") or [])
    answer = _ask("Who should be told (addresses, comma-separated; blank = nobody)",
                  current)
    recipients = [part.strip() for part in answer.replace(";", ",").split(",")
                  if part.strip()]
    alerts_cfg["to"] = recipients
    alerts_cfg["enabled"] = bool(recipients)
    if not recipients:
        print("  No addresses — nothing will be sent. Everything still goes to "
              "the log.")
    cfg["alerts"] = alerts_cfg
