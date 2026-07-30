#!/usr/bin/env python3
"""
Every Camera — Focus Assistant (local network only).

A live view for adjusting a lens or telescope: it streams frames from a camera
as fast as that camera can supply them, lets you change exposure/gain/ROI/ISO
while watching, and scores sharpness in real time so you can tell which way to
turn the focuser.

Works with all four cameras. The editable controls come from the camera itself
(``GET /api/params``), so each camera offers exactly what it supports — and the
sentry camera, whose schedule belongs to imagerd_rt, is shown read-only.

Deliberately LAN-only: a live stream over a public MQTT broker would be far
too much traffic. Use ``viewer_app.py`` for remote work.

Two safeguards keep this from disturbing measurements:
  * The camera only free-runs while this program keeps asking it to. The
    request carries a deadline, so closing the window — or losing the network,
    or crashing — lets the camera fall back to schedule-only within a minute.
  * Scheduled captures always win. The camera skips focus frames around its
    capture seconds, so a measurement is never delayed.

Usage:
    python focus_app.py                     # discover cameras on the LAN
    python focus_app.py --host 192.168.1.5  # connect straight away
"""
import io
import os
import sys

from collections import deque

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QDoubleSpinBox, QCheckBox,
    QSpinBox, QGroupBox, QMessageBox, QSizePolicy, QStatusBar, QFileDialog,
    QScrollArea,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QPainter, QPen, QColor, QFont

from net_client import (
    CameraClient, CameraHttpError, TaskRunner, DiscoveryTask, MjpegStream,
    load_settings, save_settings, node_label, node_name_of,
)

DEFAULT_HTTP_PORT = 8765
HEARTBEAT_MS = 20_000        # keep the focus session alive
PARAMS_POLL_MS = 4_000       # re-read what the camera is actually set to
FOCUS_TTL_SECONDS = 60       # camera stops free-running this long after the last ping
SHARPNESS_HISTORY = 120
ROI_MIN = 32                 # below this the sharpness metric has nothing to chew on
ROI_STEP = 8                 # keep typed and wheeled sizes on a tidy grid
ROI_WHEEL_STEP = 1.25        # one wheel notch, as a factor on the side length
STRETCH_MODES = [
    ("minmax", "Auto (min–max)"),
    ("percentile", "Auto (0.5–99.5 %)"),
    ("raw", "Raw (no stretch)"),
]


TRUE_WORDS = {"1", "true", "yes", "on", "open", "opened"}


def _display_text(field, value):
    """A camera's value as the field words it — "2 — Medium", not "2"."""
    if value is None:
        return "nothing"
    if field.get("type") == "bool":
        return field.get("true_label" if as_bool(value) else "false_label",
                         "on" if as_bool(value) else "off")
    for choice in list(field.get("choices", [])) + list(field.get("states", [])):
        if str(choice.get("value")) == str(value):
            return str(choice.get("label", value))
    return str(value)


