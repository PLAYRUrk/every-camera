"""Schedule entries and cycle timing for both imagers. Pure: no camera, and no clock
beyond what is passed in.

* **sun** — entries fire on given seconds of every minute while the sun stays below
  ``sun_max_angle``. Entry: ``filter, exposure, seconds``.
* **time** — one general cycle repeats, phase-locked to ``t_start``. Entry: ``delta,
  filter, exposure, binning, gain, readout``, ``delta`` being seconds from the start
  of the cycle.
* **sun_cycle** — the same cycle, started by the sun: the pre-darks are timed to
  finish as the altitude reaches ``sun_max_angle``. ``t_start`` is read as UTC, so
  stations at different sites run in step and a run joins at whichever slot is due.
  Without it the anchor is the first whole minute of the window.

A schedule file may carry the globals in :data:`FILE_OVERRIDE_KEYS`: a period kept
only in config.json silently survives a swap of its schedule.
"""
from __future__ import annotations

import json
import math
import re

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Filter positions of this instrument (documented in the station schedule file):
#   1 = 557.7 nm, 2 = 630.0 nm, 3 = broadband OH, 4 = 840.0 nm,
#   5 = 846.5 nm, 6 = 857.0 nm
FILTER_MIN = 1
FILTER_MAX = 6

MODES = ("sun", "time", "sun_cycle")

# Modes whose entries carry ``delta`` rather than seconds-within-a-minute.
CYCLE_MODES = ("time", "sun_cycle")

GAIN_MIN = 1        # 1 Low, 2 Medium, 3 High — PICAM AdcAnalogGain
GAIN_MAX = 3

# An explicit list, so a stray key in a schedule file cannot redefine, say, the output
# directory.
FILE_OVERRIDE_KEYS = ("mode", "sun_max_angle", "schedule_len", "t_start",
                      "dark_frames", "dead_time", "site_id", "device_id")

# ``period`` is the word the console, the setup wizard and the observers use.
FILE_OVERRIDE_ALIASES = {"period": "schedule_len", "cycle": "schedule_len"}

# Slot lines use ``;`` or ``,`` and never contain ``=``, so the two shapes cannot be
# confused.
_DIRECTIVE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


