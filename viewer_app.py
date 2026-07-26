#!/usr/bin/env python3
"""
Every Camera — Frame Viewer (observer tool, Windows and Linux).

A read-only window onto the cameras. It never changes a setting and never
triggers a capture; for that there is ``focus_app.py``.

Two sources:

  * **LAN** — talks to a camera's frame server and can open *any* frame it has
    ever captured, plus the newest one. This works even while the camera is
    busy measuring, and even if its worker has stalled, because the archive is
    served straight off disk.

  * **HiveMQ (MQTT)** — for cameras reachable only over the internet. To keep
    traffic down this fetches **only the latest frame** on request; browsing
    the archive is deliberately LAN-only, since a full archive would mean
    megabytes of base64 through a public broker.

Requirements on the observer machine: PyQt5, numpy, Pillow, and paho-mqtt for
the MQTT source. No camera drivers.

Usage:
    python viewer_app.py                     # discover cameras on the LAN
    python viewer_app.py --host 192.168.1.5  # connect straight to one camera
    python viewer_app.py --mqtt              # start on the MQTT tab
"""
import base64
import json
import os
import sys

from datetime import datetime as dt

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QCheckBox, QListWidget,
    QListWidgetItem, QGroupBox, QFileDialog, QMessageBox, QSizePolicy,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
    QStatusBar, QScrollArea,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage

from net_client import (
    CameraClient, TaskRunner, DiscoveryTask, load_settings, save_settings,
)

DEFAULT_HTTP_PORT = 8765
AUTO_REFRESH_MS = 3000
STRETCH_MODES = [
    ("minmax", "Auto (min–max)"),
    ("percentile", "Auto (0.5–99.5 %)"),
    ("raw", "Raw (no stretch)"),
]


