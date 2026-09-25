"""The two intensity-control blocks of config.json, validated.

:mod:`cameras.common.exposure` is deliberately pure — no camera, no clock, no
config parsing — so the dataclasses those controllers are constructed from, and
the validation that decides whether they may run at all, live here instead.

Both imagers read the same two blocks with the same defaults; what differs is
one rule and one label:

* the **label** is the config prefix used in every message (``asi.preflight``
  vs ``japan.preflight``), because an operator reading the error has to know
  which section of config.json to open;
* the **rule** is where the overexposure guard is allowed to run.
  ``preflight`` is a ``sun_cycle`` stage on both cameras, but the split guard is
  a feature of the general cycle: on the ASI imager that means ``time`` as well,
  while on the Hamamatsu only ``sun_cycle`` drives it — its ``time`` mode is the
  plain programme it always was. :func:`parse_overexposure` takes the modes it
  may run in rather than deciding for itself.

The house rule of both camera parsers holds here too: a misconfigured loop is
switched *off* and the reason appended to ``errors``, never corrected into
something the operator did not ask for. Shooting the twilight at the wrong
exposure is worse than not shooting it.
"""
from __future__ import annotations

from dataclasses import dataclass

import intensity

from . import cfgparse

# Full scale of these 16-bit instruments, the range every intensity setting is
# expressed in. ``intensity`` is safe to import here because it carries no numpy
# import of its own, so reading a config still does not drag one in.
SATURATION_ADU = float(intensity.FULL_SCALE)

# The archive file name resolves to one second (``archive_paths.frame_name``), so
# two frames closer together than this cannot both be filed under the name the
# station's processing program expects. The overexposure guard therefore never
# plans sub-frames tighter than this, whatever the config asks for.
ARCHIVE_NAME_RESOLUTION = 1.0


@dataclass
class PreflightCfg:
    """The bright-twilight stage of ``sun_cycle``, with automatic exposure.

    ``sun_start_angle`` is the first setpoint and must sit *above*
    ``sun_max_angle``: the sun descends through them in that order, and the
    stage lives in the gap between the two.
    """

    enabled: bool = False
    sun_start_angle: float = -6.0
    target_mean: float = 20000.0      # ADU, 0..65535
    tolerance: float = 0.15           # deadband, as a fraction of the target
    min_exposure: float = 0.05        # s
    max_exposure: float = None        # None: no longer than the slot's own
    max_step: float = 4.0             # largest exposure change per measurement
    bias: float = 0.0                 # pedestal, ADU; used when there are no darks
    binning: int = None               # None: each slot keeps its own schedule binning


@dataclass
class OverexposureCfg:
    """Dividing a slot's frame into shorter sub-frames when it over-exposes."""

    enabled: bool = False
    threshold: float = 55000.0        # mean ADU above which the slot divides
    release: float = 0.85             # hysteresis on the way back down
    max_splits: int = 4
    min_exposure: float = 0.05        # shortest sub-frame, s
    margin: float = 0.5               # slack kept inside the slot budget, s
    min_frame_gap: float = 1.0        # sub-frames stay this far apart, s
    bias: float = 0.0                 # pedestal, ADU; used when there are no darks


def parse_preflight(raw, mode, sun_max_angle, errors, *, prefix):
    """The preflight block, validated against the schedule it has to fit inside."""
    cfg = PreflightCfg(
        enabled=cfgparse.as_bool(raw, "enabled", False),
        sun_start_angle=cfgparse.as_float(raw, "sun_start_angle", -6.0),
        target_mean=cfgparse.as_float(raw, "target_mean", 20000.0),
        tolerance=cfgparse.as_float(raw, "tolerance", 0.15),
        min_exposure=cfgparse.as_float(raw, "min_exposure", 0.05),
        max_exposure=None,
        max_step=cfgparse.as_float(raw, "max_step", 4.0),
        bias=cfgparse.as_float(raw, "bias", 0.0),
        binning=None,
    )
    raw_max = raw.get("max_exposure")
    if raw_max not in (None, "", 0, 0.0):
        cfg.max_exposure = cfgparse.as_float(raw, "max_exposure", 0.0) or None
    raw_binning = raw.get("binning")
    if raw_binning not in (None, "", 0):
        cfg.binning = cfgparse.as_int(raw, "binning", 0) or None

    if cfg.enabled and mode != "sun_cycle":
        errors.append(f"{prefix}.preflight works only in 'sun_cycle' mode (mode is "
                      f"{mode!r}); the preflight stage is off")
        cfg.enabled = False
    if cfg.enabled and cfg.sun_start_angle <= sun_max_angle:
        errors.append(f"{prefix}.preflight.sun_start_angle ({cfg.sun_start_angle:g}) "
                      f"must be above {prefix}.sun_max_angle "
                      f"({sun_max_angle:g}); the preflight stage is off")
        cfg.enabled = False
    if not 0.0 < cfg.target_mean < SATURATION_ADU:
        errors.append(f"{prefix}.preflight.target_mean must be between 0 and "
                      f"{SATURATION_ADU:.0f} ADU, got "
                      f"{cfg.target_mean:g}; the preflight stage is off")
        cfg.enabled = False
    if cfg.min_exposure <= 0:
        errors.append(f"{prefix}.preflight.min_exposure must be positive, got "
                      f"{cfg.min_exposure:g}; using 0.05")
        cfg.min_exposure = 0.05
    if not 0.0 <= cfg.tolerance < 1.0:
        errors.append(f"{prefix}.preflight.tolerance must be between 0 and 1, got "
                      f"{cfg.tolerance:g}; using 0.15")
        cfg.tolerance = 0.15
    if cfg.max_step <= 1.0:
        errors.append(f"{prefix}.preflight.max_step must be greater than 1, got "
                      f"{cfg.max_step:g}; using 4.0")
        cfg.max_step = 4.0
    return cfg


