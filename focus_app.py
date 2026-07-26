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
    QPushButton, QLabel, QLineEdit, QComboBox, QDoubleSpinBox,
    QSpinBox, QGroupBox, QMessageBox, QSizePolicy, QStatusBar, QFileDialog,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QPainter, QPen, QColor, QFont

from net_client import (
    CameraClient, CameraHttpError, TaskRunner, DiscoveryTask, MjpegStream,
    load_settings, save_settings,
)

DEFAULT_HTTP_PORT = 8765
HEARTBEAT_MS = 20_000        # keep the focus session alive
FOCUS_TTL_SECONDS = 60       # camera stops free-running this long after the last ping
SHARPNESS_HISTORY = 120
STRETCH_MODES = [
    ("minmax", "Auto (min–max)"),
    ("percentile", "Auto (0.5–99.5 %)"),
    ("raw", "Raw (no stretch)"),
]


# ---------------------------------------------------------------------------
# Live view with an optional measurement region
# ---------------------------------------------------------------------------
class LiveView(QLabel):
    """Shows the stream and lets the user drag a region to measure sharpness in."""

    region_changed = pyqtSignal(object)   # (x0, y0, x1, y1) in image coords, or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#0b0b0b; color:#777;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 240)
        self.setText("Not connected")
        self._pixmap = None
        self._drag_from = None
        self._drag_to = None
        self.region = None        # normalised (x0, y0, x1, y1), 0..1

    def set_frame(self, pixmap):
        self._pixmap = pixmap
        self.update()

    def clear_frame(self, message):
        self._pixmap = None
        self.setText(message)
        self.update()

    def clear_region(self):
        self.region = None
        self._drag_from = self._drag_to = None
        self.region_changed.emit(None)
        self.update()

    # -- painting ------------------------------------------------------
    def _target_rect(self):
        """Where the frame is drawn inside this widget (letterboxed)."""
        if self._pixmap is None:
            return None
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if not pw or not ph:
            return None
        scale = min(self.width() / pw, self.height() / ph)
        w, h = int(pw * scale), int(ph * scale)
        return ((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def paintEvent(self, event):
        if self._pixmap is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b0b0b"))
        rect = self._target_rect()
        x, y, w, h = rect
        painter.drawPixmap(x, y, self._pixmap.scaled(
            w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))

        region = self._current_region()
        if region:
            rx0, ry0, rx1, ry1 = region
            painter.setPen(QPen(QColor("#3fd07a"), 2, Qt.DashLine))
            painter.drawRect(int(x + rx0 * w), int(y + ry0 * h),
                             int((rx1 - rx0) * w), int((ry1 - ry0) * h))
        painter.end()

    def _current_region(self):
        if self._drag_from and self._drag_to:
            return self._normalised(self._drag_from, self._drag_to)
        return self.region

    def _normalised(self, p0, p1):
        rect = self._target_rect()
        if not rect:
            return None
        x, y, w, h = rect
        xs = sorted((max(0.0, min(1.0, (p.x() - x) / w)) for p in (p0, p1)))
        ys = sorted((max(0.0, min(1.0, (p.y() - y) / h)) for p in (p0, p1)))
        if xs[1] - xs[0] < 0.02 or ys[1] - ys[0] < 0.02:
            return None
        return (xs[0], ys[0], xs[1], ys[1])

    # -- region selection ----------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._pixmap is not None:
            self._drag_from = event.pos()
            self._drag_to = event.pos()

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            self._drag_to = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if self._drag_from is None:
            return
        region = self._normalised(self._drag_from, event.pos())
        self._drag_from = self._drag_to = None
        self.region = region
        self.region_changed.emit(region)
        self.update()


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
    """Editable camera parameters, described by the camera itself."""

    apply_requested = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__("Camera parameters", parent)
        self._widgets = {}
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

    def build(self, schema, current, editable=True):
        while self._form.rowCount():
            self._form.removeRow(0)
        self._widgets.clear()
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

        self._reset_fields()
        self._hint.setText(
            "Change a value and press Apply — the camera applies it between "
            "frames." if editable else "Read-only camera.")
        self.btn_apply.setEnabled(editable)
        self.btn_revert.setEnabled(editable)

    def _make_widget(self, field):
        kind = field.get("type", "text")
        if kind == "choice":
            combo = QComboBox()
            for choice in field.get("choices", []):
                combo.addItem(str(choice.get("label", choice.get("value"))),
                              choice.get("value"))
            return combo
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

    def set_current(self, current):
        self._current = dict(current or {})
        self._reset_fields()

    def _reset_fields(self):
        for name, (field, widget) in self._widgets.items():
            value = self._current.get(name)
            if value is None:
                continue
            if isinstance(widget, QComboBox):
                index = widget.findData(value)
                if index < 0:
                    index = widget.findText(str(value))
                if index >= 0:
                    widget.setCurrentIndex(index)
            elif isinstance(widget, QSpinBox):
                try:
                    widget.setValue(int(value))
                except (TypeError, ValueError):
                    pass
            elif isinstance(widget, QDoubleSpinBox):
                try:
                    widget.setValue(float(value))
                except (TypeError, ValueError):
                    pass
            else:
                widget.setText(str(value))

    def values(self):
        """Only the fields that actually differ from the camera's state."""
        out = {}
        for name, (field, widget) in self._widgets.items():
            if isinstance(widget, QComboBox):
                value = widget.currentData()
                if value is None:
                    value = widget.currentText()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                value = widget.value()
            else:
                text = widget.text().strip()
                if not text:
                    continue
                value = text
            if name in self._current and str(self._current[name]) == str(value):
                continue
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

        self._build_ui()

        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(HEARTBEAT_MS)
        self._heartbeat.timeout.connect(self._send_heartbeat)

        self._metrics = QTimer(self)
        self._metrics.setInterval(1000)
        self._metrics.timeout.connect(self._update_metrics)

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
        self.view = LiveView()
        self.view.region_changed.connect(self._on_region_changed)
        left_lay.addWidget(self.view, 1)

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
        btn_region.setToolTip("Measure the whole frame again "
                              "(drag on the image to pick a region)")
        btn_region.clicked.connect(self.view.clear_region)
        row.addWidget(btn_region)
        m_lay.addLayout(row)
        left_lay.addWidget(metrics)
        splitter.addWidget(left)

        # ---- right: controls
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)

        info_box = QGroupBox("Connection")
        info_lay = QVBoxLayout(info_box)
        self.lbl_camera = QLabel("not connected")
        self.lbl_camera.setWordWrap(True)
        info_lay.addWidget(self.lbl_camera)
        self.lbl_stream = QLabel("stream: idle")
        self.lbl_stream.setStyleSheet("color:#888; font-size:11px;")
        info_lay.addWidget(self.lbl_stream)
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

        self.params = ParamForm()
        self.params.apply_requested.connect(self._on_apply_params)
        right_lay.addWidget(self.params)

        self.btn_save = QPushButton("Save current frame…")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._on_save)
        right_lay.addWidget(self.btn_save)
        right_lay.addStretch()
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([820, 340])

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
        for node in sorted(nodes, key=lambda n: str(n.get("instance_name"))):
            host, port = node.get("host"), node.get("http_port", DEFAULT_HTTP_PORT)
            focus = "" if node.get("supports_focus", True) else "  [view only]"
            self.cmb_cameras.addItem(
                f"{node.get('instance_name', '?')} "
                f"({node.get('camera_type', '?')}) — {host}:{port}{focus}",
                (host, port))
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
        self.lbl_camera.setText(
            f"<b>{info.get('instance_name', '?')}</b> "
            f"({info.get('camera_type', '?')})<br>{info.get('hostname', '')}")
        if not info.get("supports_focus", True):
            self._status(
                f"{info.get('instance_name')} streams whatever its daemon "
                f"produces; it has no free-running focus mode.")
        client = self._client
        self._tasks.run(client.params, self._on_params, self._on_error)
        self._start_stream()

    def _on_params(self, payload):
        self.params.build(payload.get("schema", []), payload.get("current", {}),
                          editable=payload.get("editable", False))

    def _start_stream(self):
        self._stop_stream()
        if not self._client:
            return
        self._reset_metrics()
        max_side = self.cmb_size.currentData() or None
        self._stream = MjpegStream(self._client, max_side=max_side,
                                   stretch=self.cmb_stretch.currentData(),
                                   fps=self.sb_fps.value(), parent=self)
        self._stream.frame.connect(self._on_frame)
        self._stream.state.connect(
            lambda s: self.lbl_stream.setText(f"stream: {s}"))
        self._stream.start()
        self._send_heartbeat()
        self._heartbeat.start()
        self._metrics.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._status("Live view running — focus mode is on while this window streams")

    def _restart_stream_if_running(self):
        if self._stream is not None:
            self._start_stream()

    def _stop_stream(self):
        self._heartbeat.stop()
        self._metrics.stop()
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
        self.view.clear_frame("Stopped")
        self.lbl_stream.setText("stream: idle")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._status("Live view stopped — the camera is back to schedule only")

    def _request_focus_off(self):
        if not self._client:
            return
        client = self._client
        self._tasks.run(client.stop_focus, None, lambda msg: None)

    def _send_heartbeat(self):
        if not self._client:
            return
        client = self._client
        self._tasks.run(lambda: client.keep_focus(FOCUS_TTL_SECONDS),
                        None, lambda msg: None)

    def _on_frame(self, jpeg, timestamp):
        image = QImage()
        if not image.loadFromData(jpeg):
            return
        self._last_jpeg = jpeg
        self._frames += 1
        self.view.set_frame(QPixmap.fromImage(image))
        self.btn_save.setEnabled(True)

    # -- metrics -------------------------------------------------------
    def _on_region_changed(self, region):
        self.plot.reset()
        self._status("Measuring the selected region" if region
                     else "Measuring the whole frame")

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
