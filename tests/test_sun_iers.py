"""A web server nobody can reach must not be able to stop a night.

astropy fetches Earth-rotation data (IERS) to convert UTC to UT1, and its
defaults are written for an interactive session: go to the network, and raise
when the table is stale and the download failed. On a station both halves are
wrong. The hosts it wants — ``datacenter.iers.org`` and
``maia.usno.navy.mil`` — are routinely unreachable from a site that otherwise
has a perfectly good connection, and the exception does not stay inside the sun
calculation: it comes out of ``sun_angle``, out of the mode loop, and stops the
run.

That is not a hypothetical. Uzur, 2026-09-25::

    [WARN ] failed to download https://datacenter.iers.org/data/9/finals2000A.all
    [ERROR] Measurement loop failed: interpolating from IERS_Auto using
            predictive values that are more than 30.0 days old
    [INFO ] Japan worker stopped — 0 light, 0 dark, 1 error(s)

Fifteen seconds of waiting, then a night lost — and it would have been every
night from then on, on both cameras, since they share this module.

What is pinned here is the settings, not astropy's behaviour under them: a test
that actually reaches the network would pass or fail depending on where it is
run, which is the property this whole module exists to remove.
"""
import importlib
import sys

from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("astropy")

from astropy.utils import iers                              # noqa: E402

from cameras.common import sun                              # noqa: E402


@pytest.fixture
def fresh_sun(monkeypatch):
    """A reimported module, with astropy's own defaults put back afterwards.

    ``configure_iers`` is once-per-process by design, and ``iers.conf`` is
    global, so a test that changed either without restoring it would decide the
    outcome of every test that ran after it.
    """
    before = (iers.conf.auto_download, iers.conf.auto_max_age,
              getattr(iers.conf, "iers_degraded_accuracy", None))
    module = importlib.reload(sun)
    try:
        yield module
    finally:
        iers.conf.auto_download = before[0]
        iers.conf.auto_max_age = before[1]
        if before[2] is not None:
            iers.conf.iers_degraded_accuracy = before[2]
        importlib.reload(sun)


# ---------------------------------------------------------------------------
# The default: the network is not involved at all
# ---------------------------------------------------------------------------
def test_the_sun_calculation_never_goes_to_the_network(fresh_sun, monkeypatch):
    """Ten seconds per URL, inside the measurement loop, is a late slot."""
    monkeypatch.delenv("EVERY_CAMERA_IERS_AUTO", raising=False)
    assert fresh_sun.configure_iers() is True
    assert iers.conf.auto_download is False


def test_a_stale_table_cannot_raise(fresh_sun, monkeypatch):
    """``auto_max_age`` is the setting that turns staleness into an exception.

    This is the whole bug: with it left at 30 days, a station stops observing
    one month after its astropy was installed, and the message it stops with
    talks about interpolation rather than about the night it just lost.
    """
    monkeypatch.delenv("EVERY_CAMERA_IERS_AUTO", raising=False)
    fresh_sun.configure_iers()
    assert iers.conf.auto_max_age is None


def test_a_solar_altitude_is_returned_for_a_date_past_the_bundled_table(
        fresh_sun, monkeypatch):
    """The end-to-end shape of it: a number, not a traceback.

    Any date far enough ahead is past whatever table shipped with the installed
    astropy, which is precisely the state every station reaches by simply
    continuing to run.
    """
    monkeypatch.delenv("EVERY_CAMERA_IERS_AUTO", raising=False)
    angle = fresh_sun.sun_angle(datetime(2030, 6, 1, 12, 0),
                                53.324236, 107.741264, 515.0)
    assert -90.0 <= angle <= 90.0


