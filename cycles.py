"""The per-filter difference frame of one cycle.

    A = the first frame of this cycle for this filter
    B = the last frame of the *previous* cycle for the same filter
    D = the last frame of this cycle for the same filter

    k0 = clamp(1 - (t_A - t_B) / T, 0, 1)
    k1 = clamp(1 - (t_D - t_A) / T, 0, 1)

    result = A - (B*k0 + D*k1) / 2

``T`` is an operator-chosen window. With B or D missing there is no result: half a
difference would be worse than none.

Choosing frames needs only names and timestamps, so the viewer does it on the standard
library. The arithmetic runs on the camera, where the pixels are: three japan frames
are ~24 MB against ~200 KB for the answer. Only :func:`composite_image` imports numpy,
and only ``frame_server`` calls it.
"""
from __future__ import annotations

def _filter_group(cycle, filter_num):
    for group in cycle.get("filters", []):
        if int(group["filter"]) == int(filter_num):
            return group["frames"]
    return []


def pick_composite_frames(session, cycle_index, filter_num):
    """``(first, previous_last, last, reason)`` for one filter of one cycle.

    On refusal the frames are ``None`` and ``reason`` is what the viewer shows the
    operator.
    """
    cycles = list(session.get("cycles") or [])
    if not cycles:
        return None, None, None, "this night has no cycles to compare"

    by_index = {int(c["index"]): c for c in cycles}
    index = int(cycle_index)
    cycle = by_index.get(index)
    if cycle is None:
        return None, None, None, f"cycle {index} is not in this archive"

    here = _filter_group(cycle, filter_num)
    if not here:
        return None, None, None, f"this filter was not shot in cycle {index}"

    # Newest first, so the earliest frame of the cycle is the last of the list.
    first, last = here[-1], here[0]

    # The newest frame so far is not the final one: a later cycle having started is the
    # only evidence from the archive that this one is over.
    if index >= max(by_index):
        return None, None, None, ("this cycle is still being shot — its last "
                                  "frame for this filter is not in yet")

    previous = by_index.get(index - 1)
    if previous is None:
        # Not "the cycle before it in the list": a gap in the archive must not
        # silently promote a much older cycle into "previous".
        return None, None, None, ("no previous cycle in this archive to "
                                  "subtract from")
    there = _filter_group(previous, filter_num)
    if not there:
        return None, None, None, ("this filter was not shot in the previous "
                                  "cycle")

    return first, there[0], last, ""


def composite_weights(t_first, t_previous, t_last, window):
    window = float(window or 0.0)
    if window <= 0:
        raise ValueError("the weighting window must be a positive number of seconds")

    def _weight(seconds):
        return max(0.0, min(1.0, 1.0 - float(seconds) / window))

    return (_weight((t_first - t_previous).total_seconds()),
            _weight((t_last - t_first).total_seconds()))


def composite_image(first, previous_last, last, k0, k1):
    """Float: the result is signed, and rounding it into the sensor's unsigned range
    would lose the half of the picture the operator is looking for.
    """
    import numpy as np

    a = np.asarray(first, dtype=np.float32)
    b = np.asarray(previous_last, dtype=np.float32)
    d = np.asarray(last, dtype=np.float32)
    if not (a.shape == b.shape == d.shape):
        raise ValueError(
            f"frames differ in shape ({a.shape}, {b.shape}, {d.shape}) — most "
            f"likely the binning changed between cycles")
    return a - (b * float(k0) + d * float(k1)) / 2.0
