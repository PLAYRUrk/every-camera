"""
Shared plumbing for the camera workers (console and Qt alike).

Every driver used to carry its own near-identical copy of the status publishing
and the error reporting. This module holds one copy of each, so a fix lands
everywhere.

One call to :meth:`WorkerBus.publish_status` reaches every reader there is:

    CameraService  -> the frame server, so /api/status and everything past it
    status file    -> ~/.every_camera/status/{pid}.json, watched by sentinel.py
                      and read by a monitor running on the same machine
"""
import json
import os
import signal
import time

from datetime import datetime as dt

import alerts

import console_ui

from utils import (
    STATUS_MIN_INTERVAL, write_status_file, cleanup_stale_status_files,
)

# Status name published while a camera is up for focusing only.
SETUP_STATUS = "setup"


def announce_setup_mode(reason=None):
    """Explain why the camera is running without a schedule.

    A missing or empty schedule used to abort the program, which made a newly
    installed camera impossible to focus: focus_app.py needs the driver running
    to get live frames from it. Now it is an ordinary state, and this is the one
    place that words it.
    """
    console_ui.warn(f"Setup mode: {reason or 'requested with --setup'}.")
    console_ui.log("No scheduled captures will be taken. Connect focus_app.py "
                   "to adjust focus and camera parameters; add a schedule and "
                   "restart to begin measuring.")


def empty_schedule_reason(section, configured, errors=()):
    """Word an empty schedule the way the operator can act on it.

    A schedule that *was* configured and came out empty is a different problem
    from one that was never written, and telling the operator to "add slots"
    when the slots are right there in front of them sends them looking in the
    wrong place. That happens most often when ``mode`` and the slots disagree —
    cycle modes need a ``delta``, ``sun`` mode needs ``seconds`` — so the first
    rejection is quoted here rather than left in the warnings above.
    """
    # No trailing full stop: the callers punctuate the sentence they build.
    if configured:
        detail = f" First problem: {errors[0].rstrip('.')}" if errors else ""
        return (f"every slot of the {section} schedule was rejected, so there "
                f"is nothing left to run.{detail}")
    return (f"the {section} schedule is empty — add slots to {section}.schedule "
            f"in config.json or point {section}.schedule_file at a file")


def publish_current_params(service, snapshot):
    """Hand ``focus_app`` the camera's parameter values, as of now.

    ``snapshot`` is a callable returning the driver's current values, called
    here so that a camera that cannot answer costs a warning rather than a
    broken status tick.

    Every driver used to publish this once at startup and again only after a
    change *it had been asked for*, which made it a lie for most of a night: a
    schedule moves the exposure, the filter and the ASI shutter by itself, so an
    observer connecting later was shown the values the process had started with.
    Drivers therefore call this on their status cadence. It must stay cheap and
    hardware-free — the values it reads are ones the driver already caches.
    """
    if service is None:
        return
    try:
        service.set_current_params(snapshot())
    except Exception as exc:
        console_ui.warn(f"Could not publish current parameters: {exc}")


def publish_schedule_state(service, active):
    """Say whether the camera is inside its measuring cycle right now.

    ``focus_app`` asks for this before it connects: taking a camera that is
    measuring costs the operator frames and has to be agreed to, while taking
    an idle one — daytime, outside the schedule's intervals, or a station with
    no schedule at all — costs nothing and must not nag.

    Drivers call this on the status cadence they already have, so a camera that
    crosses into its observing window while the tool is open reports it.
    """
    if service is None:
        return
    try:
        service.set_schedule_active(active)
    except Exception:
        pass


def serving_focus_hold(service, held):
    """Confirm to observers that the worker has paused (or resumed).

    Separate from :func:`publish_schedule_state` because the two answer
    different questions: that one is "would connecting interrupt anything",
    this one is "did the interruption actually take effect".
    """
    if service is None:
        return
    try:
        service.note_hold_effective(held)
    except Exception:
        pass


