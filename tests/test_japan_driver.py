"""The two observing programmes, run end to end against the simulators.

japan-camera's whole shape is an ordering: nothing is exposed before the window
opens, the darks bracket the measurements, and in ``time`` mode the frames land on
their phase-locked slots and nowhere else. A regression there does not crash — it
quietly fills the archive with frames the processing program will treat as data.
So these tests watch the *order* of events, not just the count.

Time is not faked wholesale: the clock runs, and the schedule is squeezed into a
few seconds so a whole night fits inside a test.
"""
import re
import sys
import threading

from datetime import datetime as dt, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import japan_driver                             # noqa: E402
from cameras.japan import config as japan_config, devices    # noqa: E402

pytest.importorskip("astropy.io.fits")

fits = pytest.importorskip("astropy.io.fits")

# ``YYYYmmddTHHMMSS_<filter>`` with an optional ``_bg``.
NAME_RE = re.compile(r"^\d{8}T\d{6}_\d(_bg)?\.fits$")


def make_config(tmp_path, **overrides):
    cfg = {
        "output_dir": str(tmp_path),
        "mode": "sun",
        "sun_max_angle": -10.0,
        "dark_frames": 1,
        "dead_time": 0.1,
        "wait_for_enter": False,
        "camera": {"backend": "sim", "binning": 8},
        "filter_wheel": {"port": "sim"},
        "location": {"lat": 51.81, "lon": 103.08, "elevation": 658},
        "schedule": [
            {"filter": 1, "exposure": 0.05, "seconds": [0]},
            {"filter": 3, "exposure": 0.05, "seconds": [30]},
        ],
    }
    cfg.update(overrides)
    return japan_config.from_dict(cfg)


def make_worker(tmp_path, conf):
    cam = japan_driver.JapanCamera(conf)
    cam.cam = devices.make_camera(conf)
    cam.wheel = devices.make_wheel(conf)
    return japan_driver.JapanWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))


@pytest.fixture
def worker(tmp_path):
    """A worker on both simulators, wired to write into ``tmp_path``."""
    return make_worker(tmp_path, make_config(tmp_path))


@pytest.fixture
def prompt_slots(monkeypatch):
    """Bring the next capture second forward so a test does not wait a minute.

    ``sun`` mode waits for a named second of the minute, which is the whole point
    of it — and which would make every test here take up to sixty seconds. The
    wait itself is pinned by ``test_a_slot_really_waits_for_its_second``; every
    other test is about what happens around it.
    """
    monkeypatch.setattr(japan_driver.japan_schedule, "next_second_slot",
                        lambda seconds, now=None: dt.now() + timedelta(seconds=0.2))


def run_sun(worker, angle_fn, seconds):
    """Run ``_run_sun_mode`` with a scripted sun, stopping after ``seconds``."""
    worker._sun_angle_fn = lambda: angle_fn
    timer = threading.Timer(seconds, worker.request_stop)
    timer.start()
    try:
        worker._run_sun_mode()
    finally:
        timer.cancel()


def run_time(worker, seconds):
    timer = threading.Timer(seconds, worker.request_stop)
    timer.start()
    try:
        worker._run_time_mode()
    finally:
        timer.cancel()


def saved(tmp_path):
    return sorted(p.name for p in tmp_path.rglob("*.fits"))


def lights(tmp_path):
    return [n for n in saved(tmp_path) if not n.endswith("_bg.fits")]


def darks(tmp_path):
    return [n for n in saved(tmp_path) if n.endswith("_bg.fits")]


# ---------------------------------------------------------------------------
# Sun mode: nothing happens before the window opens
# ---------------------------------------------------------------------------
def test_no_frame_is_taken_while_the_sun_is_still_up(worker, tmp_path):
    """Daylight: the window is hours away, so not even the darks have started."""
    run_sun(worker, lambda when: 30.0, seconds=1.5)
    assert saved(tmp_path) == []
    assert worker._phase == "waiting for pre-darks"


def test_the_pre_darks_come_before_any_light(worker, tmp_path, prompt_slots):
    """The three-phase flow, in order: darks, then measurements."""
    run_sun(worker, lambda when: -20.0, seconds=2.0)
    names = saved(tmp_path)
    assert darks(tmp_path), "no pre-darks were taken"
    assert lights(tmp_path), "no measurements were taken"
    first_light = names.index(lights(tmp_path)[0])
    assert names.index(darks(tmp_path)[0]) < first_light


