"""Compatibility shim — the module now lives in ``cameras/common/seqno.py``.

Shared with the ``japan`` driver, whose ``sun_cycle`` mode writes ``SEQNO`` too.
"""
from ..common.seqno import *                     # noqa: F401, F403
from ..common.seqno import FILENAME, next_seqno  # noqa: F401
