"""Thread-safe boundary between a camera worker and the network.

HTTP threads must never touch a camera object: a stuck USB read or a hung gphoto2
call would go unnoticed, and an observer could stall the measurement loop.
The worker never blocks on a network client. Requests are single-slot, latest wins,
so a flood of HTTP calls cannot build a queue. Focus carries a deadline and expires
by itself if the observer's program dies. Network-facing methods are non-blocking,
except ``wait_for_frame()``, which takes a timeout.
"""
import threading
import time
import uuid

from datetime import datetime as dt

import frame_archive

# What focus_app.py may edit per camera type. These let the UI render before a
# camera connects; a worker can replace them (Canon reads real choices).
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
        # No frame period: the driver derives it from the exposure, and a period set
        # shorter than the exposure silently truncates it.
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
    # Applied between exposures, never during one; the next scheduled slot restores
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
         # Home is where the wheel parks on startup: the form must show it, or a
         # just-started camera reads as being in the first filter, but it is not a
         # position to send it to.
         "states": [{"value": 0, "label": "home (after startup)"}],
         "choices": [{"value": 1, "label": "1 — 557.7 nm"},
                     {"value": 2, "label": "2 — 630.0 nm"},
                     {"value": 3, "label": "3 — OH broadband"},
                     {"value": 4, "label": "4 — 840.0 nm"},
                     {"value": 5, "label": "5 — 846.5 nm"},
                     {"value": 6, "label": "6 — 857.0 nm"}]},
        # The shutter sits on the filter-wheel controller, not the camera. It is shut
        # between runs and for darks, so focusing needs a way to open it.
        {"name": "shutter", "label": "Shutter", "type": "bool", "hint": "open",
         "true_label": "open", "false_label": "closed",
         "tooltip": "The wheel controller's shutter. Closed means the sensor "
                    "sees nothing — dark frames are taken this way."},
        {"name": "target_temp", "label": "Sensor setpoint (°C)", "type": "float",
         "min": -80.0, "max": 25.0, "step": 1.0},
    ],
    # Applied between exposures. This camera has no analog gain, and its sensor
    # temperature is a reading with no setpoint.
    "japan": [
        {"name": "exposure", "label": "Exposure (s)", "type": "float",
         "min": 0.001, "max": 3600.0, "step": 0.5},
        {"name": "binning", "label": "Binning", "type": "choice",
         "choices": [{"value": 1, "label": "1x1"},
                     {"value": 2, "label": "2x2"},
                     {"value": 4, "label": "4x4"},
                     {"value": 8, "label": "8x8"}]},
        {"name": "readout_speed", "label": "Readout speed", "type": "choice",
         "choices": [{"value": 1, "label": "1 — slow (min noise)"},
                     {"value": 2, "label": "2 — fast"}]},
        {"name": "filter", "label": "Filter", "type": "choice",
         # Home is shown but not offered, as on the ASI camera. Bare numbers: a
         # different wheel, with no filter table in the driver.
         "states": [{"value": 0, "label": "home (after startup)"}],
         "choices": [{"value": n, "label": str(n)} for n in range(1, 7)]},
        {"name": "shutter", "label": "Shutter", "type": "bool", "hint": "open",
         "true_label": "open", "false_label": "closed",
         "tooltip": "The wheel controller's shutter. Closed means the sensor "
                    "sees nothing — dark frames are taken this way."},
    ],
    # imagerd_rt owns the sentry schedule; parameters live in schedule.conf and
    # only take effect on a daemon restart, so live editing is not offered.
    "sentry": [],
}

DEFAULT_FOCUS_TTL = 60.0
MAX_FOCUS_TTL = 600.0
FOCUS_MAX_ERRORS = 5

# Finished requests, so a client polling a moment later can still read the outcome.
_MAX_RESULTS = 16