@dataclass
class Entry:
    filter: int
    exposure: float
    seconds: list = field(default_factory=list)   # sun mode: seconds within a minute
    delta: float = None                           # cycle modes: offset in the cycle
    binning: int = None                           # binning for this cycle
    gain: int = None                              # analog gain for this cycle
    readout: float = None                         # legacy prep_time, in seconds

    def as_dict(self):
        out = {"filter": self.filter, "exposure": self.exposure}
        if self.seconds:
            out["seconds"] = list(self.seconds)
        if self.delta is not None:
            out["delta"] = self.delta
        if self.binning is not None:
            out["binning"] = self.binning
        if self.gain is not None:
            out["gain"] = self.gain
        if self.readout is not None:
            out["readout"] = self.readout
        return out


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
    """Returns ``(entries, errors)``.

    Unusable slots are reported and skipped, so one bad line cannot stop a night.
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

        # Both are optional: a slot that does not name them falls back to the
        # camera-wide gain and the schedule-wide dead time.
        gain = None
        if slot.get("gain", slot.get("ccd_gain")) is not None:
            gain = _as_int(slot.get("gain", slot.get("ccd_gain")), what, errors)
            if gain is not None and not GAIN_MIN <= gain <= GAIN_MAX:
                errors.append(f"{what}: gain must be {GAIN_MIN}..{GAIN_MAX}, "
                              f"got {gain}")
                gain = None
        readout = None
        if slot.get("readout", slot.get("prep_time")) is not None:
            readout = _as_float(slot.get("readout", slot.get("prep_time")),
                                what, errors)
            if readout is not None and readout < 0:
                errors.append(f"{what}: readout must not be negative, got {readout}")
                readout = None

        if mode in CYCLE_MODES:
            # A slot written for the wrong mode is the commonest way a schedule comes
            # out empty, and the camera then starts in setup mode, so name the cause.
            if slot.get("delta") is None:
                hint = (" This slot has 'seconds' instead, which belongs to "
                        "'sun' mode — either set mode to 'sun' or give the slot "
                        "a delta." if slot.get("seconds") is not None else "")
                errors.append(f"{what}: {mode!r} mode needs 'delta', the offset "
                              f"in seconds from the start of the cycle.{hint}")
                continue
            delta = _as_float(slot.get("delta"), what, errors)
            if delta is None:
                continue
            entries.append(Entry(filter=filter_num, exposure=exposure,
                                 delta=delta, binning=binning, gain=gain,
                                 readout=readout))
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
                                 seconds=sorted(seconds), binning=binning,
                                 gain=gain, readout=readout))
    return entries, errors


def parse_schedule_text(text, mode, default_binning=1):
    """Parse a legacy ``schedule.txt``. Returns ``(entries, errors, overrides)``.

    ``sun`` mode: ``filter,exposure,seconds``, e.g. ``1,55,0:30``
    Cycle modes: ``delta;filter;exposure;binning;gain;readout``

    In a cycle line only the first three fields are required. ``binning`` falls back
    to the camera's; ``gain`` and ``readout`` stay unset, so the slot takes the
    camera-wide gain and the schedule-wide dead time. A field may be left empty:
    ``0;3;25;1;;5`` names a readout without a gain.

    A line may instead be ``key = value``, setting one of :data:`FILE_OVERRIDE_KEYS`
    for the whole programme, e.g. ``period = 1440``.
    """
    slots, errors, overrides = [], [], {}
    # Two passes, because ``mode`` decides how a slot line is punctuated: a file
    # that sets it below its slots must still be read the way it says.
    body = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        directive = _DIRECTIVE.match(line)
        if not directive:
            body.append((lineno, line))
            continue
        key = directive.group(1).strip().lower()
        key = FILE_OVERRIDE_ALIASES.get(key, key)
        value = directive.group(2).strip()
        if key not in FILE_OVERRIDE_KEYS:
            errors.append(f"Line {lineno}: unknown setting {key!r}; the "
                          f"schedule file may set {', '.join(FILE_OVERRIDE_KEYS)}")
        elif not value:
            errors.append(f"Line {lineno}: {key} has no value")
        else:
            overrides[key] = value

    file_mode = str(overrides.get("mode", mode)).strip().lower()
    if file_mode in MODES:
        mode = file_mode

    for lineno, line in body:
        try:
            if mode in CYCLE_MODES:
                parts = [p.strip() for p in line.split(";")]
                if len(parts) < 3:
                    errors.append(
                        f"Line {lineno}: expected "
                        f"'delta;filter;exposure;binning;gain;readout' "
                        f"(the last three optional), got {line!r}")
                    continue
                slot = {
                    "delta": float(parts[0]),
                    "filter": int(parts[1]),
                    "exposure": float(parts[2]),
                    "binning": int(parts[3]) if len(parts) > 3 and parts[3]
                    else default_binning,
                }
                # An absent key is what makes ``entries_from_config`` fall back.
                if len(parts) > 4 and parts[4]:
                    slot["gain"] = int(parts[4])
                if len(parts) > 5 and parts[5]:
                    slot["readout"] = float(parts[5])
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
    return entries, errors + slot_errors, overrides


def parse_schedule_json(text, mode, default_binning=1):
    """Parse a converted imagerd_rt schedule. Returns ``(entries, errors, overrides)``.

    The file is a bare list of slots, or an object holding them under ``slots`` or
    ``schedule``, whose other keys are the globals of that schedule.conf.
    """
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return [], [f"schedule file: not valid JSON ({exc})"], {}

    overrides = {}
    if isinstance(payload, dict):
        slots = payload.get("slots", payload.get("schedule", []))
        overrides = {k: payload[k] for k in FILE_OVERRIDE_KEYS if k in payload}
        mode = str(overrides.get("mode", mode)).strip().lower()
    elif isinstance(payload, list):
        slots = payload
    else:
        return [], [f"schedule file: expected an object or a list, got "
                    f"{type(payload).__name__}"], {}

    entries, errors = entries_from_config(slots, mode, default_binning)
    return entries, errors, overrides


def load_schedule_file(path, mode, default_binning=1):
    """Returns ``(entries, errors, overrides)``. JSON is told by extension or first
    character.
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    if str(path).lower().endswith(".json") or text.lstrip()[:1] in ("{", "["):
        return parse_schedule_json(text, mode, default_binning)
    return parse_schedule_text(text, mode, default_binning)


