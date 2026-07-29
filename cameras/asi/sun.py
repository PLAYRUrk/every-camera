"""Compatibility shim — the module now lives in ``cameras/common/sun.py``.

Shared with the ``japan`` driver, whose ``sun`` mode asks the same question.
"""
from ..common.sun import *                       # noqa: F401, F403
from ..common.sun import angle_fn, sun_angle     # noqa: F401
