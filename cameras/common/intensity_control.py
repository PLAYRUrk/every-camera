"""The per-run state of the two intensity loops, shared by every camera that runs them.

A driver supplies the slot (a schedule ``Entry``, or a :class:`TickSlot` for cameras that
shoot on ``capture_seconds``), its preflight budget and its dead time; this module keeps
the stage, the per-slot memories, the pedestal and the log lines.
"""
from __future__ import annotations

from dataclasses import dataclass

import console_ui

from . import exposure as intensity_math


@dataclass
class TickSlot:
    """The one slot of a camera without a slot schedule: every tick sees the same sky.

    Only the fields :func:`exposure.slot_key` and the dead-time lookup read.
    """

    exposure: float
    readout: float = None
    delta: float = 0.0
    filter: int = 0
    label: str = "scheduled frame"


def intensity_cards(sky_mean=None, split_count=1, split_index=1):
    """FITS cards ``{key: (value, comment)}``. SKYMEAN goes on every frame: it costs one
    pass over the array and answers the first question asked of an archived frame.
    """
    cards = {}
    if sky_mean is not None:
        cards["SKYMEAN"] = (round(float(sky_mean), 2), "[ADU] mean frame intensity")
    if split_count and split_count > 1:
        cards["SPLITNUM"] = (int(split_count), "sub-frames this slot was divided into")
        cards["SPLITIDX"] = (int(split_index), "index of this sub-frame, 1-based")
    return cards


def shoot_split(frames, capture, stopping=lambda: False):
    """Run ``capture(index)`` for each sub-frame; it returns ``(ok, mean)``.

    Returns ``(ok, mean)``: ``ok`` is False only when *every* sub-frame failed, so one bad
    read in a split neither costs the slot nor nears the failure limit.
    """
    means, ok_any = [], False
    for index in range(1, max(int(frames), 1) + 1):
        if stopping():
            break
        ok, mean = capture(index)
        if ok:
            ok_any = True
            means.append(mean)
    return ok_any, intensity_math.combine_means(means)


