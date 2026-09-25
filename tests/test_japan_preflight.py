"""The japan camera's preflight stage, run end to end against the simulators.

``sun_cycle`` exists to keep twilight out of the archive; the preflight stage
deliberately puts some of it back, under automatic exposure. The failure mode is
therefore the same one the mode was built to prevent, and these tests watch the
boundaries: nothing above the first setpoint, automatic exposures between the
two, scheduled exposures below the second, and a file name that always agrees
with the exposure the camera actually held.

This is ``test_asi_preflight`` asked of the other camera, and deliberately so:
the two drivers run one stage out of one module, and a difference between them
is a bug in whichever one has drifted. The differences that *are* meant to be
here are the camera's own — no gain, no cooling — and one rule of this driver's,
covered in ``test_japan_split``: the overexposure guard runs in ``sun_cycle``
only.

Time is not faked wholesale, as in ``test_japan_sun_cycle``: the clock runs and
the schedule is squeezed into a few seconds so a whole night fits inside a test.
"""
import sys
import threading

from datetime import datetime as dt, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import japan_driver                            # noqa: E402
from cameras.japan import config as japan_config, devices   # noqa: E402

pytest.importorskip("astropy.io.fits")

from astropy.io import fits                                 # noqa: E402


def make_config(tmp_path, **overrides):
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
        "location": {"name": "TORY", "lat": 51.81, "lon": 103.08,
                     "elevation": 658},
        # Slots a second apart, as in test_japan_sun_cycle: far enough that two
        # frames never share a second and collide on a file name.
        "schedule": [
            {"delta": 0.0, "filter": 1, "exposure": 0.05, "binning": 8},
            {"delta": 1.0, "filter": 3, "exposure": 0.05, "binning": 8},
        ],
        "preflight": {
            "enabled": True,
            "sun_start_angle": -6.0,
            "target_mean": 2000.0,      # brighter than the simulator's disc, so
                                        # the loop climbs to its ceiling and
                                        # stays there — which is what a twilight
                                        # exposure does until the sky catches up
            "min_exposure": 0.01,
            "tolerance": 0.05,
        },
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
    """Drop the whole-minute anchor so a test does not wait up to a minute."""
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


def lights(tmp_path):
    return sorted((p for p in tmp_path.rglob("*.fits")
                   if not p.name.endswith("_DARK.fits")),
                  key=lambda p: p.name)


def darks(tmp_path):
    return sorted(p.name for p in tmp_path.rglob("*_DARK.fits"))


def name_exposure_ms(path):
    """The exposure the archive file name claims, in milliseconds.

    Preflight frames carry a ``_pf`` suffix after the exposure field, so the
    last underscore-separated part is not always the exposure.
    """
    stem = path.name.removesuffix(".fits").removesuffix("_pf")
    return int(stem.split("_")[-1].removesuffix("ms"))


# ---------------------------------------------------------------------------
# The stage boundaries
# ---------------------------------------------------------------------------
def test_nothing_is_shot_above_the_first_setpoint(tmp_path, worker):
    """Daylight: even the preflight window is hours away."""
    run_cycle(worker, lambda when: 30.0, seconds=1.5)
    assert list(tmp_path.rglob("*.fits")) == []
    assert worker._phase == "waiting for pre-darks"


def test_the_predarks_finish_before_the_first_setpoint(tmp_path, worker,
                                                       prompt_anchor):
    """The darks are now timed against the preflight angle, not sun_max_angle."""
    opens = dt.now() + timedelta(seconds=2.5)

    def angle(when):
        return -7.0 if when >= opens else 5.0

    run_cycle(worker, angle, seconds=8.0)
    assert darks(tmp_path), "the pre-darks never ran"
    assert lights(tmp_path), "the preflight stage never started"
    assert max(darks(tmp_path)) < min(p.name for p in lights(tmp_path))


def test_the_stage_shoots_between_the_two_setpoints(tmp_path, worker,
                                                    prompt_anchor):
    """−7° is below the preflight angle and above sun_max_angle."""
    run_cycle(worker, lambda when: -7.0, seconds=4.0)
    assert lights(tmp_path), "nothing was shot in the preflight window"
    assert worker._stage == "preflight"


def test_the_frames_are_tagged_so_processing_can_tell_them_apart(tmp_path, worker,
                                                                 prompt_anchor):
    run_cycle(worker, lambda when: -7.0, seconds=4.0)
    modes = {fits.getheader(p)["OBSMODE"] for p in lights(tmp_path)}
    assert modes == {"sun_cycle_auto"}
    assert all(p.name.endswith("_pf.fits") for p in lights(tmp_path))


def test_the_exposure_is_not_the_scheduled_one(tmp_path, worker, prompt_anchor):
    """The whole point: the schedule's exposure is ignored while automating."""
    run_cycle(worker, lambda when: -7.0, seconds=4.0)
    exposures = {fits.getheader(p)["EXPTIME"] for p in lights(tmp_path)}
    assert exposures, "no preflight frames to check"
    assert exposures != {0.05}, "the stage never moved off the slot exposure"
    assert all(e >= 0.01 for e in exposures), "below the configured minimum"
    assert all(e <= 0.05 for e in exposures), "longer than the slot's own"


