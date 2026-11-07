"""The one intensity scale of this program, :data:`FULL_SCALE`.

Cameras report through a 16-bit word but do not all fill it: the SPTT CSDU-429 and the
Tanho SWIR sensor digitise 12 bits, Canon delivers an 8-bit JPEG, the PIXIS and the
Hamamatsu are true 16-bit. Drivers scale on the way in (:func:`to_full_scale`), and
everything downstream speaks :data:`FULL_SCALE` alone.

No module-level numpy import: ``cameras/asi/config.py`` needs the constant and must not
drag numpy in behind a config file.
"""

FULL_SCALE = 65535
FULL_SCALE_BITS = 16

# FITS keys recording the scale. ``BITDEPTH`` is a fallback that also covers the ASI's
# legacy ``BitDepth`` card (astropy hands both back upper-cased).
FULL_SCALE_KEY = "ADCFULL"
BIT_DEPTH_KEY = "BITDEPTH"

# A frame with no scale recorded is assumed to come from a 12-bit sensor.
LEGACY_BITS = 12
LEGACY_FULL_SCALE = (1 << LEGACY_BITS) - 1      # 4095


def shift_for(bits):
    """Zero for 16 bits or deeper, so an already-full-scale frame is left unchanged."""
    try:
        bits = int(bits)
    except (TypeError, ValueError):
        return 0
    if bits <= 0 or bits >= FULL_SCALE_BITS:
        return 0
    return FULL_SCALE_BITS - bits


def to_full_scale(arr, bits):
    """Bit replication, not a plain shift: ``<< 4`` leaves 12-bit full scale at 65520,
    and a clipped pixel four counts short of full scale is one no saturation check can
    see.

    Exact and reversible: 0 maps to 0, full scale to full scale, and ``>> shift``
    recovers the original sample. The 8-bit case is ``v * 257``.
    """
    import numpy as np

    a = np.asarray(arr)
    if a.dtype == np.uint8:
        # An 8-bit frame has to grow a container before it can be shifted.
        a = a.astype(np.uint16)
        bits = 8
    shift = shift_for(bits)
    if not shift:
        return a
    wide = a.astype(np.uint32)
    return (np.left_shift(wide, shift)
            | np.right_shift(wide, int(bits) - shift)).astype(np.uint16)


def full_scale_from_header(header):
    """Takes the ``{str: str}`` dicts ``frame_archive.read_fits_header`` produces."""
    if not header:
        return None
    for key in (FULL_SCALE_KEY, BIT_DEPTH_KEY):
        value = header.get(key)
        if value is None:
            continue
        try:
            value = int(float(str(value).strip()))
        except (TypeError, ValueError):
            continue
        if key == BIT_DEPTH_KEY:
            return (1 << value) - 1 if 0 < value <= 32 else None
        if value > 0:
            return value
    return None


def legacy_full_scale(arr):
    """A guess for old archives with no ``ADCFULL``: the only thing those files offer.
    """
    import numpy as np

    a = np.asarray(arr)
    if a.dtype == np.uint8:
        return 255
    if a.size == 0:
        return FULL_SCALE
    if a.dtype.kind in "ui":
        top = int(a.max())
        return LEGACY_FULL_SCALE if top <= LEGACY_FULL_SCALE else FULL_SCALE
    return FULL_SCALE


def rescale_to_full(arr, full_scale):
    """Lets an old 12-bit frame sit next to a 16-bit one without looking sixteen times
    darker.
    """
    import numpy as np

    a = np.asarray(arr)
    if not full_scale or full_scale >= FULL_SCALE:
        return a
    full_scale = int(full_scale)
    bits = (full_scale + 1).bit_length() - 1
    if a.dtype.kind in "ui" and (1 << bits) - 1 == full_scale:
        # A whole number of bits, as on every sensor here: the drivers' own mapping, so
        # old and new frames hold the same values.
        return to_full_scale(a, bits)
    scaled = np.rint(a.astype(np.float64) * (FULL_SCALE / float(full_scale)))
    return np.clip(scaled, 0, FULL_SCALE).astype(np.uint16)
