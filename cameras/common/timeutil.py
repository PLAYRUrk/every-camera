"""One definition of UTC for the FITS timestamp, the solar altitude and the archive path.

They must agree, or a frame lands in one day's directory carrying another day's
``DATE-OBS``. A naive timestamp is local time, which is what ``datetime.now()`` hands
the driver.
"""
from __future__ import annotations

from datetime import datetime, timezone


def to_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        t = t.astimezone()
    return t.astimezone(timezone.utc)
