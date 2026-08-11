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
        # No frame period here on purpose: the driver derives it from the
        # exposure, and a period settable from a web form is a period someone
        # can set shorter than the exposure, which silently truncates it.
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
    # The Japan driver applies these between exposures, like the ASI one. There
    # is no gain and no temperature setpoint: this camera has no analog gain, and
    # its sensor temperature is a reading with nothing to set it to.
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
         # Home is shown but not offered, for the same reason as on the ASI
         # camera — see that schema. The positions are bare numbers here: this is
         # a different wheel and the driver has no filter table to name them from.
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

# Keep a small ring of finished parameter requests so a client that polls a
# moment later can still read the outcome.
_MAX_RESULTS = 16


class CameraService:
    """Shared state between one camera worker and the local frame server."""

    def __init__(self, camera_type, instance_name, output_dir="",
                 supports_focus=True, supports_params=True, node_name="",
                 setup_mode=False, supports_hold=None):
        self.camera_type = camera_type
        self.instance_name = instance_name
        self.output_dir = output_dir or ""
        # Friendly name of the machine; travels with every status snapshot and
        # discovery reply so observers see a place, not an address.
        self.node_name = node_name or ""
        self.supports_focus = bool(supports_focus)
        # Whether a focus session can also pause the measurements. Normally it
        # can, because the worker owns its own schedule; the sentry is the
        # exception — its programme belongs to the imagerd_rt daemon, which
        # this driver supervises rather than drives.
        self.supports_hold = (bool(supports_focus) if supports_hold is None
                              else bool(supports_hold))
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
        # Why the worker is not serving live frames right now, when focus is on
        # and working — a short exposure gap, a dark run. Distinct from
        # ``_focus_disabled_reason``, which means focus was switched off.
        self._focus_note = ""
        # A focus session can additionally *hold* the camera: stop starting
        # scheduled captures for as long as somebody is focusing. Separate from
        # the deadline because the MJPEG stream renews the deadline on every
        # chunk it sends and must not change what kind of session this is.
        self._focus_hold = False
        # Whether the worker is inside its measuring cycle right now. None until
        # a driver says; focus_app uses it to decide whether taking the camera
        # needs the operator's agreement.
        self._schedule_active = None
        # Set by the worker once it has actually stopped for the hold, so the
        # operator is told the measurements really did pause.
        self._hold_effective = False

        # What the camera is observing: mode, cycle period, slots. Published so
        # an observer can reconstruct the cycles a flat archive was filed under
        # — nothing records the cycle number in the frames themselves, and
        # ``frame_grouping`` otherwise has to guess the period from the frames.
        self._schedule = {}

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

    def set_schedule(self, schedule):
        """Publish the observing programme this camera is following.

        Read by ``/api/info`` and by the archive grouping in ``cycles.py``: the
        cycle a frame belongs to is not recorded anywhere in the frame, so the
        only way to reconstruct it is the phase reference and period from here.
        """
        with self._lock:
            self._schedule = dict(schedule) if schedule else {}

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

    def focus_hold(self):
        """True while a focus session is asking the schedule to stand down.

        Read by the workers at the points where they would otherwise start a
        capture. Tied to the session deadline on purpose: if the focus tool
        disappears without saying goodbye, the hold expires with the session and
        measurements resume by themselves.
        """
        if not self.supports_hold:
            return False
        with self._lock:
            return self._focus_hold and time.monotonic() < self._focus_deadline

    def set_schedule_active(self, active):
        """Tell observers whether measurements are running right now."""
        with self._lock:
            self._schedule_active = None if active is None else bool(active)

    def note_hold_effective(self, held):
        """Worker's confirmation that it has (or has not) paused for a hold."""
        with self._lock:
            self._hold_effective = bool(held)

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

    def set_focus_note(self, text=""):
        """Say why no live frame is coming, while focus is on and healthy.

        A refused focus frame used to be silent: focus_app showed a connected
        stream and a stale picture with nothing to explain it. This is the one
        line it can show instead. Cheap enough to call on every idle tick.
        """
        text = str(text or "")
        with self._lock:
            self._focus_note = text

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
                # Carried in /api/info as well as /api/status so the focus tool
                # can decide whether connecting interrupts anything before it
                # opens a stream, without a second round trip.
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

    def request_focus(self, ttl=DEFAULT_FOCUS_TTL, hold=None):
        """Start or extend a focus session. Returns the session state.

        ``hold`` is three-valued and that matters. ``True`` asks the camera to
        stop measuring, ``False`` releases it, and ``None`` — the default —
        leaves the hold as it is and only pushes the deadline out. The MJPEG
        endpoint renews the session on every frame it sends, and it must not
        turn an operator's hold into an ordinary session by doing so.
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
