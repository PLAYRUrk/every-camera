"""Filter wheel table: which filter sits in which position.

The wavelength tag goes into the file name, tag and description into the
``FilterWavelength``/``FilterDescription`` records.
"""
from __future__ import annotations

from dataclasses import dataclass

# The Tory wheel, from imagerd_rt's imager.conf; both imagers there carry these six
# filters. A station with a different wheel says so in ``<camera>.filters``.
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
    """The wavelength is a tag, not a number: the OH channel is ``OH__`` in the archive."""

    slot: int = 0
    wavelength: str = ""
    description: str = ""


def parse_filters(raw, what="filters"):
    """Returns ``(entries, errors)``, falling back to the Tory table; ``what`` prefixes
    the errors.
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
    """Blank when the position is unknown.

    An unknown filter must cost the header its wavelength, not the night its data.
    """
    for entry in filters:
        if entry.slot == number:
            return entry
    return FilterInfo(slot=number or 0)


def slot_for_wavelength(tag, filters=None):
    """Inverse of the table: file names carry the tag, not the position."""
    table = filters if filters is not None else parse_filters(None)[0]
    for entry in table:
        if entry.wavelength and entry.wavelength == tag:
            return entry.slot
    return None
