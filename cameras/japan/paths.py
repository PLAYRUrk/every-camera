"""Where a Hamamatsu frame is filed: flat, under the name japan-camera gave it.

The standalone program wrote straight into its ``output_dir`` as
``YYYYmmddTHHMMSS_<filter>.fits``, with ``_bg`` appended for a dark, and the
directory was a night at a time (``C:/ccddata/20250826/`` in its own config.ini).
That is kept: the station's processing program matches these names, and the
per-night directory is what makes a flat layout workable at all.

This is deliberately **not** the ASI archive: no ``YYYY/MM/DD`` tree, no site or
device fields, no exposure in the name — see ``cameras/asi/paths.py`` for that one.

The timestamp is UTC, where japan-camera used local time. The old program's
``DATE-OBS`` claimed UTC while holding local time, so one of the two had to move;
having the name agree with a corrected ``DATE-OBS`` is worth more than matching a
name whose time base was never written down. On a station at UTC+8 the names shift
by eight hours relative to the old archive — say so in the night log once.
"""
from __future__ import annotations

from pathlib import Path

from ..common.timeutil import to_utc

# Stands in for the wheel position when it is unknown: at startup the wheel is
# parked at home (0), and a move that never confirmed reports nothing at all.
# Both must still produce a filename, and 0 is what the old program wrote.
NO_FILTER = 0


def frame_name(timestamp, filter_num, dark=False) -> str:
    """``YYYYmmddTHHMMSS_<filter>[_bg].fits`` for the UTC instant ``timestamp``."""
    utc = to_utc(timestamp)
    position = NO_FILTER if filter_num is None else int(filter_num)
    suffix = "_bg" if dark else ""
    return f"{utc:%Y%m%dT%H%M%S}_{position}{suffix}.fits"


def frame_path(root, timestamp, filter_num, dark=False) -> Path:
    """Full path of one frame; the directory is not created here."""
    return Path(root) / frame_name(timestamp, filter_num, dark=dark)
