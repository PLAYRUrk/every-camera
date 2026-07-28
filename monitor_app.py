#!/usr/bin/env python3
"""
Every Camera — monitor.

Every camera as a tile: what it is, which machine it runs on, what it is doing
right now, and what has gone wrong.

Two sources, in that order of importance:

* **The local network** — the main one. Cameras are found by the same UDP
  discovery ``viewer_app.py`` and ``focus_app.py`` use, and each tile is filled
  in from that camera's ``GET /api/status``. Nothing has to be configured, the
  reading is first-hand and it is as fresh as the refresh interval.
* **MQTT** — secondary, for cameras at another site with no route to their HTTP
  port. Connect the panel at the bottom and their retained status topics become
  tiles alongside the rest. A camera visible both ways is shown from the LAN:
  that reading is direct rather than relayed through a broker.

Watching only. Frames belong to ``viewer_app.py`` and focusing to
``focus_app.py`` — this program used to fetch frames over MQTT itself, which
amounted to a second, weaker viewer inside the monitor. The tiles now hand the
camera over to the right tool instead.

Usage:
    python monitor_app.py
    python monitor_app.py --host 192.168.1.5      # one discovery cannot see
    python monitor_app.py --mqtt                  # connect the broker at once
    python monitor_app.py --interval 5            # status refresh, seconds
"""
import argparse
import html
import json
import os
import subprocess
import sys

from datetime import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

APP_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_HTTP_PORT = 8765
DISCOVERY_EVERY_MS = 20_000      # a camera started later must still turn up
STATUS_EVERY_MS = 3_000
TILE_WIDTH = 330
# Two missed discovery rounds before a camera is called gone: one dropped UDP
# reply is normal on a busy network and must not make a tile flicker.
MISSES_BEFORE_GONE = 2


# ---------------------------------------------------------------------------
# Wording — everything below reads a status payload, nothing talks to hardware
# ---------------------------------------------------------------------------
def fmt_age(iso_str):
    """How long ago, in words, or an em dash when there is nothing to say."""
    if not iso_str:
        return "—"
    try:
        seconds = (dt.now() - dt.fromisoformat(iso_str)).total_seconds()
    except (ValueError, TypeError):
        return str(iso_str)
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)} s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} d ago"


def fmt_countdown(iso_str):
    """Time until a moment in the future, for the next scheduled capture."""
    if not iso_str:
        return "—"
    try:
        seconds = (dt.fromisoformat(iso_str) - dt.now()).total_seconds()
    except (ValueError, TypeError):
        return str(iso_str)
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"in {int(seconds)} s"
    if seconds < 3600:
        return f"in {int(seconds // 60)} min"
    return f"in {seconds / 3600:.1f} h"


def fmt_filter(value):
    """The ASI wheel's position: home is a place, unknown is a failed move."""
    if value is None:
        return "unknown"
    return "home" if value == 0 else str(value)


def fmt_sensor(rec):
    text = f"{rec['ccd_temp']:.1f} °C"
    if rec.get("set_temp") is not None:
        text += (f" → {rec['set_temp']:.0f} °C"
                 + ("" if rec.get("temp_locked") else ", settling"))
    return text


def short_reason(message):
    """The readable part of a connection failure, for a 330-pixel tile.

    ``urlopen`` wraps the real cause twice over; a card has room for the cause.
    """
    text = str(message or "").strip()
    _, _, tail = text.partition("unreachable: ")
    text = (tail or text).strip()
    if text.startswith("<urlopen error ") and text.endswith(">"):
        text = text[len("<urlopen error "):-1]
    if text.startswith("[Errno"):
        text = text.partition("]")[2].strip() or text
    return text[0].lower() + text[1:] if text else "not answering"


