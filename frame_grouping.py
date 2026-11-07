"""Groups an archive listing into sessions, cycles and filters.

Standard library only: it runs on an observer's machine, and the cycle arithmetic must
be testable without a display or a camera.

Neither the filename nor the FITS header records a frame's cycle, so the period is
recovered from when each filter came round again; the operator can override it.
A session is a night, not a calendar day: one night's frames can carry two UTC dates,
so sessions are cut by the gap between frames, not by midnight.
"""
import math
import os
import re

from collections import Counter, defaultdict
from datetime import datetime, timedelta

# Both stamp UTC first but punctuate it differently: japan writes
# ``20260729T140530_3.fits``, the imagerd_rt layout
# ``20260729_140530_TOR_ASI_5577_055000ms.fits``.
NAME_TIME_LENGTH = 15
NAME_TIME_FORMATS = ("%Y%m%dT%H%M%S", "%Y%m%d_%H%M%S")

# For frames with no usable name and no mtime. Sorted last, never folded into a real
# day.
UNKNOWN_DAY = "unknown date"

# Longer than any pause inside a night (a ``time``-mode cycle is minutes long, ``sun``
# mode fires continuously), far shorter than the daylight between two nights.
SESSION_GAP = timedelta(hours=6)

JAPAN_NAME_RE = re.compile(r"^(\d{8}T\d{6})_(\d+)(_bg)?$", re.IGNORECASE)

# ``YYYYMMDD_hhmmss_SITE_DEV_WAVE_EEEEEEms[_DARK][_pf]``, written by japan in
# ``sun_cycle`` mode. The wavelength tag may hold underscores (``OH__``), so it is
# matched lazily up to the exposure field.
ARCHIVE_NAME_RE = re.compile(
    r"^(\d{8}_\d{6})_([^_]+)_([^_]+)_(.+?)_(\d{6})ms(_DARK)?(_pf)?$",
    re.IGNORECASE)

# Wavelength tag -> wheel position. The Tory wheel, as in
# ``cameras/common/filters.DEFAULT_FILTERS``: spelled out to stay free of the camera
# packages; a test keeps the two in step.
ARCHIVE_WAVELENGTH_SLOTS = {
    "5577": 1,
    "6300": 2,
    "OH__": 3,
    "8400": 4,
    "8465": 5,
    "8570": 6,
}

# Hamamatsu wheel positions, from the station's schedule file. Labels only: the japan
# driver writes the bare position into ``FILTER``.
JAPAN_FILTER_NAMES = {
    0: "home / unknown",
    1: "557.7 nm",
    2: "630.0 nm",
    3: "OH (broadband)",
    4: "840.0 nm",
    5: "846.5 nm",
    6: "857.0 nm",
}

# A night must show a few uniform, dense cycles, or the answer is "no cycle here":
# the truth for a ``sun``-mode night, where frames follow seconds-within-a-minute.
MIN_LIGHTS_FOR_PERIOD = 4
MIN_CYCLES = 3
MIN_SCORE = 0.6


def frame_timestamp(name, mtime=None):
    """``(timestamp, from_name)``.

    The name's stamp is UTC and is the authority, as in ``frame_archive._matches_day``;
    the ``mtime`` fallback is local to the reading machine, so a caller can label the
    two apart.
    """
    stem = str(name or "")
    if len(stem) >= NAME_TIME_LENGTH and stem[:8].isdigit():
        head = stem[:NAME_TIME_LENGTH]
        for fmt in NAME_TIME_FORMATS:
            try:
                return datetime.strptime(head, fmt), True
            except ValueError:
                continue
    if mtime is None:
        return None, False
    try:
        return datetime.fromtimestamp(float(mtime)), False
    except (OverflowError, OSError, ValueError, TypeError):
        return None, False


def entry_timestamp(frame):
    frame = frame or {}
    return frame_timestamp(frame.get("name"), frame.get("mtime"))


def frame_day(name, mtime=None):
    """Must match ``frame_archive.list_dates``."""
    stamp, _ = frame_timestamp(name, mtime)
    return stamp.strftime("%Y-%m-%d") if stamp else UNKNOWN_DAY


def session_label(start, end):
    if start is None or end is None:
        return UNKNOWN_DAY
    if start.date() == end.date():
        return f"{start:%Y-%m-%d}"
    return f"{start:%Y-%m-%d} → {end:%Y-%m-%d}"


def group_by_session(frames, gap=SESSION_GAP):
    """Frames with no known time go into one last group, not a session they may not fit.
    """
    dated, undated = [], []
    for frame in frames or []:
        stamp, _ = entry_timestamp(frame)
        if stamp is None:
            undated.append(frame)
        else:
            dated.append((stamp, frame))
    dated.sort(key=lambda pair: pair[0])

    sessions = []
    for stamp, frame in dated:
        if sessions and stamp - sessions[-1]["end"] <= gap:
            sessions[-1]["end"] = stamp
            sessions[-1]["frames"].append(frame)
        else:
            sessions.append({"start": stamp, "end": stamp, "frames": [frame]})

    for session in sessions:
        session["frames"].reverse()          # newest first, as everywhere else
        session["label"] = session_label(session["start"], session["end"])
        session["dates"] = sorted({frame_day(f.get("name"), f.get("mtime"))
                                   for f in session["frames"]})
    sessions.reverse()
    if undated:
        sessions.append({"start": None, "end": None, "label": UNKNOWN_DAY,
                         "dates": [], "frames": undated})
    return sessions


def japan_filter_label(position):
    name = JAPAN_FILTER_NAMES.get(position)
    return f"Filter {position} — {name}" if name else f"Filter {position}"