def as_bool(value):
    """Read an on/off value a camera reported. Unknown words mean off.

    The camera answers in JSON, so this is usually already a bool; a camera that
    words it ("open") must not be read as off, and a state the camera does not
    know is shown unticked rather than guessed at.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in TRUE_WORDS


# ---------------------------------------------------------------------------
# Live view with an optional measurement region
# ---------------------------------------------------------------------------
def crop_box(shape, size, center=None):
    """``(x0, y0, side)`` of a square kept wholly inside a frame of ``shape``.

    ``center`` is ``(fx, fy)`` as a *fraction* of the frame, so aiming survives a
    change of stream resolution unchanged; None means the middle. A square asked
    for near an edge is pushed back inside rather than clipped, so it keeps the
    requested size.
    """
    h, w = shape
    side = max(1, min(int(size), h, w))
    fx, fy = (0.5, 0.5) if center is None else center
    x0 = int(round(fx * w - side / 2))
    y0 = int(round(fy * h - side / 2))
    return max(0, min(x0, w - side)), max(0, min(y0, h - side)), side


class LiveView(QLabel):
    """Shows the stream with a movable, resizable square to measure sharpness in.

    The square is stored as a centre *fraction* plus a side in frame pixels: the
    fraction keeps the aim on the same patch of sky when the stream resolution
    changes, while the side stays a number the operator typed and can recognise.
    """

    region_changed = pyqtSignal(object)   # (x0, y0, x1, y1) in image coords, or None
    roi_changed = pyqtSignal(object)      # ((fx, fy), side_px) after a drag or wheel

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#0b0b0b; color:#777;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 240)
        self.setText("Not connected")
        self.setCursor(Qt.CrossCursor)
        self._pixmap = None
        self.roi_center = (0.5, 0.5)
        self.roi_side = 0          # frame pixels; 0 means "measure the whole frame"
        self.zoom_to_roi = False
        # What the widget currently shows: (ox, oy, target_w, target_h,
        # x0, y0, w, h), the last four in frame pixels. This is what turns a
        # click into a position in the frame, zoomed or not.
        self._view = None

    def set_frame(self, pixmap):
        self._pixmap = pixmap
        self.update()

    def clear_frame(self, message):
        self._pixmap = None
        self._view = None
        self.setText(message)
        self.update()

    # -- square ---------------------------------------------------------
    def frame_pixmap(self):
        """The newest frame, for anything else that wants to draw it."""
        return self._pixmap

    def frame_shape(self):
        """(h, w) of the frame currently displayed, or None."""
        if self._pixmap is None or not self._pixmap.width():
            return None
        return (self._pixmap.height(), self._pixmap.width())

    def roi_box(self):
        """The square in frame pixels as ``(x0, y0, side)``, or None if unset."""
        shape = self.frame_shape()
        if shape is None or self.roi_side <= 0:
            return None
        return crop_box(shape, self.roi_side, self.roi_center)

    @property
    def region(self):
        """The square as a normalised box, the form :meth:`_update_metrics` wants."""
        box = self.roi_box()
        if box is None:
            return None
        h, w = self.frame_shape()
        x0, y0, side = box
        return (x0 / w, y0 / h, (x0 + side) / w, (y0 + side) / h)

    def set_roi_side(self, side, notify=True):
        side = max(0, int(side))
        if side and side < ROI_MIN:
            side = ROI_MIN
        if side == self.roi_side:
            return
        self.roi_side = side
        self.update()
        if notify:
            self.region_changed.emit(self.region)

    def set_roi_center(self, fx, fy, notify=True):
        center = (min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0))
        if center == self.roi_center:
            return
        self.roi_center = center
        self.update()
        if notify:
            self.region_changed.emit(self.region)

    def set_zoom(self, enabled):
        self.zoom_to_roi = bool(enabled)
        self.update()

    def clear_region(self):
        """Go back to measuring the whole frame."""
        self.set_roi_side(0)
        self.roi_changed.emit((self.roi_center, 0))

    # -- painting ------------------------------------------------------
    def _target_rect(self, pw, ph):
        """Where an image of ``pw`` x ``ph`` lands inside this widget (letterboxed)."""
        if not pw or not ph:
            return None
        scale = min(self.width() / pw, self.height() / ph)
        w, h = max(1, int(pw * scale)), max(1, int(ph * scale))
        return ((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def paintEvent(self, event):
        if self._pixmap is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b0b0b"))

        full_h, full_w = self.frame_shape()
        box = self.roi_box()
        zoomed = self.zoom_to_roi and box is not None

        source = self._pixmap
        if zoomed:
            x0, y0, side = box
            source = self._pixmap.copy(x0, y0, side, side)

        rect = self._target_rect(source.width(), source.height())
        if rect is None:
            painter.end()
            return
        x, y, w, h = rect
        painter.drawPixmap(x, y, source.scaled(
            w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))

        if zoomed:
            x0, y0, side = box
            self._view = (x, y, w, h, x0, y0, side, side)
        else:
            self._view = (x, y, w, h, 0, 0, full_w, full_h)
            if box is not None:
                x0, y0, side = box
                painter.setPen(QPen(QColor("#ffd24a"), 2))
                painter.drawRect(int(x + x0 / full_w * w),
                                 int(y + y0 / full_h * h),
                                 int(side / full_w * w),
                                 int(side / full_h * h))
        painter.end()

    # -- aiming ---------------------------------------------------------
    def _aim_at(self, pos):
        """Point the square at a widget position (works while dragging, too)."""
        if self._view is None or self._pixmap is None:
            return
        ox, oy, target_w, target_h, x0, y0, w, h = self._view
        full_h, full_w = self.frame_shape()
        x = x0 + (pos.x() - ox) / max(target_w, 1) * w
        y = y0 + (pos.y() - oy) / max(target_h, 1) * h
        self.set_roi_center(x / full_w, y / full_h)
        self.roi_changed.emit((self.roi_center, self.roi_side))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._aim_at(event.pos())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._aim_at(event.pos())

    def wheelEvent(self, event):
        """Ctrl+wheel resizes the square; a plain wheel must not disturb the aim."""
        if not (event.modifiers() & Qt.ControlModifier) or self._pixmap is None:
            super().wheelEvent(event)
            return
        notches = event.angleDelta().y() / 120.0
        if not notches:
            return
        side = self.roi_side
        if side <= 0:                       # from the full frame, start at its short side
            side = min(self.frame_shape())
        side = int(round(side * (ROI_WHEEL_STEP ** notches) / ROI_STEP)) * ROI_STEP
        self.set_roi_side(max(ROI_MIN, side))
        self.roi_changed.emit((self.roi_center, self.roi_side))
        event.accept()


class RoiPreview(QLabel):
    """Shows just what the measurement square covers, beside the whole frame.

    Judging focus needs the pixels big; keeping an eye on framing needs the
    whole frame. Showing both at once means neither has to be given up, and the
    square can be dragged around the full view while its contents stay readable.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#0b0b0b; color:#777;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(180, 180)
        self.setText("square preview")
        self._pixmap = None
        self._box = None
        self.smooth = False

    def set_source(self, pixmap, box):
        """``box`` is ``(x0, y0, side)`` in frame pixels, or None for no square."""
        self._pixmap = pixmap
        self._box = box
        self.update()

    def set_smooth(self, enabled):
        self.smooth = bool(enabled)
        self.update()

    def paintEvent(self, event):
        if self._pixmap is None or self._box is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b0b0b"))
        x0, y0, side = self._box
        crop = self._pixmap.copy(x0, y0, side, side)
        # Nearest-neighbour by default: smoothing a magnified crop invents edges
        # that are exactly what the operator is trying to judge.
        mode = Qt.SmoothTransformation if self.smooth else Qt.FastTransformation
        scaled = crop.scaled(self.width(), self.height(),
                             Qt.KeepAspectRatio, mode)
        painter.drawPixmap((self.width() - scaled.width()) // 2,
                           (self.height() - scaled.height()) // 2, scaled)
        painter.setPen(QPen(QColor("#ffd24a"), 1))
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)
        painter.end()


