"""Intensity control for the ASI driver — the arithmetic, with no camera.

Two feedback loops are tested here in isolation, because both fail in ways an
end-to-end run hides. The auto-exposure loop can converge on the wrong number
and still produce a plausible-looking archive; the split guard can oscillate
between one frame and two forever and still write files every slot. Both bugs
are invisible unless the arithmetic is pinned directly.

The recurring subtlety is the pedestal. A frame mean is ``bias + signal``, and
only the signal scales with exposure — so every prediction made here has to be
made on ``mean - bias``, and the tests say so explicitly.
"""
import sys

from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cameras.asi import config as asi_config           # noqa: E402
from cameras.asi import exposure as exp                # noqa: E402
from cameras.asi import schedule as sched              # noqa: E402


def entry(delta=0.0, filter_num=1, exposure=25.0, readout=None):
    return sched.Entry(filter=filter_num, exposure=exposure, delta=delta,
                       binning=1, readout=readout)


def preflight_cfg(**overrides):
    cfg = asi_config.PreflightCfg(enabled=True)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def overexposure_cfg(**overrides):
    cfg = asi_config.OverexposureCfg(enabled=True, margin=0.0, min_frame_gap=0.0)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


# ---------------------------------------------------------------------------
# Frame statistics
# ---------------------------------------------------------------------------
def test_the_mean_is_accumulated_wide_enough_for_a_bright_frame():
    """A megapixel of near-full-scale uint16 overflows anything narrower."""
    frame = np.full((1024, 1024), 60000, dtype="<u2")
    mean, saturated = exp.frame_stats(frame)
    assert mean == pytest.approx(60000.0)
    assert saturated == 0.0


def test_clipped_pixels_are_counted():
    frame = np.zeros((10, 10), dtype="<u2")
    frame[:5, :] = 65535
    mean, saturated = exp.frame_stats(frame)
    assert saturated == pytest.approx(0.5)


def test_a_missing_frame_has_no_statistics():
    assert exp.frame_stats(None) == (None, 0.0)


def test_a_slot_is_judged_by_its_brightest_sub_frame():
    """Averaging would hide the one sub-frame that is actually in trouble."""
    assert exp.combine_means([100.0, 500.0, 200.0]) == 500.0
    assert exp.combine_means([None, 300.0]) == 300.0
    assert exp.combine_means([]) is None


# ---------------------------------------------------------------------------
# Slot identity
# ---------------------------------------------------------------------------
def test_two_slots_on_the_same_filter_get_different_keys():
    assert exp.slot_key(entry(delta=0.0)) != exp.slot_key(entry(delta=60.0))


def test_the_key_survives_sorting_the_entries():
    """``next_cycle_slot`` hands back entries out of a sorted *copy*.

    Neither ``id()`` nor a position in the config list survives that, which is
    why the key is built from the values.
    """
    entries = [entry(delta=60.0, filter_num=2), entry(delta=0.0, filter_num=1)]
    keys = {exp.slot_key(e) for e in entries}
    reordered = sorted(entries, key=lambda e: e.delta)
    assert {exp.slot_key(e) for e in reordered} == keys


def test_the_key_is_hashable_where_the_entry_is_not():
    """Entry is a plain dataclass: comparable, and therefore unhashable."""
    with pytest.raises(TypeError):
        {entry(): 1}
    assert {exp.slot_key(entry()): 1}


# ---------------------------------------------------------------------------
# Pedestal arithmetic
# ---------------------------------------------------------------------------
def test_the_pedestal_is_not_scaled_when_extrapolating():
    """Doubling the exposure doubles the signal, not the pedestal."""
    predicted = exp.predicted_mean(1600.0, 10.0, 20.0, bias=600.0)
    assert predicted == pytest.approx(600.0 + 2 * 1000.0)


def test_ignoring_the_pedestal_would_overpredict():
    """The regression this guards: 2x the raw mean is not 2x the exposure."""
    naive = 1600.0 * 2
    assert exp.predicted_mean(1600.0, 10.0, 20.0, bias=600.0) < naive


def test_a_signal_below_the_pedestal_never_goes_negative():
    assert exp.signal_of(400.0, 600.0) == 0.0


# ---------------------------------------------------------------------------
# Splitting: the budget
# ---------------------------------------------------------------------------
def test_a_single_frame_keeps_the_scheduled_exposure():
    assert exp.sub_exposure(25.0, 5.0, 1) == 25.0
    assert exp.sub_exposure(25.0, 5.0, 1, margin=0.5) == 25.0


