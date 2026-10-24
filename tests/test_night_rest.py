"""What the camera does between nights, and the japan cooler around them.

The sun modes end a night by returning to ``run()``, which plans the next one at
once. The shutter used to be shut only when the whole run ended, so after a
night it stood open all day waiting for the next pre-darks — seen on a japan
station, and the ASI driver had the same loop. Both now shut it as a night ends.

The japan camera's cooler, when ``japan.cooling`` asks for it to be driven, is
off through the day, on ``lead_minutes`` before the pre-darks, and off again once
the night is over.
"""
import sys

from datetime import datetime as dt, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import asi_driver, japan_driver                    # noqa: E402
from cameras.asi import config as asi_config, devices as asi_devices  # noqa: E402
from cameras.japan import config as japan_config, devices as japan_devices  # noqa: E402
from cameras.japan.camera_sim import SimCamera                  # noqa: E402


def japan_worker(tmp_path, **overrides):
    cfg = {
        "output_dir": str(tmp_path),
        "mode": "sun_cycle",
        "name": "HAMA1",
        "sun_max_angle": -10.0,
        "dark_frames": 1,
        "dead_time": 0.1,
        "schedule_len": 2.0,
        "wait_for_enter": False,
        "camera": {"backend": "sim", "binning": 8},
        "filter_wheel": {"port": "sim"},
        "location": {"name": "TORY", "lat": 51.81, "lon": 103.08},
        "schedule": [{"delta": 0.0, "filter": 1, "exposure": 0.05}],
    }
    cfg.update(overrides)
    conf = japan_config.from_dict(cfg)
    cam = japan_driver.JapanCamera(conf)
    cam.cam = japan_devices.make_camera(conf)
    cam.wheel = japan_devices.make_wheel(conf)
    cam.cooling_controlled = cam._cooling_available()
    return japan_driver.JapanWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))


def asi_worker(tmp_path):
    conf = asi_config.from_dict({
        "output_dir": str(tmp_path),
        "mode": "sun_cycle",
        "sun_max_angle": -10.0,
        "dark_frames": 1,
        "camera": {"backend": "sim", "binning": 4},
        "filter_wheel": {"port": "sim"},
        "schedule": [{"delta": 0.0, "filter": 1, "exposure": 0.05,
                      "binning": 4, "readout": 0.5}],
    })
    cam = asi_driver.AsiCamera(conf)
    cam.cam = asi_devices.make_camera(conf)
    cam.wheel = asi_devices.make_wheel(conf)
    return asi_driver.AsiWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# The shutter between nights
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("make", [japan_worker, asi_worker],
                         ids=["japan", "asi"])
def test_the_shutter_is_shut_as_soon_as_a_night_is_over(make, tmp_path,
                                                         monkeypatch):
    worker = make(tmp_path)
    seen = []

    def one_night():
        if seen:                     # the second night: what did the day see?
            seen.append(worker.cam.shutter_open)
            worker.request_stop(force=True)
            return
        worker.cam.set_shutter(True)     # what the night opened after its darks
        seen.append(True)

    monkeypatch.setattr(worker, "_run_sun_cycle_mode", one_night)
    worker.run()
    assert seen == [True, False]


def test_a_stop_during_the_night_is_left_to_the_closing_darks(tmp_path,
                                                             monkeypatch):
    """No 'night over' on a stop: run()'s own ending shuts it and shoots darks."""
    worker = japan_worker(tmp_path)
    ended = []
    monkeypatch.setattr(worker, "_end_night", lambda: ended.append(1))

    def one_night():
        worker.cam.set_shutter(True)
        worker.request_stop(force=True)

    monkeypatch.setattr(worker, "_run_sun_cycle_mode", one_night)
    worker.run()
    assert ended == []
    assert worker.cam.shutter_open is False


def test_sun_mode_also_goes_on_night_after_night(tmp_path, monkeypatch):
    worker = japan_worker(tmp_path, mode="sun",
                          schedule=[{"seconds": [0], "filter": 1,
                                     "exposure": 0.05}])
    nights = []

    def one_night():
        nights.append(worker.cam.shutter_open)
        worker.cam.set_shutter(True)
        if len(nights) >= 2:
            worker.request_stop(force=True)

    monkeypatch.setattr(worker, "_run_sun_mode", one_night)
    worker.run()
    assert nights == [False, False]