class IntensityController:
    """``budget(entry)`` is the longest exposure preflight may give a slot;
    ``dead_time`` is the save/readout time a slot without its own ``readout`` budgets for.
    ``split_runs`` says whether the guard drives this run's mode.
    """

    def __init__(self, preflight, overexposure, *, budget=None, dead_time=0.0,
                 split_runs=None):
        self.preflight = preflight
        self.overexposure = overexposure
        self._budget = budget or (lambda entry: None)
        self._dead_time = dead_time
        self._split_runs = split_runs
        self.auto = intensity_math.AutoExposure(preflight)
        self.guard = intensity_math.SplitGuard(overexposure)
        self.stage = "main"
        self.auto_exposure = None
        self.split_frames = 1
        self._dark_means = {}
        self._warned_slots = set()

    def begin_session(self, preflight=False):
        """A night starts from the scheduled exposures, whatever the last one learnt."""
        self.auto.reset()
        self.guard.reset()
        self.stage = "preflight" if preflight and self.preflight.enabled else "main"
        self.auto_exposure = None
        self.split_frames = 1

    def split_runs(self):
        if self._split_runs is None:
            return bool(self.overexposure.enabled)
        if callable(self._split_runs):
            return bool(self._split_runs())
        return bool(self._split_runs)

    def dead_time(self, entry):
        if getattr(entry, "readout", None) is not None:
            return float(entry.readout)
        value = self._dead_time() if callable(self._dead_time) else self._dead_time
        return float(value or 0.0)

    def record_dark(self, exposure, mean):
        """Bias plus whatever dark current this exposure collects: the pedestal the loops
        extrapolate against.
        """
        if mean is not None:
            self._dark_means.setdefault(exposure, []).append(mean)

    def bias_for(self, exposure, fallback=0.0):
        """The nearest exposure's darks; the fallback covers a run with none — a late
        start, ``dark_frames`` set to zero, or a camera with no shutter.
        """
        if not self._dark_means:
            return float(fallback)
        nearest = min(self._dark_means,
                      key=lambda e: abs(float(e) - float(exposure)))
        means = self._dark_means[nearest]
        return sum(means) / len(means) if means else float(fallback)

    def idle(self):
        """Neither loop drives a dark, so nothing may go on claiming one meanwhile."""
        self.auto_exposure = None
        self.split_frames = 1

    def enter_main(self, message):
        self.stage = "main"
        self.auto_exposure = None
        self.auto.reset()
        console_ui.log(message)

    def binning(self):
        """The binning to force on this slot, or None to keep the entry's own."""
        if self.stage == "preflight" and self.preflight.binning:
            return self.preflight.binning
        return None

    def obs_mode(self, main_mode, auto_mode):
        return auto_mode if self.stage == "preflight" else main_mode

    def plan(self, entry):
        """``(exposure, frames)``; with both features off, ``(entry.exposure, 1)``."""
        if self.stage == "preflight":
            exposure = self.auto.exposure_for(entry, self._budget(entry))
            self.auto_exposure = exposure
            self.split_frames = 1
            return exposure, 1
        # The split guard owns the exposure in the main stage; a slot it has not
        # divided keeps the scheduled one untouched.
        frames, exposure = self.guard.plan(entry, self.dead_time(entry))
        self.auto_exposure = None
        self.split_frames = frames
        return exposure, frames

    def feed(self, entry, exposure, mean, saturated_fraction=0.0):
        if mean is None:
            return
        label = getattr(entry, "label", None) or intensity_math.slot_label(entry)
        if self.stage == "preflight":
            bias = self.bias_for(exposure, self.preflight.bias)
            old, new = self.auto.update(entry, exposure, mean, self._budget(entry),
                                        bias=bias,
                                        saturated_fraction=saturated_fraction)
            if abs(new - old) > 1e-6:
                console_ui.log(f"Preflight {label}: "
                               f"mean {mean:.0f} ADU → exposure {new:g} s")
            return
        if not self.split_runs():
            return
        cfg = self.overexposure
        dead = self.dead_time(entry)
        bias = self.bias_for(exposure, cfg.bias)
        old, new = self.guard.update(entry, dead, mean, bias=bias)
        if new == old:
            # A slot whose dead time swallows the budget cannot be divided. Say so once, or it
            # over-exposes all night with nothing in the log to explain why.
            if self.guard.impossible(entry) and mean >= cfg.threshold:
                key = intensity_math.slot_key(entry)
                if key not in self._warned_slots:
                    self._warned_slots.add(key)
                    console_ui.warn(
                        f"{label}: mean {mean:.0f} ADU over {cfg.threshold:.0f}, but "
                        f"{entry.exposure:g} s against {dead:g} s of dead time "
                        f"leaves no room to divide it")
            return
        if new > old:
            sub = intensity_math.sub_exposure(entry.exposure, dead, new, cfg.margin)
            console_ui.warn(f"{label}: mean {mean:.0f} ADU over {cfg.threshold:.0f} — "
                            f"next visit takes {new} frames of {sub:g} s")
        elif new == 1:
            console_ui.log(f"{label}: back to one {entry.exposure:g} s frame")
        else:
            sub = intensity_math.sub_exposure(entry.exposure, dead, new, cfg.margin)
            console_ui.log(f"{label}: down to {new} frames of {sub:g} s")

    def text(self, exposure, frames):
        """The suffix of a slot's dashboard detail line."""
        if self.stage == "preflight":
            return (f"  ·  auto {exposure:g} s "
                    f"(target {self.preflight.target_mean:.0f} ADU)")
        if frames > 1:
            return f"  ·  split ×{frames} of {exposure:g} s"
        return ""

    def row(self, last_mean):
        """The dashboard's "Intensity control" row; empty while nothing drives it."""
        mean = "" if last_mean is None else f"  ·  last mean {last_mean:.0f} ADU"
        if self.stage == "preflight":
            if self.auto_exposure is None:
                return f"preflight{mean}"
            return (f"preflight, auto {self.auto_exposure:g} s"
                    f"  ·  target {self.preflight.target_mean:.0f} ADU{mean}")
        if self.split_frames > 1:
            return (f"split ×{self.split_frames}"
                    f"  ·  limit {self.overexposure.threshold:.0f} ADU{mean}")
        return ""

    def status_fields(self, last_mean):
        return {
            "stage": self.stage,
            "auto_exposure": self.auto_exposure,
            "split_frames": self.split_frames,
            "last_mean": round(last_mean, 2) if last_mean is not None else None,
        }
