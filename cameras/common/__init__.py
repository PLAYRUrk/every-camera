"""
Code shared by the two scheduled all-sky imagers, ``asi`` and ``japan``.

The ``japan`` driver (Hamamatsu, DCAM-API) is the older of the two programs; the
``asi`` driver (Princeton PIXIS, PICAM) was written from it and then grew extra
modes, cooling control and the imagerd_rt archive layout. What is genuinely the
same between them lives here, so there is one copy to fix:

    filterwheel.py     FilterWheel — Animatics SmartMotor over serial. Both
                       instruments use this controller and this wire protocol;
                       only the port name differs.
    filterwheel_sim.py SimFilterWheel — same interface, no serial port
    schedule.py        schedule entries, cycle timing, dark-frame estimates
    sun.py             solar altitude via astropy
    timeutil.py        one definition of "UTC" for names, headers and the sun
    cfgparse.py        the small typed readers config.py needs (times, numbers)
    fits.py            the eighteen FITS cards both writers share

Nothing here knows which camera is calling. In particular ``schedule.py`` carries
the union of both schedule vocabularies — ``sun_cycle`` and per-slot ``gain`` are
ASI-only — and it is each camera's ``config.py`` that decides which parts of that
vocabulary it accepts: see ``JAPAN_MODES`` in ``cameras/japan/config.py``.

``cameras/asi/`` keeps one-line re-export shims for the modules that moved out of
it, so existing importers and tests are unaffected. ``cameras/japan/`` has no
such shims — it imports from here directly, being new.
"""
