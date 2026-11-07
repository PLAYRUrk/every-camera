"""Omega CNi3223-C24 PID controller holding the filter wheel housing temperature.

iSeries ASCII protocol, Omega manual M3397 part 5, factory settings: 9600-7O1, RS-232,
echo on. Only read commands exist here: a ``W`` writes EEPROM, rated for a million
writes, and anything else could change the regulation.

Polling runs on its own thread and port, so a slow or dead controller costs the
measurement loop nothing.
"""
from __future__ import annotations

import re
import threading
from time import monotonic

import console_ui

READING = "X01"
PEAK = "X02"
VALLEY = "X03"
SETPOINT_1 = "R01"
SETPOINT_2 = "R02"
ALARM_STATUS = "U01"
PROPORTIONAL_BAND = "G17"
RESET = "G18"
RATE = "G19"

_COMMANDS = {
    "value": READING, "peak": PEAK, "valley": VALLEY,
    "setpoint1": SETPOINT_1, "setpoint2": SETPOINT_2,
    "alarm": ALARM_STATUS,
    "proportional_band": PROPORTIONAL_BAND, "reset": RESET, "rate": RATE,
}
READ_ONLY_CLASSES = frozenset("XRUG")

# Character after U01: (alarm 1, alarm 2).
_ALARMS = {"@": (False, False), "A": (True, False),
           "B": (False, True), "C": (True, True)}

_ANSWER_TIMEOUT = 0.5
# A snapshot older than this says nothing about the temperature during a frame.
STALE_SECONDS = 60.0
_REOPEN_SECONDS = 30.0

_NUMBER = re.compile(r"[-+]?\d*\.?\d+")


def _strip_echo(answer, command):
    text = (answer or "").strip()
    return text[len(command):].strip() if text.startswith(command) else text


def parse_reading(answer, command=READING):
    """A decimal reading, or None for an error display such as ``OPEN``."""
    match = _NUMBER.search(_strip_echo(answer, command))
    return float(match.group()) if match else None


def parse_setpoint(answer, command=SETPOINT_1):
    """Three hex bytes: bit 23 sign, bits 22..20 decimal point code, the rest the value."""
    text = _strip_echo(answer, command)
    if len(text) != 6:
        return None
    try:
        word = int(text, 16)
    except ValueError:
        return None
    point = (word >> 20) & 0x7
    if not 1 <= point <= 4:
        return None
    value = (word & 0xFFFFF) / 10 ** (point - 1)
    return -value if word & 0x800000 else value


def parse_alarms(answer):
    """``(alarm 1, alarm 2)`` or None."""
    return _ALARMS.get(_strip_echo(answer, ALARM_STATUS)[:1])


def parse_hex(answer, command):
    try:
        return int(_strip_echo(answer, command), 16)
    except ValueError:
        return None


def _decode(name, command, answer):
    if answer is None:
        return None
    if name == "alarm":
        return parse_alarms(answer)
    if command.startswith("X"):
        return parse_reading(answer, command)
    if command.startswith("R"):
        return parse_setpoint(answer, command)
    return parse_hex(answer, command)


def snapshot_from(query):
    """Every parameter through ``query(command) -> answer or None``, or None if the
    controller gave no reading.
    """
    values = {name: _decode(name, command, query(command))
              for name, command in _COMMANDS.items()}
    if values["value"] is None:
        return None
    alarms = values.pop("alarm") or (None, None)
    values["alarm1"], values["alarm2"] = alarms
    return values


