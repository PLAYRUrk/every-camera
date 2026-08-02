"""
ASI all-sky imager driver: Princeton Instruments PIXIS (PICAM) + filter wheel.

This is the every-camera front for the hardware in ``cameras/asi/``. It keeps the
measurement programme of the standalone asi-camera application intact — cooling
lock, pre-darks, the two schedule modes, post-darks on Ctrl+C — and adds what
every other camera in this program already has: retained MQTT status, status
files for the monitor, the LAN frame server with UDP discovery, live view and
remote parameter editing for ``focus_app.py``, preview mode, and the shared
console dashboard.

Two rules govern the threading, both inherited from the original program because
both are load-bearing:

* **Every PICAM and serial call happens on the worker thread.** HTTP handlers
  reach the camera only through :class:`camera_service.CameraService`; the
  console renderer only reads a state dict. Two threads issuing SDK calls at
  once is what the original avoided by joining its prep thread before each
  capture, and this driver avoids it by never starting one.
* **Nothing waits without servicing the rest.** The gaps between exposures are
  where parameter changes are applied, focus frames are served and status is
  published — see :meth:`AsiWorkerConsole._service_tick`, which every wait in
  this driver runs through, including the one that waits for the operator's
  Enter. A focus frame is only taken when it comfortably fits before the next
  scheduled slot, so live view can never delay a measurement; when it does not
  fit, the reason is published rather than swallowed. A focus session opens the
  shutter for itself and shuts it again afterwards, except during a dark run,
  which owns the shutter outright.

Schedule modes (``asi.mode`` in config.json):

    sun        Entries fire on given seconds of every minute while the solar
               altitude stays at or below ``sun_max_angle``. Pre-darks are timed
               to finish just as that window opens.
    time       One general cycle repeats, phase-locked to ``t_start``; each entry
               has its offset (``delta``), filter, exposure, binning and gain.
    sun_cycle  The same general cycle, but anchored to the sun instead of the
               clock: nothing is exposed until the altitude reaches
               ``sun_max_angle``, the pre-darks finish as that happens, and the
               cycle runs until sunrise. This is what imagerd_rt did.

Frames are filed as ``<output_dir>/YYYY/MM/DD/`` in UTC, under imagerd_rt's own
file name — see ``cameras/asi/paths.py``.
"""
import os
import sys
import threading
import time

from datetime import datetime as dt, timedelta
from pathlib import Path

import console_ui

from utils import (
    claim_instance_name, get_instance_name, get_local_ip, get_node_name,
    get_system_info, APP_DIR,
)
from worker_common import (
    WorkerMqtt, parse_command_params, publish_current_params,
    publish_schedule_state, serving_focus_hold,
    run_focus_iteration, announce_setup_mode, empty_schedule_reason, SETUP_STATUS,
    install_stop_handler, stop_signal_name,
)

from cameras.asi import config as asi_config
from cameras.asi import devices, paths as asi_paths, picam, schedule as asi_schedule
from cameras.asi import exposure as asi_exposure
from cameras.asi.fits import write_fits
from cameras.asi.seqno import next_seqno
# Safe to import here: the wheel module pulls in pyserial only when it opens a
# port, so a machine with no hardware still imports this driver.
from cameras.asi.filterwheel import HOME as FILTER_HOME

# How much slack a focus frame needs before the next scheduled capture: the
# exposure itself, readout, and a margin. Below this the frame is skipped.
FOCUS_SLACK_FACTOR = 1.4
FOCUS_SLACK_SECONDS = 3.0

# Longest single sleep inside a wait loop. Short enough that a stop request or a
# focus session starts being served promptly.
TICK = 0.2

# Words that may stand for a shutter state. A remote parameter arrives as JSON
# from focus_app (a real bool) or hand-typed over MQTT, and "closed" must never
# be read as truthy just because it is a non-empty string.
_TRUE_WORDS = {"1", "true", "yes", "on", "open", "opened"}
_FALSE_WORDS = {"0", "false", "no", "off", "closed", "close", "shut"}


