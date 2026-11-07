"""The ``asi`` section of config.json, as typed configuration objects.

Problems do not raise: they are collected into ``AsiConfig.errors`` and reported on
the console, because one bad schedule line must not cost a night.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from . import schedule as schedule_mod
from ..common import cfgparse
from ..common import exposure_config
from ..common import filters as common_filters
# Only for ``default_port``; pyserial is imported when a port opens, never at import.
from ..common import filterwheel as common_filterwheel

DEFAULT_MODE = "sun"

# imagerd_rt's metadata version for the ``Version`` header. Not imported from .fits,
# so that reading a config never drags in astropy.
DEFAULT_LEGACY_VERSION = "3.0"

# Re-exported under the names the driver and its tests use.
SATURATION_ADU = exposure_config.SATURATION_ADU
ARCHIVE_NAME_RESOLUTION = exposure_config.ARCHIVE_NAME_RESOLUTION
PreflightCfg = exposure_config.PreflightCfg
OverexposureCfg = exposure_config.OverexposureCfg

DEFAULT_FILTERS = common_filters.DEFAULT_FILTERS
FilterInfo = common_filters.FilterInfo


@dataclass
class CameraCfg:
    backend: str = "picam"            # "picam" (real SDK) or "sim"
    readout_speed: float = 2.0        # MHz, P.AdcSpeed
    binning: int = 4
    gain: int = 1                     # 1 Low, 2 Medium, 3 High
    frame_timeout_ms: int = 30000
    readout_control_mode: int = 1
    shutter_timing_mode: int = 1
    output_signal: int = 4
    kinetics_window_height: int = 50
    demo: bool = False
    demo_model: str = "Pixis1024F"


@dataclass
class CoolingCfg:
    enabled: bool = True
    target_temp: float = -60.0
    tolerance: float = 3.0
    wait_on_start: bool = True
    wait_timeout: float = 1800.0
    warm_on_exit: bool = True
    warm_temp: float = 13.0
    warm_timeout: float = 900.0


@dataclass
class FilterWheelCfg:
    # ``from_dict`` fills this in; the default is this platform's usual name. "sim"
    # selects the simulator.
    port: str = field(default_factory=common_filterwheel.default_port)
    baudrate: int = 9600
    move_timeout: float = 8.0


@dataclass
class LocationCfg:
    lat: float = 0.0
    lon: float = 0.0
    elevation: float = 0.0


@dataclass
class StationCfg:
    """Identity written into every frame, carried over from imagerd_rt."""

    site_id: str = ""
    device_id: str = ""
    legacy_version: str = DEFAULT_LEGACY_VERSION
    # This controller has no temperature sensor; the header keeps the field so
    # the processing program still finds it.
    fw_temp: float = None


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
class AsiConfig:
    output_dir: str = ""
    camera: CameraCfg = field(default_factory=CameraCfg)
    cooling: CoolingCfg = field(default_factory=CoolingCfg)
    filter_wheel: FilterWheelCfg = field(default_factory=FilterWheelCfg)
    location: LocationCfg = field(default_factory=LocationCfg)
    station: StationCfg = field(default_factory=StationCfg)
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


# Shared with cameras/japan/config.py; local names keep ``asi_config.parse_time``
# working.
parse_time = cfgparse.parse_time
_sub = cfgparse.sub
_float = cfgparse.as_float
_int = cfgparse.as_int
_bool = cfgparse.as_bool


def from_dict(asi_cfg):
    """Build an :class:`AsiConfig` from the ``asi`` sub-dict of config.json.

    A readable ``asi.schedule_file`` wins over ``asi.schedule``. Its globals (mode,
    sun angle, cycle length, station identity) override the matching ``asi.*`` keys:
    converting a station's schedule moves the whole programme, not just the slots.
    """
    asi_cfg = asi_cfg or {}
    errors = []

    cam_cfg = _sub(asi_cfg, "camera")
    camera = CameraCfg(
        backend=str(cam_cfg.get("backend", "picam")).strip().lower(),
        readout_speed=_float(cam_cfg, "readout_speed", 2.0),
        binning=_int(cam_cfg, "binning", 4),
        gain=_int(cam_cfg, "gain", 1),
        frame_timeout_ms=_int(cam_cfg, "frame_timeout_ms", 30000),
        readout_control_mode=_int(cam_cfg, "readout_control_mode", 1),
        shutter_timing_mode=_int(cam_cfg, "shutter_timing_mode", 1),
        output_signal=_int(cam_cfg, "output_signal", 4),
        kinetics_window_height=_int(cam_cfg, "kinetics_window_height", 50),
        demo=_bool(cam_cfg, "demo", False),
        demo_model=str(cam_cfg.get("demo_model", "Pixis1024F")),
    )
    if camera.backend not in ("picam", "sim"):
        errors.append(f"camera.backend must be 'picam' or 'sim', got "
                      f"{camera.backend!r}; using 'picam'")
        camera.backend = "picam"

    cool_cfg = _sub(asi_cfg, "cooling")
    cooling = CoolingCfg(
        enabled=_bool(cool_cfg, "enabled", True),
        target_temp=_float(cool_cfg, "target_temp", -60.0),
        tolerance=_float(cool_cfg, "tolerance", 3.0),
        wait_on_start=_bool(cool_cfg, "wait_on_start", True),
        wait_timeout=_float(cool_cfg, "wait_timeout", 1800.0),
        warm_on_exit=_bool(cool_cfg, "warm_on_exit", True),
        warm_temp=_float(cool_cfg, "warm_temp", 13.0),
        warm_timeout=_float(cool_cfg, "warm_timeout", 900.0),
    )

    wheel_cfg = _sub(asi_cfg, "filter_wheel")
    wheel = FilterWheelCfg(
        port=str(wheel_cfg.get("port") or common_filterwheel.default_port()),
        baudrate=_int(wheel_cfg, "baudrate", 9600),
        move_timeout=_float(wheel_cfg, "move_timeout", 8.0),
    )

    loc_cfg = _sub(asi_cfg, "location")
    location = LocationCfg(
        lat=_float(loc_cfg, "lat", 0.0),
        lon=_float(loc_cfg, "lon", 0.0),
        elevation=_float(loc_cfg, "elevation", 0.0),
    )

    filters, filter_errors = _filters(asi_cfg.get("filters"))
    errors.extend(filter_errors)

    mode = str(asi_cfg.get("mode", DEFAULT_MODE)).strip().lower()
    if mode not in schedule_mod.MODES:
        errors.append(f"asi.mode must be one of {schedule_mod.MODES}, got "
                      f"{mode!r}; using {DEFAULT_MODE!r}")
        mode = DEFAULT_MODE

    overrides = {}
    schedule_file = str(asi_cfg.get("schedule_file", "") or "").strip()
    if schedule_file:
        try:
            entries, slot_errors, overrides = schedule_mod.load_schedule_file(
                schedule_file, mode, camera.binning)
        except OSError as exc:
            entries, slot_errors = [], [f"schedule_file {schedule_file}: {exc}"]
    else:
        entries, slot_errors = schedule_mod.entries_from_config(
            asi_cfg.get("schedule", []), mode, camera.binning)
    errors.extend(slot_errors)

    # The schedule file speaks last, but only for the keys it is allowed to.
    settings = dict(asi_cfg)
    settings.update(overrides)

    if overrides.get("mode"):
        file_mode = str(overrides["mode"]).strip().lower()
        if file_mode in schedule_mod.MODES:
            mode = file_mode
        else:
            errors.append(f"schedule_file mode must be one of "
                          f"{schedule_mod.MODES}, got {file_mode!r}; "
                          f"keeping {mode!r}")

    t_start = parse_time(settings.get("t_start"), None)
    if mode == "time" and t_start is None:
        errors.append("asi.t_start is required in 'time' mode (HH:MM); using 20:00")
        t_start = time(20, 0)
    # ``sun_cycle`` without a ``t_start`` is legitimate: a converted imagerd_rt
    # schedule has none and keeps the whole-minute anchor. With one, it is UTC.

    schedule_len = None
    raw_len = settings.get("schedule_len")
    if raw_len not in (None, "", 0):
        try:
            schedule_len = float(raw_len)
        except (TypeError, ValueError):
            errors.append(f"asi.schedule_len must be a number of seconds, got "
                          f"{raw_len!r}; deriving it from the slots instead")
        else:
            if schedule_len <= 0:
                errors.append(f"asi.schedule_len must be positive, got "
                              f"{schedule_len}; deriving it from the slots instead")
                schedule_len = None

    dead_time = _float(settings, "dead_time", 5.0)
    disagreement = schedule_mod.period_mismatch(
        mode, entries, schedule_len, dead_time, what="asi")
    if disagreement:
        errors.append(disagreement)

    sched = ScheduleCfg(
        mode=mode,
        sun_max_angle=_float(settings, "sun_max_angle", -10.0),
        t_start=t_start,
        entries=entries,
        dark_frames=_int(settings, "dark_frames", 3),
        dead_time=dead_time,
        schedule_len=schedule_len,
    )

    # The ASI imager drives the split guard in every mode that has slots: no
    # ``modes``.
    preflight = exposure_config.parse_preflight(
        _sub(asi_cfg, "preflight"), sched.mode, sched.sun_max_angle, errors,
        prefix="asi")
    overexposure = exposure_config.parse_overexposure(
        _sub(asi_cfg, "overexposure"), errors, prefix="asi")
    exposure_config.check_threshold_above_target(
        preflight, overexposure, errors, prefix="asi")

    fw_temp = settings.get("fw_temp")
    station = StationCfg(
        site_id=str(settings.get("site_id", "") or ""),
        device_id=str(settings.get("device_id", "") or ""),
        legacy_version=str(settings.get("legacy_version", "")
                           or DEFAULT_LEGACY_VERSION),
        fw_temp=None if fw_temp in (None, "") else _float(settings, "fw_temp", 0.0),
    )

    return AsiConfig(
        output_dir=str(asi_cfg.get("output_dir", "") or ""),
        camera=camera,
        cooling=cooling,
        filter_wheel=wheel,
        location=location,
        station=station,
        filters=filters,
        schedule=sched,
        preflight=preflight,
        overexposure=overexposure,
        wait_for_enter=_bool(asi_cfg, "wait_for_enter", True),
        errors=errors,
    )


def _filters(raw):
    return common_filters.parse_filters(raw, what="asi.filters")
