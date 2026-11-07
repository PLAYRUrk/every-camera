"""CSDU-429 frame timing, free of pyusb so ``setup_app`` works on a machine with no
libusb.

Exposure and frame period are independent registers. In continuous mode a period
shorter than the exposure truncates the integration and the frames come out dark; the
firmware accepts both numbers and its status block quotes them back, so nothing
reports it.
"""

# What the sensor itself can do: the vendor documents 6 us to 60 minutes.
EXPOSURE_MIN_US = 6
EXPOSURE_MAX_US = 3_600_000_000

# A command carries four bytes. Wider values wrap silently to a short period, which
# truncates the exposure.
CMD_VALUE_MAX = 0xFFFF_FFFF

# Sensor readout plus USB transfer. Absolute, not a fraction of the exposure:
# a fraction is too little at 0.88 s and pointless at half an hour.
PERIOD_READOUT_MARGIN_US = 75_000

# Fixed period that truncates any longer exposure. Referenced by the regression test.
LEGACY_PERIOD_US = 75_000

# How far the camera's reported exposure may sit from the requested one before
# it counts as a refusal rather than the firmware quantising to its own clock.
EXPOSURE_TOL_FRAC = 0.02
EXPOSURE_TOL_US = 50


def exposure_to_us(exposure_s):
    """Rounds rather than truncates: ``int(0.29 * 1_000_000)`` is 289 999."""
    us = int(round(float(exposure_s) * 1_000_000))
    return max(EXPOSURE_MIN_US, min(us, EXPOSURE_MAX_US))


def derive_period_us(exposure_s, min_period_us=None, override=None,
                     allow_short=False, warn=None):
    """The frame period that lets ``exposure_s`` run to completion.

    ``override`` is the operator's ``sptt.period_us``; one shorter than the exposure
    is raised and reported through ``warn``, unless ``allow_short`` is set for modes
    where the period has no bearing on the integration. ``min_period_us`` is the
    firmware's MinPeriod and always wins: it knows the readout cost of the current
    binning and ROI.
    """
    exposure_us = exposure_to_us(exposure_s)
    floor = exposure_us + PERIOD_READOUT_MARGIN_US

    if override is not None:
        period = int(override)
        if period < floor and not allow_short:
            if warn:
                warn(f"Frame period {period} us is shorter than the "
                     f"{exposure_us} us exposure — the camera would cut the "
                     f"integration short. Using {floor} us instead.")
            period = floor
    else:
        period = floor

    if min_period_us:
        period = max(period, int(min_period_us))

    if period > CMD_VALUE_MAX:
        if warn:
            warn(f"Frame period {period} us does not fit the four bytes on the "
                 f"wire — using {CMD_VALUE_MAX} us "
                 f"({CMD_VALUE_MAX / 1e6:.0f} s), the longest this camera "
                 "can be given.")
        period = CMD_VALUE_MAX

    return period


def exposure_mismatch(requested_us, reported_us,
                      tol_frac=EXPOSURE_TOL_FRAC, tol_us=EXPOSURE_TOL_US):
    """``None`` when they agree. The tolerance covers the firmware quantising to its own
    clock.
    """
    tolerance = max(abs(requested_us) * tol_frac, tol_us)
    if abs(reported_us - requested_us) <= tolerance:
        return None
    return (f"Camera reports a {reported_us / 1e6:.6g} s exposure, "
            f"not the {requested_us / 1e6:.6g} s it was asked for")


def period_mismatch(exposure_us, reported_period_us):
    """``None`` when it fits. No tolerance: a period one microsecond short truncates."""
    if reported_period_us >= exposure_us:
        return None
    return (f"Camera reports a {reported_period_us} us frame period, shorter "
            f"than its {exposure_us} us exposure — the integration is being "
            "truncated and the frames will be dark")


def describe_timing(requested_exposure_us, sl):
    """``sl`` is the unpacked status block: 5 exposure (us), 6 frame period, 14 firmware
    minimum.
    """
    return (f"SPTT timing: requested {requested_exposure_us / 1e6:.6g} s, "
            f"camera reports {sl[5] / 1e6:.6g} s exposure, "
            f"{sl[6]} us period (firmware minimum {sl[14]} us)")
