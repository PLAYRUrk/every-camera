"""The japan camera's overexposure guard: one slot, several shorter frames.

Twilight and moonrise put a filter over the top of the sensor's range, and a
saturated frame is not a dim frame — it is no measurement at all. The guard
divides an over-bright slot into sub-frames on its *next* visit, inside the time
the slot already had, and lets it back to one frame when the sky allows.

The arithmetic is ``cameras/common/exposure.py``, shared with the ASI imager and
tested on its own in ``test_asi_exposure``; what is tested here is this driver
driving it. One rule is this driver's alone and has its own tests below: the
guard runs in ``sun_cycle`` and nowhere else. On the ASI imager it also drives
``time`` mode; here ``time`` is the programme the standalone japan-camera
application ran, one frame per slot, and adding a second one to it would change
what an existing station's archive means overnight.

The sky is replaced by a flat frame of a known mean, so what the guard is
reacting to is exactly what the test asked for. Exposures are still slept
through, so slot timing stays honest.
"""
import sys
import threading

from pathlib import Path
from time import sleep

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import japan_driver                            # noqa: E402
from cameras.japan import config as japan_config, devices   # noqa: E402

pytest.importorskip("astropy.io.fits")

from astropy.io import fits                                 # noqa: E402

BIAS = 600.0


def make_config(tmp_path, slots=None, **overrides):
    cfg = {
        "output_dir": str(tmp_path),
        "mode": "sun_cycle",
        "name": "HAMA1",
        "sun_max_angle": -10.0,
        # No darks: the pedestal comes from the config, so the test controls it.
        "dark_frames": 0,
        "dead_time": 0.2,
        "camera": {"backend": "sim", "binning": 8},
        "filter_wheel": {"port": "sim"},
        "location": {"name": "TORY", "lat": 51.81, "lon": 103.08,
                     "elevation": 658},
        # Three seconds of exposure against a fifth of a second of dead time.
        # That is the smallest slot this feature can work on at all: sub-frames
        # have to stay a second apart (the archive name resolves to a second),
        # so E + dt buys at most three of them.
        "schedule": slots or [
            {"delta": 0.0, "filter": 1, "exposure": 3.0, "binning": 8},
        ],
        "overexposure": {
            "enabled": True,
            "threshold": 1000.0,
            "max_splits": 4,
            "min_exposure": 0.01,
            "margin": 0.0,
            "bias": BIAS,
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


def install_sky(monkeypatch, worker, sky):
    """Replace the simulated sensor with a flat frame of a known mean.

    ``sky["scale"]`` is ADU per second of exposure above the bias, and the test
    may change it mid-run to make the sky brighten or fade.
    """
    def capture(self):
        exposure = self.current_exposure or 0.0
        sleep(exposure)
        value = min(BIAS + sky["scale"] * exposure, 65535.0)
        return np.full((64, 64), int(round(value)), dtype="<u2")

    monkeypatch.setattr(type(worker.cam.cam), "capture", capture)


@pytest.fixture
def prompt_anchor(monkeypatch):
    monkeypatch.setattr(japan_driver.japan_schedule, "next_minute_boundary",
                        lambda t: t)


def run_cycle(worker, seconds, angle=-20.0):
    worker._sun_angle_fn = lambda: (lambda when: angle)
    timer = threading.Timer(seconds, worker.request_stop)
    timer.start()
    try:
        worker._run_sun_cycle_mode()
    finally:
        timer.cancel()


def run_time_mode(worker, seconds):
    timer = threading.Timer(seconds, worker.request_stop)
    timer.start()
    try:
        worker._run_time_mode()
    finally:
        timer.cancel()


def frames(tmp_path):
    return sorted((p for p in tmp_path.rglob("*.fits")
                   if not p.name.endswith("_DARK.fits")),
                  key=lambda p: fits.getheader(p)["DATE-OBS"])


def headers(tmp_path):
    return [fits.getheader(p) for p in frames(tmp_path)]


def visits(tmp_path):
    """Frames grouped into completed slot visits: ``[[hdr], [hdr, hdr], …]``.

    A visit only counts once its last sub-frame is on disk. The stop request
    lands wherever it lands, so the final visit of a run is often cut short —
    counting that as a one-frame visit would read as the guard giving up.
    """
    grouped, current = [], []
    for header in headers(tmp_path):
        current.append(header)
        if header.get("SPLITIDX", 1) == header.get("SPLITNUM", 1):
            grouped.append(current)
            current = []
    return grouped


# ---------------------------------------------------------------------------
# Where the guard is allowed to run — this driver's own rule
# ---------------------------------------------------------------------------
def test_the_guard_is_off_in_time_mode(tmp_path):
    """``time`` is the standalone program's cycle, and stays one frame per slot."""
    conf = make_config(tmp_path, mode="time", t_start="20:00")
    assert conf.overexposure.enabled is False
    assert any("overexposure" in e and "sun_cycle" in e for e in conf.errors)


def test_time_mode_shoots_one_whole_frame_however_bright_the_sky(tmp_path,
                                                                 monkeypatch):
    """The config says so; this is the loop actually behaving that way."""
    conf = make_config(tmp_path, mode="time", t_start="20:00",
                       schedule_len=4.0, wait_for_enter=False, slots=[
                           {"delta": 0.0, "filter": 1, "exposure": 1.0,
                            "binning": 8}])
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 60000.0})   # wildly overexposed
    run_time_mode(worker, seconds=10.0)

    assert frames(tmp_path), "the run produced nothing"
    for header in headers(tmp_path):
        assert header["EXPTIME"] == 1.0
        assert "SPLITNUM" not in header


