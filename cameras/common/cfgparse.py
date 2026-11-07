"""Config readers that fall back instead of raising.

The JSON is hand-edited, and a camera refusing to start over a stray quote is worse
than one starting with the default.
"""
from __future__ import annotations

from datetime import datetime, time


def parse_time(value, default=None):
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
