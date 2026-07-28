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
    fits.py            FITS writer: every-camera's headers plus imagerd_rt's
    paths.py           the UTC YYYY/MM/DD archive tree and legacy file names
    seqno.py           the archive-wide frame counter behind the SEQNO header
    timeutil.py        one definition of "UTC" for all of the above

Ported from the standalone asi-camera program, and from the C daemon
``imagerd_rt`` that preceded it — the observing programme, the archive layout and
the frame metadata all follow that daemon so the station's processing program
keeps working. Nothing here imports from either repository; every-camera runs
with the submodule absent.
"""
