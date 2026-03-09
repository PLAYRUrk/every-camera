"""
Camera Monitor GUI — displays live status of all running camera instances.
Supports MQTT (remote) and local file modes.
"""
import os
import json
import glob

from datetime import datetime as dt

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit,
    QTableWidget, QTableWidgetItem,
    QFileDialog, QHeaderView, QAbstractItemView,
    QGroupBox, QTabWidget, QCheckBox,
)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QColor, QFont

from mqtt_client import MQTT_AVAILABLE

from utils import HOME_STATUS_DIR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STALE_THRESHOLD_SECONDS = 30
LOCAL_REFRESH_INTERVAL_MS = 5000

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
        return "—"
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
            parts.append(f"T:{rec['cam_temp_ccd']}°")
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

        self.lbl_footer = QLabel("Not connected")
        self.lbl_footer.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(self.lbl_footer)

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
        topic = f"{prefix}/+/status"

        try:
            self._subscriber = MqttSubscriber(host, port, user, password,
                                               use_tls=self.cb_tls.isChecked())
            self._subscriber.connected.connect(self._on_broker_connected)
            self._subscriber.disconnected.connect(self._on_broker_disconnected)
            self._subscriber.message_received.connect(self._on_message)
            self._subscriber.error.connect(self._on_broker_error)
            self._subscriber.connect_broker(topic)
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

    def _on_broker_connected(self):
        self.lbl_conn.setText(f"Connected to {self.le_host.text().strip()}")
        self.lbl_conn.setStyleSheet("color:#007700; font-weight:bold;")
        self.btn_disconnect.setEnabled(True)

    def _on_broker_disconnected(self):
        self.lbl_conn.setText("Disconnected")
        self.lbl_conn.setStyleSheet("color:#888; font-weight:bold;")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)

    def _on_broker_error(self, msg):
        self.lbl_conn.setText(f"Error: {msg}")
        self.lbl_conn.setStyleSheet("color:#cc0000; font-weight:bold;")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)

    def _on_message(self, topic, payload):
        try:
            data = json.loads(payload)
            self._instances[topic] = (data, dt.now())
            self._refresh_table()
        except json.JSONDecodeError:
            pass

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
