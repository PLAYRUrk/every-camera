"""
Camera Monitor GUI — displays live status of all running camera instances.
Supports MQTT (remote) and local file modes.
Allows requesting last frame from running instances.
"""
import os
import json
import glob
import base64

from datetime import datetime as dt

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit,
    QTableWidget, QTableWidgetItem,
    QFileDialog, QHeaderView, QAbstractItemView,
    QGroupBox, QTabWidget, QCheckBox, QDialog,
    QMessageBox, QSizePolicy,
)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QColor, QFont, QImage, QPixmap

from mqtt_client import MQTT_AVAILABLE

from utils import HOME_STATUS_DIR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STALE_THRESHOLD_SECONDS = 30
LOCAL_REFRESH_INTERVAL_MS = 5000
FRAME_REQUEST_TIMEOUT_MS = 10000
ON_DEMAND_TIMEOUT_MS = 60000

TABLE_COLUMNS = [
    "Instance Name", "Type", "PID", "Status",
    "Shots", "Last Shot", "Active Until",
    "Errors", "Extra Info", "Last Update",
]

STATUS_COLORS = {
    "running": QColor(0, 150, 0),
    "waiting": QColor(180, 120, 0),
    "error":   QColor(200, 0, 0),
    "stopped": QColor(120, 120, 120),
    "idle":    QColor(80, 80, 200),
    "stale":   QColor(200, 80, 0),
    "unknown": QColor(100, 100, 100),
}