def _as_bool(value):
    """Interpret a remote on/off value, refusing anything ambiguous."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise ValueError(f"expected open/closed, got {value!r}")


def _shutter_text(state):
    return "unknown" if state is None else ("open" if state else "closed")


def _filter_text(position):
    """Word a wheel position. Home is a place; unknown is a failed move."""
    if position is None:
        return "unknown"
    return "home" if position == FILTER_HOME else str(position)


# ---------------------------------------------------------------------------
# Hardware facade
# ---------------------------------------------------------------------------
class AsiCamera:
    """Camera + filter wheel as one object, with a single open/close lifecycle.

    The two devices are always used together (an exposure is meaningless without
    a known filter and shutter state), and both are context managers, so this
    holds their contexts and exposes the handful of operations the worker needs.
    """

    def __init__(self, cfg: asi_config.AsiConfig):
        self.cfg = cfg
        self.cam = None
        self.wheel = None
        self._info = {}
        self._opened = False

    # -- lifecycle -----------------------------------------------------------
    def open(self):
        """Open both devices. Cooling is waited out here, before any capture."""
        self.cam = devices.make_camera(self.cfg)
        self.wheel = devices.make_wheel(self.cfg)
        self.cam.__enter__()
        self._opened = True
        try:
            self.wheel.__enter__()
        except Exception:
            self.cam.__exit__(None, None, None)
            self._opened = False
            raise
        # The controller never volunteers the shutter's state, so command it
        # shut: after this the state reported to focus_app is a fact rather than
        # an assumption, and closed is where every run starts anyway.
        try:
            self.set_shutter(False)
        except Exception as exc:
            console_ui.warn(f"Could not close the shutter at startup: {exc}")
        self._info = self.cam.info()
        return self

    def close(self):
        if not self._opened:
            return
        try:
            self.set_shutter(False)
        except Exception:
            pass
        try:
            self.wheel.__exit__(None, None, None)
        except Exception as exc:
            console_ui.warn(f"Filter wheel close: {exc}")
        try:
            self.cam.__exit__(None, None, None)
        except Exception as exc:
            console_ui.warn(f"Camera close: {exc}")
        self._opened = False

    # -- state ---------------------------------------------------------------
    @property
    def info(self):
        return dict(self._info)

    @property
    def current_exposure(self):
        return self.cam.current_exposure

    @property
    def current_binning(self):
        return self.cam.current_binning

    @property
    def current_gain(self):
        return self.cam.current_gain

    @property
    def current_filter(self):
        """Wheel position: 0 at home, 1..6 in a filter, None if unknown."""
        return self.wheel.current_filter

    @property
    def filter_number(self):
        """The position as a number for file names and FITS headers.

        An unconfirmed move leaves the position unknown; the archive has always
        recorded that as 0 and keeps doing so, rather than growing a second
        spelling of "no filter" that existing tooling would have to learn.
        """
        position = self.wheel.current_filter
        return FILTER_HOME if position is None else position

    @property
    def shutter_open(self):
        """True/False once the shutter has been commanded, None while unknown."""
        return getattr(self.wheel, "shutter_open", None)

    @property
    def target_temperature(self):
        return self.cam.target_temperature

    def temperature_limits(self):
        """Setpoints the camera accepts, or None when it will not say."""
        try:
            return self.cam.temperature_limits()
        except Exception:
            return None

    def temperature(self):
        return self.cam.temperature()

    def temperature_locked(self):
        try:
            return self.cam.temperature_locked()
        except Exception:
            return False

    def info_rows(self):
        return self.cam.info_rows()

    # -- control -------------------------------------------------------------
    def set_exposure(self, seconds):
        self.cam.set_exposure(float(seconds))

    def set_binning(self, binning):
        self.cam.set_binning(int(binning))

    def set_gain(self, gain):
        self.cam.set_gain(int(gain))

    def set_target_temperature(self, celsius):
        """Returns the setpoint actually applied, which may have been clamped."""
        return self.cam.set_target_temperature(float(celsius))

    def select_filter(self, number):
        return self.wheel.select(int(number))

    def set_shutter(self, is_open):
        self.wheel.set_shutter(bool(is_open))

    def capture(self):
        return self.cam.capture()

    def prepare(self, entry, exposure=None, binning=None):
        """Apply one schedule entry's exposure, binning, gain and filter.

        ``exposure`` overrides the entry's own. The preflight stage and the
        overexposure guard both choose the exposure themselves, and the camera
        has to hold exactly what the file name and ``EXPTIME`` will claim — so
        there is one value, applied here, rather than two that could drift apart.
        ``binning`` likewise overrides the entry's own, for the preflight
        stage, which holds one binning from config across every slot.
        """
        wanted = entry.exposure if exposure is None else float(exposure)
        if wanted is not None and self.current_exposure != wanted:
            self.set_exposure(wanted)
        wanted_binning = entry.binning if binning is None else int(binning)
        if wanted_binning and self.current_binning != wanted_binning:
            self.set_binning(wanted_binning)
        if entry.gain and self.current_gain != entry.gain:
            self.set_gain(entry.gain)
        if entry.filter and self.current_filter != entry.filter:
            self.select_filter(entry.filter)

    def cooling_fields(self):
        """Temperature values for the console, read from the worker thread only."""
        temp = self.temperature()
        fields = {"ccd_temp": temp}
        setpoint = self.target_temperature
        if setpoint is not None:
            fields["setpoint"] = setpoint
            fields["locked"] = self.temperature_locked()
        return fields


# ---------------------------------------------------------------------------
# Console worker
# ---------------------------------------------------------------------------
class AsiWorkerConsole(threading.Thread):
    """Runs the ASI measurement programme and publishes it to the rest of the app."""

    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, cam: AsiCamera, cfg: asi_config.AsiConfig, output_dir,
                 instance_name, status_dir, mqtt_publisher=None,
                 mqtt_prefix="every_camera", service=None, dashboard=None,
                 node_name="", setup_mode=False):
        super().__init__(daemon=True)
        self.cam = cam
        self.cfg = cfg
        self.output_dir = output_dir
        self.instance_name = instance_name
        self.status_dir = status_dir
        self.setup_mode = bool(setup_mode)
        self._service = service
        self._dash = dashboard
        self._bus = WorkerMqtt("asi", instance_name, status_dir, mqtt_publisher,
                               mqtt_prefix, service=service, node_name=node_name)

        self._stop_event = threading.Event()
        self._force_quit = False
        self._shots = 0
        self._darks = 0
        self._errors = 0
        self._last_shot = None
        self._last_file = None
        self._last_frame = None
        # Intensity control: what the last frame measured, which stage the
        # sun_cycle is in, and what the two loops currently ask of a slot.
        self._last_mean = None
        self._last_saturation = 0.0
        self._stage = "main"
        self._auto_exposure = None
        self._split_frames = 1
        self._dark_means = {}
        self._warned_slots = set()
        self._phase = "starting"
        self._next_slot = None
        # Set when a focus hold swallowed the slot we were waiting for, so the
        # mode loops know to plan again instead of shooting a stale target.
        self._schedule_dirty = False
        self._pending_capture = None
        self._pending_capture_lock = threading.Lock()
        self._pending_capture_event = threading.Event()
        self._started_event = threading.Event()
        # True only while a dark run owns the shutter. Focus must not touch it
        # then: an opened shutter turns the rest of that run into light frames.
        self._darks_running = False
        # True when a focus session, rather than the observing programme, is what
        # opened the shutter — so it can be shut again when the session ends.
        self._focus_opened_shutter = False
        # True once the operator has set the shutter from focus_app: their choice
        # then stands for the rest of the session, whatever focus would prefer.
        self._focus_shutter_manual = False

    # ------------------------------------------------------------------
    # Stop handling
    # ------------------------------------------------------------------
    def request_stop(self, force=False):
        """Ask the run to end. The first call still shoots the closing darks."""
        if force or self._stop_event.is_set():
            self._force_quit = True
        self._stop_event.set()
        self._started_event.set()      # unblock the "press Enter" wait

    @property
    def stopping(self):
        return self._stop_event.is_set()

    # ------------------------------------------------------------------
    # Dashboard / status
    # ------------------------------------------------------------------
    def _dash_update(self, **fields):
        if self._dash is not None:
            self._dash.update(**fields)

    def _camera_rows(self, cooling=None):
        cooling = cooling if cooling is not None else {}
        temp = cooling.get("ccd_temp")
        temp_text = f"{temp:.2f} °C" if isinstance(temp, (int, float)) else "n/a"
        if cooling.get("setpoint") is not None:
            state = "locked" if cooling.get("locked") else "settling"
            temp_text += f"   setpoint {cooling['setpoint']:.1f} °C ({state})"
        binning = self.cam.current_binning or 1
        exposure = self.cam.current_exposure
        rows = [
            ("Exposure / binning:",
             f"{exposure:g} s  ·  {binning}x{binning}" if exposure
             else f"-  ·  {binning}x{binning}"),
            ("Gain:", str(self.cam.current_gain)),
            ("Filter:", _filter_text(self.cam.current_filter)),
            ("Shutter:", _shutter_text(self.cam.shutter_open)),
            ("Sensor temperature:", temp_text),
        ]
        # Only shown while something is actually driving the intensity, so the
        # block keeps its usual five rows on an ordinary run.
        control = self._intensity_row()
        if control:
            rows.append(("Intensity control:", control))
        return rows

    def _intensity_row(self):
        mean = ("" if self._last_mean is None
                else f"  ·  last mean {self._last_mean:.0f} ADU")
        if self._stage == "preflight":
            if self._auto_exposure is None:
                return f"preflight{mean}"
            return (f"preflight, auto {self._auto_exposure:g} s"
                    f"  ·  target {self.cfg.preflight.target_mean:.0f} ADU{mean}")
        if self._split_frames > 1:
            return (f"split ×{self._split_frames}"
                    f"  ·  limit {self.cfg.overexposure.threshold:.0f} ADU{mean}")
        return ""

    def _refresh_camera_section(self, cooling=None):
        if self._dash is None:
            return
        if cooling is None:
            cooling = self.cam.cooling_fields()
        self._dash.set_section("camera", self._camera_rows(cooling))
        return cooling

    def _save_status(self, status, force=False, cooling=None):
        cooling = cooling or {}
        # The schedule moves the filter, the exposure and the shutter on its
        # own; focus_app only ever sees what is published from here.
        publish_current_params(self._service, self._current_params)
        publish_schedule_state(self._service, self._measuring_now())
        payload = {
            "instance_name": self.instance_name,
            "camera_type": "asi",
            "pid": os.getpid(),
            "status": status,
            "phase": self._phase,
            "mode": self.cfg.schedule.mode,
            "output_dir": self.output_dir,
            "shots_taken": self._shots,
            "darks_taken": self._darks,
            "last_shot": self._last_shot.isoformat() if self._last_shot else None,
            "last_file": self._last_file,
            "errors": self._errors,
            "exposure": self.cam.current_exposure,
            "binning": self.cam.current_binning,
            "gain": self.cam.current_gain,
            "filter": self.cam.current_filter,
            "shutter": self.cam.shutter_open,
            "next_slot": self._next_slot.isoformat() if self._next_slot else None,
            "setup_mode": self.setup_mode,
            # Intensity control, for the monitor: which sun_cycle stage is
            # running, what the automatic exposure settled on, how far the
            # current slot is divided, and what the last frame measured.
            "stage": self._stage,
            "auto_exposure": self._auto_exposure,
            "split_frames": self._split_frames,
            "last_mean": (round(self._last_mean, 2)
                          if self._last_mean is not None else None),
            "last_update": dt.now().isoformat(),
        }
        if cooling.get("ccd_temp") is not None:
            payload["ccd_temp"] = round(float(cooling["ccd_temp"]), 2)
        if cooling.get("setpoint") is not None:
            payload["set_temp"] = cooling["setpoint"]
            payload["temp_locked"] = bool(cooling.get("locked"))
        try:
            system = get_system_info(self.output_dir or None)
            payload["system"] = system
            self._dash_update(disk_free_mb=system.get("disk_free_mb"))
        except Exception:
            pass
        self._dash_update(status=status, frames=self._shots, darks=self._darks,
                          errors=self._errors, output_dir=self.output_dir,
                          last_file=self._last_file)
        self._bus.publish_status(payload, force=force)

    # ------------------------------------------------------------------
    # Live view + remote parameters
    # ------------------------------------------------------------------
    def _current_params(self):
        return {
            "exposure": self.cam.current_exposure,
            "binning": self.cam.current_binning,
            "gain": self.cam.current_gain,
            "filter": self.cam.current_filter,
            "shutter": self.cam.shutter_open,
            "target_temp": self.cam.target_temperature,
        }

    def _apply_params(self, params):
        """Apply a parameter change from focus_app / MQTT. Worker thread only."""
        applied, errors = {}, []
        for name, value in (params or {}).items():
            try:
                if name == "exposure":
                    self.cam.set_exposure(float(value))
                    applied[name] = float(value)
                elif name == "binning":
                    self.cam.set_binning(int(value))
                    applied[name] = int(value)
                elif name == "gain":
                    gain = int(value)
                    if gain not in (1, 2, 3):
                        raise ValueError("gain must be 1 (Low), 2 (Medium) or 3 (High)")
                    self.cam.set_gain(gain)
                    applied[name] = gain
                elif name == "filter":
                    number = int(value)
                    # Home is a state the wheel starts in, not a position to be
                    # sent to: the schedule always names a real filter.
                    if not (asi_schedule.FILTER_MIN <= number
                            <= asi_schedule.FILTER_MAX):
                        raise ValueError(
                            f"filter must be {asi_schedule.FILTER_MIN}.."
                            f"{asi_schedule.FILTER_MAX}")
                    if self.cam.select_filter(number):
                        applied[name] = number
                    else:
                        errors.append(f"filter: wheel did not reach position {number}")
                elif name == "shutter":
                    is_open = _as_bool(value)
                    self.cam.set_shutter(is_open)
                    applied[name] = is_open
                    # From here the operator owns the shutter for the rest of the
                    # session: a focus session opens it by itself, and that must
                    # never undo somebody deliberately closing it from focus_app.
                    self._focus_shutter_manual = True
                    self._focus_opened_shutter = False
                    # Only the darks are supposed to shoot shut. Closing it from
                    # focus_app during a run would quietly turn every scheduled
                    # frame into a dark, so say so where the operator will see it.
                    if not is_open and not self.setup_mode:
                        console_ui.warn(
                            "Shutter closed remotely — scheduled frames stay "
                            "dark until it is opened again")
                elif name == "target_temp":
                    # Report what the camera took, not what was asked for.
                    applied[name] = self.cam.set_target_temperature(float(value))
                else:
                    errors.append(f"{name}: unknown parameter")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if applied:
            console_ui.log(f"Parameters applied: {applied}")
        for err in errors:
            console_ui.warn(f"Parameter rejected — {err}")
        return applied, errors

    def _serve_params(self):
        """Apply any pending remote parameter request. Safe only between exposures."""
        if self._service is None or not self._service.has_param_request():
            return
        req_id, params = self._service.take_param_request()
        if not req_id:
            return
        applied, errors = self._apply_params(params)
        self._service.complete_param_request(req_id, applied, errors)
        self._service.set_current_params(self._current_params())
        self._refresh_camera_section()

    def _focus_note(self, text=""):
        if self._service is not None:
            self._service.set_focus_note(text)

    def _shutter_for_focus(self):
        """Open the shutter for a focus session; put it back when one ends.

        Without this, focusing before the programme has opened the shutter —
        which is every moment between opening the camera and the post-dark
        ``set_shutter(True)`` — returns black frames, and the operator has no way
        to tell a shut shutter from a broken camera. A dark run keeps the
        shutter: opening it there would silently turn darks into light frames.
        """
        if self._darks_running:
            return False
        active = self._service is not None and self._service.focus_active()
        if not active:
            self._focus_close_shutter()
            self._focus_shutter_manual = False
            return False
        # The operator set the shutter by hand from focus_app; that outranks any
        # guess made here, for as long as the session lasts.
        if self._focus_shutter_manual:
            return True
        if not self.cam.shutter_open:
            try:
                self.cam.set_shutter(True)
            except Exception as exc:
                console_ui.warn(f"Could not open the shutter for focusing: {exc}")
                return False
            self._focus_opened_shutter = True
            console_ui.log("Focus session — shutter opened (closed again when it ends)")
            self._refresh_camera_section()
        return True

    def _focus_close_shutter(self):
        """Shut the shutter again, but only if a focus session is what opened it."""
        if not self._focus_opened_shutter:
            return
        self._focus_opened_shutter = False
        try:
            self.cam.set_shutter(False)
        except Exception as exc:
            console_ui.warn(f"Could not close the shutter after focusing: {exc}")
        else:
            console_ui.log("Focus session ended — shutter closed again")
        self._refresh_camera_section()

    def _serve_focus(self, slack):
        """Grab one live frame for focus_app, if it fits in ``slack`` seconds."""
        if self._service is None or not self._service.focus_active():
            return
        if self._darks_running:
            self._focus_note("shooting dark frames — the shutter must stay shut")
            return
        exposure = self.cam.current_exposure or 0.0
        needed = exposure * FOCUS_SLACK_FACTOR + FOCUS_SLACK_SECONDS
        # In setup mode there is no scheduled capture to be late for, so a long
        # exposure must not be refused just because the idle tick is shorter.
        if not self.setup_mode and slack is not None and slack < needed:
            # A live frame must never push a scheduled one late. Silent refusal
            # is what made this look like a broken camera, so say it instead.
            self._focus_note(f"next scheduled frame in {slack:.0f} s — a live "
                             f"frame needs about {needed:.0f} s")
            return
        self._dash_update(focus=f"live frame ({exposure:g} s)")
        run_focus_iteration(
            self._service, self.cam.capture,
            on_error=lambda exc, stopped: console_ui.warn(
                f"Focus frame failed: {exc}" + (" — focus disabled" if stopped else "")),
        )
        self._focus_note("")
        self._dash_update(focus="active")

    # ------------------------------------------------------------------
    # Waiting
    # ------------------------------------------------------------------
    def _service_tick(self, slack, status="running", last_status=0.0, cooling=None,
                      allow_focus=True):
        """Everything that has to keep happening while the worker is not exposing.

        Parameter changes, on-demand captures, status publication and focus
        frames. Every wait in this driver runs through here, which is what makes
        an observer's focus session work whatever the worker is doing —
        including while it waits for the operator's Enter, where a hand-rolled
        copy of this loop used to serve parameters but not focus.

        Returns the (possibly refreshed) ``(last_status, cooling)`` pair so a
        caller can keep throttling its status publication across ticks.
        """
        if time.monotonic() - last_status >= 2.0:
            last_status = time.monotonic()
            cooling = self._refresh_camera_section()
            self._save_status(SETUP_STATUS if self.setup_mode else status,
                              cooling=cooling)
        self._serve_params()
        if self._pending_capture_event.is_set():
            self._handle_pending_capture()
        if allow_focus:
            self._shutter_for_focus()
            self._serve_focus(slack)
        return last_status, cooling

    def _wait_until(self, target, phase=None, allow_focus=True):
        """Idle until ``target``, serving focus, parameters and status meanwhile.

        Returns True if the moment was reached, False if a stop was requested.
        This is the only place the driver sleeps during a run: everything that
        has to keep happening between exposures happens in :meth:`_service_tick`.
        """
        if phase:
            self._set_phase(phase)
        # In setup mode this is an idle tick, not a scheduled capture;
        # publishing it as "next slot" told the monitor a measurement was due.
        self._next_slot = (target if isinstance(target, dt) and not self.setup_mode
                           else None)
        self._dash_update(next_at=self._next_slot)
        last_status = 0.0
        cooling = None
        # Before the wait, not only inside it: a slot whose moment has already
        # passed leaves the loop below immediately, and on a tight schedule that
        # is every slot — a hold checked only in the loop would never be seen.
        if allow_focus and self._hold_for_focus():
            self._next_slot = None
            self._dash_update(next_at=None)
            return not self._stop_event.is_set()
        while not self._stop_event.is_set():
            remaining = (target - dt.now()).total_seconds()
            if remaining <= 0:
                break
            # Checked before the tick, not after: a hold serves its own frames
            # with no slack limit, so letting the tick go first would take one
            # slack-limited frame — or refuse one — for no reason.
            if allow_focus and self._hold_for_focus():
                # The operator took the camera. The slot we were waiting for
                # is gone; the caller re-reads the schedule.
                break
            last_status, cooling = self._service_tick(
                remaining, last_status=last_status, cooling=cooling,
                allow_focus=allow_focus)
            remaining = (target - dt.now()).total_seconds()
            if remaining <= 0:
                break
            self._stop_event.wait(min(TICK, max(remaining, 0.001)))
        self._next_slot = None
        self._dash_update(next_at=None)
        return not self._stop_event.is_set()

    def _hold_for_focus(self):
        """Stand down while somebody is focusing. True if a hold happened.

        Focus used to be squeezed into the gaps between exposures, which made
        the tool unpredictable to use: on a long slot the picture refreshed
        once a minute, and a parameter change waited for a gap wide enough to
        apply it in. Now connecting the tool stops the schedule outright — the
        operator agrees to that in focus_app before it happens — and this is
        where the worker actually stops.

        A capture already under way is never interrupted; this is only reached
        between them. The exit condition is the service's, so an abandoned
        session releases the camera when its deadline passes.
        """
        # In setup mode there is no schedule to stand down from, and the idle
        # loop already serves focus without a slack limit.
        if (self.setup_mode or self._service is None
                or not self._service.focus_hold()):
            return False
        self._schedule_dirty = True
        console_ui.log("Focus session took the camera — scheduled captures are "
                       "paused until it ends.")
        self._set_phase("paused for focusing")
        self._next_slot = None
        self._dash_update(next_at=None, focus="holding the camera")
        publish_schedule_state(self._service, False)
        serving_focus_hold(self._service, True)
        last_status = 0.0
        cooling = None
        while not self._stop_event.is_set() and self._service.focus_hold():
            # ``slack=None``: the schedule is standing down, so there is no slot
            # for a live frame to be late for — the tool gets every frame the
            # camera can take. Same servicing step as every other wait, which is
            # what also opens the shutter for the session.
            last_status, cooling = self._service_tick(
                None, status=SETUP_STATUS, last_status=last_status,
                cooling=cooling)
            self._stop_event.wait(TICK)
        # Hand the shutter back before the schedule resumes; the next thing the
        # programme does may well be a dark.
        self._focus_close_shutter()
        serving_focus_hold(self._service, False)
        self._dash_update(focus="idle")
        if not self._stop_event.is_set():
            console_ui.log("Focus session ended — resuming the schedule at the "
                           "next slot.")
        return True

    def _take_schedule_dirty(self):
        """True once after a focus hold, so a mode loop plans its next slot again.

        Reading it clears it: the slot the loop was waiting for passed while the
        camera was held, and shooting it now would file a frame under a time
        that has gone by. The missed slots are not made up — the next one comes
        round on its own.
        """
        dirty = self._schedule_dirty
        self._schedule_dirty = False
        return dirty

    def _wait_seconds(self, seconds, phase=None, allow_focus=True):
        return self._wait_until(dt.now() + timedelta(seconds=seconds), phase,
                                allow_focus)

    def _measuring_now(self):
        """Is the observing programme running at this moment?

        What focus_app asks before it connects. Exposures and dark series count;
        so does the gap between two slots of an open window, because taking the
        camera there still costs the next frame. The stretches spent waiting for
        the window to open at all do not — a camera idling through the daytime
        can be focused without interrupting anything, and asking the operator to
        confirm that would only teach them to dismiss the question.
        """
        if self.setup_mode:
            return False
        phase = self._phase or ""
        if phase.startswith("waiting"):
            return False
        return phase not in ("starting", "setup", "stopped",
                             "paused for focusing")

    def _set_phase(self, phase, detail=None):
        self._phase = phase
        self._dash_update(phase=phase)
        if detail is not None:
            self._dash_update(detail=detail)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    def _write_frame(self, path, image, timestamp, exposure, image_type, obs_mode,
                     cooling=None, seqno=None, sky_mean=None, split_count=1,
                     split_index=1):
        cooling = cooling or {}
        station = self.cfg.station
        filt = self.cfg.filter_info(self.cam.filter_number)
        write_fits(
            Path(path), image,
            timestamp=timestamp,
            exposure_sec=exposure,
            binning=self.cam.current_binning,
            readout_speed=self.cfg.camera.readout_speed,
            filter_num=self.cam.filter_number,
            ccd_temp=cooling.get("ccd_temp"),
            image_type=image_type,
            obs_mode=obs_mode,
            camera_vendor=self.cam.info.get("vendor", ""),
            camera_model=self.cam.info.get("model", ""),
            camera_sn=self.cam.info.get("serial", ""),
            camera_version=self.cam.info.get("camera_version", ""),
            driver_version=self.cam.info.get("driver_version", ""),
            dcam_version=self.cam.info.get("dcam_version", ""),
            lat=self.cfg.location.lat,
            lon=self.cfg.location.lon,
            elevation=self.cfg.location.elevation,
            gain=self.cam.current_gain,
            set_temp=self.cam.target_temperature,
            # The legacy header block imagerd_rt wrote on every frame.
            bit_depth=self.cam.info.get("bit_depth"),
            seqno=seqno,
            site_id=station.site_id,
            device_id=station.device_id,
            filter_wavelength=filt.wavelength,
            filter_description=filt.description,
            fw_temp=station.fw_temp,
            legacy_version=station.legacy_version,
            sky_mean=sky_mean,
            split_count=split_count,
            split_index=split_index,
        )

    def _unique_frame_path(self, now, **kwargs):
        """A free path for this frame, or None when the second is hopelessly full.

        The archive name resolves to one second (``paths.frame_name``) and
        ``write_fits`` refuses to overwrite, so two frames taken inside the same
        second at the same exposure collide. The overexposure guard's
        ``min_frame_gap`` keeps sub-frames a second apart precisely so this
        cannot happen; this is the backstop for the case that predates it — two
        schedule slots landing in one second.

        The name's second is nudged forward until it is free. ``DATE-OBS`` keeps
        the true start of the exposure, so in that rare case a name and a header
        disagree by a second or two; the header is the one to believe.
        """
        for offset in range(5):
            path = asi_paths.frame_path(self.output_dir,
                                        now + timedelta(seconds=offset), **kwargs)
            if not path.exists():
                return path
        return None

    def _capture_one(self, now, exposure, image_type="LIGHT", obs_mode=None,
                     label=None, split_count=1, split_index=1):
        """Take, save and publish one frame. Returns True on success."""
        obs_mode = obs_mode or self.cfg.schedule.mode
        cooling = self._refresh_camera_section()
        label = label or f"{image_type} filter={_filter_text(self.cam.current_filter)}"
        if self._dash is not None:
            self._dash.capture_begin(label, exposure)
        try:
            image = self.cam.capture()
        except Exception as exc:
            console_ui.error(f"Capture failed: {exc}")
            image = None
        finally:
            if self._dash is not None:
                self._dash.capture_end()

        if image is None:
            self._errors += 1
            self._dash_update(errors=self._errors)
            self._bus.publish_error("error", "capture returned no image",
                                    ts_iso=now.isoformat())
            return False

        self._last_mean, self._last_saturation = asi_exposure.frame_stats(image)

        station = self.cfg.station
        path = self._unique_frame_path(
            now,
            site_id=station.site_id,
            device_id=station.device_id,
            wavelength=self.cfg.filter_info(self.cam.filter_number).wavelength,
            exposure_sec=exposure,
            dark=(image_type == "DARK"),
        )
        if path is None:
            self._errors += 1
            self._dash_update(errors=self._errors)
            console_ui.error(f"Five frames already share the second "
                             f"{now:%H:%M:%S} — dropping this one")
            self._bus.publish_error("error", "frame name collision",
                                    ts_iso=now.isoformat())
            return False
        # Shown on the dashboard and in the log: the full path is mostly the
        # date tree the operator already knows.
        name = str(path.relative_to(self.output_dir))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_frame(path, image, now, exposure, image_type, obs_mode,
                              cooling, seqno=next_seqno(self.output_dir),
                              sky_mean=self._last_mean,
                              split_count=split_count, split_index=split_index)
        except Exception as exc:
            self._errors += 1
            self._dash_update(errors=self._errors)
            console_ui.error(f"Could not write {name}: {exc}")
            self._bus.publish_error("error", f"write failed: {exc}",
                                    ts_iso=now.isoformat())
            return False

        self._last_frame = image
        self._last_shot = now
        self._last_file = name
        if image_type == "DARK":
            self._darks += 1
            # The pedestal the intensity loops extrapolate against: bias plus
            # whatever dark current this exposure collects.
            if self._last_mean is not None:
                self._dark_means.setdefault(exposure, []).append(self._last_mean)
        else:
            self._shots += 1
        if self._service is not None:
            # ``path`` so the frame server can read this frame's own FITS header
            # back off the disk it was just written to: the observer's info panel
            # is otherwise empty for a live frame, there being no header here to
            # publish and no cheap way to build one.
            self._service.publish_frame(image, now, {"image_type": image_type,
                                                     "filter": self.cam.current_filter,
                                                     "exposure": exposure,
                                                     "name": name,
                                                     "path": str(path)})
        self._dash_update(frames=self._shots, darks=self._darks,
                          last_file=name, errors=self._errors)
        detail = "" if self._last_mean is None else f"  mean {self._last_mean:.0f} ADU"
        if split_count > 1:
            detail += f"  ({split_index}/{split_count})"
        console_ui.log(f"Saved {name}{detail}")
        if self._bus.enabled:
            try:
                self._bus.publish_frame_array(image, now.isoformat(),
                                              params=self._current_params())
            except Exception as exc:
                console_ui.warn(f"MQTT frame publish: {exc}")
        self._save_status("running", force=True, cooling=cooling)
        return True

    # ------------------------------------------------------------------
    # Intensity control
    # ------------------------------------------------------------------
    def _bias_for(self, exposure, fallback=0.0):
        """The pedestal to extrapolate an intensity against, in ADU.

        Taken from the pre-darks, which are already shot per unique exposure, so
        this costs nothing extra: the nearest exposure's darks are what a frame
        of this length would sit on. Falls back to the configured value when the
        run has no darks — a late start, or ``dark_frames`` set to zero.
        """
        if not self._dark_means:
            return float(fallback)
        nearest = min(self._dark_means,
                      key=lambda e: abs(float(e) - float(exposure)))
        means = self._dark_means[nearest]
        return sum(means) / len(means) if means else float(fallback)

    def _capture_slot(self, entry, exposure, frames, obs_mode):
        """Shoot one cycle slot: ``frames`` back-to-back sub-frames.

        Returns ``(ok, mean)``. ``ok`` is False only when *every* sub-frame
        failed, so one bad read inside a split does not cost the whole slot and
        does not push the run towards its consecutive-failure limit.
        """
        # The camera is the authority on what was actually applied: if
        # ``prepare`` threw halfway through, filing the frame under the exposure
        # we merely intended would put a lie in the name and in EXPTIME.
        applied = self.cam.current_exposure
        if applied and abs(float(applied) - float(exposure)) > 1e-6:
            console_ui.warn(f"Camera holds {applied:g} s but {exposure:g} s was "
                            f"planned — filing the frame as {applied:g} s")
            exposure = applied

        means, ok_any = [], False
        for index in range(frames):
            if self._stop_event.is_set():
                break
            label = f"LIGHT filter={_filter_text(self.cam.current_filter)}"
            if frames > 1:
                label += f"  {index + 1}/{frames}"
            if self._capture_one(dt.now(), exposure, "LIGHT", obs_mode,
                                 label=label, split_count=frames,
                                 split_index=index + 1):
                ok_any = True
                means.append(self._last_mean)
        return ok_any, asi_exposure.combine_means(means)

    # ------------------------------------------------------------------
    # Dark frames
    # ------------------------------------------------------------------
    def _capture_darks(self, phase, sync_to_seconds=False):
        """Shoot ``dark_frames`` frames per unique exposure with the shutter shut."""
        entries = self.cfg.schedule.entries
        combos = asi_schedule.unique_dark_settings(entries)
        if not combos or self._force_quit:
            return
        total = self.cfg.schedule.dark_frames * len(combos)
        # Neither loop drives a dark, so the dashboard must not go on claiming
        # an automatic exposure or a split while the closing darks run.
        self._auto_exposure = None
        self._split_frames = 1
        self._set_phase(f"dark frames ({phase})", detail=f"0/{total}")
        console_ui.log(f"Dark frames ({phase}): {self.cfg.schedule.dark_frames} × "
                       f"{len(combos)} exposure(s)")
        # A focus session may have opened the shutter; from here until the run is
        # over the shutter belongs to the darks, and ``_darks_running`` is what
        # stops :meth:`_shutter_for_focus` from reopening it behind their back.
        self._darks_running = True
        self._focus_opened_shutter = False
        self._focus_note("shooting dark frames — the shutter must stay shut")
        try:
            self.cam.set_shutter(False)
        except Exception as exc:
            console_ui.warn(f"Could not close the shutter: {exc}")

        done = 0
        try:
            for exposure, binning, gain, _readout in combos:
                if self._force_quit:
                    break
                try:
                    self.cam.set_exposure(exposure)
                    if binning:
                        self.cam.set_binning(binning)
                    # A dark is only subtractable from lights taken at the same
                    # gain, so the schedule's gain applies here too.
                    if gain:
                        self.cam.set_gain(gain)
                except Exception as exc:
                    console_ui.error(f"Could not set up darks ({exposure} s): {exc}")
                    continue
                for _ in range(self.cfg.schedule.dark_frames):
                    if self._force_quit:
                        break
                    if sync_to_seconds:
                        seconds = sorted({s for entry in entries
                                          for s in (entry.seconds or [0])})
                        target = asi_schedule.next_second_slot(seconds)
                        # Darks must still be taken after a stop request, so this wait
                        # ignores the stop flag and only honours a forced quit.
                        self._sleep_until(target)
                    done += 1
                    self._set_phase(f"dark frames ({phase})", detail=f"{done}/{total}")
                    self._capture_one(dt.now(), exposure, image_type="DARK",
                                      obs_mode="dark",
                                      label=f"DARK exp={exposure:g} s")
        finally:
            self._darks_running = False
            self._focus_note("")
        console_ui.log(f"Dark frames ({phase}) complete: {self._darks} total")

    def _sleep_until(self, target):
        """Plain wait that only a forced quit interrupts (used inside dark runs).

        Status keeps being published — a dark run is minutes long, and without
        this the monitor and focus_app see a camera that has stopped answering.
        Parameter requests are deliberately *not* served: a changed exposure
        halfway through would leave the run with darks that match nothing.
        """
        last_status = 0.0
        while not self._force_quit:
            remaining = (target - dt.now()).total_seconds()
            if remaining <= 0:
                return True
            if time.monotonic() - last_status >= 2.0:
                last_status = time.monotonic()
                self._save_status(SETUP_STATUS if self.setup_mode else "running",
                                  cooling=self._refresh_camera_section())
            time.sleep(min(TICK, remaining))
        return False

    # ------------------------------------------------------------------
    # Sun mode
    # ------------------------------------------------------------------
    def _sun_angle_fn(self):
        from cameras.asi.sun import angle_fn
        return angle_fn(self.cfg.location)

    def _run_setup_mode(self):
        """Idle indefinitely, serving focus_app — no schedule, nothing archived.

        There is no observing programme to follow, so the worker simply keeps
        :meth:`_wait_until` running: that is already where focus frames,
        parameter changes and status publication happen, and it is the whole
        reason a camera without a schedule can now be focused at all.
        """
        self._dash_update(schedule="setup mode — no scheduled captures")
        # Nothing here opens the shutter otherwise — the schedule modes do it
        # after their pre-darks — and a focus session on a shut camera produces
        # nothing but dark frames. Closing it again is the caller's ``finally``.
        try:
            self.cam.set_shutter(True)
            console_ui.log("Setup mode: shutter opened for focusing "
                           "(toggle it from focus_app)")
        except Exception as exc:
            console_ui.warn(f"Could not open the shutter: {exc}")
        while not self._stop_event.is_set():
            if not self._wait_seconds(5.0, phase="setup"):
                return

    def _run_sun_mode(self):
        sched = self.cfg.schedule
        angle_fn = self._sun_angle_fn()

        # Pre-darks are scheduled to *finish* as the measurement window opens.
        activation = asi_schedule.sun_crossing_time(angle_fn, sched.sun_max_angle)
        dark_seconds = asi_schedule.estimate_dark_duration(sched.entries,
                                                           sched.dark_frames)
        dark_start = activation - timedelta(seconds=dark_seconds)
        if dark_start > dt.now():
            console_ui.log(f"Pre-darks start at {dark_start.strftime('%H:%M:%S')}, "
                           f"measurements ~{activation.strftime('%H:%M:%S')}")
            self._dash_update(schedule=f"sun ≤ {sched.sun_max_angle:g}°, "
                                       f"window ~{activation.strftime('%H:%M')}")
            if not self._wait_until(dark_start, phase="waiting for pre-darks"):
                return
        self._capture_darks("initial", sync_to_seconds=True)
        if self._stop_event.is_set():
            return

        try:
            self.cam.set_shutter(True)
        except Exception as exc:
            console_ui.warn(f"Could not open the shutter: {exc}")

        in_session = False
        while not self._stop_event.is_set():
            now = dt.now()
            angle = angle_fn(now)
            self._dash_update(schedule=f"sun {angle:+.1f}° "
                                       f"(threshold {sched.sun_max_angle:g}°)")
            if angle > sched.sun_max_angle:
                if in_session:
                    console_ui.log("Sun above the threshold — measurements done.")
                    break
                if not self._wait_seconds(30, phase="waiting for darkness"):
                    return
                continue

            if not in_session:
                in_session = True
                console_ui.log("Measurements started.")
            self._set_phase("measuring")

            consecutive_errors = 0
            for entry in sched.entries:
                if self._stop_event.is_set():
                    break
                try:
                    self.cam.prepare(entry)
                except Exception as exc:
                    console_ui.error(f"Could not prepare filter {entry.filter}: {exc}")
                    self._errors += 1
                    continue
                self._refresh_camera_section()
                target = asi_schedule.next_second_slot(entry.seconds or [0])
                if not self._wait_until(target, phase="measuring"):
                    return
                if self._take_schedule_dirty():
                    break          # the window is re-read from the top
                if angle_fn(dt.now()) > sched.sun_max_angle:
                    console_ui.log("Window closed while waiting — skipping frame.")
                    continue
                if self._capture_one(dt.now(), entry.exposure, "LIGHT", "sun"):
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        console_ui.error(f"{consecutive_errors} consecutive capture "
                                         f"failures — stopping.")
                        self._stop_event.set()
                        break

    # ------------------------------------------------------------------
    # Time-cycle mode
    # ------------------------------------------------------------------
    def _wait_for_start(self):
        """Time mode starts on the operator's word; unattended runs start at once."""
        sched = self.cfg.schedule
        if sched.mode != "time" or not self.cfg.wait_for_enter:
            return True
        if not sys.stdin or not sys.stdin.isatty():
            console_ui.log("No terminal on stdin — starting measurements immediately.")
            return True

        self._set_phase("waiting for start", detail="press Enter")
        if self._dash is not None:
            self._dash.set_footer("Press Enter to start measurements  ·  Ctrl+C — quit")

        def _read():
            try:
                input()
            except (EOFError, OSError):
                pass
            self._started_event.set()

        threading.Thread(target=_read, daemon=True, name="everycam-asi-start").start()
        last_status = 0.0
        cooling = None
        while not self._started_event.is_set():
            # Nothing is scheduled until Enter, so there is no slot for a live
            # frame to be late for: ``slack=None`` is the same "take it now"
            # this passes in setup mode. Ticking at TICK rather than a second
            # keeps a newly opened focus session from waiting on the sleep.
            last_status, cooling = self._service_tick(
                None, status="waiting", last_status=last_status, cooling=cooling)
            self._started_event.wait(TICK)
        # An operator who focused before starting gets the shutter back the way
        # the programme expects it; the pre-darks are about to need it shut.
        self._focus_close_shutter()
        if self._dash is not None:
            self._dash.set_footer("Ctrl+C — finish the frame, shoot the closing darks, stop")
        return not self._stop_event.is_set()

    def _run_time_mode(self):
        sched = self.cfg.schedule
        started = dt.now()
        # The occurrence of t_start belonging to the night we are in, which past
        # midnight is yesterday's — see asi_schedule.cycle_anchor.
        t_start = asi_schedule.cycle_anchor(sched.t_start, started)
        period = sched.period
        late = started >= t_start
        self._dash_update(schedule=f"cycle {period:.0f} s from "
                                   f"{t_start.strftime('%H:%M:%S')}")

        if late:
            console_ui.warn("Late start — the opening dark frames are skipped.")
            self._dash_update(note="Late start: initial darks skipped")
        else:
            self._capture_darks("initial")
        if self._stop_event.is_set():
            return

        try:
            self.cam.set_shutter(True)
        except Exception as exc:
            console_ui.warn(f"Could not open the shutter: {exc}")

        # The overexposure guard applies to the general cycle, which is what this
        # mode runs; the preflight stage does not, there being no sun in it.
        auto = asi_exposure.AutoExposure(self.cfg.preflight)
        guard = asi_exposure.SplitGuard(self.cfg.overexposure)

        consecutive_errors = 0
        while not self._stop_event.is_set():
            now = dt.now()
            slot, entry, iteration = asi_schedule.next_cycle_slot(
                t_start, period, sched.entries, now)
            exposure, frames = self._plan_slot(entry, auto, guard)
            self._dash_update(
                detail=f"cycle {iteration}, Δ{entry.delta:g} s"
                       f"{self._intensity_text(exposure, frames)}",
                schedule=f"cycle {period:.0f} s from {t_start.strftime('%H:%M:%S')}"
                         f"  ·  iteration {iteration}")

            # Prepared on this thread, in the gap before the slot: a filter move
            # takes about a second and there is normally far more room than that.
            try:
                self.cam.prepare(entry, exposure=exposure)
            except Exception as exc:
                console_ui.error(f"Could not prepare the cycle: {exc}")
                self._errors += 1
            self._refresh_camera_section()

            if not self._wait_until(slot, phase="measuring"):
                break
            if self._take_schedule_dirty():
                continue           # the cycle is re-anchored on the clock
            ok, mean = self._capture_slot(entry, exposure, frames, "time")
            self._feed_controllers(entry, exposure, mean, auto, guard)
            if ok:
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    console_ui.error(f"{consecutive_errors} consecutive capture "
                                     f"failures — stopping.")
                    break

    # ------------------------------------------------------------------
    # Sun-triggered cycle mode
    # ------------------------------------------------------------------
    def _run_sun_cycle_mode(self):
        """The general cycle of ``time`` mode, started by the sun rather than the clock.

        This is imagerd_rt's night (``imagerd_rt.c:531-738``): nothing is exposed
        until the solar altitude reaches ``sun_max_angle``; the only thing that
        happens before then is the pre-darks, timed to finish exactly as the
        window opens; the cycle is then anchored to the first whole minute of
        the window and free-runs until sunrise.

        With ``preflight`` enabled the session opens one setpoint earlier. The
        same slots, the same anchor and the same cycle phase run through the
        bright twilight, but with the exposure chosen to hold a mean intensity
        rather than read from the schedule; at ``sun_max_angle`` the automation
        stops and the programme above resumes exactly as written. Sunrise still
        ends the run at ``sun_max_angle``, so the automatic stage is an evening
        prelude and never a morning encore.
        """
        sched = self.cfg.schedule
        pre = self.cfg.preflight
        guard_cfg = self.cfg.overexposure
        angle_fn = self._sun_angle_fn()
        period = sched.period

        # The angle that opens the session. With the preflight stage off this is
        # sun_max_angle, and everything below is what it always was.
        open_angle = pre.sun_start_angle if pre.enabled else sched.sun_max_angle
        activation = asi_schedule.sun_crossing_time(angle_fn, open_angle)
        dark_seconds = asi_schedule.estimate_cycle_dark_duration(
            sched.entries, sched.dark_frames, sched.dead_time)
        dark_start = activation - timedelta(seconds=dark_seconds)
        if pre.enabled:
            self._dash_update(
                schedule=f"preflight ≤ {pre.sun_start_angle:g}° → main ≤ "
                         f"{sched.sun_max_angle:g}°, cycle {period:.0f} s, "
                         f"window ~{activation.strftime('%H:%M')}")
            console_ui.log(f"Preflight stage from {pre.sun_start_angle:g}° to "
                           f"{sched.sun_max_angle:g}°, holding "
                           f"{pre.target_mean:.0f} ADU")
        else:
            self._dash_update(
                schedule=f"sun ≤ {sched.sun_max_angle:g}°, cycle {period:.0f} s, "
                         f"window ~{activation.strftime('%H:%M')}")

        if dark_start > dt.now():
            console_ui.log(f"Pre-darks start at {dark_start.strftime('%H:%M:%S')}, "
                           f"measurements ~{activation.strftime('%H:%M:%S')}")
            if not self._wait_until(dark_start, phase="waiting for pre-darks"):
                return
        self._capture_darks("initial")
        if self._stop_event.is_set():
            return

        try:
            self.cam.set_shutter(True)
        except Exception as exc:
            console_ui.warn(f"Could not open the shutter: {exc}")

        # The anchor is waited out *before* the loop rather than handed straight
        # to next_cycle_slot: asked for a slot while ``now`` is still ahead of
        # the anchor, that function answers with the previous (negative)
        # iteration, whose late entries fall before the window opens.
        anchor = asi_schedule.next_minute_boundary(max(activation, dt.now()))
        console_ui.log(f"Cycle anchored at {anchor.strftime('%H:%M:%S')}, "
                       f"period {period:.0f} s")
        if not self._wait_until(anchor, phase="waiting for the cycle anchor"):
            return

        auto = asi_exposure.AutoExposure(pre)
        guard = asi_exposure.SplitGuard(guard_cfg)
        # A run started mid-night has already missed the twilight; it goes
        # straight to the main programme rather than automating a dark sky.
        self._stage = ("preflight"
                       if pre.enabled and angle_fn(dt.now()) > sched.sun_max_angle
                       else "main")

        consecutive_errors = 0
        while not self._stop_event.is_set():
            now = dt.now()
            angle = angle_fn(now)
            if self._stage == "preflight" and angle <= sched.sun_max_angle:
                self._enter_main_stage(auto, sched)
            elif self._stage == "main" and angle > sched.sun_max_angle:
                console_ui.log("Sun above the threshold — measurements done.")
                break
            elif self._stage == "preflight" and angle > pre.sun_start_angle:
                console_ui.log("Sun above the preflight angle — measurements done.")
                break

            slot, entry, iteration = asi_schedule.next_cycle_slot(
                anchor, period, sched.entries, now)
            exposure, frames = self._plan_slot(entry, auto, guard)
            self._dash_update(
                detail=f"cycle {iteration}, Δ{entry.delta:g} s"
                       f"{self._intensity_text(exposure, frames)}",
                schedule=f"sun {angle:+.1f}° (threshold {sched.sun_max_angle:g}°)"
                         f"  ·  cycle {period:.0f} s  ·  iteration {iteration}"
                         f"{'  ·  preflight' if self._stage == 'preflight' else ''}")

            try:
                self.cam.prepare(entry, exposure=exposure, binning=self._slot_binning())
            except Exception as exc:
                console_ui.error(f"Could not prepare the cycle: {exc}")
                self._errors += 1
            self._refresh_camera_section()

            phase = "measuring" if self._stage == "main" else "measuring (preflight)"
            if not self._wait_until(slot, phase=phase):
                break
            if self._take_schedule_dirty():
                continue           # the anchor still holds; the slot does not

            # The sun moved while we waited. In the main stage that can only
            # close the window; in the preflight stage it can also hand over to
            # the main programme, and this same slot then shoots the scheduled
            # exposure instead of the automatic one.
            angle = angle_fn(dt.now())
            if self._stage == "preflight" and angle <= sched.sun_max_angle:
                self._enter_main_stage(auto, sched)
                exposure, frames = self._plan_slot(entry, auto, guard)
                try:
                    self.cam.prepare(entry, exposure=exposure)
                except Exception as exc:
                    console_ui.error(f"Could not prepare the cycle: {exc}")
                    self._errors += 1
            elif self._stage == "main" and angle > sched.sun_max_angle:
                console_ui.log("Window closed while waiting — skipping frame.")
                continue

            obs_mode = "sun_cycle_auto" if self._stage == "preflight" else "sun_cycle"
            ok, mean = self._capture_slot(entry, exposure, frames, obs_mode)
            self._feed_controllers(entry, exposure, mean, auto, guard)
            if ok:
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    console_ui.error(f"{consecutive_errors} consecutive capture "
                                     f"failures — stopping.")
                    break

    def _enter_main_stage(self, auto, sched):
        """Hand over from the automatic twilight stage to the programme proper."""
        self._stage = "main"
        self._auto_exposure = None
        auto.reset()
        console_ui.log(f"Sun at {sched.sun_max_angle:g}° — main cycle, "
                       f"scheduled exposures.")

    def _slot_binning(self):
        """The binning to force on this slot, or ``None`` to keep the entry's own.

        Every preflight slot holds one binning from config, whatever filter or
        exposure the schedule entry itself carries, right up to the handover
        to the main programme.
        """
        if self._stage == "preflight" and self.cfg.preflight.binning:
            return self.cfg.preflight.binning
        return None

    def _plan_slot(self, entry, auto, guard):
        """``(exposure, frames)`` for this visit to ``entry``.

        With both features off this is ``(entry.exposure, 1)``, so the camera
        gets exactly the calls it always got.
        """
        sched = self.cfg.schedule
        if self._stage == "preflight":
            budget = asi_schedule.slot_budget(sched.entries, sched.period, entry,
                                              sched.dead_time)
            exposure = auto.exposure_for(entry, budget)
            self._auto_exposure = exposure
            self._split_frames = 1
            return exposure, 1
        # The split guard owns the exposure in the main stage; a slot it has not
        # divided keeps the scheduled one untouched.
        frames, exposure = guard.plan(entry, self._slot_dead_time(entry))
        self._auto_exposure = None
        self._split_frames = frames
        return exposure, frames

    def _feed_controllers(self, entry, exposure, mean, auto, guard):
        """Give the slot's measured intensity to whichever loop is driving it."""
        if mean is None:
            return
        if self._stage == "preflight":
            bias = self._bias_for(exposure, self.cfg.preflight.bias)
            budget = asi_schedule.slot_budget(
                self.cfg.schedule.entries, self.cfg.schedule.period, entry,
                self.cfg.schedule.dead_time)
            old, new = auto.update(entry, exposure, mean, budget, bias=bias,
                                   saturated_fraction=self._last_saturation)
            if abs(new - old) > 1e-6:
                console_ui.log(f"Preflight {asi_exposure.slot_label(entry)}: "
                               f"mean {mean:.0f} ADU → exposure {new:g} s")
            return
        if not self.cfg.overexposure.enabled:
            return
        dead = self._slot_dead_time(entry)
        bias = self._bias_for(exposure, self.cfg.overexposure.bias)
        old, new = guard.update(entry, dead, mean, bias=bias)
        if new == old:
            # A slot whose dead time swallows the whole budget cannot be divided
            # at all. Saying so once matters: otherwise it over-exposes all night
            # with nothing in the log to explain why the guard did nothing.
            if guard.impossible(entry) and mean >= self.cfg.overexposure.threshold:
                key = asi_exposure.slot_key(entry)
                if key not in self._warned_slots:
                    self._warned_slots.add(key)
                    console_ui.warn(
                        f"{asi_exposure.slot_label(entry)}: mean {mean:.0f} ADU "
                        f"over {self.cfg.overexposure.threshold:.0f}, but "
                        f"{entry.exposure:g} s against {dead:g} s of dead time "
                        f"leaves no room to divide it")
            return
        if new > old:
            sub = asi_exposure.sub_exposure(entry.exposure, dead, new,
                                            self.cfg.overexposure.margin)
            console_ui.warn(
                f"{asi_exposure.slot_label(entry)}: mean {mean:.0f} ADU over "
                f"{self.cfg.overexposure.threshold:.0f} — next visit takes "
                f"{new} frames of {sub:g} s")
        elif new == 1:
            console_ui.log(f"{asi_exposure.slot_label(entry)}: back to one "
                           f"{entry.exposure:g} s frame")
        else:
            sub = asi_exposure.sub_exposure(entry.exposure, dead, new,
                                            self.cfg.overexposure.margin)
            console_ui.log(f"{asi_exposure.slot_label(entry)}: down to {new} "
                           f"frames of {sub:g} s")

    def _slot_dead_time(self, entry):
        """The save/readout time this slot budgets for, in seconds."""
        if entry.readout is not None:
            return float(entry.readout)
        return float(self.cfg.schedule.dead_time)

    def _intensity_text(self, exposure, frames):
        """The bit of the dashboard detail line the two loops own."""
        if self._stage == "preflight":
            return f"  ·  auto {exposure:g} s (target {self.cfg.preflight.target_mean:.0f} ADU)"
        if frames > 1:
            return f"  ·  split ×{frames} of {exposure:g} s"
        return ""

    # ------------------------------------------------------------------
    # MQTT commands
    # ------------------------------------------------------------------
    def _on_mqtt_command(self, topic, payload):
        if not self._bus.enabled:
            return
        if topic.endswith("/cmd/get_frame"):
            if self._last_frame is None:
                self._bus.publish_error("no_frame", "No frame captured yet")
                return
            ts = self._last_shot.isoformat() if self._last_shot else dt.now().isoformat()
            try:
                self._bus.publish_frame_array(self._last_frame, ts,
                                              params=self._current_params())
            except Exception as exc:
                self._bus.publish_error("error", str(exc), ts)
            return
        if topic.endswith("/cmd/capture_frame"):
            params, err = parse_command_params(payload)
            if err:
                self._bus.publish_error("bad_request", err, on_demand=True)
                return
            with self._pending_capture_lock:
                self._pending_capture = params
            self._pending_capture_event.set()
            self._bus.publish_note("accepted", f"Request queued with params: {params}")
            return
        console_ui.warn(f"Unknown MQTT command: {topic}")

    def _handle_pending_capture(self):
        """Serve an out-of-schedule ``cmd/capture_frame``.

        The frame is published, not archived: it is taken outside the observing
        programme and would otherwise appear in the science archive as if it
        belonged there.
        """
        with self._pending_capture_lock:
            params = self._pending_capture
            self._pending_capture = None
        self._pending_capture_event.clear()
        if params is None:
            return
        self._bus.publish_note("capturing", f"Applying params: {params}")
        applied, _ = self._apply_params(params)
        exposure = self.cam.current_exposure or 0.0
        if self._dash is not None:
            self._dash.capture_begin("ON-DEMAND", exposure)
        try:
            image = self.cam.capture()
        except Exception as exc:
            image = None
            console_ui.error(f"On-demand capture failed: {exc}")
        finally:
            if self._dash is not None:
                self._dash.capture_end()
        if image is None:
            self._bus.publish_error("error", "capture returned no image",
                                    ts_iso=dt.now().isoformat(), on_demand=True)
            return
        self._last_frame = image
        now = dt.now()
        if self._service is not None:
            self._service.publish_frame(image, now, {"image_type": "ON-DEMAND"})
        try:
            self._bus.publish_frame_array(image, now.isoformat(), on_demand=True,
                                          params=applied or self._current_params())
            console_ui.log("On-demand frame published")
        except Exception as exc:
            self._bus.publish_error("error", f"encode failed: {exc}",
                                    ts_iso=now.isoformat(), on_demand=True)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def run(self):
        self._bus.prepare_status_dir()
        self._bus.subscribe(self._on_mqtt_command)
        if self._service is not None:
            self._service.set_current_params(self._current_params())
        cooling = self._refresh_camera_section()
        self._save_status("starting", force=True, cooling=cooling)
        if self.setup_mode:
            console_ui.log("ASI worker started in setup mode — no schedule, "
                           "no darks, live frames on request")
        else:
            console_ui.log(f"ASI worker started — mode '{self.cfg.schedule.mode}', "
                           f"{len(self.cfg.schedule.entries)} schedule entr"
                           f"{'y' if len(self.cfg.schedule.entries) == 1 else 'ies'}")

        try:
            if self.setup_mode:
                self._run_setup_mode()
            elif self._wait_for_start():
                mode = self.cfg.schedule.mode
                if mode == "time":
                    self._run_time_mode()
                else:
                    # A night's window closing at sunrise is not a reason to end
                    # the run: sun/sun_cycle mode functions return once their
                    # session is over, so this is what keeps the worker going
                    # into the next night instead of exiting the whole program.
                    run_night = (self._run_sun_cycle_mode if mode == "sun_cycle"
                                else self._run_sun_mode)
                    while not self._stop_event.is_set():
                        run_night()
        except Exception as exc:
            self._errors += 1
            console_ui.error(f"Measurement loop failed: {exc}")
            self._save_status("error", force=True)
        finally:
            try:
                self.cam.set_shutter(False)
            except Exception:
                pass
            # Darks bracket a measurement run; in setup mode there was none, and
            # the exposure combinations they are built from are empty anyway.
            # And if no light frame was ever taken — stopped while still waiting
            # for the start signal, the pre-darks, the sun, or the cycle anchor —
            # there is no run to bracket either, so a Ctrl+C or systemctl stop
            # there must not cost a set of darks for measurements that never ran.
            if not self._force_quit and not self.setup_mode and self._shots > 0:
                self._set_phase("closing darks")
                try:
                    self._capture_darks("final")
                except Exception as exc:
                    console_ui.error(f"Closing dark frames failed: {exc}")
            self._set_phase("stopped")
            self._save_status("stopped", force=True)
            self._bus.shutdown()
            console_ui.log(f"ASI worker stopped — {self._shots} light, "
                           f"{self._darks} dark, {self._errors} error(s)")


