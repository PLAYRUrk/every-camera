"""Filter wheel stand-in with the same interface as :class:`filterwheel.FilterWheel`.

Selected with ``[filter_wheel] port = sim``. Moves take a realistic ~1 s so the
scheduler's dead-time behaviour is exercised the same way as with real hardware.
"""
from __future__ import annotations
from time import sleep

import console_ui

from .filterwheel import FILTER_MAX, FILTER_MIN, HOME, SELECT_ATTEMPTS

MOVE_SECONDS = 1.0


class SimFilterWheel:
    def __init__(self, port: str = "sim", baudrate: int = 9600,
                 move_timeout: float = 8.0) -> None:
        # None until homed, exactly as the real controller — see filterwheel.py.
        self.current_filter: int | None = None
        self.shutter_open: bool = False
        # Moves to refuse before behaving again. A wheel that does not arrive is
        # a state the driver has to handle — it costs the frame its filter tag —
        # and without this hook there is no way to reach that path at all off
        # hardware. Set it from a test; a run never touches it.
        self.fail_selects: int = 0

    def __enter__(self) -> SimFilterWheel:
        console_ui.log("Filter wheel: SIMULATOR (no serial port in use)")
        self.current_filter = HOME
        return self

    def __exit__(self, *args) -> None:
        pass

    def read_position(self, timeout: float = 1.0) -> int | None:
        return self.current_filter

    def select(self, n: int, attempts: int = SELECT_ATTEMPTS) -> bool:
        if not FILTER_MIN <= n <= FILTER_MAX:
            raise ValueError(f"Filter must be {FILTER_MIN}..{FILTER_MAX}, got {n}")
        if n == self.current_filter:
            return True
        sleep(MOVE_SECONDS)
        if self.fail_selects > 0:
            self.fail_selects -= 1
            self.current_filter = None
            return False
        self.current_filter = n
        return True

    def home(self) -> None:
        sleep(MOVE_SECONDS)
        self.current_filter = HOME

    def set_shutter(self, open: bool) -> None:
        self.shutter_open = open