def parse_japan_frame(frame):
    """Reads both japan namings; a wavelength tag the Tory wheel lacks gives position 0.

    ``None`` for anything else, such as a hand-copied file, so a caller can show it
    unchanged rather than invent a cycle for it.
    """
    name = frame.get("name") if isinstance(frame, dict) else frame
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    match = JAPAN_NAME_RE.match(stem)
    if not match:
        return _parse_archive_frame(frame, stem)
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return {"frame": frame, "time": stamp, "filter": int(match.group(2)),
            "dark": bool(match.group(3))}


def _parse_archive_frame(frame, stem):
    match = ARCHIVE_NAME_RE.match(stem)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return {"frame": frame, "time": stamp,
            "filter": ARCHIVE_WAVELENGTH_SLOTS.get(match.group(4).upper(), 0),
            "dark": bool(match.group(6))}


def cycle_score(lights, period):
    """A product of two conditions, since either alone is easy to satisfy by accident.

    Uniform: most cycles carry the same multiset of filters. The first and last may hold
    a subset of it: a run rarely starts or stops on a boundary.

    Dense: nearly every cycle spanned has frames. Otherwise a one-filter night scores
    perfectly on any period short enough to give each frame its own cycle.
    """
    if not lights or period <= 0:
        return 0.0
    anchor = lights[0]["time"]
    buckets = defaultdict(Counter)
    for entry in lights:
        index = math.floor((entry["time"] - anchor).total_seconds() / period)
        buckets[index][entry["filter"]] += 1
    if len(buckets) < MIN_CYCLES:
        return 0.0

    keys = sorted(buckets)
    signatures = {k: tuple(sorted(buckets[k].elements())) for k in keys}
    modal = Counter(signatures.values()).most_common(1)[0][0]
    modal_counts = Counter(modal)
    good = 0
    for key in keys:
        if signatures[key] == modal:
            good += 1
        elif key in (keys[0], keys[-1]) and not (buckets[key] - modal_counts):
            good += 1
    uniform = good / len(keys)

    span = math.floor((lights[-1]["time"] - anchor).total_seconds() / period) + 1
    dense = len(keys) / span if span > 0 else 0.0
    return uniform * dense


def detect_cycle_period(parsed):
    """Candidates are gaps between frames of one filter: exposed once per cycle, that
    gap is the period.

    Ties go to the shortest, since a multiple of the true period splits the night just
    as uniformly and hides cycles. ``None`` when nothing scores well, as on a
    ``sun``-mode night.
    """
    lights = sorted((e for e in parsed or [] if not e["dark"]),
                    key=lambda e: e["time"])
    if len(lights) < MIN_LIGHTS_FOR_PERIOD:
        return None

    by_filter = defaultdict(list)
    for entry in lights:
        by_filter[entry["filter"]].append(entry["time"])

    candidates = set()
    for times in by_filter.values():
        for first, second in zip(times, times[1:]):
            gap = round((second - first).total_seconds())
            if gap > 0:
                candidates.add(float(gap))

    best, best_score = None, 0.0
    for period in sorted(candidates):
        score = cycle_score(lights, period)
        if score > best_score + 1e-9:
            best, best_score = period, score
    return best if best_score >= MIN_SCORE else None


def _anchor_for(first_light, anchor):
    if anchor is None:
        return first_light
    if isinstance(anchor, datetime):
        return anchor
    start = datetime.combine(first_light.date(), anchor)
    # A session whose first frame is after midnight belongs to the evening before,
    # so an anchor later in the day than that frame is the previous day's.
    return start - timedelta(days=1) if start > first_light else start


def _by_filter(entries):
    grouped = defaultdict(list)
    for entry in entries:
        grouped[entry["filter"]].append(entry)
    return [{"filter": position,
             "frames": [e["frame"] for e in sorted(grouped[position],
                                                   key=lambda e: e["time"],
                                                   reverse=True)]}
            for position in sorted(grouped)]


def group_japan_cycles(frames, period=None, anchor=None):
    """Cycles are counted within the session, so a night through midnight keeps one
    rising sequence. ``filters`` appears on a session only when it has no period.

    ``anchor`` is a ``datetime.time`` or a ``datetime``, read as UTC: filenames are UTC,
    the camera's ``t_start`` is station-local, and the viewer never learns the offset.

    Numbering is ``floor((t - anchor) / period) + 1``, the driver's arithmetic
    (``cameras/common/schedule.next_cycle_slot``) shifted so the first cycle is 1. A
    cycle with no frames leaves a gap, and an anchor after the first frame gives numbers
    of zero or below.
    """
    sessions = group_by_session(frames)
    for session in sessions:
        parsed, unparsed = [], []
        for frame in session["frames"]:
            entry = parse_japan_frame(frame)
            if entry:
                parsed.append(entry)
            else:
                unparsed.append(frame)
        darks = [e["frame"] for e in parsed if e["dark"]]
        lights = sorted((e for e in parsed if not e["dark"]),
                        key=lambda e: e["time"])

        chosen = float(period) if period else detect_cycle_period(parsed)
        session.update({"period": chosen, "period_auto": not period,
                        "cycles": [], "darks": darks, "unparsed": unparsed})

        if chosen and lights:
            start = _anchor_for(lights[0]["time"], anchor)
            buckets = defaultdict(list)
            for entry in lights:
                index = math.floor(
                    (entry["time"] - start).total_seconds() / chosen)
                buckets[index].append(entry)
            for index in sorted(buckets, reverse=True):
                entries = buckets[index]
                session["cycles"].append({
                    "index": index + 1,
                    "start": start + timedelta(seconds=index * chosen),
                    "first": entries[0]["time"],
                    "last": entries[-1]["time"],
                    "count": len(entries),
                    "filters": _by_filter(entries),
                })
        else:
            session["filters"] = _by_filter(lights)
    return sessions
