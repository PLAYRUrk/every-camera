#!/usr/bin/env python3
"""Read-only window onto the cameras; changing anything is ``focus_app.py``'s job.

The archive is served straight off disk, so any frame opens even while the camera
is measuring or its worker has stalled. ``--via`` reaches a camera through another.

Needs only PyQt5, numpy and Pillow on the observer machine: no camera drivers.
"""
import os
import sys

from datetime import datetime as dt, time as time_of_day

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QCheckBox, QDoubleSpinBox,
    QTimeEdit, QTreeWidget, QTreeWidgetItem,
    QGroupBox, QFileDialog, QMessageBox, QSizePolicy,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
    QStatusBar, QScrollArea, QDialog,
)
from PyQt5.QtCore import Qt, QTime, QTimer, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QPainter, QColor, QFont

from net_client import (
    CameraClient, TaskRunner, DiscoveryTask, load_settings, save_settings,
    node_label, node_name_of, parse_address,
)
from frame_grouping import (
    entry_timestamp, group_by_session, group_japan_cycles,
    japan_filter_label,
)
import cycles as composite

# Holds ``(session_index, cycle_index, filter)`` on a filter row. Frame rows use
# ``Qt.UserRole`` for their name, and the two must not collide.
FILTER_ROLE = Qt.UserRole + 1
# Keeps opened rows across a rebuild. Row text will not do: it carries a frame
# count, which changes.
GROUP_ROLE = Qt.UserRole + 2

DEFAULT_HTTP_PORT = 8765
AUTO_REFRESH_MS = 3000
# A poll asks for one listing row, not the whole archive.
LIST_REFRESH_MS = 5000
# One request must cover every day the tree shows, or whole days go missing from it.
ARCHIVE_LIMIT = 5000
# Only this camera writes a repeating general cycle into a flat directory, so
# only here can a cycle be recovered from the archive.
CYCLE_CAMERA_TYPE = "japan"
STRETCH_MODES = [
    ("minmax", "Auto (min–max)"),
    ("percentile", "Auto (0.5–99.5 %)"),
    ("raw", "Raw (no stretch)"),
]

# Already shown by the time the panel lists whatever else the camera published.
META_SHOWN = {"fits_header", "size", "mtime", "timestamp", "name", "path"}

# Shown first; the rest follow in header order, which is the writers' order.
DETAIL_ORDER = [
    "DATE-OBS", "DATE-LOC", "EXPTIME", "EXPTUS", "IMAGETYP", "OBSMODE",
    "FILTER", "FILTERWAVELENGTH", "FILTERDESCRIPTION", "GAIN", "CCDGAIN",
    "BINNING", "READSPD", "ROI", "CCD-TEMP", "CCDTEMP", "SETTEMP", "SINKTEMP",
    "TRGTEMP", "SKYMEAN", "SPLITNUM", "SPLITIDX", "STACK_N", "SUB_EXP",
    "SEQNO", "ADCFULL", "BITDEPTH", "ENCODING", "INSTRUME", "SENSOR", "VENDOR",
    "CAMSN", "CAMVER", "DRVVER", "DCAMVER", "SITEID", "DEVICEID",
    "SITELAT", "SITELON", "SITEELEV",
]

DETAIL_SKIP = {"SIMPLE", "BITPIX", "NAXIS", "NAXIS1", "NAXIS2", "EXTEND",
               "BSCALE", "BZERO", "COMMENT", "HISTORY"}


class ImageView(QScrollArea):
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