class OmegaLink:
    def __init__(self, port: str, baudrate: int = 9600,
                 timeout: float = _ANSWER_TIMEOUT) -> None:
        self.port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._ser = None

    def open(self) -> None:
        # Imported here so the observer tools and the simulator need no pyserial.
        import serial

        self._ser = serial.Serial(
            self.port, baudrate=self._baudrate, bytesize=7, parity="O",
            stopbits=1, timeout=0.05)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> OmegaLink:
        self.open()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def query(self, command: str) -> str | None:
        if command[:1] not in READ_ONLY_CLASSES:
            raise ValueError(f"Omega command {command!r} is not a read")
        self._ser.reset_input_buffer()
        self._ser.write(f"*{command}\r".encode("ascii"))
        answer = bytearray()
        deadline = monotonic() + self._timeout
        while monotonic() < deadline:
            chunk = self._ser.read(max(1, getattr(self._ser, "in_waiting", 0)))
            answer += chunk
            if b"\r" in answer or b"\n" in answer:
                return answer.decode("ascii", errors="ignore").strip() or None
        return None

    def read_all(self) -> dict | None:
        return snapshot_from(self.query)


def usb_serial_ports():
    from serial.tools import list_ports

    return [p.device for p in list_ports.comports() if p.vid is not None]


def find_port(exclude=(), baudrate: int = 9600) -> str | None:
    """The USB-serial port with an Omega on it. The wheel's port is never touched: it
    runs 8N1 and a stray command there could move it.
    """
    for port in usb_serial_ports():
        if port in exclude:
            continue
        try:
            with OmegaLink(port, baudrate) as link:
                if parse_reading(link.query(READING)) is not None:
                    return port
        except Exception:
            continue
    return None


class SimOmega:
    """Stand-in that answers like the controller, for ``port = "sim"``."""
    port = "sim"

    def __init__(self, value: float = 24.5, setpoint: float = 25.0) -> None:
        self.value = value
        self.setpoint = setpoint

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> SimOmega:
        return self

    def __exit__(self, *args) -> None:
        pass

    def read_all(self) -> dict:
        return {"value": self.value, "peak": self.value, "valley": self.value,
                "setpoint1": self.setpoint, "setpoint2": self.setpoint,
                "proportional_band": 200, "reset": 180, "rate": 0,
                "alarm1": False, "alarm2": False}


class OmegaMonitor:
    """Latest snapshot from a daemon thread; nothing it does can stop a night."""

    def __init__(self, port: str, baudrate: int = 9600, poll_seconds: float = 10.0,
                 exclude=(), link_factory=OmegaLink) -> None:
        self._port = str(port or "").strip()
        self._baudrate = baudrate
        self._poll_seconds = poll_seconds
        self._exclude = tuple(exclude)
        self._link_factory = link_factory
        self._latest = None
        self._read_at = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._complained = False

    def start(self) -> OmegaMonitor:
        if self._port:
            self._thread = threading.Thread(target=self._run, name="omega",
                                            daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def snapshot(self) -> dict | None:
        """The last reading, or None when there is none fresh enough to stand for now."""
        with self._lock:
            if self._read_at is None or monotonic() - self._read_at > STALE_SECONDS:
                return None
            return dict(self._latest)

    @property
    def temperature(self) -> float | None:
        latest = self.snapshot()
        return latest["value"] if latest else None

    def _make_link(self):
        if self._port.lower() == "sim":
            return SimOmega()
        port = self._port
        if port.lower() == "auto":
            port = find_port(self._exclude, self._baudrate)
            if port is None:
                raise OSError("no Omega found on any USB-serial port")
        return self._link_factory(port, self._baudrate)

    def _complain(self, reason) -> None:
        if not self._complained:
            self._complained = True
            console_ui.warn(f"Omega controller: {reason}. Measurements go on; "
                            "the wheel temperature is unknown until it answers.")

    def _poll(self, link) -> None:
        while not self._stop.is_set():
            values = link.read_all()
            if values is None:
                raise OSError("no reading")
            with self._lock:
                self._latest = values
                self._read_at = monotonic()
            if self._complained:
                self._complained = False
                console_ui.log(f"Omega controller answering on {link.port}")
            self._stop.wait(self._poll_seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._make_link() as link:
                    self._poll(link)
            except Exception as exc:
                self._complain(exc)
                self._stop.wait(_REOPEN_SECONDS)