class WorkerBus:
    """Status publishing for one camera worker.

    Two concerns the drivers used to duplicate:

    * **Throttling.** Outside the schedule a worker loops twice a second. It
      may call :meth:`publish_status` every iteration; only one publication per
      ``STATUS_MIN_INTERVAL`` actually goes out, plus every status *change*.
    * **One call, every reader.** A status goes to the frame server, for
      ``/api/status`` and everything downstream of it, and to the status file
      under ``~/.every_camera/status``, which is what the sentinel watches and
      what a monitor on this machine reads.

    It used to publish to a broker as well, and to carry frames there —
    downscaled to fit a payload ceiling, one at a time, on request. All of
    that is gone: the frame server serves the archive, the live stream and
    the parameters over the same network, without a broker to configure or a
    240 KB limit to squeeze a frame into.
    """

    def __init__(self, camera_type, instance_name, status_dir,
                 service=None, status_suffix="", node_name=""):
        self.camera_type = camera_type
        self.instance_name = instance_name
        self.status_dir = status_dir
        # Stamped onto every status snapshot so the monitor can say which
        # machine a camera is on without every driver remembering to add it.
        self.node_name = node_name or ""
        self._service = service

        # The GUI can run several cameras in one process, so it passes a
        # suffix to keep their status files apart.
        self.status_path = os.path.join(
            status_dir, f"{os.getpid()}{status_suffix}.json")
        self._last_status_name = None
        self._last_status_at = 0.0

    # ------------------------------------------------------------------
    def prepare_status_dir(self):
        """Create the status dir and drop files left by dead processes."""
        os.makedirs(self.status_dir, exist_ok=True)
        removed = cleanup_stale_status_files(self.status_dir)
        if removed:
            console_ui.log(f"Removed {removed} stale status file(s) from "
                           f"{self.status_dir}")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def publish_status(self, payload, force=False):
        """Write the status file and publish it, subject to throttling.

        ``force=True`` bypasses the throttle (used for terminal states).
        Returns True if the status actually went out.
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
            write_status_file(self.status_path, payload)
        except Exception as exc:
            console_ui.warn(f"Could not write status file: {exc}")
        return True

    def publish_error(self, kind, error, ts_iso=None):
        """Record something that went wrong, where observers will see it.

        This used to publish to the broker and nowhere else, which meant that
        on a station with the broker off — the default — every one of these
        calls did nothing at all. The frame that returned no image, the write
        that failed, the filter that was never reached: all reported, all
        discarded. Now it reaches ``/api/status``, which is what the monitor
        draws and what an alert letter quotes.
        """
        if self._service is None:
            return
        try:
            self._service.note_error(kind, error, when=ts_iso)
        except Exception:
            pass

    def shutdown(self, reason="stopped normally"):
        """Remove the status file, having first said this was on purpose.

        The clean-exit marker goes down *before* the status file goes away, and
        that order is the whole point of it: the sentinel decides a worker
        crashed by finding a status file with a dead process behind it, and
        without a marker written first, every orderly stop — an operator at the
        console, ``systemctl stop``, a reboot — would reach it as a crash and
        wake somebody up for nothing.
        """
        try:
            alerts.note_clean_exit(self.status_path, reason)
        except Exception:
            pass
        try:
            os.remove(self.status_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass



def install_stop_handler(handler):
    """Route both stop signals to ``handler``: Ctrl+C and ``systemctl stop``.

    Every driver used to listen for SIGINT alone, which is fine at a terminal
    and wrong under a service manager: ``systemctl stop`` and a reboot send
    SIGTERM, whose default disposition kills the process outright. For a camera
    that means the closing dark frames are lost — and, on the ASI, that the
    sensor never runs its warm-up, because that lives in the shutdown path the
    process no longer reaches. The two signals mean the same thing to us, so
    they get the same handler.

    Python delivers signals on the main thread only, so this has to be called
    from there; every driver does, from its ``run_*`` entry point.
    """
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def stop_signal_name(sig):
    """What to call the signal in a log line.

    "Ctrl+C" is what the operator did at a terminal, and nonsense in a journal
    where the request came from ``systemctl stop``.
    """
    return "Ctrl+C" if sig == signal.SIGINT else "SIGTERM"


def run_focus_iteration(service, grab, on_error=None):
    """Grab one live frame for the focus tool and hand it to the service.

    Any failure is counted; after ``FOCUS_MAX_ERRORS`` in a row the service
    turns focus mode off by itself, so a broken camera can never keep the
    worker spinning in a failing capture loop.
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
