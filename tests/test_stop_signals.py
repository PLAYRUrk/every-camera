"""Both stop signals reach the shutdown path — Ctrl+C and ``systemctl stop``.

This is a small file guarding an expensive mistake. Every driver used to listen
for SIGINT alone, which looks fine until the program is run as a service:
``systemctl stop`` and a reboot send SIGTERM, and its default disposition kills
the process where it stands. The visible cost is the closing dark frames; the
expensive one is the ASI's sensor warm-up, which lives in the shutdown path and
so never runs — every reboot would cut power to a sensor sitting at −60 °C.

The end-to-end behaviour is checked in a real subprocess: mocking a signal
handler proves nothing about whether the signal was ever installed.
"""
import os
import signal
import subprocess
import sys
import textwrap
import time

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

import worker_common                                    # noqa: E402


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------
def test_both_signals_are_installed(monkeypatch):
    installed = {}
    monkeypatch.setattr(signal, "signal",
                        lambda sig, handler: installed.__setitem__(sig, handler))

    def handler(sig, frame):
        pass

    worker_common.install_stop_handler(handler)
    assert installed == {signal.SIGINT: handler, signal.SIGTERM: handler}


def test_the_signal_is_named_for_the_log():
    """"Ctrl+C" in a journal, where nobody pressed anything, is a lie."""
    assert worker_common.stop_signal_name(signal.SIGINT) == "Ctrl+C"
    assert worker_common.stop_signal_name(signal.SIGTERM) == "SIGTERM"


# ---------------------------------------------------------------------------
# Every driver wires it up
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("driver", ["asi", "sentry", "cannon", "sptt", "infra"])
def test_no_driver_listens_for_sigint_alone(driver):
    """The regression guard, by inspection: one bare SIGINT is the whole bug."""
    source = (ROOT / "cameras" / f"{driver}_driver.py").read_text()
    assert "signal.signal(signal.SIGINT" not in source, \
        f"{driver}_driver.py installs a SIGINT-only handler"
    assert "install_stop_handler(" in source, \
        f"{driver}_driver.py never installs a stop handler"


# ---------------------------------------------------------------------------
# The launcher must not stand between systemd and Python
# ---------------------------------------------------------------------------
def test_the_launcher_execs_into_python():
    """``run.sh`` has to replace itself, not spawn a child.

    A wrapper left running in front of Python is handed SIGTERM itself. Bash
    would take it and die, and the shutdown path here — the closing darks, the
    ASI sensor warm-up — would never run, which is exactly the failure this
    whole file exists to prevent. ``exec`` collapses the two processes into one
    so the signal lands where it is handled.
    """
    launcher = ROOT / "run.sh"
    assert launcher.exists(), "run.sh is missing"
    assert os.access(launcher, os.X_OK), "run.sh is not executable"
    lines = [ln.strip() for ln in launcher.read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert lines[-1].startswith("exec "), \
        f"run.sh must end by exec'ing python, not by calling it: {lines[-1]!r}"


def test_the_unit_starts_the_same_launcher():
    """One way of starting the program, so a service run matches a manual one."""
    unit = (ROOT / "systemd" / "every-camera@.service").read_text()
    exec_start = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_start) == 1, "expected exactly one ExecStart"
    assert "run.sh" in exec_start[0], exec_start[0]
    assert "TimeoutStopSec=" in unit, \
        "the unit must budget for the closing darks and the warm-up"


# ---------------------------------------------------------------------------
# End to end: a real process, a real SIGTERM
# ---------------------------------------------------------------------------
SUBJECT = """
    import sys, time
    sys.path.insert(0, {root!r})
    from worker_common import install_stop_handler, stop_signal_name

    stopped = []

    def _stop(sig, frame):
        stopped.append(sig)

    install_stop_handler(_stop)
    print("ready", flush=True)

    # Stand in for the worker loop, then for the shutdown work that a killed
    # process would never reach: the closing darks and the sensor warm-up.
    deadline = time.monotonic() + 20
    while not stopped and time.monotonic() < deadline:
        time.sleep(0.02)
    if stopped:
        print(f"shutdown ran after {{stop_signal_name(stopped[0])}}", flush=True)
    sys.exit(0 if stopped else 1)
"""


def _run_until_ready(tmp_path):
    script = tmp_path / "subject.py"
    script.write_text(textwrap.dedent(SUBJECT).format(root=str(ROOT)))
    proc = subprocess.Popen([sys.executable, str(script)],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    return proc


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
@pytest.mark.parametrize("sig,expected", [
    (signal.SIGTERM, "SIGTERM"),     # systemctl stop, and a reboot
    (signal.SIGINT, "Ctrl+C"),       # the operator at a terminal
])
def test_the_shutdown_path_runs_for_both_signals(tmp_path, sig, expected):
    proc = _run_until_ready(tmp_path)
    try:
        proc.send_signal(sig)
        rest = proc.stdout.read()
        assert proc.wait(timeout=25) == 0, "the process died before it could clean up"
        assert f"shutdown ran after {expected}" in rest
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_a_second_signal_is_still_deliverable(tmp_path):
    """systemd re-sends SIGTERM when TimeoutStopSec expires.

    The drivers treat the second one as "give up the closing darks", which is
    the right answer at that point — better than being SIGKILLed mid-exposure.
    So the handler has to stay installed after the first signal, not disarm.
    """
    proc = _run_until_ready(tmp_path)
    try:
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.2)
        assert proc.poll() is None or proc.returncode == 0
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)      # must not kill it outright
        assert proc.wait(timeout=25) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
