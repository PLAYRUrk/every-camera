#!/usr/bin/env python3
"""
Every Camera — monitor.

Every camera as a tile: what it is, which machine it runs on, what it is doing
right now, and what has gone wrong.

Two ways of finding them, and the second is not a lesser one:

* **The local network** — cameras are found by the same UDP discovery
  ``viewer_app.py`` and ``focus_app.py`` use, and each tile is filled in from
  that camera's ``GET /api/status``. Nothing has to be configured.
* **Through a gateway** — every camera knows the others and will fetch from
  them on your behalf (``gateway.py``). Name one camera you can reach, in the
  strip at the bottom, and its neighbours appear as tiles too. This replaced a
  broker, and unlike the broker it carries everything: the tiles it produces
  have working Frames and Focus buttons, because the route is real.

Watching only. Frames belong to ``viewer_app.py`` and focusing to
``focus_app.py`` — this program used to fetch frames itself, which amounted to
a second, weaker viewer inside the monitor. The tiles now hand the camera over
to the right tool instead.

Usage:
    python monitor_app.py
    python monitor_app.py --host 192.168.1.5      # one discovery cannot see
    python monitor_app.py --via 192.168.1.5:8765  # and everything it knows
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
    one whichever way it was fetched — so a camera type that grows a field only
    has to publish it.
    """
    rows = []
    kind = (rec.get("camera_type") or "").lower()

    if rec.get("phase"):
        rows.append(("Phase", str(rec["phase"])))
    # A camera in setup mode is following no schedule at all; naming the one it
    # would have followed reads as though captures were being taken.
    if rec.get("mode") and not rec.get("setup_mode"):
        rows.append(("Schedule", str(rec["mode"])))

    # Both imagers report the same fields; the Japan camera simply has no
    # setpoint, and ``fmt_sensor`` already shows a bare reading when there is
    # none, so one branch serves both.
    if kind in ("asi", "japan"):
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
    parser.add_argument("--via", default="",
                        metavar="HOST[:PORT]",
                        help="Ask this camera for every camera it knows")
    parser.add_argument("--config", default=None, help="Path to config.json")
    args = parser.parse_args()

    from utils import can_use_gui

    if not can_use_gui():
        print("Error: no display available. The monitor needs a graphical "
              "environment; `python -m discovery` lists the cameras on the "
              "network from a terminal.")
        sys.exit(1)

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
    window = MonitorWindow(interval_ms=max(1000, int(args.interval * 1000)))
    for entry in args.host:
        window.add_manual(entry)
    window.show()
    if args.via:
        window.use_gateway(args.via)
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
from net_client import (                                            # noqa: E402
    CameraClient, TaskRunner, DiscoveryTask, node_name_of,
    load_settings, save_settings,
)

# A tile's key is (route, host, port). ``route`` is LAN for a camera this
# machine talks to directly, or the ``host:port`` of the gateway that will
# fetch for us — which is also exactly what CameraClient(via=...) wants.
LAN = "lan"


class CameraTile(QFrame):
    """One camera, as much of it as fits on a card."""

    details_requested = pyqtSignal(object)      # the whole status record

    def __init__(self, key, node, parent=None):
        super().__init__(parent)
        self.key = key                  # (route, host, port); see LAN above
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
        """``(host, port)`` of the camera itself, whichever way we reach it."""
        return (self.key[1], self.key[2])

    @property
    def via(self):
        """The gateway fetching for us, or None when we talk to it directly."""
        return None if self.source == LAN else self.source

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
            node_name_of(rec), self.address[0], self.via or "",
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
        where = f"{self.address[0]}:{self.address[1]}"
        if self.via:
            # Say which camera is fetching for us: "not answering" then points
            # at the right one of the two.
            where += f"  (via {self.via})"
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
        if rec.get("hold_effective"):
            # Distinguished from an ordinary focus session on purpose: this
            # camera has stopped measuring, and a monitor that only said
            # "someone is focusing" would leave that looking like a fault.
            notes.append("Measurements paused while someone focuses this camera.")
        elif rec.get("focus_active"):
            notes.append("Someone is focusing this camera.")
        if rec.get("focus_note"):
            notes.append(str(rec["focus_note"]))
        self.lbl_note.setText("  ".join(notes))
        self.lbl_note.setVisible(bool(notes))
        self._update_buttons(rec)

    def _update_buttons(self, rec):
        """Both work through a gateway too, which is why it replaced the broker."""
        self.btn_frames.setEnabled(not self.unreachable)
        self.btn_frames.setToolTip("Open viewer_app.py for this camera")
        focusable = bool(rec.get("supports_focus", True))
        self.btn_focus.setEnabled(not self.unreachable and focusable)
        self.btn_focus.setToolTip(
            "Open focus_app.py for this camera" if focusable
            else "This camera has no free-running focus mode")

    def _launch(self, program):
        """Hand this camera over to the program whose job that is."""
        host, port = self.address
        command = [sys.executable, os.path.join(APP_DIR, program),
                   "--host", str(host), "--port", str(port)]
        if self.via:
            command += ["--via", self.via]
        try:
            subprocess.Popen(command)
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


