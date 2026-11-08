"""Japan all-sky imager: Hamamatsu camera (DCAM-API) + filter wheel.

Every DCAM and serial call happens on the worker thread: two threads must never
issue DCAM calls at once. HTTP handlers reach the camera only through CameraService.

Every wait runs through JapanWorkerConsole._service_tick: the gaps between exposures
are where parameters, focus frames and status are served. A focus frame is taken
only when it fits before the next slot, so live view can never delay a measurement.

The Hamamatsu has no analog gain and no managed setpoint: cooling is a switch
(japan.cooling), thrown lead_minutes before the pre-darks and again once a night is
over, and only on a model that lets DCAM throw it.

Schedule modes (japan.mode):
  sun        entries on given seconds of every minute while the sun is at or below
             sun_max_angle; frames filed flat
  time       one cycle phase-locked to t_start; frames filed flat
  sun_cycle  that cycle started by the sun, with t_start read as UTC so stations
             at different sites run in step; the only mode with the preflight and
             overexposure loops, filed in the ASI archive layout
"""
import os
import sys
import threading
import time

from datetime import datetime as dt, timedelta
from pathlib import Path

import alerts

import console_ui

from utils import (
    claim_instance_name, get_instance_name, get_local_ip, get_node_name,
    get_system_info, APP_DIR,
)
from worker_common import (
    WorkerBus, publish_current_params,
    publish_schedule_state, serving_focus_hold,
    run_focus_iteration, announce_setup_mode, empty_schedule_reason, SETUP_STATUS,
    install_stop_handler, stop_signal_name,
)

from cameras.japan import config as japan_config
from cameras.japan import devices, paths as japan_paths
from cameras.japan.fits import write_fits, write_sun_cycle_fits
from cameras.common import archive_paths
# A module, not names: tests reach the controllers through
# ``japan_driver.japan_exposure``.
from cameras.common import exposure as japan_exposure
from cameras.common.intensity_control import IntensityController, shoot_split
from cameras.common.seqno import next_seqno
# A module, not names: tests patch the timing helpers through
# ``japan_driver.japan_schedule``.
from cameras.common import schedule as japan_schedule
# Safe here: the wheel module imports pyserial only when it opens a port, so a
# machine with no hardware still imports this driver.
from cameras.common.filterwheel import HOME as FILTER_HOME

# How much slack a focus frame needs before the next scheduled capture: the
# exposure itself, readout, and a margin. Below this the frame is skipped.
FOCUS_SLACK_FACTOR = 1.4
FOCUS_SLACK_SECONDS = 3.0

# Longest single sleep inside a wait loop. Short enough that a stop request or a
# focus session starts being served promptly.
TICK = 0.2

# A remote value arrives as JSON from focus_app or hand-typed in a config, and
# "closed" must never be truthy just because it is a non-empty string.
_TRUE_WORDS = {"1", "true", "yes", "on", "open", "opened"}
_FALSE_WORDS = {"0", "false", "no", "off", "closed", "close", "shut"}


def _as_bool(value):
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
    """Home is a place; unknown is a failed move."""
    if position is None:
        return "unknown"
    return "home" if position == FILTER_HOME else str(position)


class JapanCamera:
    """Camera and filter wheel with one lifecycle: an exposure is meaningless without a
    known filter and shutter state.
    """

    def __init__(self, cfg: japan_config.JapanConfig):
        self.cfg = cfg
        self.cam = None
        self.wheel = None
        self._info = {}
        self._opened = False
        # True once ``japan.cooling`` asked for the cooler and the camera allows it.
        # Otherwise everything cooling-related is a no-op and the camera cools itself.
        self.cooling_controlled = False
        # Whether "off" is among the modes the camera accepts. Some models (a
        # C11090-22B: SENSORCOOLER 2..4) only go between on and max, and every
        # attempt to switch them off is refused.
        self.cooler_off_allowed = False

    def open(self):
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
        # The controller never volunteers the shutter's state, so command it shut: what
        # focus_app is told is then a fact, not an assumption.
        try:
            self.set_shutter(False)
        except Exception as exc:
            console_ui.warn(f"Could not close the shutter at startup: {exc}")
        self._info = self.cam.info()
        self.cooling_controlled = self._cooling_available()
        return self

    def _cooling_available(self):
        if not self.cfg.cooling.enabled:
            return False
        try:
            supported = self.cam.cooler_supported()
        except Exception as exc:
            console_ui.warn(f"Could not ask the camera about its cooler: {exc}")
            supported = False
        if not supported:
            console_ui.warn("japan.cooling is enabled, but this camera does not let "
                            "its cooler be switched over DCAM — leaving it to "
                            "cool itself")
            return False
        try:
            self.cooler_off_allowed = self.cam.cooler_can_switch_off()
        except Exception as exc:
            console_ui.warn(f"Could not ask the camera whether its cooler can be "
                            f"switched off: {exc}")
            self.cooler_off_allowed = True     # try, and hear about a refusal
        if not self.cooler_off_allowed:
            console_ui.log("This camera cannot switch its cooler off over DCAM "
                           "(SENSORCOOLER accepts only on/max) — it stays on "
                           "between nights")
        return True

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
    def current_readout_speed(self):
        return self.cam.current_readout_speed

    @property
    def current_filter(self):
        """Wheel position: 0 at home, 1..6 in a filter, None if unknown."""
        return self.wheel.current_filter

    @property
    def filter_number(self):
        """An unconfirmed move is recorded as 0.

        Nothing else may read ``wheel.current_filter`` for a name or a header: None would
        file the frame as ``..._None.fits`` and leave FILTER blank.
        """
        position = self.wheel.current_filter
        return FILTER_HOME if position is None else position

    @property
    def shutter_open(self):
        """True/False once the shutter has been commanded, None while unknown."""
        return getattr(self.wheel, "shutter_open", None)

    @property
    def shutter_wanted(self):
        """Last commanded state, even unacknowledged; None before any command."""
        return getattr(self.wheel, "shutter_wanted",
                       getattr(self.wheel, "shutter_open", None))

    def temperature(self):
        return self.cam.temperature()

    @property
    def target_temperature(self):
        """The cooling target, only while this program drives the cooler."""
        if not self.cooling_controlled:
            return None
        return self.cam.target_temperature

    def cooler_status(self):
        if not self.cooling_controlled:
            return None
        return self.cam.cooler_status()

    def info_rows(self):
        return self.cam.info_rows()

    def set_exposure(self, seconds):
        self.cam.set_exposure(float(seconds))

    def set_binning(self, binning):
        self.cam.set_binning(int(binning))

    def set_readout_speed(self, speed):
        self.cam.set_readout_speed(int(speed))

    def select_filter(self, number):
        return self.wheel.select(int(number))

    def set_shutter(self, is_open):
        self.wheel.set_shutter(bool(is_open))

    def set_cooler(self, on):
        """Switch the cooler. False when it is not this program's to switch, or
        when the camera refused."""
        if not self.cooling_controlled:
            return False
        target = self.cfg.cooling.target_temp
        if on and target is not None:
            self.cam.set_target_temperature(target)
        return bool(self.cam.set_cooler(bool(on)))

    def capture(self):
        return self.cam.capture()

    def prepare(self, entry, exposure=None, binning=None):
        """Apply one schedule entry; True when everything asked for is set.

        A filter that did not arrive gives False, having already said so. ``exposure`` and
        ``binning`` override the entry's own: the camera has to hold exactly what the file
        name and EXPTIME will claim.
        """
        wanted = entry.exposure if exposure is None else float(exposure)
        if wanted is not None and self.current_exposure != wanted:
            self.set_exposure(wanted)
        wanted_binning = entry.binning if binning is None else int(binning)
        if wanted_binning and self.current_binning != wanted_binning:
            self.set_binning(wanted_binning)
        if entry.filter and self.current_filter != entry.filter:
            return self.select_filter(entry.filter)
        return True

    def temperature_fields(self):
        """Worker thread only."""
        fields = {"ccd_temp": self.temperature()}
        if self.cooling_controlled:
            fields["cooler"] = self.cooler_status()
            fields["setpoint"] = self.target_temperature
        return fields


