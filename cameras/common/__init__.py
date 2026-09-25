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
    fits.py            the eighteen FITS cards both writers share, and
                       imagerd_rt's legacy records
    archive_paths.py   imagerd_rt's YYYY/MM/DD tree and file names (asi; japan
                       in sun_cycle mode)
    seqno.py           the archive-wide SEQNO counter beside that archive
    filters.py         the wheel table: position -> wavelength tag, description

Nothing here knows which camera is calling. In particular ``schedule.py`` carries
the union of both schedule vocabularies — per-slot ``gain`` is ASI-only — and it
is each camera's ``config.py`` that decides which parts of that vocabulary it
accepts: see ``JAPAN_MODES`` in ``cameras/japan/config.py``. Both cameras run all
three modes; ``sun_cycle`` is phase-locked to ``t_start`` in UTC on both
(``schedule.utc_cycle_anchor``), which is what keeps their archives in step.

``cameras/asi/`` keeps one-line re-export shims for the modules that moved out of
it, so existing importers and tests are unaffected. ``cameras/japan/`` has no
such shims — it imports from here directly, being new.
"""
