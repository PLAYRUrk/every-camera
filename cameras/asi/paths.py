"""Compatibility shim for ``cameras/common/archive_paths.py``."""
from ..common.archive_paths import *                           # noqa: F401, F403
from ..common.archive_paths import (                           # noqa: F401
    NO_FILTER_TAG, day_dir, frame_name, frame_path,
)