class CompositeWindow(QDialog):
    """A window of its own: the difference frame is read *against* the ordinary one, so
    neither may hide the other. Modeless, so the archive stays browsable.
    """

    window_changed = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Every Camera — filter difference")
        self.setWindowFlags(Qt.Window)
        self.resize(560, 640)
        self._placed = False

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        bar = QHBoxLayout()
        self.lbl_what = QLabel("—")
        self.lbl_what.setStyleSheet("font-weight:bold;")
        bar.addWidget(self.lbl_what, 1)
        bar.addWidget(QLabel("T:"))
        self.sp_window = QDoubleSpinBox()
        self.sp_window.setRange(1.0, 86400.0)
        self.sp_window.setDecimals(0)
        self.sp_window.setSingleStep(30.0)
        self.sp_window.setSuffix(" s")
        self.sp_window.setToolTip(
            "The weighting window. A neighbouring frame this far away counts "
            "for nothing; one taken at the same moment counts for 1.")
        self.sp_window.setKeyboardTracking(False)
        self.sp_window.valueChanged.connect(self.window_changed.emit)
        bar.addWidget(self.sp_window)
        root.addLayout(bar)

        self.view = ImageView()
        root.addWidget(self.view, 1)

        self.lbl_weights = QLabel("—")
        self.lbl_weights.setStyleSheet("font-size:11px;")
        self.lbl_weights.setWordWrap(True)
        root.addWidget(self.lbl_weights)

        self.lbl_sources = QLabel("—")
        self.lbl_sources.setStyleSheet("color:#888; font-size:11px;")
        self.lbl_sources.setWordWrap(True)
        root.addWidget(self.lbl_sources)

    def set_window_seconds(self, seconds, quiet=True):
        """Quiet by default: following a camera is not a request."""
        if not seconds or float(seconds) <= 0:
            return
        if quiet:
            self.sp_window.blockSignals(True)
        self.sp_window.setValue(float(seconds))
        if quiet:
            self.sp_window.blockSignals(False)

    def window_seconds(self):
        return float(self.sp_window.value())

    def show_loading(self, label):
        self.lbl_what.setText(label)
        self.view.show_message("Building the difference frame…")

    def show_result(self, jpeg, headers, label):
        self.lbl_what.setText(label)
        if not self.view.set_jpeg(jpeg):
            return
        self.lbl_weights.setText(
            f"k0 = {headers.get('X-Composite-K0', '?')} "
            f"(Δ0 = {headers.get('X-Composite-Gap0', '?')} s, back to the "
            f"previous cycle)   ·   "
            f"k1 = {headers.get('X-Composite-K1', '?')} "
            f"(Δ1 = {headers.get('X-Composite-Gap1', '?')} s, on to this "
            f"cycle's last frame)   ·   first − (previous·k0 + last·k1) / 2")
        self.lbl_sources.setText(
            f"first {headers.get('X-Composite-First', '?')}   ·   "
            f"previous {headers.get('X-Composite-Previous', '?')}   ·   "
            f"last {headers.get('X-Composite-Last', '?')}")

    def show_error(self, message):
        self.view.show_message(message)
        self.lbl_weights.setText("—")
        self.lbl_sources.setText("—")

    def place_beside(self, other):
        if self._placed or other is None:
            return
        self._placed = True
        frame = other.frameGeometry()
        self.move(frame.x() + frame.width() + 8, frame.y())


