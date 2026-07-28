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
    {"name": "shutter", "label": "Shutter", "type": "bool", "hint": "open",
     "true_label": "open", "false_label": "closed"},
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


# -- following the camera ----------------------------------------------------
def test_the_form_is_only_rebuilt_when_the_controls_themselves_change(form):
    assert form.matches_schema(SCHEMA)
    assert not form.matches_schema(SCHEMA[:1])


def test_an_untouched_field_follows_the_camera(form):
    # The schedule opened the shutter; nobody in the app asked for it.
    form.set_current({"exposure": 2.0, "shutter": True})
    assert _checkbox(form).isChecked()
    assert form.values() == {}          # following is not an edit to send


def test_a_field_being_edited_survives_the_refresh(form):
    _checkbox(form).setChecked(True)
    form.set_current({"exposure": 9.0, "shutter": False})
    assert _checkbox(form).isChecked()          # the operator's edit is kept
    assert form._widgets["exposure"][1].value() == 9.0   # the rest follows
    assert form.values() == {"shutter": True}


def test_an_edit_that_survived_is_marked_and_explained(form):
    _checkbox(form).setChecked(True)
    form.set_current({"shutter": False})
    label = form._labels["shutter"]
    assert label.text().endswith("*")
    assert "closed" in label.toolTip()           # what the camera says instead


def test_the_mark_goes_away_once_the_camera_has_the_value(form):
    _checkbox(form).setChecked(True)
    form.set_current({"shutter": True}, reset=True)   # the Apply went through
    assert form._labels["shutter"].text() == "Shutter"
    assert form.edited_fields() == []
    assert form.values() == {}


def test_a_reading_shown_differently_is_not_mistaken_for_an_edit(form):
    # The camera says 2, the spin box shows 2.000. Comparing the two as text
    # would call the field edited and freeze it there for good.
    form.set_current({"exposure": 2, "shutter": False})
    assert form.edited_fields() == []
    assert form.values() == {}
    form.set_current({"exposure": 3, "shutter": False})
    assert form._widgets["exposure"][1].value() == 3.0
