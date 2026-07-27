"""
Schedule entries and cycle timing for the ASI imager.

Two schedule shapes exist, and both are kept because they describe genuinely
different observing programmes:

* **sun** — entries fire on given seconds of every minute for as long as the sun
  stays below ``sun_max_angle``. Each entry is ``filter, exposure, seconds``.
* **time** — one "general cycle" of fixed period repeats, phase-locked to a
  single ``t_start``. Each entry is ``delta, filter, exposure, binning``, where
  ``delta`` is seconds from the start of the cycle. The period is derived, not
  configured: last delta + that exposure + dead time.

Entries come either from ``config.json`` (a list of objects, the normal case) or
from a legacy asi-camera ``schedule.txt``; :func:`parse_schedule_text` reads the
old format so an existing station file keeps working.

Everything here is pure: no camera, no clock beyond what is passed in. That is
what makes the cycle arithmetic — the subtlest part of the driver — testable.
"""
from __future__ import annotations

import math

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Filter positions of this instrument (documented in the station schedule file):
#   1 = 557.7 nm, 2 = 630.0 nm, 3 = broadband OH, 4 = 840.0 nm,
#   5 = 846.5 nm, 6 = 857.0 nm
FILTER_MIN = 1
FILTER_MAX = 6

MODES = ("sun", "time")


@dataclass
class Entry:
    """One line of the schedule."""

    filter: int
    exposure: float
    seconds: list = field(default_factory=list)   # sun mode: seconds within a minute
    delta: float = None                           # time mode: offset in the cycle
    binning: int = None                           # time mode: binning for this cycle

    def as_dict(self):
        out = {"filter": self.filter, "exposure": self.exposure}
        if self.seconds:
            out["seconds"] = list(self.seconds)
        if self.delta is not None:
            out["delta"] = self.delta
        if self.binning is not None:
            out["binning"] = self.binning
        return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _as_int(value, what, errors):
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f"{what}: expected an integer, got {value!r}")
        return None


def _as_float(value, what, errors):
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"{what}: expected a number, got {value!r}")
        return None


def entries_from_config(slots, mode, default_binning=1):
    """Build entries from the ``asi.schedule`` list in config.json.

    Returns ``(entries, errors)``. Unusable slots are reported and skipped rather
    than raising, so one bad line cannot stop a night of measurements.
    """
    entries, errors = [], []
    for index, slot in enumerate(slots or [], 1):
        if not isinstance(slot, dict):
            errors.append(f"Slot {index}: expected an object, got {type(slot).__name__}")
            continue
        what = f"Slot {index}"
        filter_num = _as_int(slot.get("filter", slot.get("filter_num")), what, errors)
        exposure = _as_float(slot.get("exposure", slot.get("exposure_sec")), what, errors)
        if filter_num is None or exposure is None:
            continue
        if not FILTER_MIN <= filter_num <= FILTER_MAX:
            errors.append(f"{what}: filter must be {FILTER_MIN}..{FILTER_MAX}, "
                          f"got {filter_num}")
            continue
        if exposure <= 0:
            errors.append(f"{what}: exposure must be positive, got {exposure}")
            continue
        binning = _as_int(slot.get("binning", default_binning), what, errors) \
            or default_binning

        if mode == "time":
            delta = _as_float(slot.get("delta"), what, errors)
            if delta is None:
                continue
            entries.append(Entry(filter=filter_num, exposure=exposure,
                                 delta=delta, binning=binning))
        else:
            raw = slot.get("seconds", [0])
            if isinstance(raw, (int, float)):
                raw = [raw]
            seconds = []
            for value in raw:
                sec = _as_int(value, what, errors)
                if sec is None:
                    continue
                if not 0 <= sec <= 59:
                    errors.append(f"{what}: second must be 0..59, got {sec}")
                    continue
                if sec not in seconds:
                    seconds.append(sec)
            if not seconds:
                errors.append(f"{what}: no valid capture seconds")
                continue
            entries.append(Entry(filter=filter_num, exposure=exposure,
                                 seconds=sorted(seconds), binning=binning))
    return entries, errors


def parse_schedule_text(text, mode, default_binning=1):
    """Parse a legacy asi-camera ``schedule.txt``. Returns ``(entries, errors)``.

    ``sun``  mode: ``filter,exposure,seconds``       e.g. ``1,55,0:30``
    ``time`` mode: ``delta;filter;exposure;binning`` e.g. ``100;3;25;1``
    """
    slots, errors = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if mode == "time":
                parts = [p.strip() for p in line.split(";")]
                if len(parts) < 3:
                    errors.append(f"Line {lineno}: expected "
                                  f"'delta;filter;exposure;binning', got {line!r}")
                    continue
                slot = {
                    "delta": float(parts[0]),
                    "filter": int(parts[1]),
                    "exposure": float(parts[2]),
                    "binning": int(parts[3]) if len(parts) > 3 and parts[3]
                    else default_binning,
                }
            else:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 3:
                    errors.append(f"Line {lineno}: expected "
                                  f"'filter,exposure,seconds', got {line!r}")
                    continue
                slot = {
                    "filter": int(parts[0]),
                    "exposure": float(parts[1]),
                    "seconds": [int(s) for s in parts[2].split(":") if s.strip()],
                    "binning": default_binning,
                }
        except ValueError as exc:
            errors.append(f"Line {lineno}: {exc}")
            continue
        slots.append(slot)

    entries, slot_errors = entries_from_config(slots, mode, default_binning)
    return entries, errors + slot_errors


