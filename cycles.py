"""
Reconstructing observing cycles from a flat archive, and the per-filter
difference frame built from three frames around one of them.

Nothing in a capture records which cycle it belongs to. The japan archive is a
flat directory of ``YYYYmmddTHHMMSS_<filter>[_bg].fits`` (see
``cameras/japan/paths.py``), the FITS header carries ``FILTER`` and ``DATE-OBS``
but no iteration number, and the console's "cycle 12" is a runtime figure that
is never written down. So a cycle can only be recovered by replaying the
schedule the camera was following — its phase reference and its period — against
the frame times. That schedule now travels with the camera:
``CameraService.set_schedule`` publishes it and ``/api/info`` serves it.

The difference frame, per filter and per cycle, is:

    A = the first frame of this cycle for this filter
    B = the last frame of the *previous* cycle for the same filter
    D = the last frame of this cycle for the same filter

    k0 = clamp(1 - (t_A - t_B) / T, 0, 1)      # the closer B is to A, the
    k1 = clamp(1 - (t_D - t_A) / T, 0, 1)      # closer its weight is to 1

    result = A - (B*k0 + D*k1) / 2

``T`` is an operator-chosen window; the cycle period is the natural default. If
either neighbour is missing — the first cycle of a night has no B, a cycle still
being shot has no final D — there is no result at all, by design: half a
difference would be worse than none.

Two rules keep this module honest:

* It is **pure**: file names, timestamps and numpy arrays in, numbers and arrays
  out. No HTTP, no camera drivers, no disk. That is what makes the arithmetic —
  the part that is easy to get subtly wrong — testable, and it is why the module
  can be imported on an observer laptop as freely as ``frame_archive``.
* Time bases are never mixed. Frame *names* are UTC (the japan driver made that
  deliberate); ``t_start`` is the station's local wall clock, because that is
  what an operator sets. Everything here converts to local before it indexes a
  cycle, and says so at each boundary.
"""
from __future__ import annotations

import math
import os
import re

from datetime import datetime, time as dtime, timedelta, timezone

import numpy as np

# ``YYYYmmddTHHMMSS_<filter>[_bg].fits`` — the name japan-camera has always
# written. The extension list matches frame_archive.FITS_EXTENSIONS.
_JAPAN_NAME = re.compile(
    r"^(?P<stamp>\d{8}T\d{6})_(?P<filter>\d+)(?P<dark>_bg)?\.(?:fits|fit|fts)$",
    re.IGNORECASE)

# Cycle length used when a camera reports a mode whose entries fire on seconds of
# the minute rather than on offsets in a general cycle: one pass over the
# schedule is then one minute (``japan_driver._run_sun_mode``).
MINUTE = 60.0

# Modes whose cycle is a general cycle phase-locked to ``t_start``.
CYCLE_MODES = ("time", "sun_cycle")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def parse_japan_name(name):
    """``(utc_datetime, filter_num, is_dark)`` for a japan frame, else ``None``.

    The returned datetime is timezone-aware UTC: the stamp in the name is UTC
    even though the station's schedule is local, and leaving it naive is how the
    two get mixed up downstream.
    """
    match = _JAPAN_NAME.match(os.path.basename(str(name or "")))
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return (stamp.replace(tzinfo=timezone.utc),
            int(match.group("filter")),
            bool(match.group("dark")))


def _local(moment):
    """A UTC instant as the station's local wall clock, still aware."""
    return moment.astimezone()


def _parse_t_start(text):
    """``"HH:MM:SS"`` / ``"HH:MM"`` from a published schedule, else ``None``."""
    if not text:
        return None
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(text), pattern).time()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------
def cycle_bounds(moment, schedule):
    """``(start, period)`` of the cycle containing the local instant ``moment``.

    ``start`` is aware local, ``period`` is seconds. Returns ``(None, 0.0)`` when
    the schedule does not describe cycles at all.

    The anchor is ``t_start`` on the frame's *own night*: a frame before that
    hour belongs to the previous evening's run, which is what the driver does by
    combining ``t_start`` with the date the run began
    (``japan_driver._run_time_mode``) and never rolling it over at midnight.
    """
    mode = str((schedule or {}).get("mode") or "").strip().lower()
    if mode not in CYCLE_MODES:
        # Sun mode: entries fire on seconds of every minute, so one pass over
        # the schedule is one minute, and the minute boundary is the anchor.
        if mode:
            return moment.replace(second=0, microsecond=0), MINUTE
        return None, 0.0

    period = float(schedule.get("period") or 0.0)
    t_start = _parse_t_start(schedule.get("t_start"))
    if period <= 0 or t_start is None:
        return None, 0.0

    anchor = datetime.combine(moment.date(), t_start, tzinfo=moment.tzinfo)
    if moment < anchor:
        anchor -= timedelta(days=1)
    elapsed = (moment - anchor).total_seconds()
    return anchor + timedelta(seconds=math.floor(elapsed / period) * period), period


