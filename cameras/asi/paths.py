"""Compatibility shim — the module now lives in ``cameras/common/archive_paths.py``.

Shared with the ``japan`` driver, whose ``sun_cycle`` mode files its frames in
the same imagerd_rt layout.
"""
from ..common.archive_paths import *                           # noqa: F401, F403
from ..common.archive_paths import (                           # noqa: F401
    NO_FILTER_TAG, day_dir, frame_name, frame_path,
)
