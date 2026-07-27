"""Filter wheel stand-in with the same interface as :class:`filterwheel.FilterWheel`.

Selected with ``[filter_wheel] port = sim``. Moves take a realistic ~1 s so the
scheduler's dead-time behaviour is exercised the same way as with real hardware.
"""
from __future__ import annotations
from time import sleep

import console_ui

from .filterwheel import FILTER_MAX, FILTER_MIN

MOVE_SECONDS = 1.0


class SimFilterWheel:
    def __init__(self, port: str = "sim", baudrate: int = 9600,
                 move_timeout: float = 8.0) -> None:
        self.current_filter: int = 0
        self.shutter_open: bool = False

    def __enter__(self) -> SimFilterWheel:
        console_ui.log("Filter wheel: SIMULATOR (no serial port in use)")
        return self

    def __exit__(self, *args) -> None:
        pass

    def select(self, n: int) -> bool:
        if not FILTER_MIN <= n <= FILTER_MAX:
            raise ValueError(f"Filter must be {FILTER_MIN}..{FILTER_MAX}, got {n}")
        if n == self.current_filter:
            return True
        sleep(MOVE_SECONDS)
        self.current_filter = n
        return True

    def home(self) -> None:
        sleep(MOVE_SECONDS)
        self.current_filter = 0

    def set_shutter(self, open: bool) -> None:
        self.shutter_open = open