class GatewayPanel(QGroupBox):
    """The second way in: one camera, asked about all the others.

    Deliberately a strip at the bottom rather than a tab. Most of the time
    discovery finds everything and this stays collapsed; it earns its place on
    the network where broadcast does not cross a switch, or from a machine
    outside the cameras' segment with a route to exactly one of them.

    The address is remembered, so it has to be typed once. The tiles it
    produces are not second-class: the gateway forwards the archive, the live
    stream and the parameters, so Frames and Focus work on them.
    """

    nodes_received = pyqtSignal(str, object)     # gateway address, node list
    state_changed = pyqtSignal(str)

    SETTINGS_KEY = "monitor_gateway"

    def __init__(self, parent=None):
        super().__init__("Through a gateway — ask one camera about the rest", parent)
        self.setCheckable(True)
        self.setChecked(False)          # collapsed until someone wants it
        self._tasks = TaskRunner(self)
        self._asking = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 6)
        lay.setSpacing(6)

        self._widgets = []
        label = QLabel("Camera:")
        self.le_host = QLineEdit(str(load_settings().get(self.SETTINGS_KEY, "")))
        self.le_host.setPlaceholderText("host or host:port of any camera")
        self.le_host.setMaximumWidth(260)
        self.le_host.returnPressed.connect(lambda: self.ask(self.le_host.text()))
        lay.addWidget(label)
        lay.addWidget(self.le_host)
        self._widgets += [label, self.le_host]

        self.btn_ask = QPushButton("Ask")
        self.btn_ask.setToolTip("Fetch every camera this one knows about, and "
                                "watch them through it")
        self.btn_ask.clicked.connect(lambda: self.ask(self.le_host.text()))
        lay.addWidget(self.btn_ask)
        self._widgets.append(self.btn_ask)

        self.chk_auto = QCheckBox("Keep asking")
        self.chk_auto.setToolTip("Ask again every 20 s, so a camera started "
                                 "later turns up on its own")
        lay.addWidget(self.chk_auto)
        self._widgets.append(self.chk_auto)

        self.lbl_state = QLabel("not connected")
        self.lbl_state.setStyleSheet("color:#888; font-size:11px;")
        lay.addWidget(self.lbl_state, 1)
        self._widgets.append(self.lbl_state)

        self._timer = QTimer(self)
        self._timer.setInterval(DISCOVERY_EVERY_MS)
        self._timer.timeout.connect(lambda: self.ask(self.le_host.text()))
        self.chk_auto.toggled.connect(
            lambda on: self._timer.start() if on else self._timer.stop())

        self.toggled.connect(self._on_toggled)
        self._on_toggled(False)

    def _on_toggled(self, on):
        for widget in self._widgets:
            widget.setVisible(on)
        if not on:
            self._timer.stop()

    @staticmethod
    def normalise(address):
        """``host`` or ``host:port`` to the ``host:port`` a proxy URL needs."""
        text = str(address or "").strip()
        if not text:
            return ""
        host, _, port = text.partition(":")
        host = host.strip()
        if not host:
            return ""
        return f"{host}:{int(port) if port.strip().isdigit() else DEFAULT_HTTP_PORT}"

    def ask(self, address):
        """Fetch ``/api/nodes`` from ``address`` and hand the result on."""
        via = self.normalise(address)
        if not via:
            self._set_state("type the address of a camera you can reach", error=True)
            return
        if self._asking:
            return
        self.setChecked(True)
        self.le_host.setText(via)
        save_settings({self.SETTINGS_KEY: via})
        self._asking = True
        self._set_state(f"asking {via}…")

        host, _, port = via.partition(":")
        client = CameraClient(host, port, timeout=6.0)
        self._tasks.run(client.nodes,
                        lambda nodes, v=via: self._on_nodes(v, nodes),
                        lambda msg, v=via: self._on_failed(v, msg))

    def _on_nodes(self, via, nodes):
        self._asking = False
        nodes = nodes or []
        self._set_state(f"{via}: {len(nodes)} camera(s)")
        self.nodes_received.emit(via, nodes)

    def _on_failed(self, via, message):
        self._asking = False
        self._set_state(f"{via}: {message}", error=True)

    def _set_state(self, text, error=False):
        self.lbl_state.setText(text)
        self.lbl_state.setStyleSheet(
            f"color:{'#c0392b' if error else '#888'}; font-size:11px;")
        self.state_changed.emit(text)


