"""
The one intensity scale this program works in.

Every camera here reports through a 16-bit word, but they do not all fill it:
the SPTT CSDU-429 and the Tanho SWIR sensor digitise 12 bits, Canon delivers an
8-bit JPEG, the PIXIS and the Hamamatsu are true 16-bit instruments. Until this
module existed each of those ranges leaked into the code that touched it —
``/ 4095.0`` in one preview, ``ADC_MAX = 4094`` in another, a heuristic in
``frame_archive`` that guessed a frame's full scale from its brightest pixel and
therefore read a dark 16-bit exposure as a 12-bit one.

So the drivers now scale on the way in (:func:`to_full_scale`), and everything
downstream — statistics, histograms, saturation, autoexposure, display — speaks
:data:`FULL_SCALE` alone.

Deliberately free of a module-level numpy import: ``cameras/asi/config.py``
reads a config file and must not drag numpy in behind it, and it needs the
constant. The array helpers import numpy when they are actually called.
"""

# The range of every intensity value in this program: a 16-bit ADC.
FULL_SCALE = 65535
FULL_SCALE_BITS = 16

# FITS keys that record the scale a file was written in. ``ADCFULL`` is what
# this program writes; ``BITDEPTH`` is a fallback that also covers the ASI's
# legacy ``BitDepth`` card (astropy hands both back upper-cased).
FULL_SCALE_KEY = "ADCFULL"
BIT_DEPTH_KEY = "BITDEPTH"

# What a frame with no scale recorded is assumed to be. Every file this program
# wrote before the 16-bit standardisation came from a 12-bit sensor.
LEGACY_BITS = 12
LEGACY_FULL_SCALE = (1 << LEGACY_BITS) - 1      # 4095


def shift_for(bits):
    """How far left to shift a ``bits``-deep sample to reach full scale.

    Zero for anything 16-bit or deeper, so calling this on an already-full-scale
    frame costs nothing and changes nothing.
    """
    try:
        bits = int(bits)
    except (TypeError, ValueError):
        return 0
    if bits <= 0 or bits >= FULL_SCALE_BITS:
        return 0
    return FULL_SCALE_BITS - bits


def to_full_scale(arr, bits):
    """Return ``arr`` mapped onto the 0..65535 range from a ``bits``-deep sensor.

    Bit replication rather than a plain shift: the sample's own high bits fill
    the room the shift opens up, so the top of the range is reached *exactly*.
    A plain ``<< 4`` leaves 12-bit full scale at 65520, and a clipped pixel that
    lands four counts short of full scale is a pixel no saturation check can
    see — which is the whole reason the archive records a full scale at all.

    It stays exact and reversible: 0 maps to 0, full scale to full scale, and
    ``>> shift`` recovers the original sample. This is the same mapping the 8-bit
    world writes as ``v * 257``.
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
    """Full scale recorded in a FITS header, or None if it says nothing.

    Accepts the plain ``{str: str}`` dicts ``frame_archive.read_fits_header``
    produces, so the values arrive as strings.
    """
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
    """Guess the full scale of a frame written before ``ADCFULL`` existed.

    Only ever a fallback for old archives: a frame this program writes now says
    what it is. The guess is the old ``frame_archive`` heuristic, kept because
    it is the only thing those files offer.
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
    """Bring a frame recorded at ``full_scale`` up to :data:`FULL_SCALE`.

    Used when displaying archives captured before the standardisation, so an old
    12-bit frame and a new 16-bit one can be looked at side by side without the
    older one appearing sixteen times darker.
    """
    import numpy as np

    a = np.asarray(arr)
    if not full_scale or full_scale >= FULL_SCALE:
        return a
    full_scale = int(full_scale)
    bits = (full_scale + 1).bit_length() - 1
    if a.dtype.kind in "ui" and (1 << bits) - 1 == full_scale:
        # A whole number of bits, which every sensor here is: use the same
        # mapping the drivers apply on the way in, so a frame captured before
        # the standardisation and one captured after hold the same values.
        return to_full_scale(a, bits)
    scaled = np.rint(a.astype(np.float64) * (FULL_SCALE / float(full_scale)))
    return np.clip(scaled, 0, FULL_SCALE).astype(np.uint16)
