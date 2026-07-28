"""
CameraService — the thread-safe boundary between a camera worker and the network.

The frame server (``frame_server.py``) runs in its own daemon threads and must
never touch a camera object directly: a stuck USB read or a hung gphoto2 call
in an HTTP handler would be invisible, and a misbehaving observer could stall
the measurement loop. Everything therefore goes through this object:

    worker thread            CameraService            HTTP threads
    ------------------------ ----------------------- ------------------------
    publish_frame()   ---->  latest frame (1 slot)  ----> latest() / MJPEG
    publish_status()  ---->  status snapshot        ----> /api/status
    take_param_request() <-- pending request (1)    <---- POST /api/params
    focus_active()    <----  focus deadline         <---- POST /api/focus

Rules that keep the measurement loop safe:
  * The worker only ever *writes* here; it never blocks on a network client.
  * Requests are single-slot: a new request replaces an unprocessed one, so a
    flood of HTTP calls can never grow a queue or starve the worker.
  * Focus mode carries a deadline and expires by itself. If the observer's
    program crashes or the network drops, free-running capture stops on its
    own within ``ttl`` seconds.
  * Every network-facing method is non-blocking except ``wait_for_frame()``,
    which always takes a timeout.
"""
import threading
import time
import uuid

from datetime import datetime as dt

import frame_archive

# ---------------------------------------------------------------------------
# Parameter schemas — describe what focus_app.py may edit per camera type.
# The worker can replace these at runtime (Canon reads real choices from the
# camera), but these defaults let the UI render before a camera is connected.
# ---------------------------------------------------------------------------
PARAM_SCHEMAS = {
    "cannon": [
        {"name": "iso", "label": "ISO", "type": "text", "hint": "100, 400, 1600"},
        {"name": "shutterspeed", "label": "Shutter", "type": "text",
         "hint": "1/125, 1, 30"},
        {"name": "aperture", "label": "Aperture", "type": "text", "hint": "5.6"},
        {"name": "whitebalance", "label": "White balance", "type": "text",
         "hint": "Daylight"},
    ],
    "sptt": [
        {"name": "exposure", "label": "Exposure (s)", "type": "float",
         "min": 0.001, "max": 60.0, "step": 0.01},
        {"name": "gain", "label": "Gain", "type": "int", "min": 0, "max": 1023},
        {"name": "binning", "label": "Binning", "type": "choice",
         "choices": [{"value": 0, "label": "1x1"},
                     {"value": 1, "label": "2x2"},
                     {"value": 3, "label": "4x4"}]},
        {"name": "encoding", "label": "Encoding", "type": "choice",
         "choices": [{"value": "12bit", "label": "12 bit"},
                     {"value": "8bit", "label": "8 bit"}]},
    ],
    "infra": [
        {"name": "exposure_us", "label": "Exposure (us)", "type": "float",
         "min": 1.0, "max": 30_000_000.0, "step": 100.0},
        {"name": "gain", "label": "Gain", "type": "int", "min": 0, "max": 120},
        {"name": "roi", "label": "ROI", "type": "choice",
         "choices": [{"value": "1280x1024", "label": "1280 x 1024"},
                     {"value": "1280x256", "label": "1280 x 256"}]},
    ],
    # The ASI driver applies these between exposures, never during one, so the
    # observing programme is never interrupted; the next scheduled slot restores
    # whatever the schedule asks for.
    "asi": [
        {"name": "exposure", "label": "Exposure (s)", "type": "float",
         "min": 0.001, "max": 3600.0, "step": 0.5},
        {"name": "binning", "label": "Binning", "type": "choice",
         "choices": [{"value": 1, "label": "1x1"},
                     {"value": 2, "label": "2x2"},
                     {"value": 4, "label": "4x4"},
                     {"value": 8, "label": "8x8"}]},
        {"name": "gain", "label": "Analog gain", "type": "choice",
         "choices": [{"value": 1, "label": "1 — Low"},
                     {"value": 2, "label": "2 — Medium"},
                     {"value": 3, "label": "3 — High"}]},
        {"name": "filter", "label": "Filter", "type": "choice",
         # Home is where the wheel parks itself on startup. It is a state the
         # form must be able to *show* — otherwise a just-started camera reads
         # as being in whichever filter happens to be first in the list — but
         # not a position to send it to, so it lives in "states", not "choices".
         "states": [{"value": 0, "label": "home (after startup)"}],
         "choices": [{"value": 1, "label": "1 — 557.7 nm"},
                     {"value": 2, "label": "2 — 630.0 nm"},
                     {"value": 3, "label": "3 — OH broadband"},
                     {"value": 4, "label": "4 — 840.0 nm"},
                     {"value": 5, "label": "5 — 846.5 nm"},
                     {"value": 6, "label": "6 — 857.0 nm"}]},
        # The shutter sits on the filter-wheel controller, not the camera. It is
        # shut between runs and for darks, so focusing without a way to open it
        # meant staring at dark frames and wondering what was wrong.
        {"name": "shutter", "label": "Shutter", "type": "bool", "hint": "open",
         "true_label": "open", "false_label": "closed",
         "tooltip": "The wheel controller's shutter. Closed means the sensor "
                    "sees nothing — dark frames are taken this way."},
        {"name": "target_temp", "label": "Sensor setpoint (°C)", "type": "float",
         "min": -80.0, "max": 25.0, "step": 1.0},
    ],
    # imagerd_rt owns the sentry schedule; parameters live in schedule.conf and
    # only take effect on a daemon restart, so live editing is not offered.
    "sentry": [],
}

