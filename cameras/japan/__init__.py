"""
Japan all-sky imager support: Hamamatsu camera (DCAM-API) + SmartMotor filter wheel.

This package is the hardware half of the ``japan`` camera type; the every-camera
worker that wraps it (status files, LAN frame server, focus) lives in
``cameras/japan_driver.py``.

    dcamsdk/       Hamamatsu's own DCAM-API wrappers, vendored, loaded on demand
    camera.py      HamamatsuCamera — exposure, binning, readout speed, capture
    camera_sim.py  SimCamera — same interface, synthetic frames, no SDK
    devices.py     backend selection (dcam/sim, serial/sim)
    config.py      the ``japan`` section of config.json as dataclasses
    fits.py        FITS writers: the shared core with this camera's wording
                   (sun/time), and the ASI set without instrument cards plus
                   NAME (sun_cycle)
    paths.py       the flat YYYYmmddTHHMMSS_<filter>[_bg].fits names (sun/time);
                   sun_cycle uses cameras/common/archive_paths.py instead

There is deliberately no ``filterwheel.py``, ``schedule.py``, ``sun.py`` or
``timeutil.py`` here: those live in ``cameras/common/`` and are imported from there
directly. ``cameras/asi/`` keeps re-export shims under those names, but only to
avoid breaking importers that predate the split — a new package should not grow
indirection it never needed.

Ported from the standalone japan-camera program, which is also the ancestor of the
``asi`` driver: this is the older and simpler of the two observing programmes — no
cooling control, no automatic exposure. Its own two schedule modes are kept, and
the ASI imager's ``sun_cycle`` has been added, phase-locked to ``t_start`` in UTC
so both cameras, at any site, run one cycle in step. Nothing here imports
from that repository; every-camera runs with the submodule absent.

Three behaviours differ from the original on purpose, each documented where it
lives: the solar angle is computed in UTC (``cameras/common/sun.py``), ``DATE-OBS``
and the file-name stamp are UTC with the local time kept in ``DATE-LOC``
(``fits.py``, ``paths.py``), and ``capture()`` cannot wait forever (``camera.py``).
"""