# ---------------------------------------------------------------------------
# Preview mode
# ---------------------------------------------------------------------------
def run_preview_asi(cam: AsiCamera, instance_name, dashboard=None):
    """Capture continuously into ``preview_{instance}.fits`` at maximum rate."""
    preview_path = os.path.join(APP_DIR, f"preview_{instance_name}.fits")
    tmp_path = preview_path + ".tmp"
    console_ui.log(f"Preview mode: writing {preview_path}")

    stop = threading.Event()

    def _stop(sig, frame):
        console_ui.log(f"{stop_signal_name(sig)} — stopping preview…")
        stop.set()
    install_stop_handler(_stop)

    frames = 0
    t0 = dt.now()
    try:
        cam.set_shutter(True)
    except Exception as exc:
        console_ui.warn(f"Could not open the shutter: {exc}")

    while not stop.is_set():
        exposure = cam.current_exposure or 0.0
        if dashboard is not None:
            dashboard.capture_begin("PREVIEW", exposure)
        image = cam.capture()
        if dashboard is not None:
            dashboard.capture_end()
        if image is None:
            console_ui.warn("Preview: capture returned no image")
            time.sleep(0.5)
            continue
        try:
            _write_preview_fits(tmp_path, image)
            os.replace(tmp_path, preview_path)
        except Exception as exc:
            console_ui.warn(f"Preview write failed: {exc}")
            continue
        frames += 1
        elapsed = (dt.now() - t0).total_seconds()
        fps = frames / elapsed if elapsed > 0 else 0.0
        if dashboard is not None:
            dashboard.update(frames=frames, detail=f"{fps:.2f} fps",
                             last_file=os.path.basename(preview_path))
    console_ui.log(f"Preview stopped after {frames} frame(s)")