DEFAULT_FOCUS_TTL = 60.0
MAX_FOCUS_TTL = 600.0
FOCUS_MAX_ERRORS = 5

# Keep a small ring of finished parameter requests so a client that polls a
# moment later can still read the outcome.
_MAX_RESULTS = 16


class CameraService:
    """Shared state between one camera worker and the local frame server."""

    def __init__(self, camera_type, instance_name, output_dir="",
                 supports_focus=True, supports_params=True, node_name="",
                 setup_mode=False):
        self.camera_type = camera_type
        self.instance_name = instance_name
        self.output_dir = output_dir or ""
        # Friendly name of the machine; travels with every status snapshot and
        # discovery reply so observers see a place, not an address.
        self.node_name = node_name or ""
        self.supports_focus = bool(supports_focus)
        self.supports_params = bool(supports_params)
        # True when the camera is up for focusing only — no schedule, so no
        # frames are being archived. Reported to focus_app and to discovery so
        # nobody mistakes an idle setup node for a broken measurement.
        self.setup_mode = bool(setup_mode)

        self._lock = threading.Lock()
        self._frame_cond = threading.Condition(self._lock)

        # Latest live frame
        self._frame = None
        self._frame_ts = None
        self._frame_meta = {}
        self._frame_counter = 0
        self._jpeg_cache = {}        # (counter, max_side, stretch) -> bytes

        # Status snapshot
        self._status = {"status": "starting", "instance_name": instance_name,
                        "camera_type": camera_type, "node_name": self.node_name}

        # Parameter requests: single pending slot, latest wins
        self._pending_params = None      # (req_id, params_dict)
        self._param_results = {}
        self._param_result_order = []
        self._current_params = {}
        self._param_schema = list(PARAM_SCHEMAS.get(camera_type, []))

        # Focus session
        self._focus_deadline = 0.0
        self._focus_errors = 0
        self._focus_disabled_reason = ""

    # ------------------------------------------------------------------
    # Worker side — cheap, non-blocking writes
    # ------------------------------------------------------------------
    def publish_frame(self, frame, ts=None, meta=None):
        """Store the newest live frame. Called from the worker thread."""
        with self._frame_cond:
            self._frame = frame
            self._frame_ts = ts or dt.now()
            self._frame_meta = dict(meta) if meta else {}
            self._frame_counter += 1
            self._jpeg_cache.clear()
            self._frame_cond.notify_all()

    def publish_status(self, payload):
        with self._lock:
            self._status = dict(payload)

    def set_param_schema(self, schema):
        """Replace the editable-parameter schema (Canon reads real choices)."""
        with self._lock:
            self._param_schema = list(schema)

    def set_current_params(self, params):
        with self._lock:
            self._current_params = dict(params)

    def take_param_request(self):
        """Pop the pending parameter request, if any.

        Returns ``(req_id, params)`` or ``(None, None)``. The worker calls this
        at a safe point in its loop and applies the change itself.
        """
        with self._lock:
            pending = self._pending_params
            self._pending_params = None
        return pending if pending else (None, None)

    def has_param_request(self):
        with self._lock:
            return self._pending_params is not None

    def complete_param_request(self, req_id, applied=None, errors=None):
        if not req_id:
            return
        with self._lock:
            self._param_results[req_id] = {
                "id": req_id,
                "done": True,
                "applied": applied or {},
                "errors": list(errors or []),
                "finished_at": dt.now().isoformat(),
            }
            self._param_result_order.append(req_id)
            while len(self._param_result_order) > _MAX_RESULTS:
                self._param_results.pop(self._param_result_order.pop(0), None)

    # ------------------------------------------------------------------
    # Focus mode
    # ------------------------------------------------------------------
    def focus_active(self):
        """True while a focus session is running and has not expired."""
        if not self.supports_focus:
            return False
        with self._lock:
            return time.monotonic() < self._focus_deadline

    def note_focus_error(self, exc):
        """Record a failed focus iteration; disables focus after N in a row."""
        with self._lock:
            self._focus_errors += 1
            if self._focus_errors >= FOCUS_MAX_ERRORS:
                self._focus_deadline = 0.0
                self._focus_disabled_reason = (
                    f"focus stopped after {self._focus_errors} errors: {exc}")
                return True
        return False

    def clear_focus_errors(self):
        with self._lock:
            self._focus_errors = 0
            self._focus_disabled_reason = ""

    # ------------------------------------------------------------------
    # Network side — never blocks the worker
    # ------------------------------------------------------------------
    def latest(self):
        """Return ``(frame, timestamp, meta, counter)`` without waiting."""
        with self._lock:
            return self._frame, self._frame_ts, dict(self._frame_meta), self._frame_counter

    def wait_for_frame(self, since_counter, timeout=5.0):
        """Block up to ``timeout`` for a frame newer than ``since_counter``.

        Only ever called from HTTP threads (MJPEG streaming).
        """
        deadline = time.monotonic() + timeout
        with self._frame_cond:
            while self._frame_counter <= since_counter:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, None, {}, self._frame_counter
                self._frame_cond.wait(timeout=min(remaining, 0.5))
            return (self._frame, self._frame_ts, dict(self._frame_meta),
                    self._frame_counter)

    def latest_jpeg(self, max_side=None, stretch="minmax", quality=85):
        """Latest frame as JPEG bytes, cached per (frame, size, stretch).

        Caching matters: without it every MJPEG client would re-encode the same
        16-bit megapixel frame independently.
        """
        with self._lock:
            frame = self._frame
            ts = self._frame_ts
            counter = self._frame_counter
            key = (counter, max_side, stretch, quality)
            cached = self._jpeg_cache.get(key)
        if frame is None:
            return None, None, counter
        if cached is not None:
            return cached, ts, counter
        if isinstance(frame, (bytes, bytearray)):
            # Canon hands us an already-encoded JPEG.
            jpeg = bytes(frame)
        else:
            jpeg = frame_archive.to_jpeg_bytes(frame, max_side=max_side,
                                               quality=quality, stretch=stretch)
        with self._lock:
            if len(self._jpeg_cache) > 8:
                self._jpeg_cache.clear()
            self._jpeg_cache[key] = jpeg
        return jpeg, ts, counter

    def status(self):
        with self._lock:
            snap = dict(self._status)
            snap.setdefault("node_name", self.node_name)
            snap["focus_active"] = time.monotonic() < self._focus_deadline
            if self._focus_disabled_reason:
                snap["focus_note"] = self._focus_disabled_reason
            snap["has_frame"] = self._frame is not None
            snap["frame_counter"] = self._frame_counter
            snap["frame_ts"] = (self._frame_ts.isoformat()
                                if self._frame_ts else None)
        return snap

    def info(self):
        with self._lock:
            return {
                "instance_name": self.instance_name,
                "camera_type": self.camera_type,
                "node_name": self.node_name,
                "output_dir": self.output_dir,
                "setup_mode": self.setup_mode,
                "supports_focus": self.supports_focus,
                "supports_params": self.supports_params and bool(self._param_schema),
                "param_schema": list(self._param_schema),
            }

    def params(self):
        with self._lock:
            return {
                "camera_type": self.camera_type,
                "current": dict(self._current_params),
                "schema": list(self._param_schema),
                "editable": self.supports_params and bool(self._param_schema),
            }

    def request_params(self, params):
        """Queue a parameter change for the worker. Returns a request id.

        The slot holds one request: a new call replaces an unprocessed one, so
        hammering this endpoint cannot build up a backlog for the worker.
        """
        if not self.supports_params:
            return None
        req_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._pending_params = (req_id, dict(params))
        return req_id

    def param_result(self, req_id):
        with self._lock:
            if req_id in self._param_results:
                return dict(self._param_results[req_id])
            pending = self._pending_params
        if pending and pending[0] == req_id:
            return {"id": req_id, "done": False}
        return None

    def request_focus(self, ttl=DEFAULT_FOCUS_TTL):
        """Start or extend a focus session. Returns the session state."""
        if not self.supports_focus:
            return {"focus_active": False, "supported": False}
        try:
            ttl = float(ttl)
        except (TypeError, ValueError):
            ttl = DEFAULT_FOCUS_TTL
        ttl = max(1.0, min(ttl, MAX_FOCUS_TTL))
        with self._lock:
            self._focus_deadline = time.monotonic() + ttl
            self._focus_errors = 0
            self._focus_disabled_reason = ""
        return {"focus_active": True, "supported": True, "ttl": ttl}

    def stop_focus(self):
        with self._lock:
            self._focus_deadline = 0.0
        return {"focus_active": False, "supported": self.supports_focus}


class NullService(CameraService):
    """Archive-only service, used when no worker is running.

    ``frame_server.py`` can be started standalone against a directory of
    captured files; the archive endpoints work, the live ones report that no
    camera is attached.
    """

    def __init__(self, output_dir, instance_name="archive", camera_type="unknown",
                 node_name=""):
        super().__init__(camera_type, instance_name, output_dir,
                         supports_focus=False, supports_params=False,
                         node_name=node_name)
        self.publish_status({"status": "archive-only",
                             "instance_name": instance_name,
                             "camera_type": camera_type,
                             "node_name": self.node_name})