class JapanWorkerConsole(threading.Thread):
    MAX_CONSECUTIVE_ERRORS = 5

    # Seconds the frame name may be nudged forward: enough for a run of back-to-back
    # darks (``dark_frames`` up to 9), the case that actually collides.
    NAME_ATTEMPTS = 10

    def __init__(self, cam: JapanCamera, cfg: japan_config.JapanConfig, output_dir,
                 instance_name, status_dir, service=None, dashboard=None,
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
        self._bus = WorkerBus("japan", instance_name, status_dir,
                              service=service, node_name=node_name)

        self._stop_event = threading.Event()
        self._force_quit = False
        self._shots = 0
        # Lights of the current session, not the run: in sun mode one run spans many nights,
        # and a stop at noon must not shoot darks for a night that ended hours earlier.
        self._session_shots = 0
        self._darks = 0
        self._errors = 0
        self._last_shot = None
        self._last_file = None
        self._last_frame = None
        self._last_mean = None
        self._last_saturation = 0.0
        self._intensity = IntensityController(
            cfg.preflight, cfg.overexposure, budget=self._preflight_budget,
            dead_time=lambda: self.cfg.schedule.dead_time,
            split_runs=self._split_guard_runs)
        self._phase = "starting"
        self._next_slot = None
        # Set when a focus hold swallowed the slot we were waiting for, so the
        # mode loops know to plan again instead of shooting a stale target.
        self._schedule_dirty = False
        self._params_touched = False
        # Why the worker gave up, if it did: the process then exits as failed.
        self.failed = None
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
        self._shutter_after_focus = None
        # What this program last told the cooler: None until it has said
        # anything, so the first command always goes out.
        self._cooler_commanded = None

    def request_stop(self, force=False):
        """Ask the run to end. The first call still shoots the closing darks."""
        if force or self._stop_event.is_set():
            self._force_quit = True
        self._stop_event.set()
        self._started_event.set()      # unblock the "press Enter" wait

    @property
    def stopping(self):
        return self._stop_event.is_set()

    def _dash_update(self, **fields):
        if self._dash is not None:
            self._dash.update(**fields)

    def _camera_rows(self, readings=None):
        readings = readings if readings is not None else {}
        temp = readings.get("ccd_temp")
        temp_text = f"{temp:.2f} °C" if isinstance(temp, (int, float)) else "n/a"
        if "cooler" in readings:
            temp_text += f"  ·  cooler {readings['cooler'] or 'unknown'}"
            if readings.get("setpoint") is not None:
                temp_text += f", target {readings['setpoint']:.1f} °C"
        binning = self.cam.current_binning or 1
        exposure = self.cam.current_exposure
        rows = [
            ("Exposure / binning:",
             f"{exposure:g} s  ·  {binning}x{binning}" if exposure
             else f"-  ·  {binning}x{binning}"),
            ("Readout speed:",
             japan_config.readout_text(self.cam.current_readout_speed)),
            ("Filter:", _filter_text(self.cam.current_filter)),
            ("Shutter:", _shutter_text(self.cam.shutter_open)),
            ("Sensor temperature:", temp_text),
        ]
        # Only shown while something is actually driving the intensity, so the
        # block keeps its usual five rows on an ordinary run.
        control = self._intensity.row(self._last_mean)
        if control:
            rows.append(("Intensity control:", control))
        return rows

    @property
    def _stage(self):
        return self._intensity.stage

    @property
    def _auto_exposure(self):
        return self._intensity.auto_exposure

    def _preflight_budget(self, entry):
        sched = self.cfg.schedule
        return japan_schedule.slot_budget(sched.entries, sched.period, entry,
                                          sched.dead_time)

    def _refresh_camera_section(self, readings=None):
        if readings is None:
            readings = self.cam.temperature_fields()
        if self._dash is not None:
            self._dash.set_section("camera", self._camera_rows(readings))
        return readings

    def _save_status(self, status, force=False, readings=None):
        readings = readings or {}
        # The schedule moves the filter, the exposure and the shutter on its
        # own; focus_app only ever sees what is published from here.
        publish_current_params(self._service, self._current_params)
        publish_schedule_state(self._service, self._measuring_now())
        payload = {
            "instance_name": self.instance_name,
            "camera_type": "japan",
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
            "readout_speed": self.cam.current_readout_speed,
            "filter": self.cam.current_filter,
            "shutter": self.cam.shutter_open,
            "next_slot": self._next_slot.isoformat() if self._next_slot else None,
            "setup_mode": self.setup_mode,
            # Same keys as the ASI imager, so one monitor reads both.
            **self._intensity.status_fields(self._last_mean),
            "last_update": dt.now().isoformat(),
        }
        if readings.get("ccd_temp") is not None:
            payload["ccd_temp"] = round(float(readings["ccd_temp"]), 2)
        # A target only while this program drives the cooler; otherwise the
        # monitor shows a reading rather than a reading against a target.
        if "cooler" in readings:
            payload["cooler"] = readings["cooler"]
        if readings.get("setpoint") is not None:
            payload["set_temp"] = readings["setpoint"]
            payload["temp_locked"] = readings.get("cooler") == "ready"
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

    def _current_params(self):
        return {
            "exposure": self.cam.current_exposure,
            "binning": self.cam.current_binning,
            "readout_speed": self.cam.current_readout_speed,
            "filter": self.cam.current_filter,
            "shutter": self.cam.shutter_open,
        }

    def _apply_params(self, params):
        """Worker thread only."""
        applied, errors = {}, []
        for name, value in (params or {}).items():
            try:
                if name == "exposure":
                    self.cam.set_exposure(float(value))
                    applied[name] = float(value)
                elif name == "binning":
                    binning = int(value)
                    if not (japan_config.BINNING_MIN <= binning
                            <= japan_config.BINNING_MAX):
                        raise ValueError(f"binning must be "
                                         f"{japan_config.BINNING_MIN}.."
                                         f"{japan_config.BINNING_MAX}")
                    self.cam.set_binning(binning)
                    applied[name] = binning
                elif name == "readout_speed":
                    speed = int(value)
                    if speed not in japan_config.READOUT_SPEEDS:
                        raise ValueError("readout_speed must be 1 (slow) or 2 (fast)")
                    self.cam.set_readout_speed(speed)
                    applied[name] = speed
                elif name == "filter":
                    number = int(value)
                    # Home is a state the wheel starts in, not a position to be
                    # sent to: the schedule always names a real filter.
                    if not (japan_schedule.FILTER_MIN <= number
                            <= japan_schedule.FILTER_MAX):
                        raise ValueError(
                            f"filter must be {japan_schedule.FILTER_MIN}.."
                            f"{japan_schedule.FILTER_MAX}")
                    if self.cam.select_filter(number):
                        applied[name] = number
                    else:
                        errors.append(f"filter: wheel did not reach position {number}")
                elif name == "shutter":
                    is_open = _as_bool(value)
                    if not self._focus_shutter_manual:
                        # Put back when the session ends: a shutter left shut by hand
                        # would turn every later frame into a dark, one left open in the
                        # day lets the sun in.
                        self._shutter_after_focus = (
                            False if self._focus_opened_shutter
                            else self.cam.shutter_wanted)
                    self.cam.set_shutter(is_open)
                    applied[name] = is_open
                    # The operator owns the shutter for the rest of the session: focus opening it by
                    # itself must never undo somebody deliberately closing it.
                    self._focus_shutter_manual = True
                    self._focus_opened_shutter = False
                    # Closing it during a run turns every scheduled frame into a dark.
                    if not is_open and not self.setup_mode:
                        console_ui.warn(
                            "Shutter closed remotely — scheduled frames stay "
                            "dark until it is opened again")
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
        """Safe only between exposures."""
        if self._service is None or not self._service.has_param_request():
            return
        req_id, params = self._service.take_param_request()
        if not req_id:
            return
        applied, errors = self._apply_params(params)
        self._params_touched = True
        self._service.complete_param_request(req_id, applied, errors)
        self._service.set_current_params(self._current_params())
        self._refresh_camera_section()

    def _focus_note(self, text=""):
        if self._service is not None:
            self._service.set_focus_note(text)

    def _shutter_for_focus(self):
        """Open the shutter for a focus session; put it back when one ends.

        A shut shutter gives black frames, which look like a broken camera. A dark run keeps
        the shutter: opening it there would turn darks into light frames.
        """
        if self._darks_running:
            return False
        active = self._service is not None and self._service.focus_active()
        if not active:
            self._focus_close_shutter()
            if self._focus_shutter_manual:
                self._focus_shutter_manual = False
                self._restore_manual_shutter()
            return False
        # Set by hand from focus_app: that outranks any guess made here.
        if self._focus_shutter_manual:
            return True
        if not self.cam.shutter_wanted and not self._focus_opened_shutter:
            try:
                self.cam.set_shutter(True)
            except Exception as exc:
                console_ui.warn(f"Could not open the shutter for focusing: {exc}")
                return False
            self._focus_opened_shutter = True
            console_ui.log("Focus session — shutter opened (closed again when it ends)")
            self._refresh_camera_section()
        return True

    def _restore_manual_shutter(self):
        wanted, self._shutter_after_focus = self._shutter_after_focus, None
        # Reopened only inside a measurement: the night may have ended during the
        # session, and an open shutter in daylight is the worse mistake.
        if wanted and not str(self._phase).startswith("measuring"):
            return
        if wanted is None or self.cam.shutter_wanted == wanted:
            return
        try:
            self.cam.set_shutter(wanted)
            console_ui.log(f"Focus session over — shutter "
                           f"{'opened' if wanted else 'closed'} again")
        except Exception as exc:
            console_ui.warn(f"Could not restore the shutter after focusing: {exc}")

    def _focus_close_shutter(self):
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
            # A live frame must never push a scheduled one late. Say so: a silent refusal
            # looks like a broken camera.
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

    def _service_tick(self, slack, status="running", last_status=0.0, readings=None,
                      allow_focus=True):
        """Everything that has to keep happening while the worker is not exposing.

        Every wait in this driver runs through here, so a focus session works whatever the
        worker is doing; a hand-rolled copy of this loop falls out of step.

        Returns ``(last_status, readings)`` so a caller can throttle status across ticks.
        """
        started = time.monotonic()
        if started - last_status >= 2.0:
            last_status = started
            readings = self._refresh_camera_section()
            self._save_status(SETUP_STATUS if self.setup_mode else status,
                              readings=readings)
        self._serve_params()
        if allow_focus:
            self._shutter_for_focus()
            # A filter move in _serve_params can eat most of the slack.
            if slack is not None:
                slack -= time.monotonic() - started
            self._serve_focus(slack)
        return last_status, readings

    def _wait_until(self, target, phase=None, allow_focus=True):
        """Idle until ``target`` through _service_tick; False if a stop was requested."""
        if phase:
            self._set_phase(phase)
        # In setup mode this is an idle tick, not a capture the monitor should expect.
        self._next_slot = (target if isinstance(target, dt) and not self.setup_mode
                           else None)
        self._dash_update(next_at=self._next_slot)
        last_status = 0.0
        readings = None
        # Before the wait too: a slot already past leaves the loop at once, and on a tight
        # schedule that is every slot, so a hold checked only in the loop is never seen.
        if allow_focus and self._hold_for_focus():
            self._next_slot = None
            self._dash_update(next_at=None)
            return not self._stop_event.is_set()
        while not self._stop_event.is_set():
            remaining = (target - dt.now()).total_seconds()
            if remaining <= 0:
                break
            # Before the tick: a hold serves its own frames with no slack limit, so the tick
            # would take or refuse one slack-limited frame for no reason.
            if allow_focus and self._hold_for_focus():
                # The slot is gone; the caller re-reads the schedule.
                break
            last_status, readings = self._service_tick(
                remaining, last_status=last_status, readings=readings,
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

        The operator agrees to this in focus_app. A capture under way is never interrupted:
        this is only reached between them. An abandoned session releases the camera at its
        deadline.
        """
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
        readings = None
        while not self._stop_event.is_set() and self._service.focus_hold():
            # ``slack=None``: no slot for a live frame to be late for. This step is also what
            # opens the shutter for the session.
            last_status, readings = self._service_tick(
                None, status=SETUP_STATUS, last_status=last_status,
                readings=readings)
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

    def _closing_darks_due(self):
        """Darks bracket a session that had lights in it.

        A stop before the first light or between nights must not cost a full set of darks.
        """
        if self._force_quit or self.setup_mode:
            return False
        return self._session_shots > 0

    def _prepare_entry(self, entry, **kwargs):
        """Apply one schedule entry; False if the preparation itself broke.

        A filter that did not arrive is not that: the frame is still worth taking and is
        filed with what the wheel reports, so the failure is counted and published instead.
        """
        try:
            arrived = self.cam.prepare(entry, **kwargs)
        except Exception as exc:
            console_ui.error(f"Could not prepare filter {entry.filter}: {exc}")
            self._errors += 1
            self._dash_update(errors=self._errors)
            return False
        if not arrived:
            self._errors += 1
            self._dash_update(errors=self._errors)
            console_ui.error(
                f"Filter {entry.filter} was not reached — the wheel reports "
                f"{_filter_text(self.cam.current_filter)}; this frame is filed "
                f"with what the wheel reports, not with what the schedule asked for")
            self._bus.publish_error(
                "error", f"filter wheel did not reach position {entry.filter}")
        return True

    def _give_up(self, reason):
        console_ui.error(f"{reason} — stopping.")
        self.failed = reason
        self._stop_event.set()

    def _take_schedule_dirty(self):
        """True once after a focus hold, so a mode loop plans its next slot again.

        The awaited slot passed during the hold and is not made up: shooting it now would
        file a frame under a time gone by.
        """
        dirty = self._schedule_dirty
        self._schedule_dirty = False
        return dirty

    def _wait_seconds(self, seconds, phase=None, allow_focus=True):
        return self._wait_until(dt.now() + timedelta(seconds=seconds), phase,
                                allow_focus)

    def _measuring_now(self):
        """What focus_app asks before it connects.

        The gap between two slots counts: taking the camera there costs the next frame.
        Waiting for the window to open does not, so a camera idling through the day can be
        focused with no confirmation.
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

    @property
    def _archive_layout(self):
        """True in ``sun_cycle`` mode, darks included: a session's darks are filed beside its
        lights, or the processing program would never pair them.
        """
        return self.cfg.schedule.mode == "sun_cycle"

    def _write_frame(self, path, image, timestamp, exposure, image_type, obs_mode,
                     readings=None, sky_mean=None, split_count=1, split_index=1):
        readings = readings or {}
        if self._archive_layout:
            filt = self.cfg.filter_info(self.cam.filter_number)
            write_sun_cycle_fits(
                Path(path), image,
                timestamp=timestamp,
                exposure_sec=exposure,
                binning=self.cam.current_binning,
                readout_speed=self.cam.current_readout_speed,
                readout_speed_text=japan_config.readout_text(
                    self.cam.current_readout_speed),
                filter_num=self.cam.filter_number,
                ccd_temp=readings.get("ccd_temp"),
                image_type=image_type,
                obs_mode=obs_mode,
                name=self.cfg.name,
                site_name=self.cfg.location.name,
                lat=self.cfg.location.lat,
                lon=self.cfg.location.lon,
                elevation=self.cfg.location.elevation,
                seqno=next_seqno(self.output_dir),
                filter_wavelength=filt.wavelength,
                filter_description=filt.description,
                sky_mean=sky_mean,
                split_count=split_count,
                split_index=split_index,
            )
            return
        # Nothing in ``sun`` or ``time`` mode drives the intensity, so no measurement is written.
        write_fits(
            Path(path), image,
            timestamp=timestamp,
            exposure_sec=exposure,
            binning=self.cam.current_binning,
            readout_speed=self.cam.current_readout_speed,
            filter_num=self.cam.filter_number,
            ccd_temp=readings.get("ccd_temp"),
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
        )

    def _unique_frame_path(self, now, dark=False, exposure=None, preflight=False):
        """A free path for this frame, or None when the seconds are hopelessly full.

        Names resolve to one second and write_fits refuses to overwrite, and ``time``-mode
        darks run back-to-back with no second-sync, so sub-second exposures do collide. The
        name's second is nudged forward, so name and header may differ: believe DATE-OBS.

        In ``sun_cycle`` mode the name is the ASI archive's,
        ``YYYY/MM/DD/YYYYMMDD_hhmmss_SITE_NAME_WAVE_EEEEEEms[_DARK][_pf].fits``, where
        ``_pf`` marks a preflight frame.
        """
        for offset in range(self.NAME_ATTEMPTS):
            when = now + timedelta(seconds=offset)
            if self._archive_layout:
                path = archive_paths.frame_path(
                    self.output_dir, when,
                    site_id=self.cfg.location.name,
                    device_id=self.cfg.name,
                    wavelength=self.cfg.filter_info(
                        self.cam.filter_number).wavelength,
                    exposure_sec=exposure or 0.0,
                    dark=dark, preflight=preflight)
            else:
                path = japan_paths.frame_path(self.output_dir, when,
                                              self.cam.filter_number, dark=dark)
            if not path.exists():
                return path
        return None

    def _capture_one(self, now, exposure, image_type="LIGHT", obs_mode=None,
                     label=None, split_count=1, split_index=1):
        obs_mode = obs_mode or self.cfg.schedule.mode
        readings = self._refresh_camera_section()
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

        self._last_mean, self._last_saturation = japan_exposure.frame_stats(image)

        path = self._unique_frame_path(now, dark=(image_type == "DARK"),
                                       exposure=exposure,
                                       preflight=(obs_mode == "sun_cycle_auto"))
        if path is None:
            self._errors += 1
            self._dash_update(errors=self._errors)
            console_ui.error(f"{self.NAME_ATTEMPTS} frames already share the "
                             f"seconds after {now:%H:%M:%S} — dropping this one")
            self._bus.publish_error("error", "frame name collision",
                                    ts_iso=now.isoformat())
            return False
        # In the dated layout the date tree identifies the frame; flat, it does not.
        name = (str(path.relative_to(self.output_dir)) if self._archive_layout
                else path.name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_frame(path, image, now, exposure, image_type, obs_mode,
                              readings, sky_mean=self._last_mean,
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
            self._intensity.record_dark(exposure, self._last_mean)
        else:
            self._shots += 1
            self._session_shots += 1
        if self._service is not None:
            # ``path`` lets the frame server read this frame's FITS header back off the disk
            # for the observer's info panel: a live frame has no header to publish.
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
        self._save_status("running", force=True, readings=readings)
        return True

    def _capture_slot(self, entry, exposure, frames, obs_mode):
        """Returns ``(ok, mean)``. ``ok`` is False only when *every* sub-frame failed, so one
        bad read in a split neither costs the slot nor nears the failure limit.
        """
        # If ``prepare`` threw halfway, filing the frame under the intended exposure would
        # put a lie in the name and in EXPTIME.
        applied = self.cam.current_exposure
        if applied and abs(float(applied) - float(exposure)) > 1e-6:
            console_ui.warn(f"Camera holds {applied:g} s but {exposure:g} s was "
                            f"planned — filing the frame as {applied:g} s")
            exposure = applied

        def capture(index):
            label = f"LIGHT filter={_filter_text(self.cam.current_filter)}"
            if frames > 1:
                label += f"  {index}/{frames}"
            ok = self._capture_one(dt.now(), exposure, "LIGHT", obs_mode, label=label,
                                   split_count=frames, split_index=index)
            return ok, self._last_mean

        return shoot_split(frames, capture, self._stop_event.is_set)

    def _enter_main_stage(self, sched):
        self._intensity.enter_main(f"Sun at {sched.sun_max_angle:g}° — main cycle, "
                                   f"scheduled exposures.")

    def _split_guard_runs(self):
        """``sun_cycle`` only: unlike on the ASI imager, ``time`` mode stays one frame per slot."""
        return (self.cfg.overexposure.enabled
                and self.cfg.schedule.mode in japan_config.SPLIT_MODES)

    def _cooler_on(self):
        if self._cooler_commanded is True:
            return
        try:
            if self.cam.set_cooler(True):
                self._cooler_commanded = True
                target = self.cfg.cooling.target_temp
                console_ui.log("Cooler on" + (f", target {target:g} °C"
                                              if target is not None else ""))
            elif self.cam.cooling_controlled:
                # Left uncommanded, so the next call tries again.
                console_ui.warn("The camera refused to switch the cooler on")
        except Exception as exc:
            console_ui.warn(f"Could not switch the cooler on: {exc}")
        self._refresh_camera_section()

    def _cooler_off(self):
        if self._cooler_commanded is False or not self.cam.cooler_off_allowed:
            return
        try:
            if self.cam.set_cooler(False):
                self._cooler_commanded = False
                console_ui.log("Cooler off")
            elif self.cam.cooling_controlled:
                # Left uncommanded, so the next night's end tries again.
                console_ui.warn("The camera refused to switch the cooler off — "
                                "it is still cooling")
        except Exception as exc:
            console_ui.warn(f"Could not switch the cooler off: {exc}")
        self._refresh_camera_section()

    def _wait_for_pre_darks(self, dark_start, activation):
        """Wait for the pre-darks, switching the cooler on ``lead_minutes`` ahead.

        The darks start on time even on a warm sensor: they finish as the window opens, and
        a missed window costs more than darks a few degrees warm. False on a stop.
        """
        if dark_start > dt.now():
            console_ui.log(f"Pre-darks start at {dark_start.strftime('%H:%M:%S')}, "
                           f"measurements ~{activation.strftime('%H:%M:%S')}")
        if self.cam.cooling_controlled:
            cool_start = dark_start - timedelta(
                minutes=self.cfg.cooling.lead_minutes)
            if cool_start > dt.now():
                # Started in the day: the camera powers up with its cooler
                # running, and there is no reason to keep it so until evening.
                if self.cfg.cooling.off_between_nights:
                    self._cooler_off()
                console_ui.log(f"Cooler on at {cool_start.strftime('%H:%M:%S')}")
                if not self._wait_until(cool_start, phase="waiting for cooling"):
                    return False
            self._cooler_on()
        if dark_start > dt.now():
            phase = ("waiting for pre-darks (cooling)" if self.cam.cooling_controlled
                     else "waiting for pre-darks")
            if not self._wait_until(dark_start, phase=phase):
                return False
        self._check_cooled()
        return True

    def _check_cooled(self):
        if not self.cam.cooling_controlled:
            return
        try:
            temp = self.cam.temperature()
            target = self.cam.target_temperature
            status = self.cam.cooler_status()
        except Exception as exc:
            console_ui.warn(f"Could not read the cooler before the darks: {exc}")
            return
        if target is not None and temp is not None:
            cooled = abs(temp - target) <= self.cfg.cooling.tolerance
        else:
            cooled = status in (None, "ready", "always")
        if not cooled:
            reading = "n/a" if temp is None else f"{temp:.1f} °C"
            console_ui.warn(
                f"Sensor not cooled yet ({reading}"
                + (f", target {target:g} °C" if target is not None else "")
                + f", cooler {status or 'unknown'}) — pre-darks start anyway; "
                  f"consider a longer japan.cooling.lead_minutes")

    def _end_night(self):
        """Shutter shut, cooler off.

        The sun modes plan the next night straight away, and the shutter must not stand
        open all day waiting for the next pre-darks.
        """
        self._focus_opened_shutter = False
        try:
            self.cam.set_shutter(False)
            console_ui.log("Night over — shutter closed until the next pre-darks")
        except Exception as exc:
            console_ui.warn(f"Could not close the shutter after the night: {exc}")
        if self.cfg.cooling.off_between_nights:
            self._cooler_off()
        self._set_phase("waiting for the next night")
        self._refresh_camera_section()

    def _capture_darks(self, phase, sync_to_seconds=False, abort_on_stop=False):
        """Shoot ``dark_frames`` frames per unique exposure with the shutter shut.

        ``abort_on_stop`` is for the opening darks, whose measurements a stop has cancelled.
        The closing darks exist to be taken *after* a stop.
        """
        entries = self.cfg.schedule.entries
        combos = japan_schedule.unique_exposures(entries)

        def cancelled():
            return self._force_quit or (abort_on_stop and self._stop_event.is_set())

        if not combos or cancelled() or not self.cfg.schedule.dark_frames:
            return
        total = self.cfg.schedule.dark_frames * len(combos)
        # Neither loop drives a dark, so the dashboard must not go on claiming
        # an automatic exposure or a split while the closing darks run.
        self._intensity.idle()
        self._set_phase(f"dark frames ({phase})", detail=f"0/{total}")
        console_ui.log(f"Dark frames ({phase}): {self.cfg.schedule.dark_frames} × "
                       f"{len(combos)} exposure(s)")
        # From here until the run is over the shutter belongs to the darks:
        # ``_darks_running`` stops _shutter_for_focus from reopening it.
        self._darks_running = True
        self._focus_opened_shutter = False
        self._focus_note("shooting dark frames — the shutter must stay shut")
        try:
            self.cam.set_shutter(False)
            if self.cam.shutter_open is not False:
                self.cam.set_shutter(False)
        except Exception as exc:
            # Frames behind a shutter that could not be commanded would be filed as DARK.
            console_ui.error(f"Could not close the shutter — dark frames ({phase}) "
                             f"skipped: {exc}")
            self._darks_running = False
            self._focus_note("")
            return
        if self.cam.shutter_open is not False:
            # Some controllers never acknowledge; skipping would leave such a station
            # with no darks at all.
            console_ui.warn(f"Shutter close not acknowledged — dark frames ({phase}) "
                            f"are taken anyway; check them for light")

        done = 0
        try:
            for exposure, binning in combos:
                if cancelled():
                    break
                try:
                    self.cam.set_exposure(exposure)
                    if binning:
                        self.cam.set_binning(binning)
                except Exception as exc:
                    console_ui.error(f"Could not set up darks ({exposure} s): {exc}")
                    continue
                for _ in range(self.cfg.schedule.dark_frames):
                    if cancelled():
                        break
                    if sync_to_seconds:
                        seconds = sorted({s for entry in entries
                                          for s in (entry.seconds or [0])})
                        target = japan_schedule.next_second_slot(seconds)
                        # Closing darks must still be taken after a stop request, so this wait ignores
                        # the stop flag unless the caller asked for the opposite.
                        if not self._sleep_until(target, abort_on_stop=abort_on_stop):
                            break
                    done += 1
                    self._set_phase(f"dark frames ({phase})", detail=f"{done}/{total}")
                    self._capture_one(dt.now(), exposure, image_type="DARK",
                                      obs_mode="dark",
                                      label=f"DARK exp={exposure:g} s")
        finally:
            self._darks_running = False
            self._focus_note("")
        if cancelled() and phase == "initial":
            console_ui.log("Stop requested — the opening dark frames are cut short "
                           "and no measurements will follow")
        else:
            console_ui.log(f"Dark frames ({phase}) complete: {self._darks} total")

    def _sleep_until(self, target, abort_on_stop=False):
        """Wait inside a dark run, outside _service_tick on purpose.

        Status keeps going out, or the monitor sees a dead camera for minutes. Parameter
        requests are *not* served: a changed exposure would leave darks that match nothing.
        """
        last_status = 0.0
        while not (self._force_quit
                   or (abort_on_stop and self._stop_event.is_set())):
            remaining = (target - dt.now()).total_seconds()
            if remaining <= 0:
                return True
            if time.monotonic() - last_status >= 2.0:
                last_status = time.monotonic()
                self._save_status(SETUP_STATUS if self.setup_mode else "running",
                                  readings=self._refresh_camera_section())
            time.sleep(min(TICK, remaining))
        return False

    def _sun_angle_fn(self):
        from cameras.common.sun import angle_fn
        return angle_fn(self.cfg.location)

    def _run_setup_mode(self):
        """No programme to follow: _wait_until alone serves focus, parameters and status."""
        self._dash_update(schedule="setup mode — no scheduled captures")
        # Nothing else opens it here, the schedule modes do so after their pre-darks.
        # Closing it again is the caller's ``finally``.
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
        # A new session. Called once per night, so this also clears the counter once the
        # last night is over: from then on there is nothing for closing darks to bracket.
        self._session_shots = 0
        sched = self.cfg.schedule
        angle_fn = self._sun_angle_fn()

        # Pre-darks are scheduled to *finish* as the measurement window opens.
        activation = japan_schedule.sun_crossing_time(angle_fn, sched.sun_max_angle)
        dark_seconds = japan_schedule.estimate_dark_duration(sched.entries,
                                                            sched.dark_frames)
        dark_start = activation - timedelta(seconds=dark_seconds)
        if dark_start > dt.now():
            self._dash_update(schedule=f"sun ≤ {sched.sun_max_angle:g}°, "
                                       f"window ~{activation.strftime('%H:%M')}")
        if not self._wait_for_pre_darks(dark_start, activation):
            return
        self._capture_darks("initial", sync_to_seconds=True, abort_on_stop=True)
        if self._stop_event.is_set():
            return

        try:
            self.cam.set_shutter(True)
        except Exception as exc:
            console_ui.warn(f"Could not open the shutter: {exc}")

        in_session = False
        consecutive_errors = 0
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

            for entry in sched.entries:
                if self._stop_event.is_set():
                    break
                self._params_touched = False
                prepared = self._prepare_entry(entry)
                self._refresh_camera_section()
                target = japan_schedule.next_second_slot(entry.seconds or [0])
                # Waited even when the preparation broke: skipping the wait turns a dead
                # wheel into a busy loop.
                if not self._wait_until(target, phase="measuring"):
                    return
                if self._take_schedule_dirty():
                    break          # the window is re-read from the top
                if not prepared:
                    consecutive_errors += 1
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        self._give_up(f"{consecutive_errors} consecutive failures")
                        break
                    continue
                if self._params_touched:
                    self._prepare_entry(entry)
                if angle_fn(dt.now()) > sched.sun_max_angle:
                    console_ui.log("Window closed while waiting — skipping frame.")
                    continue
                if self._capture_one(dt.now(), entry.exposure, "LIGHT", "sun"):
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        self._give_up(f"{consecutive_errors} consecutive capture "
                                      f"failures")
                        break

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

        threading.Thread(target=_read, daemon=True, name="everycam-japan-start").start()
        last_status = 0.0
        readings = None
        while not self._started_event.is_set():
            # Nothing is scheduled until Enter, so ``slack=None``: no slot for a live frame
            # to be late for. TICK keeps a newly opened focus session from waiting on the sleep.
            last_status, readings = self._service_tick(
                None, status="waiting", last_status=last_status, readings=readings)
            self._started_event.wait(TICK)
        # An operator who focused before starting gets the shutter back the way
        # the programme expects it; the pre-darks are about to need it shut.
        self._focus_close_shutter()
        if self._dash is not None:
            self._dash.set_footer("Ctrl+C — finish the frame, shoot the closing darks, stop")
        return not self._stop_event.is_set()

    def _run_time_mode(self):
        # A new session. Called once per night in sun mode, so this also clears the counter
        # once the last night is over: from then on there is nothing for closing darks to bracket.
        self._session_shots = 0
        sched = self.cfg.schedule
        started = dt.now()
        # The occurrence of t_start belonging to this night, which past midnight is yesterday's.
        t_start = japan_schedule.cycle_anchor(sched.t_start, started)
        period = sched.period
        late = started >= t_start
        self._dash_update(schedule=f"cycle {period:.0f} s from "
                                   f"{t_start.strftime('%H:%M:%S')}")

        if late:
            console_ui.warn("Late start — the opening dark frames are skipped.")
            self._dash_update(note="Late start: initial darks skipped")
        else:
            self._capture_darks("initial", abort_on_stop=True)
        if self._stop_event.is_set():
            return

        try:
            self.cam.set_shutter(True)
        except Exception as exc:
            console_ui.warn(f"Could not open the shutter: {exc}")

        consecutive_errors = 0
        while not self._stop_event.is_set():
            now = dt.now()
            slot, entry, iteration = japan_schedule.next_cycle_slot(
                t_start, period, sched.entries, now)
            self._dash_update(
                detail=f"cycle {iteration}, Δ{entry.delta:g} s",
                schedule=f"cycle {period:.0f} s from {t_start.strftime('%H:%M:%S')}"
                         f"  ·  iteration {iteration}")

            # Prepared on this thread, in the gap before the slot: a filter move takes about
            # a second and there is normally far more room than that.
            self._params_touched = False
            self._prepare_entry(entry)
            self._refresh_camera_section()

            if not self._wait_until(slot, phase="measuring"):
                break
            if self._take_schedule_dirty():
                continue           # the cycle is re-anchored on the clock
            if self._params_touched:
                self._prepare_entry(entry)

            # If ``prepare`` threw halfway, filing the frame under the intended exposure would
            # put a lie in EXPTIME.
            exposure = entry.exposure
            applied = self.cam.current_exposure
            if applied and abs(float(applied) - float(exposure)) > 1e-6:
                console_ui.warn(f"Camera holds {applied:g} s but {exposure:g} s was "
                                f"planned — filing the frame as {applied:g} s")
                exposure = applied

            if self._capture_one(dt.now(), exposure, "LIGHT", "time"):
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    self._give_up(f"{consecutive_errors} consecutive capture failures")
                    break

    def _run_sun_cycle_mode(self):
        """The general cycle of ``time`` mode, started by the sun rather than the clock.

        Before the altitude reaches ``sun_max_angle`` only the pre-darks run, timed to finish
        as the window opens; the cycle then runs until sunrise. Its phase is locked to
        ``t_start`` in UTC, so stations far apart, whose windows open at different moments,
        shoot the same filter at the same instant.

        With ``preflight`` the session opens earlier: the same slots and phase run through
        the bright twilight, with the exposure chosen to hold a mean intensity. Sunrise still
        ends the run at ``sun_max_angle``, so the automatic stage is evening-only.
        """
        # A new session: see _run_sun_mode.
        self._session_shots = 0
        sched = self.cfg.schedule
        pre = self.cfg.preflight
        angle_fn = self._sun_angle_fn()
        period = sched.period

        # The angle that opens the session: sun_max_angle with the preflight stage off.
        open_angle = pre.sun_start_angle if pre.enabled else sched.sun_max_angle
        activation = japan_schedule.sun_crossing_time(angle_fn, open_angle)
        dark_seconds = japan_schedule.estimate_cycle_dark_duration(
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
            self._dash_update(schedule=f"sun ≤ {sched.sun_max_angle:g}°, cycle "
                                       f"{period:.0f} s, window "
                                       f"~{activation.strftime('%H:%M')}")
        if not self._wait_for_pre_darks(dark_start, activation):
            return
        self._capture_darks("initial", abort_on_stop=True)
        if self._stop_event.is_set():
            return

        try:
            self.cam.set_shutter(True)
        except Exception as exc:
            console_ui.warn(f"Could not open the shutter: {exc}")

        anchor = self._sun_cycle_anchor(activation, period)
        if anchor is None:
            return

        # A run started mid-night has already missed the twilight; it goes
        # straight to the main programme rather than automating a dark sky.
        self._intensity.begin_session(
            preflight=angle_fn(dt.now()) > sched.sun_max_angle)

        consecutive_errors = 0
        in_session = False
        while not self._stop_event.is_set():
            now = dt.now()
            angle = angle_fn(now)
            if self._stage == "preflight" and angle <= sched.sun_max_angle:
                self._enter_main_stage(sched)
            # The angle this stage runs down to closes the session: sunrise ends the main
            # programme at sun_max_angle and never returns the morning to the automatic stage.
            close_angle = (sched.sun_max_angle if self._stage == "main"
                           else pre.sun_start_angle)
            if angle > close_angle:
                if in_session:
                    console_ui.log("Sun above the threshold — measurements done."
                                   if self._stage == "main" else
                                   "Sun above the preflight angle — "
                                   "measurements done.")
                    break
                # The window was predicted but has not opened — a crossing
                # estimate a few seconds early. Wait rather than end the night.
                if not self._wait_seconds(30, phase="waiting for darkness"):
                    return
                continue
            if not in_session:
                in_session = True
                console_ui.log("Measurements started.")

            slot, entry, iteration = japan_schedule.next_cycle_slot(
                anchor, period, sched.entries, now)
            exposure, frames = self._intensity.plan(entry)
            self._dash_update(
                detail=f"cycle {iteration}, Δ{entry.delta:g} s"
                       f"{self._intensity.text(exposure, frames)}",
                schedule=f"sun {angle:+.1f}° (threshold {sched.sun_max_angle:g}°)"
                         f"  ·  cycle {period:.0f} s  ·  iteration {iteration}"
                         f"{'  ·  preflight' if self._stage == 'preflight' else ''}")

            self._params_touched = False
            self._prepare_entry(entry, exposure=exposure,
                                binning=self._intensity.binning())
            self._refresh_camera_section()

            phase = "measuring" if self._stage == "main" else "measuring (preflight)"
            if not self._wait_until(slot, phase=phase):
                break
            if self._take_schedule_dirty():
                continue           # the anchor still holds; the slot does not
            if self._params_touched:
                self._prepare_entry(entry, exposure=exposure,
                                    binning=self._intensity.binning())

            # The sun moved during the wait. In the main stage that can only close the window;
            # in preflight it can hand over, and this slot then shoots the scheduled exposure.
            angle = angle_fn(dt.now())
            if self._stage == "preflight" and angle <= sched.sun_max_angle:
                self._enter_main_stage(sched)
                exposure, frames = self._intensity.plan(entry)
                self._prepare_entry(entry, exposure=exposure)
            elif angle > (sched.sun_max_angle if self._stage == "main"
                          else pre.sun_start_angle):
                console_ui.log("Window closed while waiting — skipping frame.")
                continue

            obs_mode = self._intensity.obs_mode("sun_cycle", "sun_cycle_auto")
            ok, mean = self._capture_slot(entry, exposure, frames, obs_mode)
            self._intensity.feed(entry, exposure, mean, self._last_saturation)
            if ok:
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    self._give_up(f"{consecutive_errors} consecutive capture failures")
                    break

    def _sun_cycle_anchor(self, activation, period):
        """Fix the cycle's phase for tonight and wait for the window to open.

        With ``t_start`` (UTC here) the cycle reaches slot zero at the same instant at every
        station, which lets the ASI and Japan archives line up; without it the anchor is the
        window's first whole minute. The wait is here because next_cycle_slot, asked while
        the window is shut, answers with a slot before it opens. Returns the anchor, or None
        on a stop.
        """
        sched = self.cfg.schedule
        window = max(activation, dt.now())
        if sched.t_start is None:
            anchor = japan_schedule.next_minute_boundary(window)
            console_ui.log(f"Cycle anchored at {anchor.strftime('%H:%M:%S')}, "
                           f"period {period:.0f} s (no t_start: not phase-locked "
                           f"to other stations)")
            if not self._wait_until(anchor, phase="waiting for the cycle anchor"):
                return None
            return anchor
        anchor = japan_schedule.utc_cycle_anchor(sched.t_start, window)
        slot, entry, iteration = japan_schedule.next_cycle_slot(
            anchor, period, sched.entries, window)
        console_ui.log(f"Cycle phase locked to t_start {sched.t_start:%H:%M:%S} UTC "
                       f"({anchor:%H:%M:%S} local), period {period:.0f} s — first "
                       f"slot {slot:%H:%M:%S}, filter {entry.filter} "
                       f"(Δ{entry.delta:g} s, iteration {iteration})")
        if not self._wait_until(window, phase="waiting for the cycle anchor"):
            return None
        return anchor

    def run(self):
        self._bus.prepare_status_dir()
        if self._service is not None:
            self._service.set_current_params(self._current_params())
        readings = self._refresh_camera_section()
        self._save_status("starting", force=True, readings=readings)
        if self.setup_mode:
            console_ui.log("Japan worker started in setup mode — no schedule, "
                           "no darks, live frames on request")
        else:
            console_ui.log(f"Japan worker started — mode '{self.cfg.schedule.mode}', "
                           f"{len(self.cfg.schedule.entries)} schedule entr"
                           f"{'y' if len(self.cfg.schedule.entries) == 1 else 'ies'}")

        try:
            if self.setup_mode:
                self._cooler_on()
                self._run_setup_mode()
            elif self._wait_for_start():
                mode = self.cfg.schedule.mode
                if mode == "time":
                    self._cooler_on()
                    self._run_time_mode()
                else:
                    # One night per call: sunrise ends a session, not the run.
                    run_night = (self._run_sun_cycle_mode if mode == "sun_cycle"
                                 else self._run_sun_mode)
                    while not self._stop_event.is_set():
                        run_night()
                        if not self._stop_event.is_set():
                            self._end_night()
        except Exception as exc:
            self._errors += 1
            self.failed = f"Measurement loop failed: {exc}"
            console_ui.error(self.failed)
            self._save_status("error", force=True)
        finally:
            try:
                self.cam.set_shutter(False)
            except Exception:
                pass
            if self._closing_darks_due():
                self._set_phase("closing darks")
                try:
                    self._capture_darks("final")
                except Exception as exc:
                    console_ui.error(f"Closing dark frames failed: {exc}")
            # After the closing darks, which still want the sensor cold.
            if self.cfg.cooling.off_on_exit:
                self._cooler_off()
            self._set_phase("stopped")
            self._save_status("error" if self.failed else "stopped", force=True)
            self._bus.shutdown(failed=self.failed)
            console_ui.log(f"Japan worker stopped — {self._shots} light, "
                           f"{self._darks} dark, {self._errors} error(s)")


def run_preview_japan(cam: JapanCamera, instance_name, dashboard=None):
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


def run_probe_japan(config_path=None):
    """Report what DCAM sees and whether the filter wheel answers.

    The camera half writes nothing. **The wheel half moves the wheel**: it homes it and
    drives it to three positions, so do not run this during a night's measurements.
    """
    from utils import load_config

    conf = japan_config.from_dict(load_config(config_path).get("japan", {}))
    for problem in conf.errors:
        print(f"config: {problem}")

    _probe_camera(conf)
    print()
    _probe_wheel(conf)


# Only the properties the driver sets or reads.
_PROBE_PROPERTIES = ("EXPOSURETIME", "BINNING", "READOUTSPEED", "SENSORTEMPERATURE",
                     "SENSORCOOLER", "SENSORTEMPERATURETARGET", "SENSORCOOLERSTATUS")


def _probe_camera(conf):
    if conf.camera.backend != "dcam":
        print(f"Camera backend is {conf.camera.backend!r}, so there is no camera "
              f"to ask.")
        print(f"The simulator accepts binning "
              f"{japan_config.BINNING_MIN}..{japan_config.BINNING_MAX} and "
              f"readout speed 1 (slow) or 2 (fast).")
        return

    from cameras.japan import dcamsdk

    sdk = dcamsdk.load()
    print("Opening the first camera DCAM reports…")
    if not sdk.Dcamapi.init():
        print(f"Dcamapi.init() failed: {sdk.Dcamapi.lasterr()}")
        return
    dcam = sdk.Dcam()
    try:
        if not dcam.dev_open():
            print(f"dev_open() failed: {dcam.lasterr()}")
            return
        for label, key in (("Vendor", "VENDOR"), ("Model", "MODEL"),
                           ("Bus", "BUS"), ("DCAM API", "DCAMAPIVERSION"),
                           ("Serial", "CAMERAID"),
                           ("Camera firmware", "CAMERAVERSION"),
                           ("Driver", "DRIVERVERSION"),
                           ("Module", "MODULEVERSION")):
            print(f"{label + ':':<18}"
                  f"{dcam.dev_getstring(getattr(sdk.DCAM_IDSTR, key))}")
        print()
        print(f"{'Property':<22}{'Current':>14}   Accepted")
        print("-" * 80)
        for name in _PROBE_PROPERTIES:
            prop = getattr(sdk.DCAM_IDPROP, name)
            print(f"{name:<22}{_probe_current(dcam, prop):>14}"
                  f"   {_probe_allowed(dcam, prop)}")
        print()
        print("japan.camera.binning and japan.camera.readout_speed in config.json "
              "have to be values the matching rows allow.")
        print("japan.cooling needs SENSORCOOLER to be writable (1 = off, 2 = on); "
              "target_temp needs SENSORTEMPERATURETARGET.")
        print()
        _probe_cooling_properties(dcam)
    finally:
        dcam.dev_close()
        sdk.Dcamapi.uninit()


# The camera is asked for every property it has, not a fixed list: a model may
# switch its cooling through something the table above does not name.
_PROBE_COOLING_WORDS = ("COOL", "TEMP", "FAN", "PELTIER")


def _probe_cooling_properties(dcam):
    print("Every cooling-related property this camera reports "
          "(value = the camera's own name for it):")
    print("-" * 80)
    prop, found = 0, 0
    while True:
        prop = dcam.prop_getnextid(prop)
        if prop is False or prop == 0:
            break
        name = dcam.prop_getname(prop)
        if name is False or not any(w in name.upper()
                                    for w in _PROBE_COOLING_WORDS):
            continue
        found += 1
        attr = dcam.prop_getattr(prop)
        access = ("?" if attr is False else
                  ("R/W" if attr.is_writable() else "R/O"))
        print(f"{name:<32} 0x{prop:08X}  {access:<4}"
              f"current {_probe_current(dcam, prop)}")
        if attr is not False and 0 < attr.valuemax - attr.valuemin <= 16:
            values = []
            for value in range(int(attr.valuemin), int(attr.valuemax) + 1):
                text = dcam.prop_getvaluetext(prop, float(value))
                if text is not False:
                    values.append(f"{value} = {text}")
            if values:
                print(f"{'':<32} values: {', '.join(values)}")
    if not found:
        print("(none)")


def _probe_current(dcam, prop):
    value = dcam.prop_getvalue(prop)
    if value is False:
        return "n/a"
    text = dcam.prop_getvaluetext(prop, value)
    return f"{value:g}" if text is False else f"{value:g} ({text})"


def _probe_allowed(dcam, prop):
    attr = dcam.prop_getattr(prop)
    if attr is False:
        return "n/a"
    low, high, step = attr.valuemin, attr.valuemax, attr.valuestep
    if low == high:
        return f"{low:g} (fixed)"
    if step:
        return f"{low:g}..{high:g} step {step:g}"
    return f"{low:g}..{high:g}"


def _probe_wheel(conf):
    """Trace the controller's raw answers while the wheel actually moves.

    Shows the real move time ``FilterWheel.select()`` relies on, and confirms the
    ``GOSUB4`` → ``FILT:<n>`` protocol on a controller that has been rewired.
    """
    port = conf.filter_wheel.port
    if str(port).strip().lower() == "sim":
        print("Filter wheel port is 'sim', so there is no controller to ask.")
        return

    import serial

    print(f"Filter wheel on {port} — homing, then moving. THIS MOVES THE WHEEL.")
    ser = serial.Serial(port, baudrate=conf.filter_wheel.baudrate,
                        bytesize=8, parity="N", stopbits=1, timeout=0.3)
    try:
        ser.write(("GOSUB5" + chr(13)).encode())
        time.sleep(10)
        print(f"init response: {ser.readline()!r}")

        for target in (1, 5, 3):
            ser.reset_input_buffer()
            t0 = time.monotonic()
            ser.write((f"g={target}" + chr(13)).encode())
            print(f"\n--- goto {target} (raw GOSUB4 answers vs. time) ---")
            for _ in range(80):          # ~8 s of polling at 0.1 s
                ser.write(("GOSUB4" + chr(13)).encode())
                resp = ser.readline()
                print(f"{time.monotonic() - t0:6.2f}s  {resp!r}")
                if resp.strip() == f"FILT:{target}".encode():
                    print(f"       arrived after {time.monotonic() - t0:.2f} s")
                    break
                time.sleep(0.1)
    finally:
        ser.close()


def run_console_japan(config_path=None, preview=False, verbose=False,
                      setup_mode=False):
    from utils import load_config, configure_console_japan
    from camera_service import CameraService
    from frame_server import start_frame_server

    cfg = load_config(config_path)
    japan_cfg = cfg.get("japan", {})

    node_name = get_node_name(cfg)
    status_dir = cfg.get("status_dir") or str(Path.home() / ".every_camera" / "status")

    # The wizard needs a plain terminal, so it runs before the dashboard starts.
    if not preview and not japan_cfg.get("output_dir"):
        configure_console_japan(cfg, config_path)
        cfg = load_config(config_path)
        japan_cfg = cfg.get("japan", {})
        node_name = get_node_name(cfg)

    claim = claim_instance_name(japan_cfg.get("instance_name")
                                or get_instance_name("JAPAN", cfg))
    instance_name = claim.name

    conf = japan_config.from_dict(japan_cfg)
    dash = console_ui.start_dashboard(
        "japan", instance_name, verbose=verbose,
        footer="Ctrl+C — finish the frame, shoot the closing darks, stop")
    # Before the camera is opened, so a camera that will not open still raises an alert.
    # The crash handler goes first: it alone can speak for a process killed outright.
    alert_status_path = os.path.join(status_dir, f"{os.getpid()}.json")
    alerts.install_crash_handler(alert_status_path)
    alerts.start(cfg.get("alerts", {}), node_name, instance_name, "japan",
                 log_path=console_ui.default_log_path("japan", instance_name),
                 status_path=alert_status_path)

    dash.update(status="starting", node_name=node_name,
                output_dir=conf.output_dir, frames=0, darks=0, errors=0)

    cam = None
    server = None
    worker = None
    try:
        for problem in conf.errors:
            console_ui.warn(problem)
        console_ui.log(f"Instance {instance_name} on {node_name} — backend "
                       f"{conf.camera.backend}, wheel {conf.filter_wheel.port}")

        if not conf.output_dir:
            console_ui.error("japan.output_dir is not set — nothing would be saved.")
            return
        os.makedirs(conf.output_dir, exist_ok=True)
        os.makedirs(status_dir, exist_ok=True)

        if not preview and (setup_mode or not conf.schedule.entries):
            setup_mode = True
            announce_setup_mode(
                "requested with --setup" if conf.schedule.entries else
                empty_schedule_reason(
                    "japan",
                    japan_cfg.get("schedule") or japan_cfg.get("schedule_file"),
                    conf.errors))

        console_ui.log("Opening the camera…")
        cam = JapanCamera(conf).open()
        dash.set_section("device", cam.info_rows())

        if preview:
            run_preview_japan(cam, instance_name, dashboard=dash)
            return


        # No setpoint limits to publish: every parameter in this schema has a range known
        # without asking the hardware.
        service = CameraService("japan", instance_name, conf.output_dir,
                                node_name=node_name, setup_mode=setup_mode)
        service.set_schedule(japan_schedule.schedule_snapshot(conf.schedule))
        server = start_frame_server(cfg.get("server", {}), service)
        if server:
            dash.update(server_url=server.url, focus="idle")
            if setup_mode:
                console_ui.log(f"Focus this camera with: python focus_app.py "
                               f"--host {get_local_ip()} --port {server.port}")

        worker = JapanWorkerConsole(
            cam=cam,
            cfg=conf,
            output_dir=conf.output_dir,
            instance_name=instance_name,
            status_dir=status_dir,
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
            # The second signal must stay available under a service manager too: systemd sends
            # SIGTERM again when TimeoutStopSec runs out, and giving up the darks beats being
            # SIGKILLed mid-exposure.
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
        if cam:
            cam.close()
        dash.stop()
        claim.release()
    if worker is not None and worker.failed:
        # Non-zero, so systemd's Restart=on-failure gives the night another try.
        sys.exit(1)
