"""Free of camera-driver imports (gphoto2, pyusb, ctypes), so an observer machine can
run ``viewer_app.py`` and ``focus_app.py`` without any camera SDK.
"""
import io
import os
import json

import numpy as np

import intensity

FITS_EXTENSIONS = (".fits", ".fit", ".fts")
IMAGE_EXTENSIONS = (".tiff", ".tif", ".png", ".jpg", ".jpeg")
FRAME_EXTENSIONS = FITS_EXTENSIONS + IMAGE_EXTENSIONS

MIME_TYPES = {
    ".fits": "application/fits", ".fit": "application/fits",
    ".fts": "application/fits",
    ".tiff": "image/tiff", ".tif": "image/tiff",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
}

# Drivers scale captures onto this range; only frames from older archives are not
# on it, see ``read_frame_with_scale``.
FULL_SCALE = intensity.FULL_SCALE

DEFAULT_JPEG_QUALITY = 85


def _matches_day(name, mtime, day_prefix):
    """The filename stamp is the authority; mtime is only for names that lack one, or a
    frame captured yesterday but copied today would show under both days.
    """
    if len(name) >= 8 and name[:8].isdigit():
        return name.startswith(day_prefix)
    import time as _time
    return _time.strftime("%Y%m%d", _time.localtime(mtime)) == day_prefix


def _scan_frame_files(output_dir):
    """Recursive: the ASI driver files frames under a ``YYYY/MM/DD`` tree, the others
    write flat.
    """
    for dirpath, _dirnames, filenames in os.walk(output_dir):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in FRAME_EXTENSIONS:
                continue
            try:
                st = os.stat(os.path.join(dirpath, fname))
            except OSError:
                continue
            yield {"name": fname, "size": st.st_size, "mtime": st.st_mtime,
                  "ext": ext}


def list_frames(output_dir, date=None, limit=500, offset=0, newest_first=True):
    """``date`` is ``YYYY-MM-DD``. Never raises: a broken archive must not take down a
    caller such as the HTTP frame server.
    """
    result = {"total": 0, "offset": offset, "limit": limit, "frames": []}
    if not output_dir or not os.path.isdir(output_dir):
        return result

    day_prefix = date.replace("-", "") if date else None
    entries = []
    try:
        for entry in _scan_frame_files(output_dir):
            if day_prefix and not _matches_day(entry["name"], entry["mtime"],
                                               day_prefix):
                continue
            entries.append(entry)
    except OSError:
        return result

    entries.sort(key=lambda e: e["mtime"], reverse=newest_first)
    result["total"] = len(entries)
    offset = max(0, int(offset))
    limit = max(0, int(limit))
    result["frames"] = entries[offset:offset + limit] if limit else entries[offset:]
    return result


def list_dates(output_dir, max_days=400):
    import time as _time
    days = set()
    if not output_dir or not os.path.isdir(output_dir):
        return []
    try:
        for entry in _scan_frame_files(output_dir):
            name = entry["name"]
            if len(name) >= 8 and name[:8].isdigit():
                days.add(f"{name[:4]}-{name[4:6]}-{name[6:8]}")
            else:
                days.add(_time.strftime("%Y-%m-%d",
                                        _time.localtime(entry["mtime"])))
    except OSError:
        return []
    return sorted(days, reverse=True)[:max_days]


def _dated_subpath(output_dir, name):
    """Mirrors ``cameras.asi.paths.day_dir`` without importing it: frame names start
    with their capture date, so the file is found without listing directories.
    """
    if len(name) < 8 or not name[:8].isdigit():
        return None
    year, month, day = name[0:4], name[4:6], name[6:8]
    return os.path.join(output_dir, year, month, day, name)


def resolve_frame_path(output_dir, name):
    """Refuses any path traversal. Returns None if ``name`` is not a frame file inside.

    Clients only send the bare name a listing gave them, so a dated name that is not
    directly in ``output_dir`` is looked for at the date subpath it implies.
    """
    if not output_dir or not name:
        return None
    if os.path.basename(name) != name:
        return None
    base = os.path.realpath(output_dir)

    def _within_base(path):
        real = os.path.realpath(path)
        if real != base and os.path.commonpath([base, real]) != base:
            return None
        return real

    candidate = _within_base(os.path.join(base, name))
    if candidate is None:
        return None
    if not os.path.isfile(candidate):
        dated = _dated_subpath(output_dir, name)
        candidate = _within_base(dated) if dated else None
        if candidate is None or not os.path.isfile(candidate):
            return None
    if os.path.splitext(candidate)[1].lower() not in FRAME_EXTENSIONS:
        return None
    return candidate


def mime_for(path):
    return MIME_TYPES.get(os.path.splitext(path)[1].lower(),
                          "application/octet-stream")


def read_frame_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in FITS_EXTENSIONS:
        return read_fits(path)
    # Pillow handles 16-bit TIFF/PNG as well as 8-bit JPEG.
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        return np.asarray(img)