def test_the_guard_is_off_unless_asked_for(tmp_path, prompt_anchor, monkeypatch):
    conf = make_config(tmp_path, overexposure={"enabled": False})
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 60000.0})
    run_cycle(worker, seconds=8.0)

    assert frames(tmp_path), "the run produced nothing"
    for header in headers(tmp_path):
        assert header["EXPTIME"] == 3.0
        assert "SPLITNUM" not in header


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------
def test_a_bright_slot_is_split_on_its_next_visit_not_this_one(tmp_path,
                                                               prompt_anchor,
                                                               monkeypatch):
    """The guard reacts to a measurement; it cannot know before it has one."""
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 200.0})     # 1200 ADU at 3 s
    run_cycle(worker, seconds=9.0)

    slots = visits(tmp_path)
    assert len(slots) >= 2, "the run did not reach a second visit"
    assert len(slots[0]) == 1, "the first visit should be one whole frame"
    assert slots[0][0]["EXPTIME"] == 3.0
    assert len(slots[1]) == 2, "the second visit should have divided"


def test_the_sub_exposure_leaves_room_for_the_extra_save(tmp_path, prompt_anchor,
                                                         monkeypatch):
    """Two frames of E/2 would overrun; the guard takes (E + dt)/2 - dt."""
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 200.0})
    run_cycle(worker, seconds=9.0)

    divided = [v for v in visits(tmp_path) if len(v) == 2]
    assert divided, "nothing was divided"
    expected = (3.0 + 0.2) / 2 - 0.2                       # 1.4 s, not 1.5 s
    for header in divided[0]:
        assert header["EXPTIME"] == pytest.approx(expected)
        assert header["SPLITNUM"] == 2
    assert [h["SPLITIDX"] for h in divided[0]] == [1, 2]


def test_the_file_name_carries_the_sub_exposure(tmp_path, prompt_anchor,
                                                monkeypatch):
    """The archive is read by name; a name claiming E for an E/2 frame is a lie."""
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 200.0})
    run_cycle(worker, seconds=9.0)

    assert frames(tmp_path), "the run produced nothing"
    for path in frames(tmp_path):
        header = fits.getheader(path)
        claimed = int(path.name.split("_")[-1].removesuffix("ms.fits"))
        assert claimed == round(header["EXPTIME"] * 1000)


def test_the_split_escalates_while_the_slot_stays_bright(tmp_path, prompt_anchor,
                                                         monkeypatch):
    """One step per measurement, up to what the slot's timing allows."""
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 60000.0})
    run_cycle(worker, seconds=14.0)

    counts = [len(v) for v in visits(tmp_path)]
    assert counts, "the run produced nothing"
    assert max(counts) >= 3, f"the guard stopped escalating too early: {counts}"
    assert counts == sorted(counts), f"the split went backwards: {counts}"


def test_the_split_never_exceeds_what_the_slot_can_hold(tmp_path, prompt_anchor,
                                                        monkeypatch):
    """Three seconds against a fifth of a second of dead time buys three frames.

    Not four: a fourth would have to be shorter than the second that the archive
    file name resolves to, and two frames in one second overwrite each other.
    """
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 60000.0})
    run_cycle(worker, seconds=16.0)

    assert max(len(v) for v in visits(tmp_path)) <= 3


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------
def test_a_slot_that_darkens_returns_to_a_single_frame(tmp_path, prompt_anchor,
                                                       monkeypatch):
    """Dawn in reverse: the guard has to give the whole exposure back."""
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    sky = {"scale": 200.0}
    install_sky(monkeypatch, worker, sky)
    threading.Timer(7.0, lambda: sky.update(scale=1.0)).start()
    run_cycle(worker, seconds=18.0)

    counts = [len(v) for v in visits(tmp_path)]
    assert max(counts) >= 2, f"the slot never divided: {counts}"
    assert counts[-1] == 1, f"the slot never came back to one frame: {counts}"


def test_a_successful_split_does_not_undo_itself(tmp_path, prompt_anchor,
                                                 monkeypatch):
    """The trap in every naive version of this loop.

    A split that works produces sub-frames *under* the threshold, which read as
    "not overexposed" — so a guard judging the release on the same number it
    judged the escalation on would release immediately and oscillate for ever.
    The release is judged on the mean extrapolated back to the longer exposure.
    """
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 400.0})     # 2200 ADU at 3 s,
    run_cycle(worker, seconds=16.0)                        # 1160 ADU at 1.4 s

    counts = [len(v) for v in visits(tmp_path)]
    divided = [i for i, n in enumerate(counts) if n > 1]
    assert divided, f"the slot never divided: {counts}"
    after = counts[divided[0]:]
    assert all(n > 1 for n in after), f"the split undid itself: {counts}"


