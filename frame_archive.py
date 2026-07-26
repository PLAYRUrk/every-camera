"""
Shared frame archive helpers: listing, reading and encoding captured frames.

This module is deliberately free of camera-driver imports (no gphoto2, pyusb or
ctypes), so it can be installed on an observer machine together with
``viewer_app.py`` / ``focus_app.py`` without any camera SDK.

Provides:
  - list_frames()        — enumerate captured files in an output directory
  - resolve_frame_path() — path-traversal-safe lookup of one archived frame
  - read_frame_file()    — decode FITS / TIFF / PNG / JPEG into a numpy array
  - to_jpeg_bytes()      — 8/16/32-bit frame -> JPEG (replaces four copies of
                           this code that used to live in the camera drivers)
  - to_jpeg_capped()     — same, downscaling until it fits an MQTT payload cap
  - sharpness()          — focus metric used by focus_app.py
"""
import io
import os
import json

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FITS_EXTENSIONS = (".fits", ".fit", ".fts")
IMAGE_EXTENSIONS = (".tiff", ".tif", ".png", ".jpg", ".jpeg")
FRAME_EXTENSIONS = FITS_EXTENSIONS + IMAGE_EXTENSIONS

MIME_TYPES = {
    ".fits": "application/fits", ".fit": "application/fits",
    ".fts": "application/fits",
    ".tiff": "image/tiff", ".tif": "image/tiff",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
}

# Cameras deliver 12-bit data in 16-bit containers; used as the default
# full-scale value when a frame has no better hint.
DEFAULT_ADC_MAX = 4094

DEFAULT_JPEG_QUALITY = 85


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def _matches_day(name, mtime, day_prefix):
    """True if a capture belongs to ``day_prefix`` (``YYYYMMDD``).

    Captures are named ``%Y%m%dT%H%M%S``; for those the filename is the
    authority. Only files that do not follow the convention fall back to their
    modification time — otherwise a frame captured yesterday but copied today
    would show up under both days.
    """
    if len(name) >= 8 and name[:8].isdigit():
        return name.startswith(day_prefix)
    import time as _time
    return _time.strftime("%Y%m%d", _time.localtime(mtime)) == day_prefix


def list_frames(output_dir, date=None, limit=500, offset=0, newest_first=True):
    """List captured frames in ``output_dir``.

    Args:
        output_dir: directory holding the captured files (not recursive).
        date: optional ``YYYY-MM-DD`` filter. Matched against the filename
            prefix first (files are named ``%Y%m%dT%H%M%S``), then mtime.
        limit / offset: pagination over the sorted result.
        newest_first: sort by modification time, newest first.

    Returns:
        dict with ``total``, ``offset``, ``limit`` and ``frames`` (list of
        ``{name, size, mtime, ext}``). Never raises — an unreadable directory
        yields an empty listing, so a broken archive can never take down a
        caller such as the HTTP frame server.
    """
    result = {"total": 0, "offset": offset, "limit": limit, "frames": []}
    if not output_dir or not os.path.isdir(output_dir):
        return result

    day_prefix = date.replace("-", "") if date else None
    entries = []
    try:
        with os.scandir(output_dir) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext not in FRAME_EXTENSIONS:
                        continue
                    st = entry.stat()
                    if day_prefix and not _matches_day(entry.name, st.st_mtime,
                                                       day_prefix):
                        continue
                    entries.append({
                        "name": entry.name,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "ext": ext,
                    })
                except OSError:
                    continue
    except OSError:
        return result

    entries.sort(key=lambda e: e["mtime"], reverse=newest_first)
    result["total"] = len(entries)
    offset = max(0, int(offset))
    limit = max(0, int(limit))
    result["frames"] = entries[offset:offset + limit] if limit else entries[offset:]
    return result


def list_dates(output_dir, max_days=400):
    """Return the sorted list of ``YYYY-MM-DD`` days present in the archive."""
    import time as _time
    days = set()
    if not output_dir or not os.path.isdir(output_dir):
        return []
    try:
        with os.scandir(output_dir) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                    if os.path.splitext(entry.name)[1].lower() not in FRAME_EXTENSIONS:
                        continue
                    name = entry.name
                    if len(name) >= 8 and name[:8].isdigit():
                        days.add(f"{name[:4]}-{name[4:6]}-{name[6:8]}")
                    else:
                        days.add(_time.strftime("%Y-%m-%d",
                                                _time.localtime(entry.stat().st_mtime)))
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(days, reverse=True)[:max_days]


def resolve_frame_path(output_dir, name):
    """Resolve ``name`` inside ``output_dir``, refusing any path traversal.

    Returns the absolute path, or None if the name escapes the directory,
    does not exist or is not a frame file.
    """
    if not output_dir or not name:
        return None
    # Reject anything that looks like a path rather than a plain file name.
    if os.path.basename(name) != name:
        return None
    base = os.path.realpath(output_dir)
    candidate = os.path.realpath(os.path.join(base, name))
    if candidate != base and os.path.commonpath([base, candidate]) != base:
        return None
    if not os.path.isfile(candidate):
        return None
    if os.path.splitext(candidate)[1].lower() not in FRAME_EXTENSIONS:
        return None
    return candidate


