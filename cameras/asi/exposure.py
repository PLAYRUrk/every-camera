"""
Intensity control for the ASI imager: preflight auto-exposure and split frames.

Two optional feedback loops live here, both driven by the mean intensity of the
frame just taken, in 16-bit ADU (0..65535):

* **preflight** — the bright-twilight stage of ``sun_cycle``. Between the first
  solar-altitude setpoint and ``sun_max_angle`` the cycle runs on its normal
  slots, but the exposure of each slot is chosen to hold a target mean instead
  of being read from the schedule.
* **split** — the overexposure guard of the general cycle. When a slot comes
  back brighter than a threshold, its single frame is divided into ``N`` shorter
  sub-frames on the *next* visit, so no single frame saturates.

Everything here is pure, in the same sense as :mod:`schedule`: no camera, no
clock, no config parsing. NumPy is used only to reduce a frame to two numbers.
That is deliberate — this is the part with arithmetic worth testing on its own.

Two things trip up every naive version of this code, so they are handled
explicitly throughout:

*Pedestal.* A frame mean is ``bias + signal(exposure)``: bias and dark current do
not scale with exposure the way sky signal does. Extrapolating a raw mean
linearly ("half the exposure, half the mean") is therefore wrong, and wrong in
the unsafe direction at short exposures. Every formula below works on
``mean - bias`` and adds the pedestal back.

*Saturation.* Once pixels clip, the mean stops growing with exposure, so the
linear model reads a badly overexposed frame as only mildly bright. Both loops
detect that from the saturated-pixel fraction and take the largest step down they
are allowed rather than believing the ratio.
"""
from __future__ import annotations

import math

import numpy as np

# The imager is a true 16-bit instrument: the PICAM backend refuses anything
# deeper (``camera.py:_configure_static``) and the simulator hands out 16-bit
# frames, so full scale is a constant rather than something to sniff per frame.
SATURATION_ADU = 65535.0

# A pixel this close to full scale is clipped for our purposes.
SATURATED_AT = 0.98

# Above this fraction of clipped pixels the mean no longer tracks the exposure,
# and the controllers stop trusting their linear model.
SATURATED_LIMIT = 0.01


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------
def frame_stats(image):
    """``(mean_adu, saturated_fraction)`` of one frame; ``(None, 0.0)`` for None.

    The mean is accumulated in float64 on purpose: ``np.mean`` on a uint16 array
    would otherwise be free to sum in a narrow type on some platforms, and a
    megapixel of 60000-ADU samples overflows anything smaller.
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
    """The one number a multi-frame slot is judged by: the brightest sub-frame.

    Not the average. The guard exists to keep the *worst* frame off the ceiling,
    and during twilight the sky changes monotonically across a slot, so averaging
    would hide exactly the sub-frame that is in trouble.
    """
    values = [m for m in means if m is not None]
    return max(values) if values else None


def slot_key(entry):
    """Hashable identity of a cycle slot.

    ``Entry`` is a plain dataclass — comparable but unhashable — and
    :func:`schedule.next_cycle_slot` hands back objects taken from a *sorted
    copy* of the entry list, so neither ``id()`` nor a position in the config
    survives a round trip. The delta is what actually names a slot within the
    cycle; the filter and exposure ride along so the key reads sensibly in a log.
    """
    return (round(float(entry.delta or 0.0), 6),
            int(entry.filter or 0),
            round(float(entry.exposure or 0.0), 6))


def slot_label(entry):
    """How a slot is named in the log: ``Δ100 s filter 3``."""
    return f"Δ{float(entry.delta or 0.0):g} s filter {entry.filter}"


# ---------------------------------------------------------------------------
# Pedestal-aware intensity arithmetic
# ---------------------------------------------------------------------------
def signal_of(mean, bias):
    """Sky signal above the pedestal, never negative."""
    if mean is None:
        return 0.0
    return max(float(mean) - float(bias), 0.0)


def predicted_mean(measured_mean, measured_exposure, candidate_exposure, bias=0.0):
    """The mean a frame of ``candidate_exposure`` would have shown.

    Linear in the signal, flat in the pedestal. Used to decide whether a split
    slot could go back to a longer sub-exposure — or to a whole frame — without
    running into the threshold again.
    """
    measured_exposure = float(measured_exposure)
    if measured_exposure <= 0:
        return float(measured_mean)
    signal = signal_of(measured_mean, bias)
    return float(bias) + signal * float(candidate_exposure) / measured_exposure


# ---------------------------------------------------------------------------
# Preflight: automatic exposure
# ---------------------------------------------------------------------------
def next_exposure(current, measured_mean, target_mean, *, bias=0.0,
                  tolerance=0.15, max_step=4.0, min_exposure=0.05,
                  max_exposure=None, saturated_fraction=0.0):
    """One step of the closed loop. Returns the exposure for the next visit.

    A multiplicative controller rather than a PID: the response of a CCD to
    exposure is a straight line through the pedestal, so the exact correction is
    a ratio, and the only reasons to damp it are noise and a sky that moves
    between frames. ``tolerance`` is the deadband that keeps the exposure (and
    therefore the file names) still when the mean is close enough; ``max_step``
    caps how far one measurement may move it.
    """
    current = float(current)
    max_step = max(float(max_step), 1.0)
    saturated = (saturated_fraction > SATURATED_LIMIT
                 or (measured_mean is not None
                     and measured_mean >= SATURATED_AT * SATURATION_ADU))
    if measured_mean is None:
        ratio = 1.0
    elif saturated:
        # The mean understates a clipped frame, so the ratio would ask for a
        # gentle trim of something that is badly overexposed. Take the largest
        # step down instead and re-measure.
        ratio = 1.0 / max_step
    elif abs(float(measured_mean) - float(target_mean)) <= tolerance * float(target_mean):
        ratio = 1.0
    else:
        signal = signal_of(measured_mean, bias)
        want = max(float(target_mean) - float(bias), 0.0)
        # A frame with no signal at all carries no information about how much
        # longer it should have been; open up by the largest step allowed.
        ratio = max_step if signal <= 1.0 or want <= 0.0 else want / signal

    ratio = min(max(ratio, 1.0 / max_step), max_step)
    result = current * ratio
    if max_exposure is not None:
        result = min(result, float(max_exposure))
    return max(result, float(min_exposure))


class AutoExposure:
    """Per-slot exposure memory for the preflight stage.

    Each slot is a different filter looking at a sky that is orders of magnitude
    brighter through one than through another, so there is one exposure per slot
    and no shared state at all beyond the configuration.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._exposures = {}

    def reset(self):
        self._exposures.clear()

    def exposure_for(self, entry, budget=None):
        """The exposure to use on this visit, clamped to what the slot allows.

        The first visit starts at ``min_exposure`` rather than at the slot's own
        exposure: in twilight the scheduled exposure saturates by definition, and
        one blown frame per slot at the start of the stage is precisely what this
        feature exists to avoid. Climbing from underneath costs a few cycles and
        never costs data.
        """
        ceiling = self._ceiling(entry, budget)
        stored = self._exposures.get(slot_key(entry))
        if stored is None:
            stored = max(float(self.cfg.min_exposure), 0.0)
        return min(max(stored, float(self.cfg.min_exposure)), ceiling)

    def update(self, entry, exposure, measured_mean, budget=None, *, bias=0.0,
               saturated_fraction=0.0):
        """Fold one measurement in. Returns ``(old, new)`` for the caller to log."""
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
        """Longest exposure this slot may be given.

        The scheduled exposure is the hard ceiling — the preflight stage exists
        to shoot *shorter* than the programme, never longer — narrowed further by
        the configured maximum and by the time actually available in the slot.
        """
        limits = [float(entry.exposure)]
        if self.cfg.max_exposure:
            limits.append(float(self.cfg.max_exposure))
        if budget:
            limits.append(float(budget))
        return max(min(limits), float(self.cfg.min_exposure))


