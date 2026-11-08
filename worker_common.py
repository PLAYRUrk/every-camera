"""Status publishing and stop handling shared by the camera workers.

One :meth:`WorkerBus.publish_status` reaches every reader: CameraService for
/api/status, and ~/.every_camera/status/{pid}.json, watched by sentinel.py and read by a
monitor on the same machine.
"""
import json
import os
import signal
import threading
import time

from datetime import datetime as dt

import alerts

import console_ui
from telemetry import TelemetryLog

from utils import (
    STATUS_MIN_INTERVAL, write_status_file, cleanup_stale_status_files,
)

SETUP_STATUS = "setup"


def announce_setup_mode(reason=None):
    """Setup mode exists so a new camera can be focused: focus_app.py needs the driver
    running to get live frames from it.
    """
    console_ui.warn(f"Setup mode: {reason or 'requested with --setup'}.")
    console_ui.log("No scheduled captures will be taken. Connect focus_app.py "
                   "to adjust focus and camera parameters; add a schedule and "
                   "restart to begin measuring.")


def empty_schedule_reason(section, configured, errors=()):
    """A configured schedule that came out empty is a different problem from one never
    written, so "add slots" would mislead.

    Usually ``mode`` and the slots disagree (cycle modes need ``delta``, ``sun`` mode
    needs ``seconds``), so the first rejection is quoted.
    """
    # No trailing full stop: the callers punctuate the sentence they build.
    if configured:
        detail = f" First problem: {errors[0].rstrip('.')}" if errors else ""
        return (f"every slot of the {section} schedule was rejected, so there "
                f"is nothing left to run.{detail}")
    return (f"the {section} schedule is empty — add slots to {section}.schedule "
            f"in config.json or point {section}.schedule_file at a file")


def publish_current_params(service, snapshot):
    """``snapshot`` runs here so a camera that cannot answer costs a warning, not a
    broken status tick.

    Drivers call this on their status cadence, because a schedule moves the exposure,
    the filter and the ASI shutter by itself. It must stay cheap and hardware-free: the
    values it reads are ones the driver already caches.
    """
    if service is None:
        return
    try:
        service.set_current_params(snapshot())
    except Exception as exc:
        console_ui.warn(f"Could not publish current parameters: {exc}")


def publish_schedule_state(service, active):
    """``focus_app`` asks before connecting: taking a measuring camera costs frames and
    needs agreement, while taking an idle one must not nag.

    Drivers call this on their status cadence, so a camera crossing into its observing
    window while the tool is open reports it.
    """
    if service is None:
        return
    try:
        service.set_schedule_active(active)
    except Exception:
        pass


def serving_focus_hold(service, held):
    """Whether the interruption took effect; :func:`publish_schedule_state` says whether
    connecting would interrupt anything.
    """
    if service is None:
        return
    try:
        service.note_hold_effective(held)
    except Exception:
        pass


