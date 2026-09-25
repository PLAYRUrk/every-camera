"""The filter wheel table: which filter sits in which wheel position.

imagerd_rt described each position in ``imager.conf`` by a wavelength *tag* and a
free-text description, and both went into every frame it archived — the tag into
the file name, both into the ``FilterWavelength``/``FilterDescription`` records.
The ASI imager has always carried this table (``asi.filters``); the Hamamatsu
imager needs it for its ``sun_cycle`` mode, which files frames in the same
layout (``japan.filters``). Both instruments at Tory carry the same six filters,
so the default table is shared.
"""
from __future__ import annotations

from dataclasses import dataclass

# The Tory instrument's wheel, from imagerd_rt's imager.conf. Overridable
# through ``<camera>.filters``; a station with a different wheel says so there.
DEFAULT_FILTERS = [
    {"slot": 1, "wavelength": "5577", "description": "557.7nm x 2.0nm"},
    {"slot": 2, "wavelength": "6300", "description": "630.0nm x 2.0nm"},
    {"slot": 3, "wavelength": "OH__",
     "description": "Broadband OH with 18 nm notch at 865.0nm"},
    {"slot": 4, "wavelength": "8400", "description": "840.0nm x 1.8nm"},
    {"slot": 5, "wavelength": "8465", "description": "846.5nm x 1.8nm"},
    {"slot": 6, "wavelength": "8570", "description": "857.0nm x 1.8nm"},
]


@dataclass
class FilterInfo:
    """One filter wheel position, as imagerd_rt's ``imager.conf`` described it.

    The wavelength is a *tag*, not a number: the OH channel is spelled ``OH__``
    in the archive, and both the file name and the ``FilterWavelength`` header
    have always carried it that way.
    """

    slot: int = 0
    wavelength: str = ""
    description: str = ""


def parse_filters(raw, what="filters"):
    """Build the wheel table, falling back to the Tory instrument's.

    Returns ``(entries, errors)``; ``what`` names the config key in the error
    messages (``asi.filters``, ``japan.filters``).
    """
    entries, errors = [], []
    for index, item in enumerate(raw if isinstance(raw, list) and raw
                                 else DEFAULT_FILTERS, 1):
        if not isinstance(item, dict):
            errors.append(f"{what}[{index}]: expected an object, got "
                          f"{type(item).__name__}")
            continue
        try:
            slot = int(item.get("slot", item.get("filter", index)))
        except (TypeError, ValueError):
            errors.append(f"{what}[{index}]: slot must be an integer, got "
                          f"{item.get('slot')!r}")
            continue
        entries.append(FilterInfo(
            slot=slot,
            wavelength=str(item.get("wavelength", "") or ""),
            description=str(item.get("description", "") or ""),
        ))
    return entries, errors


def lookup(filters, number):
    """The wheel position's tag and description; blank when it is unknown.

    A wheel that never confirmed its move reports position 0, and a frame still
    has to be saved — an unknown filter must cost the header its wavelength, not
    the night its data.
    """
    for entry in filters:
        if entry.slot == number:
            return entry
    return FilterInfo(slot=number or 0)


def slot_for_wavelength(tag, filters=None):
    """The wheel position a wavelength tag belongs to, or None if none does.

    The imagerd_rt file name carries the tag, not the position; reading a name
    back into a position needs the table the frame was filed with.
    """
    table = filters if filters is not None else parse_filters(None)[0]
    for entry in table:
        if entry.wavelength and entry.wavelength == tag:
            return entry.slot
    return None