# ---------------------------------------------------------------------------
# Sharpness trend plot
# ---------------------------------------------------------------------------
class SharpnessPlot(QWidget):
    """Rolling sharpness trace — turn the focuser until the line peaks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._values = deque(maxlen=SHARPNESS_HISTORY)
        self._best = 0.0

    def add(self, value):
        self._values.append(float(value))
        self._best = max(self._best, float(value))
        self.update()

    def reset(self):
        self._values.clear()
        self._best = 0.0
        self.update()

    @property
    def best(self):
        return self._best

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#141414"))
        w, h = self.width(), self.height()
        if len(self._values) < 2:
            painter.setPen(QColor("#666"))
            painter.drawText(self.rect(), Qt.AlignCenter, "waiting for frames…")
            painter.end()
            return

        peak = max(self._values) or 1.0
        # Scale to the best value seen this session, so a good focus visibly
        # sits near the top of the plot rather than drifting with the window.
        top = max(peak, self._best) or 1.0
        step = w / (len(self._values) - 1)

        painter.setPen(QPen(QColor("#2f6b45"), 1, Qt.DotLine))
        best_y = h - (self._best / top) * (h - 8) - 4
        painter.drawLine(0, int(best_y), w, int(best_y))

        painter.setPen(QPen(QColor("#3fd07a"), 2))
        points = [(i * step, h - (v / top) * (h - 8) - 4)
                  for i, v in enumerate(self._values)]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            painter.drawLine(int(x0), int(y0), int(x1), int(y1))

        painter.setPen(QColor("#888"))
        painter.setFont(QFont("", 8))
        painter.drawText(4, 12, f"best {self._best:,.0f}")
        painter.end()


# ---------------------------------------------------------------------------
# Parameter form built from the camera's own schema
# ---------------------------------------------------------------------------
class ParamForm(QGroupBox):
    """Editable camera parameters, described by the camera itself.

    The values shown are a *reading*, refreshed while the window is open: a
    camera changes its own settings between frames — a schedule moves the
    exposure, the filter, the ASI shutter — and the form has to follow, or the
    operator is looking at the state the camera had when they connected.
    Following must not fight the operator, so a field that has been edited and
    not yet applied keeps what was typed and is marked with a ``*``.
    """

    apply_requested = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__("Camera parameters", parent)
        self._widgets = {}
        self._labels = {}
        self._schema = []
        # Fields the operator has changed and not yet sent. Taken from the
        # widgets' own signals rather than inferred by comparing values: a
        # camera that reports nothing for a field (a wheel whose position is
        # unknown) would otherwise make the widget's default look like an
        # intention, and Apply would send a filter change nobody asked for.
        self._touched = set()
        self._updating = False      # True while the form writes to its widgets
        self._layout = QVBoxLayout(self)
        self._form = QFormLayout()
        self._layout.addLayout(self._form)
        self._hint = QLabel("Connect to a camera to see its controls.")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color:#888; font-size:11px;")
        self._layout.addWidget(self._hint)

        row = QHBoxLayout()
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._emit)
        row.addWidget(self.btn_apply)
        self.btn_revert = QPushButton("Reset fields")
        self.btn_revert.setEnabled(False)
        self.btn_revert.clicked.connect(self._reset_fields)
        row.addWidget(self.btn_revert)
        self._layout.addLayout(row)

        self._current = {}

    def matches_schema(self, schema):
        """True if ``schema`` is the one already on screen.

        The form is rebuilt only when the camera describes different controls;
        rebuilding it on every refresh would throw away an edit in progress and
        the keyboard focus with it.
        """
        return list(schema or []) == self._schema

    def build(self, schema, current, editable=True):
        while self._form.rowCount():
            self._form.removeRow(0)
        self._widgets.clear()
        self._labels.clear()
        self._touched.clear()
        self._schema = list(schema or [])
        self._current = dict(current or {})

        if not schema:
            self._hint.setText(
                "This camera does not accept live parameter changes.\n"
                "For the sentry camera the schedule lives in schedule.conf and "
                "only takes effect when imagerd_rt restarts.")
            self.btn_apply.setEnabled(False)
            self.btn_revert.setEnabled(False)
            return

        for field in schema:
            widget = self._make_widget(field)
            if widget is None:
                continue
            self._widgets[field["name"]] = (field, widget)
            self._form.addRow(field.get("label", field["name"]), widget)
            self._labels[field["name"]] = self._form.labelForField(widget)
            self._watch(field["name"], widget)

        self._reset_fields()
        self._hint.setText(
            "Change a value and press Apply — the camera applies it between "
            "frames. The values shown are re-read from the camera; a field "
            "marked * holds an edit that has not been sent yet."
            if editable else "Read-only camera. The values shown are re-read "
            "from the camera.")
        self.btn_apply.setEnabled(editable)
        self.btn_revert.setEnabled(editable)

    def _make_widget(self, field):
        widget = self._build_widget(field)
        if widget is not None and field.get("tooltip"):
            widget.setToolTip(field["tooltip"])
        return widget

    def _build_widget(self, field):
        kind = field.get("type", "text")
        if kind == "choice":
            combo = QComboBox()
            for choice in field.get("choices", []):
                combo.addItem(str(choice.get("label", choice.get("value"))),
                              choice.get("value"))
            # States a camera can report but nobody can ask for — a filter
            # wheel at home is where it parks itself, not somewhere to send it.
            # Listed so the reading can be shown honestly, and greyed out so it
            # cannot be picked.
            for state in field.get("states", []):
                combo.addItem(str(state.get("label", state.get("value"))),
                              state.get("value"))
                item = combo.model().item(combo.count() - 1)
                if item is not None:
                    item.setEnabled(False)
            return combo
        if kind == "bool":
            # A shutter, a cooler — things that are simply on or off. The hint is
            # the caption next to the box ("open"), the label says what it is.
            return QCheckBox(field.get("hint", ""))
        if kind == "int":
            spin = QSpinBox()
            spin.setRange(int(field.get("min", 0)), int(field.get("max", 1_000_000)))
            return spin
        if kind == "float":
            spin = QDoubleSpinBox()
            spin.setDecimals(3)
            spin.setRange(float(field.get("min", 0.0)),
                          float(field.get("max", 1e9)))
            spin.setSingleStep(float(field.get("step", 1.0)))
            return spin
        line = QLineEdit()
        line.setPlaceholderText(field.get("hint", ""))
        return line

    def _watch(self, name, widget):
        """Notice when the operator changes this field, and only then."""
        for signal in ("currentIndexChanged", "toggled", "valueChanged",
                       "textEdited"):
            if hasattr(widget, signal):
                getattr(widget, signal).connect(
                    lambda *_a, _n=name: self._on_user_change(_n))

    def _on_user_change(self, name):
        if self._updating:
            return              # the form filling itself in is not an edit
        self._touched.add(name)
        self._mark_edited()

    def set_current(self, current, reset=False):
        """Show a fresh reading from the camera.

        Fields the operator has not touched follow the camera. One that is
        being edited keeps the edit — the operator is mid-thought and an Apply
        must still send what they typed — and is marked instead. ``reset``
        takes the camera's word for everything, which is what an applied change
        or the Reset button means.
        """
        self._current = dict(current or {})
        for name in self._widgets:
            if name not in self._current:
                continue
            value = self._current[name]
            if value is None:
                # The camera is not saying — a wheel whose move never confirmed
                # knows no position. Leaving the last filter on screen would
                # claim knowledge nobody has.
                if reset or not self.is_edited(name):
                    self._show_nothing(name)
                continue
            if self._widget_shows(name, value):
                # The camera has caught up with the edit — it *is* the reading
                # now, so the field is no longer pending. Without this the star
                # never went out after a successful Apply.
                self._touched.discard(name)
            elif reset or not self.is_edited(name):
                self._show_value(name, value)
        self._mark_edited()

    def is_edited(self, name):
        """True if the operator has changed this field and not yet sent it."""
        return name in self._touched

    def edited_fields(self):
        return [name for name in self._widgets if self.is_edited(name)]

    def _reset_fields(self):
        self.set_current(self._current, reset=True)

    def _widget_shows(self, name, value):
        """True if the widget already holds ``value``, as it would hold it.

        Compared against the *rendered* value, never the raw reading: a camera
        reporting 2 and a spin box showing 2.000 are the same setting, and a
        camera reporting a filter the wheel is between is no setting at all.
        """
        rendered = self._rendered(name, value)
        return rendered is not None and rendered == self._widget_value(name)

    def _rendered(self, name, value):
        """``value`` as this widget would hold it, or None if it cannot hold it."""
        _field, widget = self._widgets[name]
        if isinstance(widget, QComboBox):
            index = widget.findData(value)
            if index < 0:
                index = widget.findText(str(value))
            if index < 0:
                return None             # not one of the choices on offer
            data = widget.itemData(index)
            return widget.itemText(index) if data is None else data
        if isinstance(widget, QCheckBox):
            return as_bool(value)
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            try:
                number = (int(value) if isinstance(widget, QSpinBox)
                          else round(float(value), widget.decimals()))
            except (TypeError, ValueError):
                return None
            # setValue clamps and rounds; predicting it is what keeps a value
            # outside the field's range from looking like an operator's edit.
            return max(widget.minimum(), min(widget.maximum(), number))
        return str(value).strip()

    def _show_nothing(self, name):
        """Show that the camera reports no value here, where the widget can.

        Only a combo box has a way to say it: no selection at all. A number
        field has no blank state, so it keeps its last reading rather than
        pretend to a zero the camera never mentioned.
        """
        _field, widget = self._widgets[name]
        if not isinstance(widget, QComboBox):
            return
        self._updating = True
        try:
            widget.setCurrentIndex(-1)
        finally:
            self._updating = False
        self._touched.discard(name)

    def _show_value(self, name, value):
        """Put a value from the camera into its widget, without calling it an edit."""
        rendered = self._rendered(name, value)
        if rendered is None:
            return
        _field, widget = self._widgets[name]
        self._updating = True
        try:
            if isinstance(widget, QComboBox):
                index = widget.findData(rendered)
                widget.setCurrentIndex(index if index >= 0
                                       else widget.findText(str(rendered)))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(rendered)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.setValue(rendered)
            else:
                widget.setText(rendered)
        finally:
            self._updating = False
        self._touched.discard(name)

    def _widget_value(self, name):
        """What the widget holds now, in the form it would be sent in."""
        _field, widget = self._widgets[name]
        if isinstance(widget, QComboBox):
            value = widget.currentData()
            return widget.currentText() if value is None else value
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return widget.value()
        return widget.text().strip()

    def _mark_edited(self):
        """Star the rows holding an unapplied edit, and say what the camera says."""
        for name, (field, widget) in self._widgets.items():
            label = self._labels.get(name)
            if label is None:
                continue
            title = field.get("label", name)
            if self.is_edited(name):
                label.setText(f"{title} *")
                label.setToolTip("Not applied yet. The camera reports "
                                 f"{_display_text(field, self._current.get(name))}.")
            else:
                label.setText(title)
                label.setToolTip("")

    def values(self):
        """Only what the operator changed — never a widget's own default."""
        out = {}
        for name in self._touched:
            _field, widget = self._widgets[name]
            value = self._widget_value(name)
            if isinstance(widget, QLineEdit) and not value:
                continue        # a cleared box means "leave it alone"
            out[name] = value
        return out

    def _emit(self):
        values = self.values()
        if values:
            self.apply_requested.emit(values)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class FocusWindow(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.setWindowTitle("Every Camera — Focus Assistant")
        self.resize(1180, 780)
        self.setMinimumSize(820, 560)

        self._settings = settings
        self._tasks = TaskRunner(self)
        self._client = None
        self._stream = None
        self._discovery = None
        self._info = {}
        self._last_jpeg = None
        self._frames = 0
        self._frame_shape = None

        self._build_ui()

        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(HEARTBEAT_MS)
        self._heartbeat.timeout.connect(self._send_heartbeat)

        self._metrics = QTimer(self)
        self._metrics.setInterval(1000)
        self._metrics.timeout.connect(self._update_metrics)

        # The camera changes its own settings between frames, so the form is
        # re-read rather than filled in once at connect.
        self._params_poll = QTimer(self)
        self._params_poll.setInterval(PARAMS_POLL_MS)
        self._params_poll.timeout.connect(self._poll_params)
        self._params_inflight = False

    # -- UI ------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        # ---- connection bar
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Camera:"))
        self.cmb_cameras = QComboBox()
        self.cmb_cameras.setMinimumWidth(280)
        bar.addWidget(self.cmb_cameras)
        self.btn_discover = QPushButton("Search LAN")
        self.btn_discover.clicked.connect(self.discover)
        bar.addWidget(self.btn_discover)
        self.le_manual = QLineEdit()
        self.le_manual.setPlaceholderText("host or host:port")
        self.le_manual.setMaximumWidth(180)
        self.le_manual.returnPressed.connect(self._on_manual)
        bar.addWidget(self.le_manual)
        self.btn_start = QPushButton("Start live view")
        self.btn_start.clicked.connect(self._on_start)
        bar.addWidget(self.btn_start)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        bar.addWidget(self.btn_stop)
        bar.addStretch()
        root.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # ---- left: live view + sharpness
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        # Whole frame on the left, the square's contents on the right, so the
        # operator can aim and judge sharpness without switching between them.
        views = QSplitter(Qt.Horizontal)
        self.view = LiveView()
        self.view.region_changed.connect(self._on_region_changed)
        self.view.roi_changed.connect(self._on_roi_changed)
        views.addWidget(self.view)

        preview_panel = QWidget()
        preview_lay = QVBoxLayout(preview_panel)
        preview_lay.setContentsMargins(0, 0, 0, 0)
        preview_lay.setSpacing(2)
        self.lbl_preview = QLabel("Square — whole frame")
        self.lbl_preview.setStyleSheet("color:#888; font-size:11px;")
        preview_lay.addWidget(self.lbl_preview)
        self.roi_preview = RoiPreview()
        preview_lay.addWidget(self.roi_preview, 1)
        views.addWidget(preview_panel)
        views.setStretchFactor(0, 3)
        views.setStretchFactor(1, 2)
        views.setSizes([520, 300])
        left_lay.addWidget(views, 1)

        metrics = QGroupBox("Sharpness (turn the focuser until this peaks)")
        m_lay = QVBoxLayout(metrics)
        self.lbl_sharpness = QLabel("—")
        font = QFont()
        font.setPointSize(20)
        font.setBold(True)
        self.lbl_sharpness.setFont(font)
        self.lbl_sharpness.setAlignment(Qt.AlignCenter)
        m_lay.addWidget(self.lbl_sharpness)
        self.plot = SharpnessPlot()
        m_lay.addWidget(self.plot)
        row = QHBoxLayout()
        self.lbl_exposure = QLabel("—")
        self.lbl_exposure.setStyleSheet("font-size:11px;")
        row.addWidget(self.lbl_exposure, 1)
        btn_reset = QPushButton("Reset trace")
        btn_reset.clicked.connect(self._reset_metrics)
        row.addWidget(btn_reset)
        btn_region = QPushButton("Whole frame")
        btn_region.setToolTip("Measure the whole frame again")
        btn_region.clicked.connect(self.view.clear_region)
        row.addWidget(btn_region)
        m_lay.addLayout(row)
        left_lay.addWidget(metrics)
        splitter.addWidget(left)

        # ---- right: controls
        # In a scroll area because the number of camera parameters is decided by
        # the camera, not by us: an ASI with five fields on a short screen used
        # to squash the form until the labels overlapped.
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QScrollArea.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 6, 0)

        info_box = QGroupBox("Connection")
        info_lay = QVBoxLayout(info_box)
        self.lbl_camera = QLabel("not connected")
        self.lbl_camera.setWordWrap(True)
        info_lay.addWidget(self.lbl_camera)
        self.lbl_stream = QLabel("stream: idle")
        self.lbl_stream.setStyleSheet("color:#888; font-size:11px;")
        info_lay.addWidget(self.lbl_stream)
        # Confirmation from the camera that it really did stand down, rather
        # than an assumption that asking was enough.
        self.lbl_hold = QLabel("")
        self.lbl_hold.setWordWrap(True)
        self.lbl_hold.setStyleSheet("color:#c87000; font-size:11px;")
        info_lay.addWidget(self.lbl_hold)
        right_lay.addWidget(info_box)

        view_box = QGroupBox("Display")
        view_grid = QGridLayout(view_box)
        view_grid.addWidget(QLabel("Contrast:"), 0, 0)
        self.cmb_stretch = QComboBox()
        for value, label in STRETCH_MODES:
            self.cmb_stretch.addItem(label, value)
        self.cmb_stretch.currentIndexChanged.connect(self._restart_stream_if_running)
        view_grid.addWidget(self.cmb_stretch, 0, 1)
        view_grid.addWidget(QLabel("Frame rate:"), 1, 0)
        self.sb_fps = QSpinBox()
        self.sb_fps.setRange(1, 30)
        self.sb_fps.setValue(10)
        self.sb_fps.setSuffix(" fps")
        self.sb_fps.valueChanged.connect(self._restart_stream_if_running)
        view_grid.addWidget(self.sb_fps, 1, 1)
        view_grid.addWidget(QLabel("Max size:"), 2, 0)
        self.cmb_size = QComboBox()
        for label, value in [("Full", 0), ("1024 px", 1024), ("768 px", 768),
                             ("512 px", 512)]:
            self.cmb_size.addItem(label, value)
        self.cmb_size.setCurrentIndex(1)
        self.cmb_size.setToolTip(
            "Downscaling on the camera side keeps a slow link usable")
        self.cmb_size.currentIndexChanged.connect(self._restart_stream_if_running)
        view_grid.addWidget(self.cmb_size, 2, 1)
        right_lay.addWidget(view_box)

        # ---- the measured square
        roi_box = QGroupBox("Measurement square")
        roi_grid = QGridLayout(roi_box)
        roi_grid.addWidget(QLabel("Side:"), 0, 0)
        self.sb_roi = QSpinBox()
        self.sb_roi.setRange(0, 8192)
        self.sb_roi.setSingleStep(ROI_STEP)
        self.sb_roi.setSuffix(" px")
        self.sb_roi.setSpecialValueText("whole frame")
        self.sb_roi.setToolTip("Any size, in pixels of the streamed frame. "
                               "0 measures everything.")
        self.sb_roi.valueChanged.connect(self._on_roi_side_typed)
        roi_grid.addWidget(self.sb_roi, 0, 1, 1, 2)

        roi_grid.addWidget(QLabel("Centre:"), 1, 0)
        self.sb_roi_x = QSpinBox()
        self.sb_roi_y = QSpinBox()
        for box in (self.sb_roi_x, self.sb_roi_y):
            box.setRange(0, 8192)
            box.setSuffix(" px")
            box.valueChanged.connect(self._on_roi_center_typed)
        roi_grid.addWidget(self.sb_roi_x, 1, 1)
        roi_grid.addWidget(self.sb_roi_y, 1, 2)

        btn_centre = QPushButton("Centre")
        btn_centre.setToolTip("Put the square back in the middle of the frame")
        btn_centre.clicked.connect(self._centre_roi)
        roi_grid.addWidget(btn_centre, 2, 1)
        self.chk_zoom = QCheckBox("Zoom main view too")
        self.chk_zoom.setToolTip("Also replace the whole-frame view with the "
                                 "square, when the framing no longer matters")
        self.chk_zoom.toggled.connect(self.view.set_zoom)
        roi_grid.addWidget(self.chk_zoom, 2, 2)

        self.chk_smooth = QCheckBox("Smooth the square preview")
        self.chk_smooth.setToolTip("Off by default: smoothing a magnified crop "
                                   "invents the very edges you are judging")
        self.chk_smooth.toggled.connect(self.roi_preview.set_smooth)
        roi_grid.addWidget(self.chk_smooth, 3, 0, 1, 3)

        hint = QLabel("Click or drag the image to aim · Ctrl+wheel resizes")
        hint.setStyleSheet("color:#888; font-size:11px;")
        hint.setWordWrap(True)
        roi_grid.addWidget(hint, 4, 0, 1, 3)
        right_lay.addWidget(roi_box)

        self.params = ParamForm()
        self.params.apply_requested.connect(self._on_apply_params)
        right_lay.addWidget(self.params)

        self.btn_save = QPushButton("Save current frame…")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._on_save)
        right_lay.addWidget(self.btn_save)
        right_lay.addStretch()
        right_scroll.setWidget(right)
        right_scroll.setMinimumWidth(360)
        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([820, 380])

        self.setStatusBar(QStatusBar())
        self._status("Ready — search the LAN or type a camera address")

    def _status(self, message):
        self.statusBar().showMessage(message, 15000)

    # -- camera selection ----------------------------------------------
    def discover(self):
        self.btn_discover.setEnabled(False)
        self._status("Searching the local network…")
        self._discovery = DiscoveryTask(timeout=2.0, parent=self)
        self._discovery.found.connect(self._on_discovered)
        self._discovery.finished.connect(
            lambda: self.btn_discover.setEnabled(True))
        self._discovery.start()

    def _on_discovered(self, nodes):
        self.cmb_cameras.clear()
        for node in sorted(nodes, key=lambda n: (node_name_of(n).lower(),
                                                 str(n.get("instance_name")))):
            host, port = node.get("host"), node.get("http_port", DEFAULT_HTTP_PORT)
            focus = "" if node.get("supports_focus", True) else "  [view only]"
            self.cmb_cameras.addItem(
                node_label(node, DEFAULT_HTTP_PORT) + focus, (host, port))
        for host, port in self._settings.get("recent_hosts", []):
            self.cmb_cameras.addItem(f"{host}:{port}", (host, port))
        if self.cmb_cameras.count() == 0:
            self.cmb_cameras.addItem("— no cameras found —", None)
            self._status("No cameras answered. Type host:port to connect directly.")
        else:
            self._status(f"Found {len(nodes)} camera(s)")

    def _on_manual(self):
        text = self.le_manual.text().strip()
        if not text:
            return
        host, _, port = text.partition(":")
        target = (host.strip(),
                  int(port) if port.strip().isdigit() else DEFAULT_HTTP_PORT)
        self.cmb_cameras.insertItem(0, f"{target[0]}:{target[1]}", target)
        self.cmb_cameras.setCurrentIndex(0)
        self._on_start()

    # -- streaming -----------------------------------------------------
    def _on_start(self):
        target = self.cmb_cameras.currentData()
        if not target:
            QMessageBox.information(self, "No camera",
                                    "Pick a camera or type its address first.")
            return
        host, port = target
        self._client = CameraClient(host, port)
        self._remember(host, port)
        self.lbl_camera.setText(f"connecting to {host}:{port}…")
        self._tasks.run(self._client.info, self._on_info, self._on_error)

    def _remember(self, host, port):
        recent = [tuple(x) for x in self._settings.get("recent_hosts", [])]
        entry = (host, int(port))
        recent = [entry] + [r for r in recent if r != entry]
        self._settings["recent_hosts"] = [list(r) for r in recent[:8]]
        save_settings(self._settings)

    def _on_info(self, info):
        self._info = info
        # A camera with no schedule is up purely to be focused. Saying so here
        # saves the operator wondering why nothing is being archived.
        setup = ('&nbsp;<span style="color:#c87000"><b>SETUP</b></span>'
                 if info.get("setup_mode") else "")
        self.lbl_camera.setText(
            f"<b>{info.get('instance_name', '?')}</b> "
            f"({info.get('camera_type', '?')}){setup}<br>{node_name_of(info)}")
        if info.get("setup_mode"):
            self._status(f"{info.get('instance_name')} is in setup mode: no "
                         f"schedule, nothing is being archived — focus freely.")
        if not info.get("supports_focus", True):
            self._status(
                f"{info.get('instance_name')} streams whatever its daemon "
                f"produces; it has no free-running focus mode.")
        if not self._agree_to_interrupt(info):
            self._client = None
            self.lbl_camera.setText("not connected")
            self._status("Left the camera measuring — nothing was interrupted.")
            return
        client = self._client
        self._tasks.run(client.params, self._on_params, self._on_error)
        self._start_stream()

    def _agree_to_interrupt(self, info):
        """Ask before taking a camera away from its measurements.

        Focusing pauses the schedule for as long as this window is open, which
        is what makes the tool usable — but on a camera that is measuring right
        now it costs frames, and that is the operator's call, not ours. A camera
        in setup mode or between its observing windows costs nothing, and asking
        there would only teach the operator to click through the question.
        """
        if not info.get("hold_supported", True):
            self._status(
                f"{info.get('instance_name')}'s schedule belongs to its "
                f"imagerd_rt daemon — focusing cannot pause it, so the live "
                f"view shows whatever the daemon is producing.")
            return True
        if info.get("setup_mode") or not info.get("schedule_active"):
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Interrupt the measurements?")
        box.setText(
            f"<b>{info.get('instance_name', 'This camera')}</b> is measuring on "
            f"its schedule right now.")
        box.setInformativeText(
            "Focusing pauses it until you stop the live view or close this "
            "window. The frames its schedule would have taken meanwhile are not "
            "made up afterwards.")
        # Spelled out rather than Yes/No: "yes" to a question about interrupting
        # is exactly the answer an operator misreads at three in the morning.
        proceed = box.addButton("Pause and focus", QMessageBox.AcceptRole)
        box.addButton("Leave it measuring", QMessageBox.RejectRole)
        box.setDefaultButton(proceed)
        box.exec_()
        return box.clickedButton() is proceed

    def _on_params(self, payload, reset=True):
        """Show what the camera reports. ``reset`` overrides unapplied edits."""
        schema = payload.get("schema", [])
        current = payload.get("current", {})
        if self.params.matches_schema(schema):
            self.params.set_current(current, reset=reset)
        else:
            self.params.build(schema, current,
                              editable=payload.get("editable", False))

    def _poll_params(self):
        """Re-read the camera's settings; it moves them without being asked.

        Only fields the operator is not editing follow — see
        :meth:`ParamForm.set_current`. A poll that fails is passed over in
        silence: the stream label already says when the camera is unreachable,
        and this runs every few seconds.
        """
        if not self._client or self._params_inflight:
            return
        client = self._client
        self._params_inflight = True

        def _done(payload):
            self._params_inflight = False
            self._on_params(payload, reset=False)

        def _failed(_message):
            self._params_inflight = False

        self._tasks.run(client.params, _done, _failed)

    def _start_stream(self):
        self._stop_stream()
        if not self._client:
            return
        self._reset_metrics()
        # The hold goes out before the stream does. The stream renews the focus
        # session on every frame it carries, so asking for the pause afterwards
        # would race with a renewal that does not know about it.
        self._send_heartbeat(hold=True)
        max_side = self.cmb_size.currentData() or None
        self._stream = MjpegStream(self._client, max_side=max_side,
                                   stretch=self.cmb_stretch.currentData(),
                                   fps=self.sb_fps.value(), parent=self)
        self._stream.frame.connect(self._on_frame)
        self._stream.state.connect(
            lambda s: self.lbl_stream.setText(f"stream: {s}"))
        self._stream.start()
        self._heartbeat.start()
        self._metrics.start()
        self._params_poll.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._status("Live view running — focus mode is on while this window streams")

    def _restart_stream_if_running(self):
        if self._stream is not None:
            self._start_stream()

    def _stop_stream(self):
        self._heartbeat.stop()
        self._metrics.stop()
        self._params_poll.stop()
        if self._stream is not None:
            self._stream.stop()
            self._stream.wait(3000)
            self._stream = None

    def _on_stop(self):
        self._stop_stream()
        # Tell the camera to leave focus mode now, rather than waiting out the
        # deadline, so it goes back to schedule-only immediately. The repeat is
        # not belt-and-braces: the server renews focus on every chunk it sends,
        # so a stop that arrives before it notices our disconnect gets undone.
        self._request_focus_off()
        QTimer.singleShot(2500, self._request_focus_off)
        self.roi_preview.set_source(None, None)
        self.view.clear_frame("Stopped")
        self.lbl_stream.setText("stream: idle")
        self.lbl_hold.setText("")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._status("Live view stopped — the camera has resumed its schedule")

    def _request_focus_off(self):
        if not self._client:
            return
        client = self._client
        self._tasks.run(client.stop_focus, None, lambda msg: None)

    def _send_heartbeat(self, hold=True):
        """Renew the focus session, restating the hold every time.

        Restating it matters: if the camera process restarts under a running
        live view, the next heartbeat puts the pause back rather than leaving
        the tool streaming while the schedule quietly carries on.
        """
        if not self._client:
            return
        client = self._client
        self._tasks.run(lambda: client.keep_focus(FOCUS_TTL_SECONDS, hold=hold),
                        self._on_focus_state, lambda msg: None)

    def _on_focus_state(self, state):
        """Show whether the camera really did stand down for us."""
        if not isinstance(state, dict):
            return
        if state.get("hold") and not state.get("hold_supported", True):
            return
        held = bool(state.get("hold"))
        self.lbl_hold.setText("measurements paused for focusing" if held else "")

    def _on_frame(self, jpeg, timestamp):
        image = QImage()
        if not image.loadFromData(jpeg):
            return
        self._last_jpeg = jpeg
        self._frames += 1
        self.view.set_frame(QPixmap.fromImage(image))
        # The stream resolution changes with the "Max size" setting; the square's
        # centre is a fraction and rides it out, but the pixel boxes must follow.
        shape = self.view.frame_shape()
        if shape != self._frame_shape:
            self._frame_shape = shape
            self._sync_roi_widgets()
        self._refresh_roi_preview()
        self.btn_save.setEnabled(True)

    # -- the measured square -------------------------------------------
    def _on_region_changed(self, region):
        # A different patch of sky, or a different amount of it, means a
        # different sharpness scale — the old trace would be misleading, and so
        # would the big "% of best" readout that was measured against it.
        self._reset_metrics()
        box = self.view.roi_box()
        if box is None:
            self._status("Measuring the whole frame")
        else:
            x0, y0, side = box
            self._status(f"Measuring {side}×{side} px at "
                         f"({x0 + side // 2}, {y0 + side // 2})")

    def _on_roi_changed(self, roi):
        """The view moved or resized the square itself — mirror it in the boxes."""
        self._sync_roi_widgets()
        self._refresh_roi_preview()

    def _refresh_roi_preview(self):
        """Repaint the side panel now, so it tracks the square while dragging."""
        box = self.view.roi_box()
        self.roi_preview.set_source(self.view.frame_pixmap(), box)
        if box is None:
            self.lbl_preview.setText("Square — whole frame (set a side to zoom)")
        else:
            x0, y0, side = box
            self.lbl_preview.setText(
                f"Square — {side}×{side} px at ({x0 + side // 2}, {y0 + side // 2})")

    def _sync_roi_widgets(self):
        """Show the square's size and centre in pixels of the current frame.

        Signals are blocked so that reflecting the view's state does not look
        like the operator typing, which would loop straight back into the view.
        """
        shape = self.view.frame_shape()
        fx, fy = self.view.roi_center
        limits = (None, None, None) if shape is None else (
            min(shape), shape[1], shape[0])
        for widget, limit, value in (
                (self.sb_roi, limits[0], self.view.roi_side),
                (self.sb_roi_x, limits[1], fx * (limits[1] or 0)),
                (self.sb_roi_y, limits[2], fy * (limits[2] or 0))):
            widget.blockSignals(True)
            if limit:
                widget.setMaximum(limit)
            widget.setValue(int(round(value)))
            widget.blockSignals(False)

    def _on_roi_side_typed(self, value):
        self.view.set_roi_side(value)
        self._sync_roi_widgets()
        self._refresh_roi_preview()

    def _on_roi_center_typed(self, _value):
        shape = self.view.frame_shape()
        if shape is None:
            return
        full_h, full_w = shape
        self.view.set_roi_center(self.sb_roi_x.value() / full_w,
                                 self.sb_roi_y.value() / full_h)
        self._refresh_roi_preview()

    def _centre_roi(self):
        self.view.set_roi_center(0.5, 0.5)
        self._sync_roi_widgets()
        self._refresh_roi_preview()

    # -- metrics -------------------------------------------------------

    def _reset_metrics(self):
        self.plot.reset()
        self.lbl_sharpness.setText("—")

    def _update_metrics(self):
        """Score the newest frame. Done locally so the camera stays untouched."""
        if self._last_jpeg is None:
            return
        try:
            import numpy as np
            from PIL import Image
            import frame_archive
            with Image.open(io.BytesIO(self._last_jpeg)) as img:
                array = np.asarray(img.convert("L"))
        except Exception:
            return

        region = self.view.region
        if region:
            h, w = array.shape[:2]
            x0, y0, x1, y1 = region
            array = array[int(y0 * h):max(int(y1 * h), int(y0 * h) + 1),
                          int(x0 * w):max(int(x1 * w), int(x0 * w) + 1)]
        if array.size == 0:
            return

        score = frame_archive.sharpness(array)
        self.plot.add(score)
        best = self.plot.best or 1.0
        percent = 100.0 * score / best
        self.lbl_sharpness.setText(f"{score:,.0f}   ({percent:.0f} % of best)")
        colour = "#3fd07a" if percent > 95 else ("#d8b13f" if percent > 75
                                                 else "#c96b6b")
        self.lbl_sharpness.setStyleSheet(f"color:{colour};")

        stats = frame_archive.frame_stats(array)
        saturated = stats.get("saturated_pct", 0)
        warning = "  ⚠ overexposed" if saturated > 1 else ""
        self.lbl_exposure.setText(
            f"mean {stats.get('mean')}  ·  max {stats.get('max')}  ·  "
            f"saturated {saturated} %{warning}  ·  {self._frames} frames")

    # -- parameters ----------------------------------------------------
    def _on_apply_params(self, params):
        if not self._client:
            return
        client = self._client
        self._status(f"Applying {params}…")

        def _accepted(response):
            req_id = response.get("id")
            if req_id:
                QTimer.singleShot(1500, lambda: self._poll_param(req_id))

        self._tasks.run(lambda: client.set_params(params), _accepted,
                        self._on_error)

    def _poll_param(self, req_id, attempt=0):
        if not self._client:
            return
        client = self._client

        def _done(result):
            if not result.get("done"):
                if attempt < 20:
                    QTimer.singleShot(
                        1500, lambda: self._poll_param(req_id, attempt + 1))
                else:
                    self._status(
                        "The camera has not applied the change yet — it only "
                        "does so between frames, which can take one exposure.")
                return
            errors = result.get("errors") or []
            applied = result.get("applied") or {}
            if errors:
                self._status(f"Applied {applied}; problems: {'; '.join(errors)}")
            else:
                self._status(f"Applied {applied}")
            self.plot.reset()
            self._tasks.run(client.params, self._on_params, lambda msg: None)

        self._tasks.run(lambda: client.param_result(req_id), _done,
                        lambda msg: None)

    # -- misc ----------------------------------------------------------
    def _on_save(self):
        if not self._last_jpeg:
            return
        from datetime import datetime as dt
        default = f"focus_{dt.now().strftime('%Y%m%dT%H%M%S')}.jpg"
        target, _ = QFileDialog.getSaveFileName(self, "Save frame", default,
                                                "JPEG (*.jpg);;All files (*)")
        if not target:
            return
        try:
            with open(target, "wb") as fh:
                fh.write(self._last_jpeg)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return
        self._status(f"Saved {target}")

    def _on_error(self, message):
        self._status(message)
        self.lbl_stream.setText("stream: error")

    def closeEvent(self, event):
        self._stop_stream()
        if self._client:
            # Synchronous here: the window is going away, so there is no event
            # loop left to run a retry on. If it fails, the camera's focus TTL
            # still switches free-running off within a minute.
            try:
                self._client.stop_focus()
            except CameraHttpError:
                pass
        self._tasks.wait_all()
        save_settings(self._settings)
        event.accept()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Every Camera — focus assistant (local network only)")
    parser.add_argument("--host", help="Connect to this camera straight away")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    args = parser.parse_args()

    try:
        import PyQt5 as _pyqt5
        plugins = os.path.join(os.path.dirname(_pyqt5.__file__), "Qt5", "plugins")
        if os.path.isdir(plugins):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = plugins
    except Exception:
        os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = FocusWindow(load_settings())
    window.show()

    if args.host:
        window.cmb_cameras.addItem(f"{args.host}:{args.port}",
                                   (args.host, args.port))
        QTimer.singleShot(100, window._on_start)
    else:
        QTimer.singleShot(200, window.discover)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