def test_the_file_name_carries_the_exposure_that_was_actually_used(tmp_path, worker,
                                                                   prompt_anchor):
    """The name and EXPTIME come from one value, applied to the camera once."""
    run_cycle(worker, lambda when: -7.0, seconds=4.0)
    assert lights(tmp_path), "no preflight frames to check"
    for path in lights(tmp_path):
        header_ms = round(fits.getheader(path)["EXPTIME"] * 1000)
        assert name_exposure_ms(path) == header_ms


def test_the_main_cycle_starts_at_the_second_setpoint(tmp_path, worker,
                                                      prompt_anchor):
    """Below sun_max_angle the automation stops and the programme resumes."""
    handover = dt.now() + timedelta(seconds=3.0)

    def angle(when):
        return -12.0 if when >= handover else -7.0

    run_cycle(worker, angle, seconds=7.0)
    modes = [fits.getheader(p)["OBSMODE"] for p in lights(tmp_path)]
    assert "sun_cycle_auto" in modes, "the preflight stage never ran"
    assert "sun_cycle" in modes, "the main cycle never started"
    # Once it hands over it never goes back.
    assert modes.index("sun_cycle") > modes.index("sun_cycle_auto")
    assert modes[-1] == "sun_cycle"
    assert worker._stage == "main"


def test_the_main_cycle_uses_the_scheduled_exposure(tmp_path, worker,
                                                    prompt_anchor):
    handover = dt.now() + timedelta(seconds=3.0)

    def angle(when):
        return -12.0 if when >= handover else -7.0

    run_cycle(worker, angle, seconds=7.0)
    main = [p for p in lights(tmp_path)
            if fits.getheader(p)["OBSMODE"] == "sun_cycle"]
    assert main, "the main cycle never started"
    assert {fits.getheader(p)["EXPTIME"] for p in main} == {0.05}


def test_the_run_still_ends_at_sun_max_angle(tmp_path, worker, prompt_anchor):
    """Sunrise: the main cycle ends where it always did.

    The automatic stage is an evening prelude, not a morning encore, so a sun
    climbing back through sun_max_angle stops the session even though it is
    still below the preflight angle.
    """
    closes = dt.now() + timedelta(seconds=3.0)

    def angle(when):
        return -8.0 if when >= closes else -12.0

    started = dt.now()
    worker._sun_angle_fn = lambda: angle
    worker._run_sun_cycle_mode()          # no stop timer: a hang fails the suite
    assert dt.now() - started < timedelta(seconds=15)
    assert lights(tmp_path), "the session ended without taking anything"


def test_the_stage_ends_when_the_sun_climbs_above_its_own_angle(tmp_path, worker,
                                                                prompt_anchor):
    """A session that never got below sun_max_angle still has to end.

    High summer at a northern site: the sun dips past the preflight setpoint,
    never reaches the main one, and comes back up. Without this the loop would
    wait for a handover that is not coming.
    """
    closes = dt.now() + timedelta(seconds=3.0)

    def angle(when):
        return -4.0 if when >= closes else -7.0

    started = dt.now()
    worker._sun_angle_fn = lambda: angle
    worker._run_sun_cycle_mode()
    assert dt.now() - started < timedelta(seconds=15)
    assert worker._stage == "preflight", "it handed over with nothing to hand to"
    assert lights(tmp_path), "the twilight session took nothing"


def test_a_run_started_after_dark_goes_straight_to_the_main_cycle(tmp_path, worker,
                                                                  prompt_anchor):
    """A restart at midnight has no twilight left to automate."""
    run_cycle(worker, lambda when: -20.0, seconds=4.0)
    assert worker._stage == "main"
    assert {fits.getheader(p)["OBSMODE"] for p in lights(tmp_path)} == {"sun_cycle"}


# ---------------------------------------------------------------------------
# The loop itself
# ---------------------------------------------------------------------------
def test_the_exposure_moves_towards_the_target(tmp_path, worker, prompt_anchor):
    """The simulator's mean is monotone in exposure, so the loop must converge."""
    run_cycle(worker, lambda when: -7.0, seconds=8.0)
    frames = lights(tmp_path)
    if len(frames) < 4:
        pytest.skip("too few frames in the window to judge convergence")
    target = worker.cfg.preflight.target_mean
    first = fits.getheader(frames[0])["SKYMEAN"]
    last = fits.getheader(frames[-1])["SKYMEAN"]
    assert abs(last - target) < abs(first - target)


def test_every_frame_records_what_it_measured(tmp_path, worker, prompt_anchor):
    """``SKYMEAN`` is what makes an archived preflight frame interpretable."""
    run_cycle(worker, lambda when: -7.0, seconds=4.0)
    assert lights(tmp_path), "no preflight frames to check"
    for path in lights(tmp_path):
        assert fits.getheader(path)["SKYMEAN"] > 0


