"""Intensity control, driven by the mean of the frame just taken, in 16-bit ADU. Pure: no
camera, clock or config parsing.

* **preflight** — the bright-twilight stage of ``sun_cycle``: between the first
  solar-altitude setpoint and ``sun_max_angle`` each slot's exposure is chosen to hold
  a target mean.
* **split** — the overexposure guard of the general cycle: a slot brighter than a
  threshold is divided into ``N`` shorter sub-frames on the next visit.

*Pedestal.* A frame mean is ``bias + signal(exposure)``. Extrapolating it raw is wrong
in the unsafe direction at short exposures, so every formula works on ``mean - bias``.

*Saturation.* Once pixels clip the mean stops growing, so a badly overexposed frame
reads as only mildly bright. Preflight detects that from the saturated-pixel fraction
and takes the largest step down rather than believe the ratio.
"""
from __future__ import annotations

import math

import numpy as np

import intensity

# Each backend refuses a camera deeper than 16 bits and shifts a shallower one up, so
# full scale is a constant rather than something to sniff per frame.
SATURATION_ADU = float(intensity.FULL_SCALE)

# A pixel this close to full scale is clipped for our purposes.
SATURATED_AT = 0.98

# Above this fraction of clipped pixels the mean no longer tracks the exposure,
# and the controllers stop trusting their linear model.
SATURATED_LIMIT = 0.01


def frame_stats(image):
    """``(mean_adu, saturated_fraction)``; ``(None, 0.0)`` for None or an empty frame.

    float64 on purpose: ``np.mean`` on uint16 may sum in a narrow type on some
    platforms, and a megapixel of 60000-ADU samples overflows it.
    """
    if image is None:
        return None, 0.0
    arr = np.asarray(image)
    if arr.size == 0:
        return None, 0.0
    mean = float(np.mean(arr, dtype=np.float64))
    clipped = int(np.count_nonzero(arr >= SATURATED_AT * SATURATION_ADU))
    return mean, clipped / float(arr.size)


def combine_means(means):
    """The brightest sub-frame, not the average.

    The guard keeps the worst frame off the ceiling, and the sky changes monotonically
    across a twilight slot, so averaging would hide the sub-frame in trouble.
    """
    values = [m for m in means if m is not None]
    return max(values) if values else None


def slot_key(entry):
    """Hashable identity of a cycle slot.

    ``Entry`` is unhashable, and :func:`schedule.next_cycle_slot` returns objects from
    a sorted copy, so neither ``id()`` nor a config position survives. The delta names
    the slot; filter and exposure make the key readable in a log.
    """
    return (round(float(entry.delta or 0.0), 6),
            int(entry.filter or 0),
            round(float(entry.exposure or 0.0), 6))


def slot_label(entry):
    return f"Δ{float(entry.delta or 0.0):g} s filter {entry.filter}"


def signal_of(mean, bias):
    if mean is None:
        return 0.0
    return max(float(mean) - float(bias), 0.0)


def predicted_mean(measured_mean, measured_exposure, candidate_exposure, bias=0.0):
    """The mean a frame of ``candidate_exposure`` would show: linear in signal, flat in
    pedestal.
    """
    measured_exposure = float(measured_exposure)
    if measured_exposure <= 0:
        return float(measured_mean)
    signal = signal_of(measured_mean, bias)
    return float(bias) + signal * float(candidate_exposure) / measured_exposure


def next_exposure(current, measured_mean, target_mean, *, bias=0.0,
                  tolerance=0.15, max_step=4.0, min_exposure=0.05,
                  max_exposure=None, saturated_fraction=0.0):
    """One step of the closed loop; returns the exposure for the next visit.

    Multiplicative, not a PID: the response is a straight line through the pedestal,
    so the exact correction is a ratio. ``tolerance`` is a deadband keeping the
    exposure, and so the file names, still; ``max_step`` caps how far one measurement
    may move it.
    """
    current = float(current)
    max_step = max(float(max_step), 1.0)
    saturated = (saturated_fraction > SATURATED_LIMIT
                 or (measured_mean is not None
                     and measured_mean >= SATURATED_AT * SATURATION_ADU))
    if measured_mean is None:
        ratio = 1.0
    elif saturated:
        # The mean understates a clipped frame, so take the largest step down and
        # re-measure.
        ratio = 1.0 / max_step
    elif abs(float(measured_mean) - float(target_mean)) <= tolerance * float(target_mean):
        ratio = 1.0
    else:
        signal = signal_of(measured_mean, bias)
        want = max(float(target_mean) - float(bias), 0.0)
        # A frame with no signal says nothing about how much longer it should have
        # been.
        ratio = max_step if signal <= 1.0 or want <= 0.0 else want / signal

    ratio = min(max(ratio, 1.0 / max_step), max_step)
    result = current * ratio
    if max_exposure is not None:
        result = min(result, float(max_exposure))
    return max(result, float(min_exposure))


