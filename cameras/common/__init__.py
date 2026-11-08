"""Code shared by the cameras: mostly the scheduled all-sky imagers, ``asi`` and ``japan``;
``sun_gate.py`` also serves SPTT.

Nothing here knows which camera is calling: ``schedule.py`` carries both schedule
vocabularies, and each camera's ``config.py`` decides which parts it accepts.
"""
