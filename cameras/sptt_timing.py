"""Frame timing for the SPTT CSDU-429: how long it integrates, and how often
it is allowed to start over.

The camera takes the exposure and the frame period as two independent
registers. In continuous mode the period is the frame interval, so a period
shorter than the exposure does not slow the camera down — it truncates the
integration, and the frames simply come out dark. Nothing reports it: the
firmware accepts both numbers and the status block goes on quoting them back.

This driver used to send a fixed 75 000 us period whatever the exposure was, so
the default 0.88 s exposure was being cut to 75 ms — every SPTT frame ever
archived by this program is about twelve times shorter than its header claims.
The vendor's own tools make the operator raise the period by hand
(``SPTT-CAM/capture.py --period``, the "Period (us)" box in its GUI); here it is
derived instead, because nothing in this program asks an operator to compute a
frame period.

Deliberately free of a pyusb import, unlike :mod:`cameras.sptt_driver`: this is
the arithmetic alone, so ``setup_app`` can show the derived period on a machine
with no libusb, and the tests can check it without a fake USB device.
"""

# What the sensor itself can do: the vendor documents 6 us to 60 minutes.
EXPOSURE_MIN_US = 6
EXPOSURE_MAX_US = 3_600_000_000

# A command carries its value in four bytes, so anything wider wraps. Worth a
# named constant because the wrap is silent and lands as a *short* period —
# the same darkening bug, arrived at from the opposite end.
CMD_VALUE_MAX = 0xFFFF_FFFF

# Time the camera needs after the shutter closes, before the next frame may
# start: sensor readout plus the USB transfer. This is the vendor's own default
# period, i.e. what it budgeted for a full 744x576 frame, and it is an absolute
# margin rather than a fraction of the exposure on purpose — a fraction would
# leave 44 ms at 0.88 s (too little to read the sensor out) and a pointless
# minute and a half at a half-hour exposure. Where the constant turns out to be
# optimistic, the firmware's own MinPeriod raises it (see derive_period_us).
PERIOD_READOUT_MARGIN_US = 75_000

# The period this driver sent regardless of the exposure, until it didn't.
# Referenced by the regression test that pins the bug.
LEGACY_PERIOD_US = 75_000

# How far the camera's reported exposure may sit from the requested one before
# it counts as a refusal rather than the firmware quantising to its own clock.
EXPOSURE_TOL_FRAC = 0.02
EXPOSURE_TOL_US = 50


def exposure_to_us(exposure_s):
    """Convert an exposure in seconds to the microseconds the camera wants.

    Rounds rather than truncates: ``int(0.29 * 1_000_000)`` is 289 999, and a
    driver that quietly loses a microsecond every time is a bad place to start
    an investigation into a camera that quietly loses most of them.
    """
    us = int(round(float(exposure_s) * 1_000_000))
    return max(EXPOSURE_MIN_US, min(us, EXPOSURE_MAX_US))


def derive_period_us(exposure_s, min_period_us=None, override=None,
                     allow_short=False, warn=None):
    """The frame period that lets ``exposure_s`` actually run to completion.

    ``override`` is the operator's ``sptt.period_us``; ``None`` means derive it.
    An override shorter than the exposure is raised to fit and reported through
    ``warn`` — outside continuous mode, where the period has no bearing on the
    integration, pass ``allow_short`` and the number goes through untouched.
    ``min_period_us`` is the firmware's own MinPeriod and always wins: it knows
    the readout cost of the current binning and ROI, which this module does not.
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
    """Describe the gap between the exposure asked for and the one in force.

    Returns ``None`` when they agree. The tolerance exists because the firmware
    quantises to its own clock, not to let a shortened exposure through: the
    default allowance is 2 %, and the failure this guards against is a factor
    of twelve.
    """
    tolerance = max(abs(requested_us) * tol_frac, tol_us)
    if abs(reported_us - requested_us) <= tolerance:
        return None
    return (f"Camera reports a {reported_us / 1e6:.6g} s exposure, "
            f"not the {requested_us / 1e6:.6g} s it was asked for")


def period_mismatch(exposure_us, reported_period_us):
    """Flag a frame period too short to contain the exposure.

    No tolerance here: this is precisely the condition that darkens the frames,
    and a period one microsecond short is a period that truncates.
    """
    if reported_period_us >= exposure_us:
        return None
    return (f"Camera reports a {reported_period_us} us frame period, shorter "
            f"than its {exposure_us} us exposure — the integration is being "
            "truncated and the frames will be dark")


def describe_timing(requested_exposure_us, sl):
    """One log line covering what was asked for and what the camera is doing.

    ``sl`` is the unpacked status block: index 5 is the exposure in
    microseconds, 6 the frame period, 14 the firmware's minimum period.
    """
    return (f"SPTT timing: requested {requested_exposure_us / 1e6:.6g} s, "
            f"camera reports {sl[5] / 1e6:.6g} s exposure, "
            f"{sl[6]} us period (firmware minimum {sl[14]} us)")
