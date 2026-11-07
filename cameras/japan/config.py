"""The ``japan`` section of config.json, as typed configuration objects.

Problems do not raise: they are collected into ``JapanConfig.errors`` and reported on
the console, because one bad schedule line must not cost a night. This is also where
the camera's own policy lives (schedule modes, readout speeds, binnings), since
``cameras/common/schedule.py`` carries both imagers' vocabularies.

``sun_cycle`` is the ASI imager's night run on this camera, filed in that archive
layout, hence ``name``, ``location.name``, ``filters``, ``preflight`` and
``overexposure``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from ..common import cfgparse
from ..common import exposure_config
from ..common import filters as common_filters
# Only for ``default_port``; pyserial is imported when a port opens, never at import.
from ..common import filterwheel as common_filterwheel
from ..common import schedule as schedule_mod

DEFAULT_MODE = "sun"

# ``sun_cycle`` is the general cycle of ``time``, started by the sun and phase-locked
# to ``t_start`` in UTC, so this camera and the ASI imager, or stations far apart, run
# in step.
JAPAN_MODES = ("sun", "time", "sun_cycle")

# On the ASI imager the split guard runs in every mode with slots. Here ``time`` keeps
# shooting exactly what its schedule says, one frame per slot.
SPLIT_MODES = ("sun_cycle",)

# Re-exported so ``japan_config.PreflightCfg`` reads as ``asi_config``'s does.
PreflightCfg = exposure_config.PreflightCfg
OverexposureCfg = exposure_config.OverexposureCfg
SATURATION_ADU = exposure_config.SATURATION_ADU

# Characters a name may not carry into the ``sun_cycle`` file name: ``_``
# separates its fields, and the rest would make a path or a broken name.
_NAME_FORBIDDEN = frozenset('_/\\ \t:*?"<>|')

# DCAM READOUTSPEED: 1 is the slow, low-noise readout, 2 the fast one. An enumeration,
# not a range, so a third value is a mistake rather than something to clamp.
READOUT_SPEEDS = (1, 2)

BINNING_MIN = 1
BINNING_MAX = 8


def readout_text(speed) -> str:
    return {1: "1 (slow, low noise)", 2: "2 (fast)"}.get(int(speed), str(speed))


@dataclass
class CameraCfg:
    backend: str = "dcam"             # "dcam" (real SDK) or "sim"
    readout_speed: int = 2            # 1 slow / low noise, 2 fast
    binning: int = 1
    frame_timeout_ms: int = 1000      # one DCAM frame-ready wait, not the exposure


@dataclass
class CoolingCfg:
    # Off by default: the camera then cools itself and nothing here touches the
    # cooler.
    enabled: bool = False
    lead_minutes: float = 30.0        # cooler on this long before the pre-darks
    target_temp: float = None         # None keeps the camera's own target
    tolerance: float = 3.0            # how far off target still counts as cooled
    off_between_nights: bool = True   # sun modes: cooler off once a night is over
    off_on_exit: bool = True


@dataclass
class FilterWheelCfg:
    # ``from_dict`` fills this in; the default is this platform's usual name. "sim"
    # selects the simulator.
    port: str = field(default_factory=common_filterwheel.default_port)
    baudrate: int = 9600
    move_timeout: float = 8.0


@dataclass
class LocationCfg:
    name: str = ""                    # observing site; sun_cycle file names
    lat: float = 0.0
    lon: float = 0.0
    elevation: float = 0.0            # height above sea level, metres


@dataclass
class ScheduleCfg:
    mode: str = DEFAULT_MODE
    sun_max_angle: float = -10.0
    # Phase reference of the cycle: local time in ``time`` mode, UTC in
    # ``sun_cycle`` mode.
    t_start: time = None
    entries: list = field(default_factory=list)
    dark_frames: int = 3
    dead_time: float = 5.0
    schedule_len: float = None        # cycle modes: explicit period, seconds

    @property
    def period(self):
        if self.schedule_len:
            return float(self.schedule_len)
        return schedule_mod.cycle_period(self.entries, self.dead_time)


@dataclass
class JapanConfig:
    output_dir: str = ""
    name: str = ""                    # instrument name: NAME card, sun_cycle names
    camera: CameraCfg = field(default_factory=CameraCfg)
    cooling: CoolingCfg = field(default_factory=CoolingCfg)
    filter_wheel: FilterWheelCfg = field(default_factory=FilterWheelCfg)
    location: LocationCfg = field(default_factory=LocationCfg)
    filters: list = field(default_factory=list)
    schedule: ScheduleCfg = field(default_factory=ScheduleCfg)
    preflight: PreflightCfg = field(default_factory=PreflightCfg)
    overexposure: OverexposureCfg = field(default_factory=OverexposureCfg)
    wait_for_enter: bool = True
    errors: list = field(default_factory=list)

    @property
    def simulated(self):
        return self.camera.backend == "sim"

    def filter_info(self, number):
        return common_filters.lookup(self.filters, number)


def clean_name(value, what, errors):
    """``value`` made safe for the ``sun_cycle`` file name, with a note if changed.

    The name is split on underscores, so one inside a field would shift every field
    after it.
    """
    text = str(value or "").strip()
    cleaned = "".join("-" if ch in _NAME_FORBIDDEN else ch for ch in text)
    if cleaned != text:
        errors.append(f"{what} {text!r} carries characters a file name field "
                      f"cannot hold; using {cleaned!r}")
    return cleaned


def from_dict(japan_cfg):
    """Build a :class:`JapanConfig` from the ``japan`` sub-dict of config.json.

    A readable ``japan.schedule_file`` wins over ``japan.schedule``. Its ``key =
    value`` lines speak last for the settings this camera understands; ``period`` is
    stored as ``schedule_len``.
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

    cool_cfg = cfgparse.sub(japan_cfg, "cooling")
    cooling = CoolingCfg(
        enabled=cfgparse.as_bool(cool_cfg, "enabled", False),
        lead_minutes=cfgparse.as_float(cool_cfg, "lead_minutes", 30.0),
        tolerance=cfgparse.as_float(cool_cfg, "tolerance", 3.0),
        off_between_nights=cfgparse.as_bool(cool_cfg, "off_between_nights", True),
        off_on_exit=cfgparse.as_bool(cool_cfg, "off_on_exit", True),
    )
    raw_target = cool_cfg.get("target_temp")
    if raw_target not in (None, ""):
        try:
            cooling.target_temp = float(raw_target)
        except (TypeError, ValueError):
            errors.append(f"japan.cooling.target_temp must be a number of °C, got "
                          f"{raw_target!r}; keeping the camera's own target")
    if cooling.lead_minutes < 0:
        errors.append(f"japan.cooling.lead_minutes must not be negative, got "
                      f"{cooling.lead_minutes}; using 30")
        cooling.lead_minutes = 30.0
    if cooling.tolerance <= 0:
        errors.append(f"japan.cooling.tolerance must be positive, got "
                      f"{cooling.tolerance}; using 3")
        cooling.tolerance = 3.0

    wheel_cfg = cfgparse.sub(japan_cfg, "filter_wheel")
    wheel = FilterWheelCfg(
        port=str(wheel_cfg.get("port") or common_filterwheel.default_port()),
        baudrate=cfgparse.as_int(wheel_cfg, "baudrate", 9600),
        move_timeout=cfgparse.as_float(wheel_cfg, "move_timeout", 8.0),
    )

    loc_cfg = cfgparse.sub(japan_cfg, "location")
    location = LocationCfg(
        name=str(loc_cfg.get("name", "") or "").strip(),
        lat=cfgparse.as_float(loc_cfg, "lat", 0.0),
        lon=cfgparse.as_float(loc_cfg, "lon", 0.0),
        elevation=cfgparse.as_float(loc_cfg, "elevation", 0.0),
    )

    mode = str(japan_cfg.get("mode", DEFAULT_MODE)).strip().lower()
    if mode not in JAPAN_MODES:
        errors.append(f"japan.mode must be one of {JAPAN_MODES}, got {mode!r}; "
                      f"using {DEFAULT_MODE!r}")
        mode = DEFAULT_MODE

    overrides = {}
    schedule_file = str(japan_cfg.get("schedule_file", "") or "").strip()
    if schedule_file:
        try:
            entries, slot_errors, overrides = schedule_mod.load_schedule_file(
                schedule_file, mode, camera.binning)
        except (OSError, ValueError) as exc:
            # UnicodeDecodeError is a ValueError: a file in the wrong encoding.
            entries, slot_errors = [], [f"schedule_file {schedule_file}: {exc}"]
    else:
        entries, slot_errors = schedule_mod.entries_from_config(
            japan_cfg.get("schedule", []), mode, camera.binning)
    errors.extend(slot_errors)

    # This camera has no gain to set, so honouring the rest of such a slot silently
    # would leave the operator believing it took effect.
    if any(entry.gain is not None for entry in entries):
        errors.append("japan schedule slots carry 'gain', which this camera has "
                      "no equivalent for; it is ignored")

    # Only the keys this camera understands: ``site_id`` and ``device_id`` belong to
    # the ASI archive.
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
    # Without ``t_start``, which is UTC in ``sun_cycle`` mode, the cycle is anchored
    # to the first whole minute of the window and in step with no other station.

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

    # Both are ``sun_cycle`` features on this camera; see SPLIT_MODES.
    preflight = exposure_config.parse_preflight(
        cfgparse.sub(japan_cfg, "preflight"), sched.mode, sched.sun_max_angle,
        errors, prefix="japan")
    if preflight.binning and not BINNING_MIN <= preflight.binning <= BINNING_MAX:
        errors.append(f"japan.preflight.binning must be between {BINNING_MIN} and "
                      f"{BINNING_MAX}, got {preflight.binning}; each slot keeps "
                      f"its own binning instead")
        preflight.binning = None
    overexposure = exposure_config.parse_overexposure(
        cfgparse.sub(japan_cfg, "overexposure"), errors, prefix="japan",
        mode=sched.mode, modes=SPLIT_MODES)
    exposure_config.check_threshold_above_target(
        preflight, overexposure, errors, prefix="japan")

    filters, filter_errors = common_filters.parse_filters(
        japan_cfg.get("filters"), what="japan.filters")
    errors.extend(filter_errors)

    name = clean_name(japan_cfg.get("name", ""), "japan.name", errors)
    location.name = clean_name(location.name, "japan.location.name", errors)
    if mode == "sun_cycle":
        # Both go into every file name; an empty field would still have to be
        # parsed, so it is filled with something that says what is missing.
        if not name:
            errors.append("japan.name is not set; sun_cycle frames are named and "
                          "headed with 'JAPAN'")
            name = "JAPAN"
        if not location.name:
            errors.append("japan.location.name is not set; sun_cycle frames are "
                          "named with 'SITE'")
            location.name = "SITE"

    return JapanConfig(
        output_dir=str(japan_cfg.get("output_dir", "") or ""),
        name=name,
        camera=camera,
        cooling=cooling,
        filter_wheel=wheel,
        location=location,
        filters=filters,
        schedule=sched,
        preflight=preflight,
        overexposure=overexposure,
        wait_for_enter=cfgparse.as_bool(japan_cfg, "wait_for_enter", True),
        errors=errors,
    )
