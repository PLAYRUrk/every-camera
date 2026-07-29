"""Hamamatsu's own DCAM-API Python wrappers, vendored, loaded on demand.

Provenance
----------
``vendor/dcamapi4.py`` (dated Apr 16 2025) and ``vendor/dcam.py`` (dated
2025-04-16) come from ``samples/python/`` inside
``Hamamatsu_DCAMSDK4_v25056964.zip``, which is kept in the ``japan-camera``
submodule. They are the vendor's files, not ours:

* ``dcam.py`` is **verbatim** — including ``from dcamapi4 import *``, so the
  vendor's own sample scripts still run out of ``vendor/`` and stay usable as the
  reference when the SDK is next updated.
* ``dcamapi4.py`` differs in **one marked hunk**, which replaces the hard-coded
  ``/usr/local/lib/libdcamapi.so`` with a candidate list honouring ``DCAM_LIB``.

Updating the SDK is therefore: copy both files in, re-apply that one hunk.
``tests/test_japan_sdk.py`` fails loudly if the hunk is ever lost.

Why loading is deferred
-----------------------
``dcamapi4`` loads ``libdcamapi.so`` and binds fifty-odd ctypes prototypes *at
import time*, so importing it on a machine without the SDK raises. every-camera
must import ``cameras.japan_driver`` on any machine — ``main.py`` dispatches
through it, the tests exercise the whole driver against simulators, and a station
running only the simulator has no SDK at all. Nothing here is therefore imported
at module scope: ``cameras/japan/camera.py`` calls :func:`load` inside
``__enter__``, and ``cameras/japan/devices.py`` imports that module only on the
real-hardware branch.

This mirrors how ``cameras/asi/picam.py`` defers ``libpicam.so`` behind ``_load()``
— the difference being that here the deferral has to wrap a whole module, because
the vendor file does its work on import.
"""
from __future__ import annotations

import importlib
import sys

from pathlib import Path
from types import SimpleNamespace

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"

_sdk: SimpleNamespace | None = None


def load() -> SimpleNamespace:
    """Import the vendor wrappers and return the handful of names we use.

    Raises ``OSError`` with every path it tried when the runtime is missing. The
    result is cached, so a second camera open costs nothing.
    """
    global _sdk
    if _sdk is not None:
        return _sdk

    # The vendor's modules import each other by bare name (``from dcamapi4 import
    # *``), so they have to be importable as top-level modules. The path entry is
    # removed again immediately: leaving it would put ``dcam`` and the vendor's
    # sample scripts on every later import in the process.
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
