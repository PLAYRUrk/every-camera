"""The overexposure guard: dividing a slot's frame when it comes back too bright.

Run end to end against the simulators, with the sky replaced by something whose
brightness the test controls exactly. The guard's failure modes are quiet ones —
a slot that flaps between one frame and two forever, a division that overruns its
slot, two sub-frames that collide on a file name and lose one of themselves — so
these tests check the numbers written into the archive rather than just that
files appeared.

The sky model is the real one: ``mean = bias + scale * exposure``. Only the
signal scales with exposure, which is exactly the property the guard's
extrapolation depends on.
"""
import sys
import threading

from pathlib import Path
from time import sleep

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras import asi_driver                              # noqa: E402
from cameras.asi import config as asi_config, devices       # noqa: E402

pytest.importorskip("astropy.io.fits")

from astropy.io import fits                                 # noqa: E402

BIAS = 600.0


def make_config(tmp_path, slots=None, **overrides):
    cfg = {
        "output_dir": str(tmp_path),
        "mode": "sun_cycle",
        "sun_max_angle": -10.0,
        # No darks: the pedestal comes from the config, so the test controls it.
        "dark_frames": 0,
        "dead_time": 0.1,
        "site_id": "TORY",
        "device_id": "ASI0",
        "camera": {"backend": "sim", "binning": 4},
        "filter_wheel": {"port": "sim"},
        "location": {"lat": 51.81, "lon": 103.08, "elevation": 658},
        # Three seconds of exposure against a fifth of a second of dead time.
        # That is the smallest slot this feature can work on at all: sub-frames
        # have to stay a second apart (the archive name resolves to a second),
        # so E + dt buys at most three of them.
        "schedule": slots or [
            {"delta": 0.0, "filter": 1, "exposure": 3.0, "binning": 4,
             "gain": 3, "readout": 0.2},
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
    return asi_config.from_dict(cfg)


def make_worker(tmp_path, conf):
    cam = asi_driver.AsiCamera(conf)
    cam.cam = devices.make_camera(conf)
    cam.wheel = devices.make_wheel(conf)
    return asi_driver.AsiWorkerConsole(
        cam=cam, cfg=conf, output_dir=str(tmp_path), instance_name="test",
        status_dir=str(tmp_path))


def install_sky(monkeypatch, worker, sky):
    """Replace the simulated sensor with a flat frame of a known mean.

    ``sky["scale"]`` is ADU per second of exposure above the bias, and the test
    may change it mid-run to make the sky brighten or fade. The exposure is
    still slept through, so slot timing stays honest.
    """
    def capture(self):
        exposure = self.current_exposure or 0.0
        sleep(exposure)
        value = min(BIAS + sky["scale"] * exposure, 65535.0)
        return np.full((64, 64), int(round(value)), dtype="<u2")

    monkeypatch.setattr(type(worker.cam.cam), "capture", capture)


@pytest.fixture
def prompt_anchor(monkeypatch):
    monkeypatch.setattr(asi_driver.asi_schedule, "next_minute_boundary",
                        lambda t: t)


def run_cycle(worker, seconds, angle=-20.0):
    worker._sun_angle_fn = lambda: (lambda when: angle)
    timer = threading.Timer(seconds, worker.request_stop)
    timer.start()
    try:
        worker._run_sun_cycle_mode()
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
# Off by default
# ---------------------------------------------------------------------------
def test_the_guard_is_off_unless_asked_for(tmp_path, prompt_anchor, monkeypatch):
    conf = make_config(tmp_path, overexposure={"enabled": False})
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 60000.0})   # wildly overexposed
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
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 200.0})
    run_cycle(worker, seconds=9.0)

    for path in frames(tmp_path):
        header = fits.getheader(path)
        claimed = int(path.name.split("_")[-1].removesuffix("ms.fits"))
        assert claimed == round(header["EXPTIME"] * 1000)


def test_the_split_escalates_while_the_slot_stays_bright(tmp_path, prompt_anchor,
                                                         monkeypatch):
    """One step per visit — 2, then 3, then 4 — as the requirement asks."""
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 800.0})     # 3000 ADU at 3 s
    run_cycle(worker, seconds=13.0)

    counts = [len(v) for v in visits(tmp_path)]
    assert counts[:2] == [1, 2], f"expected 1 then 2 frames, got {counts}"
    assert 3 in counts, f"the guard never escalated past two: {counts}"
    assert counts == sorted(counts), f"the split went backwards: {counts}"