# ---------------------------------------------------------------------------
# Frame viewer dialog
# ---------------------------------------------------------------------------
class FrameViewerDialog(QDialog):
    """Dialog to display a received camera frame."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Last Frame")
        self.resize(800, 600)
        lay = QVBoxLayout(self)

        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("font-weight:bold; font-size:12px;")
        lay.addWidget(self.lbl_info)

        self.lbl_image = QLabel("Waiting for frame...")
        self.lbl_image.setAlignment(Qt.AlignCenter)
        self.lbl_image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.lbl_image.setStyleSheet("background-color: #1a1a1a; color: #888;")
        lay.addWidget(self.lbl_image, 1)

    def show_frame(self, instance_name, camera_type, jpeg_data, timestamp=None):
        """Display a JPEG frame."""
        qimg = QImage()
        if qimg.loadFromData(jpeg_data):
            pixmap = QPixmap.fromImage(qimg)
            scaled = pixmap.scaled(self.lbl_image.size(),
                                   Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.lbl_image.setPixmap(scaled)
        else:
            self.lbl_image.setText("Failed to decode image")

        ts = timestamp or "?"
        self.lbl_info.setText(
            f"{instance_name}  |  {camera_type.upper()}  |  {ts}")
        self.setWindowTitle(f"Last Frame — {instance_name}")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-scale pixmap on resize
        pm = self.lbl_image.pixmap()
        if pm and not pm.isNull():
            self.lbl_image.setPixmap(
                pm.scaled(self.lbl_image.size(),
                          Qt.KeepAspectRatio, Qt.SmoothTransformation))


class CaptureParamsDialog(QDialog):
    """Dialog to enter capture params for on-demand frame request."""

    def __init__(self, instance_name, camera_type, parent=None):
        super().__init__(parent)
        self.camera_type = (camera_type or "").lower()
        self.setWindowTitle(f"Capture from {instance_name}")
        self.resize(360, 220)
        from PyQt5.QtWidgets import QFormLayout, QDialogButtonBox

        lay = QVBoxLayout(self)
        lbl = QLabel(
            f"<b>{instance_name}</b> ({self.camera_type.upper() or 'unknown'})"
            "<br>Leave a field blank to keep current value.")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        form = QFormLayout()
        self._fields = {}

        if self.camera_type == "cannon":
            for key, placeholder in [
                ("iso", "e.g. 100, 400, 1600"),
                ("shutterspeed", "e.g. 1/125, 1, 30"),
                ("aperture", "e.g. 5.6"),
                ("imageformat", "e.g. Large Fine JPEG"),
                ("whitebalance", "e.g. Daylight"),
            ]:
                le = QLineEdit()
                le.setPlaceholderText(placeholder)
                form.addRow(key, le)
                self._fields[key] = le
        elif self.camera_type == "sptt":
            for key, placeholder in [
                ("exposure", "seconds, e.g. 0.88"),
                ("gain", "0..255, e.g. 100"),
                ("binning", "0 (1x1), 1 (2x2), 3 (4x4)"),
                ("encoding", "8bit or 12bit"),
            ]:
                le = QLineEdit()
                le.setPlaceholderText(placeholder)
                form.addRow(key, le)
                self._fields[key] = le
        elif self.camera_type == "infra":
            for key, placeholder in [
                ("exposure_us", "microseconds, e.g. 10000"),
                ("gain", "e.g. 1"),
                ("roi_width", "pixels, e.g. 1280"),
                ("roi_height", "pixels, e.g. 1024"),
            ]:
                le = QLineEdit()
                le.setPlaceholderText(placeholder)
                form.addRow(key, le)
                self._fields[key] = le
        else:
            form.addRow(QLabel("Unknown camera type — no fields available."))

        lay.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def get_params(self):
        params = {}
        for key, field in self._fields.items():
            val = field.text().strip()
            if not val:
                continue
            # Type coercion for numeric fields
            int_keys = {
                "sptt": {"gain", "binning"},
                "infra": {"gain", "roi_width", "roi_height"},
            }.get(self.camera_type, set())
            float_keys = {
                "sptt": {"exposure"},
                "infra": {"exposure_us"},
            }.get(self.camera_type, set())
            if key in int_keys:
                try:
                    params[key] = int(val)
                except ValueError:
                    params[key] = val
            elif key in float_keys:
                try:
                    params[key] = float(val)
                except ValueError:
                    params[key] = val
            else:
                params[key] = val
        return params


# ---------------------------------------------------------------------------
# Shared table logic
# ---------------------------------------------------------------------------
def make_table():
    table = QTableWidget(0, len(TABLE_COLUMNS))
    table.setHorizontalHeaderLabels(TABLE_COLUMNS)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    hdr = table.horizontalHeader()
    hdr.setSectionResizeMode(0, QHeaderView.Stretch)
    for col in range(1, len(TABLE_COLUMNS)):
        hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
    bold = QFont()
    bold.setBold(True)
    table.horizontalHeader().setFont(bold)
    return table


def status_display(rec):
    status = rec.get("status", "unknown")
    last_update_str = rec.get("last_update")
    if status in ("running", "waiting") and last_update_str:
        try:
            last_update = dt.fromisoformat(last_update_str)
            age = (dt.now() - last_update).total_seconds()
            if age > STALE_THRESHOLD_SECONDS:
                return "STALE", STATUS_COLORS["stale"]
        except (ValueError, TypeError):
            pass
    return status.upper(), STATUS_COLORS.get(status, STATUS_COLORS["unknown"])


def fmt_dt(iso_str):
    if not iso_str:
        return "\u2014"
    try:
        return dt.fromisoformat(iso_str).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(iso_str)


def _extra_info(rec):
    """Build short extra info string from monitoring data."""
    parts = []
    cam_type = rec.get("camera_type", "")
    if cam_type == "cannon":
        if rec.get("iso"):
            parts.append(f"ISO:{rec['iso']}")
        if rec.get("shutterspeed"):
            parts.append(f"SS:{rec['shutterspeed']}")
    elif cam_type == "sptt":
        if rec.get("exposure_s") is not None:
            parts.append(f"Exp:{rec['exposure_s']}s")
        if rec.get("gain") is not None:
            parts.append(f"G:{rec['gain']}")
        if rec.get("frame_size"):
            parts.append(rec["frame_size"])
        if rec.get("cam_temp_ccd") is not None:
            parts.append(f"T:{rec['cam_temp_ccd']}\u00b0")
    # System info
    sys_info = rec.get("system", {})
    if sys_info.get("disk_free_mb") is not None:
        parts.append(f"Disk:{sys_info['disk_free_mb']}MB")
    if sys_info.get("mem_used_pct") is not None:
        parts.append(f"Mem:{sys_info['mem_used_pct']}%")
    return "  ".join(parts)


def populate_table(table, records):
    table.setRowCount(len(records))
    for row, rec in enumerate(records):
        st_text, st_color = status_display(rec)

        def item(text, color=None, bold=False):
            it = QTableWidgetItem(str(text))
            it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            if color:
                it.setForeground(color)
            if bold:
                f = QFont()
                f.setBold(True)
                it.setFont(f)
            return it

        table.setItem(row, 0, item(rec.get("instance_name", "?")))
        table.setItem(row, 1, item(rec.get("camera_type", "?").upper()))
        table.setItem(row, 2, item(rec.get("pid", "?")))
        table.setItem(row, 3, item(st_text, st_color, bold=True))
        table.setItem(row, 4, item(rec.get("shots_taken", 0)))
        table.setItem(row, 5, item(fmt_dt(rec.get("last_shot"))))
        table.setItem(row, 6, item(fmt_dt(rec.get("active_until"))))
        errors = rec.get("errors", 0)
        table.setItem(row, 7, item(errors,
                                   STATUS_COLORS["error"] if errors > 0 else None))
        table.setItem(row, 8, item(_extra_info(rec)))
        table.setItem(row, 9, item(fmt_dt(rec.get("last_update"))))


# ---------------------------------------------------------------------------
# MQTT tab
# ---------------------------------------------------------------------------
class MqttTab(QWidget):
    def __init__(self, mqtt_cfg=None):
        super().__init__()
        self._subscriber = None
        self._instances = {}
        self._mqtt_cfg = mqtt_cfg or {}
        self._frame_viewer = None
        self._pending_frame_instance = None
        self._frame_timeout_timer = QTimer(self)
        self._frame_timeout_timer.setSingleShot(True)
        self._frame_timeout_timer.timeout.connect(self._on_frame_timeout)
        self._build_ui()
        self._load_config()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        cfg_box = QGroupBox("MQTT Broker Settings")
        cfg_grid = QGridLayout(cfg_box)
        cfg_grid.setColumnStretch(1, 1)

        cfg_grid.addWidget(QLabel("Broker host:"), 0, 0)
        self.le_host = QLineEdit("broker.hivemq.com")
        cfg_grid.addWidget(self.le_host, 0, 1)

        cfg_grid.addWidget(QLabel("Port:"), 0, 2)
        self.le_port = QLineEdit("1883")
        self.le_port.setMaximumWidth(70)
        cfg_grid.addWidget(self.le_port, 0, 3)

        cfg_grid.addWidget(QLabel("Username:"), 1, 0)
        self.le_user = QLineEdit()
        self.le_user.setPlaceholderText("(optional)")
        cfg_grid.addWidget(self.le_user, 1, 1)

        cfg_grid.addWidget(QLabel("Password:"), 1, 2)
        self.le_pass = QLineEdit()
        self.le_pass.setEchoMode(QLineEdit.Password)
        self.le_pass.setPlaceholderText("(optional)")
        cfg_grid.addWidget(self.le_pass, 1, 3)

        cfg_grid.addWidget(QLabel("Topic prefix:"), 2, 0)
        self.le_prefix = QLineEdit("every_camera")
        cfg_grid.addWidget(self.le_prefix, 2, 1)

        self.cb_tls = QCheckBox("TLS (port 8883)")
        self.cb_tls.stateChanged.connect(
            lambda s: self.le_port.setText("8883" if s else "1883")
        )
        cfg_grid.addWidget(self.cb_tls, 3, 0, 1, 2)

        btn_row = QHBoxLayout()
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        self.lbl_conn = QLabel("Disconnected")
        self.lbl_conn.setStyleSheet("color:#888; font-weight:bold;")
        btn_row.addWidget(self.btn_connect)
        btn_row.addWidget(self.btn_disconnect)
        btn_row.addSpacing(12)
        btn_row.addWidget(self.lbl_conn, 1)
        cfg_grid.addLayout(btn_row, 2, 2, 1, 2)
        lay.addWidget(cfg_box)

        self.table = make_table()
        lay.addWidget(self.table, 1)

        # Footer with frame request button
        footer_lay = QHBoxLayout()
        self.lbl_footer = QLabel("Not connected")
        self.lbl_footer.setStyleSheet("color:#666; font-size:11px;")
        footer_lay.addWidget(self.lbl_footer, 1)

        self.btn_get_frame = QPushButton("View Last Frame")
        self.btn_get_frame.setEnabled(False)
        self.btn_get_frame.setToolTip("Request last frame from selected instance")
        self.btn_get_frame.clicked.connect(self._on_request_frame)
        footer_lay.addWidget(self.btn_get_frame)

        self.btn_capture_frame = QPushButton("Capture with Params…")
        self.btn_capture_frame.setEnabled(False)
        self.btn_capture_frame.setToolTip(
            "Capture a new frame outside schedule with custom exposure/gain/ISO")
        self.btn_capture_frame.clicked.connect(self._on_capture_frame)
        footer_lay.addWidget(self.btn_capture_frame)

        lay.addLayout(footer_lay)

        self._stale_timer = QTimer(self)
        self._stale_timer.setInterval(5000)
        self._stale_timer.timeout.connect(self._refresh_table)
        self._stale_timer.start()

    def _load_config(self):
        cfg = self._mqtt_cfg
        if cfg:
            self.le_host.setText(cfg.get("host", "broker.hivemq.com"))
            self.le_port.setText(str(cfg.get("port", 1883)))
            self.le_user.setText(cfg.get("user", ""))
            self.le_pass.setText(cfg.get("password", ""))
            self.le_prefix.setText(cfg.get("prefix", "every_camera"))
            self.cb_tls.setChecked(cfg.get("tls", False))

    def _on_connect(self):
        if not MQTT_AVAILABLE:
            self.lbl_conn.setText("Error: install paho-mqtt")
            self.lbl_conn.setStyleSheet("color:#cc0000; font-weight:bold;")
            return

        from mqtt_client import MqttSubscriber
        host = self.le_host.text().strip()
        port = self.le_port.text().strip()
        user = self.le_user.text().strip()
        password = self.le_pass.text()
        prefix = self.le_prefix.text().strip() or "every_camera"
        status_topic = f"{prefix}/+/status"
        frame_topic = f"{prefix}/+/frame"

        try:
            self._subscriber = MqttSubscriber(host, port, user, password,
                                               use_tls=self.cb_tls.isChecked())
            self._subscriber.connected.connect(self._on_broker_connected)
            self._subscriber.disconnected.connect(self._on_broker_disconnected)
            self._subscriber.message_received.connect(self._on_message)
            self._subscriber.error.connect(self._on_broker_error)
            # Subscribe to both status and frame topics
            self._subscriber.connect_broker([status_topic, frame_topic])
            self.lbl_conn.setText("Connecting...")
            self.lbl_conn.setStyleSheet("color:#888; font-weight:bold;")
            self.btn_connect.setEnabled(False)
        except Exception as e:
            self.lbl_conn.setText(f"Error: {e}")
            self.lbl_conn.setStyleSheet("color:#cc0000; font-weight:bold;")

    def _on_disconnect(self):
        if self._subscriber:
            self._subscriber.disconnect_broker()
            self._subscriber = None
        self._instances.clear()
        self._refresh_table()
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.btn_get_frame.setEnabled(False)
        self.btn_capture_frame.setEnabled(False)

    def _on_broker_connected(self):
        self.lbl_conn.setText(f"Connected to {self.le_host.text().strip()}")
        self.lbl_conn.setStyleSheet("color:#007700; font-weight:bold;")
        self.btn_disconnect.setEnabled(True)
        self.btn_get_frame.setEnabled(True)
        self.btn_capture_frame.setEnabled(True)

    def _on_broker_disconnected(self):
        self.lbl_conn.setText("Disconnected")
        self.lbl_conn.setStyleSheet("color:#888; font-weight:bold;")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.btn_get_frame.setEnabled(False)
        self.btn_capture_frame.setEnabled(False)

    def _on_broker_error(self, msg):
        self.lbl_conn.setText(f"Error: {msg}")
        self.lbl_conn.setStyleSheet("color:#cc0000; font-weight:bold;")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.btn_get_frame.setEnabled(False)
        self.btn_capture_frame.setEnabled(False)

    def _on_message(self, topic, payload):
        # Handle frame responses
        if topic.endswith("/frame"):
            self._on_frame_received(topic, payload)
            return
        # Handle status messages
        try:
            data = json.loads(payload)
            self._instances[topic] = (data, dt.now())
            self._refresh_table()
        except json.JSONDecodeError:
            pass

    def _on_request_frame(self):
        """Request last frame from the selected instance."""
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "No selection",
                                    "Select an instance in the table first.")
            return

        instance_item = self.table.item(row, 0)
        if not instance_item:
            return
        instance_name = instance_item.text()
        prefix = self.le_prefix.text().strip() or "every_camera"
        cmd_topic = f"{prefix}/{instance_name}/cmd/get_frame"

        if self._subscriber:
            self._subscriber.publish(cmd_topic, b"", retain=False)
            print(f"[monitor] Published cmd: {cmd_topic}", flush=True)
            self.lbl_footer.setText(f"Frame requested from {instance_name}...")

            # Open viewer dialog
            if not self._frame_viewer or not self._frame_viewer.isVisible():
                self._frame_viewer = FrameViewerDialog(self)
            self._frame_viewer.lbl_image.setText(
                f"Waiting for frame from {instance_name}...")
            self._frame_viewer.lbl_info.setText(f"Requesting: {instance_name}")
            self._frame_viewer.show()
            self._frame_viewer.raise_()

            self._pending_frame_instance = instance_name
            self._frame_timeout_timer.start(FRAME_REQUEST_TIMEOUT_MS)

    def _on_capture_frame(self):
        """Request an on-demand frame with custom exposure/gain params."""
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "No selection",
                                    "Select an instance in the table first.")
            return
        instance_item = self.table.item(row, 0)
        if not instance_item:
            return
        instance_name = instance_item.text()

        # Look up camera_type from cached status
        camera_type = None
        for (data, _) in self._instances.values():
            if data.get("instance_name") == instance_name:
                camera_type = data.get("camera_type")
                break

        dlg = CaptureParamsDialog(instance_name, camera_type, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        params = dlg.get_params()

        if not self._subscriber:
            return
        prefix = self.le_prefix.text().strip() or "every_camera"
        cmd_topic = f"{prefix}/{instance_name}/cmd/capture_frame"
        payload_bytes = json.dumps(params).encode("utf-8")
        self._subscriber.publish(cmd_topic, payload_bytes, retain=False)
        print(f"[monitor] Published cmd: {cmd_topic} payload={params}",
              flush=True)
        self.lbl_footer.setText(
            f"On-demand capture requested from {instance_name}: {params or '(defaults)'}")

        if not self._frame_viewer or not self._frame_viewer.isVisible():
            self._frame_viewer = FrameViewerDialog(self)
        self._frame_viewer.lbl_image.setText(
            f"Capturing new frame from {instance_name}...\nParams: {params}")
        self._frame_viewer.lbl_info.setText(f"On-demand: {instance_name}")
        self._frame_viewer.show()
        self._frame_viewer.raise_()

        self._pending_frame_instance = instance_name
        # On-demand captures may take longer (exposure, reconfigure) — give more time.
        self._frame_timeout_timer.start(ON_DEMAND_TIMEOUT_MS)

    def _on_frame_timeout(self):
        instance_name = self._pending_frame_instance
        if not instance_name:
            return
        self._pending_frame_instance = None
        hint = ("Check that the worker is running, MQTT prefix matches, and "
                "the instance name is correct.")
        msg = (f"No response from {instance_name}. "
               f"{hint}")
        self.lbl_footer.setText(msg)
        print(f"[monitor] Timeout waiting for {instance_name}", flush=True)
        if self._frame_viewer and self._frame_viewer.isVisible():
            self._frame_viewer.lbl_image.setText(msg)
            self._frame_viewer.lbl_info.setText(f"Timeout: {instance_name}")

    def _on_frame_received(self, topic, payload):
        """Handle incoming frame data."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            msg = f"Frame decode error: {e}"
            self.lbl_footer.setText(msg)
            print(f"[monitor] {msg}", flush=True)
            return

        instance_name = data.get("instance_name", "?")
        camera_type = data.get("camera_type", "?")
        timestamp = data.get("timestamp")
        status = data.get("status", "ok")
        note = data.get("note", "")

        print(f"[monitor] /frame from {instance_name} ({camera_type}): "
              f"status={status} note={note!r}", flush=True)

        # Intermediate statuses — keep the timeout running but refresh the UI.
        if status == "accepted":
            self.lbl_footer.setText(
                f"{instance_name}: request accepted — {note}")
            if self._frame_viewer and self._frame_viewer.isVisible():
                self._frame_viewer.lbl_image.setText(
                    f"Request accepted by {instance_name}\n{note}")
                self._frame_viewer.lbl_info.setText(
                    f"{instance_name} ({camera_type}) — accepted")
            # Extend the window: camera will start capturing soon.
            self._frame_timeout_timer.start(ON_DEMAND_TIMEOUT_MS)
            return
        if status == "capturing":
            self.lbl_footer.setText(
                f"{instance_name}: capturing — {note}")
            if self._frame_viewer and self._frame_viewer.isVisible():
                self._frame_viewer.lbl_image.setText(
                    f"Capturing new frame on {instance_name}...\n{note}")
                self._frame_viewer.lbl_info.setText(
                    f"{instance_name} ({camera_type}) — capturing")
            self._frame_timeout_timer.start(ON_DEMAND_TIMEOUT_MS)
            return

        # Terminal statuses — stop waiting-timeout.
        self._pending_frame_instance = None
        if self._frame_timeout_timer:
            self._frame_timeout_timer.stop()

        if status != "ok" or not data.get("data"):
            err = data.get("error") or f"status={status}"
            msg = f"No frame from {instance_name}: {err}"
            self.lbl_footer.setText(msg)
            print(f"[monitor] {msg}", flush=True)
            if self._frame_viewer and self._frame_viewer.isVisible():
                self._frame_viewer.lbl_image.setText(msg)
                self._frame_viewer.lbl_info.setText(
                    f"{instance_name} ({camera_type}) — {status}")
            return

        try:
            jpeg_data = base64.b64decode(data["data"])
        except (ValueError, TypeError) as e:
            self.lbl_footer.setText(f"Frame base64 decode error: {e}")
            return

        if not self._frame_viewer or not self._frame_viewer.isVisible():
            self._frame_viewer = FrameViewerDialog(self)
            self._frame_viewer.show()

        try:
            self._frame_viewer.show_frame(
                instance_name, camera_type, jpeg_data, timestamp)
        except Exception as e:
            self.lbl_footer.setText(f"Frame display error: {e}")
            return

        self._frame_viewer.raise_()
        self.lbl_footer.setText(
            f"Frame received from {instance_name} at "
            f"{dt.now().strftime('%H:%M:%S')}")

    def _refresh_table(self):
        records = [rec for rec, _ in self._instances.values()]
        records.sort(key=lambda r: str(r.get("instance_name", "")))
        populate_table(self.table, records)
        n = len(records)
        running = sum(1 for r in records if r.get("status") == "running")
        ts = dt.now().strftime("%H:%M:%S")
        self.lbl_footer.setText(
            f"Last update: {ts}   |   {n} instance(s)   |   {running} running"
        )


