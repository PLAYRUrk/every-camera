"""The SmartMotor filter wheel on the wire, with the clock under test control.

What these tests exist for: a move that is not confirmed files the frame with no
filter tag — ``_none_`` in the name, ``FILTER = 0`` in the header — and nothing
else says anything is wrong. A whole night can go that way, so every route from
"the answer did not arrive on time" to "the position is unknown" is pinned down
here, including the ones that must *not* end there.

Real time cannot do that: a test that sleeps is slow, and flaky at exactly the
margins that matter. So the port has a virtual clock — a blocking read advances
it, waking early when a byte lands, as a real one does — and ``monotonic`` and
``sleep`` in the module are pointed at it. "Answers 250 ms late" is then an
exact and fast statement, and so is "stopped answering altogether".
"""
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.common import filterwheel                        # noqa: E402
from cameras.common.filterwheel import FilterWheel, HOME      # noqa: E402


class FakeController:
    """A serial port whose answers arrive on a schedule the test decides.

    The knobs are the things that were invisible to the driver and cost a night
    of filter tags: how late an answer is (``latency``), whether the wheel
    actually turns (``moves``), how long that takes (``move_seconds``), and
    whether the controller is answering at all yet (``silent_until``, ``deaf``).
    """

    def __init__(self, *, latency=0.05, terminator=b"\r", position=HOME,
                 moves=True, move_seconds=0.0, silent_until=0.0, deaf=False,
                 revive_on_reopen=False):
        self.now = 0.0
        self.latency = latency
        self.terminator = terminator
        self.position = position          # where the wheel really is
        self.moves = moves                # does a goto actually turn the wheel
        self.move_seconds = move_seconds  # how long that takes
        self.silent_until = silent_until  # answers nothing before this moment
        self.deaf = deaf                  # answers nothing for now
        self.revive_on_reopen = revive_on_reopen   # ...until the port is rebuilt
        self.is_open = True
        self.timeout = filterwheel._CHUNK_TIMEOUT
        self.written = []
        self.opens = 1                    # how many times the port was created
        self._pending = []                # (arrival time, bytes)
        self._out = bytearray()
        self._arrives_at = None           # when the goto in flight completes
        self._target = None

    # -- the test's side -----------------------------------------------------
    def say(self, text, *, delay=None):
        """Queue one answer, arriving ``delay`` seconds from now."""
        when = self.now + (self.latency if delay is None else delay)
        self._pending.append((when, text.encode() + self.terminator))

    def advance(self, seconds):
        self.now += seconds
        self._deliver()

    def _settle(self):
        if self._arrives_at is not None and self.now >= self._arrives_at:
            self.position = self._target
            self._arrives_at = None

    def _deliver(self):
        self._settle()
        ready = [item for item in self._pending if item[0] <= self.now]
        self._pending = [item for item in self._pending if item[0] > self.now]
        for _, payload in sorted(ready):
            self._out += payload

    # -- the driver's side ---------------------------------------------------
    @property
    def in_waiting(self):
        self._deliver()
        return len(self._out)

    def write(self, data):
        text = data.decode(errors="ignore").strip()
        self.written.append(text)
        if self.deaf or self.now < self.silent_until:
            if text.startswith("g=") and self.moves:
                self._target = int(text[2:])
                self._arrives_at = self.now + self.move_seconds
            return len(data)
        if text.startswith("g="):
            if self.moves:
                self._target = int(text[2:])
                self._arrives_at = self.now + self.move_seconds
        elif text == "GOSUB4":
            self._settle()
            if self.position is not None:
                self.say(f"FILT:{self.position}")
        elif text == "GOSUB5":
            self.position = HOME
            self._arrives_at = None
            self.say("HOME OK")
        elif text == "GOSUB6":
            self.say("SHTR:1")
        return len(data)

    def read(self, size=1):
        self._deliver()
        if not self._out:
            # A read that finds nothing blocks until a byte arrives, or for the
            # port timeout, whichever comes first — that is where the driver
            # spends its waiting, so that is where the clock moves on. Waking on
            # the byte matters: a real answer lands in one burst, and a model
            # that always slept the whole timeout would make the old code look
            # broken even against a controller that answered instantly.
            arriving = min((when for when, _ in self._pending), default=None)
            if arriving is not None and arriving <= self.now + self.timeout:
                self.advance(max(0.0, arriving - self.now))
            else:
                self.advance(self.timeout)
        chunk = bytes(self._out[:size])
        del self._out[:len(chunk)]
        return chunk

    def reset_input_buffer(self):
        self._deliver()
        del self._out[:]

    def close(self):
        self.is_open = False


