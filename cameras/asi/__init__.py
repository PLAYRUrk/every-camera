"""
ASI all-sky imager support: Princeton Instruments PIXIS + SmartMotor filter wheel.

This package is the hardware half of the ``asi`` camera type; the every-camera
worker that wraps it (MQTT, status files, LAN frame server, focus) lives in
``cameras/asi_driver.py``.

    picam.py           ctypes binding to the PICAM SDK (libpicam.so)
    camera.py          PixisCamera — exposure, binning, gain, cooling, capture
    camera_sim.py      SimCamera — same interface, synthetic frames, no SDK
    filterwheel.py     FilterWheel — Animatics SmartMotor over serial
    filterwheel_sim.py SimFilterWheel
    devices.py         backend selection (picam/sim, serial/sim)
    config.py          the ``asi`` section of config.json as dataclasses
    schedule.py        schedule entries, cycle timing, dark-frame estimates
    sun.py             solar altitude via astropy
    fits.py            FITS writer with the instrument's header set

Ported from the standalone asi-camera program. Nothing here imports from that
repository — every-camera runs with the submodule absent.
"""
