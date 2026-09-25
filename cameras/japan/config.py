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

The ``sun_cycle`` mode brings five settings of its own, because it is the ASI
imager's night run on this camera: its frames are filed in that archive layout,
so ``name`` (the instrument name, written into the ``NAME`` card and into the
file name), ``location.name`` (the observing site's name, the file name's site
field and the ``SiteID`` record) and ``filters`` (the wheel table whose
wavelength tags go into the name); and it runs that mode's two intensity-control
loops, so ``preflight`` and ``overexposure`` as well. Those last two are parsed
by ``cameras/common/exposure_config.py``, shared with the ASI imager, with one
difference this module imposes: the split guard here runs in ``sun_cycle`` only,
while on the ASI imager it also runs in ``time``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from ..common import cfgparse
from ..common import exposure_config
from ..common import filters as common_filters
# Only for ``default_port``; the module pulls in pyserial when it opens a port,
# never at import, so reading a config still costs nothing.
from ..common import filterwheel as common_filterwheel
from ..common import schedule as schedule_mod

DEFAULT_MODE = "sun"

# The two schedule shapes the Hamamatsu program had, plus ``sun_cycle``: the
# general cycle of ``time`` mode, started by the sun and phase-locked to
# ``t_start`` in UTC, so that this camera and the ASI imager — or two stations
# far apart — run the same cycle in step, with the same automatic twilight
# exposure and the same slot splitting.
JAPAN_MODES = ("sun", "time", "sun_cycle")

# Where the split guard is allowed to drive. On the ASI imager it runs in every
# mode that has slots; here it is a ``sun_cycle`` feature only, so that the
# ``time`` mode this camera came with keeps shooting exactly what its schedule
# says, one frame per slot, as the standalone program did.
SPLIT_MODES = ("sun_cycle",)

# The intensity-control blocks are shared with the ASI imager; re-exported so
# ``japan_config.PreflightCfg`` reads as naturally as ``asi_config``'s does.
PreflightCfg = exposure_config.PreflightCfg
OverexposureCfg = exposure_config.OverexposureCfg
SATURATION_ADU = exposure_config.SATURATION_ADU

# Characters a name may not carry into the ``sun_cycle`` file name: ``_``
# separates its fields, and the rest would make a path or a broken name.
_NAME_FORBIDDEN = frozenset('_/\\ \t:*?"<>|')

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
    # Filled in by ``from_dict`` from config.json; the dataclass default is this
    # platform's usual name. "sim" selects the simulator.
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
    schedule_len: float = None        # time mode: explicit period, seconds

    @property
    def period(self):
        """Length of one general cycle (``time`` and ``sun_cycle`` modes).

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
    name: str = ""                    # instrument name: NAME card, sun_cycle names
    camera: CameraCfg = field(default_factory=CameraCfg)
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
        """The wheel position's wavelength tag and description (``sun_cycle``)."""
        return common_filters.lookup(self.filters, number)


def clean_name(value, what, errors):
    """``value`` made safe for the ``sun_cycle`` file name, with a note if changed.

    The name is ``..._SITE_NAME_WAVE_...``, split on underscores by whatever
    reads it back, so an underscore inside a field would silently shift every
    field after it.
    """
    text = str(value or "").strip()
    cleaned = "".join("-" if ch in _NAME_FORBIDDEN else ch for ch in text)
    if cleaned != text:
        errors.append(f"{what} {text!r} carries characters a file name field "
                      f"cannot hold; using {cleaned!r}")
    return cleaned


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
    # In ``sun_cycle`` mode ``t_start`` is UTC and optional: without it the
    # cycle is anchored to the first whole minute of the window, as the ASI
    # imager does, and is simply not in step with any other station.

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

    # The two intensity-control loops. Both are ``sun_cycle`` features on this
    # camera — see SPLIT_MODES for why the guard is narrower here than on the
    # ASI imager — and both switch themselves off rather than run misconfigured.
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
        filter_wheel=wheel,
        location=location,
        filters=filters,
        schedule=sched,
        preflight=preflight,
        overexposure=overexposure,
        wait_for_enter=cfgparse.as_bool(japan_cfg, "wait_for_enter", True),
        errors=errors,
    )