def test_each_slot_keeps_its_own_exposure(tmp_path, prompt_anchor):
    """Two filters, one much brighter: their exposures must diverge.

    One exposure shared across the slots would be the obvious implementation and
    the wrong one — through one filter the sky is orders of magnitude brighter
    than through another, and a single number cannot hold for both.
    """
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    real = type(worker.cam.cam).capture

    def brighter_on_filter_3(self):
        frame = real(self)
        if worker.cam.current_filter == 3:
            return (frame.astype("uint32") * 10).clip(0, 65535).astype("<u2")
        return frame

    type(worker.cam.cam).capture = brighter_on_filter_3
    try:
        run_cycle(worker, lambda when: -7.0, seconds=8.0)
    finally:
        type(worker.cam.cam).capture = real

    by_filter = {}
    for path in lights(tmp_path):
        header = fits.getheader(path)
        by_filter.setdefault(header["FILTER"], set()).add(header["EXPTIME"])
    if len(by_filter) < 2:
        pytest.skip("the window was too short to visit both slots")
    assert len(set(map(frozenset, by_filter.values()))) > 1, (
        "both slots were given the same exposure")


def test_the_forced_binning_holds_across_the_stage(tmp_path, prompt_anchor):
    """``preflight.binning`` overrides the slot's own, and only while automating."""
    conf = make_config(tmp_path, preflight={
        "enabled": True, "sun_start_angle": -6.0, "target_mean": 2000.0,
        "min_exposure": 0.01, "tolerance": 0.05, "binning": 4,
    })
    worker = make_worker(tmp_path, conf)
    handover = dt.now() + timedelta(seconds=3.0)

    def angle(when):
        return -12.0 if when >= handover else -7.0

    run_cycle(worker, angle, seconds=7.0)
    binnings = {}
    for path in lights(tmp_path):
        header = fits.getheader(path)
        binnings.setdefault(header["OBSMODE"], set()).add(header["BINNING"])
    assert binnings.get("sun_cycle_auto") == {4}, "the forced binning was ignored"
    assert binnings.get("sun_cycle") == {8}, "the slot never got its own back"


def test_the_status_payload_reports_the_stage(tmp_path, worker, prompt_anchor,
                                              monkeypatch):
    """The monitor tells the two stages apart off these keys, as it does for asi."""
    captured = {}
    monkeypatch.setattr(worker._bus, "publish_status",
                        lambda payload, force=False: captured.update(payload))
    run_cycle(worker, lambda when: -7.0, seconds=4.0)
    assert captured["stage"] == "preflight"
    assert captured["auto_exposure"] is not None
    assert captured["last_mean"] > 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_a_disabled_stage_waits_for_the_main_angle(tmp_path):
    """With the stage off, −7° is still daylight as far as the run is concerned."""
    conf = make_config(tmp_path, preflight={"enabled": False})
    worker = make_worker(tmp_path, conf)
    run_cycle(worker, lambda when: -7.0, seconds=3.0)
    assert list(tmp_path.rglob("*.fits")) == [], "it shot above sun_max_angle"


def test_the_stage_is_off_unless_asked_for(tmp_path):
    conf = make_config(tmp_path)
    conf = japan_config.from_dict({"mode": "sun_cycle", "name": "HAMA1"})
    assert conf.preflight.enabled is False
    assert conf.overexposure.enabled is False


def test_a_first_setpoint_below_the_second_is_reported(tmp_path):
    """The sun passes the setpoints in order; reversed, there is no stage at all."""
    conf = make_config(tmp_path, preflight={"enabled": True,
                                            "sun_start_angle": -12.0})
    assert conf.preflight.enabled is False
    assert any("sun_start_angle" in e for e in conf.errors)


def test_the_stage_outside_sun_cycle_mode_is_reported(tmp_path):
    conf = make_config(tmp_path, mode="time", t_start="20:00",
                       preflight={"enabled": True, "sun_start_angle": -6.0})
    assert conf.preflight.enabled is False
    assert any("sun_cycle" in e and "preflight" in e for e in conf.errors)


def test_a_target_outside_the_bit_depth_is_reported(tmp_path):
    conf = make_config(tmp_path, preflight={"enabled": True,
                                            "sun_start_angle": -6.0,
                                            "target_mean": 99999.0})
    assert conf.preflight.enabled is False
    assert any("target_mean" in e for e in conf.errors)


def test_a_binning_the_sensor_cannot_take_is_reported(tmp_path):
    """The Hamamatsu takes 1..8; the PIXIS takes fewer. Each camera says its own."""
    conf = make_config(tmp_path, preflight={"enabled": True,
                                            "sun_start_angle": -6.0,
                                            "binning": 99})
    assert conf.preflight.binning is None
    assert any("preflight.binning" in e for e in conf.errors)
