"""Grouping an archive listing the way the night was actually measured.

``frame_archive`` answers "which files are there"; this answers "which day, which
cycle, which filter". It is what turns the viewer's one-thousand-file ribbon into
a tree an observer can walk.

Deliberately pure — no Qt, no numpy, no camera drivers, nothing but the standard
library — for the same reason as ``frame_archive``: it has to run on an observer's
machine, and the cycle arithmetic is the subtle part, so it has to be testable
without a display or a camera.

The hard part is the cycle. The driver knows exactly which iteration of the
general cycle a frame belongs to (``cameras/common/schedule.next_cycle_slot``)
but never writes it down: not in the filename (``cameras/japan/paths.frame_name``
carries only the stamp and the wheel position), not in the FITS header
(``cameras/common/fits`` writes ``FILTER``, not a cycle), and not in
``/api/status``, which publishes ``mode`` and ``next_slot`` but neither
``t_start`` nor the period. So the period is *recovered* here from the one thing
the archive does record — when each filter came round again — and the operator can
override it when the guess is wrong.

The other thing that is not what it looks like is the day. A measuring session is
a *night*, and a night is not a calendar day: the station's evening can fall on
one side of UTC midnight and its morning on the other, so one night's frames
carry two dates. Grouping by date would then put the beginning of a run in one
list and its end in another. Sessions are therefore cut by the gap between
frames, not by midnight.

Provides:
  - frame_timestamp()      — capture time of a listing entry, and where it came from
  - frame_day()            — the ``YYYY-MM-DD`` a frame belongs to
  - group_by_session()     — measuring sessions, newest first (every camera)
  - parse_japan_frame()    — stamp / wheel position / dark flag off a japan name
  - detect_cycle_period()  — the general cycle's period, guessed from the frames
  - group_japan_cycles()   — sessions -> cycles -> filters (japan, ``time`` mode)
"""
import math
import os
import re

from collections import Counter, defaultdict
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
# Both archives stamp UTC and both put it first, but they punctuate it
# differently: japan writes ``20260729T140530_3.fits``
# (``cameras/japan/paths.py``), the imagerd_rt layout the ASI kept writes
# ``20260729_140530_TOR_ASI_5577_055000ms.fits`` (``cameras/asi/paths.py``).
NAME_TIME_LENGTH = 15
NAME_TIME_FORMATS = ("%Y%m%dT%H%M%S", "%Y%m%d_%H%M%S")

# Shown for frames whose day cannot be established at all — no usable name and no
# modification time. Sorted last, never silently folded into a real day.
UNKNOWN_DAY = "unknown date"

# What separates two measuring sessions. Longer than any pause a schedule leaves
# inside a night — a ``time``-mode general cycle is minutes long, and ``sun`` mode
# fires continuously while the sun is below its limit — and far shorter than the
# daylight between two nights. This is what keeps a run that starts in the evening
# and ends after midnight in one group instead of two.
SESSION_GAP = timedelta(hours=6)

JAPAN_NAME_RE = re.compile(r"^(\d{8}T\d{6})_(\d+)(_bg)?$", re.IGNORECASE)

# Wheel positions of the Hamamatsu instrument, as the station's schedule file
# documents them (see the comment at ``cameras/common/schedule.py:36``). The
# japan driver has no filter table of its own — it writes the bare position into
# ``FILTER`` — so the names live here, and only ever for a label.
JAPAN_FILTER_NAMES = {
    0: "home / unknown",
    1: "557.7 nm",
    2: "630.0 nm",
    3: "OH (broadband)",
    4: "840.0 nm",
    5: "846.5 nm",
    6: "857.0 nm",
}

# Period detection thresholds. A night has to show at least a few cycles before a
# period means anything, and the cycles it implies have to be both uniform and
# dense for most of the night, or the answer is "no cycle here" — which is the
# truth for a ``sun``-mode night, where frames follow seconds-within-a-minute.
MIN_LIGHTS_FOR_PERIOD = 4
MIN_CYCLES = 3
MIN_SCORE = 0.6


def frame_timestamp(name, mtime=None):
    """``(timestamp, from_name)`` for one listing entry.

    The filename is the authority whenever it follows the capture convention —
    the same rule the camera uses to decide which day a frame belongs to
    (``frame_archive._matches_day``), because a frame captured last night but
    copied today must not group under today. That stamp is **UTC**.

    Everything else falls back to ``mtime``, which is a **local** time on the
    machine reading it. The flag says which of the two was used, so a caller can
    label the two differently instead of presenting one as the other.
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
    """:func:`frame_timestamp` for a ``{name, mtime, ...}`` listing entry."""
    frame = frame or {}
    return frame_timestamp(frame.get("name"), frame.get("mtime"))


def frame_day(name, mtime=None):
    """The ``YYYY-MM-DD`` a frame belongs to, matching ``frame_archive.list_dates``."""
    stamp, _ = frame_timestamp(name, mtime)
    return stamp.strftime("%Y-%m-%d") if stamp else UNKNOWN_DAY


def session_label(start, end):
    """``2026-07-29``, or ``2026-07-29 → 2026-07-30`` for a night across midnight."""
    if start is None or end is None:
        return UNKNOWN_DAY
    if start.date() == end.date():
        return f"{start:%Y-%m-%d}"
    return f"{start:%Y-%m-%d} → {end:%Y-%m-%d}"


def group_by_session(frames, gap=SESSION_GAP):
    """Cut a listing into measuring sessions, newest session first.

    Returns one dict per session::

        {"label": "2026-07-29 → 2026-07-30", "start": dt, "end": dt,
         "dates": ["2026-07-29", "2026-07-30"], "frames": [...]}

    A session ends where the archive falls quiet for longer than ``gap``, which is
    what keeps a run that begins in the evening and ends in the morning together
    even though its frames carry two dates. Works for every camera: the capture
    time comes from the filename where there is one and from ``mtime`` otherwise.

    Frames whose time cannot be established at all go into one last group rather
    than being attached to a session they may not belong to.
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


