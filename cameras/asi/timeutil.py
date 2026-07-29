"""Compatibility shim — the module now lives in ``cameras/common/timeutil.py``.

Shared with the ``japan`` driver, which needs the same one definition of UTC.
"""
from ..common.timeutil import *        # noqa: F401, F403
from ..common.timeutil import to_utc   # noqa: F401
