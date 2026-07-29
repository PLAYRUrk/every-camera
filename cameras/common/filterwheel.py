"""Filter wheel / shutter controller (Animatics SmartMotor, 9600-8N1).

One controller, one wire protocol, shared by both imagers: the legacy C daemon
drove it (``imagerd_rt/src/imagerd_rt/src/imager_ctrl.c``, ``FW_Control()``), the
PIXIS (``asi``) drives it, and the Hamamatsu (``japan``) drives it. Only the port
name differs (``/dev/ttyUSB0`` on Linux instead of ``COM1``), which is why this
module lives in ``cameras/common/`` and not under either camera.

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
HOME = 0            # the position GOSUB5 parks the wheel at


class FilterWheel:
    def __init__(self, port: str, baudrate: int, move_timeout: float = 8.0,
                 poll_interval: float = 0.15) -> None:
        self._port = port
        self._baudrate = baudrate
        self._move_timeout = move_timeout    # max seconds to wait for the wheel to arrive
        self._poll_interval = poll_interval  # status-poll cadence while the wheel moves
        self._ser: Serial | None = None
        # 0 is the home position — a real place the wheel is parked at, and
        # where it sits from ``__enter__`` until a filter is chosen. None means
        # the position is genuinely unknown (a move that never confirmed), and
        # the two must not be shown as the same thing.
        self.current_filter: int | None = None
        # None until the shutter has actually been commanded: the controller
        # never reports its state on its own, and focus_app must not be shown a
        # guess as if it were a reading.
        self.shutter_open: bool | None = None

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
        self.current_filter = HOME       # GOSUB5 is the homing command
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
        # ``current_filter`` unknown (None): the wheel may be anywhere, and labelling
        # frames with a filter the instrument never reached would silently corrupt a
        # night of data. Unknown is not home — home is where the wheel actually is
        # after GOSUB5, and reporting one as the other hides a failed move.
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
            self.current_filter = None
            console_ui.warn(f"Filter wheel did not confirm position {n} within "
                            f"{self._move_timeout:.0f} s — position unknown")
        return arrived

    def home(self) -> None:
        """Send the wheel back to its home position."""
        self._ser.write(("GOSUB5" + chr(13)).encode())
        sleep(10)
        self._ser.readline()
        self.current_filter = HOME

    def set_shutter(self, open: bool) -> None:
        val = 1 if open else 0
        self._ser.write((f"d={val}" + chr(13)).encode())
        sleep(0.1)
        self._ser.write(("GOSUB6" + chr(13)).encode())
        sleep(0.1)
        self._ser.readline()  # controller ack (bounded by the serial read timeout)
        self.shutter_open = bool(open)