def read_frame_with_scale(path):
    """Returns ``(array, full_scale)``.

    Frames without ``ADCFULL`` came straight off a 12-bit sensor and are lifted to the
    current range, or an old archive would look sixteen times darker.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in FITS_EXTENSIONS:
        arr = read_fits(path)
        recorded = intensity.full_scale_from_header(read_fits_header(path))
    else:
        # Only FITS records a scale, so it is guessed: Canon JPEGs are 8-bit, old infra
        # TIFFs 12-bit, current ones fill the full range.
        arr = read_frame_file(path)
        recorded = intensity.legacy_full_scale(arr)
    if recorded is None:
        recorded = intensity.legacy_full_scale(arr)
    if recorded < FULL_SCALE:
        return intensity.rescale_to_full(arr, recorded), FULL_SCALE
    return arr, recorded


def read_fits(path):
    """A colour cube (SPTT CSDU-285C with ``sptt.debayer``) arrives as (3, H, W); planes
    are moved last because everything here takes colour as (H, W, 3).
    """
    try:
        from astropy.io import fits
    except ImportError:
        return _planes_last(read_fits_minimal(path))
    with fits.open(path, memmap=False) as hdul:
        for hdu in hdul:
            if getattr(hdu, "data", None) is not None:
                return _planes_last(np.asarray(hdu.data))
    raise ValueError(f"No image data in {path}")


def _planes_last(arr):
    if arr.ndim == 3 and arr.shape[0] in (3, 4):
        return np.ascontiguousarray(np.moveaxis(arr, 0, -1))
    return arr


def read_fits_minimal(path):
    """Fallback without astropy; reads only the primary HDU."""
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
        # A colour cube's planes; 1 for an ordinary 2-D frame.
        naxis3 = int(header.get("NAXIS3", 1)) if naxis >= 3 else 1
        bitpix = int(header.get("BITPIX", 16))
        nbytes = abs(bitpix) // 8 * naxis1 * naxis2 * naxis3
        data = f.read(nbytes)
        if len(data) < nbytes:
            raise ValueError(f"Truncated FITS data: {path}")
        if bitpix == 16:
            # FITS stores 16-bit data signed; unsigned frames carry BZERO=32768.
            # Ignoring it moves every pixel of a full 16-bit frame by 32768.
            arr = np.frombuffer(data, dtype=">i2").astype(np.int32)
            arr = _apply_bscale(arr, header)
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
        if naxis3 > 1:
            return arr.reshape(naxis3, naxis2, naxis1)
        return arr.reshape(naxis2, naxis1)


def _apply_bscale(arr, header):
    """``BZERO=32768, BSCALE=1`` means signed shorts that are really unsigned: returns
    uint16.
    """
    try:
        bzero = float(header.get("BZERO", 0) or 0)
        bscale = float(header.get("BSCALE", 1) or 1)
    except (TypeError, ValueError):
        return arr.astype(np.uint16)
    if bscale == 1.0 and bzero == 0.0:
        # Files without BZERO hold unsigned values that wrapped: read them back that
        # way.
        return arr.astype(np.uint16)
    values = arr * bscale + bzero
    if bscale == 1.0 and bzero == 32768.0:
        return np.clip(values, 0, 65535).astype(np.uint16)
    return values.astype(np.float32)


def read_fits_header(path):
    try:
        from astropy.io import fits
        with fits.open(path, memmap=False) as hdul:
            return {k: str(v) for k, v in hdul[0].header.items() if k}
    except Exception:
        return {}


def to_uint8(frame, stretch="minmax", low_pct=0.5, high_pct=99.5,
             full_scale=None):
    """``stretch``: ``minmax``; ``percentile`` keeps faint detail next to hot pixels;
    ``raw`` divides by ``full_scale`` without looking at the data.
    """
    arr = np.asarray(frame)
    if arr.dtype == np.uint8:
        return arr
    arr = arr.astype(np.float32)

    if stretch == "raw":
        scale = float(full_scale or FULL_SCALE) or 1.0
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


def jpeg_bytes_passthrough(data):
    """Canon delivers JPEG already."""
    return data


def sharpness(frame):
    """Variance of the Laplacian. Independent of bit depth but not of exposure: compare
    values only within one live session.
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


def frame_stats(frame, full_scale=None):
    """``full_scale`` matters for saturation: a pixel is clipped only relative to the
    scale its frame was digitised on.
    """
    arr = np.asarray(frame)
    if arr.size == 0:
        return {}
    flat = arr.reshape(-1)
    finite = flat[np.isfinite(flat)] if flat.dtype.kind == "f" else flat
    if finite.size == 0:
        return {}
    saturation = float(full_scale or FULL_SCALE)
    return {
        "shape": list(arr.shape),
        "dtype": str(arr.dtype),
        "full_scale": saturation,
        "min": float(finite.min()),
        "max": float(finite.max()),
        "mean": round(float(finite.mean()), 2),
        "std": round(float(finite.std()), 2),
        "saturated_pct": round(
            100.0 * float((finite >= saturation).sum()) / finite.size, 3),
    }


def histogram(frame, bins=256, full_scale=None):
    """Binned over the full intensity range, not the data, so frames of one camera
    compare.
    """
    arr = np.asarray(frame)
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    if arr.size == 0:
        return [], []
    top = float(full_scale or FULL_SCALE)
    counts, edges = np.histogram(arr, bins=bins, range=(0.0, top))
    return counts.tolist(), edges.tolist()


def frame_metadata(path):
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
    def _default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return str(o)
    return json.dumps(obj, default=_default)