def _write_preview_fits(path, image):
    from astropy.io import fits
    fits.writeto(path, image, overwrite=True)


def publish_setpoint_limits(service, cam):
    """Replace the guessed setpoint range in the focus_app schema with the real one.

    The defaults in ``camera_service.PARAM_SCHEMAS`` have to exist before any
    camera is connected, so they are a guess; an open camera knows better and
    the slider should not offer temperatures it will refuse.
    """
    from camera_service import PARAM_SCHEMAS

    limits = cam.temperature_limits()
    if limits is None:
        return
    if isinstance(limits, list):
        if not limits:
            return
        low, high, step = min(limits), max(limits), 0.0
    else:
        low, high, step = limits.minimum, limits.maximum, limits.increment
    schema = []
    for field in PARAM_SCHEMAS.get("asi", []):
        field = dict(field)
        if field.get("name") == "target_temp":
            field["min"], field["max"] = float(low), float(high)
            if step:
                field["step"] = float(step)
        schema.append(field)
    service.set_param_schema(schema)


def run_probe_asi(config_path=None):
    """Print what this camera accepts for the parameters the driver sets.

    Nothing is written to the camera, so this still works when a value in
    config.json is exactly the one the camera refuses — which is the situation
    it exists for: PICAM answers a bad value with "Invalid Parameter Value" and
    no hint of what would have been accepted.
    """
    from utils import load_config
    from cameras.asi.picam import PicamParameter as P, PicamEnumeratedType as E

    conf = asi_config.from_dict(load_config(config_path).get("asi", {}))
    if conf.camera.backend != "picam":
        from cameras.asi.camera_sim import SETPOINT_LIMITS
        print(f"Backend is {conf.camera.backend!r}, so there is no camera to ask.")
        print(f"The simulator accepts setpoints: "
              f"{picam.describe_constraint(SETPOINT_LIMITS)}")
        return

    # Enumerated parameters read back as numbers; show the names too.
    enums = {
        P.ShutterTimingMode: E.ShutterTimingMode,
        P.AdcAnalogGain: E.AdcAnalogGain,
        P.OutputSignal: E.OutputSignal,
        P.ReadoutControlMode: E.ReadoutControlMode,
        P.PixelFormat: E.PixelFormat,
    }
    parameters = [
        P.SensorTemperatureSetPoint, P.SensorTemperatureReading, P.AdcSpeed,
        P.AdcAnalogGain, P.AdcQuality, P.ReadoutControlMode, P.ShutterTimingMode,
        P.OutputSignal, P.KineticsWindowHeight, P.ExposureTime, P.PixelFormat,
        P.PixelBitDepth,
    ]

    print("Opening the first camera PICAM reports…")
    picam.initialize_library()
    handle = None
    try:
        handle = picam.open_first_camera()
        cid = picam.get_camera_id(handle)
        width, height = picam.sensor_size(handle)
        print(f"Model:   {picam.enum_string(E.Model, cid.model)}")
        print(f"Serial:  {cid.serial_number.decode(errors='replace')}")
        print(f"Bus:     {picam.enum_string(E.ComputerInterface, cid.computer_interface)}")
        print(f"Sensor:  {cid.sensor_name.decode(errors='replace')}  ({width}x{height})")
        print(f"PICAM:   {picam.library_version()}")
        print()
        print(f"{'Parameter':<28}{'Current':>14}   Accepted")
        print("-" * 100)
        for parameter in parameters:
            name = picam.enum_string(E.Parameter, int(parameter))
            print(f"{name:<28}{_probe_current(handle, parameter, enums):>14}"
                  f"   {_probe_allowed(handle, parameter, enums)}")
        print()
        print("asi.cooling.target_temp in config.json has to be a value the "
              "SensorTemperatureSetPoint row allows.")
    finally:
        if handle is not None:
            picam.close_camera(handle)
        picam.uninitialize_library()


