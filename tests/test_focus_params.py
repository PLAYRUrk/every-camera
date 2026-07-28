"""The parameter form focus_app builds from whatever the camera describes.

The form is generic: the camera sends a schema, the app renders it. The switch
(``"type": "bool"``) is what the ASI shutter is offered through, so these tests
cover the round trip — a state the camera reports becomes a tick, a tick becomes
a value to send — and the rule that only *changed* fields are sent, which is
what keeps an Apply from moving a shutter nobody touched.
"""
import os
import sys

from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PyQt5", reason="focus_app is a Qt program")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QCheckBox            # noqa: E402

from focus_app import ParamForm, as_bool                       # noqa: E402

SCHEMA = [
    {"name": "exposure", "label": "Exposure (s)", "type": "float",
     "min": 0.001, "max": 60.0, "step": 0.1},
    {"name": "shutter", "label": "Shutter", "type": "bool", "hint": "open"},
]


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance()
    if app is None:
        try:
            app = QApplication([])
        except Exception as exc:                # no display of any kind
            pytest.skip(f"Qt will not start here: {exc}")
    return app


@pytest.fixture
def form(qt_app):
    widget = ParamForm()
    widget.build(SCHEMA, {"exposure": 2.0, "shutter": False})
    return widget


def _checkbox(form):
    return form._widgets["shutter"][1]


# -- reading what the camera reported ---------------------------------------
@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), (1, True), (0, False),
    ("open", True), ("closed", False), ("nonsense", False),
])
def test_a_reported_state_is_read_the_way_the_camera_meant_it(value, expected):
    assert as_bool(value) is expected


# -- rendering ---------------------------------------------------------------
def test_a_switch_field_becomes_a_tick_box(form):
    assert isinstance(_checkbox(form), QCheckBox)


def test_the_box_starts_at_the_state_the_camera_reported(qt_app):
    form = ParamForm()
    form.build(SCHEMA, {"shutter": True})
    assert _checkbox(form).isChecked()


def test_an_unknown_state_is_shown_unticked_rather_than_guessed(qt_app):
    # The ASI wheel reports None until the shutter has been commanded.
    form = ParamForm()
    form.build(SCHEMA, {"shutter": None})
    assert not _checkbox(form).isChecked()


# -- what gets sent ----------------------------------------------------------
def test_an_untouched_form_sends_nothing(form):
    assert form.values() == {}


def test_ticking_the_box_sends_the_new_state(form):
    _checkbox(form).setChecked(True)
    assert form.values() == {"shutter": True}


def test_unticking_it_sends_the_closed_state_too(qt_app):
    # False is a value like any other here; a form that dropped it would leave
    # the shutter open with the box showing it shut.
    form = ParamForm()
    form.build(SCHEMA, {"exposure": 2.0, "shutter": True})
    _checkbox(form).setChecked(False)
    assert form.values() == {"shutter": False}


def test_editing_another_field_leaves_the_shutter_out_of_the_request(form):
    form._widgets["exposure"][1].setValue(5.0)
    assert form.values() == {"exposure": 5.0}


def test_reset_fields_puts_the_box_back_to_the_camera_state(form):
    _checkbox(form).setChecked(True)
    form._reset_fields()
    assert not _checkbox(form).isChecked()
    assert form.values() == {}