# ---------------------------------------------------------------------------
# Per-slot state and the archive
# ---------------------------------------------------------------------------
def test_only_the_bright_slot_is_divided(tmp_path, prompt_anchor, monkeypatch):
    """Two filters, one over the threshold: the other keeps its whole frame."""
    # An explicit cycle length, so the wrap from the last slot back to the first
    # keeps a second of slack: derived from the slots it would be exactly one
    # exposure long, and any jitter would cost a slot.
    conf = make_config(tmp_path, schedule_len=7.0, slots=[
        {"delta": 0.0, "filter": 1, "exposure": 2.0, "binning": 8},
        {"delta": 3.0, "filter": 3, "exposure": 2.0, "binning": 8},
    ])
    worker = make_worker(tmp_path, conf)

    def capture(self):
        exposure = self.current_exposure or 0.0
        sleep(exposure)
        scale = 800.0 if worker.cam.current_filter == 1 else 50.0
        return np.full((64, 64), int(BIAS + scale * exposure), dtype="<u2")

    monkeypatch.setattr(type(worker.cam.cam), "capture", capture)
    run_cycle(worker, seconds=24.0)

    by_filter = {}
    for visit in visits(tmp_path):
        by_filter.setdefault(visit[0]["FILTER"], []).append(len(visit))
    assert len(by_filter[1]) >= 2, f"the bright slot was visited once: {by_filter}"
    assert max(by_filter[1]) >= 2, "the bright slot was never divided"
    assert set(by_filter[3]) == {1}, "the dim slot should not have been divided"


def test_sub_frames_never_collide_on_a_file_name(tmp_path, prompt_anchor,
                                                 monkeypatch):
    """The archive name resolves to a second; sub-frames can be closer than that.

    Nothing may be lost to that: every frame taken has to reach the archive, and
    the sequence counter has to agree with what is on disk.
    """
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 60000.0})
    run_cycle(worker, seconds=14.0)

    written = list(tmp_path.rglob("*.fits"))
    assert written, "the run produced nothing"
    assert worker._errors == 0, "frames were lost to write errors"
    assert len(written) == len({p.name for p in written}), "duplicate names"
    assert int((tmp_path / "seqno.txt").read_text()) == len(written)


def test_every_frame_records_what_it_measured(tmp_path, prompt_anchor,
                                              monkeypatch):
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 200.0})
    run_cycle(worker, seconds=9.0)

    assert headers(tmp_path), "the run produced nothing"
    for header in headers(tmp_path):
        assert header["SKYMEAN"] == pytest.approx(
            BIAS + 200.0 * header["EXPTIME"], abs=1.5)


def test_a_failed_sub_frame_does_not_cost_the_whole_slot(tmp_path, prompt_anchor,
                                                         monkeypatch):
    """One bad read inside a split must not push the run towards its error limit."""
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 200.0})
    good = type(worker.cam.cam).capture
    state = {"fail_next": False}

    def flaky(self):
        if state["fail_next"]:
            state["fail_next"] = False
            raise RuntimeError("simulated read failure")
        return good(self)

    monkeypatch.setattr(type(worker.cam.cam), "capture", flaky)
    # Break the first sub-frame of the visit that follows the first split.
    threading.Timer(7.0, lambda: state.update(fail_next=True)).start()
    run_cycle(worker, seconds=13.0)

    assert worker._errors >= 1, "the simulated failure never happened"
    assert frames(tmp_path), "the run produced nothing"
    assert not worker._stop_event.is_set() or worker._errors < 5, (
        "one bad sub-frame ended the run")


# ---------------------------------------------------------------------------
# Configuration errors switch the guard off rather than guessing
# ---------------------------------------------------------------------------
def test_a_threshold_above_the_bit_depth_is_reported(tmp_path):
    conf = make_config(tmp_path, overexposure={"enabled": True,
                                               "threshold": 70000.0})
    assert conf.overexposure.enabled is False
    assert any("threshold" in e for e in conf.errors)


def test_a_cap_below_two_is_reported(tmp_path):
    conf = make_config(tmp_path, overexposure={"enabled": True,
                                               "max_splits": 1})
    assert conf.overexposure.enabled is False
    assert any("max_splits" in e for e in conf.errors)


def test_a_threshold_under_the_preflight_target_is_reported(tmp_path):
    """The two loops must not be asked to fight over the same frame."""
    conf = make_config(
        tmp_path,
        preflight={"enabled": True, "sun_start_angle": -6.0,
                   "target_mean": 20000.0},
        overexposure={"enabled": True, "threshold": 15000.0})
    assert any("work against each other" in e for e in conf.errors)


def test_a_sub_frame_gap_under_the_name_resolution_is_raised(tmp_path):
    """Not negotiable: two frames in one second cannot both be filed."""
    conf = make_config(tmp_path, overexposure={"enabled": True,
                                               "min_frame_gap": 0.1})
    assert conf.overexposure.min_frame_gap == 1.0
    assert any("min_frame_gap" in e for e in conf.errors)
