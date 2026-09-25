"""The japan camera's ``sun_cycle`` mode, run end to end against the simulators.

Two promises are pinned here. The first is the ASI imager's: between start-up and
the moment the sun reaches ``sun_max_angle`` only the pre-darks may touch the
shutter. The second is what the mode exists for: the cycle's phase is locked to
``t_start`` in UTC, so whichever slot is due when the window opens is the one
shot, and two stations whose windows open at different moments still shoot the
same filter at the same instant.

Frames go into the ASI archive layout with the ASI header set, minus the cards
that describe the camera and its software, plus ``NAME``.

Time is not faked wholesale: the clock runs, and the schedule is squeezed into a
few seconds so a whole night fits inside a test.
"""
import re
import sys
import threading

from datetime import datetime as dt, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import japan_driver                             # noqa: E402
from cameras.common import schedule as common_schedule       # noqa: E402
from cameras.common.timeutil import to_utc                   # noqa: E402
from cameras.japan import config as japan_config, devices    # noqa: E402

fits = pytest.importorskip("astropy.io.fits")

PERIOD = 2.0


def make_config(tmp_path, **overrides):
    cfg = {
        "output_dir": str(tmp_path),
        "mode": "sun_cycle",
        "name": "HAMA1",
        "sun_max_angle": -10.0,
        "dark_frames": 1,
        "dead_time": 0.1,
        "schedule_len": PERIOD,
        "wait_for_enter": False,
        "camera": {"backend": "sim", "binning": 8},
        "filter_wheel": {"port": "sim"},
        "location": {"name": "TORY", "lat": 51.81, "lon": 103.08,
                     "elevation": 658},
        "schedule": [
            # Slots a second apart: close enough for a quick test, far enough
            # that two frames never share a second and collide on a file name.
            {"delta": 0.0, "filter": 1, "exposure": 0.05},
            {"delta": 1.0, "filter": 3, "exposure": 0.05},
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
    return make_worker(tmp_path, make_config(tmp_path))


@pytest.fixture
def prompt_anchor(monkeypatch):
    """Without ``t_start`` the cycle waits for a whole minute; skip that wait."""
    monkeypatch.setattr(japan_driver.japan_schedule, "next_minute_boundary",
                        lambda t: t)


def run_cycle(worker, angle_fn, seconds):
    worker._sun_angle_fn = lambda: angle_fn
    timer = threading.Timer(seconds, worker.request_stop)
    timer.start()
    try:
        worker._run_sun_cycle_mode()
    finally:
        timer.cancel()


def frames(tmp_path):
    return sorted(tmp_path.rglob("*.fits"))


def light_frames(tmp_path):
    return [p for p in frames(tmp_path) if not p.name.endswith("_DARK.fits")]


def dark_frames(tmp_path):
    return [p for p in frames(tmp_path) if p.name.endswith("_DARK.fits")]


def date_obs(path):
    stamp = dt.strptime(fits.getheader(path)["DATE-OBS"], "%Y-%m-%dT%H:%M:%S.%f")
    return stamp.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Nothing happens before the sun is low enough
# ---------------------------------------------------------------------------
def test_no_frame_is_taken_while_the_sun_is_still_up(worker, tmp_path):
    run_cycle(worker, lambda when: 30.0, seconds=1.5)
    assert frames(tmp_path) == []
    assert worker._phase == "waiting for pre-darks"


def test_the_darks_come_before_any_light_frame(worker, tmp_path, prompt_anchor):
    opens = dt.now() + timedelta(seconds=2.5)

    def angle(when):
        return -20.0 if when >= opens else 5.0

    run_cycle(worker, angle, seconds=8.0)
    darks, lights = dark_frames(tmp_path), light_frames(tmp_path)
    assert darks, "the pre-darks never ran"
    assert lights, "the cycle never started"
    assert max(date_obs(p) for p in darks) < min(date_obs(p) for p in lights)


def test_the_run_stops_when_the_sun_comes_back_up(worker, tmp_path,
                                                  prompt_anchor):
    closes = dt.now() + timedelta(seconds=3.0)

    def angle(when):
        return 5.0 if when >= closes else -20.0

    started = dt.now()
    worker._sun_angle_fn = lambda: angle
    worker._run_sun_cycle_mode()
    assert dt.now() - started < timedelta(seconds=15)
    assert light_frames(tmp_path), "the session ended without taking anything"


def test_the_run_goes_on_night_after_night(worker, monkeypatch):
    """Sunrise ends a session, not the run."""
    nights = []

    def one_night():
        nights.append(1)
        if len(nights) >= 3:
            worker.request_stop(force=True)

    monkeypatch.setattr(worker, "_run_sun_cycle_mode", one_night)
    worker.run()
    assert len(nights) == 3


# ---------------------------------------------------------------------------
# The cycle's phase is t_start, in UTC
# ---------------------------------------------------------------------------
def test_the_slots_are_phase_locked_to_t_start_in_utc(tmp_path):
    """Each light lands on t_start + k·period + delta, with the right filter.

    ``t_start`` is set a couple of seconds into the future and off any whole
    second, so neither a whole-minute anchor nor an anchor at the
    moment the window opened could produce these phases by accident.
    """
    # Slots two seconds apart here: the simulated wheel takes a second to move,
    # and a frame shot late because of it would say nothing about the phase.
    period = 4.0
    t0 = (dt.now(timezone.utc) + timedelta(seconds=2.4)).replace(tzinfo=None)
    conf = make_config(tmp_path, t_start=t0.time(), schedule_len=period,
                       schedule=[{"delta": 0.0, "filter": 1, "exposure": 0.05},
                                 {"delta": 2.0, "filter": 3, "exposure": 0.05}])
    assert conf.errors == []
    worker = make_worker(tmp_path, conf)
    run_cycle(worker, lambda when: -20.0, seconds=13.0)

    lights = sorted(light_frames(tmp_path), key=date_obs)
    assert len(lights) >= 4, "the cycle took too few frames to judge its phase"
    anchor = t0.replace(tzinfo=timezone.utc)
    # The first frame follows the wheel's first move out of home, which the
    # simulator makes take a whole second; it can be that late, and says
    # nothing about the phase. Every frame after it must sit on its slot.
    for path in lights[1:]:
        phase = (date_obs(path) - anchor).total_seconds() % period
        header = fits.getheader(path)
        if abs(phase) < 0.3 or abs(phase - period) < 0.3:
            assert header["FILTER"] == 1, path.name
        else:
            assert abs(phase - 2.0) < 0.3, f"{path.name}: phase {phase:.2f} s"
            assert header["FILTER"] == 3, path.name


def test_a_window_opening_mid_cycle_joins_at_the_slot_that_is_due(tmp_path):
    """The run does not restart the cycle from slot zero when the sun is ready."""
    # Slot zero fell 1.2 s ago, slot one is due 0.2 s before this second is
    # out: a run joining the cycle now must open with filter 3, not filter 1.
    t0 = (dt.now(timezone.utc) - timedelta(seconds=PERIOD + 0.6)).replace(tzinfo=None)
    conf = make_config(tmp_path, t_start=t0.time(), dark_frames=0)
    worker = make_worker(tmp_path, conf)
    anchor = common_schedule.utc_cycle_anchor(conf.schedule.t_start, dt.now())
    _slot, entry, _k = common_schedule.next_cycle_slot(
        anchor, PERIOD, conf.schedule.entries, dt.now())
    run_cycle(worker, lambda when: -20.0, seconds=1.2)
    lights = light_frames(tmp_path)
    assert lights
    assert fits.getheader(lights[0])["FILTER"] == entry.filter


def test_utc_cycle_anchor_is_the_nearest_utc_occurrence():
    now = dt.now()
    t_utc = (to_utc(now) + timedelta(hours=3)).time().replace(microsecond=0)
    anchor = common_schedule.utc_cycle_anchor(t_utc, now)
    assert to_utc(anchor).time() == t_utc
    assert abs((anchor - now).total_seconds()) <= 12 * 3600


def test_the_snapshot_reports_t_start_as_the_utc_time_it_is(tmp_path):
    conf = make_config(tmp_path, t_start="12:00")
    snapshot = common_schedule.schedule_snapshot(conf.schedule)
    assert snapshot["mode"] == "sun_cycle"
    assert snapshot["t_start_utc"] == "12:00:00"
    anchor = common_schedule.utc_cycle_anchor(conf.schedule.t_start, dt.now())
    assert snapshot["t_start"] == anchor.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# The ASI archive: names, tree and header
# ---------------------------------------------------------------------------
def test_frames_land_in_a_utc_date_tree_with_asi_names(worker, tmp_path,
                                                        prompt_anchor):
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    files = frames(tmp_path)
    assert files
    for path in files:
        year, month, day = path.relative_to(tmp_path).parts[:3]
        assert (len(year), len(month), len(day)) == (4, 2, 2)
        assert "_TORY_HAMA1_" in path.name
        assert path.name.endswith("ms.fits") or path.name.endswith("_DARK.fits")
    for path in light_frames(tmp_path):
        assert re.search(r"_TORY_HAMA1_(5577|OH__)_\d{6}ms\.fits$",
                         path.name), path.name


def test_the_header_names_the_instrument_and_nothing_else_about_it(
        worker, tmp_path, prompt_anchor):
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    lights = light_frames(tmp_path)
    assert lights
    header = fits.getheader(lights[0])
    for key in ("INSTRUME", "VENDOR", "CAMSN", "CAMVER", "DRVVER", "DCAMVER",
                "BitDepth", "CCDGain", "DeviceID", "Version", "GAIN", "SETTEMP"):
        assert key not in header, key
    assert header["NAME"] == "HAMA1"
    assert header["SiteID"] == "TORY"
    assert header["OBSMODE"] == "sun_cycle"
    assert header["IMAGETYP"] == "LIGHT"
    assert header["SITEELEV"] == 658
    assert header["ReadoutSpeed"] == "2 (fast)"
    assert header["FilterWavelength"] in ("5577", "OH__")
    assert "SEQNO" in header


def test_a_plain_night_measures_but_does_not_divide(worker, tmp_path,
                                                    prompt_anchor):
    """With both intensity loops off, the mode is the cycle and nothing else.

    ``SKYMEAN`` is still written — it costs one pass over the array already made
    and is the first thing anyone asks of an archived frame — but the ``SPLIT*``
    pair belongs to a slot that was actually divided, and no slot is, here.
    """
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    lights = light_frames(tmp_path)
    assert lights
    for path in lights:
        header = fits.getheader(path)
        assert header["SKYMEAN"] > 0
        assert "SPLITNUM" not in header
        assert "SPLITIDX" not in header
        assert header["OBSMODE"] == "sun_cycle"
        assert not path.name.endswith("_pf.fits")


def test_the_intensity_keys_read_as_an_idle_main_stage(worker, tmp_path,
                                                       prompt_anchor,
                                                       monkeypatch):
    """What the monitor sees on a station that has not enabled either loop."""
    captured = {}
    monkeypatch.setattr(worker._bus, "publish_status",
                        lambda payload, force=False: captured.update(payload))
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    assert captured["stage"] == "main"
    assert captured["auto_exposure"] is None
    assert captured["split_frames"] == 1


def test_the_darks_are_filed_and_headed_the_same_way(worker, tmp_path,
                                                     prompt_anchor):
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    darks = dark_frames(tmp_path)
    assert darks
    header = fits.getheader(darks[0])
    assert header["IMAGETYP"] == "DARK"
    assert header["OBSMODE"] == "dark"
    assert header["NAME"] == "HAMA1"
    assert "INSTRUME" not in header


def test_a_seqno_counter_is_kept_beside_the_archive(worker, tmp_path,
                                                   prompt_anchor):
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    counter = tmp_path / "seqno.txt"
    assert counter.exists()
    assert worker._errors == 0
    assert int(counter.read_text()) == len(frames(tmp_path))


def test_sun_and_time_modes_keep_their_flat_names(tmp_path):
    """The archive layout belongs to sun_cycle alone."""
    conf = make_config(tmp_path, mode="time", t_start="20:00")
    worker = make_worker(tmp_path, conf)
    assert not worker._archive_layout
    path = worker._unique_frame_path(dt(2026, 7, 29, 14, 5, 30), exposure=5.0)
    assert path.parent == tmp_path
    assert "_" in path.stem and "ms" not in path.stem