def camera_rows(rec):
    """The lines worth showing for one camera, as ``(label, value)`` pairs.

    Everything comes from the status payload the worker publishes — the same
    one for LAN and MQTT — so a camera type that grows a field only has to
    publish it.
    """
    rows = []
    kind = (rec.get("camera_type") or "").lower()

    if rec.get("phase"):
        rows.append(("Phase", str(rec["phase"])))
    # A camera in setup mode is following no schedule at all; naming the one it
    # would have followed reads as though captures were being taken.
    if rec.get("mode") and not rec.get("setup_mode"):
        rows.append(("Schedule", str(rec["mode"])))

    if kind == "asi":
        exposure, binning = rec.get("exposure"), rec.get("binning")
        if exposure is not None:
            rows.append(("Exposure", f"{exposure:g} s"
                         + (f" · {binning}×{binning}" if binning else "")))
        rows.append(("Filter", fmt_filter(rec.get("filter"))))
        if rec.get("shutter") is not None:
            rows.append(("Shutter", "open" if rec["shutter"] else "closed"))
        if rec.get("ccd_temp") is not None:
            rows.append(("Sensor", fmt_sensor(rec)))
    elif kind == "sptt":
        if rec.get("exposure_s") is not None:
            rows.append(("Exposure", f"{rec['exposure_s']:g} s"))
        if rec.get("gain") is not None:
            rows.append(("Gain", str(rec["gain"])))
        if rec.get("frame_size"):
            rows.append(("Frame", str(rec["frame_size"])))
        if rec.get("cam_temp_ccd") is not None:
            rows.append(("Sensor", f"{rec['cam_temp_ccd']} °C"))
    elif kind == "infra":
        if rec.get("exposure_us") is not None:
            micros = rec["exposure_us"]
            rows.append(("Exposure", f"{micros / 1000:.1f} ms" if micros < 1e6
                         else f"{micros / 1e6:.2f} s"))
        if rec.get("gain") is not None:
            rows.append(("Gain", str(rec["gain"])))
        if rec.get("roi"):
            rows.append(("ROI", str(rec["roi"])))
    elif kind == "cannon":
        settings = [str(rec[key]) for key in ("iso", "shutterspeed", "aperture")
                    if rec.get(key)]
        if settings:
            rows.append(("Camera", " · ".join(settings)))
    elif kind == "sentry":
        if rec.get("daemon_running") is not None:
            rows.append(("imagerd_rt",
                         "running" if rec["daemon_running"] else "DOWN"))
        if rec.get("ccdtemp") is not None:
            rows.append(("Sensor", f"{rec['ccdtemp']} °C"))
        if rec.get("seqno") is not None:
            rows.append(("Sequence", str(rec["seqno"])))

    shots = rec.get("shots_taken")
    if shots is not None:
        text = str(shots)
        if rec.get("darks_taken"):
            text += f"   ({rec['darks_taken']} dark)"
        rows.append(("Frames", text))
    rows.append(("Last frame", fmt_age(rec.get("last_shot"))))
    if rec.get("next_slot"):
        rows.append(("Next slot", fmt_countdown(rec["next_slot"])))
    if rec.get("active_until"):
        rows.append(("Window ends", str(rec["active_until"])[11:19]))

    system = rec.get("system") or {}
    if system.get("disk_free_mb") is not None:
        free = system["disk_free_mb"]
        rows.append(("Disk free", f"{free / 1024:.1f} GB" if free > 1024
                     else f"{free} MB"))
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Every Camera — monitor the cameras on this network")
    parser.add_argument("--host", action="append", default=[],
                        metavar="HOST[:PORT]",
                        help="Watch this camera too, even if discovery misses it")
    parser.add_argument("--interval", type=float, default=STATUS_EVERY_MS / 1000,
                        help="Seconds between status refreshes (default 3)")
    parser.add_argument("--mqtt", action="store_true",
                        help="Also connect to the broker from config.json at once")
    parser.add_argument("--config", default=None, help="Path to config.json")
    args = parser.parse_args()

    from utils import can_use_gui, load_config

    if not can_use_gui():
        print("Error: no display available. The monitor needs a graphical "
              "environment; `python -m discovery` lists the cameras on the "
              "network from a terminal.")
        sys.exit(1)

    try:
        cfg = load_config(args.config)
    except Exception:
        cfg = {}

    # Qt's own plugins, not OpenCV's — the two conflict when both are installed.
    try:
        import PyQt5 as _pyqt5
        plugins = os.path.join(os.path.dirname(_pyqt5.__file__), "Qt5", "plugins")
        if os.path.isdir(plugins):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = plugins
    except Exception:
        os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)

    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MonitorWindow(mqtt_cfg=cfg.get("mqtt", {}),
                           interval_ms=max(1000, int(args.interval * 1000)))
    for entry in args.host:
        window.add_manual(entry)
    window.show()
    if args.mqtt:
        window.connect_broker()
    sys.exit(app.exec_())


