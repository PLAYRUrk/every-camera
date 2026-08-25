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

A move that is not confirmed costs the frame its filter tag — ``_none_`` in the
name, ``FILTER = 0`` in the header — so everything here is built around not
giving a position up too early.

Answers are collected byte by byte into ``_rx`` and handed out a whole line at a
time. The previous version emptied the port before every ``GOSUB4`` and then
read for one poll interval with ``read_until``, which meant an answer that
straddled the end of a window was returned half-read, judged wrong, and had its
tail thrown away by the next flush. That loses individual polls rather than all
of them, so on its own it does not explain a wheel that reports "unknown" for
the rest of a night — it is simply a way of reading a serial line that cannot be
relied on, and whole lines assembled across reads can.

What does explain a night is that nothing here used to recover. The position was
only ever read as a side effect of a move, there was no retry, and a controller
that had stopped talking — a USB-serial link that reset, a wheel that jammed —
stayed unreachable until the program was restarted, every frame in between filed
without a filter. Hence, below: ``read_position`` to ask outright, a second
attempt, and ``_reinitialise`` to rebuild the link. Which of the three did the
work is visible in the log.
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
# Time the shutter is given to actually move before the caller is told it has.
# This used to happen by accident — ``readline()`` waited for an LF the
# controller never sends, and so always burned its full timeout — and the darks
# of both cameras have been taken after that pause ever since. It is spelled out
# here rather than dropped: shortening it is a measurement, not a cleanup.
_SHUTTER_SETTLE = 1.5
# Moves attempted before the link to the controller is rebuilt.
SELECT_ATTEMPTS = 2


