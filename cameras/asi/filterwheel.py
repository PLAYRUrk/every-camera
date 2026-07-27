"""Filter wheel / shutter controller (Animatics SmartMotor, 9600-8N1).

This is the same controller and the same wire protocol the legacy C daemon uses
(``imagerd_rt/src/imagerd_rt/src/imager_ctrl.c``, ``FW_Control()``) and the same
one the Hamamatsu reference program talks to — only the port name differs
(``/dev/ttyUSB0`` on Linux instead of ``COM1``).

    GOSUB5      home the wheel
    g=<n>       select filter n (1..6)
    GOSUB4      query position, answers "FILT:<n>" once the wheel has arrived
    d=<0|1>     shutter closed / open
    GOSUB6      apply the shutter command, answers "SHTR:<0|1>"
"""
from __future__ import annotations
from time import monotonic, sleep
from typing import TYPE_CHECKING

import console_ui

if TYPE_CHECKING:
    from serial import Serial

FILTER_MIN = 1
FILTER_MAX = 6


class FilterWheel:
    def __init__(self, port: str, baudrate: int, move_timeout: float = 8.0,
                 poll_interval: float = 0.15) -> None:
        self._port = port
        self._baudrate = baudrate
        self._move_timeout = move_timeout    # max seconds to wait for the wheel to arrive
        self._poll_interval = poll_interval  # status-poll cadence while the wheel moves
        self._ser: Serial | None = None
        self.current_filter: int = 0

    def __enter__(self) -> FilterWheel:
        # Imported here so the simulator backend runs without pyserial installed.
        import serial

        self._ser = serial.Serial(
            self._port, baudrate=self._baudrate,
            bytesize=8, parity="N", stopbits=1, timeout=1.5,
        )
        self._ser.write(("GOSUB5" + chr(13)).encode())
        sleep(10)
        response = self._ser.readline().decode().strip()
        console_ui.log(f"Filter controller: {response}")
        return self

    def __exit__(self, *args) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()

    def select(self, n: int) -> bool:
        # Minimal-latency filter move: issue the goto, then poll the controller status
        # (GOSUB4 -> "FILT:<n>") and return the instant the wheel reports arrival at the
        # target. While moving the controller returns nothing; on arrival it echoes
        # "FILT:<target>". This captures the real arrival moment (~1 s) instead of a
        # fixed wait, so captures land on their scheduled (round) times. `move_timeout`
        # is only a safety ceiling for a stuck move.
        #
        # Returns True when arrival was confirmed. A move that only timed out leaves
        # ``current_filter`` unknown (0): the wheel may be anywhere, and labelling
        # frames with a filter the instrument never reached would silently corrupt a
        # night of data.
        if not FILTER_MIN <= n <= FILTER_MAX:
            raise ValueError(f"Filter must be {FILTER_MIN}..{FILTER_MAX}, got {n}")
        if n == self.current_filter:
            return True
        target = f"FILT:{n}"
        self._ser.write((f"g={n}" + chr(13)).encode())
        deadline = monotonic() + self._move_timeout
        prev_timeout = self._ser.timeout
        self._ser.timeout = self._poll_interval
        arrived = False
        try:
            while monotonic() < deadline:
                self._ser.reset_input_buffer()
                self._ser.write(("GOSUB4" + chr(13)).encode())
                resp = self._ser.read_until(b"\r").decode(errors="ignore").strip()
                if resp == target:
                    arrived = True
                    break
        finally:
            self._ser.timeout = prev_timeout
        if arrived:
            self.current_filter = n
        else:
            self.current_filter = 0
            console_ui.warn(f"Filter wheel did not confirm position {n} within "
                            f"{self._move_timeout:.0f} s — position unknown")
        return arrived

    def home(self) -> None:
        """Send the wheel back to its home position."""
        self._ser.write(("GOSUB5" + chr(13)).encode())
        sleep(10)
        self._ser.readline()
        self.current_filter = 0

    def set_shutter(self, open: bool) -> None:
        val = 1 if open else 0
        self._ser.write((f"d={val}" + chr(13)).encode())
        sleep(0.1)
        self._ser.write(("GOSUB6" + chr(13)).encode())
        sleep(0.1)
        self._ser.readline()  # controller ack (bounded by the serial read timeout)