# ---------------------------------------------------------------------------
# The japan cooler
# ---------------------------------------------------------------------------
def test_cooling_is_left_alone_unless_asked_for(tmp_path):
    worker = japan_worker(tmp_path)
    assert worker.cam.cooling_controlled is False
    assert worker.cam.set_cooler(False) is False
    assert worker.cam.cam.cooler_on is True          # untouched
    assert "cooler" not in worker.cam.temperature_fields()


def test_the_cooler_goes_on_lead_minutes_before_the_pre_darks(tmp_path,
                                                              monkeypatch):
    worker = japan_worker(tmp_path, cooling={"enabled": True,
                                             "lead_minutes": 1.5 / 60,
                                             "target_temp": -40})
    events = []
    real_set = worker.cam.cam.set_cooler

    def set_cooler(on):
        events.append(("cooler", on, dt.now()))
        return real_set(on)

    monkeypatch.setattr(worker.cam.cam, "set_cooler", set_cooler)
    dark_start = dt.now() + timedelta(seconds=2.5)
    assert worker._wait_for_pre_darks(dark_start, dark_start)
    events.append(("darks", None, dt.now()))

    kinds = [(kind, on) for kind, on, _ in events]
    # Off first: the camera powers up cooling, and it is still day.
    assert kinds == [("cooler", False), ("cooler", True), ("darks", None)]
    on_at = events[1][2]
    assert dark_start - on_at >= timedelta(seconds=1.2)
    assert worker.cam.cam.target_temperature == -40


def test_the_pre_darks_start_on_time_even_if_still_warm(tmp_path, monkeypatch):
    worker = japan_worker(tmp_path, cooling={"enabled": True, "lead_minutes": 0,
                                             "target_temp": -60})
    worker.cam.cam._temp = 20.0                       # nowhere near
    warnings = []
    monkeypatch.setattr(japan_driver.console_ui, "warn", warnings.append)
    dark_start = dt.now() + timedelta(seconds=0.3)
    assert worker._wait_for_pre_darks(dark_start, dark_start)
    assert dt.now() - dark_start < timedelta(seconds=1)
    assert any("not cooled" in w for w in warnings)


def test_the_cooler_is_switched_off_once_the_night_is_over(tmp_path):
    worker = japan_worker(tmp_path, cooling={"enabled": True})
    worker._cooler_on()
    worker._end_night()
    assert worker.cam.cam.cooler_on is False
    assert worker.cam.shutter_open is False


def test_a_refused_cooler_off_is_not_reported_as_done(tmp_path, monkeypatch):
    # Seen on a japan station: the camera answered OFF with INVALIDPARAM and the
    # log still said "Cooler off", so nothing ever tried again.
    worker = japan_worker(tmp_path, cooling={"enabled": True})
    worker._cooler_on()
    logs, warnings = [], []
    monkeypatch.setattr(japan_driver.console_ui, "log", logs.append)
    monkeypatch.setattr(japan_driver.console_ui, "warn", warnings.append)
    monkeypatch.setattr(worker.cam.cam, "set_cooler", lambda on: False)
    worker._cooler_off()
    assert "Cooler off" not in logs
    assert any("refused" in w for w in warnings)
    assert worker._cooler_commanded is True             # tried again next time


class _FakeDcam:
    """Just enough of ``Dcam`` for :meth:`HamamatsuCamera.set_cooler`."""

    def __init__(self, setvalue_ok, setgetvalue_ok, err=-2147481592):
        self._ok = (setvalue_ok, setgetvalue_ok)
        self._err = err
        self.value = 2.0

    def prop_setvalue(self, prop, value):
        if self._ok[0]:
            self.value = value
        return self._ok[0]

    def prop_setgetvalue(self, prop, value):
        if not self._ok[1]:
            return False
        self.value = value
        return value

    def prop_getvalue(self, prop):
        return self.value

    def prop_getattr(self, prop):
        class Attr:
            valuemin, valuemax = 2.0, 4.0
        return Attr()

    def lasterr(self):
        return self._err