# ---------------------------------------------------------------------------
# japan: cycles and filters
# ---------------------------------------------------------------------------
def japan_filter_label(position):
    """``Filter 3 — OH (broadband)``, or the bare position for an unknown one."""
    name = JAPAN_FILTER_NAMES.get(position)
    return f"Filter {position} — {name}" if name else f"Filter {position}"


def parse_japan_frame(frame):
    """Read the capture time, wheel position and dark flag off a japan name.

    ``None`` for anything that is not one — a hand-copied file, an ASI frame in a
    shared directory, a screenshot — so a caller can show those unchanged rather
    than invent a cycle for them.
    """
    name = frame.get("name") if isinstance(frame, dict) else frame
    stem = os.path.splitext(str(name or ""))[0]
    match = JAPAN_NAME_RE.match(stem)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return {"frame": frame, "time": stamp, "filter": int(match.group(2)),
            "dark": bool(match.group(3))}


def cycle_score(lights, period):
    """How well ``period`` explains ``lights`` as a repeating cycle, 0..1.

    Two things have to hold, and the score is their product because either alone
    is easy to satisfy by accident:

    *Uniform* — a cycle is identified by the multiset of filters in it, since that
    is what a schedule repeats, and most cycles should carry the same one. The
    first and last are allowed to be incomplete, a run rarely starting or
    stopping on a boundary, but only as a *subset* of the usual cycle.

    *Dense* — nearly every cycle the night spans should actually contain frames.
    Without this a night shot at one filter would score perfectly on any period
    short enough to give each frame a cycle of its own.
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
    """The general cycle's period in seconds, guessed from the frames alone.

    Candidates are the gaps between successive frames of the *same* filter: for a
    filter exposed once per cycle that gap **is** the period. Each candidate is
    then scored by :func:`cycle_score` and the best wins, ties going to the
    shortest — a multiple of the true period splits the night just as uniformly,
    and would hide half the cycles.

    ``None`` when nothing scores well enough. That is the honest answer for a
    ``sun``-mode night, and it leaves the caller free to fall back to a plainer
    grouping instead of drawing cycles that do not exist.
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
    """Where cycle numbering starts: a given time, else the night's first frame."""
    if anchor is None:
        return first_light
    if isinstance(anchor, datetime):
        return anchor
    start = datetime.combine(first_light.date(), anchor)
    # A session whose first frame is after midnight belongs to the evening before,
    # so an anchor later in the day than that frame is the previous day's.
    return start - timedelta(days=1) if start > first_light else start


def _by_filter(entries):
    """``[{filter, frames}, ...]`` by wheel position, newest frame first."""
    grouped = defaultdict(list)
    for entry in entries:
        grouped[entry["filter"]].append(entry)
    return [{"filter": position,
             "frames": [e["frame"] for e in sorted(grouped[position],
                                                   key=lambda e: e["time"],
                                                   reverse=True)]}
            for position in sorted(grouped)]


def group_japan_cycles(frames, period=None, anchor=None):
    """Sessions -> cycles -> filters for a japan archive.

    Returns one dict per measuring session (see :func:`group_by_session`), newest
    first, each carrying that session's fields plus::

        {"period": 160.0, "period_auto": True, "darks": [...], "unparsed": [...],
         "filters": [...],                      # only when there is no period
         "cycles": [{"index": 7, "start": dt, "first": dt, "last": dt,
                     "count": 2, "filters": [{"filter": 3, "frames": [...]}]}]}

    Cycles are counted within the session, so a night that runs through midnight
    keeps one rising sequence of cycle numbers instead of restarting at the date
    boundary.

    ``period`` and ``anchor`` override the automatic detection. ``anchor`` is a
    ``datetime.time`` (combined with the session's first evening) or a full
    ``datetime``, and it is read as **UTC**: japan filenames are UTC while the
    camera's ``t_start`` is the station's local time, and the viewer never learns
    the station's offset — so the operator gives the anchor in the same clock the
    names are in.

    Cycle numbering is ``floor((t - anchor) / period) + 1``, the driver's own
    arithmetic (``cameras/common/schedule.next_cycle_slot``) shifted so the first
    cycle is 1. Numbers are therefore not contiguous: a cycle that produced no
    frames leaves a gap, which is information, and with an explicit anchor later
    than the first frame the early cycles are numbered zero or below.
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
            # No cycle to be found: still worth grouping the night by filter,
            # which is the other question an observer asks of these frames.
            session["filters"] = _by_filter(lights)
    return sessions