# The Qt widgets live below the entry point so that ``--help`` and the
# no-display check above run without importing PyQt5 first.
from PyQt5.QtWidgets import (                                       # noqa: E402
    QMainWindow, QWidget, QFrame, QLabel, QPushButton, QLineEdit,
    QVBoxLayout, QHBoxLayout, QGridLayout, QScrollArea, QCheckBox,
    QSizePolicy, QStatusBar, QDialog, QPlainTextEdit, QGroupBox,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal                     # noqa: E402
from PyQt5.QtGui import QFont                                       # noqa: E402

from monitor import STATUS_COLORS, status_display                   # noqa: E402
from mqtt_client import MQTT_AVAILABLE                              # noqa: E402
from net_client import (                                            # noqa: E402
    CameraClient, TaskRunner, DiscoveryTask, node_name_of,
)

LAN, MQTT = "lan", "mqtt"


class CameraTile(QFrame):
    """One camera, as much of it as fits on a card."""

    details_requested = pyqtSignal(object)      # the whole status record

    def __init__(self, key, node, parent=None):
        super().__init__(parent)
        self.key = key                  # (LAN, host, port) or (MQTT, instance)
        self.node = dict(node or {})
        self.record = {}
        self.misses = 0
        self.unreachable = ""
        # Whether the filter box excludes this tile. Kept here rather than read
        # back from Qt: a widget that has not been shown yet is "hidden" too,
        # and taking that for "filtered out" left every new tile invisible.
        self.filtered_out = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setFixedWidth(TILE_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        head = QHBoxLayout()
        self.lbl_name = QLabel()
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)
        self.lbl_name.setFont(font)
        head.addWidget(self.lbl_name, 1)
        self.lbl_status = QLabel()
        self.lbl_status.setAlignment(Qt.AlignCenter)
        head.addWidget(self.lbl_status)
        lay.addLayout(head)

        self.lbl_where = QLabel()
        self.lbl_where.setStyleSheet("color:#888; font-size:11px;")
        self.lbl_where.setWordWrap(True)
        lay.addWidget(self.lbl_where)

        self.lbl_rows = QLabel()
        self.lbl_rows.setTextFormat(Qt.RichText)
        self.lbl_rows.setWordWrap(True)
        lay.addWidget(self.lbl_rows)

        self.lbl_note = QLabel()
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setStyleSheet("color:#c07000; font-size:11px;")
        self.lbl_note.hide()
        lay.addWidget(self.lbl_note)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.btn_frames = QPushButton("Frames…")
        self.btn_frames.clicked.connect(lambda: self._launch("viewer_app.py"))
        buttons.addWidget(self.btn_frames)
        self.btn_focus = QPushButton("Focus…")
        self.btn_focus.clicked.connect(lambda: self._launch("focus_app.py"))
        buttons.addWidget(self.btn_focus)
        self.btn_details = QPushButton("Details")
        self.btn_details.setToolTip("The whole status payload, as published")
        self.btn_details.clicked.connect(
            lambda: self.details_requested.emit(self.snapshot))
        buttons.addWidget(self.btn_details)
        lay.addLayout(buttons)

        self.refresh()

    # -- identity ---------------------------------------------------------
    @property
    def source(self):
        return self.key[0]

    @property
    def address(self):
        """``(host, port)`` for a camera on this network, None over MQTT."""
        return (self.key[1], self.key[2]) if self.source == LAN else None

    @property
    def instance_name(self):
        return str(self.snapshot.get("instance_name") or "?")

    @property
    def snapshot(self):
        """Everything known about this camera: discovery reply plus status."""
        merged = dict(self.node)
        merged.update(self.record)
        return merged

    @property
    def label(self):
        """The text the filter box matches against."""
        rec = self.snapshot
        return " ".join(str(x) for x in (
            rec.get("instance_name"), rec.get("camera_type"),
            node_name_of(rec), self.address[0] if self.address else "mqtt",
            rec.get("status")))

    # -- updates ----------------------------------------------------------
    def seen(self, node):
        """A fresh discovery reply for this camera."""
        self.node.update(node or {})
        self.misses = 0

    def set_status(self, record):
        self.record = dict(record or {})
        self.unreachable = ""
        self.misses = 0
        self.refresh()

    def set_unreachable(self, message):
        """It answered once and is not answering now — say so, keep the tile."""
        self.unreachable = message or "not answering"
        self.refresh()

    def refresh(self):
        rec = self.snapshot
        self.lbl_name.setText(str(rec.get("instance_name") or "?"))
        where = (f"{self.address[0]}:{self.address[1]}" if self.address
                 else "via MQTT")
        self.lbl_where.setText(
            f"{str(rec.get('camera_type') or '?').upper()}  ·  "
            f"{node_name_of(rec) or '?'}  ·  {where}")

        if self.unreachable:
            text, colour = "OFFLINE", STATUS_COLORS["offline"]
        elif not self.record:
            text, colour = "…", STATUS_COLORS["unknown"]
        else:
            text, colour = status_display(self.record)
        self.lbl_status.setText(f" {text} ")
        self.lbl_status.setStyleSheet(
            f"background:{colour.name()}; color:white; font-size:10px; "
            f"font-weight:bold; border-radius:3px; padding:1px 4px;")

        if self.unreachable:
            self.lbl_rows.setText("<span style='color:#888'>"
                                  f"{html.escape(short_reason(self.unreachable))}"
                                  "</span>")
        else:
            # Escaped as it is put in: a status value is a camera's own text,
            # and one with a "<" in it silently ate the rest of the tile.
            cells = [(key, html.escape(str(value)))
                     for key, value in camera_rows(rec)]
            errors = rec.get("errors") or 0
            if errors:
                cells.append(("Errors", f"<span style='color:#c0392b'><b>"
                                        f"{int(errors)}</b></span>"))
            cells.append(("Updated", html.escape(fmt_age(rec.get("last_update")))))
            self.lbl_rows.setText(
                "<table cellspacing='0' cellpadding='0'>"
                + "".join(
                    f"<tr><td style='color:#888; padding-right:12px'>{key}</td>"
                    f"<td>{value}</td></tr>" for key, value in cells)
                + "</table>")

        notes = []
        if rec.get("setup_mode"):
            notes.append("Setup mode — no schedule, nothing is being archived.")
        if rec.get("focus_active"):
            notes.append("Someone is focusing this camera.")
        if rec.get("focus_note"):
            notes.append(str(rec["focus_note"]))
        self.lbl_note.setText("  ".join(notes))
        self.lbl_note.setVisible(bool(notes))
        self._update_buttons(rec)

    def _update_buttons(self, rec):
        """Only a camera we have a route to can be viewed or focused."""
        if self.address is None:
            for button in (self.btn_frames, self.btn_focus):
                button.setEnabled(False)
                button.setToolTip("Only reachable over MQTT — viewing frames "
                                  "and focusing need a route to the camera")
            return
        self.btn_frames.setEnabled(not self.unreachable)
        self.btn_frames.setToolTip("Open viewer_app.py for this camera")
        focusable = bool(rec.get("supports_focus", True))
        self.btn_focus.setEnabled(not self.unreachable and focusable)
        self.btn_focus.setToolTip(
            "Open focus_app.py for this camera" if focusable
            else "This camera has no free-running focus mode")

    def _launch(self, program):
        """Hand this camera over to the program whose job that is."""
        if self.address is None:
            return
        host, port = self.address
        try:
            subprocess.Popen([sys.executable, os.path.join(APP_DIR, program),
                              "--host", str(host), "--port", str(port)])
        except OSError as exc:
            self.lbl_note.setText(f"Could not start {program}: {exc}")
            self.lbl_note.show()


class DetailsDialog(QDialog):
    """The camera's whole status payload, for when a tile is not enough."""

    def __init__(self, record, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{record.get('instance_name', 'camera')} — status")
        self.resize(520, 560)
        lay = QVBoxLayout(self)
        text = QPlainTextEdit(json.dumps(record, indent=2, sort_keys=True,
                                         default=str))
        text.setReadOnly(True)
        text.setFont(QFont("monospace", 9))
        lay.addWidget(text)


class MqttPanel(QGroupBox):
    """The secondary source: retained status topics from a broker.

    Deliberately a strip at the bottom rather than a tab of its own. The LAN is
    where the cameras are; this is for the ones that are somewhere else.
    """

    record_received = pyqtSignal(str, object)    # instance name, status or None
    state_changed = pyqtSignal(str)

    def __init__(self, mqtt_cfg=None, parent=None):
        super().__init__("MQTT — cameras outside this network (secondary)", parent)
        self.setCheckable(True)
        self.setChecked(False)          # collapsed until someone wants it
        self._subscriber = None
        cfg = mqtt_cfg or {}

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 6)
        lay.setSpacing(6)
        self.le_host = QLineEdit(str(cfg.get("host", "broker.hivemq.com")))
        self.le_port = QLineEdit(str(cfg.get("port", 1883)))
        self.le_port.setMaximumWidth(60)
        self.le_user = QLineEdit(str(cfg.get("user", "")))
        self.le_user.setPlaceholderText("user (optional)")
        self.le_user.setMaximumWidth(130)
        self.le_pass = QLineEdit(str(cfg.get("password", "")))
        self.le_pass.setEchoMode(QLineEdit.Password)
        self.le_pass.setPlaceholderText("password")
        self.le_pass.setMaximumWidth(130)
        self.le_prefix = QLineEdit(str(cfg.get("prefix", "every_camera")))
        self.le_prefix.setMaximumWidth(130)
        self.chk_tls = QCheckBox("TLS")
        self.chk_tls.setChecked(bool(cfg.get("tls", False)))
        self.chk_tls.toggled.connect(
            lambda on: self.le_port.setText("8883" if on else "1883"))

        self._widgets = []
        for caption, widget in (("Broker:", self.le_host), ("Port:", self.le_port),
                                ("Prefix:", self.le_prefix)):
            label = QLabel(caption)
            lay.addWidget(label)
            lay.addWidget(widget)
            self._widgets += [label, widget]
        for widget in (self.le_user, self.le_pass, self.chk_tls):
            lay.addWidget(widget)
            self._widgets.append(widget)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.connect_broker)
        lay.addWidget(self.btn_connect)
        self.lbl_state = QLabel("not connected")
        self.lbl_state.setStyleSheet("color:#888; font-size:11px;")
        lay.addWidget(self.lbl_state, 1)
        self._widgets += [self.btn_connect, self.lbl_state]
        self.toggled.connect(self._on_toggled)
        self._on_toggled(False)

    def _on_toggled(self, on):
        for widget in self._widgets:
            widget.setVisible(on)
        if not on and self._subscriber is not None:
            self.disconnect_broker()

    @property
    def connected(self):
        return self._subscriber is not None

    def connect_broker(self):
        if not MQTT_AVAILABLE:
            self._set_state("paho-mqtt is not installed", error=True)
            return
        if self._subscriber is not None:
            self.disconnect_broker()
            return
        self.setChecked(True)
        from mqtt_client import MqttSubscriber

        prefix = self.le_prefix.text().strip() or "every_camera"
        try:
            self._subscriber = MqttSubscriber(
                self.le_host.text().strip(), self.le_port.text().strip(),
                self.le_user.text().strip(), self.le_pass.text(),
                use_tls=self.chk_tls.isChecked())
            self._subscriber.connected.connect(
                lambda: self._set_state(f"connected to "
                                        f"{self.le_host.text().strip()}"))
            self._subscriber.disconnected.connect(
                lambda: self._set_state("disconnected"))
            self._subscriber.error.connect(
                lambda msg: self._set_state(str(msg), error=True))
            self._subscriber.message_received.connect(self._on_message)
            # Status only: frames are viewer_app's business, and subscribing to
            # them here would pull megabytes through the broker for nothing.
            self._subscriber.connect_broker([f"{prefix}/+/status"])
            self._set_state("connecting…")
            self.btn_connect.setText("Disconnect")
        except Exception as exc:
            self._subscriber = None
            self._set_state(str(exc), error=True)

    def disconnect_broker(self):
        if self._subscriber is not None:
            try:
                self._subscriber.disconnect_broker()
            except Exception:
                pass
            self._subscriber = None
        self.btn_connect.setText("Connect")
        self._set_state("not connected")

    def _set_state(self, text, error=False):
        self.lbl_state.setText(text)
        self.lbl_state.setStyleSheet(
            f"color:{'#c0392b' if error else '#888'}; font-size:11px;")
        self.state_changed.emit(text)

    def _on_message(self, topic, payload):
        if not topic.endswith("/status"):
            return
        instance = topic.split("/")[-2] if "/" in topic else topic
        # An empty retained payload is how a worker erases itself on shutdown.
        if not (payload or "").strip():
            self.record_received.emit(instance, None)
            return
        try:
            self.record_received.emit(instance, json.loads(payload))
        except (ValueError, TypeError):
            pass