class HistogramPlot(QWidget):
    """Log height: sky frames are one narrow peak, and on a linear axis the rest is flat."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._counts = []
        self._edges = []
        self._full_scale = 65535

    def set_data(self, counts, edges, full_scale=65535):
        self._counts = list(counts or [])
        self._edges = list(edges or [])
        self._full_scale = full_scale or 65535
        self.update()

    def clear(self):
        self.set_data([], [])

    def paintEvent(self, event):
        import math

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#141414"))
        w, h = self.width(), self.height()
        if not self._counts or max(self._counts) <= 0:
            painter.setPen(QColor("#666"))
            painter.drawText(self.rect(), Qt.AlignCenter, "no histogram")
            painter.end()
            return

        top = math.log10(max(self._counts) + 1.0) or 1.0
        bar_w = w / float(len(self._counts))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#4a90d9"))
        for i, count in enumerate(self._counts):
            bar_h = (math.log10(count + 1.0) / top) * (h - 14)
            if bar_h < 1 and count:
                bar_h = 1        # a bin with anything in it must be visible
            painter.drawRect(int(i * bar_w), int(h - bar_h),
                             max(1, int(bar_w)), int(bar_h))

        painter.setPen(QColor("#888"))
        painter.setFont(QFont("", 8))
        painter.drawText(2, h - 2, "0")
        label = f"{int(self._full_scale)}"
        painter.drawText(w - painter.fontMetrics().width(label) - 2, h - 2, label)
        painter.end()


class FrameInfoPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 0, 0)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Key", "Value"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        layout.addWidget(QLabel("Intensity histogram"))
        self.histogram = HistogramPlot()
        layout.addWidget(self.histogram)

    def show_payload(self, payload, name=None):
        header = (payload.get("metadata") or {}).get("fits_header") or {}
        rows = self._rows(header, payload, name)
        self.table.setRowCount(len(rows))
        for row, (key, value) in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(key))
            self.table.setItem(row, 1, QTableWidgetItem(value))
        hist = payload.get("histogram") or {}
        self.histogram.set_data(hist.get("counts"), hist.get("edges"),
                                full_scale_of(payload))

    def clear(self):
        self.table.setRowCount(0)
        self.histogram.clear()

    def _rows(self, header, payload, name=None):
        """A Canon JPEG has no header and setup mode writes no file; both still get the
        file, pixel and published-fact rows.
        """
        meta = payload.get("metadata") or {}
        # ``metadata.name`` comes from the driver that wrote the live frame: a followed
        # frame has no other name.
        rows = [("Frame", payload.get("name") or name or meta.get("name")
                 or "latest frame")]

        upper = {k.upper(): v for k, v in header.items()}
        when = upper.get("DATE-OBS") or meta.get("timestamp")
        if when:
            rows.append(("Taken", str(when)))
        if meta.get("size"):
            rows.append(("File size", f"{meta['size'] / 1024:.0f} KB"))
        if meta.get("mtime"):
            rows.append(("Written",
                         dt.fromtimestamp(meta["mtime"]).strftime("%Y-%m-%d %H:%M:%S")))
        rows.extend(self._measured(payload))
        sharp = payload.get("sharpness")
        if sharp:
            rows.append(("Sharpness", f"{float(sharp):,.0f}"))

        remaining = {k.upper(): (k, v) for k, v in header.items()
                     if k.upper() not in DETAIL_SKIP}
        for key in DETAIL_ORDER:
            entry = remaining.pop(key, None)
            if entry is not None:
                rows.append((entry[0], str(entry[1])))
        for key, value in remaining.values():
            rows.append((key, str(value)))

        if not header:
            # No file to read a header from: show what the camera did publish.
            for key, value in meta.items():
                if key not in META_SHOWN and value not in (None, ""):
                    rows.append((str(key), str(value)))
        return rows

    @staticmethod
    def _measured(payload):
        stats = payload.get("stats") or {}
        if not stats:
            return []
        rows = []
        shape = "×".join(str(x) for x in stats.get("shape", []))
        if shape:
            rows.append(("Size", f"{shape} px"))
        if stats.get("dtype"):
            rows.append(("Data type", str(stats["dtype"])))
        if stats.get("min") is not None:
            rows.append(("Min / max / mean",
                         f"{stats['min']:.0f} / {stats['max']:.0f} / "
                         f"{stats.get('mean')}"))
        if stats.get("saturated_pct") is not None:
            rows.append(("Saturated", f"{stats['saturated_pct']} %"))
        rows.append(("Scale", f"0–{int(full_scale_of(payload))}"))
        return rows


def full_scale_of(payload):
    stats = payload.get("stats") or {}
    return payload.get("full_scale") or stats.get("full_scale") or 65535


class LanPanel(QWidget):
    status = pyqtSignal(str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._tasks = TaskRunner(self)
        self._discovery = None
        self._client = None
        self._frames = []
        self._total = 0
        self._sessions = 0
        self._current_name = None
        self._camera_type = None
        self._mode = None
        # What the camera says it is observing, from /api/info; beats guessing the period.
        self._schedule = {}
        self._grouped = []
        self._difference = None          # the second window, created on demand
        self._difference_target = None   # (session, cycle index, filter)
        self._build_ui()

        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(AUTO_REFRESH_MS)
        self._auto_timer.timeout.connect(self._refresh_latest)

        # ``(total, newest name)`` of the last listing drawn, compared by the poll.
        self._archive_signature = None
        self._poll_in_flight = False
        self._poll_failed = False
        # The first drawing opens the newest frame; later ones leave the observer's
        # choices alone.
        self._drawn = False
        self._list_timer = QTimer(self)
        self._list_timer.setInterval(LIST_REFRESH_MS)
        self._list_timer.timeout.connect(self._poll_archive)

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

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
        self.le_manual.setPlaceholderText("host, host:port or tunnel URL")
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
        # The camera's days are UTC. A night through midnight stays whole only on "all
        # dates": a single-day request cannot contain its other half.
        self.cmb_date.setToolTip(
            "Ask the camera for one UTC calendar day. Leave on “all dates” to see "
            "whole nights, including those that run past midnight.")
        self.cmb_date.addItem("all dates", None)
        self.cmb_date.currentIndexChanged.connect(self._reload_frames)
        row.addWidget(self.cmb_date, 1)
        btn_reload = QPushButton("Reload")
        btn_reload.setToolTip(
            "Re-read the archive now. New frames arrive on their own every few "
            "seconds; this is for when the camera's directory changed some other "
            "way, such as files removed by hand.")
        btn_reload.clicked.connect(self._reload_all)
        row.addWidget(btn_reload)
        arch_lay.addLayout(row)
        arch_lay.addWidget(self._build_cycle_controls())

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.currentItemChanged.connect(self._on_frame_selected)
        arch_lay.addWidget(self.tree, 1)

        self.lbl_count = QLabel("—")
        self.lbl_count.setStyleSheet("color:#888; font-size:11px;")
        arch_lay.addWidget(self.lbl_count)
        left_lay.addWidget(arch_box, 1)
        splitter.addWidget(left)

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

        self.cb_details = QCheckBox("Frame info")
        self.cb_details.setToolTip("Show the full header and the histogram")
        self.cb_details.setChecked(True)
        self.cb_details.stateChanged.connect(
            lambda state: self.details.setVisible(bool(state)))
        bar.addWidget(self.cb_details)

        bar.addStretch()
        self.btn_save = QPushButton("Save original…")
        self.btn_save.setToolTip("Download the untouched capture file")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._on_save)
        bar.addWidget(self.btn_save)
        right_lay.addLayout(bar)

        self.view = ImageView()
        right_lay.addWidget(self.view, 1)
        splitter.addWidget(right)

        self.details = FrameInfoPanel()
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([300, 780, 280])

    def _build_cycle_controls(self):
        self.cycle_box = QWidget()
        box = QVBoxLayout(self.cycle_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)

        self.cb_cycles = QCheckBox("Group by cycle and filter")
        self.cb_cycles.setToolTip(
            "Split each night into iterations of the general measuring cycle, and "
            "each cycle by filter. Only meaningful for frames taken in time mode.")
        box.addWidget(self.cb_cycles)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.cb_manual = QCheckBox("Period")
        self.cb_manual.setToolTip(
            "Set the cycle period yourself instead of letting the viewer work it "
            "out from the frames")
        row.addWidget(self.cb_manual)

        self.spin_period = QDoubleSpinBox()
        self.spin_period.setRange(1.0, 86400.0)
        self.spin_period.setDecimals(1)
        self.spin_period.setSingleStep(10.0)
        self.spin_period.setSuffix(" s")
        row.addWidget(self.spin_period, 1)

        self.time_anchor = QTimeEdit()
        self.time_anchor.setDisplayFormat("HH:mm:ss")
        # UTC, because the file names are UTC and this viewer never learns the
        # station's offset — the camera's own ``t_start`` is its local time.
        self.time_anchor.setToolTip("Start of the first cycle, UTC")
        row.addWidget(self.time_anchor)
        box.addLayout(row)

        self.cb_difference = QCheckBox("Filter difference")
        self.cb_difference.setToolTip(
            "Selecting a filter inside a cycle opens a second window with\n"
            "    first − (previous cycle's last · k0 + this cycle's last · k1) / 2\n"
            "for that filter. Nothing is shown for the first cycle of a night "
            "or for a cycle still being shot.")
        box.addWidget(self.cb_difference)

        self._load_cycle_settings()
        self.cb_difference.stateChanged.connect(self._on_difference_toggled)
        self.cb_cycles.stateChanged.connect(self._on_grouping_changed)
        # Separate from the above: touching any of these takes the period over from the
        # camera's schedule.
        self.cb_manual.stateChanged.connect(self._on_manual_changed)
        self.spin_period.valueChanged.connect(self._on_manual_changed)
        self.time_anchor.timeChanged.connect(self._on_manual_changed)
        self._sync_cycle_controls()
        self.cycle_box.setVisible(False)
        return self.cycle_box

    def _load_cycle_settings(self):
        saved = self._settings.get("japan_grouping") or {}
        self.cb_cycles.setChecked(bool(saved.get("enabled")))
        self.cb_manual.setChecked(bool(saved.get("manual")))
        self.cb_difference.setChecked(bool(saved.get("difference")))
        self._period_by_hand = bool(saved.get("manual_by_hand"))
        try:
            self.spin_period.setValue(float(saved.get("period") or 160.0))
        except (TypeError, ValueError):
            self.spin_period.setValue(160.0)
        anchor = QTime.fromString(str(saved.get("anchor_utc") or ""), "HH:mm:ss")
        self.time_anchor.setTime(anchor if anchor.isValid() else QTime(11, 0, 0))

    def _save_cycle_settings(self):
        self._settings["japan_grouping"] = {
            "enabled": self.cb_cycles.isChecked(),
            "manual": self.cb_manual.isChecked(),
            "period": float(self.spin_period.value()),
            "anchor_utc": self.time_anchor.time().toString("HH:mm:ss"),
            "difference": self.cb_difference.isChecked(),
            "manual_by_hand": self._period_by_hand,
        }
        save_settings(self._settings)

    def _sync_cycle_controls(self):
        grouping = self.cb_cycles.isChecked()
        manual = grouping and self.cb_manual.isChecked()
        self.cb_manual.setEnabled(grouping)
        self.spin_period.setEnabled(manual)
        self.time_anchor.setEnabled(manual)
        self.cb_difference.setEnabled(grouping)
        if not grouping:
            self._close_difference()

    def _on_manual_changed(self):
        self._period_by_hand = True
        self._on_grouping_changed()

    def _on_grouping_changed(self):
        self._sync_cycle_controls()
        self._save_cycle_settings()
        if (self.cb_cycles.isChecked() and self._mode
                and self._mode != "time"):
            self.status.emit(
                f"This camera is in {self._mode} mode right now — cycles only "
                "describe frames taken in time mode.")
        # The data is in hand, so no new request. A different tree opens like a first
        # drawing rather than restoring the shape just left.
        self._rebuild_tree(preserve=False)

    def _cycle_grouping_on(self):
        return (self._camera_type == CYCLE_CAMERA_TYPE
                and self.cb_cycles.isChecked())

    def _manual_cycle_settings(self):
        if not self.cb_manual.isChecked():
            return None, None
        chosen = self.time_anchor.time()
        return (float(self.spin_period.value()),
                time_of_day(chosen.hour(), chosen.minute(), chosen.second()))

    def _adopt_camera_period(self):
        """Prefer the camera's reported schedule to guessing from frames, which a lost slot
        or a filter shot twice can move. An operator's override still wins.
        """
        if self._camera_type != CYCLE_CAMERA_TYPE:
            return
        period = float(self._schedule.get("period") or 0.0)
        anchor = str(self._schedule.get("t_start_utc") or "")
        if period <= 0 or not anchor:
            return
        if self._period_by_hand:
            return          # the operator set these; leave them alone
        parsed = QTime.fromString(anchor, "HH:mm:ss")
        if not parsed.isValid():
            return
        for widget in (self.cb_manual, self.spin_period, self.time_anchor):
            widget.blockSignals(True)
        self.spin_period.setValue(period)
        self.time_anchor.setTime(parsed)
        self.cb_manual.setChecked(True)
        for widget in (self.cb_manual, self.spin_period, self.time_anchor):
            widget.blockSignals(False)
        self._sync_cycle_controls()
        if self._difference is not None:
            self._difference.set_window_seconds(period)

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
        # Node name first: an observer looks for a place, not an instance id.
        for node in sorted(nodes, key=lambda n: (node_name_of(n).lower(),
                                                 str(n.get("instance_name")))):
            host = node.get("host")
            port = node.get("http_port", DEFAULT_HTTP_PORT)
            self.cmb_cameras.addItem(node_label(node, DEFAULT_HTTP_PORT),
                                     (host, port))
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
        host, port, _ = parse_address(text, DEFAULT_HTTP_PORT)
        self.connect_to(host, port)

    def connect_to(self, host, port=DEFAULT_HTTP_PORT, via=None):
        if "://" in str(host):
            host, port, _ = parse_address(host, port)
        self._client = CameraClient(host, port, via=via)
        if "://" not in host:
            # A tunnel address lasts one run; remembered it would mislead.
            self._remember(host, port)
        self.lbl_camera.setText(f"connecting to {self._client}…")
        # The switch belongs to a camera type: hide it while the next camera identifies itself.
        self._camera_type = None
        self._mode = None
        self.cycle_box.setVisible(False)
        # Left running, the poll would compare the next camera's archive to this listing.
        self._list_timer.stop()
        self._archive_signature = None
        self._poll_failed = False
        self._drawn = False
        # The previous frame stays on screen for a moment; its header must not, or the
        # two read as one frame.
        self._clear_stats()
        client = self._client
        self._tasks.run(client.info, self._for(client, self._on_info), self._on_error)

    def _for(self, client, callback, name=False):
        """Each request runs on its own thread, so answers can arrive out of order: one
        meant for an earlier camera, or frame, is dropped. ``name``: the frame it is
        for (None is the latest frame)."""
        def deliver(*args):
            if client is not self._client:
                return
            if name is not False and name != self._current_name:
                return
            callback(*args)
        return deliver

    def _remember(self, host, port):
        recent = [tuple(x) for x in self._settings.get("recent_hosts", [])]
        entry = (host, int(port))
        recent = [entry] + [r for r in recent if r != entry]
        self._settings["recent_hosts"] = [list(r) for r in recent[:8]]
        save_settings(self._settings)

    def _on_info(self, info):
        self.lbl_camera.setText(
            f"<b>{info.get('instance_name', '?')}</b> "
            f"({info.get('camera_type', '?')})<br>{node_name_of(info)}"
            f"<br>{info.get('output_dir') or 'no archive directory'}")
        self._camera_type = info.get("camera_type")
        self._schedule = info.get("schedule") or {}
        self._adopt_camera_period()
        self.cycle_box.setVisible(self._camera_type == CYCLE_CAMERA_TYPE)
        if not info.get("archive_available"):
            self.status.emit(
                "Connected, but this camera has no archive directory "
                "configured — only the latest frame is available.")
        if self._camera_type == CYCLE_CAMERA_TYPE:
            # The current mode does not decide the grouping (a directory can hold nights of
            # both), but a disagreement is worth saying.
            client = self._client
            self._tasks.run(client.status, self._for(client, self._on_status),
                            lambda msg: None)
        self._reload_all()
        if info.get("archive_available"):
            self._list_timer.start()

    def _on_status(self, status):
        self._mode = (status or {}).get("mode")

    def _on_error(self, message):
        self.status.emit(message)

    def _reload_all(self):
        if not self._client:
            return
        client = self._client
        self._tasks.run(client.dates, self._for(client, self._on_dates), self._on_error)

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
        self._tasks.run(lambda: client.frames(date=date, limit=ARCHIVE_LIMIT),
                        self._for(client, self._on_frames), self._on_error)

    def _on_frames(self, listing):
        self._frames = listing.get("frames", [])
        self._total = listing.get("total", len(self._frames))
        self._archive_signature = self._signature_of(listing)
        self._rebuild_tree()

    @staticmethod
    def _signature_of(listing):
        """The total alone misses a night trimmed and reshot to the same count; the newest
        name alone misses frames removed behind it.
        """
        frames = listing.get("frames") or []
        return (listing.get("total", len(frames)),
                frames[0]["name"] if frames else None)

    def _poll_archive(self):
        """A full listing is every frame and a rebuild disturbs the tree, so ``limit=1`` asks
        for the same ``total`` and newest frame; only a real difference reloads.
        """
        if not self._client or self._poll_in_flight:
            return
        client = self._client
        date = self.cmb_date.currentData()
        self._poll_in_flight = True

        def _done(listing):
            self._poll_in_flight = False
            if self._poll_failed:
                self._poll_failed = False
                self.status.emit("Archive reachable again.")
            if self._signature_of(listing) != self._archive_signature:
                self._reload_all()

        def _failed(message):
            self._poll_in_flight = False
            # Once per outage: a rebooting camera must not flood the status bar.
            if not self._poll_failed:
                self._poll_failed = True
                self.status.emit(f"Archive not answering: {message}")

        self._tasks.run(lambda: client.frames(date=date, limit=1), _done, _failed)

    def _frame_item(self, frame):
        stamp, from_name = entry_timestamp(frame)
        if stamp is None:
            when = "time unknown"
        elif from_name:
            # File names are UTC on every camera; saying so keeps it apart from the local mtime.
            when = f"{stamp:%H:%M:%S} UTC"
        else:
            when = f"{stamp:%Y-%m-%d %H:%M:%S} local"
        item = QTreeWidgetItem(
            [f"{frame['name']}\n    {when} · {frame['size'] // 1024} KB"])
        item.setData(0, Qt.UserRole, frame["name"])
        return item

    @staticmethod
    def _group_item(parent, text, key=None):
        item = QTreeWidgetItem(parent, [text])
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        if key is not None:
            item.setData(0, GROUP_ROLE, key)
        return item

    def _rebuild_tree(self, preserve=True):
        """Runs while the observer reads the tree, so open rows, scroll position and selection
        survive. ``preserve=False`` is for a regrouping, where there is nothing to put back.
        """
        previous = self._current_name
        opened = self._expanded_keys() if preserve else set()
        scrolled = self.tree.verticalScrollBar().value()

        self.tree.blockSignals(True)
        self.tree.clear()
        if self._cycle_grouping_on():
            note = self._fill_cycle_tree(opened)
        else:
            note = self._fill_session_tree(opened)
        self.tree.blockSignals(False)

        self.lbl_count.setText(" · ".join(self._count_parts(note)))
        if not self._frames:
            self.view.show_message("No frames in this archive")
            self.btn_save.setEnabled(False)
            return
        # Opening the newest frame is for a first drawing only: later ones must not override
        # the observer's choice, including the live view, which holds no selection.
        self._restore_selection(previous, default=not preserve or not self._drawn)
        self._drawn = True
        if opened:
            self._restore_expanded(opened)
            self.tree.verticalScrollBar().setValue(scrolled)

    def _walk(self):
        stack = [self.tree.topLevelItem(i)
                 for i in range(self.tree.topLevelItemCount() - 1, -1, -1)]
        while stack:
            item = stack.pop()
            yield item
            for index in range(item.childCount() - 1, -1, -1):
                stack.append(item.child(index))

    def _expanded_keys(self):
        return {item.data(0, GROUP_ROLE) for item in self._walk()
                if item.isExpanded() and item.data(0, GROUP_ROLE) is not None}

    def _restore_expanded(self, opened):
        for item in self._walk():
            key = item.data(0, GROUP_ROLE)
            if key is not None and key in opened:
                item.setExpanded(True)

    def _session_item(self, session):
        """One row per measuring session, keyed by where the night began: the label gains a
        second date past midnight, and the row must not collapse then.
        """
        return self._group_item(
            self.tree,
            f"{session['label']} · {len(session['frames'])} frame(s)",
            key=("session", session.get("start")))

    def _fill_session_tree(self, opened=None):
        sessions = group_by_session(self._frames)
        for session in sessions:
            item = self._session_item(session)
            for frame in session["frames"]:
                item.addChild(self._frame_item(frame))
        self._sessions = len(sessions)
        # Only when nothing is open: re-opening the newest night would fight the observer.
        if sessions and not opened:
            self.tree.topLevelItem(0).setExpanded(True)
        return ""

    def _fill_cycle_tree(self, opened=None):
        period, anchor = self._manual_cycle_settings()
        sessions = group_japan_cycles(self._frames, period=period, anchor=anchor)
        self._sessions = len(sessions)
        # Kept so the difference frame uses the grouping on screen, not one of its own.
        self._grouped = sessions
        for number, session in enumerate(sessions):
            item = self._session_item(session)
            start = session.get("start")
            for cycle in session["cycles"]:
                label = (f"Cycle {cycle['index']} · "
                         f"{cycle['first']:%H:%M:%S}–{cycle['last']:%H:%M:%S} UTC"
                         f" · {cycle['count']} frame(s)")
                cycle_item = self._group_item(
                    item, label, key=("cycle", start, cycle["index"]))
                for group in cycle["filters"]:
                    self._fill_filter_group(cycle_item, group, start,
                                            cycle["index"],
                                            (number, cycle["index"],
                                             group["filter"]))
            for group in session.get("filters", []):
                self._fill_filter_group(item, group, start, None)
            # Darks stand outside the cycle: the driver takes them once per
            # unique exposure before and after the run, not on a slot.
            if session["darks"]:
                darks_item = self._group_item(
                    item, f"Darks · {len(session['darks'])} frame(s)",
                    key=("darks", start))
                for frame in session["darks"]:
                    darks_item.addChild(self._frame_item(frame))
            if session["unparsed"]:
                other = self._group_item(
                    item, f"Other files · {len(session['unparsed'])}",
                    key=("other", start))
                for frame in session["unparsed"]:
                    other.addChild(self._frame_item(frame))

        if sessions and not opened:
            first = self.tree.topLevelItem(0)
            first.setExpanded(True)
            if first.childCount():
                first.child(0).setExpanded(True)
        return self._period_note(sessions)

    def _fill_filter_group(self, parent, group, start, cycle_index, target=None):
        item = self._group_item(
            parent, f"{japan_filter_label(group['filter'])} · "
                    f"{len(group['frames'])} frame(s)",
            key=("filter", start, cycle_index, group["filter"]))
        # Only a filter *inside a cycle* can carry a difference frame; the
        # per-session filter groups exist precisely because no cycle was found.
        if target is not None:
            item.setData(0, FILTER_ROLE, target)
        for frame in group["frames"]:
            item.addChild(self._frame_item(frame))

    @staticmethod
    def _period_note(sessions):
        if not sessions:
            return ""
        if not sessions[0]["period_auto"]:
            return f"period {sessions[0]['period']:g} s (set)"
        found = sorted({s["period"] for s in sessions if s["period"]})
        if not found:
            return "no cycle found — set the period"
        shown = "/".join(f"{value:g}" for value in found[:3])
        return f"period {shown} s (auto)"

    def _count_parts(self, note):
        shown, total = len(self._frames), self._total
        parts = [f"{shown} of {total} frame(s)" if shown < total
                 else f"{total} frame(s)"]
        if self._sessions:
            parts.append(f"{self._sessions} session(s)")
        if note:
            parts.append(note)
        if shown < total:
            parts.append("newest only — pick a date")
        return parts

    def _restore_selection(self, name, default=True):
        """A surviving selection is restored with the signal blocked, so no reload.

        ``default=False`` leaves an empty selection empty: that is what "Follow live" leaves,
        and selecting a frame would fire ``_on_frame_selected`` and switch it off.
        """
        wanted = self._find_frame_item(name) if name else None
        if wanted is not None:
            self.tree.blockSignals(True)
            self._expand_to(wanted)
            self.tree.setCurrentItem(wanted)
            self.tree.blockSignals(False)
            self.tree.scrollToItem(wanted)
            return
        if not default:
            return
        first = self._find_frame_item(None)
        if first is not None:
            self._expand_to(first)
            self.tree.setCurrentItem(first)

    def _find_frame_item(self, name):
        for item in self._walk():
            value = item.data(0, Qt.UserRole)
            if value is not None and (name is None or value == name):
                return item
        return None

    @staticmethod
    def _expand_to(item):
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()

    def _on_frame_selected(self, item, previous=None):
        # Group rows carry no name, so selecting one leaves the picture alone. A filter
        # row inside a cycle is the exception: it names a difference frame.
        target = item.data(0, FILTER_ROLE) if item is not None else None
        if target is not None:
            self._difference_target = tuple(target)
            self._request_difference()
            return
        name = item.data(0, Qt.UserRole) if item is not None else None
        if not name or not self._client:
            return
        self.cb_auto.setChecked(False)
        self._current_name = name
        self._load_frame(name)

    def _on_difference_toggled(self, state):
        self._save_cycle_settings()
        if state:
            self._request_difference()
        else:
            self._close_difference()

    def _difference_dialog(self):
        if self._difference is None:
            self._difference = CompositeWindow(self.window())
            self._difference.set_window_seconds(self._default_window())
            self._difference.window_changed.connect(
                lambda _value: self._request_difference())
        return self._difference

    def _default_window(self):
        for session in (self._grouped or []):
            if session.get("period"):
                return float(session["period"])
        return float(self._schedule.get("period") or 0.0)

    def _request_difference(self):
        if not (self.cb_difference.isChecked() and self.cb_difference.isEnabled()):
            return
        if not self._client or not self._difference_target:
            return
        number, cycle_index, filter_num = self._difference_target
        sessions = self._grouped or []
        if number >= len(sessions):
            return
        first, previous, last, reason = composite.pick_composite_frames(
            sessions[number], cycle_index, filter_num)
        label = (f"{japan_filter_label(filter_num)} · cycle {cycle_index} · "
                 f"{sessions[number]['label']}")
        if reason:
            # An ordinary state, not a failure: no window, and the reason in the status bar.
            self.status.emit(f"No difference frame — {reason}")
            if self._difference is not None and self._difference.isVisible():
                self._difference.show_error(reason)
            return

        dialog = self._difference_dialog()
        window = dialog.window_seconds()
        if window <= 0:
            return
        if dialog.isVisible():
            dialog.show_loading(label)
        client = self._client
        stretch = self.cmb_stretch.currentData()
        names = (first["name"], previous["name"], last["name"])
        self._tasks.run(
            lambda: client.composite(*names, window=window, stretch=stretch),
            lambda result: self._on_difference(result, names, label),
            lambda message: self._on_difference_failed(message, label))

    def _on_difference(self, result, names, label):
        target = self._difference_target
        if target is None:
            return
        jpeg, headers = result
        dialog = self._difference_dialog()
        dialog.show_result(jpeg, headers, label)
        if not dialog.isVisible():
            dialog.place_beside(self.window())
            dialog.show()
        self.status.emit(f"Difference frame — {label}")

    def _on_difference_failed(self, message, label):
        self.status.emit(f"No difference frame for {label}: {message}")
        if self._difference is not None and self._difference.isVisible():
            self._difference.show_error(message)

    def _close_difference(self):
        if self._difference is not None:
            self._difference.hide()

    def _reload_current(self):
        # Same contrast as the main window: two different stretches side by side would
        # invite false comparisons.
        self._request_difference()
        if self._current_name:
            self._load_frame(self._current_name)
        elif self.cb_auto.isChecked() or self.view.has_frame():
            self._refresh_latest()

    def _load_frame(self, name):
        client = self._client
        stretch = self.cmb_stretch.currentData()
        self.view.show_message(f"Loading {name}…")
        self._tasks.run(lambda: client.frame_jpeg(name, stretch=stretch),
                        self._for(client, lambda data: self._show_frame(data, name),
                                  name=name),
                        self._on_error)
        self._tasks.run(lambda: client.frame_stats(name),
                        self._for(client, lambda payload: self._show_stats(payload, name),
                                  name=name),
                        lambda msg: None)

    def _show_frame(self, data, name):
        if self.view.set_jpeg(data):
            self.btn_save.setEnabled(name is not None)
            self.status.emit(f"Showing {name or 'latest frame'}")

    def _show_stats(self, payload, name=None):
        self.details.show_payload(payload, name)

    def _clear_stats(self):
        self.details.clear()

    def _refresh_latest(self):
        if not self._client:
            return
        client = self._client
        stretch = self.cmb_stretch.currentData()
        self._current_name = None
        self.btn_save.setEnabled(False)
        self._tasks.run(lambda: client.latest_jpeg(stretch=stretch),
                        self._for(client, self._on_latest, name=None), self._on_error)
        # Without this the panel keeps describing the last archived frame, which looks current.
        self._tasks.run(client.frame_stats,
                        self._for(client, self._show_stats, name=None),
                        lambda msg: None)

    def _on_latest(self, result):
        data, timestamp = result
        if self.view.set_jpeg(data):
            self.status.emit(f"Latest frame{f' — {timestamp}' if timestamp else ''}")
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(None)
            self.tree.blockSignals(False)

    def _on_auto_toggled(self, state):
        if state:
            self._refresh_latest()
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

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
        self._list_timer.stop()
        if self._difference is not None:
            self._difference.close()
            self._difference = None
        self._tasks.wait_all()


class ViewerWindow(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.setWindowTitle("Every Camera — Viewer")
        self.resize(1150, 750)
        self.setMinimumSize(760, 480)

        self._settings = settings
        self.lan = LanPanel(settings)
        self.setCentralWidget(self.lan)

        self.setStatusBar(QStatusBar())
        self.lan.status.connect(self._show_status)
        self._show_status("Ready")

    def _show_status(self, message):
        self.statusBar().showMessage(message, 15000)

    def closeEvent(self, event):
        self.lan.cleanup()
        save_settings(self._settings)
        event.accept()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Every Camera — frame viewer (read-only)")
    parser.add_argument("--host", help="Connect to this camera straight away")
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--via", default="", metavar="HOST:PORT",
                        help="Reach --host through this camera, which fetches on our behalf")
    args = parser.parse_args()

    # Qt/OpenCV plugin clash, same workaround as the other GUIs
    try:
        import PyQt5 as _pyqt5
        plugins = os.path.join(os.path.dirname(_pyqt5.__file__), "Qt5", "plugins")
        if os.path.isdir(plugins):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = plugins
    except Exception:
        os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)

    settings = load_settings()

    app = QApplication(sys.argv)
    import qt_support
    qt_support.install_excepthook()
    app.setStyle("Fusion")
    window = ViewerWindow(settings)
    window.show()

    if args.host:
        window.lan.connect_to(args.host, args.port, via=args.via or None)
    else:
        QTimer.singleShot(200, window.lan.discover)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