def test_the_altitude_is_still_right_to_well_inside_a_degree(fresh_sun,
                                                             monkeypatch):
    """What the default trades away, measured rather than asserted.

    Dropping UT1−UTC moves the sun by at most a leap second's worth of Earth
    rotation. The thresholds this feeds are written in whole degrees, so the
    test allows a hundredth of one — two orders of magnitude of headroom, and
    still tight enough to catch a genuinely wrong sun.
    """
    monkeypatch.delenv("EVERY_CAMERA_IERS_AUTO", raising=False)
    when = datetime(2026, 9, 25, 8, 57)
    offline = fresh_sun.sun_angle(when, 53.324236, 107.741264, 515.0)

    # The same instant one second later: the sun's own motion, which bounds
    # everything the missing correction could do.
    one_second = fresh_sun.sun_angle(when + timedelta(seconds=1),
                                     53.324236, 107.741264, 515.0)
    assert abs(offline - one_second) < 0.01


# ---------------------------------------------------------------------------
# The opt-in: try the network, but still never raise
# ---------------------------------------------------------------------------
def test_the_operator_can_ask_for_the_network_back(fresh_sun, monkeypatch):
    monkeypatch.setenv("EVERY_CAMERA_IERS_AUTO", "1")
    assert fresh_sun.configure_iers() is False
    assert iers.conf.auto_download is True


def test_asking_for_the_network_is_not_agreeing_to_lose_the_night(fresh_sun,
                                                                  monkeypatch):
    """The point of the opt-in is fresher data, not astropy's error handling.

    No setting of this program may leave a run in a state where an unreachable
    web server ends it, so ``auto_max_age`` stays off in both modes.
    """
    monkeypatch.setenv("EVERY_CAMERA_IERS_AUTO", "1")
    fresh_sun.configure_iers()
    assert iers.conf.auto_max_age is None


@pytest.mark.parametrize("value", ["0", "no", "off", "", "  "])
def test_only_an_affirmative_turns_the_network_on(fresh_sun, monkeypatch, value):
    """An unset-looking value must not quietly re-enable the stall."""
    monkeypatch.setenv("EVERY_CAMERA_IERS_AUTO", value)
    assert fresh_sun.configure_iers() is True
    assert iers.conf.auto_download is False


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
def test_configuring_twice_changes_nothing(fresh_sun, monkeypatch):
    """Called from every solar calculation; it has to be free after the first."""
    monkeypatch.delenv("EVERY_CAMERA_IERS_AUTO", raising=False)
    first = fresh_sun.configure_iers()
    # A later change of mind must not take effect mid-run: astropy has already
    # opened and cached a table by now, so the setting would be a lie.
    monkeypatch.setenv("EVERY_CAMERA_IERS_AUTO", "1")
    assert fresh_sun.configure_iers() == first
    assert iers.conf.auto_download is False


def test_it_says_which_mode_it_is_in_once(fresh_sun, monkeypatch):
    """A night log that went quiet for fifteen seconds should say why."""
    monkeypatch.delenv("EVERY_CAMERA_IERS_AUTO", raising=False)
    lines = []
    monkeypatch.setattr(fresh_sun.console_ui, "log", lines.append)
    fresh_sun.configure_iers()
    fresh_sun.configure_iers()
    assert len(lines) == 1
    assert "IERS" in lines[0]


def test_every_solar_calculation_configures_first(fresh_sun, monkeypatch):
    """``sun_angle`` is the only door into astropy here, so it is the one guard.

    astropy caches the table on first use, which makes ordering load-bearing:
    configured after the first calculation is configured too late.
    """
    monkeypatch.delenv("EVERY_CAMERA_IERS_AUTO", raising=False)
    calls = []
    real = fresh_sun.configure_iers
    monkeypatch.setattr(fresh_sun, "configure_iers",
                        lambda: (calls.append(1), real())[1])
    fresh_sun.sun_angle(datetime(2026, 9, 25, 8, 57), 53.3, 107.7, 515.0)
    assert calls, "sun_angle reached astropy without configuring IERS"


def test_the_schedule_generator_configures_it_too():
    """1440 calculations per night, and the same two web servers behind them."""
    source = (Path(__file__).resolve().parent.parent
              / "schedule_generator.py").read_text(encoding="utf-8")
    assert "configure_iers" in source
