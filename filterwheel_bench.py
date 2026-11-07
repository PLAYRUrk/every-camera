#!/usr/bin/env python3
"""Bench checks of the filter wheel controller and the Omega, with no camera attached.

    python filterwheel_bench.py ports
    python filterwheel_bench.py ping --port /dev/ttyUSB0 --count 100
    python filterwheel_bench.py positions --port /dev/ttyUSB0
    python filterwheel_bench.py cycle --port /dev/ttyUSB0 --count 0
    python filterwheel_bench.py omega --port /dev/ttyUSB1 --watch

``--count 0`` runs until Ctrl-C. ``--sim`` needs no hardware. Exit code 0: nothing failed.
"""
import argparse
import csv
import random
import sys
import time
from dataclasses import dataclass, field

from cameras.common import omega as omega_mod
from cameras.common.filterwheel import (FILTER_MAX, FILTER_MIN, FilterWheel,
                                        default_port)
from cameras.common.filterwheel_sim import SimFilterWheel

SOAK_MOVE_EVERY = 20
# Modes that are one full check as they stand; the rest are repeated.
SINGLE_PASS = {"home", "positions", "omega"}
DEFAULT_COUNT = 20


@dataclass
class Tally:
    """Every operation of a run, for the summary and the optional CSV."""
    csv_path: str = ""
    ok: int = 0
    failed: int = 0
    seconds: dict = field(default_factory=dict)
    _handle: object = None
    _writer: object = None

    def __post_init__(self):
        if self.csv_path:
            self._handle = open(self.csv_path, "a", newline="", encoding="utf-8")
            self._writer = csv.writer(self._handle)
            self._writer.writerow(["time", "operation", "ok", "seconds", "detail"])

    def add(self, operation, ok, seconds, detail=""):
        if ok:
            self.ok += 1
            self.seconds.setdefault(operation, []).append(seconds)
        else:
            self.failed += 1
        state = "ok  " if ok else "FAIL"
        print(f"{state} {operation:<12} {seconds:7.3f} s  {detail}", flush=True)
        if self._writer:
            self._writer.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), operation,
                                   int(ok), f"{seconds:.3f}", detail])
            self._handle.flush()

    def summary(self):
        total = self.ok + self.failed
        print(f"\nOperations: {total}, failed: {self.failed}")
        for operation, values in sorted(self.seconds.items()):
            print(f"  {operation:<12} n={len(values):<5} min {min(values):.3f}  "
                  f"mean {sum(values) / len(values):.3f}  max {max(values):.3f} s")
        if self._handle:
            self._handle.close()
        return self.failed == 0


def timed(call):
    started = time.monotonic()
    result = call()
    return result, time.monotonic() - started


def forever_or(count):
    n = 0
    while not count or n < count:
        yield n
        n += 1


def ping(wheel, tally, count, pause):
    for n in forever_or(count):
        position, seconds = timed(wheel.read_position)
        tally.add("ping", position is not None, seconds, f"position {position}")
        time.sleep(pause)


def home(wheel, tally, count, pause):
    for n in forever_or(count):
        _, seconds = timed(wheel.home)
        position = wheel.read_position()
        tally.add("home", position == 0, seconds, f"position {position}")
        time.sleep(pause)


def move(wheel, tally, target, operation="move"):
    start = wheel.current_filter
    ok, seconds = timed(lambda: wheel.select(target))
    reported = wheel.read_position()
    good = bool(ok) and reported == target
    tally.add(operation, good, seconds, f"{start} -> {target}, reports {reported}")
    return good


def positions(wheel, tally, count, pause):
    for n in forever_or(count):
        for target in range(FILTER_MIN, FILTER_MAX + 1):
            move(wheel, tally, target, "positions")
            time.sleep(pause)


def cycle(wheel, tally, count, pause):
    wanted = range(FILTER_MIN, FILTER_MAX + 1)
    for n in forever_or(count):
        move(wheel, tally, wanted[n % len(wanted)], "cycle")
        time.sleep(pause)


def random_moves(wheel, tally, count, pause, rng=random):
    for n in forever_or(count):
        choices = [p for p in range(FILTER_MIN, FILTER_MAX + 1)
                   if p != wheel.current_filter]
        move(wheel, tally, rng.choice(choices), "random")
        time.sleep(pause)