def _parse_position(line):
    """The number in a ``FILT:<n>`` answer; None for anything else.

    The controller also answers ``SHTR:<0|1>``, and a shutter acknowledgement
    still in flight must not be read as a wheel position.
    """
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
        # 0 is the home position — a real place the wheel is parked at, and
        # where it sits from ``__enter__`` until a filter is chosen. None means
        # the position is genuinely unknown (a move that never confirmed), and
        # the two must not be shown as the same thing.
        self.current_filter: int | None = None
        # None until the shutter has actually been commanded: the controller
        # never reports its state on its own, and focus_app must not be shown a
        # guess as if it were a reading.
        self.shutter_open: bool | None = None

    # -- the wire ------------------------------------------------------------
    def _open_port(self) -> None:
        # Imported here so the simulator backend runs without pyserial installed.
        import serial

        self._ser = serial.Serial(
            self._port, baudrate=self._baudrate,
            bytesize=8, parity="N", stopbits=1, timeout=_CHUNK_TIMEOUT,
        )
        del self._rx[:]

    def _flush(self) -> None:
        """Drop what the controller has already said, in the port and in ``_rx``."""
        self._ser.reset_input_buffer()
        del self._rx[:]

    def _take_line(self):
        """The first complete answer sitting in ``_rx``, or None."""
        while self._rx[:1] in (b"\r", b"\n"):
            del self._rx[:1]
        for index, byte in enumerate(self._rx):
            if byte in _EOL:
                line = bytes(self._rx[:index]).decode(errors="ignore").strip()
                del self._rx[:index + 1]
                return line
        return None

    def _read_line(self, deadline):
        """One complete answer, or None if none arrived before ``deadline``.

        An answer split across two reads is finished by the next one rather than
        thrown away half-read, which is what the old ``read_until`` did to any
        answer that straddled the end of a poll window.
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

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> FilterWheel:
        self._open_port()
        response = self._home()
        console_ui.log(f"Filter controller: {response or 'no answer'}")
        return self

    def __exit__(self, *args) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()

    def _home(self):
        """Drive the wheel home and record that it is there. Takes ten seconds."""
        self._flush()
        self._ser.write(("GOSUB5" + chr(13)).encode())
        sleep(_HOME_SECONDS)
        response = self._read_line(monotonic() + _ACK_TIMEOUT)
        self.current_filter = HOME       # GOSUB5 is the homing command
        return response

    def _reinitialise(self, n) -> bool:
        """Rebuild the link and re-home the wheel after the retries have failed.

        The port is closed and opened again and the wheel driven home, which
        costs about ten seconds — only ever paid once a move has failed twice.
        It answers both ways a move can fail: a controller that has stopped
        talking, and one that talks while the wheel stays put, which homing may
        yet free. The shutter hangs on this same controller, so a state that was
        known before is commanded again afterwards: leaving it unknown would let
        a scheduled frame be taken behind a shut shutter, which is worse than
        the lost filter tag this whole path exists to avoid.
        """
        console_ui.error(f"Filter wheel would not reach position {n} — reopening "
                         f"the port and homing the wheel")
        shutter = self.shutter_open
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass
        try:
            self._open_port()
            self._home()
        except Exception as exc:
            console_ui.error(f"Filter wheel would not reopen: {exc}")
            return False
        if shutter is not None:
            try:
                self.set_shutter(shutter)
            except Exception as exc:
                console_ui.warn(f"Shutter state could not be restored after "
                                f"reinitialising the wheel: {exc}")
        console_ui.log("Filter wheel reinitialised — parked at home")
        return True

    # -- position ------------------------------------------------------------
    def read_position(self, timeout: float = 1.0) -> int | None:
        """Ask the controller where the wheel is: 0..6, or None if it will not say.

        The only place a position is *read* rather than remembered. ``select``
        asks here before giving a position up for lost: "the wheel never got
        there" and "it got there and the answer went missing" used to be
        indistinguishable, and both cost the frame its filter tag.
        """
        self._flush()
        self._ser.write(("GOSUB4" + chr(13)).encode())
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            line = self._read_line(deadline)
            if line is None:
                return None
            position = _parse_position(line)
            if position is not None:
                return position
        return None

    def _goto(self, n):
        """One move, watched to its arrival: ``(seen, seconds taken)``.

        Returns the instant the controller echoes the target, so a capture lands
        on its scheduled (round) second instead of after a fixed wait; a real
        move takes about a second, and ``move_timeout`` is only a ceiling for a
        stuck one. The port is emptied once, before the goto, rather than before
        every poll: a flush in the loop can only throw away an answer that is
        already in, never one still on its way.
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
                if _parse_position(line) == n:
                    return True, monotonic() - started
        return False, monotonic() - started

    def select(self, n: int, attempts: int = SELECT_ATTEMPTS) -> bool:
        """Move to filter ``n``; True when the wheel is known to be there.

        An unconfirmed move is not yet a failure: the controller is asked
        outright where the wheel is, and only a move that neither arrived nor
        could be located is retried and finally answered by rebuilding the link.
        What survives all of that leaves ``current_filter`` at whatever the
        controller last reported — a position that was read is a fact, and beats
        "unknown" — or at None, meaning the wheel may be anywhere and labelling
        frames with a filter the instrument never reached would silently corrupt
        a night of data. Unknown is not home: home is where the wheel actually
        is after GOSUB5, and reporting one as the other hides a failed move.
        """
        if not FILTER_MIN <= n <= FILTER_MAX:
            raise ValueError(f"Filter must be {FILTER_MIN}..{FILTER_MAX}, got {n}")
        if n == self.current_filter:
            return True
        position = None
        for attempt in range(1, attempts + 1):
            arrived, elapsed = self._goto(n)
            if not arrived:
                position = self.read_position()
                arrived = position == n
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
            arrived, elapsed = self._goto(n)
            if arrived:
                self.current_filter = n
                console_ui.log(f"Filter {n} reached in {elapsed:.1f} s after "
                               f"reinitialising the wheel")
                return True
            position = self.read_position()
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
        return False

    def home(self) -> None:
        """Send the wheel back to its home position."""
        self._home()

    def set_shutter(self, open: bool) -> None:
        val = 1 if open else 0
        self._flush()
        started = monotonic()
        self._ser.write((f"d={val}" + chr(13)).encode())
        sleep(0.1)
        self._ser.write(("GOSUB6" + chr(13)).encode())
        # Read the acknowledgement instead of leaving it in the port: a stray
        # SHTR: answer picked up later, during a move, is one more way for an
        # arrival to be missed.
        deadline = monotonic() + _ACK_TIMEOUT
        while monotonic() < deadline:
            line = self._read_line(deadline)
            if line is None:
                break
            if line.strip().upper().startswith("SHTR:"):
                break
        remaining = _SHUTTER_SETTLE - (monotonic() - started)
        if remaining > 0:
            sleep(remaining)
        self.shutter_open = bool(open)