def schedule_to_text(entries, mode, period=None):
    """Render entries as a text schedule; ``period`` becomes a ``period =`` header when
    given.
    """
    lines = []
    if period:
        lines.append(f"period = {float(period):g}")
    if mode in CYCLE_MODES:
        # The long form only when needed, so a schedule that never named a gain
        # round-trips to the four fields a station file has.
        detailed = any(e.gain is not None or e.readout is not None
                       for e in entries)
        lines.append("# delta(s);filter;exposure(s);binning" +
                     (";gain;readout(s)" if detailed else ""))
        for entry in sorted(entries, key=lambda e: e.delta or 0):
            line = (f"{entry.delta:g};{entry.filter};{entry.exposure:g};"
                    f"{entry.binning or 1}")
            if detailed:
                gain = "" if entry.gain is None else f"{entry.gain:g}"
                readout = "" if entry.readout is None else f"{entry.readout:g}"
                line += f";{gain};{readout}"
            lines.append(line)
    else:
        lines.append("# filter,exposure(s),seconds")
        for entry in entries:
            secs = ":".join(str(s) for s in (entry.seconds or [0]))
            lines.append(f"{entry.filter},{entry.exposure:g},{secs}")
    return "\n".join(lines) + "\n"


def unique_dark_settings(entries):
    """One ``(exposure, binning, gain, readout)`` per unique exposure, in schedule order.

    A dark must match the lights it is subtracted from, so the settings come from the
    first slot using that exposure.
    """
    combos, seen = [], set()
    for entry in entries:
        if entry.exposure not in seen:
            seen.add(entry.exposure)
            combos.append((entry.exposure, entry.binning or 1, entry.gain,
                           entry.readout))
    return combos


def unique_exposures(entries):
    return [(exposure, binning)
            for exposure, binning, _gain, _readout in unique_dark_settings(entries)]


def cycle_period(entries, dead_time):
    """Last slot's delta + exposure + readout (its own ``prep_time``, else the dead
    time).
    """
    if not entries:
        return float(dead_time)
    last = max(entries, key=lambda e: e.delta or 0.0)
    readout = last.readout if last.readout is not None else dead_time
    return (last.delta or 0.0) + last.exposure + readout


def period_mismatch(mode, entries, schedule_len, dead_time, what="the schedule"):
    """The warning for a stated period that disagrees with its slots, or ``""``.

    A cycle may close a little after its last slot. The mistake is a gap at the end of
    the cycle wider than any gap inside it: the first slot of the following cycle then
    becomes the next frame, and a start less than one period before ``t_start`` sits
    idle until ``t_start``.

    The period is never corrected: which number is wrong is the operator's to say.
    """
    if mode not in CYCLE_MODES or not entries or not schedule_len:
        return ""
    stated = float(schedule_len)
    derived = cycle_period(entries, dead_time)

    last = max(entries, key=lambda e: e.delta or 0.0)
    own = last.readout is not None
    readout = last.readout if own else dead_time
    how = (f"Δ{last.delta or 0.0:g} + {last.exposure:g} s exposure + "
           f"{readout:g} s {'readout' if own else 'dead time'}")

    if stated < derived - 1.0:
        return (f"{what}: period = {stated:g} s, but the slots need {derived:g} s "
                f"({how}). The last slot overruns the cycle by "
                f"{derived - stated:g} s.")

    deltas = sorted(float(e.delta or 0.0) for e in entries)
    if len(deltas) < 2:
        return ""
    widest = max(b - a for a, b in zip(deltas, deltas[1:]))
    tail = stated - derived
    if tail <= widest:
        return ""
    return (f"{what}: period = {stated:g} s, but the slots close the cycle at "
            f"{derived:g} s ({how}). The last {tail:g} s of every cycle hold no "
            f"slot, though its slots are never more than {widest:g} s apart — "
            f"one of the two numbers is not what was meant.")


def slot_gap(entries, period, entry):
    """Seconds from this slot to the next, wrapping at the end.

    The last slot's gap runs to the first slot of the next iteration, not to the end
    of the cycle.
    """
    if not entries:
        return float(period)
    deltas = sorted(float(e.delta or 0.0) for e in entries)
    mine = float(entry.delta or 0.0)
    later = [d for d in deltas if d > mine]
    if later:
        return later[0] - mine
    return float(period) - mine + deltas[0]


def slot_budget(entries, period, entry, dead_time):
    """How long an exposure at this slot may run: the gap to the next slot, less the
    readout.

    The guard that stops an automatically chosen exposure from pushing the next slot
    late.
    """
    readout = entry.readout if entry.readout is not None else dead_time
    return max(slot_gap(entries, period, entry) - float(readout), 0.0)


def _as_utc_time(t_start):
    if t_start is None:
        return None
    local = datetime.combine(datetime.now().date(), t_start).astimezone()
    return local.astimezone(timezone.utc).strftime("%H:%M:%S")


