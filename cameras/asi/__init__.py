"""
ASI all-sky imager support: Princeton Instruments PIXIS + SmartMotor filter wheel.

This package is the hardware half of the ``asi`` camera type; the every-camera
worker that wraps it (MQTT, status files, LAN frame server, focus) lives in
``cameras/asi_driver.py``.

What is specific to this instrument:

    picam.py           ctypes binding to the PICAM SDK (libpicam.so)
    camera.py          PixisCamera — exposure, binning, gain, cooling, capture
    camera_sim.py      SimCamera — same interface, synthetic frames, no SDK
    devices.py         backend selection (picam/sim, serial/sim)
    config.py          the ``asi`` section of config.json as dataclasses
    exposure.py        auto-exposure and the overexposure split guard
    fits.py            FITS writer: the shared core plus imagerd_rt's own headers
    paths.py           the UTC YYYY/MM/DD archive tree and legacy file names
    seqno.py           the archive-wide frame counter behind the SEQNO header

What it shares with the ``japan`` imager now lives in ``cameras/common/`` —
``filterwheel.py``, ``filterwheel_sim.py``, ``schedule.py``, ``sun.py``,
``timeutil.py``, ``cfgparse.py`` and the FITS core. The same names still exist here
as one-line re-export shims, so ``from cameras.asi import schedule`` and friends
keep working unchanged.

Ported from the standalone asi-camera program, and from the C daemon
``imagerd_rt`` that preceded it — the observing programme, the archive layout and
the frame metadata all follow that daemon so the station's processing program
keeps working. Nothing here imports from either repository; every-camera runs
with the submodule absent.
"""