@pytest.mark.parametrize("splits", [2, 3, 4])
def test_the_sub_frames_and_their_saves_fit_one_frame_and_one_save(splits):
    """The whole point of the formula: N frames + N saves inside E + dt."""
    e = exp.sub_exposure(25.0, 5.0, splits)
    assert splits * (e + 5.0) == pytest.approx(25.0 + 5.0)


def test_the_sub_exposure_is_shorter_than_the_naive_division():
    """``E / N`` would overrun the slot by the extra saves it forgets."""
    assert exp.sub_exposure(25.0, 5.0, 2) == pytest.approx(10.0)
    assert exp.sub_exposure(25.0, 5.0, 2) < 25.0 / 2


def test_the_margin_comes_out_of_the_budget():
    e = exp.sub_exposure(25.0, 5.0, 2, margin=0.5)
    assert 2 * (e + 5.0) == pytest.approx(25.0 + 5.0 - 0.5)


# ---------------------------------------------------------------------------
# Splitting: how far a slot may divide
# ---------------------------------------------------------------------------
def test_the_frame_count_is_capped_by_the_minimum_sub_exposure():
    """E=10, dt=1, e_min=3: floor(11 / 4) = 2, not the configured 4."""
    assert exp.max_splits(10.0, 1.0, 3.0, cap=4) == 2


def test_the_frame_count_is_capped_by_the_configured_maximum():
    assert exp.max_splits(600.0, 1.0, 0.05, cap=3) == 3


def test_a_slot_swamped_by_its_dead_time_cannot_divide_at_all():
    assert exp.max_splits(1.0, 5.0, 1.0, cap=4) == 1


def test_the_minimum_frame_gap_keeps_sub_frames_out_of_one_second():
    """The archive name resolves to a second, so sub-frames must not share one."""
    packed = exp.max_splits(20.0, 0.1, 0.05, cap=100, min_gap=0.0)
    spaced = exp.max_splits(20.0, 0.1, 0.05, cap=100, min_gap=1.0)
    assert spaced < packed
    assert exp.sub_exposure(20.0, 0.1, spaced) + 0.1 >= 1.0


# ---------------------------------------------------------------------------
# Splitting: escalation and release
# ---------------------------------------------------------------------------
def test_an_unseen_slot_takes_one_whole_frame():
    guard = exp.SplitGuard(overexposure_cfg())
    assert guard.plan(entry(exposure=25.0), 5.0) == (1, 25.0)


def test_a_frame_over_the_threshold_escalates_one_step_at_a_time():
    """The requirement is 2, then 3, then 4 — never a jump straight to the cap."""
    guard = exp.SplitGuard(overexposure_cfg(threshold=50000.0))
    slot = entry(exposure=25.0)
    assert guard.update(slot, 5.0, 64000.0) == (1, 2)
    assert guard.update(slot, 5.0, 64000.0) == (2, 3)
    assert guard.update(slot, 5.0, 64000.0) == (3, 4)


def test_escalation_stops_at_the_cap():
    guard = exp.SplitGuard(overexposure_cfg(threshold=50000.0, max_splits=2))
    slot = entry(exposure=25.0)
    guard.update(slot, 5.0, 64000.0)
    assert guard.update(slot, 5.0, 64000.0) == (2, 2)


def test_a_successful_split_is_not_undone_by_its_own_sub_frame_mean():
    """The oscillation this design exists to avoid.

    After splitting, the sub-frame is below the threshold *by construction* —
    that is the split working. Reading that as "no longer overexposed" and going
    back to one frame would blow the next visit, and the slot would flap between
    one frame and two forever. Release is judged on the extrapolation, not on the
    raw measurement.
    """
    guard = exp.SplitGuard(overexposure_cfg(threshold=50000.0))
    slot = entry(exposure=25.0)
    guard.update(slot, 5.0, 64000.0)                  # -> 2 frames of 10 s
    # 40000 ADU at 10 s extrapolates to 100000 at the full 25 s: still hopeless.
    assert guard.update(slot, 5.0, 40000.0) == (2, 2)


def test_a_darkening_slot_returns_to_a_single_frame():
    guard = exp.SplitGuard(overexposure_cfg(threshold=50000.0))
    slot = entry(exposure=25.0)
    guard.update(slot, 5.0, 64000.0)                  # -> 2 frames of 10 s
    # 6000 ADU at 10 s extrapolates to 15000 at 25 s, well under the threshold.
    assert guard.update(slot, 5.0, 6000.0) == (2, 1)
    assert guard.plan(slot, 5.0) == (1, 25.0)


