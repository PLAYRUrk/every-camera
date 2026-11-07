"""Filter wheel / shutter controller (Animatics SmartMotor, 9600-8N1), shared by both
imagers.

    GOSUB5      home the wheel
    g=<n>       select filter n (1..6)
    GOSUB4      query position, answers "FILT:<n>" once the wheel has arrived
    d=<0|1>     shutter closed / open
    GOSUB6      apply the shutter command, answers "SHTR:<0|1>"

A move that is not confirmed costs the frame its filter tag, so nothing here gives a
position up early: answers are assembled into whole lines across reads, an unconfirmed
move asks the position outright, is retried, and finally rebuilds the link.

That full recovery costs the better part of a minute per frame. After
``SILENT_MOVES_BEFORE_LOST`` moves with no answer at all the controller is written
off: moves fail at once and it is asked again every few minutes. Only silence does
this; a wheel that talks but will not turn still gets the retries and the rebuild.
"""
from __future__ import annotations
import os

from time import monotonic, sleep
from typing import TYPE_CHECKING

import console_ui

if TYPE_CHECKING:
    from serial import Serial

FILTER_MIN = 1
FILTER_MAX = 6
HOME = 0            # the position GOSUB5 parks the wheel at

# A first guess at a USB-serial adapter's name, in the right notation for the
# platform.
DEFAULT_PORT_POSIX = "/dev/ttyUSB0"
DEFAULT_PORT_WINDOWS = "COM3"


def default_port() -> str:
    return DEFAULT_PORT_WINDOWS if os.name == "nt" else DEFAULT_PORT_POSIX

# How long a single read of the port may block. Real waiting is done against the
# caller's deadline, so this only bounds how late an answer can be noticed.
_CHUNK_TIMEOUT = 0.1
# The controller terminates its answers with CR; some firmware adds LF.
_EOL = b"\r\n"
# GOSUB5 drives the wheel home mechanically and the controller says nothing
# while it does, so the wait for it is a fixed one.
_HOME_SECONDS = 10.0
# Ceiling for an acknowledgement that is expected to come straight back.
_ACK_TIMEOUT = 1.5
# Time the shutter is given to move before the caller is told it has. The darks of
# both cameras are taken after this pause, so shortening it needs a measurement.
_SHUTTER_SETTLE = 1.5
# Moves attempted before the link to the controller is rebuilt.
SELECT_ATTEMPTS = 2
# Consecutive moves with no answer at all before the controller is written off. One
# that answers, even with a wrong position, never is.
SILENT_MOVES_BEFORE_LOST = 3
# How often a written-off controller is asked again, and how long that may take.
_LOST_RETRY_SECONDS = 300.0
_LOST_PROBE_TIMEOUT = 1.0


def _parse_position(line):
    """None for anything else: a ``SHTR:`` acknowledgement in flight is not a position."""
    text = (line or "").strip().upper()
    if not text.startswith("FILT:"):
        return None
    try:
        return int(text[5:].strip())
    except ValueError:
        return None


