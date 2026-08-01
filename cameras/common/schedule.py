"""
Schedule entries and cycle timing for the ASI imager.

Three schedule shapes exist, and all are kept because they describe genuinely
different observing programmes:

* **sun** — entries fire on given seconds of every minute for as long as the sun
  stays below ``sun_max_angle``. Each entry is ``filter, exposure, seconds``.
* **time** — one "general cycle" of fixed period repeats, phase-locked to a
  single ``t_start``. Each entry is ``delta, filter, exposure, binning, gain,
  readout``, where ``delta`` is seconds from the start of the cycle. The period
  is ``schedule_len`` when configured, otherwise derived: last delta + that
  exposure + that entry's readout.
* **sun_cycle** — the same cycle as *time*, but anchored to the evening the sun
  drops to ``sun_max_angle`` instead of to a wall-clock time. This is what
  imagerd_rt did (``imagerd_rt.c:531-738``): nothing is exposed until the sun is
  low enough, the pre-darks are timed to finish just as that happens, and the
  cycle then free-runs from the first whole minute of the window.

Entries come from ``config.json`` (a list of objects, the normal case), from a
converted imagerd_rt schedule in JSON, or from a legacy asi-camera
``schedule.txt``; :func:`parse_schedule_text` reads the oldest format so an
existing station file keeps working.

Either file format may also carry the globals in :data:`FILE_OVERRIDE_KEYS` —
notably ``schedule_len``, writable as ``period = 1440`` at the top of a text
schedule. A schedule and the length of its cycle belong together: keeping the
period in config.json meant that swapping schedules silently kept the old one.

Everything here is pure: no camera, no clock beyond what is passed in. That is
what makes the cycle arithmetic — the subtlest part of the driver — testable.
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

# Modes built from a repeating general cycle, where entries carry ``delta``
# rather than seconds-within-a-minute.
CYCLE_MODES = ("time", "sun_cycle")

GAIN_MIN = 1        # 1 Low, 2 Medium, 3 High — PICAM AdcAnalogGain
GAIN_MAX = 3

# The globals a converted imagerd_rt schedule may carry with it. Keeping this an
# explicit list means a stray key in a schedule file cannot silently redefine,
# say, the output directory.
FILE_OVERRIDE_KEYS = ("mode", "sun_max_angle", "schedule_len", "t_start",
                      "dark_frames", "dead_time", "site_id", "device_id")

# What an operator may write instead of the canonical key. ``period`` is the
# word the console, the setup wizard and the observers all use for
# ``schedule_len``, and a schedule file is edited by hand far more often than
# config.json is.
FILE_OVERRIDE_ALIASES = {"period": "schedule_len", "cycle": "schedule_len"}

# A ``key = value`` line in a text schedule. Slot lines are separated by ``;``
# (cycle modes) or ``,`` (sun mode) and never contain ``=``, so the two shapes
# cannot be confused for one another.
_DIRECTIVE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


@dataclass
class Entry:
    """One line of the schedule."""

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
            # A slot written for the wrong mode is the commonest way a schedule
            # comes out empty, and the camera then starts in setup mode. Say so
            # here: "expected a number, got None" left the operator to work out
            # both which field was missing and why it was needed.
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
    """Parse a legacy asi-camera ``schedule.txt``.

    Returns ``(entries, errors, overrides)``.

    ``sun``   mode: ``filter,exposure,seconds``       e.g. ``1,55,0:30``
    cycle modes: ``delta;filter;exposure;binning;gain;readout``

    In a cycle line only the first three fields are required; ``binning`` falls
    back to the camera's, and ``gain`` and ``readout`` are left unset — the slot
    then takes the camera-wide gain and the schedule-wide dead time, as a
    four-field station file always has. A field may also be left empty to skip
    it, so ``0;3;25;1;;5`` names a readout without naming a gain::

        100;3;25;1              # gain and readout from the camera
        1428;3;7;4;3;5          # a converted imagerd_rt slot, in full

    A line may instead be ``key = value``, which sets one of
    :data:`FILE_OVERRIDE_KEYS` for the whole programme — the same globals the
    JSON schedule format carries. This is what lets a station state the length
    of its cycle where the cycle itself is written::

        period = 1440
        0;1;55;1
        100;3;25;1

    Without it the period could only be set in config.json, and every change of
    schedule meant editing two files in step — which is exactly how a schedule
    and its period drift apart.
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
                # Only name the optional fields when the line does. A key left
                # out here is what makes ``entries_from_config`` fall back to
                # the camera-wide gain and the schedule-wide dead time.
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

    The file is either a bare list of slots or an object whose ``slots`` (or
    ``schedule``) key holds them. In the object form its other keys are the
    globals the imagerd_rt schedule.conf carried — ``mode``, ``sun_max_angle``,
    ``schedule_len`` and friends — and they are handed back so the caller can
    let the file speak for the whole programme, as it did in the old station.
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
    """Load a schedule file. Returns ``(entries, errors, overrides)``.

    JSON is detected by extension or by the first non-blank character, so a
    converted schedule can be dropped in beside a legacy ``schedule.txt``
    without a second config key to say which is which.
    """
    with open(path, "r") as fh:
        text = fh.read()
    if str(path).lower().endswith(".json") or text.lstrip()[:1] in ("{", "["):
        return parse_schedule_json(text, mode, default_binning)
    return parse_schedule_text(text, mode, default_binning)


