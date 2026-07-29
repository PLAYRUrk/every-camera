"""Reading values out of a config dict without ever raising.

every-camera keeps one JSON file for every camera, hand-edited as often as it is
written by the setup wizard, so a value can be the wrong type, a number can be
spelled as a string, and a whole sub-object can be missing. A camera refusing to
start over a stray quote would be worse than a camera starting with the default,
so these readers fall back instead of raising; the camera's ``from_dict`` collects
what looked wrong into its ``errors`` list and the console reports it.

Both ``cameras/asi/config.py`` and ``cameras/japan/config.py`` are built out of
these five, which is why they live here.
"""
from __future__ import annotations

from datetime import datetime, time


def parse_time(value, default=None):
    """Parse ``HH:MM`` / ``HH:MM:SS`` into a ``time``; ``default`` when unusable."""
    if value in (None, ""):
        return default
    if isinstance(value, time):
        return value
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return default


def sub(cfg, key):
    """The nested object at ``key``, or an empty dict if it is missing or not one."""
    value = cfg.get(key)
    return value if isinstance(value, dict) else {}


def as_float(cfg, key, default):
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def as_int(cfg, key, default):
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def as_bool(cfg, key, default):
    value = cfg.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on", "да")
    return bool(value)