def test_the_split_never_exceeds_the_cap(tmp_path, prompt_anchor, monkeypatch):
    conf = make_config(tmp_path, overexposure={"enabled": True,
                                               "threshold": 1000.0,
                                               "max_splits": 2,
                                               "min_exposure": 0.01,
                                               "margin": 0.0,
                                               "bias": BIAS})
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 60000.0})   # saturated outright
    run_cycle(worker, seconds=13.0)

    assert max(len(v) for v in visits(tmp_path)) == 2


def test_a_slot_too_short_to_divide_is_left_alone(tmp_path, prompt_anchor,
                                                  monkeypatch):
    """The dead time can swallow the whole budget; then there is nothing to do."""
    conf = make_config(tmp_path, slots=[
        {"delta": 0.0, "filter": 1, "exposure": 0.2, "binning": 4, "gain": 3,
         "readout": 1.0},
    ])
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 60000.0})
    run_cycle(worker, seconds=6.0)

    assert frames(tmp_path), "the run produced nothing"
    assert all(len(v) == 1 for v in visits(tmp_path))
    assert all(h["EXPTIME"] == 0.2 for h in headers(tmp_path))


def test_a_slot_too_short_to_divide_says_so_once(tmp_path, prompt_anchor,
                                                 monkeypatch):
    """Silence here would look like the guard simply not working."""
    import console_ui

    conf = make_config(tmp_path, slots=[
        {"delta": 0.0, "filter": 1, "exposure": 0.2, "binning": 4, "gain": 3,
         "readout": 1.0},
    ])
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 60000.0})

    warnings = []
    real_warn = console_ui.warn
    monkeypatch.setattr(console_ui, "warn",
                        lambda message: warnings.append(message))
    try:
        run_cycle(worker, seconds=8.0)
    finally:
        monkeypatch.setattr(console_ui, "warn", real_warn)

    said = [w for w in warnings if "no room to divide" in w]
    assert len(said) == 1, f"expected one warning, got {said}"


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------
def test_a_slot_that_darkens_returns_to_a_single_frame(tmp_path, prompt_anchor,
                                                       monkeypatch):
    """The requirement's other half: back to one whole frame when it is safe."""
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    sky = {"scale": 200.0}
    install_sky(monkeypatch, worker, sky)
    # Let the guard divide, then let the sky fade the way it does after dusk.
    fade = threading.Timer(8.0, lambda: sky.update(scale=60.0))
    fade.start()
    try:
        run_cycle(worker, seconds=17.0)
    finally:
        fade.cancel()

    counts = [len(v) for v in visits(tmp_path)]
    assert 2 in counts, f"the guard never divided: {counts}"
    assert counts[-1] == 1, f"the slot never came back to one frame: {counts}"
    assert visits(tmp_path)[-1][0]["EXPTIME"] == 3.0


def test_a_successful_split_does_not_undo_itself(tmp_path, prompt_anchor,
                                                 monkeypatch):
    """The oscillation guard, end to end.

    Once divided, the sub-frames read below the threshold — that is the split
    working. If the guard judged release on that raw number it would go back to
    one frame, over-expose, divide again, and flap forever.
    """
    conf = make_config(tmp_path)
    worker = make_worker(tmp_path, conf)
    install_sky(monkeypatch, worker, {"scale": 200.0})
    run_cycle(worker, seconds=17.0)

    counts = [len(v) for v in visits(tmp_path)]
    assert 2 in counts
    after = counts[counts.index(2):]
    assert all(c >= 2 for c in after), f"the split flapped: {counts}"


# ---------------------------------------------------------------------------
# Per-slot state and the archive
# ---------------------------------------------------------------------------
def test_only_the_bright_slot_is_divided(tmp_path, prompt_anchor, monkeypatch):
    """Two filters, one over the threshold: the other keeps its whole frame."""
    # An explicit cycle length, so the wrap from the last slot back to the first
    # keeps a second of slack: derived from the slots it would be exactly one
    # exposure long, and any jitter would cost a slot.
    conf = make_config(tmp_path, schedule_len=7.0, slots=[
        {"delta": 0.0, "filter": 1, "exposure": 2.0, "binning": 4, "gain": 3,
         "readout": 0.2},
        {"delta": 3.0, "filter": 3, "exposure": 2.0, "binning": 4, "gain": 3,
         "readout": 0.2},
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
    conf = make_config(
        tmp_path,
        preflight={"enabled": True, "sun_start_angle": -6.0,
                   "target_mean": 20000.0},
        overexposure={"enabled": True, "threshold": 15000.0})
    assert any("work against each other" in e for e in conf.errors)