def _probe_current(handle, parameter, enums):
    """The value the camera currently holds for one parameter, as text."""
    try:
        if picam.value_type(parameter) is picam.PicamValueType.FloatingPoint:
            try:
                return f"{picam.get_float(handle, parameter):g}"
            except picam.PicamError:
                # Live-only values (the temperature reading) have no cached one.
                return f"{picam.read_float(handle, parameter):g}"
        value = picam.get_int(handle, parameter)
    except picam.PicamError:
        return "n/a"
    enum_type = enums.get(parameter)
    if enum_type is None:
        return str(value)
    return f"{value} ({picam.enum_string(enum_type, value)})"


def _probe_allowed(handle, parameter, enums):
    """What the camera would accept for one parameter, as text."""
    limits = picam.constraint(handle, parameter)
    enum_type = enums.get(parameter)
    if enum_type is not None and isinstance(limits, list):
        return ", ".join(
            f"{int(v)} ({picam.enum_string(enum_type, int(v))})" for v in limits
        )
    return picam.describe_constraint(limits)


# ---------------------------------------------------------------------------
# Console entry point
# ---------------------------------------------------------------------------
def run_console_asi(config_path=None, preview=False, verbose=False,
                    setup_mode=False):
    """Run the ASI camera in console mode."""
    from utils import load_config, configure_console_asi
    from mqtt_client import create_console_publisher
    from camera_service import CameraService
    from frame_server import start_frame_server

    cfg = load_config(config_path)
    asi_cfg = cfg.get("asi", {})
    mqtt_cfg = cfg.get("mqtt", {})

    node_name = get_node_name(cfg)
    status_dir = cfg.get("status_dir") or str(Path.home() / ".every_camera" / "status")

    # The wizard needs a plain terminal, so it runs before the dashboard starts.
    if not preview and not asi_cfg.get("output_dir"):
        configure_console_asi(cfg, config_path)
        cfg = load_config(config_path)
        asi_cfg = cfg.get("asi", {})
        mqtt_cfg = cfg.get("mqtt", {})
        node_name = get_node_name(cfg)

    claim = claim_instance_name(asi_cfg.get("instance_name")
                                or get_instance_name("ASI", cfg))
    instance_name = claim.name

    conf = asi_config.from_dict(asi_cfg)
    dash = console_ui.start_dashboard(
        "asi", instance_name, verbose=verbose,
        footer="Ctrl+C — finish the frame, shoot the closing darks, stop")
    dash.update(status="starting", node_name=node_name,
                output_dir=conf.output_dir, frames=0, darks=0, errors=0)

    cam = None
    server = None
    mqtt_pub = None
    worker = None
    try:
        for problem in conf.errors:
            console_ui.warn(problem)
        console_ui.log(f"Instance {instance_name} on {node_name} — backend "
                       f"{conf.camera.backend}, wheel {conf.filter_wheel.port}")

        if not conf.output_dir:
            console_ui.error("asi.output_dir is not set — nothing would be saved.")
            return
        os.makedirs(conf.output_dir, exist_ok=True)
        os.makedirs(status_dir, exist_ok=True)

        if not preview and (setup_mode or not conf.schedule.entries):
            setup_mode = True
            announce_setup_mode(
                "requested with --setup" if conf.schedule.entries else
                empty_schedule_reason(
                    "asi",
                    asi_cfg.get("schedule") or asi_cfg.get("schedule_file"),
                    conf.errors))

        console_ui.log("Opening the camera (cooling may take a while)…")
        cam = AsiCamera(conf).open()
        dash.set_section("device", cam.info_rows())

        if preview:
            run_preview_asi(cam, instance_name, dashboard=dash)
            return

        mqtt_pub = create_console_publisher(mqtt_cfg, instance_name, "asi")
        if mqtt_pub:
            dash.update(mqtt=f"{mqtt_cfg.get('host', '?')} — connected")

        service = CameraService("asi", instance_name, conf.output_dir,
                                node_name=node_name, setup_mode=setup_mode)
        service.set_schedule(asi_schedule.schedule_snapshot(conf.schedule))
        publish_setpoint_limits(service, cam)
        server = start_frame_server(cfg.get("server", {}), service)
        if server:
            dash.update(server_url=server.url, focus="idle")
            if setup_mode:
                console_ui.log(f"Focus this camera with: python focus_app.py "
                               f"--host {get_local_ip()} --port {server.port}")

        worker = AsiWorkerConsole(
            cam=cam,
            cfg=conf,
            output_dir=conf.output_dir,
            instance_name=instance_name,
            status_dir=status_dir,
            mqtt_publisher=mqtt_pub,
            mqtt_prefix=mqtt_cfg.get("prefix", "every_camera"),
            service=service,
            dashboard=dash,
            node_name=node_name,
            setup_mode=setup_mode,
        )

        def _stop(sig, frame):
            name = stop_signal_name(sig)
            if worker.stopping:
                console_ui.warn(f"Second {name} — quitting without closing darks.")
                worker.request_stop(force=True)
                return
            # The second signal is the operator's escape hatch, so it must stay
            # available under a service manager too: systemd sends SIGTERM again
            # only when TimeoutStopSec runs out, and by then this is exactly the
            # behaviour wanted — give up the darks rather than be SIGKILLed
            # mid-exposure.
            console_ui.log(f"{name} — finishing the current frame, then closing "
                           f"darks. Send it again to quit at once.")
            worker.request_stop()
        install_stop_handler(_stop)

        worker.start()
        while worker.is_alive():
            worker.join(timeout=0.5)
    finally:
        # An exception on the way out must not leave the worker exposing while
        # the camera is being closed underneath it.
        if worker is not None and worker.is_alive():
            worker.request_stop(force=True)
            worker.join(timeout=30)
        if server:
            server.stop()
        if mqtt_pub:
            try:
                mqtt_pub.disconnect_broker()
            except Exception:
                pass
        if cam:
            cam.close()
        dash.stop()
        claim.release()
