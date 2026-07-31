"""
The ``japan`` section of config.json, as typed configuration objects.

The standalone japan-camera program read a ``config.ini`` through configparser and
a ``schedule.txt`` beside it. every-camera keeps one JSON file for every camera, so
this module does the same job against a plain dict: validate, fill defaults, and
hand the hardware classes the small typed objects they expect (``CameraCfg``,
``FilterWheelCfg``, …) instead of letting them dig around in raw config keys. A
station that already keeps a ``schedule.txt`` can point ``japan.schedule_file`` at
it and nothing else changes.

Validation never raises for a bad *slot* or a bad value: problems are collected
into ``JapanConfig.errors`` and reported on the console, because losing one
schedule line must not cost a night. This is also where the camera's own policy
lives — which schedule modes it runs, which readout speeds and binnings the sensor
takes — since ``cameras/common/schedule.py`` deliberately carries the union of both
imagers' vocabularies and does not know who is asking.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from ..common import cfgparse
from ..common import schedule as schedule_mod

DEFAULT_MODE = "sun"

# The two schedule shapes the Hamamatsu program had, and the only two this driver
# runs. ``sun_cycle`` exists in the shared schedule module but belongs to the ASI
# imager: it is imagerd_rt's night, anchored to the sun with automatic exposures,
# and nothing in this programme corresponds to it.
JAPAN_MODES = ("sun", "time")

# DCAM READOUTSPEED on this camera: 1 is the slow, low-noise readout and 2 the
# fast one. The property is an enumeration, not a range, so a third value is a
# configuration mistake rather than something to clamp.
READOUT_SPEEDS = (1, 2)

BINNING_MIN = 1
BINNING_MAX = 8


def readout_text(speed) -> str:
    """Word a READOUTSPEED setting the way the camera's manual does."""
    return {1: "1 (slow, low noise)", 2: "2 (fast)"}.get(int(speed), str(speed))


@dataclass
class CameraCfg:
    backend: str = "dcam"             # "dcam" (real SDK) or "sim"
    readout_speed: int = 2            # 1 slow / low noise, 2 fast
    binning: int = 1
    frame_timeout_ms: int = 1000      # one DCAM frame-ready wait, not the exposure


@dataclass
class FilterWheelCfg:
    port: str = "/dev/ttyUSB0"        # "sim" selects the simulator
    baudrate: int = 9600
    move_timeout: float = 8.0


@dataclass
class LocationCfg:
    lat: float = 0.0
    lon: float = 0.0
    elevation: float = 0.0


@dataclass
class ScheduleCfg:
    mode: str = DEFAULT_MODE
    sun_max_angle: float = -10.0
    t_start: time = None              # time mode: phase reference of the cycle
    entries: list = field(default_factory=list)
    dark_frames: int = 3
    dead_time: float = 5.0
    schedule_len: float = None        # time mode: explicit period, seconds

    @property
    def period(self):
        """Length of one general cycle (``time`` mode only).

        Derived from the slots — last delta plus that exposure plus the dead
        time — which is what the Hamamatsu program did and what its schedules
        are still written for. A schedule that states its own period wins:
        ``period = 1440`` at the top of the file, or ``japan.schedule_len``.
        Stating it in the file is the point — a period kept only in config.json
        goes stale the moment the schedule beside it is swapped.
        """
        if self.schedule_len:
            return float(self.schedule_len)
        return schedule_mod.cycle_period(self.entries, self.dead_time)