@pytest.fixture
def wired(monkeypatch):
    """Build a wheel on a fake controller, with the module clock following it."""

    def build(**kwargs):
        port = FakeController(**kwargs)
        monkeypatch.setattr(filterwheel, "monotonic", lambda: port.now)
        monkeypatch.setattr(filterwheel, "sleep", port.advance)
        wheel = FilterWheel("fake", 9600, move_timeout=8.0, poll_interval=0.15)
        wheel._ser = port

        def reopen():
            port.opens += 1
            port.is_open = True
            if port.revive_on_reopen:
                port.deaf = False
                port.silent_until = 0.0
            del wheel._rx[:]

        monkeypatch.setattr(wheel, "_open_port", reopen)
        wheel.current_filter = HOME
        return wheel, port

    return build


# -- reading the wire --------------------------------------------------------
def test_an_answer_slower_than_the_poll_window_is_still_seen(wired):
    # 250 ms of latency against a 150 ms poll window: the answer never fits the
    # window it was asked in, and is only ever read one poll late.
    wheel, port = wired(latency=0.25, move_seconds=0.3)
    assert wheel.select(5) is True
    assert wheel.current_filter == 5


def test_an_answer_split_across_two_reads_is_reassembled(wired):
    wheel, port = wired(moves=False, position=None)
    port._pending.append((0.05, b"FILT:"))
    port._pending.append((0.12, b"5\r"))
    assert wheel.read_position() == 5


def test_a_backlog_of_stale_answers_does_not_hide_the_arrival(wired):
    # The controller is running several answers behind, so every answer read
    # belongs to a poll sent long before it.
    wheel, port = wired(latency=0.55, move_seconds=0.4)
    assert wheel.select(5) is True
    assert wheel.current_filter == 5


def test_the_answer_for_the_old_position_is_not_mistaken_for_arrival(wired):
    # The wheel is stuck at 2 and says so; that must not read as arrival at 5.
    wheel, port = wired(moves=False, position=2)
    assert wheel.select(5) is False


def test_a_shutter_ack_in_flight_is_not_read_as_a_position(wired):
    wheel, port = wired(moves=False, position=3)
    port._pending.append((0.02, b"SHTR:1\r"))
    assert wheel.read_position() == 3


@pytest.mark.parametrize("terminator", [b"\r", b"\r\n"])
def test_both_cr_and_crlf_terminated_answers_are_read(wired, terminator):
    wheel, port = wired(moves=False, position=4, terminator=terminator)
    assert wheel.read_position() == 4


# -- what a move does about failure ------------------------------------------
def test_the_move_returns_the_instant_the_wheel_arrives(wired):
    # Not after a fixed wait: captures are timed to land on round seconds, and a
    # move that returned late would push every one of them.
    wheel, port = wired(move_seconds=1.0, latency=0.05)
    assert wheel.select(5) is True
    assert port.now < 2.0


def test_a_move_that_times_out_asks_the_controller_where_it_is(wired):
    # Silence while the move runs, an answer once it is over: the wheel did
    # arrive, and calling that a failure costs the frame its filter tag for
    # nothing.
    wheel, port = wired(move_seconds=0.5, silent_until=9.0)
    assert wheel.select(5) is True
    assert wheel.current_filter == 5


def test_a_wheel_that_never_answers_is_reinitialised_then_reported_unknown(wired):
    wheel, port = wired(deaf=True)
    assert wheel.select(5) is False
    assert port.opens > 1, "the link should have been rebuilt before giving up"
    assert wheel.current_filter is None


def test_a_link_that_died_is_rebuilt_rather_than_lost_for_the_night(wired):
    # The failure that actually costs a night: the controller stops answering —
    # a USB-serial link that reset, a wheel driven into a stall — and nothing
    # short of a new port brings it back. Before, that state lasted until the
    # program was restarted, and every frame in between was filed without a
    # filter.
    wheel, port = wired(deaf=True, revive_on_reopen=True)
    assert wheel.select(5) is True
    assert wheel.current_filter == 5
    assert port.opens > 1


