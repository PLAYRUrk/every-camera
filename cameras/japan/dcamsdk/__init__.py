"""Hamamatsu's DCAM-API Python wrappers, vendored and loaded on demand.

``vendor/dcam.py`` is verbatim. ``vendor/dcamapi4.py`` differs in one marked hunk,
replacing the hard-coded library path with a candidate list honouring ``DCAM_LIB``. To
update the SDK, copy both files in and re-apply that hunk.

``dcamapi4`` loads ``libdcamapi.so`` at import time and raises without the SDK, while
``cameras.japan_driver`` must import on any machine, so nothing here imports it at
module scope.
"""
from __future__ import annotations

import importlib
import sys

from pathlib import Path
from types import SimpleNamespace

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"

_sdk: SimpleNamespace | None = None


def load() -> SimpleNamespace:
    """Import the vendor wrappers, cached. ``OSError`` lists every path tried if the
    runtime is missing.
    """
    global _sdk
    if _sdk is not None:
        return _sdk

    # The vendor's modules import each other by bare name, so they must be top-level.
    # The path entry goes straight back out to keep the vendor's samples off later
    # imports.
    added = str(VENDOR_DIR)
    sys.path.insert(0, added)
    try:
        dcam = importlib.import_module("dcam")
        dcamapi4 = importlib.import_module("dcamapi4")
    finally:
        try:
            sys.path.remove(added)
        except ValueError:
            pass

    _sdk = SimpleNamespace(
        Dcam=dcam.Dcam,
        Dcamapi=dcam.Dcamapi,
        DCAMERR=dcamapi4.DCAMERR,
        DCAMPROP=dcamapi4.DCAMPROP,
        DCAM_IDPROP=dcamapi4.DCAM_IDPROP,
        DCAM_IDSTR=dcamapi4.DCAM_IDSTR,
        dcam=dcam,
        dcamapi4=dcamapi4,
    )
    return _sdk