def group_cycles(frames, schedule, now=None):
    """Group an archive listing into cycles, and each cycle by filter.

    ``frames`` is the ``/api/frames`` listing shape — dicts with at least a
    ``name``. Darks and names that are not japan frames are dropped: a dark
    belongs to no cycle, and subtracting one would be meaningless.

    Returns ``{"mode", "period", "cycles": [...], "reason": str}``. Each cycle is

        {"id": int,              # epoch seconds of its start — a stable handle
         "index": int,           # iteration since the night's anchor, for display
         "start": iso, "end": iso,
         "complete": bool,       # a later frame exists, or its end is past
         "filters": {"1": [frame, ...], ...}}   # each list in time order

    ``reason`` is non-empty only when nothing could be grouped, so a caller can
    say *why* instead of showing an empty tree.
    """
    schedule = schedule or {}
    mode = str(schedule.get("mode") or "").strip().lower()
    result = {"mode": mode, "period": 0.0, "cycles": [], "reason": ""}
    if not mode:
        result["reason"] = ("this camera does not report a schedule, so its "
                            "frames cannot be grouped into cycles")
        return result

    parsed = []
    for frame in frames or []:
        details = parse_japan_name(frame.get("name"))
        if details is None:
            continue
        moment, filter_num, is_dark = details
        if is_dark:
            continue
        parsed.append((_local(moment), filter_num, frame))
    if not parsed:
        result["reason"] = "no japan light frames in this archive"
        return result
    parsed.sort(key=lambda item: item[0])

    now = now or datetime.now().astimezone()
    latest = parsed[-1][0]

    buckets = {}
    for moment, filter_num, frame in parsed:
        start, period = cycle_bounds(moment, schedule)
        if start is None:
            result["reason"] = ("this camera's schedule has no cycle period, so "
                                "its frames cannot be grouped into cycles")
            return result
        result["period"] = period
        cycle = buckets.get(start)
        if cycle is None:
            anchor = datetime.combine(start.date(), _parse_t_start(
                schedule.get("t_start")) or dtime(0, 0), tzinfo=start.tzinfo)
            if start < anchor:
                anchor -= timedelta(days=1)
            cycle = buckets[start] = {
                "id": int(start.timestamp()),
                "index": int(round((start - anchor).total_seconds() / period)),
                "start": start.isoformat(),
                "end": (start + timedelta(seconds=period)).isoformat(),
                "complete": False,
                "filters": {},
                "_end": start + timedelta(seconds=period),
            }
        entry = dict(frame)
        entry["time"] = moment.isoformat()
        entry["filter"] = filter_num
        cycle["filters"].setdefault(str(filter_num), []).append(entry)

    for start in sorted(buckets):
        cycle = buckets[start]
        # A cycle is finished once the clock is past its end, or once the archive
        # holds a frame from after it. Without this, the cycle being shot right
        # now would offer a "last frame" that is merely the latest so far.
        cycle["complete"] = bool(cycle["_end"] <= now or latest >= cycle["_end"])
        cycle.pop("_end")
        result["cycles"].append(cycle)
    return result


def find_cycle(grouped, cycle_id):
    """The cycle with this id, or ``None``."""
    for cycle in (grouped or {}).get("cycles", []):
        if int(cycle.get("id", 0)) == int(cycle_id):
            return cycle
    return None


def pick_composite_frames(grouped, cycle_id, filter_num):
    """``(a, b, d, reason)`` — the three frames the difference is built from.

    On any refusal the frames are ``None`` and ``reason`` says which condition
    failed, in the words an operator needs: this is the message the viewer shows
    instead of a window.
    """
    cycles = (grouped or {}).get("cycles", [])
    key = str(int(filter_num))
    index = next((i for i, c in enumerate(cycles)
                  if int(c.get("id", 0)) == int(cycle_id)), None)
    if index is None:
        return None, None, None, f"cycle {cycle_id} is not in this archive"
    cycle = cycles[index]
    here = cycle["filters"].get(key) or []
    if not here:
        return None, None, None, f"filter {key} was not shot in this cycle"
    if not cycle.get("complete"):
        return None, None, None, ("this cycle is still being shot — its last "
                                  "frame for this filter is not in yet")
    if index == 0:
        return None, None, None, ("no previous cycle in this archive to "
                                  "subtract from")

    previous = cycles[index - 1]
    # Adjacent in time, not merely the one before it in the list: a gap in the
    # archive must not silently promote a much older cycle into "previous".
    expected = datetime.fromisoformat(cycle["start"]) - timedelta(
        seconds=float(grouped.get("period") or 0.0))
    if abs((datetime.fromisoformat(previous["start"]) - expected).total_seconds()) > 1.0:
        return None, None, None, "the previous cycle is missing from this archive"
    there = previous["filters"].get(key) or []
    if not there:
        return None, None, None, f"filter {key} was not shot in the previous cycle"

    return here[0], there[-1], here[-1], ""


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------
def composite_weights(t_a, t_b, t_d, window):
    """``(k0, k1)`` for the three frame times and the chosen window ``T``.

    Each weight falls linearly from 1 at no separation to 0 at ``T`` and beyond,
    so a neighbour further away than the window contributes nothing rather than
    contributing backwards.
    """
    window = float(window or 0.0)
    if window <= 0:
        raise ValueError("the weighting window must be a positive number of seconds")

    def _weight(seconds):
        return max(0.0, min(1.0, 1.0 - float(seconds) / window))

    return (_weight((t_a - t_b).total_seconds()),
            _weight((t_d - t_a).total_seconds()))


def composite_image(first, previous_last, last, k0, k1):
    """``first - (previous_last*k0 + last*k1) / 2``, in float32.

    Float, not integer: the result is signed by nature, and rounding it back
    into the sensor's unsigned range would throw away exactly the half of the
    picture the operator is looking for.
    """
    a = np.asarray(first, dtype=np.float32)
    b = np.asarray(previous_last, dtype=np.float32)
    d = np.asarray(last, dtype=np.float32)
    if not (a.shape == b.shape == d.shape):
        raise ValueError(
            f"frames differ in shape ({a.shape}, {b.shape}, {d.shape}) — most "
            f"likely the binning changed between cycles")
    return a - (b * float(k0) + d * float(k1)) / 2.0
