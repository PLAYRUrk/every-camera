"""
Camera monitor widget — the cameras running on this machine.

Reads the status files under ~/.every_camera/status, so it sees every camera
started here, console or GUI, and nothing else. The network is
``monitor_app.py``'s business.
"""
import os
import json
import glob

from datetime import datetime as dt

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit,
    QTableWidget, QTableWidgetItem,
    QFileDialog, QHeaderView, QAbstractItemView,
    QGroupBox,
)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QColor, QFont

from utils import HOME_STATUS_DIR, pid_alive

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
    "offline": QColor(200, 80, 0),
    "idle":    QColor(80, 80, 200),
    "stale":   QColor(200, 80, 0),
    # A camera up for focusing only: nothing is being archived, which is a
    # state of its own and not a fault. Same orange the apps badge it with.
    "setup":   QColor(200, 112, 0),
    "starting": QColor(80, 80, 200),
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
        return "\u2014"
    try:
        return dt.fromisoformat(iso_str).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(iso_str)


def _extra_info(rec):
    """Build short extra info string from monitoring data."""
    parts = []
    # Which machine this camera sits on — the same friendly name the observer
    # programs show instead of an IP.
    if rec.get("node_name"):
        parts.append(f"@{rec['node_name']}")
    cam_type = rec.get("camera_type", "")
    if cam_type == "cannon":
        if rec.get("iso"):
            parts.append(f"ISO:{rec['iso']}")
        if rec.get("shutterspeed"):
            parts.append(f"SS:{rec['shutterspeed']}")
    elif cam_type == "sptt":
        if rec.get("exposure_s") is not None:
            parts.append(f"Exp:{rec['exposure_s']}s")
        # Only when the camera disagrees with the request — that divergence is
        # the whole story of the truncated-exposure bug.
        cam_exp = rec.get("cam_exposure_s")
        if cam_exp is not None and cam_exp != rec.get("exposure_s"):
            parts.append(f"hw:{cam_exp}s")
        if rec.get("gain") is not None:
            parts.append(f"G:{rec['gain']}")
        if rec.get("frame_size"):
            parts.append(rec["frame_size"])
        if rec.get("cam_temp_ccd") is not None:
            parts.append(f"T:{rec['cam_temp_ccd']}\u00b0")
    elif cam_type == "infra":
        if rec.get("exposure_us") is not None:
            exp_ms = rec["exposure_us"] / 1000.0
            parts.append(f"Exp:{exp_ms:.1f}ms" if exp_ms < 1000
                         else f"Exp:{exp_ms / 1000:.2f}s")
        if rec.get("gain") is not None:
            parts.append(f"G:{rec['gain']}")
        if rec.get("roi"):
            parts.append(rec["roi"])
    elif cam_type == "sentry":
        if rec.get("seqno") is not None:
            parts.append(f"Seq:{rec['seqno']}")
        if rec.get("ccdtemp") is not None:
            parts.append(f"T:{rec['ccdtemp']}\u00b0")
        if rec.get("exposure") is not None:
            parts.append(f"Exp:{rec['exposure']}")
        if rec.get("daemon_running") is not None:
            parts.append("daemon:up" if rec["daemon_running"] else "daemon:DOWN")
    elif cam_type == "asi":
        if rec.get("phase"):
            parts.append(str(rec["phase"]))
        if rec.get("filter") is not None:
            parts.append(f"F:{rec['filter']}")
        if rec.get("exposure") is not None:
            parts.append(f"Exp:{rec['exposure']}s")
        if rec.get("binning") is not None:
            parts.append(f"B:{rec['binning']}x{rec['binning']}")
        if rec.get("ccd_temp") is not None:
            parts.append(f"T:{rec['ccd_temp']}°"
                         + ("" if rec.get("temp_locked", True) else "!"))
        if rec.get("darks_taken"):
            parts.append(f"darks:{rec['darks_taken']}")
    elif cam_type == "japan":
        # The same fields as asi, minus the "!" marker: this camera has no
        # setpoint, so its temperature can never be reported as unlocked.
        if rec.get("phase"):
            parts.append(str(rec["phase"]))
        if rec.get("filter") is not None:
            parts.append(f"F:{rec['filter']}")
        if rec.get("exposure") is not None:
            parts.append(f"Exp:{rec['exposure']}s")
        if rec.get("binning") is not None:
            parts.append(f"B:{rec['binning']}x{rec['binning']}")
        if rec.get("ccd_temp") is not None:
            parts.append(f"T:{rec['ccd_temp']}°")
        if rec.get("darks_taken"):
            parts.append(f"darks:{rec['darks_taken']}")
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
                # A worker killed with SIGKILL never removes its own file. Show
                # it as stopped instead of pretending it is still running.
                if data["status"] not in ("stopped", "offline") \
                        and not pid_alive(data.get("pid")):
                    data["status"] = "stopped"
                    data["instance_name"] = f"{data['instance_name']} (dead)"
                records.append(data)
            except Exception:
                pass
        records.sort(key=lambda r: str(r.get("instance_name", "")))
        return records


# ---------------------------------------------------------------------------
# Monitor widget (embeddable in tabs)
# ---------------------------------------------------------------------------
class MonitorWidget(QWidget):
    """The cameras running on this machine, read from their status files.

    It used to have a second tab, fed by a broker, showing cameras anywhere.
    That is ``monitor_app.py``'s job and it does it over the LAN, first-hand
    and with the archive and the live view a click away. What is left here is
    the one thing a tab inside the camera program is best placed to say.
    """

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._local_tab = LocalTab()
        lay.addWidget(self._local_tab)