def load_schedule_file(path, mode, default_binning=1):
    with open(path, "r") as fh:
        return parse_schedule_text(fh.read(), mode, default_binning)


def schedule_to_text(entries, mode):
    """Render entries back into the legacy file format (used by the setup tools)."""
    lines = []
    if mode == "time":
        lines.append("# delta(s);filter;exposure(s);binning")
        for entry in sorted(entries, key=lambda e: e.delta or 0):
            lines.append(f"{entry.delta:g};{entry.filter};{entry.exposure:g};"
                         f"{entry.binning or 1}")
    else:
        lines.append("# filter,exposure(s),seconds")
        for entry in entries:
            secs = ":".join(str(s) for s in (entry.seconds or [0]))
            lines.append(f"{entry.filter},{entry.exposure:g},{secs}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
def unique_exposures(entries):
    """One ``(exposure, binning)`` pair per unique exposure, in schedule order.

    Dark frames are keyed by exposure only; the binning comes from the first
    cycle that uses that exposure.
    """
    combos, seen = [], set()
    for entry in entries:
        if entry.exposure not in seen:
            seen.add(entry.exposure)
            combos.append((entry.exposure, entry.binning or 1))
    return combos


def cycle_period(entries, dead_time):
    """General-cycle length = last cycle's delta + its exposure + dead time."""
    if not entries:
        return float(dead_time)
    last = max(entries, key=lambda e: e.delta or 0.0)
    return (last.delta or 0.0) + last.exposure + dead_time


def next_cycle_slot(t_start, period, entries, now):
    """``(slot_time, entry, iteration)`` of the next capture, phase-locked to ``t_start``.

    Handles warm-up (``now`` before ``t_start`` → negative iteration) and a late
    start identically: the phase is always measured from ``t_start``, never from
    when the program happened to be launched.
    """
    ordered = sorted(entries, key=lambda e: e.delta or 0.0)
    elapsed = (now - t_start).total_seconds()
    k = math.floor(elapsed / period)
    eps = timedelta(milliseconds=1)
    for kk in (k, k + 1):
        base = t_start + timedelta(seconds=kk * period)
        for entry in ordered:
            slot = base + timedelta(seconds=entry.delta or 0.0)
            if slot > now + eps:
                return slot, entry, kk
    # Not reachable in practice; first entry of the next iteration.
    base = t_start + timedelta(seconds=(k + 1) * period)
    return base + timedelta(seconds=ordered[0].delta or 0.0), ordered[0], k + 1


def next_second_slot(seconds, now=None):
    """Next datetime matching any of ``seconds`` within this or the next minute."""
    now = now or datetime.now()
    ordered = sorted(set(int(s) for s in seconds)) or [0]
    current = now.second + now.microsecond / 1e6
    future = [s for s in ordered if s > current]
    if future:
        return now.replace(second=future[0], microsecond=0)
    nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return nxt.replace(second=ordered[0])


def estimate_dark_duration(entries, dark_frames):
    """Worst-case seconds to shoot every dark frame, +10 % and a 30 s buffer.

    Used to start the pre-darks early enough that they finish exactly when the
    measurement window opens.
    """
    per_exposure = {}
    for entry in entries:
        per_exposure.setdefault(entry.exposure, set()).update(entry.seconds or [0])
    total = 0.0
    for exposure, secs in per_exposure.items():
        interval = 60.0 / max(1, len(secs))     # longest wait between capture slots
        total += dark_frames * (interval + exposure)
    return total * 1.1 + 30.0


def sun_crossing_time(angle_fn, sun_max_angle, now=None):
    """Next moment the solar altitude drops to ``sun_max_angle``.

    ``angle_fn(datetime) -> degrees``. Walks forward in 5-minute steps to bracket
    the crossing, then binary-searches it to ~5 ms. Returns ``now`` when it is
    already dark enough, and ``now + 24 h`` if no crossing is found.
    """
    now = now or datetime.now()
    if angle_fn(now) <= sun_max_angle:
        return now
    step = timedelta(minutes=5)
    t = now
    for _ in range(288):                        # up to 24 h
        t += step
        if angle_fn(t) <= sun_max_angle:
            lo, hi = t - step, t
            for _ in range(20):
                mid = lo + (hi - lo) / 2
                if angle_fn(mid) <= sun_max_angle:
                    hi = mid
                else:
                    lo = mid
            return hi
    return now + timedelta(hours=24)