# ---------------------------------------------------------------------------
# Local files tab
# ---------------------------------------------------------------------------
class LocalTab(QWidget):
    def __init__(self):
        super().__init__()
        self._status_dir = HOME_STATUS_DIR
        self._build_ui()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(LOCAL_REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start()
        self.refresh()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        dir_box = QGroupBox("Status Directory")
        dir_lay = QHBoxLayout(dir_box)
        self.le_dir = QLineEdit(self._status_dir)
        self.le_dir.returnPressed.connect(self._apply_dir)
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse)
        btn_apply = QPushButton("Apply")
        btn_apply.clicked.connect(self._apply_dir)
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh)
        dir_lay.addWidget(self.le_dir, 1)
        dir_lay.addWidget(btn_browse)
        dir_lay.addWidget(btn_apply)
        dir_lay.addSpacing(8)
        dir_lay.addWidget(btn_refresh)
        lay.addWidget(dir_box)

        self.table = make_table()
        lay.addWidget(self.table, 1)

        self.lbl_footer = QLabel("--")
        self.lbl_footer.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(self.lbl_footer)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select status directory",
                                             self._status_dir)
        if d:
            self.le_dir.setText(d)
            self._apply_dir()

    def _apply_dir(self):
        p = self.le_dir.text().strip()
        if p:
            self._status_dir = p
        self.refresh()

    def refresh(self):
        records = self._read_files()
        populate_table(self.table, records)
        n = len(records)
        running = sum(1 for r in records if r.get("status") == "running")
        ts = dt.now().strftime("%H:%M:%S")
        self.lbl_footer.setText(
            f"Last refresh: {ts}   |   {n} instance(s)   |   {running} running"
        )

    def _read_files(self):
        records = []
        if not os.path.isdir(self._status_dir):
            return records
        for path in glob.glob(os.path.join(self._status_dir, "*.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
                data.setdefault("instance_name", os.path.basename(path))
                data.setdefault("pid", "?")
                data.setdefault("status", "unknown")
                records.append(data)
            except Exception:
                pass
        records.sort(key=lambda r: str(r.get("instance_name", "")))
        return records


# ---------------------------------------------------------------------------
# Monitor widget (embeddable in tabs)
# ---------------------------------------------------------------------------
class MonitorWidget(QWidget):
    def __init__(self, mqtt_cfg=None):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        tabs = QTabWidget()
        self._mqtt_tab = MqttTab(mqtt_cfg)
        tabs.addTab(self._mqtt_tab, "MQTT (remote)")
        self._local_tab = LocalTab()
        tabs.addTab(self._local_tab, "Local files")
        lay.addWidget(tabs)