# ---------------------------------------------------------------------------
# Image display
# ---------------------------------------------------------------------------
class ImageView(QScrollArea):
    """Displays a frame, either fitted to the window or at a fixed zoom."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._label = QLabel("No frame")
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet("background:#111; color:#888;")
        self._label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setWidget(self._label)
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignCenter)
        # The full-resolution pixmap; scaling always starts from this so
        # repeated resizes never degrade the picture.
        self._pixmap = None
        self._zoom = 0.0          # 0 = fit to window

    def set_jpeg(self, data):
        image = QImage()
        if not image.loadFromData(data):
            self.show_message("Could not decode frame")
            return False
        self._pixmap = QPixmap.fromImage(image)
        self._apply()
        return True

    def show_message(self, text):
        self._pixmap = None
        self._label.setPixmap(QPixmap())
        self._label.setText(text)

    def set_zoom(self, zoom):
        self._zoom = zoom
        self._apply()

    def has_frame(self):
        return self._pixmap is not None and not self._pixmap.isNull()

    def pixmap_size(self):
        return self._pixmap.size() if self.has_frame() else None

    def _apply(self):
        if not self.has_frame():
            return
        if self._zoom <= 0:
            self.setWidgetResizable(True)
            scaled = self._pixmap.scaled(self.viewport().size(),
                                         Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
        else:
            self.setWidgetResizable(False)
            scaled = self._pixmap.scaled(self._pixmap.size() * self._zoom,
                                         Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation)
            self._label.resize(scaled.size())
        self._label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._zoom <= 0:
            self._apply()


# ---------------------------------------------------------------------------
# LAN source
# ---------------------------------------------------------------------------
class LanPanel(QWidget):
    """Browse the full archive of a camera over the local network."""

    status = pyqtSignal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._tasks = TaskRunner(self)
        self._discovery = None
        self._client = None
        self._frames = []
        self._current_name = None
        self._build_ui()

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(AUTO_REFRESH_MS)
        self._auto_timer.timeout.connect(self._refresh_latest)

    # -- UI ------------------------------------------------------------
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        # ---- left: camera + frame list
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)

        cam_box = QGroupBox("Camera")
        cam_lay = QGridLayout(cam_box)
        cam_lay.setColumnStretch(0, 1)

        self.cmb_cameras = QComboBox()
        self.cmb_cameras.setMinimumWidth(200)
        self.cmb_cameras.currentIndexChanged.connect(self._on_camera_chosen)
        cam_lay.addWidget(self.cmb_cameras, 0, 0, 1, 2)

        self.btn_discover = QPushButton("Search LAN")
        self.btn_discover.setToolTip(
            "Broadcast on the local network and list every camera that answers")
        self.btn_discover.clicked.connect(self.discover)
        cam_lay.addWidget(self.btn_discover, 1, 0)

        self.le_manual = QLineEdit()
        self.le_manual.setPlaceholderText("host or host:port")
        self.le_manual.returnPressed.connect(self._on_manual_connect)
        cam_lay.addWidget(self.le_manual, 2, 0)
        btn_connect = QPushButton("Connect")
        btn_connect.clicked.connect(self._on_manual_connect)
        cam_lay.addWidget(btn_connect, 2, 1)

        self.lbl_camera = QLabel("not connected")
        self.lbl_camera.setWordWrap(True)
        self.lbl_camera.setStyleSheet("color:#888; font-size:11px;")
        cam_lay.addWidget(self.lbl_camera, 3, 0, 1, 2)
        left_lay.addWidget(cam_box)

        arch_box = QGroupBox("Archive")
        arch_lay = QVBoxLayout(arch_box)
        row = QHBoxLayout()
        self.cmb_date = QComboBox()
        self.cmb_date.addItem("all dates", None)
        self.cmb_date.currentIndexChanged.connect(self._reload_frames)
        row.addWidget(self.cmb_date, 1)
        btn_reload = QPushButton("Reload")
        btn_reload.clicked.connect(self._reload_all)
        row.addWidget(btn_reload)
        arch_lay.addLayout(row)

        self.list_frames = QListWidget()
        self.list_frames.currentRowChanged.connect(self._on_frame_selected)
        arch_lay.addWidget(self.list_frames, 1)

        self.lbl_count = QLabel("—")
        self.lbl_count.setStyleSheet("color:#888; font-size:11px;")
        arch_lay.addWidget(self.lbl_count)
        left_lay.addWidget(arch_box, 1)
        splitter.addWidget(left)

        # ---- right: image + info
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.btn_latest = QPushButton("Latest frame")
        self.btn_latest.setToolTip("Show the newest frame the camera captured")
        self.btn_latest.clicked.connect(self._refresh_latest)
        bar.addWidget(self.btn_latest)

        self.cb_auto = QCheckBox("Follow live")
        self.cb_auto.setToolTip("Reload the newest frame every few seconds")
        self.cb_auto.stateChanged.connect(self._on_auto_toggled)
        bar.addWidget(self.cb_auto)

        bar.addWidget(QLabel("Contrast:"))
        self.cmb_stretch = QComboBox()
        for value, label in STRETCH_MODES:
            self.cmb_stretch.addItem(label, value)
        self.cmb_stretch.currentIndexChanged.connect(self._reload_current)
        bar.addWidget(self.cmb_stretch)

        bar.addWidget(QLabel("Zoom:"))
        self.cmb_zoom = QComboBox()
        for label, value in [("Fit", 0.0), ("50 %", 0.5), ("100 %", 1.0),
                             ("200 %", 2.0), ("400 %", 4.0)]:
            self.cmb_zoom.addItem(label, value)
        self.cmb_zoom.currentIndexChanged.connect(
            lambda: self.view.set_zoom(self.cmb_zoom.currentData()))
        bar.addWidget(self.cmb_zoom)

        bar.addStretch()
        self.btn_save = QPushButton("Save original…")
        self.btn_save.setToolTip("Download the untouched capture file")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._on_save)
        bar.addWidget(self.btn_save)
        right_lay.addLayout(bar)

        self.view = ImageView()
        right_lay.addWidget(self.view, 1)

        self.lbl_info = QLabel("—")
        self.lbl_info.setStyleSheet("font-size:11px;")
        self.lbl_info.setWordWrap(True)
        right_lay.addWidget(self.lbl_info)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 900])

    # -- camera selection ----------------------------------------------
    def discover(self):
        self.btn_discover.setEnabled(False)
        self.status.emit("Searching the local network…")
        self._discovery = DiscoveryTask(timeout=2.0, parent=self)
        self._discovery.found.connect(self._on_discovered)
        self._discovery.finished.connect(
            lambda: self.btn_discover.setEnabled(True))
        self._discovery.start()

    def _on_discovered(self, nodes):
        current = self.cmb_cameras.currentData()
        self.cmb_cameras.blockSignals(True)
        self.cmb_cameras.clear()
        for node in sorted(nodes, key=lambda n: str(n.get("instance_name"))):
            host = node.get("host")
            port = node.get("http_port", DEFAULT_HTTP_PORT)
            label = (f"{node.get('instance_name', '?')} "
                     f"({node.get('camera_type', '?')}) — {host}:{port}")
            self.cmb_cameras.addItem(label, (host, port))
        for host, port in self._settings.get("recent_hosts", []):
            self.cmb_cameras.addItem(f"{host}:{port}", (host, port))
        self.cmb_cameras.blockSignals(False)

        if not nodes and self.cmb_cameras.count() == 0:
            self.status.emit(
                "No cameras answered. Enter host:port manually, and check that "
                "server.discovery is enabled on the camera.")
            self.cmb_cameras.addItem("— no cameras found —", None)
            return
        self.status.emit(f"Found {len(nodes)} camera(s) on the LAN")
        if current in [self.cmb_cameras.itemData(i)
                       for i in range(self.cmb_cameras.count())]:
            self.cmb_cameras.setCurrentIndex(
                [self.cmb_cameras.itemData(i)
                 for i in range(self.cmb_cameras.count())].index(current))
        else:
            self._on_camera_chosen(self.cmb_cameras.currentIndex())

    def _on_camera_chosen(self, index):
        target = self.cmb_cameras.itemData(index)
        if not target:
            return
        self.connect_to(*target)

    def _on_manual_connect(self):
        text = self.le_manual.text().strip()
        if not text:
            return
        host, _, port = text.partition(":")
        self.connect_to(host.strip(), int(port) if port.strip().isdigit()
                        else DEFAULT_HTTP_PORT)

    def connect_to(self, host, port=DEFAULT_HTTP_PORT):
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
        self.lbl_camera.setText(
            f"<b>{info.get('instance_name', '?')}</b> "
            f"({info.get('camera_type', '?')})<br>{info.get('hostname', '')}"
            f"<br>{info.get('output_dir') or 'no archive directory'}")
        if not info.get("archive_available"):
            self.status.emit(
                "Connected, but this camera has no archive directory "
                "configured — only the latest frame is available.")
        self._reload_all()

    def _on_error(self, message):
        self.status.emit(message)

    # -- archive -------------------------------------------------------
    def _reload_all(self):
        if not self._client:
            return
        client = self._client
        self._tasks.run(client.dates, self._on_dates, self._on_error)

    def _on_dates(self, dates):
        current = self.cmb_date.currentData()
        self.cmb_date.blockSignals(True)
        self.cmb_date.clear()
        self.cmb_date.addItem("all dates", None)
        for day in dates:
            self.cmb_date.addItem(day, day)
        if current in dates:
            self.cmb_date.setCurrentIndex(dates.index(current) + 1)
        self.cmb_date.blockSignals(False)
        self._reload_frames()

    def _reload_frames(self):
        if not self._client:
            return
        client = self._client
        date = self.cmb_date.currentData()
        self._tasks.run(lambda: client.frames(date=date, limit=1000),
                        self._on_frames, self._on_error)

    def _on_frames(self, listing):
        self._frames = listing.get("frames", [])
        self.list_frames.blockSignals(True)
        self.list_frames.clear()
        for frame in self._frames:
            when = dt.fromtimestamp(frame["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
            item = QListWidgetItem(f"{frame['name']}\n    {when} · "
                                   f"{frame['size'] // 1024} KB")
            item.setData(Qt.UserRole, frame["name"])
            self.list_frames.addItem(item)
        self.list_frames.blockSignals(False)
        total = listing.get("total", len(self._frames))
        shown = len(self._frames)
        self.lbl_count.setText(
            f"{shown} of {total} frame(s)" if shown < total
            else f"{total} frame(s)")
        if self._frames:
            self.list_frames.setCurrentRow(0)
        else:
            self.view.show_message("No frames in this archive")
            self.btn_save.setEnabled(False)

    def _on_frame_selected(self, row):
        if row < 0 or row >= len(self._frames) or not self._client:
            return
        self.cb_auto.setChecked(False)
        self._current_name = self._frames[row]["name"]
        self._load_frame(self._current_name)

    def _reload_current(self):
        if self._current_name:
            self._load_frame(self._current_name)
        elif self.cb_auto.isChecked() or self.view.has_frame():
            self._refresh_latest()

    def _load_frame(self, name):
        client = self._client
        stretch = self.cmb_stretch.currentData()
        self.view.show_message(f"Loading {name}…")
        self._tasks.run(lambda: client.frame_jpeg(name, stretch=stretch),
                        lambda data: self._show_frame(data, name),
                        self._on_error)
        self._tasks.run(lambda: client.frame_stats(name),
                        self._show_stats, lambda msg: None)

    def _show_frame(self, data, name):
        if self.view.set_jpeg(data):
            self.btn_save.setEnabled(name is not None)
            self.status.emit(f"Showing {name or 'latest frame'}")

    def _show_stats(self, payload):
        stats = payload.get("stats", {})
        meta = payload.get("metadata", {}) or {}
        parts = []
        if stats:
            parts.append("×".join(str(x) for x in stats.get("shape", [])))
            parts.append(str(stats.get("dtype", "")))
            parts.append(f"min {stats.get('min')} / max {stats.get('max')}")
            parts.append(f"mean {stats.get('mean')}")
            parts.append(f"saturated {stats.get('saturated_pct')} %")
        header = meta.get("fits_header") or {}
        for key in ("EXPTIME", "EXPTUS", "GAIN", "CCDTEMP", "ROI", "DATE-OBS"):
            if key in header:
                parts.append(f"{key} {header[key]}")
        self.lbl_info.setText("   ·   ".join(p for p in parts if p) or "—")

    # -- live ----------------------------------------------------------
    def _refresh_latest(self):
        if not self._client:
            return
        client = self._client
        stretch = self.cmb_stretch.currentData()
        self._current_name = None
        self.btn_save.setEnabled(False)
        self._tasks.run(lambda: client.latest_jpeg(stretch=stretch),
                        self._on_latest, self._on_error)

    def _on_latest(self, result):
        data, timestamp = result
        if self.view.set_jpeg(data):
            self.status.emit(f"Latest frame{f' — {timestamp}' if timestamp else ''}")
            self.list_frames.blockSignals(True)
            self.list_frames.setCurrentRow(-1)
            self.list_frames.blockSignals(False)

    def _on_auto_toggled(self, state):
        if state:
            self._refresh_latest()
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

    # -- saving --------------------------------------------------------
    def _on_save(self):
        if not self._client or not self._current_name:
            return
        name = self._current_name
        target, _ = QFileDialog.getSaveFileName(
            self, "Save original frame", name, "All files (*)")
        if not target:
            return
        client = self._client
        self.status.emit(f"Downloading {name}…")

        def _write(data):
            try:
                with open(target, "wb") as fh:
                    fh.write(data)
            except OSError as exc:
                QMessageBox.critical(self, "Save failed", str(exc))
                return
            self.status.emit(f"Saved {target} ({len(data) // 1024} KB)")

        self._tasks.run(lambda: client.frame_raw(name), _write, self._on_error)

    def cleanup(self):
        self._auto_timer.stop()
        self._tasks.wait_all()


# ---------------------------------------------------------------------------
# MQTT source — latest frame only
# ---------------------------------------------------------------------------
class MqttPanel(QWidget):
    """Watch cameras through a public broker; fetches only the latest frame."""

    status = pyqtSignal(str)

    def __init__(self, mqtt_cfg, parent=None):
        super().__init__(parent)
        self._cfg = mqtt_cfg or {}
        self._subscriber = None
        self._instances = {}
        self._pending = None
        self._build_ui()

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)

        self._refresh = QTimer(self)
        self._refresh.setInterval(5000)
        self._refresh.timeout.connect(self._refresh_table)
        self._refresh.start()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        note = QLabel(
            "Over MQTT the viewer requests <b>only the latest frame</b>, to keep "
            "traffic small. To browse the whole archive, use the LAN tab.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#888; font-size:11px;")
        root.addWidget(note)

        box = QGroupBox("Broker")
        grid = QGridLayout(box)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        grid.addWidget(QLabel("Host:"), 0, 0)
        self.le_host = QLineEdit(self._cfg.get("host", "broker.hivemq.com"))
        grid.addWidget(self.le_host, 0, 1)
        grid.addWidget(QLabel("Port:"), 0, 2)
        self.le_port = QLineEdit(str(self._cfg.get("port", 1883)))
        self.le_port.setMaximumWidth(70)
        grid.addWidget(self.le_port, 0, 3)

        grid.addWidget(QLabel("User:"), 1, 0)
        self.le_user = QLineEdit(self._cfg.get("user", ""))
        self.le_user.setPlaceholderText("(optional)")
        grid.addWidget(self.le_user, 1, 1)
        grid.addWidget(QLabel("Password:"), 1, 2)
        self.le_pass = QLineEdit(self._cfg.get("password", ""))
        self.le_pass.setEchoMode(QLineEdit.Password)
        self.le_pass.setPlaceholderText("(optional)")
        grid.addWidget(self.le_pass, 1, 3)

        grid.addWidget(QLabel("Prefix:"), 2, 0)
        self.le_prefix = QLineEdit(self._cfg.get("prefix", "every_camera"))
        grid.addWidget(self.le_prefix, 2, 1)
        self.cb_tls = QCheckBox("TLS")
        self.cb_tls.setChecked(self._cfg.get("tls", False))
        self.cb_tls.stateChanged.connect(
            lambda s: self.le_port.setText("8883" if s else "1883"))
        grid.addWidget(self.cb_tls, 2, 2)

        btns = QHBoxLayout()
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        self.lbl_conn = QLabel("Disconnected")
        self.lbl_conn.setStyleSheet("color:#888; font-weight:bold;")
        btns.addWidget(self.btn_connect)
        btns.addWidget(self.btn_disconnect)
        btns.addWidget(self.lbl_conn, 1)
        grid.addLayout(btns, 2, 3)
        root.addWidget(box)

        splitter = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Instance", "Type", "Status", "Shots", "Last update"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setMaximumHeight(180)
        splitter.addWidget(self.table)

        lower = QWidget()
        lower_lay = QVBoxLayout(lower)
        lower_lay.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.btn_get = QPushButton("Request latest frame")
        self.btn_get.setEnabled(False)
        self.btn_get.clicked.connect(self._on_request)
        bar.addWidget(self.btn_get)
        bar.addStretch()
        self.lbl_frame_info = QLabel("—")
        self.lbl_frame_info.setStyleSheet("font-size:11px;")
        bar.addWidget(self.lbl_frame_info)
        lower_lay.addLayout(bar)
        self.view = ImageView()
        lower_lay.addWidget(self.view, 1)
        splitter.addWidget(lower)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    # -- broker --------------------------------------------------------
    def _on_connect(self):
        from mqtt_client import MQTT_AVAILABLE
        if not MQTT_AVAILABLE:
            self.lbl_conn.setText("paho-mqtt not installed")
            self.lbl_conn.setStyleSheet("color:#cc0000; font-weight:bold;")
            return
        from mqtt_client import MqttSubscriber
        prefix = self.le_prefix.text().strip() or "every_camera"
        try:
            self._subscriber = MqttSubscriber(
                self.le_host.text().strip(), self.le_port.text().strip(),
                self.le_user.text().strip(), self.le_pass.text(),
                use_tls=self.cb_tls.isChecked())
        except Exception as exc:
            self.lbl_conn.setText(f"Error: {exc}")
            self.lbl_conn.setStyleSheet("color:#cc0000; font-weight:bold;")
            return
        self._subscriber.connected.connect(self._on_connected)
        self._subscriber.disconnected.connect(self._on_disconnected)
        self._subscriber.message_received.connect(self._on_message)
        self._subscriber.error.connect(self._on_broker_error)
        self._subscriber.connect_broker([f"{prefix}/+/status",
                                         f"{prefix}/+/frame"])
        self.lbl_conn.setText("Connecting…")
        self.btn_connect.setEnabled(False)

    def _on_disconnect(self):
        if self._subscriber:
            self._subscriber.disconnect_broker()
            self._subscriber = None
        self._instances.clear()
        self._refresh_table()
        self._on_disconnected()

    def _on_connected(self):
        self.lbl_conn.setText(f"Connected to {self.le_host.text().strip()}")
        self.lbl_conn.setStyleSheet("color:#007700; font-weight:bold;")
        self.btn_disconnect.setEnabled(True)
        self.btn_get.setEnabled(True)

    def _on_disconnected(self):
        self.lbl_conn.setText("Disconnected")
        self.lbl_conn.setStyleSheet("color:#888; font-weight:bold;")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.btn_get.setEnabled(False)

    def _on_broker_error(self, message):
        self.lbl_conn.setText(f"Error: {message}")
        self.lbl_conn.setStyleSheet("color:#cc0000; font-weight:bold;")
        self.btn_connect.setEnabled(True)

    # -- messages ------------------------------------------------------
    def _on_message(self, topic, payload):
        if topic.endswith("/frame"):
            self._on_frame(payload)
            return
        if not payload.strip():
            # Retained status cleared: the camera shut down cleanly.
            if self._instances.pop(topic, None) is not None:
                self._refresh_table()
            return
        try:
            self._instances[topic] = json.loads(payload)
        except json.JSONDecodeError:
            return
        self._refresh_table()

    def _refresh_table(self):
        records = sorted(self._instances.values(),
                         key=lambda r: str(r.get("instance_name", "")))
        selected = self._selected_instance()
        self.table.setRowCount(len(records))
        for row, rec in enumerate(records):
            values = [
                rec.get("instance_name", "?"),
                str(rec.get("camera_type", "?")).upper(),
                str(rec.get("status", "?")).upper(),
                str(rec.get("shots_taken", 0)),
                str(rec.get("last_update", ""))[:19].replace("T", " "),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                self.table.setItem(row, col, item)
            if selected and values[0] == selected:
                self.table.selectRow(row)

    def _selected_instance(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.text() if item else None

    def _on_request(self):
        instance = self._selected_instance()
        if not instance:
            QMessageBox.information(self, "No selection",
                                    "Select a camera in the table first.")
            return
        prefix = self.le_prefix.text().strip() or "every_camera"
        self._subscriber.publish(f"{prefix}/{instance}/cmd/get_frame", b"",
                                 retain=False)
        self._pending = instance
        self.view.show_message(f"Waiting for the latest frame from {instance}…")
        self.status.emit(f"Requested the latest frame from {instance}")
        self._timeout.start(15000)

    def _on_timeout(self):
        if not self._pending:
            return
        message = (f"No answer from {self._pending}. It may be offline, or its "
                   f"MQTT prefix may differ.")
        self.view.show_message(message)
        self.status.emit(message)
        self._pending = None

    def _on_frame(self, payload):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        instance = data.get("instance_name", "?")
        status = data.get("status", "ok")
        if status in ("accepted", "capturing"):
            self.view.show_message(f"{instance}: {status} — {data.get('note', '')}")
            return

        self._timeout.stop()
        self._pending = None
        if status != "ok" or not data.get("data"):
            message = (f"{instance}: {data.get('error') or status}")
            self.view.show_message(message)
            self.status.emit(message)
            return
        try:
            jpeg = base64.b64decode(data["data"])
        except (ValueError, TypeError) as exc:
            self.view.show_message(f"Could not decode frame: {exc}")
            return
        self.view.set_jpeg(jpeg)
        self.lbl_frame_info.setText(
            f"{instance} ({data.get('camera_type', '?')})   ·   "
            f"{data.get('timestamp', '')}   ·   {len(jpeg) // 1024} KB")
        self.status.emit(f"Received the latest frame from {instance}")

    def cleanup(self):
        self._refresh.stop()
        if self._subscriber:
            self._subscriber.disconnect_broker()
            self._subscriber = None


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class ViewerWindow(QMainWindow):
    def __init__(self, mqtt_cfg, settings, start_tab="lan"):
        super().__init__()
        self.setWindowTitle("Every Camera — Viewer")
        self.resize(1150, 750)
        self.setMinimumSize(760, 480)

        self._settings = settings
        self.tabs = QTabWidget()
        self.lan = LanPanel(settings)
        self.mqtt = MqttPanel(mqtt_cfg)
        self.tabs.addTab(self.lan, "Local network")
        self.tabs.addTab(self.mqtt, "HiveMQ (latest frame)")
        self.setCentralWidget(self.tabs)
        if start_tab == "mqtt":
            self.tabs.setCurrentWidget(self.mqtt)

        self.setStatusBar(QStatusBar())
        self.lan.status.connect(self._show_status)
        self.mqtt.status.connect(self._show_status)
        self._show_status("Ready")

    def _show_status(self, message):
        self.statusBar().showMessage(message, 15000)

    def closeEvent(self, event):
        self.lan.cleanup()
        self.mqtt.cleanup()
        save_settings(self._settings)
        event.accept()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Every Camera — frame viewer (read-only)")
    parser.add_argument("--host", help="Connect to this camera straight away")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--mqtt", action="store_true",
                        help="Start on the MQTT tab")
    parser.add_argument("--config", default=None,
                        help="config.json to read MQTT defaults from")
    args = parser.parse_args()

    # Qt/OpenCV plugin clash, same workaround as the other GUIs
    try:
        import PyQt5 as _pyqt5
        plugins = os.path.join(os.path.dirname(_pyqt5.__file__), "Qt5", "plugins")
        if os.path.isdir(plugins):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = plugins
    except Exception:
        os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)

    mqtt_cfg = {}
    try:
        from utils import load_config
        mqtt_cfg = load_config(args.config).get("mqtt", {})
    except Exception:
        pass
    settings = load_settings()
    mqtt_cfg = {**mqtt_cfg, **settings.get("mqtt", {})}

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = ViewerWindow(mqtt_cfg, settings,
                          start_tab="mqtt" if args.mqtt else "lan")
    window.show()

    if args.host:
        window.lan.connect_to(args.host, args.port)
    elif not args.mqtt:
        QTimer.singleShot(200, window.lan.discover)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
