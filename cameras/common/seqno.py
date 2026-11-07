"""The archive-wide counter behind the ``SEQNO`` header, dark frames included.

The processing program uses the number, so it lives beside the archive to survive
restarts. A station moving from imagerd_rt can copy its ``seqno.txt`` here.
"""
from __future__ import annotations

import os

from pathlib import Path

import console_ui

FILENAME = "seqno.txt"


def _read(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        # A corrupt counter must not cost a night: restart the numbering rather
        # than refuse to save the frame.
        console_ui.warn(f"Could not read {path} ({exc}); restarting SEQNO at 1")
        return 0


def next_seqno(output_dir) -> int:
    path = Path(output_dir) / FILENAME
    value = _read(path) + 1
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(f"{value}\n")
        os.replace(tmp, path)
    except OSError as exc:
        console_ui.warn(f"Could not update {path}: {exc}")
    return value