def pair(wheel, tally, count, pause, first, second):
    for n in forever_or(count):
        move(wheel, tally, second if n % 2 else first, "pair")
        time.sleep(pause)


def shutter(wheel, tally, count, pause):
    for n in forever_or(count):
        state = not n % 2
        _, seconds = timed(lambda: wheel.set_shutter(state))
        tally.add("shutter", wheel.shutter_open == state, seconds,
                  "open" if state else "closed")
        time.sleep(pause)
    wheel.set_shutter(False)


def soak(wheel, tally, count, pause):
    """Long link test: polls, with a move now and then, as during a night."""
    for n in forever_or(count):
        position, seconds = timed(wheel.read_position)
        tally.add("soak-poll", position is not None, seconds, f"position {position}")
        if n and n % SOAK_MOVE_EVERY == 0:
            move(wheel, tally, FILTER_MIN + n // SOAK_MOVE_EVERY % FILTER_MAX,
                 "soak-move")
        time.sleep(pause)


def show_ports(args):
    ports = omega_mod.usb_serial_ports()
    if not ports:
        print("No USB-serial ports found.")
        return True
    for port in ports:
        kind = "no answer"
        try:
            with omega_mod.OmegaLink(port, args.baud_omega) as link:
                if omega_mod.parse_reading(link.query(omega_mod.READING)) is not None:
                    kind = "Omega"
        except Exception as exc:
            kind = f"cannot open: {exc}"
        if kind == "no answer":
            try:
                wheel = FilterWheel(port, args.baud)
                wheel._open_port()
                try:
                    if wheel.read_position() is not None:
                        kind = "filter wheel"
                finally:
                    wheel.__exit__()
            except Exception as exc:
                kind = f"cannot open: {exc}"
        print(f"{port}: {kind}")
    return True


def omega(args, tally):
    if args.sim:
        link = omega_mod.SimOmega()
    else:
        link = omega_mod.OmegaLink(args.port or omega_mod.find_port(), args.baud_omega)
    with link:
        for n in forever_or(0 if args.watch else args.count):
            values, seconds = timed(link.read_all)
            detail = ", ".join(f"{k}={v}" for k, v in (values or {}).items())
            tally.add("omega", values is not None, seconds, detail or "no reading")
            if args.watch:
                time.sleep(args.pause or 2.0)


MODES = {"ping": ping, "home": home, "positions": positions, "cycle": cycle,
         "random": random_moves, "shutter": shutter, "soak": soak}


def parser():
    p = argparse.ArgumentParser(description="Filter wheel and Omega bench checks")
    p.add_argument("mode", choices=[*MODES, "pair", "omega", "ports"])
    p.add_argument("values", nargs="*", type=int, help="pair: the two positions")
    p.add_argument("--port", default="", help=f"default {default_port()} for the "
                                              "wheel, a search for the Omega")
    p.add_argument("--baud", type=int, default=9600, help="wheel speed")
    p.add_argument("--baud-omega", type=int, default=9600)
    p.add_argument("--move-timeout", type=float, default=8.0)
    p.add_argument("--count", type=int, default=None,
                   help="repetitions, 0: until Ctrl-C")
    p.add_argument("--pause", type=float, default=0.0, help="seconds between operations")
    p.add_argument("--watch", action="store_true", help="omega: keep reading")
    p.add_argument("--sim", action="store_true", help="use the simulators")
    p.add_argument("--csv", default="", help="append every operation to this file")
    return p


def run(args, tally):
    if args.mode == "omega":
        omega(args, tally)
        return
    if args.mode == "pair" and len(args.values) != 2:
        raise SystemExit("pair needs two positions, e.g.: pair 1 4")
    wheel = (SimFilterWheel() if args.sim else
             FilterWheel(args.port or default_port(), args.baud, args.move_timeout))
    with wheel:
        if args.mode == "pair":
            pair(wheel, tally, args.count, args.pause, *args.values)
        else:
            MODES[args.mode](wheel, tally, args.count, args.pause)


def main(argv=None):
    args = parser().parse_args(argv)
    if args.count is None:
        args.count = 1 if args.mode in SINGLE_PASS else DEFAULT_COUNT
    if args.mode == "ports":
        return 0 if show_ports(args) else 1
    tally = Tally(args.csv)
    try:
        run(args, tally)
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        tally.add(args.mode, False, 0.0, str(exc))
    return 0 if tally.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
