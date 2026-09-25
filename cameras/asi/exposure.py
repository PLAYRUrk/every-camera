"""Compatibility shim — the module now lives in ``cameras/common/exposure.py``.

Shared with the ``japan`` driver, whose ``sun_cycle`` mode runs the same two
feedback loops. Nothing in the arithmetic was ever ASI-specific; the module sat
here only because the ASI imager was the first to need it.

The explicit re-export list below is load-bearing, not decoration: tests and the
driver reach the controllers through ``asi_driver.asi_exposure``, so the names
have to be attributes of *this* module object for a patch to intercept them.
"""
from ..common.exposure import *                          # noqa: F401, F403
from ..common.exposure import (                          # noqa: F401
    SATURATED_AT, SATURATED_LIMIT, SATURATION_ADU,
    AutoExposure, SplitGuard,
    combine_means, frame_stats, max_splits, next_exposure, predicted_mean,
    signal_of, slot_key, slot_label, sub_exposure,
)