@dataclass
class JapanConfig:
    output_dir: str = ""
    camera: CameraCfg = field(default_factory=CameraCfg)
    filter_wheel: FilterWheelCfg = field(default_factory=FilterWheelCfg)
    location: LocationCfg = field(default_factory=LocationCfg)
    schedule: ScheduleCfg = field(default_factory=ScheduleCfg)
    wait_for_enter: bool = True
    errors: list = field(default_factory=list)

    @property
    def simulated(self):
        return self.camera.backend == "sim"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def from_dict(japan_cfg):
    """Build a :class:`JapanConfig` from the ``japan`` sub-dict of config.json.

    The schedule comes from ``japan.schedule`` (a list of slot objects). If
    ``japan.schedule_file`` points at a readable file, that file wins instead —
    a japan-camera ``schedule.txt`` is exactly the legacy text format the shared
    parser reads, comma-separated in ``sun`` mode and semicolon-separated in
    ``time`` mode.

    Such a file may open with ``key = value`` lines, and those speak last of all
    for the settings this camera understands — ``mode``, ``sun_max_angle``,
    ``t_start``, ``dark_frames``, ``dead_time`` and the cycle period
    (``period = 1440``, stored as ``schedule_len``). The period especially
    belongs with the slots it describes: kept only in config.json, it survives
    the schedule it was measured for.
    """
    japan_cfg = japan_cfg or {}
    errors = []

    cam_cfg = cfgparse.sub(japan_cfg, "camera")
    camera = CameraCfg(
        backend=str(cam_cfg.get("backend", "dcam")).strip().lower(),
        readout_speed=cfgparse.as_int(cam_cfg, "readout_speed", 2),
        binning=cfgparse.as_int(cam_cfg, "binning", 1),
        frame_timeout_ms=cfgparse.as_int(cam_cfg, "frame_timeout_ms", 1000),
    )
    if camera.backend not in ("dcam", "sim"):
        errors.append(f"japan.camera.backend must be 'dcam' or 'sim', got "
                      f"{camera.backend!r}; using 'dcam'")
        camera.backend = "dcam"
    if camera.readout_speed not in READOUT_SPEEDS:
        errors.append(f"japan.camera.readout_speed must be 1 (slow) or 2 (fast), "
                      f"got {camera.readout_speed}; using 2")
        camera.readout_speed = 2
    if not BINNING_MIN <= camera.binning <= BINNING_MAX:
        errors.append(f"japan.camera.binning must be {BINNING_MIN}..{BINNING_MAX}, "
                      f"got {camera.binning}; using 1")
        camera.binning = 1
    if camera.frame_timeout_ms <= 0:
        errors.append(f"japan.camera.frame_timeout_ms must be positive, got "
                      f"{camera.frame_timeout_ms}; using 1000")
        camera.frame_timeout_ms = 1000

    wheel_cfg = cfgparse.sub(japan_cfg, "filter_wheel")
    wheel = FilterWheelCfg(
        port=str(wheel_cfg.get("port", "/dev/ttyUSB0")),
        baudrate=cfgparse.as_int(wheel_cfg, "baudrate", 9600),
        move_timeout=cfgparse.as_float(wheel_cfg, "move_timeout", 8.0),
    )

    loc_cfg = cfgparse.sub(japan_cfg, "location")
    location = LocationCfg(
        lat=cfgparse.as_float(loc_cfg, "lat", 0.0),
        lon=cfgparse.as_float(loc_cfg, "lon", 0.0),
        elevation=cfgparse.as_float(loc_cfg, "elevation", 0.0),
    )

    mode = str(japan_cfg.get("mode", DEFAULT_MODE)).strip().lower()
    if mode not in JAPAN_MODES:
        extra = ""
        if mode in schedule_mod.MODES:
            # Almost certainly a config copied from the ASI camera. Saying so is
            # the difference between a five-second fix and an evening of puzzling.
            extra = (f" ({mode!r} belongs to the asi camera, which has modes "
                     f"{schedule_mod.MODES})")
        errors.append(f"japan.mode must be one of {JAPAN_MODES}, got {mode!r}"
                      f"{extra}; using {DEFAULT_MODE!r}")
        mode = DEFAULT_MODE

    overrides = {}
    schedule_file = str(japan_cfg.get("schedule_file", "") or "").strip()
    if schedule_file:
        try:
            entries, slot_errors, overrides = schedule_mod.load_schedule_file(
                schedule_file, mode, camera.binning)
        except OSError as exc:
            entries, slot_errors = [], [f"schedule_file {schedule_file}: {exc}"]
    else:
        entries, slot_errors = schedule_mod.entries_from_config(
            japan_cfg.get("schedule", []), mode, camera.binning)
    errors.extend(slot_errors)

    # A slot may legitimately carry a gain for the ASI imager. This camera has no
    # gain to set, so honouring the rest of such a slot silently would leave the
    # operator believing a setting took effect.
    if any(entry.gain is not None for entry in entries):
        errors.append("japan schedule slots carry 'gain', which this camera has "
                      "no equivalent for; it is ignored")

    # The schedule file speaks last, but only for the keys it is allowed to and
    # only for the ones this camera understands: ``site_id`` and ``device_id``
    # belong to the ASI archive and are ignored here.
    settings = dict(japan_cfg)
    settings.update({key: value for key, value in overrides.items()
                     if key in ("mode", "sun_max_angle", "t_start",
                                "dark_frames", "dead_time", "schedule_len")})

    if overrides.get("mode"):
        file_mode = str(overrides["mode"]).strip().lower()
        if file_mode in JAPAN_MODES:
            mode = file_mode
        else:
            errors.append(f"schedule_file mode must be one of {JAPAN_MODES}, got "
                          f"{file_mode!r}; keeping {mode!r}")

    t_start = cfgparse.parse_time(settings.get("t_start"), None)
    if mode == "time" and t_start is None:
        errors.append("japan.t_start is required in 'time' mode (HH:MM); "
                      "using 20:00")
        t_start = time(20, 0)

    schedule_len = None
    raw_len = settings.get("schedule_len")
    if raw_len not in (None, "", 0):
        try:
            schedule_len = float(raw_len)
        except (TypeError, ValueError):
            errors.append(f"the cycle period must be a number of seconds, got "
                          f"{raw_len!r}; deriving it from the slots instead")
        else:
            if schedule_len <= 0:
                errors.append(f"the cycle period must be positive, got "
                              f"{schedule_len}; deriving it from the slots instead")
                schedule_len = None

    dead_time = cfgparse.as_float(settings, "dead_time", 5.0)
    disagreement = schedule_mod.period_mismatch(
        mode, entries, schedule_len, dead_time, what="japan")
    if disagreement:
        errors.append(disagreement)

    sched = ScheduleCfg(
        mode=mode,
        sun_max_angle=cfgparse.as_float(settings, "sun_max_angle", -10.0),
        t_start=t_start,
        entries=entries,
        dark_frames=cfgparse.as_int(settings, "dark_frames", 3),
        dead_time=dead_time,
        schedule_len=schedule_len,
    )
    if sched.dark_frames < 0:
        errors.append(f"japan.dark_frames must not be negative, got "
                      f"{sched.dark_frames}; using 0")
        sched.dark_frames = 0

    return JapanConfig(
        output_dir=str(japan_cfg.get("output_dir", "") or ""),
        camera=camera,
        filter_wheel=wheel,
        location=location,
        schedule=sched,
        wait_for_enter=cfgparse.as_bool(japan_cfg, "wait_for_enter", True),
        errors=errors,
    )
