"""Flat frame names of the ``sun`` and ``time`` modes, as the processing program matches
them.

Not the archive of ``cameras/common/archive_paths.py``, which ``sun_cycle`` files
into. The per-night output directory is what makes a flat layout workable. The stamp
is UTC, to agree with ``DATE-OBS``.
"""
from __future__ import annotations

from pathlib import Path

from ..common.timeutil import to_utc

# Wheel position written when it is unknown: every frame still needs a filename.
NO_FILTER = 0


def frame_name(timestamp, filter_num, dark=False) -> str:
    """``YYYYmmddTHHMMSS_<filter>[_bg].fits`` for the UTC instant ``timestamp``."""
    utc = to_utc(timestamp)
    position = NO_FILTER if filter_num is None else int(filter_num)
    suffix = "_bg" if dark else ""
    return f"{utc:%Y%m%dT%H%M%S}_{position}{suffix}.fits"


def frame_path(root, timestamp, filter_num, dark=False) -> Path:
    return Path(root) / frame_name(timestamp, filter_num, dark=dark)