class WorkerBus:
    """Outside the schedule a worker loops twice a second and may call
    :meth:`publish_status` every time: one publication per ``STATUS_MIN_INTERVAL`` goes
    out, plus every status *change*.
    """

    def __init__(self, camera_type, instance_name, status_dir,
                 service=None, status_suffix="", node_name=""):
        self.camera_type = camera_type
        self.instance_name = instance_name
        self.status_dir = status_dir
        # Stamped here so no driver has to remember to add it.
        self.node_name = node_name or ""
        self._service = service

        # The GUI can run several cameras in one process, so it passes a
        # suffix to keep their status files apart.
        self.status_path = os.path.join(
            status_dir, f"{os.getpid()}{status_suffix}.json")
        self._last_status_name = None
        self._last_status_at = 0.0
        self._telemetry = TelemetryLog(instance_name)

    def prepare_status_dir(self):
        os.makedirs(self.status_dir, exist_ok=True)
        # Markers are keyed by PID; one left by an earlier process with this PID would
        # make the sentinel misread this worker's end.
        key = alerts.marker_key(self.status_path)
        for directory in (alerts.EXIT_DIR, alerts.CRASH_DIR):
            try:
                os.remove(os.path.join(directory, f"{key}.json"))
            except OSError:
                pass
        removed = cleanup_stale_status_files(self.status_dir)
        if removed:
            console_ui.log(f"Removed {removed} stale status file(s) from "
                           f"{self.status_dir}")

    def publish_status(self, payload, force=False):
        """``force=True`` bypasses the throttle, for terminal states. Returns True if it
        went out.
        """
        if self.node_name and not payload.get("node_name"):
            payload = dict(payload, node_name=self.node_name)
        status_name = payload.get("status")
        now = time.monotonic()
        changed = status_name != self._last_status_name
        if not force and not changed and (now - self._last_status_at) < STATUS_MIN_INTERVAL:
            return False
        self._last_status_name = status_name
        self._last_status_at = now

        if self._service is not None:
            try:
                self._service.publish_status(payload)
            except Exception:
                pass
        try:
            write_status_file(self.status_path, self._with_tunnel(payload))
        except Exception as exc:
            console_ui.warn(f"Could not write status file: {exc}")
        self._telemetry.write(payload)
        return True

    def _with_tunnel(self, payload):
        """Only the status file, which only this machine reads, carries the token:
        ``/api/status`` is served through the tunnel itself.
        """
        url = getattr(self._service, "tunnel_url", None)
        if not url:
            return payload
        return dict(payload, tunnel_url=url,
                    tunnel_token=getattr(self._service, "tunnel_token", None))

    def publish_error(self, kind, error, ts_iso=None):
        if self._service is None:
            return
        try:
            self._service.note_error(kind, error, when=ts_iso)
        except Exception:
            pass

    def shutdown(self, reason="stopped normally", failed=None):
        """The clean-exit marker goes down *before* the status file goes away.

        The sentinel reads a status file with a dead process behind it as a crash, and
        without the marker every orderly stop would wake somebody up for nothing.
        ``failed`` (a reason) means the worker gave up: that is reported as a crash.
        """
        try:
            if failed:
                alerts.note_crash(self.status_path, str(failed))
            else:
                alerts.note_clean_exit(self.status_path, reason)
        except Exception:
            pass
        try:
            os.remove(self.status_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        _shutdown_done.set()


# Kept alive for the life of the process: ``SetConsoleCtrlHandler`` stores the
# pointer, and a garbage-collected callback is a crash as the machine shuts down.
_console_ctrl_handler = None

# Windows console events meaning "this process is going away". CTRL_C and CTRL_BREAK
# arrive as signals; these three do not.
_WINDOWS_CLOSE_EVENTS = (2, 5, 6)      # CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN
# Windows ends the process as soon as the console callback returns, so the callback
# waits for the worker's shutdown, within the roughly five seconds it is given.
_shutdown_done = threading.Event()
_CONSOLE_CLOSE_GRACE = 4.5


def _install_windows_console_handler(handler):
    """Gives the closing darks a chance when the console window is shut.

    Windows never sends SIGTERM itself, so closing the console, logging off or shutting
    down would end the process mid-exposure, shutter open, with no closing darks.

    The callback runs on an OS thread, so it only calls the handler the signals call,
    which sets an event. Windows still ends the process after a timeout (about five
    seconds for CTRL_CLOSE): a long closing dark run may be cut short.
    """
    global _console_ctrl_handler
    import ctypes

    prototype = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def on_console_event(event):
        if event in _WINDOWS_CLOSE_EVENTS:
            handler(signal.SIGTERM, None)
            _shutdown_done.wait(_CONSOLE_CLOSE_GRACE)
            return 1
        return 0

    _console_ctrl_handler = prototype(on_console_event)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_ctrl_handler, True)


def install_stop_handler(handler):
    """Routes Ctrl+C and ``systemctl stop`` to ``handler``.

    SIGTERM's default kills the process outright, losing the closing darks and, on the
    ASI, the sensor warm-up, which lives in the shutdown path.

    On Windows ``SIGBREAK`` and the console-close events lead here too; nothing in
    Windows itself sends SIGTERM. Python delivers signals on the main thread only, so
    call this from there.
    """
    signals = [signal.SIGINT, signal.SIGTERM]
    # Windows only.
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signals.append(sigbreak)
    for sig in signals:
        signal.signal(sig, handler)
    if os.name == "nt":
        try:
            _install_windows_console_handler(handler)
        except Exception as exc:      # pragma: no cover - Windows only
            console_ui.warn(f"Could not hook the console close button: {exc}. "
                            f"Closing the window will end the run without its "
                            f"closing darks — stop it with Ctrl+C instead.")


def stop_signal_name(sig):
    """A journal must not say "Ctrl+C" when the request came from ``systemctl stop``."""
    if sig == signal.SIGINT:
        return "Ctrl+C"
    if sig == getattr(signal, "SIGBREAK", None):
        return "Ctrl+Break"
    return "SIGTERM"


def run_focus_iteration(service, grab, on_error=None):
    """After ``FOCUS_MAX_ERRORS`` in a row the service turns focus off, so a broken
    camera cannot keep the worker spinning in a failing capture loop.
    """
    try:
        frame = grab()
    except Exception as exc:
        stopped = service.note_focus_error(exc)
        if on_error:
            on_error(exc, stopped)
        return False
    if frame is None:
        return False
    service.clear_focus_errors()
    service.publish_frame(frame, dt.now())
    return True
