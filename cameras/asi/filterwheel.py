"""Compatibility shim for ``cameras/common/filterwheel.py``."""
from ..common.filterwheel import *        # noqa: F401, F403
from ..common.filterwheel import (        # noqa: F401
    FILTER_MAX, FILTER_MIN, HOME, FilterWheel,
)