def test_the_run_ends_by_itself_when_the_sun_comes_back_up(worker, tmp_path,
                                                           prompt_slots):
    """Sunrise ends a night: the loop returns rather than waiting for a stop."""
    angles = iter([-20.0] * 6 + [30.0] * 200)
    # Never stopped from outside — if the loop does not end on its own the test
    # hangs, which is the failure worth having.
    worker._sun_angle_fn = lambda: (lambda when: next(angles, 30.0))
    worker._run_sun_mode()
    assert lights(tmp_path), "the session should have taken frames first"
    assert not worker.stopping


def test_a_window_that_closes_during_the_wait_costs_the_frame_not_the_run(
        worker, tmp_path, prompt_slots):
    """The re-check after the wait is what stops a twilight frame being filed."""
    # Low enough to open the session, then up before the first slot fires.
    state = {"calls": 0}

    def angle(when):
        state["calls"] += 1
        return -20.0 if state["calls"] <= 2 else 30.0

    run_sun(worker, angle, seconds=2.0)
    assert lights(tmp_path) == []
    assert worker._errors == 0


def test_a_slot_really_waits_for_its_second(worker, tmp_path):
    """Without the fixture: the capture lands on the scheduled second."""
    seconds = [(dt.now().second + 2) % 60]
    worker.cfg.schedule.entries[0].seconds = seconds
    worker.cfg.schedule.entries = worker.cfg.schedule.entries[:1]
    worker.cfg.schedule.dark_frames = 0
    run_sun(worker, lambda when: -20.0, seconds=4.0)
    names = lights(tmp_path)
    assert names, "no frame was taken"
    assert int(names[0][13:15]) == seconds[0]


# ---------------------------------------------------------------------------
# Sun mode: what lands on disk
# ---------------------------------------------------------------------------
def test_frames_are_flat_and_carry_japan_camera_names(worker, tmp_path,
                                                      prompt_slots):
    run_sun(worker, lambda when: -20.0, seconds=2.0)
    assert saved(tmp_path), "nothing was written"
    for path in tmp_path.rglob("*.fits"):
        assert path.parent == tmp_path, f"{path} is not flat in the output dir"
        assert NAME_RE.match(path.name), path.name


def test_no_seqno_counter_is_written(worker, tmp_path, prompt_slots):
    """``seqno.txt`` belongs to the ASI archive, not to this one."""
    run_sun(worker, lambda when: -20.0, seconds=2.0)
    assert not (tmp_path / "seqno.txt").exists()


def test_a_light_frames_header_carries_its_slot(worker, tmp_path, prompt_slots):
    run_sun(worker, lambda when: -20.0, seconds=2.5)
    names = lights(tmp_path)
    assert names
    header = fits.getheader(tmp_path / names[0])
    assert header["IMAGETYP"] == "LIGHT"
    assert header["OBSMODE"] == "sun"
    assert header["EXPTIME"] == pytest.approx(0.05)
    assert header["BINNING"] == 8
    assert header["FILTER"] in (1, 3)
    # The filter in the name and the filter in the header are the same fact.
    assert int(names[0].split("_")[1].split(".")[0]) == header["FILTER"]


def test_the_darks_are_shot_with_the_shutter_shut(worker, tmp_path, prompt_slots):
    run_sun(worker, lambda when: -20.0, seconds=2.0)
    for name in darks(tmp_path):
        assert fits.getheader(tmp_path / name)["IMAGETYP"] == "DARK"
        assert fits.getheader(tmp_path / name)["OBSMODE"] == "dark"


def test_the_status_payload_is_what_the_monitor_reads(worker, monkeypatch):
    """The monitor keys every per-camera row off ``camera_type``.

    The absent keys matter as much as the present ones: ``set_temp`` and
    ``temp_locked`` would make ``monitor_app`` draw a setpoint this camera does
    not have, and ``gain`` would name a control it does not have either.
    """
    captured = {}
    monkeypatch.setattr(worker._bus, "publish_status",
                        lambda payload, force=False: captured.update(payload))
    worker._save_status("running", readings=worker.cam.temperature_fields())

    assert captured["camera_type"] == "japan"
    for key in ("status", "phase", "mode", "exposure", "binning",
                "readout_speed", "filter", "shutter", "ccd_temp"):
        assert key in captured, key
    for key in ("set_temp", "temp_locked", "gain", "stage", "auto_exposure",
                "split_frames"):
        assert key not in captured, key