def mime_for(path):
    return MIME_TYPES.get(os.path.splitext(path)[1].lower(),
                          "application/octet-stream")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def read_frame_file(path):
    """Decode a captured file into a numpy array.

    Supports FITS (astropy, with a dependency-free fallback), TIFF, PNG and
    JPEG. Raises on unreadable / unsupported files.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in FITS_EXTENSIONS:
        return read_fits(path)
    # Pillow handles 16-bit TIFF/PNG as well as 8-bit JPEG.
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        return np.asarray(img)


def read_fits(path):
    """Read the primary HDU of a FITS file as a numpy array."""
    try:
        from astropy.io import fits
        with fits.open(path, memmap=False) as hdul:
            for hdu in hdul:
                if getattr(hdu, "data", None) is not None:
                    return np.asarray(hdu.data)
        raise ValueError(f"No image data in {path}")
    except ImportError:
        return read_fits_minimal(path)


def read_fits_minimal(path):
    """Minimal FITS reader (no astropy). Reads only the primary HDU data."""
    with open(path, "rb") as f:
        header = {}
        done = False
        while not done:
            block = f.read(2880)
            if not block:
                break
            for i in range(0, len(block), 80):
                card = block[i:i + 80].decode("ascii", errors="replace")
                if card.startswith("END"):
                    done = True
                    break
                if "=" in card:
                    key = card[:8].strip()
                    val = card[9:].split("/")[0].strip().strip("'").strip()
                    header[key] = val
        naxis = int(header.get("NAXIS", 0))
        if naxis < 2:
            raise ValueError(f"Unsupported FITS (NAXIS={naxis}): {path}")
        naxis1 = int(header.get("NAXIS1", 0))
        naxis2 = int(header.get("NAXIS2", 0))
        bitpix = int(header.get("BITPIX", 16))
        nbytes = abs(bitpix) // 8 * naxis1 * naxis2
        data = f.read(nbytes)
        if len(data) < nbytes:
            raise ValueError(f"Truncated FITS data: {path}")
        if bitpix == 16:
            arr = np.frombuffer(data, dtype=">i2").astype(np.uint16)
        elif bitpix == 8:
            arr = np.frombuffer(data, dtype=np.uint8)
        elif bitpix == 32:
            arr = np.frombuffer(data, dtype=">i4")
        elif bitpix == -32:
            arr = np.frombuffer(data, dtype=">f4")
        elif bitpix == -64:
            arr = np.frombuffer(data, dtype=">f8")
        else:
            raise ValueError(f"Unsupported BITPIX={bitpix}: {path}")
        return arr.reshape(naxis2, naxis1)


def read_fits_header(path):
    """Return the primary FITS header as a plain ``{str: str}`` dict."""
    try:
        from astropy.io import fits
        with fits.open(path, memmap=False) as hdul:
            return {k: str(v) for k, v in hdul[0].header.items() if k}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Display conversion
# ---------------------------------------------------------------------------
def to_uint8(frame, stretch="minmax", low_pct=0.5, high_pct=99.5,
             full_scale=None):
    """Scale any frame to 8-bit for display.

    stretch:
      ``"minmax"``     — map [min, max] to [0, 255] (default; matches what the
                         drivers used to do inline).
      ``"percentile"`` — map [low_pct, high_pct] percentiles, which keeps faint
                         detail visible next to hot pixels.
      ``"raw"``        — divide by ``full_scale`` (defaults to the 12-bit ADC
                         maximum) without looking at the data.
    """
    arr = np.asarray(frame)
    if arr.dtype == np.uint8:
        return arr
    arr = arr.astype(np.float32)

    if stretch == "raw":
        scale = float(full_scale or DEFAULT_ADC_MAX) or 1.0
        return np.clip(arr * (255.0 / scale), 0, 255).astype(np.uint8)

    if stretch == "percentile":
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.zeros(arr.shape, dtype=np.uint8)
        lo = float(np.percentile(finite, low_pct))
        hi = float(np.percentile(finite, high_pct))
    else:
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.zeros(arr.shape, dtype=np.uint8)
        lo = float(finite.min())
        hi = float(finite.max())

    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.uint8)
    scaled = (arr - lo) * (255.0 / (hi - lo))
    return np.clip(np.nan_to_num(scaled), 0, 255).astype(np.uint8)


def _downscale(img8, max_side):
    """Downscale a uint8 array so its longest side is <= max_side."""
    if not max_side:
        return img8
    h, w = img8.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return img8
    from PIL import Image
    factor = max_side / float(longest)
    new_size = (max(1, int(w * factor)), max(1, int(h * factor)))
    mode = "L" if img8.ndim == 2 else None
    pil = Image.fromarray(img8, mode=mode) if mode else Image.fromarray(img8)
    return np.asarray(pil.resize(new_size, Image.BILINEAR))


def to_jpeg_bytes(frame, max_side=None, quality=DEFAULT_JPEG_QUALITY,
                  stretch="minmax", full_scale=None):
    """Convert any frame (8/16/32-bit, grayscale or RGB) to JPEG bytes."""
    img8 = to_uint8(frame, stretch=stretch, full_scale=full_scale)
    img8 = _downscale(img8, max_side)
    from PIL import Image
    if img8.ndim == 2:
        pil = Image.fromarray(img8, mode="L")
    elif img8.ndim == 3 and img8.shape[2] == 3:
        pil = Image.fromarray(img8, mode="RGB")
    elif img8.ndim == 3 and img8.shape[2] == 4:
        pil = Image.fromarray(img8, mode="RGBA").convert("RGB")
    else:
        raise ValueError(f"Unsupported frame shape: {img8.shape}")
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def to_jpeg_capped(frame, max_payload_bytes, quality=DEFAULT_JPEG_QUALITY,
                   stretch="minmax", min_side=320):
    """Encode to JPEG, halving the resolution until it fits a payload cap.

    Returns ``(jpeg_bytes, width, height)``. Used for MQTT publishing, where
    brokers (HiveMQ free tier included) reject oversized messages.
    """
    import base64

    img8 = to_uint8(frame, stretch=stretch)
    from PIL import Image
    if img8.ndim == 2:
        pil = Image.fromarray(img8, mode="L")
    elif img8.ndim == 3 and img8.shape[2] == 3:
        pil = Image.fromarray(img8, mode="RGB")
    else:
        raise ValueError(f"Unsupported frame shape: {img8.shape}")

    while True:
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=quality)
        jpeg_bytes = buf.getvalue()
        # +512 leaves room for the surrounding JSON envelope.
        payload_size = len(base64.b64encode(jpeg_bytes)) + 512
        if payload_size <= max_payload_bytes:
            return jpeg_bytes, pil.size[0], pil.size[1]
        if min(pil.size) <= min_side:
            raise ValueError(
                f"Frame payload still {payload_size} bytes after downscale "
                f"to {pil.size[0]}x{pil.size[1]}")
        pil = pil.resize((max(1, pil.size[0] // 2), max(1, pil.size[1] // 2)))


def jpeg_bytes_passthrough(data):
    """Return already-encoded JPEG bytes unchanged (Canon delivers JPEG)."""
    return data


# ---------------------------------------------------------------------------
# Focus / quality metrics
# ---------------------------------------------------------------------------
def sharpness(frame):
    """Focus metric: variance of the Laplacian. Higher means sharper.

    Computed on a float32 copy so it is independent of bit depth, but *not*
    of exposure — compare values only within one live session, which is
    exactly how focus_app.py uses it.
    """
    arr = np.asarray(frame)
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    if arr.size == 0 or arr.ndim != 2 or min(arr.shape) < 3:
        return 0.0
    a = arr.astype(np.float32)
    # 4-neighbour Laplacian without scipy/cv2.
    lap = (-4.0 * a[1:-1, 1:-1]
           + a[:-2, 1:-1] + a[2:, 1:-1]
           + a[1:-1, :-2] + a[1:-1, 2:])
    return float(lap.var())


def frame_stats(frame):
    """Basic statistics used by the viewer/focus UIs."""
    arr = np.asarray(frame)
    if arr.size == 0:
        return {}
    flat = arr.reshape(-1)
    finite = flat[np.isfinite(flat)] if flat.dtype.kind == "f" else flat
    if finite.size == 0:
        return {}
    saturation = _full_scale_for(arr)
    return {
        "shape": list(arr.shape),
        "dtype": str(arr.dtype),
        "min": float(finite.min()),
        "max": float(finite.max()),
        "mean": round(float(finite.mean()), 2),
        "std": round(float(finite.std()), 2),
        "saturated_pct": round(
            100.0 * float((finite >= saturation).sum()) / finite.size, 3),
    }


def _full_scale_for(arr):
    if arr.dtype == np.uint8:
        return 255
    if arr.dtype == np.uint16:
        # 12-bit sensors in 16-bit containers saturate at ADC_MAX, but a true
        # 16-bit frame saturates at 65535. Pick whichever the data suggests.
        return DEFAULT_ADC_MAX if arr.max() <= DEFAULT_ADC_MAX + 1 else 65535
    return float(arr.max()) if arr.size else 1.0


def histogram(frame, bins=256):
    """Return ``(counts, edges)`` as plain lists, ready for JSON."""
    arr = np.asarray(frame)
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    if arr.size == 0:
        return [], []
    counts, edges = np.histogram(arr, bins=bins)
    return counts.tolist(), edges.tolist()


# ---------------------------------------------------------------------------
# Metadata sidecars
# ---------------------------------------------------------------------------
def frame_metadata(path):
    """Best-effort metadata for one archived frame (FITS header, file stat)."""
    meta = {}
    try:
        st = os.stat(path)
        meta["size"] = st.st_size
        meta["mtime"] = st.st_mtime
    except OSError:
        pass
    if os.path.splitext(path)[1].lower() in FITS_EXTENSIONS:
        header = read_fits_header(path)
        if header:
            meta["fits_header"] = header
    return meta


def dumps(obj):
    """JSON dump helper that never chokes on numpy scalars."""
    def _default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return str(o)
    return json.dumps(obj, default=_default)
