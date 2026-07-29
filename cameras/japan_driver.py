"""
Japan all-sky imager driver: Hamamatsu camera (DCAM-API) + filter wheel.

This is the every-camera front for the hardware in ``cameras/japan/``. It keeps the
measurement programme of the standalone japan-camera application intact — pre-darks
timed to the window, the two schedule modes, post-darks on Ctrl+C — and adds what
every other camera in this program already has: retained MQTT status, status files
for the monitor, the LAN frame server with UDP discovery, live view and remote
parameter editing for ``focus_app.py``, preview mode, and the shared console
dashboard.

japan-camera is the ancestor of the ``asi`` driver, so the two are deliberately
close: the sun-mode and time-mode loops here are the same loops, and the shared
schedule arithmetic, filter wheel and FITS core live in ``cameras/common/``. What
this driver does *not* have is everything the PIXIS grew afterwards — no
``sun_cycle`` mode, no automatic exposure, no overexposure splitting, and no
cooling control, the Hamamatsu offering a sensor temperature to read and no
setpoint to command.

Two rules govern the threading, both inherited from the original program because
both are load-bearing:

* **Every DCAM and serial call happens on the worker thread.** HTTP handlers reach
  the camera only through :class:`camera_service.CameraService`; the console
  renderer only reads a state dict. The original avoided two threads issuing DCAM
  calls at once by joining its prep thread before each capture, and this driver
  avoids it by never starting one.
* **Nothing waits without servicing the rest.** The gaps between exposures are
  where parameter changes are applied, focus frames are served and status is
  published — see :meth:`JapanWorkerConsole._wait_until`. A focus frame is only
  taken when it comfortably fits before the next scheduled slot, so live view can
  never delay a measurement.

Schedule modes (``japan.mode`` in config.json):

    sun    Entries fire on given seconds of every minute while the solar altitude
           stays at or below ``sun_max_angle``. Pre-darks are timed to finish just
           as that window opens, and the closing darks are synced the same way.
    time   One general cycle repeats, phase-locked to ``t_start``; each entry has
           its offset (``delta``), filter, exposure and binning. Started on time,
           the opening darks are shot first; started late, they are skipped and
           only the closing ones are taken.

Frames are filed flat into ``<output_dir>`` under the original's own name — see
``cameras/japan/paths.py``, which also explains why the stamp is UTC.
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
    run_focus_iteration, announce_setup_mode, SETUP_STATUS,
    install_stop_handler, stop_signal_name,
)

from cameras.japan import config as japan_config
from cameras.japan import devices, paths as japan_paths
from cameras.japan.fits import write_fits
# Imported as a module rather than by name so a test can patch the timing helpers
# through ``japan_driver.japan_schedule``.
from cameras.common import schedule as japan_schedule
# Safe to import here: the wheel module pulls in pyserial only when it opens a
# port, so a machine with no hardware still imports this driver.
from cameras.common.filterwheel import HOME as FILTER_HOME

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
class JapanCamera:
    """Camera + filter wheel as one object, with a single open/close lifecycle.

    The two devices are always used together (an exposure is meaningless without
    a known filter and shutter state), and both are context managers, so this
    holds their contexts and exposes the handful of operations the worker needs.
    """

    def __init__(self, cfg: japan_config.JapanConfig):
        self.cfg = cfg
        self.cam = None
        self.wheel = None
        self._info = {}
        self._opened = False

    # -- lifecycle -----------------------------------------------------------
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
    def current_readout_speed(self):
        return self.cam.current_readout_speed

    @property
    def current_filter(self):
        """Wheel position: 0 at home, 1..6 in a filter, None if unknown."""
        return self.wheel.current_filter

    @property
    def filter_number(self):
        """The position as a number for file names and FITS headers.

        An unconfirmed move leaves the position unknown, and a frame still has to
        be saved; the archive records that as 0, which is what the original wrote
        for a wheel it had not moved yet. Nothing outside this property may read
        ``wheel.current_filter`` for a name or a header — ``None`` would file the
        frame as ``..._None.fits`` and leave ``FILTER`` blank.
        """
        position = self.wheel.current_filter
        return FILTER_HOME if position is None else position

    @property
    def shutter_open(self):
        """True/False once the shutter has been commanded, None while unknown."""
        return getattr(self.wheel, "shutter_open", None)

    def temperature(self):
        return self.cam.temperature()

    def info_rows(self):
        return self.cam.info_rows()

    # -- control -------------------------------------------------------------
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

    def capture(self):
        return self.cam.capture()

    def prepare(self, entry):
        """Apply one schedule entry's exposure, binning and filter.

        Unlike the ASI driver there is no exposure override: nothing here chooses
        an exposure, so the entry is the only authority on what the camera should
        hold.
        """
        if entry.exposure is not None and self.current_exposure != entry.exposure:
            self.set_exposure(entry.exposure)
        if entry.binning and self.current_binning != entry.binning:
            self.set_binning(entry.binning)
        if entry.filter and self.current_filter != entry.filter:
            self.select_filter(entry.filter)

    def temperature_fields(self):
        """The sensor reading for the console, taken on the worker thread only."""
        return {"ccd_temp": self.temperature()}


# ---------------------------------------------------------------------------
# Console worker
# ---------------------------------------------------------------------------
class JapanWorkerConsole(threading.Thread):
    """Runs the Japan measurement programme and publishes it to the rest of the app."""

    MAX_CONSECUTIVE_ERRORS = 5

    # How many seconds forward the frame name may be nudged to find a free one.
    # Enough for a full run of back-to-back darks (``dark_frames`` up to 9), which
    # is the case that actually collides — see :meth:`_unique_frame_path`.
    NAME_ATTEMPTS = 10

    def __init__(self, cam: JapanCamera, cfg: japan_config.JapanConfig, output_dir,
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
        self._bus = WorkerMqtt("japan", instance_name, status_dir, mqtt_publisher,
                               mqtt_prefix, service=service, node_name=node_name)

        self._stop_event = threading.Event()
        self._force_quit = False
        self._shots = 0
        self._darks = 0
        self._errors = 0
        self._last_shot = None
        self._last_file = None
        self._last_frame = None
        self._phase = "starting"
        self._next_slot = None
        self._pending_capture = None
        self._pending_capture_lock = threading.Lock()
        self._pending_capture_event = threading.Event()
        self._started_event = threading.Event()

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

    def _camera_rows(self, readings=None):
        readings = readings if readings is not None else {}
        temp = readings.get("ccd_temp")
        temp_text = f"{temp:.2f} °C" if isinstance(temp, (int, float)) else "n/a"
        binning = self.cam.current_binning or 1
        exposure = self.cam.current_exposure
        return [
            ("Exposure / binning:",
             f"{exposure:g} s  ·  {binning}x{binning}" if exposure
             else f"-  ·  {binning}x{binning}"),
            ("Readout speed:",
             japan_config.readout_text(self.cam.current_readout_speed)),
            ("Filter:", _filter_text(self.cam.current_filter)),
            ("Shutter:", _shutter_text(self.cam.shutter_open)),
            ("Sensor temperature:", temp_text),
        ]

    def _refresh_camera_section(self, readings=None):
        if self._dash is None:
            return
        if readings is None:
            readings = self.cam.temperature_fields()
        self._dash.set_section("camera", self._camera_rows(readings))
        return readings

    def _save_status(self, status, force=False, readings=None):
        readings = readings or {}
        # The schedule moves the filter, the exposure and the shutter on its
        # own; focus_app only ever sees what is published from here.
        publish_current_params(self._service, self._current_params)
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
            "last_update": dt.now().isoformat(),
        }
        # No ``set_temp``/``temp_locked``: this camera has no setpoint, so the
        # monitor shows a reading rather than a reading against a target.
        if readings.get("ccd_temp") is not None:
            payload["ccd_temp"] = round(float(readings["ccd_temp"]), 2)
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
            "readout_speed": self.cam.current_readout_speed,
            "filter": self.cam.current_filter,
            "shutter": self.cam.shutter_open,
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
                    self.cam.set_shutter(is_open)
                    applied[name] = is_open
                    # Only the darks are supposed to shoot shut. Closing it from
                    # focus_app during a run would quietly turn every scheduled
                    # frame into a dark, so say so where the operator will see it.
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

    def _serve_focus(self, slack):
        """Grab one live frame for focus_app, if it fits in ``slack`` seconds."""
        if self._service is None or not self._service.focus_active():
            return
        exposure = self.cam.current_exposure or 0.0
        needed = exposure * FOCUS_SLACK_FACTOR + FOCUS_SLACK_SECONDS
        # In setup mode there is no scheduled capture to be late for, so a long
        # exposure must not be refused just because the idle tick is shorter.
        if not self.setup_mode and slack is not None and slack < needed:
            return          # a live frame must never push a scheduled one late
        self._dash_update(focus=f"live frame ({exposure:g} s)")
        run_focus_iteration(
            self._service, self.cam.capture,
            on_error=lambda exc, stopped: console_ui.warn(
                f"Focus frame failed: {exc}" + (" — focus disabled" if stopped else "")),
        )
        self._dash_update(focus="active")

    # ------------------------------------------------------------------
    # Waiting
    # ------------------------------------------------------------------
    def _wait_until(self, target, phase=None, allow_focus=True):
        """Idle until ``target``, serving focus, parameters and status meanwhile.

        Returns True if the moment was reached, False if a stop was requested.
        This is the only place the driver sleeps: everything that has to keep
        happening between exposures happens here.
        """
        if phase:
            self._set_phase(phase)
        # In setup mode this is an idle tick, not a scheduled capture;
        # publishing it as "next slot" would tell the monitor a measurement was due.
        self._next_slot = (target if isinstance(target, dt) and not self.setup_mode
                           else None)
        self._dash_update(next_at=self._next_slot)
        last_status = 0.0
        readings = None
        while not self._stop_event.is_set():
            now = dt.now()
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                break
            if time.monotonic() - last_status >= 2.0:
                last_status = time.monotonic()
                readings = self._refresh_camera_section()
                self._save_status(SETUP_STATUS if self.setup_mode else "running",
                                  readings=readings)
            self._serve_params()
            if self._pending_capture_event.is_set():
                self._handle_pending_capture()
                remaining = (target - dt.now()).total_seconds()
                if remaining <= 0:
                    break
            if allow_focus:
                self._serve_focus(remaining)
                remaining = (target - dt.now()).total_seconds()
            if remaining <= 0:
                break
            self._stop_event.wait(min(TICK, max(remaining, 0.001)))
        self._next_slot = None
        self._dash_update(next_at=None)
        return not self._stop_event.is_set()

    def _wait_seconds(self, seconds, phase=None, allow_focus=True):
        return self._wait_until(dt.now() + timedelta(seconds=seconds), phase,
                                allow_focus)

    def _set_phase(self, phase, detail=None):
        self._phase = phase
        self._dash_update(phase=phase)
        if detail is not None:
            self._dash_update(detail=detail)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    def _write_frame(self, path, image, timestamp, exposure, image_type, obs_mode,
                     readings=None):
        readings = readings or {}
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

    def _unique_frame_path(self, now, dark=False):
        """A free path for this frame, or None when the second is hopelessly full.

        The name resolves to one second and ``write_fits`` refuses to overwrite, so
        two frames taken inside the same second at the same filter collide. That is
        not a corner case here: the ``time``-mode dark run is back-to-back with no
        second-sync, so any exposure under a second used to raise partway through
        and abandon the rest of the darks.

        The name's second is nudged forward until it is free. ``DATE-OBS`` keeps the
        true start of the exposure, so a name and a header can then disagree by a
        second or two; the header is the one to believe.
        """
        for offset in range(self.NAME_ATTEMPTS):
            path = japan_paths.frame_path(self.output_dir,
                                          now + timedelta(seconds=offset),
                                          self.cam.filter_number, dark=dark)
            if not path.exists():
                return path
        return None

    def _capture_one(self, now, exposure, image_type="LIGHT", obs_mode=None,
                     label=None):
        """Take, save and publish one frame. Returns True on success."""
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

        path = self._unique_frame_path(now, dark=(image_type == "DARK"))
        if path is None:
            self._errors += 1
            self._dash_update(errors=self._errors)
            console_ui.error(f"{self.NAME_ATTEMPTS} frames already share the "
                             f"seconds after {now:%H:%M:%S} — dropping this one")
            self._bus.publish_error("error", "frame name collision",
                                    ts_iso=now.isoformat())
            return False
        name = path.name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_frame(path, image, now, exposure, image_type, obs_mode,
                              readings)
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
        else:
            self._shots += 1
        if self._service is not None:
            self._service.publish_frame(image, now, {"image_type": image_type,
                                                     "filter": self.cam.current_filter,
                                                     "exposure": exposure})
        self._dash_update(frames=self._shots, darks=self._darks,
                          last_file=name, errors=self._errors)
        console_ui.log(f"Saved {name}")
        if self._bus.enabled:
            try:
                self._bus.publish_frame_array(image, now.isoformat(),
                                              params=self._current_params())
            except Exception as exc:
                console_ui.warn(f"MQTT frame publish: {exc}")
        self._save_status("running", force=True, readings=readings)
        return True

    # ------------------------------------------------------------------
    # Dark frames
    # ------------------------------------------------------------------
    def _capture_darks(self, phase, sync_to_seconds=False):
        """Shoot ``dark_frames`` frames per unique exposure with the shutter shut."""
        entries = self.cfg.schedule.entries
        combos = japan_schedule.unique_exposures(entries)
        if not combos or self._force_quit or not self.cfg.schedule.dark_frames:
            return
        total = self.cfg.schedule.dark_frames * len(combos)
        self._set_phase(f"dark frames ({phase})", detail=f"0/{total}")
        console_ui.log(f"Dark frames ({phase}): {self.cfg.schedule.dark_frames} × "
                       f"{len(combos)} exposure(s)")
        try:
            self.cam.set_shutter(False)
        except Exception as exc:
            console_ui.warn(f"Could not close the shutter: {exc}")

        done = 0
        for exposure, binning in combos:
            if self._force_quit:
                break
            try:
                self.cam.set_exposure(exposure)
                if binning:
                    self.cam.set_binning(binning)
            except Exception as exc:
                console_ui.error(f"Could not set up darks ({exposure} s): {exc}")
                continue
            for _ in range(self.cfg.schedule.dark_frames):
                if self._force_quit:
                    break
                if sync_to_seconds:
                    seconds = sorted({s for entry in entries
                                      for s in (entry.seconds or [0])})
                    target = japan_schedule.next_second_slot(seconds)
                    # Darks must still be taken after a stop request, so this wait
                    # ignores the stop flag and only honours a forced quit.
                    self._sleep_until(target)
                done += 1
                self._set_phase(f"dark frames ({phase})", detail=f"{done}/{total}")
                self._capture_one(dt.now(), exposure, image_type="DARK",
                                  obs_mode="dark",
                                  label=f"DARK exp={exposure:g} s")
        console_ui.log(f"Dark frames ({phase}) complete: {self._darks} total")

    def _sleep_until(self, target):
        """Plain wait that only a forced quit interrupts (used inside dark runs)."""
        while not self._force_quit:
            remaining = (target - dt.now()).total_seconds()
            if remaining <= 0:
                return True
            time.sleep(min(TICK, remaining))
        return False

    # ------------------------------------------------------------------
    # Sun mode
    # ------------------------------------------------------------------
    def _sun_angle_fn(self):
        from cameras.common.sun import angle_fn
        return angle_fn(self.cfg.location)

    def _run_setup_mode(self):
        """Idle indefinitely, serving focus_app — no schedule, nothing archived.

        There is no observing programme to follow, so the worker simply keeps
        :meth:`_wait_until` running: that is already where focus frames, parameter
        changes and status publication happen, and it is the whole reason a camera
        without a schedule can be focused at all.
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
        activation = japan_schedule.sun_crossing_time(angle_fn, sched.sun_max_angle)
        dark_seconds = japan_schedule.estimate_dark_duration(sched.entries,
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
                target = japan_schedule.next_second_slot(entry.seconds or [0])
                if not self._wait_until(target, phase="measuring"):
                    return
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

        threading.Thread(target=_read, daemon=True, name="everycam-japan-start").start()
        while not self._started_event.is_set():
            readings = self._refresh_camera_section()
            self._save_status("waiting", readings=readings)
            self._serve_params()
            self._started_event.wait(1.0)
        if self._dash is not None:
            self._dash.set_footer("Ctrl+C — finish the frame, shoot the closing darks, stop")
        return not self._stop_event.is_set()

    def _run_time_mode(self):
        sched = self.cfg.schedule
        started = dt.now()
        t_start = dt.combine(started.date(), sched.t_start)
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

        consecutive_errors = 0
        while not self._stop_event.is_set():
            now = dt.now()
            slot, entry, iteration = japan_schedule.next_cycle_slot(
                t_start, period, sched.entries, now)
            self._dash_update(
                detail=f"cycle {iteration}, Δ{entry.delta:g} s",
                schedule=f"cycle {period:.0f} s from {t_start.strftime('%H:%M:%S')}"
                         f"  ·  iteration {iteration}")

            # Prepared on this thread, in the gap before the slot: a filter move
            # takes about a second and there is normally far more room than that.
            # The original used a background thread and joined it before the
            # capture; doing it here is the same guarantee without the thread.
            try:
                self.cam.prepare(entry)
            except Exception as exc:
                console_ui.error(f"Could not prepare the cycle: {exc}")
                self._errors += 1
            self._refresh_camera_section()

            if not self._wait_until(slot, phase="measuring"):
                break

            # The camera is the authority on what was actually applied: if
            # ``prepare`` threw halfway through, filing the frame under the
            # exposure we merely intended would put a lie in EXPTIME.
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
                    console_ui.error(f"{consecutive_errors} consecutive capture "
                                     f"failures — stopping.")
                    break

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
                self._run_setup_mode()
            elif self._wait_for_start():
                if self.cfg.schedule.mode == "time":
                    self._run_time_mode()
                else:
                    self._run_sun_mode()
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
            if not self._force_quit and not self.setup_mode:
                self._set_phase("closing darks")
                try:
                    self._capture_darks("final")
                except Exception as exc:
                    console_ui.error(f"Closing dark frames failed: {exc}")
            self._set_phase("stopped")
            self._save_status("stopped", force=True)
            self._bus.shutdown()
            console_ui.log(f"Japan worker stopped — {self._shots} light, "
                           f"{self._darks} dark, {self._errors} error(s)")


# ---------------------------------------------------------------------------
# Preview mode
# ---------------------------------------------------------------------------
def run_preview_japan(cam: JapanCamera, instance_name, dashboard=None):
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


# ---------------------------------------------------------------------------
# --probe
# ---------------------------------------------------------------------------
def run_probe_japan(config_path=None):
    """Report what this hardware is and what it will accept.

    Two questions in one command, because an operator setting a station up asks
    them together: does DCAM see the camera and which readout speeds and binnings
    will it take, and does the filter wheel answer on its serial port.

    The camera half writes nothing. **The wheel half moves the wheel** — it homes
    it and drives it to three positions — so it is not something to run in the
    middle of a night's measurements.
    """
    from utils import load_config

    conf = japan_config.from_dict(load_config(config_path).get("japan", {}))
    for problem in conf.errors:
        print(f"config: {problem}")

    _probe_camera(conf)
    print()
    _probe_wheel(conf)


# The properties the driver actually sets or reads. Anything else DCAM exposes is
# not this driver's business and would only make the table harder to read.
_PROBE_PROPERTIES = ("EXPOSURETIME", "BINNING", "READOUTSPEED", "SENSORTEMPERATURE")


def _probe_camera(conf):
    """Print the camera's identity and the range of each property we set."""
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
    finally:
        dcam.dev_close()
        sdk.Dcamapi.uninit()


def _probe_current(dcam, prop):
    """The value the camera currently holds for one property, as text."""
    value = dcam.prop_getvalue(prop)
    if value is False:
        return "n/a"
    text = dcam.prop_getvaluetext(prop, value)
    return f"{value:g}" if text is False else f"{value:g} ({text})"


def _probe_allowed(dcam, prop):
    """What the camera would accept for one property, as text."""
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

    Ported from ``japan-camera/wheel_probe.py``. It is the way to see the real move
    time that ``FilterWheel.select()`` relies on, and to confirm the ``GOSUB4`` →
    ``FILT:<n>`` protocol on a controller that has been rewired.
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


# ---------------------------------------------------------------------------
# Console entry point
# ---------------------------------------------------------------------------
def run_console_japan(config_path=None, preview=False, verbose=False,
                      setup_mode=False):
    """Run the Japan camera in console mode."""
    from utils import load_config, configure_console_japan
    from mqtt_client import create_console_publisher
    from camera_service import CameraService
    from frame_server import start_frame_server

    cfg = load_config(config_path)
    japan_cfg = cfg.get("japan", {})
    mqtt_cfg = cfg.get("mqtt", {})

    node_name = get_node_name(cfg)
    status_dir = cfg.get("status_dir") or str(Path.home() / ".every_camera" / "status")

    # The wizard needs a plain terminal, so it runs before the dashboard starts.
    if not preview and not japan_cfg.get("output_dir"):
        configure_console_japan(cfg, config_path)
        cfg = load_config(config_path)
        japan_cfg = cfg.get("japan", {})
        mqtt_cfg = cfg.get("mqtt", {})
        node_name = get_node_name(cfg)

    claim = claim_instance_name(japan_cfg.get("instance_name")
                                or get_instance_name("JAPAN", cfg))
    instance_name = claim.name

    conf = japan_config.from_dict(japan_cfg)
    dash = console_ui.start_dashboard(
        "japan", instance_name, verbose=verbose,
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
            console_ui.error("japan.output_dir is not set — nothing would be saved.")
            return
        os.makedirs(conf.output_dir, exist_ok=True)
        os.makedirs(status_dir, exist_ok=True)

        if not preview and (setup_mode or not conf.schedule.entries):
            setup_mode = True
            announce_setup_mode(
                "requested with --setup" if conf.schedule.entries else
                "the Japan schedule is empty — add slots to japan.schedule in "
                "config.json or point japan.schedule_file at a file")

        console_ui.log("Opening the camera…")
        cam = JapanCamera(conf).open()
        dash.set_section("device", cam.info_rows())

        if preview:
            run_preview_japan(cam, instance_name, dashboard=dash)
            return

        mqtt_pub = create_console_publisher(mqtt_cfg, instance_name, "japan")
        if mqtt_pub:
            dash.update(mqtt=f"{mqtt_cfg.get('host', '?')} — connected")

        # No ``publish_setpoint_limits`` counterpart: every parameter in this
        # camera's schema has a range known without asking the hardware, the
        # sensor temperature being a reading rather than a setting.
        service = CameraService("japan", instance_name, conf.output_dir,
                                node_name=node_name, setup_mode=setup_mode)
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
