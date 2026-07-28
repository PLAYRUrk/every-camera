"""The monitor's tiles — everything on them is read from a status payload.

``monitor_app`` shows one card per camera found on the network. The wording
functions here are the whole of its rendering: given the JSON a worker
publishes, they decide what a person is told. They are pure, so they are tested
without a network, a camera or a window.
"""
import sys

from datetime import datetime as dt, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PyQt5", reason="the monitor is a Qt program")

import monitor_app                                             # noqa: E402


def ago(**kwargs):
    return (dt.now() - timedelta(**kwargs)).isoformat()


def rows_of(record):
    return dict(monitor_app.camera_rows(record))


# -- wording -----------------------------------------------------------------
@pytest.mark.parametrize("when,expected", [
    (None, "—"),
    ({"seconds": 12}, "12 s ago"),
    ({"minutes": 5}, "5 min ago"),
    ({"hours": 3}, "3.0 h ago"),
])
def test_ages_are_read_at_a_glance(when, expected):
    # Timed inside the test: an age computed at collection time would drift by
    # however long the rest of the suite takes.
    assert monitor_app.fmt_age(ago(**when) if when else None) == expected


def test_a_future_moment_counts_down_instead_of_up():
    soon = (dt.now() + timedelta(seconds=90)).isoformat()
    assert monitor_app.fmt_countdown(soon) == "in 1 min"
    assert monitor_app.fmt_countdown((dt.now()).isoformat()) == "now"


@pytest.mark.parametrize("value,expected", [
    (0, "home"), (3, "3"), (None, "unknown"),
])
def test_the_wheel_position_says_home_when_it_is_home(value, expected):
    # 0 is where the wheel parks itself on startup — a place, not a failure.
    assert monitor_app.fmt_filter(value) == expected


def test_a_connection_failure_is_shortened_to_its_cause():
    message = ("192.168.1.5:8765 unreachable: <urlopen error [Errno 111] "
               "Connection refused>")
    assert monitor_app.short_reason(message) == "connection refused"


def test_an_unrecognised_failure_is_still_shown():
    assert monitor_app.short_reason("timed out") == "timed out"
    assert monitor_app.short_reason("") == "not answering"


# -- what each camera type shows ---------------------------------------------
def test_an_asi_tile_shows_the_wheel_and_the_shutter():
    rows = rows_of({"camera_type": "asi", "status": "running", "mode": "sun",
                    "exposure": 30.0, "binning": 2, "filter": 0,
                    "shutter": True, "ccd_temp": -55.2, "set_temp": -55.0,
                    "temp_locked": True, "shots_taken": 12})
    assert rows["Exposure"] == "30 s · 2×2"
    assert rows["Filter"] == "home"
    assert rows["Shutter"] == "open"
    assert rows["Sensor"] == "-55.2 °C → -55 °C"
    assert rows["Schedule"] == "sun"


def test_a_settling_cooler_says_so():
    rows = rows_of({"camera_type": "asi", "ccd_temp": -30.0, "set_temp": -55.0,
                    "temp_locked": False})
    assert "settling" in rows["Sensor"]


def test_a_camera_in_setup_mode_is_not_credited_with_a_schedule():
    # It follows none: saying "sun" reads as though measurements were running.
    rows = rows_of({"camera_type": "asi", "mode": "sun", "setup_mode": True,
                    "phase": "setup"})
    assert "Schedule" not in rows
    assert rows["Phase"] == "setup"


def test_other_camera_types_show_their_own_settings():
    sptt = rows_of({"camera_type": "sptt", "exposure_s": 0.88, "gain": 100,
                    "frame_size": "2048x2048"})
    assert sptt["Exposure"] == "0.88 s" and sptt["Frame"] == "2048x2048"

    infra = rows_of({"camera_type": "infra", "exposure_us": 10_000, "gain": 1,
                     "roi": "1280x1024"})
    assert infra["Exposure"] == "10.0 ms" and infra["ROI"] == "1280x1024"

    cannon = rows_of({"camera_type": "cannon", "iso": "800",
                      "shutterspeed": "1/125", "aperture": "5.6"})
    assert cannon["Camera"] == "800 · 1/125 · 5.6"

    sentry = rows_of({"camera_type": "sentry", "daemon_running": False})
    assert sentry["imagerd_rt"] == "DOWN"


def test_a_camera_that_has_said_almost_nothing_still_renders():
    rows = rows_of({"camera_type": "unknown"})
    assert rows["Last frame"] == "—"


# -- the tile itself ---------------------------------------------------------
@pytest.fixture(scope="module")
def qt_app():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        try:
            app = QApplication([])
        except Exception as exc:
            pytest.skip(f"Qt will not start here: {exc}")
    return app


def lan_tile(qt_app, node=None):
    return monitor_app.CameraTile(
        (monitor_app.LAN, "192.168.1.5", 8765),
        node or {"instance_name": "ASI_42", "camera_type": "asi",
                 "node_name": "dome-north"})


def test_a_tile_can_hand_the_camera_to_the_other_programs(qt_app):
    tile = lan_tile(qt_app)
    tile.set_status({"status": "running", "supports_focus": True})
    assert tile.address == ("192.168.1.5", 8765)
    assert tile.btn_frames.isEnabled() and tile.btn_focus.isEnabled()


def test_a_camera_without_a_focus_mode_cannot_be_focused_from_here(qt_app):
    tile = lan_tile(qt_app)
    tile.set_status({"status": "running", "supports_focus": False})
    assert tile.btn_frames.isEnabled()
    assert not tile.btn_focus.isEnabled()


def test_an_mqtt_tile_offers_neither(qt_app):
    # There is no route to the camera — only a broker relaying what it says.
    tile = monitor_app.CameraTile((monitor_app.MQTT, "Sentry_far"),
                                  {"instance_name": "Sentry_far"})
    tile.set_status({"status": "running", "camera_type": "sentry"})
    assert tile.address is None
    assert not tile.btn_frames.isEnabled()
    assert not tile.btn_focus.isEnabled()


def test_an_offline_tile_says_why_and_stops_offering_anything(qt_app):
    tile = lan_tile(qt_app)
    tile.set_status({"status": "running"})
    tile.set_unreachable("192.168.1.5:8765 unreachable: <urlopen error "
                         "[Errno 111] Connection refused>")
    assert "connection refused" in tile.lbl_rows.text()
    assert not tile.btn_frames.isEnabled()


def test_a_status_value_cannot_break_the_tile_open(qt_app):
    # The label renders rich text, so an unescaped "<" swallowed the rest of
    # the card — which is how the offline message lost its own reason.
    tile = lan_tile(qt_app)
    tile.set_status({"status": "running", "phase": "<b>measuring</b>"})
    assert "&lt;b&gt;measuring" in tile.lbl_rows.text()