class CameraService:
    def __init__(self, camera_type, instance_name, output_dir="",
                 supports_focus=True, supports_params=True, node_name="",
                 setup_mode=False, supports_hold=None):
        self.camera_type = camera_type
        self.instance_name = instance_name
        self.output_dir = output_dir or ""
        # Travels with every status snapshot and discovery reply: a place, not an
        # address.
        self.node_name = node_name or ""
        self.supports_focus = bool(supports_focus)
        # False for the sentry: its programme belongs to the imagerd_rt daemon, which
        # this driver supervises rather than drives.
        self.supports_hold = (bool(supports_focus) if supports_hold is None
                              else bool(supports_hold))
        self.supports_params = bool(supports_params)
        # Up for focusing only: no schedule, nothing archived. Reported so an idle setup
        # node is not mistaken for a broken measurement.
        self.setup_mode = bool(setup_mode)

        self._lock = threading.Lock()
        self._frame_cond = threading.Condition(self._lock)

        self._error_count = 0
        self._last_error = ""
        self._last_error_kind = ""
        self._last_error_at = ""

        self._frame = None
        self._frame_ts = None
        self._frame_meta = {}
        self._frame_counter = 0
        self._jpeg_cache = {}        # (counter, max_side, stretch, quality) -> bytes

        self._status = {"status": "starting", "instance_name": instance_name,
                        "camera_type": camera_type, "node_name": self.node_name}

        self._pending_params = None      # (req_id, params_dict)
        self._param_results = {}
        self._param_result_order = []
        self._current_params = {}
        self._param_schema = list(PARAM_SCHEMAS.get(camera_type, []))

        self._focus_deadline = 0.0
        self._focus_errors = 0
        self._focus_disabled_reason = ""
        # Why no live frames while focus is on and working: a short exposure gap, a dark
        # run. ``_focus_disabled_reason`` means focus was switched off.
        self._focus_note = ""
        # Hold: no scheduled captures while somebody focuses. Separate from the
        # deadline: the MJPEG stream renews that on every chunk and must not change the
        # session kind.
        self._focus_hold = False
        # None until a driver says. focus_app uses it to decide whether taking the
        # camera needs the operator's agreement.
        self._schedule_active = None
        # Set once the worker has actually stopped, so the operator is told it really
        # did.
        self._hold_effective = False

        # Published so an observer can reconstruct the cycles of a flat archive: frames
        # do not record their cycle, and ``frame_grouping`` otherwise guesses the
        # period.
        self._schedule = {}

    def publish_frame(self, frame, ts=None, meta=None):
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
        with self._lock:
            self._param_schema = list(schema)

    def set_current_params(self, params):
        with self._lock:
            self._current_params = dict(params)

    def set_schedule(self, schedule):
        with self._lock:
            self._schedule = dict(schedule) if schedule else {}

    def take_param_request(self):
        """Called by the worker at a safe point in its loop; it applies the change
        itself.
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

    def focus_active(self):
        if not self.supports_focus:
            return False
        with self._lock:
            return time.monotonic() < self._focus_deadline

    def focus_hold(self):
        """Tied to the session deadline: if the focus tool disappears, the hold expires
        with the session and measurements resume by themselves.
        """
        if not self.supports_hold:
            return False
        with self._lock:
            return self._focus_hold and time.monotonic() < self._focus_deadline

    def set_schedule_active(self, active):
        with self._lock:
            self._schedule_active = None if active is None else bool(active)

    def note_hold_effective(self, held):
        with self._lock:
            self._hold_effective = bool(held)

    def note_focus_error(self, exc):
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

    def note_error(self, kind, message, when=None):
        """Reaches ``/api/status``, hence the monitor and the alert letters."""
        stamp = when or dt.now().isoformat(timespec="seconds")
        with self._lock:
            self._error_count += 1
            self._last_error = str(message)
            self._last_error_kind = str(kind or "error")
            self._last_error_at = stamp

    def clear_errors(self):
        with self._lock:
            self._error_count = 0
            self._last_error = ""
            self._last_error_kind = ""
            self._last_error_at = ""

    def set_focus_note(self, text=""):
        """Shown by focus_app instead of a stale picture. Cheap enough for every idle
        tick.
        """
        text = str(text or "")
        with self._lock:
            self._focus_note = text

    def latest(self):
        with self._lock:
            return self._frame, self._frame_ts, dict(self._frame_meta), self._frame_counter

    def wait_for_frame(self, since_counter, timeout=5.0):
        """Only called from HTTP threads (MJPEG streaming)."""
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
        """Cached so MJPEG clients do not each re-encode the same 16-bit megapixel
        frame.
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
            active = time.monotonic() < self._focus_deadline
            snap["focus_active"] = active
            snap["focus_hold"] = active and self._focus_hold
            snap["hold_effective"] = active and self._hold_effective
            snap["schedule_active"] = self._schedule_active
            # "focus was switched off" outranks "no frame just now".
            if self._focus_disabled_reason:
                snap["focus_note"] = self._focus_disabled_reason
            elif self._focus_note:
                snap["focus_note"] = self._focus_note
            if self._last_error:
                snap["last_error"] = self._last_error
                snap["last_error_kind"] = self._last_error_kind
                snap["last_error_at"] = self._last_error_at
                # Fallback only: the drivers keep their own, fuller tally.
                snap.setdefault("errors", self._error_count)
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
                # Also in /api/info so the focus tool can decide before opening a
                # stream, without a second round trip.
                "schedule_active": self._schedule_active,
                "focus_hold": self._focus_hold,
                "hold_supported": self.supports_hold,
                "supports_focus": self.supports_focus,
                "supports_params": self.supports_params and bool(self._param_schema),
                "param_schema": list(self._param_schema),
                "schedule": dict(self._schedule),
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

    def request_focus(self, ttl=DEFAULT_FOCUS_TTL, hold=None):
        """``hold``: True stops measuring, False releases, None only extends the
        deadline.

        The MJPEG endpoint renews the session on every frame and must not turn an
        operator's hold into an ordinary session.
        """
        if not self.supports_focus:
            return {"focus_active": False, "supported": False,
                    "hold": False, "hold_supported": False}
        try:
            ttl = float(ttl)
        except (TypeError, ValueError):
            ttl = DEFAULT_FOCUS_TTL
        ttl = max(1.0, min(ttl, MAX_FOCUS_TTL))
        with self._lock:
            was_measuring = bool(self._schedule_active)
            self._focus_deadline = time.monotonic() + ttl
            self._focus_errors = 0
            self._focus_disabled_reason = ""
            if hold is not None:
                self._focus_hold = bool(hold) and self.supports_hold
                if not hold:
                    self._hold_effective = False
            held = self._focus_hold
            schedule_active = self._schedule_active
        return {"focus_active": True, "supported": True, "ttl": ttl,
                "hold": held, "hold_supported": self.supports_hold,
                "was_measuring": was_measuring,
                "schedule_active": schedule_active}

    def stop_focus(self):
        with self._lock:
            self._focus_deadline = 0.0
            self._focus_hold = False
            self._hold_effective = False
        return {"focus_active": False, "supported": self.supports_focus,
                "hold": False, "hold_supported": self.supports_hold}


class NullService(CameraService):
    """For a standalone ``frame_server.py`` over a directory of captured files: archive
    endpoints work, live ones report that no camera is attached.
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
