"""Compatibility shim — the module now lives in ``cameras/common/schedule.py``.

The schedule vocabulary there is the union of both imagers': ``sun_cycle`` and
per-slot ``gain`` are ASI-only, and it is each camera's ``config.py`` that decides
what it accepts.

The explicit re-export list below is load-bearing, not decoration. Tests reach
into the driver's module reference — ``monkeypatch.setattr(asi_driver.asi_schedule,
"next_minute_boundary", ...)`` — and ``asi_driver`` calls these as attributes of
that module object, so patching this shim still intercepts them. What a patch here
does *not* reach is a cross-call made inside ``cameras/common/schedule.py`` itself;
a test that needs that should patch ``cameras.common.schedule`` instead.
"""
from ..common.schedule import *          # noqa: F401, F403
from ..common.schedule import (          # noqa: F401
    CYCLE_MODES, Entry, FILE_OVERRIDE_KEYS, FILTER_MAX, FILTER_MIN,
    GAIN_MAX, GAIN_MIN, MODES,
    cycle_anchor, cycle_period, entries_from_config, estimate_cycle_dark_duration,
    estimate_dark_duration, load_schedule_file, next_cycle_slot,
    next_minute_boundary, next_second_slot, parse_schedule_json,
    parse_schedule_text, period_mismatch, schedule_snapshot, schedule_to_text,
    slot_budget, slot_gap, sun_crossing_time, unique_dark_settings,
    unique_exposures,
)
