"""Compatibility shim — the module now lives in ``cameras/common/filterwheel.py``.

Both imagers drive the same SmartMotor controller with the same commands, so
there is one implementation and it is not owned by either of them.
"""
from ..common.filterwheel import *        # noqa: F401, F403
from ..common.filterwheel import (        # noqa: F401
    FILTER_MAX, FILTER_MIN, HOME, FilterWheel,
)