def test_a_wheel_that_talks_but_will_not_turn_is_rehomed(wired):
    # The other way a move fails: the controller answers perfectly, the wheel
    # stays put. Homing is the one thing left to try, and afterwards the wheel
    # really is at home — so that, and not "unknown", is what gets recorded.
    wheel, port = wired(moves=False, position=3)
    assert wheel.select(5) is False
    assert port.opens > 1
    assert wheel.current_filter == HOME


def test_a_position_the_controller_reports_survives_a_failed_rebuild(wired,
                                                                    monkeypatch):
    # A read position is a fact and beats "unknown": the frame is then filed
    # with the filter that is actually in the light path.
    wheel, port = wired(moves=False, position=3)

    def no_port():
        raise OSError("could not open /dev/ttyUSB0")

    monkeypatch.setattr(wheel, "_open_port", no_port)
    assert wheel.select(5) is False
    assert wheel.current_filter == 3


def test_a_move_to_the_filter_already_held_asks_the_wheel_nothing(wired):
    wheel, port = wired()
    wheel.current_filter = 4
    assert wheel.select(4) is True
    assert port.written == []


def test_a_position_outside_the_wheel_is_refused(wired):
    wheel, _ = wired()
    with pytest.raises(ValueError):
        wheel.select(7)


# -- the shutter shares the controller ---------------------------------------
def test_the_shutter_ack_is_consumed_so_the_next_move_is_not_read_one_behind(wired):
    wheel, port = wired(move_seconds=0.2, latency=0.05)
    wheel.set_shutter(True)
    assert wheel.shutter_open is True
    assert wheel.select(5) is True
    assert wheel.current_filter == 5


def test_the_shutter_is_given_time_to_move_before_it_is_reported_shut(wired):
    # The pause used to be an accident of readline() waiting for an LF that
    # never came; the darks of both cameras are taken after it.
    wheel, port = wired(latency=0.05)
    started = port.now
    wheel.set_shutter(False)
    assert port.now - started >= filterwheel._SHUTTER_SETTLE


# -- a controller that is simply not there ------------------------------------
def test_homing_without_an_answer_leaves_the_position_unknown(wired):
    # GOSUB5 used to record HOME whether or not anything came back, which is the
    # one place the module claimed a position it had never read.
    wheel, port = wired(deaf=True)
    assert wheel._home() is None
    assert wheel.current_filter is None


def test_a_rebuild_that_does_not_restore_the_link_says_so(wired):
    wheel, port = wired(deaf=True)
    assert wheel._reinitialise(2) is False


def test_a_controller_that_never_answers_is_written_off(wired):
    wheel, port = wired(deaf=True)
    for _ in range(filterwheel.SILENT_MOVES_BEFORE_LOST):
        assert wheel.select(2) is False
    assert wheel._lost is True


def test_a_written_off_controller_stops_costing_a_minute_a_frame(wired):
    wheel, port = wired(deaf=True)
    started = port.now
    assert wheel.select(2) is False
    full_attempt = port.now - started
    # Two moves, two position reads, a port rebuild and a ten-second homing.
    assert full_attempt > 20.0

    for _ in range(filterwheel.SILENT_MOVES_BEFORE_LOST - 1):
        wheel.select(2)
    assert wheel._lost is True

    started = port.now
    assert wheel.select(3) is False
    # Written off, and not yet due to be asked again: the frame is filed without
    # a filter tag now instead of in three quarters of a minute.
    assert port.now - started < 1.0


def test_a_written_off_controller_is_picked_back_up_when_it_answers(wired):
    wheel, port = wired(deaf=True)
    for _ in range(filterwheel.SILENT_MOVES_BEFORE_LOST):
        wheel.select(2)
    assert wheel._lost is True

    port.deaf = False
    port.advance(filterwheel._LOST_RETRY_SECONDS)
    assert wheel.select(2) is True
    assert wheel.current_filter == 2
    assert wheel._lost is False


def test_a_wheel_that_talks_but_will_not_turn_is_never_written_off(wired):
    # Writing a controller off is about silence only. This one answers every
    # poll and simply refuses to move, which is what the retries and the rebuild
    # above exist for — and they must keep being paid for it.
    wheel, port = wired(moves=False, position=2)
    for _ in range(filterwheel.SILENT_MOVES_BEFORE_LOST + 2):
        assert wheel.select(5) is False
    assert wheel._lost is False
    # And the position stays a reading rather than "unknown": the
    # controller answered every poll, so the wheel is where it says it is.
    assert wheel.current_filter is not None