def schedule_to_text(entries, mode, period=None):
    """Render entries back into the legacy file format (used by the setup tools).

    ``period`` is written as a ``period =`` header when given, so a file that
    stated its own cycle length still states it after a round trip through an
    editor. Passing ``None`` leaves the period to be derived from the slots, as
    it always was.
    """
    lines = []
    if period:
        lines.append(f"period = {float(period):g}")
    if mode in CYCLE_MODES:
        # The long line only appears once something needs it, so a schedule that
        # never named a gain still round-trips to the four fields a station file
        # has always had.
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


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
def unique_dark_settings(entries):
    """One ``(exposure, binning, gain, readout)`` tuple per unique exposure.

    Dark frames are keyed by exposure only, in schedule order; the remaining
    settings come from the first cycle that uses that exposure. A dark must
    match the light frames it will be subtracted from, so the gain travels with
    the exposure rather than being left at whatever the last slot happened to
    set.
    """
    combos, seen = [], set()
    for entry in entries:
        if entry.exposure not in seen:
            seen.add(entry.exposure)
            combos.append((entry.exposure, entry.binning or 1, entry.gain,
                           entry.readout))
    return combos


def unique_exposures(entries):
    """``(exposure, binning)`` per unique exposure — the settings darks reuse."""
    return [(exposure, binning)
            for exposure, binning, _gain, _readout in unique_dark_settings(entries)]


def cycle_period(entries, dead_time):
    """General-cycle length = last cycle's delta + its exposure + its readout.

    The readout is the entry's own when the schedule gives one (imagerd_rt's
    per-slot ``prep_time``), otherwise the schedule-wide dead time. For the Tory
    programme this reproduces the original ``schedule_len`` exactly:
    1428 + 7 + 5 = 1440 s.
    """
    if not entries:
        return float(dead_time)
    last = max(entries, key=lambda e: e.delta or 0.0)
    readout = last.readout if last.readout is not None else dead_time
    return (last.delta or 0.0) + last.exposure + readout


def period_mismatch(mode, entries, schedule_len, dead_time, what="the schedule"):
    """Say it when a stated cycle period disagrees with the slots it describes.

    Returns the sentence to warn with, or ``""`` when there is nothing to say.

    A period is stated in one place (``period = 1440`` in the schedule file, or
    ``schedule_len`` in config.json) and described in another — the slots — and
    nothing used to compare the two. It should: a station that set the period to
    twice what its slots close at spent the second half of every cycle with no
    slot in it, and the driver's answer to "when is the next frame" was then the
    first slot of the *following* cycle. Started less than one such period before
    ``t_start``, that answer is ``t_start`` itself, so the camera sits doing
    nothing until the appointed hour. The arithmetic was right and the number was
    wrong, and from the outside there was no way to tell which.

    Not every difference is worth a word. A cycle may close a little after its
    last slot, and a programme of one frame per cycle is nothing but tail. What
    marks the mistake is a gap at the end of the cycle longer than any gap
    *inside* it: room for another slot, and no slot in it. Below that the tail is
    ordinary and this keeps quiet.

    The period is never corrected here, only reported. Which of the two numbers
    is the wrong one is the operator's to say.
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
    """Seconds from this slot to the next one in the cycle, wrapping at the end.

    Entries are ordered by delta, the way :func:`next_cycle_slot` orders them.
    The last slot's gap runs into the next iteration, so it is measured to the
    first slot of that one rather than to the end of the cycle.
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
    """How long an exposure at this slot may actually run.

    The gap to the next slot, less the time the frame needs after the shutter
    closes — the entry's own readout when it names one, otherwise the
    schedule-wide dead time. Nothing in a well-formed schedule exceeds this; it
    is the guard that stops an automatically chosen exposure from pushing the
    following slot late.
    """
    readout = entry.readout if entry.readout is not None else dead_time
    return max(slot_gap(entries, period, entry) - float(readout), 0.0)


