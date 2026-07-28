"""
Shared plumbing for the camera workers (console and Qt alike).

Every driver used to carry its own near-identical copy of: the status-file +
retained-MQTT publishing, the frame/error/note publishing envelope, and the
JPEG encoder. This module holds one copy of each, so a fix lands everywhere.

The wire format is unchanged — ``monitor.py`` and ``viewer_app.py`` see exactly
the same topics and JSON keys as before.

Topics (``prefix`` defaults to ``every_camera``):
    {prefix}/{instance}/status          retained status snapshot
    {prefix}/{instance}/frame           frames, errors and progress notes
    {prefix}/{instance}/cmd/#           incoming commands
"""
import json
import os
import time

from datetime import datetime as dt

import console_ui

import frame_archive

from utils import (
    STATUS_MIN_INTERVAL, write_status_file, cleanup_stale_status_files,
)

# HiveMQ's free tier caps messages at ~256 KB — leave headroom for the JSON
# envelope and base64 expansion.
MQTT_MAX_PAYLOAD_BYTES = 240_000

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


class WorkerMqtt:
    """Status and frame publishing for one camera worker.

    Handles three concerns the drivers kept duplicating:

    * **Throttling.** Outside the schedule a worker loops twice a second. It
      may call :meth:`publish_status` every iteration; only one publication per
      ``STATUS_MIN_INTERVAL`` actually goes out, plus every status *change*.
    * **Retained cleanup.** :meth:`shutdown` erases the retained status topic,
      so a stopped camera disappears from monitors instead of being remembered
      by the broker forever.
    * **Payload limits.** Frames are downscaled until they fit the broker cap.
    """

    def __init__(self, camera_type, instance_name, status_dir,
                 mqtt_publisher=None, mqtt_prefix="every_camera",
                 service=None, status_suffix="", node_name=""):
        self.camera_type = camera_type
        self.instance_name = instance_name
        self.status_dir = status_dir
        # Stamped onto every status snapshot so the monitor can say which
        # machine a camera is on without every driver remembering to add it.
        self.node_name = node_name or ""
        self.prefix = mqtt_prefix
        self._mqtt = mqtt_publisher
        self._service = service

        self.status_topic = f"{mqtt_prefix}/{instance_name}/status"
        self.frame_topic = f"{mqtt_prefix}/{instance_name}/frame"
        self.cmd_topic = f"{mqtt_prefix}/{instance_name}/cmd/#"

        # The GUI can run several cameras in one process, so it passes a
        # suffix to keep their status files apart.
        self.status_path = os.path.join(
            status_dir, f"{os.getpid()}{status_suffix}.json")
        self._last_status_name = None
        self._last_status_at = 0.0

    # ------------------------------------------------------------------
    @property
    def enabled(self):
        return self._mqtt is not None

    def prepare_status_dir(self):
        """Create the status dir and drop files left by dead processes."""
        os.makedirs(self.status_dir, exist_ok=True)
        removed = cleanup_stale_status_files(self.status_dir)
        if removed:
            console_ui.log(f"Removed {removed} stale status file(s) from "
                           f"{self.status_dir}")

    def subscribe(self, callback):
        """Subscribe to the instance command topic. Returns True if wired up."""
        if not self._mqtt:
            console_ui.log("No MQTT — remote commands disabled")
            return False
        self._mqtt.subscribe_commands(self.cmd_topic, callback)
        console_ui.log(f"Subscribed to commands: {self.cmd_topic}")
        return True

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
        if self._mqtt:
            try:
                self._mqtt.publish(self.status_topic, json.dumps(payload),
                                   retain=True)
            except Exception:
                pass
        return True

    def shutdown(self):
        """Remove the status file and erase the retained status topic."""
        try:
            os.remove(self.status_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if self._mqtt:
            try:
                self._mqtt.clear_retained(self.status_topic)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------
    def publish_frame_jpeg(self, jpeg_bytes, ts_iso, on_demand=False,
                           params=None, extra=None):
        """Publish already-encoded JPEG bytes."""
        if not self._mqtt:
            return False
        import base64
        body = {
            "camera_type": self.camera_type,
            "instance_name": self.instance_name,
            "status": "ok",
            "format": "jpeg",
            "data": base64.b64encode(jpeg_bytes).decode(),
            "timestamp": ts_iso,
            "on_demand": on_demand,
        }
        if params:
            body["params"] = params
        if extra:
            body.update(extra)
        payload = json.dumps(body)
        if len(payload) > MQTT_MAX_PAYLOAD_BYTES:
            self.publish_error(
                "too_large",
                f"Frame payload {len(payload)} bytes exceeds broker limit",
                ts_iso=ts_iso, on_demand=on_demand)
            console_ui.warn(f"Frame too large ({len(payload)} bytes); not sent")
            return False
        self._mqtt.publish(self.frame_topic, payload, retain=False)
        return True

    def publish_frame_array(self, frame, ts_iso, on_demand=False, params=None,
                            extra=None):
        """Encode a numpy frame (downscaling as needed) and publish it."""
        if not self._mqtt:
            return False
        if isinstance(frame, (bytes, bytearray)):
            return self.publish_frame_jpeg(bytes(frame), ts_iso,
                                           on_demand=on_demand, params=params,
                                           extra=extra)
        jpeg_bytes, w, h = frame_archive.to_jpeg_capped(
            frame, MQTT_MAX_PAYLOAD_BYTES)
        merged = {"width": w, "height": h}
        if extra:
            merged.update(extra)
        return self.publish_frame_jpeg(jpeg_bytes, ts_iso, on_demand=on_demand,
                                       params=params, extra=merged)

    def publish_error(self, status, error, ts_iso=None, on_demand=False):
        if not self._mqtt:
            return
        self._mqtt.publish(self.frame_topic, json.dumps({
            "camera_type": self.camera_type,
            "instance_name": self.instance_name,
            "status": status,
            "error": error,
            "timestamp": ts_iso,
            "on_demand": on_demand,
        }), retain=False)

    def publish_note(self, status, note=""):
        """Publish a progress note (``accepted`` / ``capturing``)."""
        if not self._mqtt:
            return
        self._mqtt.publish(self.frame_topic, json.dumps({
            "camera_type": self.camera_type,
            "instance_name": self.instance_name,
            "status": status,
            "note": note,
            "timestamp": dt.now().isoformat(),
            "on_demand": True,
        }), retain=False)


def parse_command_params(payload):
    """Decode a ``cmd/capture_frame`` payload into a dict.

    Returns ``(params, error)``; ``error`` is a message string when the payload
    is not valid JSON.
    """
    if not payload:
        return {}, None
    try:
        params = json.loads(payload)
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON: {exc}"
    if not isinstance(params, dict):
        return {}, None
    return params, None


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