class MonitorWindow(QMainWindow):
    def __init__(self, mqtt_cfg=None, interval_ms=STATUS_EVERY_MS):
        super().__init__()
        self.setWindowTitle("Every Camera — monitor")
        self.resize(1080, 760)

        self._tasks = TaskRunner(self)
        self._tiles = {}             # key -> CameraTile
        self._polling = set()        # LAN keys with a status request in flight
        self._discovery = None
        self._columns = 0
        self._last_search = None

        self._build_ui(mqtt_cfg)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(interval_ms)
        self._status_timer.timeout.connect(self._poll_all)
        self._status_timer.start()

        self._discovery_timer = QTimer(self)
        self._discovery_timer.setInterval(DISCOVERY_EVERY_MS)
        self._discovery_timer.timeout.connect(self.discover)
        self._discovery_timer.start()

        QTimer.singleShot(150, self.discover)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self, mqtt_cfg):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 4)

        bar = QHBoxLayout()
        self.btn_search = QPushButton("Search LAN")
        self.btn_search.clicked.connect(self.discover)
        bar.addWidget(self.btn_search)
        self.le_add = QLineEdit()
        self.le_add.setPlaceholderText("add host or host:port")
        self.le_add.setMaximumWidth(180)
        self.le_add.returnPressed.connect(self._on_add_typed)
        bar.addWidget(self.le_add)
        self.le_filter = QLineEdit()
        self.le_filter.setPlaceholderText("filter by name, type or machine")
        self.le_filter.setMaximumWidth(230)
        self.le_filter.textChanged.connect(self._apply_filter)
        bar.addWidget(self.le_filter)
        self.chk_auto = QCheckBox("Keep searching")
        self.chk_auto.setChecked(True)
        self.chk_auto.setToolTip("Re-probe the network every 20 s, so a camera "
                                 "started later appears on its own")
        self.chk_auto.toggled.connect(
            lambda on: self._discovery_timer.start() if on
            else self._discovery_timer.stop())
        bar.addWidget(self.chk_auto)
        bar.addStretch()
        root.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        self._canvas = QWidget()
        self._grid = QGridLayout(self._canvas)
        self._grid.setContentsMargins(0, 6, 0, 6)
        self._grid.setSpacing(8)
        self._grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setWidget(self._canvas)
        root.addWidget(scroll, 1)

        self.lbl_empty = QLabel(
            "No cameras yet. They answer a UDP probe on port 45455 — if a "
            "firewall blocks it, add one by address above.")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        self.lbl_empty.setStyleSheet("color:#888;")
        root.addWidget(self.lbl_empty)

        self.mqtt = MqttPanel(mqtt_cfg)
        self.mqtt.record_received.connect(self._on_mqtt_record)
        self.mqtt.state_changed.connect(lambda _t: self._update_summary())
        root.addWidget(self.mqtt)

        self.setStatusBar(QStatusBar())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    # -- finding cameras ----------------------------------------------------
    def discover(self):
        if self._discovery is not None and self._discovery.isRunning():
            return
        self.btn_search.setEnabled(False)
        self._discovery = DiscoveryTask(timeout=2.0, parent=self)
        self._discovery.found.connect(self._on_discovered)
        self._discovery.finished.connect(
            lambda: self.btn_search.setEnabled(True))
        self._discovery.start()

    def _on_discovered(self, nodes):
        self._last_search = dt.now()
        seen = set()
        for node in nodes or []:
            key = (LAN, node.get("host"),
                   int(node.get("http_port") or DEFAULT_HTTP_PORT))
            seen.add(key)
            tile = self._tiles.get(key)
            if tile is None:
                self._add_tile(key, node)
                # The same camera relayed through the broker is a poorer copy.
                self._drop_mqtt_twin(node.get("instance_name"))
            else:
                tile.seen(node)
                tile.refresh()
        for key, tile in self._tiles.items():
            # Manually added cameras never answer discovery, and MQTT ones are
            # not on this network at all: both are judged by their own source.
            if key in seen or tile.source == MQTT or tile.node.get("manual"):
                continue
            tile.misses += 1
            if tile.misses >= MISSES_BEFORE_GONE and not tile.unreachable:
                tile.set_unreachable("gone from the network")
        self._poll_all()
        self._update_summary()

    def add_manual(self, text):
        """Watch a camera given as ``host`` or ``host:port``."""
        text = (text or "").strip()
        if not text:
            return
        host, _, port = text.partition(":")
        key = (LAN, host.strip(),
               int(port) if port.strip().isdigit() else DEFAULT_HTTP_PORT)
        if key in self._tiles:
            return
        self._add_tile(key, {"host": key[1], "http_port": key[2],
                             "instance_name": key[1], "manual": True})
        self._poll_all()

    def _on_add_typed(self):
        self.add_manual(self.le_add.text())
        self.le_add.clear()

    def _add_tile(self, key, node):
        tile = CameraTile(key, node, self._canvas)
        tile.details_requested.connect(self._show_details)
        self._tiles[key] = tile
        # Through the filter, so a camera appearing while one is typed in does
        # not jump the queue.
        self._apply_filter(self.le_filter.text())
        return tile

    def _remove_tile(self, key):
        tile = self._tiles.pop(key, None)
        if tile is None:
            return
        tile.setParent(None)
        tile.deleteLater()
        self._relayout(force=True)

    def _drop_mqtt_twin(self, instance_name):
        """A camera found on the LAN no longer needs its relayed tile."""
        if instance_name:
            self._remove_tile((MQTT, str(instance_name)))

    # -- MQTT ---------------------------------------------------------------
    def connect_broker(self):
        self.mqtt.setChecked(True)
        self.mqtt.connect_broker()

    def _on_mqtt_record(self, instance, record):
        key = (MQTT, str(instance))
        if record is None:              # the worker cleared its retained topic
            self._remove_tile(key)
            self._update_summary()
            return
        # Anything on this network is already shown first-hand; a broker copy
        # of it would be a second tile for one camera.
        if any(tile.source == LAN and tile.instance_name == str(instance)
               for tile in self._tiles.values()):
            return
        tile = self._tiles.get(key)
        if tile is None:
            tile = self._add_tile(key, {"instance_name": instance})
        tile.set_status(record)
        self._apply_filter(self.le_filter.text())
        self._update_summary()

    # -- reading status -----------------------------------------------------
    def _poll_all(self):
        for key, tile in list(self._tiles.items()):
            if tile.source != LAN or key in self._polling:
                continue        # a slow camera must not queue up requests
            self._polling.add(key)
            client = CameraClient(key[1], key[2], timeout=4.0)
            self._tasks.run(client.status,
                            lambda rec, k=key: self._on_status(k, rec),
                            lambda msg, k=key: self._on_status_failed(k, msg))

    def _on_status(self, key, record):
        self._polling.discard(key)
        tile = self._tiles.get(key)
        if tile is None:
            return
        tile.set_status(record)
        self._drop_mqtt_twin(tile.instance_name)
        self._update_summary()

    def _on_status_failed(self, key, message):
        self._polling.discard(key)
        tile = self._tiles.get(key)
        if tile is None:
            return
        tile.set_unreachable(message)
        self._update_summary()

    def _show_details(self, record):
        DetailsDialog(record, self).exec_()

    # -- layout -------------------------------------------------------------
    def _relayout(self, force=False):
        """Reflow the tiles into as many columns as the window now fits."""
        width = max(self._canvas.width(), TILE_WIDTH)
        columns = max(1, (width + self._grid.spacing())
                      // (TILE_WIDTH + self._grid.spacing()))
        if columns == self._columns and not force:
            return
        self._columns = columns
        while self._grid.count():
            self._grid.takeAt(0)
        index = 0
        for tile in self._sorted_tiles():
            if tile.filtered_out:
                tile.hide()
                continue
            # Top-aligned: without it every tile in a row grows to the height of
            # the fullest one, and a camera with little to say is mostly gap.
            self._grid.addWidget(tile, index // columns, index % columns,
                                 Qt.AlignTop)
            tile.show()
            index += 1
        self.lbl_empty.setVisible(not self._tiles)

    def _sorted_tiles(self):
        """LAN first, then by camera type and name — a stable, readable order."""
        return sorted(self._tiles.values(),
                      key=lambda t: (t.source != LAN,
                                     str(t.snapshot.get("camera_type") or ""),
                                     t.instance_name, str(t.key)))

    def _apply_filter(self, text):
        needle = (text or "").strip().lower()
        for tile in self._tiles.values():
            tile.filtered_out = needle not in tile.label.lower()
        self._relayout(force=True)

    def _update_summary(self):
        tiles = list(self._tiles.values())
        live = [t for t in tiles if not t.unreachable]
        running = sum(1 for t in live if t.record.get("status") == "running")
        setup = sum(1 for t in live if t.record.get("setup_mode"))
        errors = sum(int(t.record.get("errors") or 0) for t in live)
        relayed = sum(1 for t in tiles if t.source == MQTT)
        searched = (self._last_search.strftime("%H:%M:%S")
                    if self._last_search else "—")
        parts = [f"{len(tiles)} camera(s)", f"{running} running"]
        if setup:
            parts.append(f"{setup} in setup mode")
        if len(tiles) - len(live):
            parts.append(f"{len(tiles) - len(live)} not answering")
        if errors:
            parts.append(f"{errors} error(s) reported")
        if relayed:
            parts.append(f"{relayed} over MQTT")
        parts.append(f"last search {searched}")
        self.statusBar().showMessage("   ·   ".join(parts))

    def closeEvent(self, event):
        self._status_timer.stop()
        self._discovery_timer.stop()
        self.mqtt.disconnect_broker()
        self._tasks.wait_all()
        event.accept()


if __name__ == "__main__":
    main()