# ---------------------------------------------------------------------------
# Split frames: the overexposure guard
# ---------------------------------------------------------------------------
def sub_exposure(exposure, dead_time, splits, margin=0.0):
    """Exposure of one sub-frame when a slot is divided into ``splits``.

    The budget of a slot is one exposure plus one file save, ``E + dt``. Fitting
    ``N`` sub-frames *and their* ``N`` *saves* inside it gives

        N * (e + dt) = E + dt - margin   ->   e = (E + dt - margin) / N - dt

    which is a little shorter than the obvious ``E / N`` — the extra saves have
    to come out of somewhere. ``N = 1`` returns ``E`` untouched, margin and all,
    so an unsplit slot shoots exactly what the schedule says.
    """
    splits = max(int(splits), 1)
    if splits == 1:
        return float(exposure)
    return (float(exposure) + float(dead_time) - float(margin)) / splits - float(dead_time)


def max_splits(exposure, dead_time, min_exposure, cap=4, margin=0.0, min_gap=0.0):
    """Largest useful ``N``: the point where sub-frames get too short.

    From ``e >= e_min``:

        (E + dt - margin) / N - dt >= e_min   ->   N <= (E + dt - margin) / (e_min + dt)

    ``min_gap`` raises the floor under ``e_min`` so that one sub-frame plus its
    save still spans at least that long. The archive file name resolves to one
    second (``paths.frame_name``), so sub-frames packed tighter than that would
    land on the same name; keeping them a second apart is cheaper than teaching
    the station's processing program a new name.
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

    The escalation and the release deliberately test *different* quantities. A
    slot escalates when the sub-frame it just took is itself over the threshold —
    the frame that was actually written is too bright, whatever exposure produced
    it. It only steps back down when the mean *extrapolated* to a longer
    sub-exposure stays clear of the threshold. Judging both on the same number
    would make the state oscillate: any successful split immediately reads as
    "not overexposed" and would undo itself on the next visit.
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
        """``(splits, sub_exposure)`` for this visit to ``entry``."""
        splits = self.splits_for(entry)
        if splits <= 1:
            return 1, float(entry.exposure)
        return splits, sub_exposure(entry.exposure, dead_time, splits,
                                    self.cfg.margin)

    def cap_for(self, entry, dead_time):
        """How far this slot could be divided at all, given its timing."""
        return max_splits(entry.exposure, dead_time, self.cfg.min_exposure,
                          cap=self.cfg.max_splits, margin=self.cfg.margin,
                          min_gap=self.cfg.min_frame_gap)

    def update(self, entry, dead_time, measured_mean, *, bias=0.0):
        """Fold one slot measurement in. Returns ``(old, new)`` split factors."""
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
            # Smallest division whose predicted mean still clears the threshold
            # with room to spare. ``n = 1`` is the whole frame, which is what the
            # slot returns to as soon as the sky allows.
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