def schedule_snapshot(sched):
    """An observing programme as plain JSON types, for observers.

    Which cycle a frame belongs to is written in neither file name nor header, so an
    archive can only be grouped into cycles by replaying this phase reference and
    period against the frame times.

    Duck-typed: the japan and asi ``ScheduleCfg`` are separate but agree on every
    field read here.
    """
    if sched is None:
        return {}
    mode = getattr(sched, "mode", "")
    t_start = getattr(sched, "t_start", None)
    entries = list(getattr(sched, "entries", None) or [])
    if mode == "sun_cycle" and t_start is not None:
        # ``t_start`` is already UTC in this mode; the local reading is derived.
        anchor = utc_cycle_anchor(t_start, datetime.now())
        t_start_local = anchor.strftime("%H:%M:%S")
        t_start_utc = t_start.strftime("%H:%M:%S")
    else:
        t_start_local = t_start.strftime("%H:%M:%S") if t_start else None
        t_start_utc = _as_utc_time(t_start)
    return {
        "mode": mode,
        "t_start": t_start_local,
        # Frame names are UTC and an observer never learns the station's offset, so
        # the anchor travels in UTC, and only the camera can convert it.
        "t_start_utc": t_start_utc,
        "period": float(getattr(sched, "period", 0.0) or 0.0),
        "dead_time": float(getattr(sched, "dead_time", 0.0) or 0.0),
        "entries": [{"filter": e.filter, "delta": e.delta,
                     "exposure": e.exposure, "seconds": list(e.seconds or [])}
                    for e in entries],
    }


def next_cycle_slot(t_start, period, entries, now):
    """``(slot_time, entry, iteration)`` of the next capture.

    The phase is always measured from ``t_start``, never from launch; before
    ``t_start`` the iteration is negative.
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


def cycle_anchor(t_start, now):
    """The occurrence of ``t_start`` nearest to ``now``, in either direction.

    Today's date alone is wrong after midnight: at 00:30 under ``t_start = 22:00`` the
    anchor would be 21.5 h away, so a station would shoot darks it should skip and, on
    a period that does not divide a day, put every slot on the wrong phase.
    """
    anchor = datetime.combine(now.date(), t_start)
    if anchor - now > timedelta(hours=12):
        return anchor - timedelta(days=1)
    if now - anchor > timedelta(hours=12):
        return anchor + timedelta(days=1)
    return anchor


def utc_cycle_anchor(t_start_utc, now):
    """The occurrence of the UTC time of day ``t_start_utc`` nearest to ``now``.

    The ``sun_cycle`` counterpart of :func:`cycle_anchor`: UTC lets stations in
    different time zones share one phase. ``now`` and the result are naive local
    datetimes, so the result can go straight to :func:`next_cycle_slot`.
    """
    from .timeutil import to_utc

    now_utc = to_utc(now)
    anchor = datetime.combine(now_utc.date(), t_start_utc, tzinfo=timezone.utc)
    if anchor - now_utc > timedelta(hours=12):
        anchor -= timedelta(days=1)
    elif now_utc - anchor > timedelta(hours=12):
        anchor += timedelta(days=1)
    return anchor.astimezone().replace(tzinfo=None)


def next_second_slot(seconds, now=None):
    now = now or datetime.now()
    ordered = sorted(set(int(s) for s in seconds)) or [0]
    current = now.second + now.microsecond / 1e6
    future = [s for s in ordered if s > current]
    if future:
        return now.replace(second=future[0], microsecond=0)
    nxt = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return nxt.replace(second=ordered[0])


def next_minute_boundary(t):
    """The first whole minute at or after ``t``; keeps slot times comparable between
    nights.
    """
    truncated = t.replace(second=0, microsecond=0)
    return truncated if truncated == t else truncated + timedelta(minutes=1)


def estimate_cycle_dark_duration(entries, dark_frames, dead_time):
    """Worst-case seconds for the cycle-mode darks, +10 % and a 30 s buffer.

    They run back to back, with no second-of-the-minute to wait for.
    """
    total = 0.0
    for exposure, _binning, _gain, readout in unique_dark_settings(entries):
        gap = readout if readout is not None else dead_time
        total += dark_frames * (exposure + gap)
    return total * 1.1 + 30.0


def estimate_dark_duration(entries, dark_frames):
    """Worst-case seconds for every dark frame, +10 % and a 30 s buffer.

    Starts the pre-darks early enough to finish as the measurement window opens.
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

    Brackets in 5-minute steps, then bisects. ``now`` when already dark enough, ``now
    + 24 h`` if no crossing is found.
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