class FilterWheel:
    def __init__(self, port: str, baudrate: int, move_timeout: float = 8.0,
                 poll_interval: float = 0.15) -> None:
        self._port = port
        self._baudrate = baudrate
        self._move_timeout = move_timeout    # max seconds to wait for the wheel to arrive
        self._poll_interval = poll_interval  # status-poll cadence while the wheel moves
        self._ser: Serial | None = None
        self._rx = bytearray()               # received bytes not yet handed out as lines
        # 0 is home, a real place; None means the position is genuinely unknown (a
        # move that never confirmed). The two must not be shown as the same thing.
        self.current_filter: int | None = None
        # None until commanded: the controller never reports its state on its own, and
        # focus_app must not be shown a guess as a reading.
        self.shutter_open: bool | None = None
        # What was last commanded, acknowledged or not: decisions about the shutter
        # rest on this, the confirmed state only on what was reported.
        self.shutter_wanted: bool | None = None
        self._silent_moves = 0
        self._lost = False
        self._probe_at = 0.0

    def _open_port(self) -> None:
        # Imported here so the simulator backend runs without pyserial installed.
        import serial

        from cameras.common.omega import open_serial

        self._ser = open_serial(
            serial, self._port, baudrate=self._baudrate,
            bytesize=8, parity="N", stopbits=1, timeout=_CHUNK_TIMEOUT,
        )
        del self._rx[:]

    def _flush(self) -> None:
        self._ser.reset_input_buffer()
        del self._rx[:]

    def _take_line(self):
        while self._rx[:1] in (b"\r", b"\n"):
            del self._rx[:1]
        for index, byte in enumerate(self._rx):
            if byte in _EOL:
                line = bytes(self._rx[:index]).decode(errors="ignore").strip()
                del self._rx[:index + 1]
                return line
        return None

    def _read_line(self, deadline):
        """One complete answer, or None. One split across two reads is finished by the
        next.
        """
        line = self._take_line()
        if line is not None:
            return line
        while monotonic() < deadline:
            waiting = getattr(self._ser, "in_waiting", 0)
            chunk = self._ser.read(waiting if waiting else 1)
            if chunk:
                self._rx += chunk
                line = self._take_line()
                if line is not None:
                    return line
        return None

    def __enter__(self) -> FilterWheel:
        self._open_port()
        response = self._home()
        if response is not None:
            console_ui.log(f"Filter controller: {response}")
            return self
        # An error, not a log line: the earliest warning that a night is about to be
        # filed without filter tags.
        console_ui.error(
            f"Filter controller on {self._port}: no answer. The port opened, "
            f"so the adapter is there and the fault is past it — the "
            f"controller is unpowered, it is now a different port than the "
            f"one configured, or the motor has lost the program that answers "
            f"GOSUB. Frames will be filed without a filter tag.")
        # One that will not even say where the wheel is has nothing to offer. Find
        # that out once, here, rather than at a minute per frame.
        if self.read_position(_LOST_PROBE_TIMEOUT) is None:
            self._write_off()
        return self

    def __exit__(self, *args) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()

    def _home(self):
        """Drive the wheel home, in ten seconds; the position is recorded only on an
        answer.
        """
        self._flush()
        self._ser.write(("GOSUB5" + chr(13)).encode())
        sleep(_HOME_SECONDS)
        response = self._read_line(monotonic() + _ACK_TIMEOUT)
        # Unknown is not home: a silent controller must not be reported as parked
        # there.
        self.current_filter = HOME if response is not None else None
        if response is not None:
            self._heard_from()
        return response

    def _reinitialise(self, n) -> bool:
        """Reopen the port and re-home, about ten seconds, once a move has failed twice.

        Answers both a controller that stopped talking and a wheel that stays put. The
        shutter hangs on this controller, so a known state is commanded again: left
        unknown, a frame could be taken behind a shut shutter.
        """
        console_ui.error(f"Filter wheel would not reach position {n} — reopening "
                         f"the port and homing the wheel")
        shutter = self.shutter_wanted
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass
        try:
            self._open_port()
            homed = self._home()
        except Exception as exc:
            console_ui.error(f"Filter wheel would not reopen: {exc}")
            return False
        if homed is None:
            # Nothing left to try: no shutter worth commanding and no move worth
            # making.
            console_ui.error(f"Filter wheel: the port reopened but the "
                             f"controller on {self._port} still says nothing")
            return False
        if shutter is not None:
            try:
                self.set_shutter(shutter)
            except Exception as exc:
                console_ui.warn(f"Shutter state could not be restored after "
                                f"reinitialising the wheel: {exc}")
        console_ui.log("Filter wheel reinitialised — parked at home")
        return True

    def _write_off(self) -> None:
        if self._lost:
            return
        self._lost = True
        self._probe_at = monotonic() + _LOST_RETRY_SECONDS
        console_ui.error(
            f"Filter controller written off — nothing has answered on "
            f"{self._port} for {SILENT_MOVES_BEFORE_LOST} moves. Moves will "
            f"now fail at once instead of costing about a minute each; it is "
            f"asked again every {_LOST_RETRY_SECONDS / 60:.0f} min and picked "
            f"straight back up if it answers. Frames stay filed without a "
            f"filter tag until it does.")

    def _heard_from(self) -> None:
        """Called on any answer at all, even one that parses as nothing: it ends a
        write-off.
        """
        self._silent_moves = 0
        if self._lost:
            self._lost = False
            self._probe_at = 0.0
            console_ui.log(f"Filter controller on {self._port} is answering "
                           f"again — moves resume in full")

    def _due_for_probe(self) -> bool:
        now = monotonic()
        if now < self._probe_at:
            return False
        self._probe_at = now + _LOST_RETRY_SECONDS
        return True

    def read_position(self, timeout: float = 1.0) -> int | None:
        """Where the wheel is, 0..6, or None if it will not say.

        The only place a position is read rather than remembered: it tells "never got
        there" from "the answer went missing".
        """
        self._flush()
        self._ser.write(("GOSUB4" + chr(13)).encode())
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            line = self._read_line(deadline)
            if line is None:
                return None
            self._heard_from()
            position = _parse_position(line)
            if position is not None:
                return position
        return None

    def _goto(self, n):
        """One move, watched to its arrival: ``(seen, seconds taken)``.

        Returns the instant the controller echoes the target, so a capture lands on
        its scheduled second. The port is emptied once, before the goto: a flush in
        the loop could throw away an answer already in.
        """
        self._flush()
        started = monotonic()
        self._ser.write((f"g={n}" + chr(13)).encode())
        deadline = started + self._move_timeout
        while monotonic() < deadline:
            self._ser.write(("GOSUB4" + chr(13)).encode())
            poll_deadline = min(deadline, monotonic() + self._poll_interval)
            while True:
                line = self._read_line(poll_deadline)
                if line is None:
                    break
                self._heard_from()
                if _parse_position(line) == n:
                    return True, monotonic() - started
        return False, monotonic() - started

    def select(self, n: int, attempts: int = SELECT_ATTEMPTS) -> bool:
        """Move to filter ``n``; True when the wheel is known to be there.

        An unconfirmed move is located, retried, then answered by rebuilding the link.
        Failing that, ``current_filter`` stays at the last reported position, or None:
        a wheel that may be anywhere must not label frames with a filter it never
        reached. Unknown is not home.
        """
        if not FILTER_MIN <= n <= FILTER_MAX:
            raise ValueError(f"Filter must be {FILTER_MIN}..{FILTER_MAX}, got {n}")
        if n == self.current_filter:
            return True
        if self._lost:
            # Written off: one cheap question when due, otherwise straight back. The
            # frame is filed without a filter tag either way.
            if not self._due_for_probe():
                self.current_filter = None
                return False
            if self.read_position(_LOST_PROBE_TIMEOUT) is None:
                self.current_filter = None
                return False
            # It answered, so _heard_from has picked it back up: make the move in
            # full.
        position = None
        for attempt in range(1, attempts + 1):
            try:
                arrived, elapsed = self._goto(n)
                if not arrived:
                    position = self.read_position()
                    arrived = position == n
            except OSError as exc:
                # A USB-serial adapter that dropped off the bus: only reopening helps.
                console_ui.error(f"Filter wheel port {self._port} failed: {exc}")
                break
            if arrived:
                self.current_filter = n
                console_ui.log(f"Filter {n} reached in {elapsed:.1f} s")
                return True
            console_ui.warn(
                f"Filter wheel did not confirm position {n} within "
                f"{self._move_timeout:.0f} s (it reports "
                f"{'nothing' if position is None else position})"
                + (" — retrying" if attempt < attempts else ""))
        if self._reinitialise(n):
            try:
                arrived, elapsed = self._goto(n)
                if arrived:
                    self.current_filter = n
                    console_ui.log(f"Filter {n} reached in {elapsed:.1f} s after "
                                   f"reinitialising the wheel")
                    return True
                position = self.read_position()
            except OSError as exc:
                console_ui.error(f"Filter wheel port {self._port} failed: {exc}")
                position = None
            if position == n:
                self.current_filter = n
                console_ui.log(f"Filter {n} confirmed after reinitialising the wheel")
                return True
        self.current_filter = position
        console_ui.error(
            f"Filter wheel would not reach position {n} — it will not say where "
            f"it is; frames are filed without a filter tag"
            if position is None else
            f"Filter wheel would not reach position {n} — it reports {position}; "
            f"frames are filed with that, not with what the schedule asked for")
        if position is None:
            # Any answer would have reset this through _heard_from, so this really
            # counts silence.
            self._silent_moves += 1
            if self._silent_moves >= SILENT_MOVES_BEFORE_LOST:
                self._write_off()
        return False

    def home(self) -> None:
        self._home()

    def set_shutter(self, open: bool) -> None:
        self.shutter_wanted = bool(open)
        try:
            acked = self._command_shutter(open)
        except OSError:
            # The adapter may have re-enumerated: reopen once and try again.
            self._reopen_port()
            acked = self._command_shutter(open)
        # Without an acknowledgement the state is only what was asked for.
        self.shutter_open = bool(open) if acked else None
        if not acked:
            console_ui.warn(f"Shutter on {self._port}: no acknowledgement — "
                            f"its state is unknown")

    def _reopen_port(self) -> None:
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass
        self._open_port()

    def _command_shutter(self, open: bool) -> bool:
        val = 1 if open else 0
        self._flush()
        started = monotonic()
        self._ser.write((f"d={val}" + chr(13)).encode())
        sleep(0.1)
        self._ser.write(("GOSUB6" + chr(13)).encode())
        # Read the acknowledgement: a stray SHTR: answer picked up during a move is
        # one more way to miss an arrival.
        acked = False
        deadline = monotonic() + _ACK_TIMEOUT
        while monotonic() < deadline:
            line = self._read_line(deadline)
            if line is None:
                break
            if line.strip().upper().startswith("SHTR:"):
                self._heard_from()
                acked = True
                break
        remaining = _SHUTTER_SETTLE - (monotonic() - started)
        if remaining > 0:
            sleep(remaining)
        return acked