def test_the_published_params_match_the_focus_app_schema(worker):
    """A name in one and not the other is a field focus_app cannot round-trip."""
    from camera_service import PARAM_SCHEMAS

    schema = {field["name"] for field in PARAM_SCHEMAS["japan"]}
    assert set(worker._current_params()) == schema


# ---------------------------------------------------------------------------
# Time mode
# ---------------------------------------------------------------------------
def time_config(tmp_path, t_start, **overrides):
    return make_config(
        tmp_path, mode="time", t_start=t_start, dead_time=1.0,
        schedule=[
            {"delta": 0.0, "filter": 1, "exposure": 0.05, "binning": 8},
            {"delta": 1.0, "filter": 3, "exposure": 0.05, "binning": 8},
        ],
        **overrides)


def test_a_late_start_skips_the_opening_darks(tmp_path):
    """Started after T_start there is no time for them; only the closing ones run."""
    past = (dt.now() - timedelta(hours=2)).strftime("%H:%M:%S")
    worker = make_worker(tmp_path, time_config(tmp_path, past))
    run_time(worker, seconds=2.0)
    assert darks(tmp_path) == []
    assert lights(tmp_path), "measurements should still have run"


def test_an_on_time_start_shoots_the_opening_darks_first(tmp_path):
    future = (dt.now() + timedelta(seconds=30)).strftime("%H:%M:%S")
    worker = make_worker(tmp_path, time_config(tmp_path, future))
    run_time(worker, seconds=2.0)
    assert darks(tmp_path), "the opening darks were skipped"


def test_the_slots_are_phase_locked_to_t_start(tmp_path):
    """Frames land on t_start + k*period + delta, not "as fast as possible"."""
    started = dt.now()
    t_start = (started - timedelta(seconds=10)).replace(microsecond=0)
    worker = make_worker(tmp_path, time_config(tmp_path,
                                               t_start.strftime("%H:%M:%S")))
    period = worker.cfg.schedule.period          # 1.0 + 0.05 + 1.0 = 2.05 s
    run_time(worker, seconds=5.0)

    names = lights(tmp_path)
    assert len(names) >= 3, names
    # The names are UTC, but the *gaps* between them are zone-independent, and
    # they are what the phase lock produces: the two slots sit 1 s apart (their
    # deltas), and the last of a cycle to the first of the next closes the period.
    stamps = [dt.strptime(name[:15], "%Y%m%dT%H%M%S") for name in names]
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    for gap in gaps:
        assert (gap == pytest.approx(1.0, abs=0.6)
                or gap == pytest.approx(period - 1.0, abs=0.6)), gaps
    # And nothing free-runs: over several cycles the frames stay on the grid.
    assert sum(gaps) == pytest.approx((len(gaps) / 2) * period, abs=1.0)


def test_the_closing_darks_always_run(tmp_path):
    past = (dt.now() - timedelta(hours=2)).strftime("%H:%M:%S")
    conf = time_config(tmp_path, past)
    worker = make_worker(tmp_path, conf)
    run_time(worker, seconds=1.5)
    assert darks(tmp_path) == []
    # ``run()``'s finally is what shoots them; call the same step directly.
    worker._capture_darks("final")
    assert darks(tmp_path), "the closing darks were not taken"


def test_a_time_frames_header_says_time(tmp_path):
    past = (dt.now() - timedelta(hours=2)).strftime("%H:%M:%S")
    worker = make_worker(tmp_path, time_config(tmp_path, past))
    run_time(worker, seconds=2.0)
    names = lights(tmp_path)
    assert names
    assert fits.getheader(tmp_path / names[0])["OBSMODE"] == "time"


# ---------------------------------------------------------------------------
# Setup mode
# ---------------------------------------------------------------------------
def test_setup_mode_archives_nothing(tmp_path):
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    worker.setup_mode = True
    timer = threading.Timer(1.0, worker.request_stop)
    timer.start()
    try:
        worker._run_setup_mode()
    finally:
        timer.cancel()
    assert saved(tmp_path) == []
    # The shutter is opened so a focus session sees something other than dark.
    assert worker.cam.shutter_open is True