def test_an_intermediate_step_down_is_taken_when_a_whole_frame_would_still_saturate():
    """Coming down need not go all the way to one frame in a single visit."""
    guard = exp.SplitGuard(overexposure_cfg(threshold=50000.0, max_splits=4))
    slot = entry(exposure=25.0)
    for _ in range(3):
        guard.update(slot, 5.0, 64000.0)              # -> 4 frames of 2.5 s
    assert guard.splits_for(slot) == 4
    # 12000 ADU at 2.5 s -> 48000 at 10 s (2 frames): over the safe line.
    # -> 120000 at 25 s (1 frame): hopeless. So 3 frames of 5 s is the answer.
    old, new = guard.update(slot, 5.0, 12000.0)
    assert (old, new) == (4, 3)


def test_the_hysteresis_holds_a_borderline_slot_split():
    """A prediction that only just clears the threshold is not good enough."""
    guard = exp.SplitGuard(overexposure_cfg(threshold=50000.0, release=0.85))
    slot = entry(exposure=25.0)
    guard.update(slot, 5.0, 64000.0)                  # -> 2 frames of 10 s
    # 19800 at 10 s -> 49500 at 25 s: under the threshold, over 0.85 * threshold.
    assert guard.update(slot, 5.0, 19800.0) == (2, 2)


def test_the_release_uses_the_pedestal():
    """With a large pedestal the signal is smaller, so release comes sooner."""
    slot = entry(exposure=25.0)
    without = exp.SplitGuard(overexposure_cfg(threshold=50000.0))
    with_bias = exp.SplitGuard(overexposure_cfg(threshold=50000.0))
    without.update(slot, 5.0, 64000.0)
    with_bias.update(slot, 5.0, 64000.0)
    measured = 18000.0
    assert without.update(slot, 5.0, measured)[1] == 2
    assert with_bias.update(slot, 5.0, measured, bias=12000.0)[1] == 1


def test_a_slot_with_no_measurement_keeps_its_split():
    guard = exp.SplitGuard(overexposure_cfg(threshold=50000.0))
    slot = entry(exposure=25.0)
    guard.update(slot, 5.0, 64000.0)
    assert guard.update(slot, 5.0, None) == (2, 2)


def test_each_slot_keeps_its_own_split():
    guard = exp.SplitGuard(overexposure_cfg(threshold=50000.0))
    bright, dim = entry(delta=0.0, filter_num=1), entry(delta=60.0, filter_num=2)
    guard.update(bright, 5.0, 64000.0)
    assert guard.splits_for(bright) == 2
    assert guard.splits_for(dim) == 1


def test_a_slot_too_short_to_divide_is_remembered_as_impossible():
    guard = exp.SplitGuard(overexposure_cfg(threshold=50000.0))
    slot = entry(exposure=1.0)
    assert guard.update(slot, 5.0, 64000.0) == (1, 1)
    assert guard.impossible(slot)


# ---------------------------------------------------------------------------
# Preflight: the controller
# ---------------------------------------------------------------------------
def test_a_dim_frame_lengthens_the_exposure_towards_the_target():
    assert exp.next_exposure(1.0, 5000.0, 20000.0, max_step=8.0) == pytest.approx(4.0)


def test_a_bright_frame_shortens_the_exposure():
    assert exp.next_exposure(4.0, 40000.0, 20000.0) == pytest.approx(2.0)


def test_the_controller_works_on_the_signal_not_the_raw_mean():
    """With a 10000 ADU pedestal, 15000 -> 20000 is a doubling of the signal."""
    assert exp.next_exposure(1.0, 15000.0, 20000.0, bias=10000.0) == pytest.approx(2.0)


def test_a_measurement_inside_the_deadband_leaves_the_exposure_alone():
    """Steady exposures mean steady file names; small errors are not worth it."""
    assert exp.next_exposure(2.0, 21000.0, 20000.0, tolerance=0.15) == 2.0


def test_the_step_is_limited_to_the_maximum_factor():
    assert exp.next_exposure(1.0, 100.0, 20000.0, max_step=2.0) == pytest.approx(2.0)
    assert exp.next_exposure(1.0, 60000.0, 20000.0, max_step=2.0) == pytest.approx(0.5)