def _as_utc_time(t_start):
    """A local time of day as ``"HH:MM:SS"`` UTC, or None."""
    if t_start is None:
        return None
    local = datetime.combine(datetime.now().date(), t_start).astimezone()
    return local.astimezone(timezone.utc).strftime("%H:%M:%S")


def schedule_snapshot(sched):
    """Describe an observing programme for observers, as plain JSON types.

    Which cycle a frame belongs to is written nowhere — not in the file name,
    not in the FITS header — so an archive can only be grouped into cycles by
    replaying this phase reference and period against the frame times. Published
    through ``CameraService.set_schedule`` and consumed by ``cycles.py``.

    Duck-typed on purpose: the japan and asi ``ScheduleCfg`` classes are
    separate but agree on every field read here.
    """
    if sched is None:
        return {}
    t_start = getattr(sched, "t_start", None)
    entries = list(getattr(sched, "entries", None) or [])
    return {
        "mode": getattr(sched, "mode", ""),
        "t_start": t_start.strftime("%H:%M:%S") if t_start else None,
        # The same instant in UTC. Frame names are UTC on every camera that
        # writes them, and an observer never learns the station's offset — so
        # the anchor has to travel in the clock the names are in, and only the
        # camera can convert it.
        "t_start_utc": _as_utc_time(t_start),
        "period": float(getattr(sched, "period", 0.0) or 0.0),
        "dead_time": float(getattr(sched, "dead_time", 0.0) or 0.0),
        "entries": [{"filter": e.filter, "delta": e.delta,
                     "exposure": e.exposure, "seconds": list(e.seconds or [])}
                    for e in entries],
    }


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


def cycle_anchor(t_start, now):
    """The occurrence of ``t_start`` that belongs to the night ``now`` is in.

    ``t_start`` is a time of day, so it happens once every 24 h and something has
    to choose which one. Combining it with today's date — what both drivers used
    to do — is right for the ordinary case of starting up in the evening before
    the programme begins, and wrong for a restart after midnight: a station
    coming back at 00:30 under ``t_start = 22:00`` would read the anchor as
    22:00 *tonight*, twenty-one and a half hours away. It would then believe it
    had started early, shoot the opening darks it should have skipped, and — on
    any period that does not divide into a day — put every slot of the night on
    the wrong phase.

    So the anchor is simply the nearest occurrence, in either direction: within
    twelve hours ahead it is tonight's, beyond that it was last night's. Both
    readings describe the same repeating cycle; this one picks the night the
    operator is actually standing in.
    """
    anchor = datetime.combine(now.date(), t_start)
    if anchor - now > timedelta(hours=12):
        return anchor - timedelta(days=1)
    if now - anchor > timedelta(hours=12):
        return anchor + timedelta(days=1)
    return anchor


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


def next_minute_boundary(t):
    """The first whole minute at or after ``t``.

    imagerd_rt armed its cycle timer only on a whole UTC minute
    (``imagerd_rt.c:725-736``); the cycle modes anchor themselves the same way so
    that slot times stay readable and comparable between nights.
    """
    truncated = t.replace(second=0, microsecond=0)
    return truncated if truncated == t else truncated + timedelta(minutes=1)


def estimate_cycle_dark_duration(entries, dark_frames, dead_time):
    """Worst-case seconds to shoot the cycle-mode darks, +10 % and a 30 s buffer.

    Cycle-mode darks run back to back — there is no second-of-the-minute to wait
    for — so this is simply the exposures plus their readouts, unlike
    :func:`estimate_dark_duration`, which has to allow for that wait.
    """
    total = 0.0
    for exposure, _binning, _gain, readout in unique_dark_settings(entries):
        gap = readout if readout is not None else dead_time
        total += dark_frames * (exposure + gap)
    return total * 1.1 + 30.0


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