class AutoExposure:
    """Per-slot exposure memory: the sky differs by orders of magnitude between filters."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._exposures = {}

    def reset(self):
        self._exposures.clear()

    def exposure_for(self, entry, budget=None):
        """The exposure for this visit, clamped to what the slot allows.

        The first visit starts at ``min_exposure``: in twilight the scheduled exposure
        saturates, and climbing from underneath costs a few cycles but never data.
        """
        ceiling = self._ceiling(entry, budget)
        stored = self._exposures.get(slot_key(entry))
        if stored is None:
            stored = max(float(self.cfg.min_exposure), 0.0)
        return min(max(stored, float(self.cfg.min_exposure)), ceiling)

    def update(self, entry, exposure, measured_mean, budget=None, *, bias=0.0,
               saturated_fraction=0.0):
        """Returns ``(old, new)`` for the caller to log."""
        ceiling = self._ceiling(entry, budget)
        new = next_exposure(
            exposure, measured_mean, self.cfg.target_mean,
            bias=bias,
            tolerance=self.cfg.tolerance,
            max_step=self.cfg.max_step,
            min_exposure=self.cfg.min_exposure,
            max_exposure=ceiling,
            saturated_fraction=saturated_fraction,
        )
        self._exposures[slot_key(entry)] = new
        return float(exposure), new

    def _ceiling(self, entry, budget):
        """The scheduled exposure is the hard ceiling: preflight only ever shoots
        shorter.
        """
        limits = [float(entry.exposure)]
        if self.cfg.max_exposure:
            limits.append(float(self.cfg.max_exposure))
        if budget:
            limits.append(float(budget))
        return max(min(limits), float(self.cfg.min_exposure))


def sub_exposure(exposure, dead_time, splits, margin=0.0):
    """Exposure of one sub-frame when a slot is divided into ``splits``.

    The slot's budget ``E + dt`` has to hold ``N`` sub-frames and their ``N`` saves::

        N * (e + dt) = E + dt - margin   ->   e = (E + dt - margin) / N - dt

    ``N = 1`` returns ``E`` untouched, margin and all.
    """
    splits = max(int(splits), 1)
    if splits == 1:
        return float(exposure)
    return (float(exposure) + float(dead_time) - float(margin)) / splits - float(dead_time)


def max_splits(exposure, dead_time, min_exposure, cap=4, margin=0.0, min_gap=0.0):
    """Largest useful ``N``, from ``e >= e_min``::

        (E + dt - margin) / N - dt >= e_min   ->   N <= (E + dt - margin) / (e_min + dt)

    ``min_gap`` raises the floor under ``e_min``: file names resolve to one second, so
    sub-frames packed tighter would land on the same name.
    """
    floor = max(float(min_exposure), float(min_gap) - float(dead_time))
    denominator = floor + float(dead_time)
    if denominator <= 0:
        return max(int(cap), 1)
    allowed = int(math.floor((float(exposure) + float(dead_time) - float(margin))
                             / denominator))
    return max(1, min(allowed, int(cap)))


class SplitGuard:
    """Per-slot split factor, escalated on overexposure and released on hysteresis.

    Escalation tests the sub-frame just taken; release tests the mean extrapolated to
    a longer sub-exposure. Judging both on one number would oscillate: a successful
    split reads as "not overexposed" and undoes itself.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._splits = {}
        self._impossible = set()

    def reset(self):
        self._splits.clear()
        self._impossible.clear()

    def splits_for(self, entry):
        return self._splits.get(slot_key(entry), 1)

    def plan(self, entry, dead_time):
        splits = self.splits_for(entry)
        if splits <= 1:
            return 1, float(entry.exposure)
        return splits, sub_exposure(entry.exposure, dead_time, splits,
                                    self.cfg.margin)

    def cap_for(self, entry, dead_time):
        return max_splits(entry.exposure, dead_time, self.cfg.min_exposure,
                          cap=self.cfg.max_splits, margin=self.cfg.margin,
                          min_gap=self.cfg.min_frame_gap)

    def update(self, entry, dead_time, measured_mean, *, bias=0.0):
        """Returns ``(old, new)`` split factors."""
        key = slot_key(entry)
        old = self._splits.get(key, 1)
        if measured_mean is None:
            return old, old

        ceiling = self.cap_for(entry, dead_time)
        current = sub_exposure(entry.exposure, dead_time, old, self.cfg.margin)
        threshold = float(self.cfg.threshold)

        if measured_mean >= threshold:
            new = min(old + 1, ceiling)
            if new == old and ceiling < 2:
                self._impossible.add(key)
        else:
            # Smallest division whose predicted mean clears the threshold with room to
            # spare.
            safe = threshold * float(self.cfg.release)
            new = old
            for candidate in range(1, old):
                predicted = predicted_mean(
                    measured_mean, current,
                    sub_exposure(entry.exposure, dead_time, candidate,
                                 self.cfg.margin),
                    bias=bias)
                if predicted < safe:
                    new = candidate
                    break
            new = min(new, ceiling)

        self._splits[key] = new
        return old, new

    def impossible(self, entry):
        """True once this slot has been found too short to divide at all."""
        return slot_key(entry) in self._impossible