def parse_overexposure(raw, errors, *, prefix, mode=None, modes=None):
    """The overexposure block. Same rule: unusable settings switch it off.

    ``modes`` names the schedule modes this camera drives the guard in; with it
    given, a guard enabled in any other mode is switched off and said so. Left
    out, the mode is not checked at all — which is the ASI imager, where every
    mode that has slots also has the guard.
    """
    cfg = OverexposureCfg(
        enabled=cfgparse.as_bool(raw, "enabled", False),
        threshold=cfgparse.as_float(raw, "threshold", 55000.0),
        release=cfgparse.as_float(raw, "release", 0.85),
        max_splits=cfgparse.as_int(raw, "max_splits", 4),
        min_exposure=cfgparse.as_float(raw, "min_exposure", 0.05),
        margin=cfgparse.as_float(raw, "margin", 0.5),
        min_frame_gap=cfgparse.as_float(raw, "min_frame_gap", 1.0),
        bias=cfgparse.as_float(raw, "bias", 0.0),
    )
    if cfg.enabled and modes and mode not in modes:
        listed = " or ".join(repr(m) for m in modes)
        errors.append(f"{prefix}.overexposure works only in {listed} mode (mode "
                      f"is {mode!r}); the overexposure guard is off")
        cfg.enabled = False
    if not 0.0 < cfg.threshold <= SATURATION_ADU:
        errors.append(f"{prefix}.overexposure.threshold must be between 0 and "
                      f"{SATURATION_ADU:.0f} ADU, got "
                      f"{cfg.threshold:g}; the overexposure guard is off")
        cfg.enabled = False
    if cfg.enabled and cfg.max_splits < 2:
        errors.append(f"{prefix}.overexposure.max_splits must be at least 2, got "
                      f"{cfg.max_splits}; the overexposure guard is off")
        cfg.enabled = False
    if cfg.min_exposure <= 0:
        errors.append(f"{prefix}.overexposure.min_exposure must be positive, got "
                      f"{cfg.min_exposure:g}; using 0.05")
        cfg.min_exposure = 0.05
    if not 0.0 < cfg.release <= 1.0:
        errors.append(f"{prefix}.overexposure.release must be between 0 and 1, got "
                      f"{cfg.release:g}; using 0.85")
        cfg.release = 0.85
    if cfg.margin < 0:
        errors.append(f"{prefix}.overexposure.margin must not be negative, got "
                      f"{cfg.margin:g}; using 0.0")
        cfg.margin = 0.0
    # Not negotiable: sub-frames packed tighter than the archive's own name
    # resolution would overwrite each other, and a frame taken is a frame that
    # has to reach the disk. A config asking for less is raised, with a note.
    if cfg.min_frame_gap < ARCHIVE_NAME_RESOLUTION:
        errors.append(f"{prefix}.overexposure.min_frame_gap must be at least "
                      f"{ARCHIVE_NAME_RESOLUTION:g} s — the archive file name "
                      f"resolves to one second — got {cfg.min_frame_gap:g}; "
                      f"using {ARCHIVE_NAME_RESOLUTION:g}")
        cfg.min_frame_gap = ARCHIVE_NAME_RESOLUTION
    return cfg


def check_threshold_above_target(preflight, overexposure, errors, *, prefix):
    """The two loops must not be asked to fight: split above what preflight holds."""
    if (preflight.enabled and overexposure.enabled
            and overexposure.threshold <= preflight.target_mean):
        errors.append(
            f"{prefix}.overexposure.threshold ({overexposure.threshold:g}) is at "
            f"or below {prefix}.preflight.target_mean ({preflight.target_mean:g}); "
            f"the two loops would work against each other")
