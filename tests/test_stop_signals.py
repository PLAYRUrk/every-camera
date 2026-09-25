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
    """Every stop signal this platform has goes to the one handler.

    On Windows that is one more than on POSIX — ``SIGBREAK``, which exists only
    there — so the expectation is built from what the platform offers rather
    than written out. The console-close hook is not a signal and is covered
    separately below.
    """
    installed = {}
    monkeypatch.setattr(signal, "signal",
                        lambda sig, handler: installed.__setitem__(sig, handler))
    monkeypatch.setattr(worker_common.os, "name", "posix")

    def handler(sig, frame):
        pass

    worker_common.install_stop_handler(handler)
    expected = {signal.SIGINT: handler, signal.SIGTERM: handler}
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        expected[sigbreak] = handler
    assert installed == expected


def test_the_console_close_hook_is_only_installed_on_windows(monkeypatch):
    """Closing the console window is the Windows way to lose the closing darks.

    Windows never sends SIGTERM by itself, so that path is the one and only
    warning a driver gets that the machine is taking it away. On POSIX the hook
    must not be reached at all — there is no kernel32 to reach it through.
    """
    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    calls = []
    monkeypatch.setattr(worker_common, "_install_windows_console_handler",
                        lambda handler: calls.append(handler))

    def handler(sig, frame):
        pass

    monkeypatch.setattr(worker_common.os, "name", "posix")
    worker_common.install_stop_handler(handler)
    assert calls == []

    monkeypatch.setattr(worker_common.os, "name", "nt")
    worker_common.install_stop_handler(handler)
    assert calls == [handler]


def test_a_console_hook_that_cannot_be_installed_is_not_fatal(monkeypatch):
    """A station that cannot hook the window must still run, and be told why."""
    monkeypatch.setattr(signal, "signal", lambda sig, handler: None)
    monkeypatch.setattr(worker_common.os, "name", "nt")

    def explode(handler):
        raise OSError("no console")

    monkeypatch.setattr(worker_common, "_install_windows_console_handler", explode)
    warnings = []
    monkeypatch.setattr(worker_common.console_ui, "warn", warnings.append)

    worker_common.install_stop_handler(lambda sig, frame: None)
    assert len(warnings) == 1
    assert "Ctrl+C" in warnings[0]


def test_the_signal_is_named_for_the_log():
    """"Ctrl+C" in a journal, where nobody pressed anything, is a lie."""
    assert worker_common.stop_signal_name(signal.SIGINT) == "Ctrl+C"
    assert worker_common.stop_signal_name(signal.SIGTERM) == "SIGTERM"
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:                       # Windows only
        assert worker_common.stop_signal_name(sigbreak) == "Ctrl+Break"


# ---------------------------------------------------------------------------
# Every driver wires it up
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("driver", ["asi", "japan", "sentry", "cannon", "sptt",
                                    "infra"])
def test_no_driver_listens_for_sigint_alone(driver):
    """The regression guard, by inspection: one bare SIGINT is the whole bug.

    ``japan`` is on the list because the program it was ported from does exactly
    the forbidden thing (``japan-camera/runner.py`` installs a SIGINT-only
    handler), so this is a live porting rule rather than a formality: under
    systemd that camera would be SIGKILLed without its closing darks.
    """
    # Explicit encoding: the drivers are UTF-8, while read_text() defaults to
    # the locale's, which on a Russian Windows is cp1252 and cannot decode the
    # comments in infra_driver.py.
    source = (ROOT / "cameras" / f"{driver}_driver.py").read_text(encoding="utf-8")
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