def test_a_saturated_frame_forces_the_largest_step_down():
    """The mean understates a clipped frame, so its ratio must not be believed."""
    result = exp.next_exposure(4.0, 30000.0, 20000.0, max_step=4.0,
                               saturated_fraction=0.2)
    assert result == pytest.approx(1.0)


def test_a_black_frame_opens_up_by_the_maximum_factor():
    assert exp.next_exposure(1.0, 0.0, 20000.0, max_step=4.0) == pytest.approx(4.0)


def test_the_exposure_never_falls_below_the_minimum():
    assert exp.next_exposure(0.1, 65000.0, 1000.0, min_exposure=0.05,
                             max_step=100.0) == 0.05


def test_the_exposure_never_exceeds_the_ceiling():
    assert exp.next_exposure(1.0, 100.0, 20000.0, max_step=100.0,
                             max_exposure=3.0) == 3.0


def test_a_failed_measurement_leaves_the_exposure_alone():
    assert exp.next_exposure(2.0, None, 20000.0) == 2.0


# ---------------------------------------------------------------------------
# Preflight: the per-slot state
# ---------------------------------------------------------------------------
def test_the_first_visit_starts_from_the_minimum_not_the_slot_exposure():
    """In twilight the scheduled exposure saturates by definition."""
    auto = exp.AutoExposure(preflight_cfg(min_exposure=0.05))
    assert auto.exposure_for(entry(exposure=55.0)) == 0.05


def test_the_exposure_is_never_longer_than_the_slot_asks_for():
    """The preflight stage shoots shorter than the programme, never longer."""
    auto = exp.AutoExposure(preflight_cfg(min_exposure=0.05, max_step=100.0))
    slot = entry(exposure=2.0)
    auto.update(slot, 0.05, 10.0)
    assert auto.exposure_for(slot) <= 2.0


def test_the_budget_narrows_the_ceiling_further():
    auto = exp.AutoExposure(preflight_cfg(min_exposure=0.05, max_step=100.0))
    slot = entry(exposure=55.0)
    auto.update(slot, 0.05, 1.0, budget=1.5)
    assert auto.exposure_for(slot, budget=1.5) <= 1.5


def test_each_slot_keeps_its_own_exposure():
    """Different filters see skies orders of magnitude apart."""
    auto = exp.AutoExposure(preflight_cfg(min_exposure=0.05))
    bright = entry(delta=0.0, filter_num=1, exposure=55.0)
    dim = entry(delta=60.0, filter_num=2, exposure=55.0)
    auto.update(bright, 1.0, 40000.0)
    auto.update(dim, 1.0, 5000.0)
    assert auto.exposure_for(bright) < auto.exposure_for(dim)


def test_the_loop_converges_on_the_target():
    """Simulated sky, linear in exposure over a pedestal, in a closed loop."""
    cfg = preflight_cfg(min_exposure=0.01, tolerance=0.02, max_step=4.0)
    auto = exp.AutoExposure(cfg)
    slot = entry(exposure=55.0)

    def sky(exposure):
        return 600.0 + 8000.0 * exposure

    current = auto.exposure_for(slot)
    for _ in range(12):
        _, current = auto.update(slot, current, sky(current), bias=600.0)
    assert sky(current) == pytest.approx(cfg.target_mean, rel=0.05)


def test_the_stage_forgets_its_exposures_when_it_hands_over():
    auto = exp.AutoExposure(preflight_cfg(min_exposure=0.05))
    slot = entry(exposure=55.0)
    auto.update(slot, 1.0, 5000.0)
    auto.reset()
    assert auto.exposure_for(slot) == 0.05


# ---------------------------------------------------------------------------
# Slot budget (schedule.py)
# ---------------------------------------------------------------------------
def test_the_slot_gap_runs_to_the_next_delta():
    entries = [entry(delta=0.0), entry(delta=60.0), entry(delta=200.0)]
    assert sched.slot_gap(entries, 300.0, entries[0]) == 60.0
    assert sched.slot_gap(entries, 300.0, entries[1]) == 140.0


def test_the_last_slots_gap_wraps_into_the_next_iteration():
    entries = [entry(delta=0.0), entry(delta=60.0)]
    assert sched.slot_gap(entries, 120.0, entries[1]) == 60.0


def test_the_budget_subtracts_the_entrys_own_readout():
    entries = [entry(delta=0.0, readout=7.0), entry(delta=60.0)]
    assert sched.slot_budget(entries, 120.0, entries[0], 5.0) == 53.0
    assert sched.slot_budget(entries, 120.0, entries[1], 5.0) == 55.0
