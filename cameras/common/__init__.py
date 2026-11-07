"""Code shared by the two scheduled all-sky imagers, ``asi`` and ``japan``.

Nothing here knows which camera is calling: ``schedule.py`` carries both schedule
vocabularies, and each camera's ``config.py`` decides which parts it accepts.
"""