class MonitorWindow(QMainWindow):
    def __init__(self, interval_ms=STATUS_EVERY_MS):
        super().__init__()
        self.setWindowTitle("Every Camera — monitor")
        self.resize(1080, 760)

        self._tasks = TaskRunner(self)
        self._tiles = {}             # key -> CameraTile
        self._polling = set()        # LAN keys with a status request in flight
        self._discovery = None
        self._columns = 0
        self._last_search = None

        self._build_ui()

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
    def _build_ui(self):
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

        self.gateway = GatewayPanel()
        self.gateway.nodes_received.connect(self._on_gateway_nodes)
        self.gateway.state_changed.connect(lambda _t: self._update_summary())
        root.addWidget(self.gateway)

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
            else:
                tile.seen(node)
                tile.refresh()
            # Reached first-hand now, so a forwarded tile for it is one hop of
            # hearsay too many.
            self._drop_forwarded_twin(key)
        for key, tile in self._tiles.items():
            # A camera added by hand never answers discovery, and one reached
            # through a gateway is not on this network at all: both are
            # judged by their own route, not by this probe.
            if key in seen or tile.via or tile.node.get("manual"):
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

    def _drop_forwarded_twin(self, lan_key):
        """A camera we can reach directly does not also need a forwarded tile.

        Same camera, same address, two routes to it. The direct one is a
        first-hand reading and one hop shorter, so the forwarded tile goes.
        """
        _, host, port = lan_key
        for key in [k for k in self._tiles
                    if k[0] != LAN and k[1] == host and k[2] == port]:
            self._remove_tile(key)

    # -- through a gateway ---------------------------------------------------
    def use_gateway(self, address):
        """Ask ``address`` for every camera it knows."""
        self.gateway.setChecked(True)
        self.gateway.ask(address)

    def _on_gateway_nodes(self, via, nodes):
        for node in nodes or []:
            host = node.get("host")
            port = int(node.get("http_port") or DEFAULT_HTTP_PORT)
            if not host:
                continue
            if (LAN, host, port) in self._tiles:
                continue        # already reached first-hand
            key = (via, host, port)
            tile = self._tiles.get(key)
            if tile is None:
                tile = self._add_tile(key, node)
            else:
                tile.seen(node)
            status = node.get("status")
            if status:
                tile.set_status(status)
        self._apply_filter(self.le_filter.text())
        self._poll_all()
        self._update_summary()

    # -- reading status -----------------------------------------------------
    def _poll_all(self):
        for key, tile in list(self._tiles.items()):
            if key in self._polling:
                continue        # a slow camera must not queue up requests
            self._polling.add(key)
            client = CameraClient(key[1], key[2], timeout=4.0, via=tile.via)
            self._tasks.run(client.status,
                            lambda rec, k=key: self._on_status(k, rec),
                            lambda msg, k=key: self._on_status_failed(k, msg))

    def _on_status(self, key, record):
        self._polling.discard(key)
        tile = self._tiles.get(key)
        if tile is None:
            return
        tile.set_status(record)
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
                      key=lambda t: (bool(t.via),
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
        relayed = sum(1 for t in tiles if t.via)
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
            parts.append(f"{relayed} through a gateway")
        parts.append(f"last search {searched}")
        self.statusBar().showMessage("   ·   ".join(parts))

    def closeEvent(self, event):
        self._status_timer.stop()
        self._discovery_timer.stop()
        self._tasks.wait_all()
        event.accept()


if __name__ == "__main__":
    main()
