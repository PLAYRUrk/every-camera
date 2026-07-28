"""The archive-wide frame counter behind the legacy ``SEQNO`` header.

imagerd_rt kept one integer in ``aux/seqno.txt`` and bumped it before every
frame, dark ones included (``file_io.c:624-660``, called from
``lib_capture.c:136``). The processing program uses that number, so the counter
has to survive restarts — it lives beside the archive rather than in memory.

A station moving over from the old program can simply copy its ``seqno.txt``
here and the numbering carries on.
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
    """Increment the counter in ``<output_dir>/seqno.txt`` and return the new value."""
    path = Path(output_dir) / FILENAME
    value = _read(path) + 1
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(f"{value}\n")
        os.replace(tmp, path)
    except OSError as exc:
        console_ui.warn(f"Could not update {path}: {exc}")
    return value