def _hamamatsu(dcam):
    # The vendor module loads the DCAM runtime on import, so the few values the
    # cooler needs are restated here, as dcamapi4.py has them.
    from enum import IntEnum

    from cameras.japan import camera as hama

    class Sdk:
        class DCAM_IDPROP(IntEnum):
            SENSORCOOLER = 0x00200320

        class DCAMPROP:
            class SENSORCOOLER(IntEnum):
                OFF = 1
                ON = 2

        class DCAMERR(IntEnum):
            INVALIDPARAM = -2147481592

    cam = hama.HamamatsuCamera.__new__(hama.HamamatsuCamera)
    cam._sdk, cam._dcam = Sdk, dcam
    return cam, hama


@pytest.mark.parametrize("setvalue_ok, setgetvalue_ok",
                         [(True, False), (False, True)])
def test_hamamatsu_cooler_switches_by_either_set(setvalue_ok, setgetvalue_ok):
    cam, _ = _hamamatsu(_FakeDcam(setvalue_ok, setgetvalue_ok))
    assert cam.set_cooler(False) is True
    assert cam._dcam.value == 1


def test_hamamatsu_cooler_refusal_is_named(monkeypatch):
    cam, hama = _hamamatsu(_FakeDcam(False, False))
    warnings = []
    monkeypatch.setattr(hama.console_ui, "warn", warnings.append)
    assert cam.set_cooler(False) is False
    assert any("INVALIDPARAM" in w and "2..4" in w for w in warnings)


def test_hamamatsu_knows_when_off_is_out_of_range():
    cam, _ = _hamamatsu(_FakeDcam(True, True))      # accepts 2..4, as a C11090
    assert cam.cooler_can_switch_off() is False


def test_a_cooler_that_cannot_go_off_is_not_asked_to(tmp_path, monkeypatch):
    logs = []
    monkeypatch.setattr(japan_driver.console_ui, "log", logs.append)
    monkeypatch.setattr(SimCamera, "cooler_can_switch_off", lambda self: False)
    worker = japan_worker(tmp_path, cooling={"enabled": True})
    assert worker.cam.cooling_controlled is True
    assert any("cannot switch its cooler off" in m for m in logs)
    worker._cooler_on()
    calls = []
    monkeypatch.setattr(worker.cam.cam, "set_cooler",
                        lambda on: calls.append(on) or True)
    worker._end_night()
    assert calls == []
    assert "Cooler off" not in logs


def test_the_cooler_can_be_kept_on_between_nights(tmp_path):
    worker = japan_worker(tmp_path, cooling={"enabled": True,
                                             "off_between_nights": False})
    worker._cooler_on()
    worker._end_night()
    assert worker.cam.cam.cooler_on is True


def test_a_camera_without_a_switchable_cooler_is_warned_about(tmp_path,
                                                             monkeypatch):
    warnings = []
    monkeypatch.setattr(japan_driver.console_ui, "warn", warnings.append)
    monkeypatch.setattr(SimCamera, "cooler_supported", lambda self: False)
    worker = japan_worker(tmp_path, cooling={"enabled": True})
    assert worker.cam.cooling_controlled is False
    assert any("cooler" in w for w in warnings)


def test_the_dashboard_and_status_show_the_cooler(tmp_path):
    worker = japan_worker(tmp_path, cooling={"enabled": True, "target_temp": -50})
    worker._cooler_on()
    fields = worker.cam.temperature_fields()
    assert fields["cooler"] in ("busy", "ready")
    assert fields["setpoint"] == -50
    rows = dict(worker._camera_rows(fields))
    assert "cooler" in rows["Sensor temperature:"]


# ---------------------------------------------------------------------------
# japan.cooling
# ---------------------------------------------------------------------------
def test_cooling_defaults_are_off():
    conf = japan_config.from_dict({})
    assert conf.cooling.enabled is False
    assert conf.cooling.target_temp is None
    assert conf.cooling.lead_minutes == 30.0


def test_bad_cooling_values_are_reported_and_replaced():
    conf = japan_config.from_dict({"cooling": {"enabled": True,
                                               "lead_minutes": -5,
                                               "tolerance": 0,
                                               "target_temp": "cold"}})
    assert conf.cooling.lead_minutes == 30.0
    assert conf.cooling.tolerance == 3.0
    assert conf.cooling.target_temp is None
    assert sum("japan.cooling" in e for e in conf.errors) == 3
