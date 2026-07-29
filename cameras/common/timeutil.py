"""One definition of "what UTC means here".

Three places need the same conversion — the FITS timestamp, the solar altitude
and the archive path — and they must agree, otherwise a frame can land in one
day's directory carrying another day's ``DATE-OBS``. A naive timestamp is taken
to be local time, because that is what ``datetime.now()`` hands the driver.
"""
from __future__ import annotations

from datetime import datetime, timezone


def to_utc(t: datetime) -> datetime:
    """Return ``t`` as an aware UTC datetime, assuming local time if naive."""
    if t.tzinfo is None:
        t = t.astimezone()
    return t.astimezone(timezone.utc)
