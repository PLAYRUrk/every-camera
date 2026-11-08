"""Tabbed camera control. Closing the window rewrites config.json, or the --config file."""
import os
import sys
import time
import json
import argparse
import threading

import numpy as np

from datetime import datetime as dt
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QTextEdit,
    QTableWidget, QTableWidgetItem, QComboBox, QSpinBox, QDoubleSpinBox, QSlider,
    QFileDialog, QHeaderView, QMessageBox, QGroupBox, QCheckBox,
    QAbstractItemView, QSizePolicy, QScrollArea, QStatusBar,
)
from PyQt5.QtCore import QThread, pyqtSignal, QTimer, Qt, QObject
from PyQt5.QtGui import QColor, QFont, QTextCursor, QImage, QPixmap

import frame_archive

from utils import (
    load_config, save_config, can_use_gui, get_instance_name,
    claim_instance_name,
    parse_schedule_text, load_schedule_file, save_schedule_file,
    load_schedule_safe,
    write_status_file,
    SCHEDULE_DT_FMT, HOME_STATUS_DIR, APP_DIR, schedule_now,
)
from monitor import MonitorWidget


# Closing the window waits this long for an exposure in progress to end.
WORKER_CLOSE_WAIT = 120


def _make_dirs(parent, *dirs):
    """False, having told the operator, if a directory cannot be created."""
    for directory in dirs:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(parent, "Directory", f"Cannot create {directory}:\n{exc}")
            return False
    return True


def _reload_section(cfg, name):
    """setup_app may have changed the file since the GUI read it; Start uses that."""
    path = getattr(cfg, "source_path", None)
    if not path or getattr(cfg, "load_error", None):
        return
    fresh = load_config(path)
    if not fresh.load_error and isinstance(fresh.get(name), dict):
        cfg[name] = fresh[name]


def _set_nested(target, updates):
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _set_nested(target[key], value)
        else:
            target[key] = value


def _fill_schedule_table(table, entries):
    table.setRowCount(0)
    for e in entries:
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(e.start.strftime(SCHEDULE_DT_FMT)))
        table.setItem(row, 1, QTableWidgetItem(e.end.strftime(SCHEDULE_DT_FMT)))


def _release_claim(tab):
    claim = getattr(tab, "_claim", None)
    if claim is not None:
        claim.release()
        tab._claim = None


def _start_tab_server(cfg, camera_type, instance_name, output_dir,
                      setup_mode=False):
    """Returns ``(service, server)``; ``server`` is None when disabled or unbound, and the
    measurement goes ahead either way. Tabs share one ``cfg["server"]``, so several servers
    rely on the port search in :func:`frame_server.start_frame_server`.
    """
    from camera_service import CameraService
    from frame_server import start_frame_server
    from utils import get_node_name
    service = CameraService(camera_type, instance_name, output_dir,
                            node_name=get_node_name(cfg), setup_mode=setup_mode)
    return service, start_frame_server(cfg.get("server", {}), service)


_RUN_STATUS_TEXT = {"running": "Running", "waiting": "Waiting for schedule",
                    "error": "Error", "stopped": "Stopped"}


def run_status_text(snap):
    name = str(snap.get("status") or "")
    return _RUN_STATUS_TEXT.get(name, name.capitalize() or "—")


class ConsoleRun(QObject):
    """A console worker run from a tab, the way AsiTab runs its own: one implementation
    of the schedule for main.py, systemd and the GUI. Log lines arrive through a
    ``console_ui`` sink; the status is read back from the CameraService.
    """

    log_line = pyqtSignal(str, str)
    status = pyqtSignal(dict)
    finished = pyqtSignal()

    def __init__(self, worker, service, log_fn, interval_ms=1000):
        super().__init__()
        self.worker = worker
        self._service = service
        self._done = False
        self.log_line.connect(log_fn)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

    def _sink(self, msg, level):
        # Called from the worker thread — hop to the GUI thread via the signal.
        self.log_line.emit(str(msg), str(level).lower())

    def start(self):
        import console_ui
        console_ui.add_sink(self._sink, owner=lambda: self.worker)
        self.worker.start()
        self._timer.start()

    def is_running(self):
        return self.worker.is_alive()

    def stop(self, wait=5.0):
        """Waits up to ``wait`` seconds; a worker still writing finishes on its own and
        ``finished`` follows.
        """
        self.worker.request_stop()
        self.worker.join(timeout=wait)
        self._poll()

    def _poll(self):
        if self._service is not None:
            self.status.emit(self._service.status())
        if not self.worker.is_alive() and not self._done:
            import console_ui
            self._done = True
            self._timer.stop()
            console_ui.remove_sink(self._sink)
            self.finished.emit()


class CannonConnectThread(QThread):
    connected = pyqtSignal(object, object, str)  # cam, config, model_name
    failed = pyqtSignal(str)

    def __init__(self, cannon_cfg=None):
        super().__init__()
        self._cannon_cfg = cannon_cfg or {}

    def run(self):
        try:
            from cannon_driver import (release_camera_usb, detect_model,
                                       apply_camcfg, apply_shutterspeed)
            import gphoto2cffi as gp
            release_camera_usb()
            cam = gp.Camera()
            config = cam._get_config()
            model_name = detect_model(config)
            apply_camcfg(config, model_name,
                         self._cannon_cfg.get("camcfg_file", ""))
            apply_shutterspeed(config, self._cannon_cfg.get("shutterspeed", ""))
            self.connected.emit(cam, config, model_name)
        except Exception as e:
            self.failed.emit(str(e))


class CannonTab(QWidget):
    def __init__(self, cfg, log_fn):
        super().__init__()
        self._cfg = cfg
        self._log = log_fn
        self.cam = None
        self.config = None
        self.model_name = None
        self._params = []
        self.worker = None
        self.connect_thread = None
        self._server = None
        self._service = None
        self._build_ui()
        self._load_config()

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(200)
        self._countdown_timer.timeout.connect(self._update_idle_countdown)
        self._countdown_timer.start()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        panel = QWidget()
        lay = QVBoxLayout(panel)

        cam_box = QGroupBox("Canon Camera")
        cam_lay = QHBoxLayout(cam_box)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        self.lbl_model = QLabel("Not connected")
        self.lbl_model.setStyleSheet("color:#cc0000; font-weight:bold;")
        cam_lay.addWidget(self.btn_connect)
        cam_lay.addWidget(self.btn_disconnect)
        cam_lay.addWidget(self.lbl_model, 1)
        lay.addWidget(cam_box)

        sess_box = QGroupBox("Session")
        sess_grid = QGridLayout(sess_box)
        sess_grid.setColumnStretch(1, 1)

        sess_grid.addWidget(QLabel("Instance name:"), 0, 0)
        self.le_instance = QLineEdit()
        sess_grid.addWidget(self.le_instance, 0, 1, 1, 2)

        sess_grid.addWidget(QLabel("Output directory:"), 1, 0)
        self.le_output = QLineEdit()
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(lambda: self._browse_dir(self.le_output))
        sess_grid.addWidget(self.le_output, 1, 1)
        sess_grid.addWidget(btn_browse, 1, 2)

        sess_grid.addWidget(QLabel("Capture seconds:"), 2, 0)
        self.le_cap_seconds = QLineEdit("0, 30")
        self.le_cap_seconds.setToolTip("Comma-separated seconds of each minute (e.g. 0, 15, 30, 45)")
        sess_grid.addWidget(self.le_cap_seconds, 2, 1, 1, 2)

        lay.addWidget(sess_box)

        sched_box = QGroupBox("Measurement Schedule")
        sched_lay = QVBoxLayout(sched_box)
        self.sched_table = QTableWidget(0, 2)
        self.sched_table.setHorizontalHeaderLabels(["Start UTC (YYYY-MM-DD HH:MM:SS)",
                                                     "End UTC (YYYY-MM-DD HH:MM:SS)"])
        self.sched_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.sched_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        sched_lay.addWidget(self.sched_table, 1)

        sched_btns = QHBoxLayout()
        btn_add = QPushButton("+ Add")
        btn_add.clicked.connect(self._add_schedule_row)
        btn_del = QPushButton("- Remove")
        btn_del.clicked.connect(self._del_schedule_row)
        btn_load = QPushButton("Load...")
        btn_load.clicked.connect(self._load_schedule)
        btn_save = QPushButton("Save...")
        btn_save.clicked.connect(self._save_schedule)
        sched_btns.addWidget(btn_add)
        sched_btns.addWidget(btn_del)
        sched_btns.addStretch()
        sched_btns.addWidget(btn_load)
        sched_btns.addWidget(btn_save)
        sched_lay.addLayout(sched_btns)
        lay.addWidget(sched_box, 1)

        ctrl_box = QGroupBox("Control")
        ctrl_lay = QHBoxLayout(ctrl_box)
        self.btn_start = QPushButton("START")
        self.btn_start.setEnabled(False)
        self.btn_start.setStyleSheet("background:#2a7a2a; color:white; font-weight:bold; padding:6px 18px;")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet("background:#7a2a2a; color:white; font-weight:bold; padding:6px 18px;")
        self.btn_stop.clicked.connect(self._on_stop)
        self.lbl_status = QLabel("Idle")
        self.lbl_status.setStyleSheet("font-weight:bold; color:#555;")
        self.lbl_shots = QLabel("Shots: 0")
        self.lbl_errors = QLabel("")
        self.lbl_countdown = QLabel("")
        ctrl_lay.addWidget(self.btn_start)
        ctrl_lay.addWidget(self.btn_stop)
        ctrl_lay.addSpacing(16)
        ctrl_lay.addWidget(QLabel("Status:"))
        ctrl_lay.addWidget(self.lbl_status)
        ctrl_lay.addSpacing(16)
        ctrl_lay.addWidget(self.lbl_shots)
        ctrl_lay.addWidget(self.lbl_errors)
        ctrl_lay.addStretch()
        ctrl_lay.addWidget(self.lbl_countdown)
        lay.addWidget(ctrl_box)

        scroll.setWidget(panel)
        outer.addWidget(scroll)

    def _load_config(self):
        c = self._cfg.get("cannon", {})
        self.le_instance.setText(c.get("instance_name")
                                 or get_instance_name("Cannon", self._cfg))
        self.le_output.setText(c.get("output_dir", ""))
        secs = c.get("capture_seconds", [0, 30])
        self.le_cap_seconds.setText(", ".join(str(s) for s in secs))
        self._load_configured_schedule(c)

    def _load_configured_schedule(self, section):
        if not str(section.get("schedule_file") or "").strip():
            return
        entries, errors, reason = load_schedule_safe(section["schedule_file"])
        if reason:
            self._log(f"Schedule: {reason}", "warn")
        _fill_schedule_table(self.sched_table, entries)

    def config_updates(self):
        updates = {"output_dir": self.le_output.text().strip()}
        secs = self._parse_capture_seconds()
        if secs:
            updates["capture_seconds"] = secs
        return {"cannon": updates}

    def _browse_dir(self, line_edit):
        d = QFileDialog.getExistingDirectory(self, "Select directory")
        if d:
            line_edit.setText(d)

    def _parse_capture_seconds(self):
        text = self.le_cap_seconds.text()
        try:
            secs = [int(s.strip()) for s in text.split(",") if s.strip()]
            return sorted({s for s in secs if 0 <= s < 60})
        except ValueError:
            return []

    def _on_connect(self):
        self.btn_connect.setEnabled(False)
        self.lbl_model.setText("Connecting...")
        self.lbl_model.setStyleSheet("color:#888; font-weight:bold;")
        self._log("Connecting to Canon camera...", "info")
        self.connect_thread = CannonConnectThread(self._cfg.get("cannon", {}))
        self.connect_thread.connected.connect(self._on_connected)
        self.connect_thread.failed.connect(self._on_failed)
        self.connect_thread.start()

    def _on_connected(self, cam, config, model_name):
        self.cam = cam
        self.config = config
        self.model_name = model_name
        self.lbl_model.setText(f"Connected: {model_name}")
        self.lbl_model.setStyleSheet("color:#007700; font-weight:bold;")
        self.btn_disconnect.setEnabled(True)
        self.btn_start.setEnabled(True)
        self._log(f"Connected: {model_name}", "info")

    def _on_failed(self, msg):
        self.lbl_model.setText("Connection failed")
        self.lbl_model.setStyleSheet("color:#cc0000; font-weight:bold;")
        self.btn_connect.setEnabled(True)
        self._log(f"Failed: {msg}", "error")

    def _on_disconnect(self):
        self._on_stop()
        self.cam = None
        self.config = None
        self.lbl_model.setText("Not connected")
        self.lbl_model.setStyleSheet("color:#cc0000; font-weight:bold;")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        self.btn_start.setEnabled(False)

    def _add_schedule_row(self):
        row = self.sched_table.rowCount()
        self.sched_table.insertRow(row)
        now = schedule_now().replace(second=0, microsecond=0)
        end = now.replace(hour=23, minute=59)
        self.sched_table.setItem(row, 0, QTableWidgetItem(now.strftime(SCHEDULE_DT_FMT)))
        self.sched_table.setItem(row, 1, QTableWidgetItem(end.strftime(SCHEDULE_DT_FMT)))

    def _del_schedule_row(self):
        rows = sorted({idx.row() for idx in self.sched_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.sched_table.removeRow(r)

    def _load_schedule(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load schedule", "", "Text files (*.txt);;All (*)")
        if not path:
            return
        entries, errors = load_schedule_file(path)
        if errors:
            QMessageBox.warning(self, "Parse errors", "\n".join(errors))
        self.sched_table.setRowCount(0)
        for e in entries:
            row = self.sched_table.rowCount()
            self.sched_table.insertRow(row)
            self.sched_table.setItem(row, 0, QTableWidgetItem(e.start.strftime(SCHEDULE_DT_FMT)))
            self.sched_table.setItem(row, 1, QTableWidgetItem(e.end.strftime(SCHEDULE_DT_FMT)))
        self._log(f"Loaded {len(entries)} intervals", "info")

    def _save_schedule(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save schedule", "", "Text files (*.txt);;All (*)")
        if not path:
            return
        entries, errors = parse_schedule_text(self._table_to_text())
        if errors:
            QMessageBox.warning(self, "Errors", "\n".join(errors))
            return
        save_schedule_file(path, entries)
        self._log(f"Saved {len(entries)} intervals", "info")

    def _table_to_text(self):
        lines = []
        for row in range(self.sched_table.rowCount()):
            s = self.sched_table.item(row, 0)
            e = self.sched_table.item(row, 1)
            if s and e:
                lines.append(f"{s.text().strip()} - {e.text().strip()}")
        return "\n".join(lines)

    def _on_start(self):
        if not self.cam:
            QMessageBox.warning(self, "No camera", "Connect camera first.")
            return
        output_dir = self.le_output.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "No output", "Select output directory.")
            return

        status_dir = self._cfg.get("status_dir") or HOME_STATUS_DIR
        entries, errors = parse_schedule_text(self._table_to_text())
        if errors:
            QMessageBox.warning(self, "Schedule errors", "\n".join(errors))
            return

        if not _make_dirs(self, output_dir, status_dir):
            return

        self._claim = claim_instance_name(
            self.le_instance.text().strip() or get_instance_name("Cannon", self._cfg))
        instance_name = self._claim.name
        setup_mode = not entries
        if setup_mode:
            self._log("Schedule is empty — starting in setup mode: live frames "
                      "for focus_app.py, nothing archived.")
        capture_seconds = self._parse_capture_seconds()
        if not capture_seconds:
            QMessageBox.warning(self, "Capture seconds",
                                "Give at least one second of the minute, 0..59.")
            _release_claim(self)
            return
        self._service, self._server = _start_tab_server(
            self._cfg, "cannon", instance_name, output_dir, setup_mode)

        from cameras.cannon_driver import CannonWorkerConsole
        from utils import get_node_name
        self.worker = ConsoleRun(CannonWorkerConsole(
            cam=self.cam, config=self.config, schedule=entries,
            output_dir=output_dir, instance_name=instance_name,
            status_dir=status_dir, capture_seconds=capture_seconds,
            service=self._service, node_name=get_node_name(self._cfg),
            setup_mode=setup_mode,
        ), self._service, self._log)
        self.worker.status.connect(self._on_run_status)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

    def _on_stop(self):
        if self.worker and self.worker.is_running():
            self.worker.stop()
        if self._server:
            self._server.stop()
            self._server = None
        _release_claim(self)
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(self.cam is not None)
        self._on_status_msg("Idle")
        self.lbl_countdown.setText("")

    def _on_worker_finished(self):
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(self.cam is not None)
        self._on_status_msg("Idle")
        self.lbl_countdown.setText("")

    def _on_run_status(self, snap):
        self.lbl_shots.setText(f"Shots: {snap.get('shots_taken', 0)}")
        errors = snap.get("errors") or 0
        self.lbl_errors.setText(f"Errors: {errors}" if errors else "")
        if snap.get("status"):
            self._on_status_msg(run_status_text(snap))

    def _on_status_msg(self, msg):
        colors = {"Running": "#007700", "Waiting for schedule": "#aa6600",
                  "Idle": "#555", "Stopped": "#555"}
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(f"font-weight:bold; color:{colors.get(msg, '#555')};")

    def _update_idle_countdown(self):
        now = dt.now()
        sec = now.second + now.microsecond / 1_000_000
        cap_secs = self._parse_capture_seconds()
        next_secs = [s for s in cap_secs if s > sec]
        if next_secs:
            remaining = next_secs[0] - sec
        else:
            remaining = (60 - sec) + (cap_secs[0] if cap_secs else 0)
        self.lbl_countdown.setText(f"Next: {remaining:.1f}s")

    def cleanup(self):
        self._on_stop()


# Binning codes offered in the SPTT tab, in combo-box order.
SPTT_BINNINGS = [0, 1, 3]


class SpttCaptureThread(QThread):
    frame_ready = pyqtSignal(np.ndarray)
    error = pyqtSignal(str)
    status_msg = pyqtSignal(str)

    MAX_CONSECUTIVE_ERRORS = 5
    RETRY_DELAY = 0.1

    def __init__(self, cam):
        super().__init__()
        self.cam = cam
        self._running = False

    def run(self):
        import usb.core
        from sptt_driver import _usb_write_retry, make_command, CMD_FIFO_INIT

        self._running = True
        consecutive_errors = 0

        while self._running:
            try:
                frame = self.cam.grab_frame()
                self.frame_ready.emit(frame)
                consecutive_errors = 0
            except usb.core.USBError as e:
                if not self._running:
                    break
                consecutive_errors += 1
                self.status_msg.emit(f"USB error ({consecutive_errors}): {e}")
                if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    self.error.emit(f"Too many USB errors: {e}")
                    break
                try:
                    self.cam._flush_endpoints()
                    _usb_write_retry(self.cam.ep_wr, make_command(CMD_FIFO_INIT))
                    time.sleep(self.RETRY_DELAY * consecutive_errors)
                except Exception:
                    pass
            except RuntimeError as e:
                if not self._running:
                    break
                consecutive_errors += 1
                self.status_msg.emit(f"Frame error ({consecutive_errors}): {e}")
                if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    self.error.emit(str(e))
                    break
                try:
                    self.cam._flush_endpoints()
                    _usb_write_retry(self.cam.ep_wr, make_command(CMD_FIFO_INIT))
                    time.sleep(self.RETRY_DELAY * consecutive_errors)
                except Exception:
                    pass
            except Exception as e:
                if self._running:
                    import traceback
                    self.error.emit(f"Unexpected: {e}\n{traceback.format_exc()}")
                break

    def stop(self):
        self._running = False
        self.wait(5000)


class SpttTab(QWidget):
    def __init__(self, cfg, log_fn):
        super().__init__()
        self._cfg = cfg
        self._log = log_fn
        self.cam = None
        self.capture_thread = None
        self.scheduled_worker = None
        self.current_frame = None
        self.frame_count = 0
        self.fps_time = time.time()
        self.fps = 0.0
        self._server = None
        self._service = None
        self._build_ui()
        self._load_config()

    def _build_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(2, 2, 2, 2)

        self.image_label = QLabel("No image")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(200, 150)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.setStyleSheet("background-color: #1a1a1a; color: #888;")
        main_layout.addWidget(self.image_label, stretch=3)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        scroll.setMinimumWidth(250)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)

        grp_conn = QGroupBox("Connection")
        gl = QVBoxLayout(grp_conn)
        self.btn_connect = QPushButton("Connect")
        self.btn_reconnect = QPushButton("Reconnect")
        self.btn_reconnect.setEnabled(False)
        h = QHBoxLayout()
        self.btn_preview_start = QPushButton("Preview")
        self.btn_preview_stop = QPushButton("Stop Preview")
        self.btn_preview_start.setEnabled(False)
        self.btn_preview_stop.setEnabled(False)
        h.addWidget(self.btn_preview_start)
        h.addWidget(self.btn_preview_stop)
        gl.addWidget(self.btn_connect)
        gl.addWidget(self.btn_reconnect)
        gl.addLayout(h)
        layout.addWidget(grp_conn)

        grp_live = QGroupBox("Exposure / Gain")
        g = QGridLayout(grp_live)
        g.addWidget(QLabel("Exposure (s):"), 0, 0)
        self.spin_exp = QDoubleSpinBox()
        self.spin_exp.setRange(0.000006, 3600.0)
        self.spin_exp.setValue(0.88)
        self.spin_exp.setSingleStep(0.01)
        self.spin_exp.setDecimals(6)
        g.addWidget(self.spin_exp, 0, 1)
        g.addWidget(QLabel("Gain:"), 1, 0)
        self.spin_gain = QSpinBox()
        self.spin_gain.setRange(0, 1023)
        self.spin_gain.setValue(100)
        g.addWidget(self.spin_gain, 1, 1)
        layout.addWidget(grp_live)

        grp_cap = QGroupBox("Capture Settings (Apply)")
        g2 = QGridLayout(grp_cap)
        g2.addWidget(QLabel("Binning:"), 0, 0)
        self.combo_binning = QComboBox()
        # The binnings setup_app and the console wizard offer; the camera has no 3x3.
        self.combo_binning.addItems(["0 - 1x1", "1 - 2x2", "3 - 4x4"])
        g2.addWidget(self.combo_binning, 0, 1)
        g2.addWidget(QLabel("Encoding:"), 1, 0)
        self.combo_encoding = QComboBox()
        self.combo_encoding.addItems(["0 - 8 bit", "1 - 12 bit"])
        self.combo_encoding.setCurrentIndex(1)
        g2.addWidget(self.combo_encoding, 1, 1)
        # A checkbox, not "0 means off": 0 °C must stay possible.
        self.chk_temp = QCheckBox("Target Temp:")
        g2.addWidget(self.chk_temp, 2, 0)
        self.spin_temp = QSpinBox()
        self.spin_temp.setRange(-30, 30)
        self.spin_temp.setValue(-10)
        self.spin_temp.setEnabled(False)
        self.chk_temp.toggled.connect(self.spin_temp.setEnabled)
        g2.addWidget(self.spin_temp, 2, 1)
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setEnabled(False)
        g2.addWidget(self.btn_apply, 3, 0, 1, 2)
        layout.addWidget(grp_cap)

        grp_meas = QGroupBox("Scheduled Measurement (FITS at :00 and :30)")
        meas_lay = QVBoxLayout(grp_meas)
        m_grid = QGridLayout()
        m_grid.addWidget(QLabel("Instance:"), 0, 0)
        self.le_instance = QLineEdit()
        m_grid.addWidget(self.le_instance, 0, 1)
        m_grid.addWidget(QLabel("Output dir:"), 1, 0)
        self.le_output = QLineEdit()
        btn_browse = QPushButton("...")
        btn_browse.setMaximumWidth(40)
        btn_browse.clicked.connect(lambda: self._browse_dir(self.le_output))
        m_grid.addWidget(self.le_output, 1, 1)
        m_grid.addWidget(btn_browse, 1, 2)
        meas_lay.addLayout(m_grid)

        meas_btns = QHBoxLayout()
        self.btn_meas_start = QPushButton("START Measurement")
        self.btn_meas_start.setEnabled(False)
        self.btn_meas_start.setStyleSheet("background:#2a7a2a; color:white; font-weight:bold;")
        self.btn_meas_stop = QPushButton("STOP")
        self.btn_meas_stop.setEnabled(False)
        self.btn_meas_stop.setStyleSheet("background:#7a2a2a; color:white; font-weight:bold;")
        meas_btns.addWidget(self.btn_meas_start)
        meas_btns.addWidget(self.btn_meas_stop)
        meas_lay.addLayout(meas_btns)

        self.lbl_meas_status = QLabel("Idle")
        self.lbl_meas_status.setStyleSheet("font-weight:bold; color:#555;")
        self.lbl_meas_shots = QLabel("Shots: 0")
        self.lbl_meas_countdown = QLabel("")
        info_lay = QHBoxLayout()
        info_lay.addWidget(self.lbl_meas_status)
        info_lay.addWidget(self.lbl_meas_shots)
        info_lay.addStretch()
        info_lay.addWidget(self.lbl_meas_countdown)
        meas_lay.addLayout(info_lay)

        layout.addWidget(grp_meas)
        layout.addStretch()

        scroll.setWidget(panel)
        main_layout.addWidget(scroll, stretch=1)

        self.lbl_statusbar = QLabel("")

        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_reconnect.clicked.connect(self._on_reconnect)
        self.btn_preview_start.clicked.connect(self._on_preview_start)
        self.btn_preview_stop.clicked.connect(self._on_preview_stop)
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_meas_start.clicked.connect(self._on_meas_start)
        self.btn_meas_stop.clicked.connect(self._on_meas_stop)
        self.spin_exp.editingFinished.connect(self._on_exp_changed)
        self.spin_gain.editingFinished.connect(self._on_gain_changed)

    def _load_config(self):
        c = self._cfg.get("sptt", {})
        self.le_instance.setText(c.get("instance_name")
                                 or get_instance_name("SPTT", self._cfg))
        self.le_output.setText(c.get("output_dir", ""))
        self.spin_exp.setValue(c.get("exposure", 0.88))
        # Not on the form: the period follows the exposure unless config.json
        # overrides it, and this tab is not where that decision is made.
        self._period_us = c.get("period_us")
        self._trigmode = c.get("trigmode")
        self._firmware_dir = c.get("firmware_dir", "")
        # Colour settings are not on this tab either; they come from config.json.
        self._sptt_cfg = c
        self.spin_gain.setValue(c.get("gain", 100))
        binning = c.get("binning", 0)
        self.combo_binning.setCurrentIndex(
            SPTT_BINNINGS.index(binning) if binning in SPTT_BINNINGS else 0)
        self.combo_encoding.setCurrentIndex(c.get("encoding", 1))
        self._capture_seconds = c.get("capture_seconds")
        if c.get("target_temp") is not None:
            # setup_app stores it as a float; QSpinBox.setValue takes an int.
            self.spin_temp.setValue(int(round(float(c["target_temp"]))))
            self.chk_temp.setChecked(True)

    def config_updates(self):
        return {"sptt": {"output_dir": self.le_output.text().strip()}}

    def _browse_dir(self, le):
        d = QFileDialog.getExistingDirectory(self, "Select directory")
        if d:
            le.setText(d)

    def _on_connect(self):
        self._log("Initializing SPTT camera...", "info")
        self.btn_connect.setEnabled(False)
        QApplication.processEvents()

        try:
            from sptt_driver import (
                colour_offsets, ensure_firmware_loaded, find_libusb_backend,
                make_camera, usb_port_setting,
            )
            backend = find_libusb_backend()
            found = ensure_firmware_loaded(backend, self._firmware_dir,
                                           usb_port_setting(self._sptt_cfg))
            if not found:
                raise RuntimeError(
                    "The camera could not be brought up — see the log: no "
                    "camera, several cameras and no sptt.usb_port, or no "
                    "firmware where it was looked for.")
            time.sleep(1.5)
            self.cam = make_camera(backend, found, self._sptt_cfg)
            self.cam.open()
            self.cam.configure(
                exposure=self.spin_exp.value(),
                gain=self.spin_gain.value(),
                binning=SPTT_BINNINGS[self.combo_binning.currentIndex()],
                encoding=self.combo_encoding.currentIndex(),
                target_temp=self.spin_temp.value() if self.chk_temp.isChecked() else None,
                period_us=self._period_us,
                trigmode=self._trigmode,
                **colour_offsets(self._sptt_cfg),
            )
            self.btn_reconnect.setEnabled(True)
            self.btn_preview_start.setEnabled(True)
            self.btn_apply.setEnabled(True)
            self.btn_meas_start.setEnabled(True)
            self._log(f"SPTT connected: {found.model.name} on USB port {found.port}, "
                      f"{self.cam.w}x{self.cam.h}", "info")
        except Exception as e:
            QMessageBox.critical(self, "Connection Error", str(e))
            self.btn_connect.setEnabled(True)
            self._log(f"SPTT connection failed: {e}", "error")

    def _on_reconnect(self):
        if self.capture_thread:
            self.capture_thread.stop()
            self.capture_thread = None
        if self.cam:
            self.cam.close()
            self.cam = None
        time.sleep(1.0)
        self._on_connect()

    def _on_preview_start(self):
        if not self.cam:
            return
        self.cam.cancel_event.clear()
        try:
            self.cam.start()
        except Exception as e:
            QMessageBox.critical(self, "Start Error", str(e))
            return
        self.capture_thread = SpttCaptureThread(self.cam)
        self.capture_thread.frame_ready.connect(self._on_frame)
        self.capture_thread.error.connect(self._on_preview_error)
        self.capture_thread.start()
        self.frame_count = 0
        self.fps_time = time.time()
        self.btn_preview_start.setEnabled(False)
        self.btn_preview_stop.setEnabled(True)

    def _on_preview_stop(self):
        if self.capture_thread:
            self.capture_thread.stop()
            self.capture_thread = None
        if self.cam:
            try:
                self.cam.stop()
            except Exception:
                pass
        self.btn_preview_start.setEnabled(True)
        self.btn_preview_stop.setEnabled(False)

    def _on_frame(self, frame):
        self.current_frame = frame
        self.frame_count += 1
        now = time.time()
        elapsed = now - self.fps_time
        if elapsed >= 1.0:
            self.fps = self.frame_count / elapsed
            self.frame_count = 0
            self.fps_time = now

        # The driver hands every encoding over on the 0..65535 scale.
        if self.cam is not None:
            # A colour camera's mosaic comes out as (H, W, 3) RGB, the byte
            # order Format_RGB888 reads.
            frame = self.cam.for_display(frame)
        display = np.ascontiguousarray(
            frame_archive.to_uint8(frame, stretch="raw"))
        h, w = display.shape[:2]
        if display.ndim == 3:
            qimg = QImage(display.data, w, h, 3 * w, QImage.Format_RGB888)
        else:
            qimg = QImage(display.data, w, h, w, QImage.Format_Grayscale8)
        pixmap = QPixmap.fromImage(qimg)
        label_size = self.image_label.size()
        scaled = pixmap.scaled(label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)

    def _on_preview_error(self, msg):
        self._on_preview_stop()
        QMessageBox.warning(self, "Preview Error", msg)

    def _on_apply(self):
        was_previewing = self.capture_thread is not None
        if was_previewing:
            self._on_preview_stop()
        if not self.cam:
            return
        try:
            self.cam.configure(
                exposure=self.spin_exp.value(),
                gain=self.spin_gain.value(),
                binning=SPTT_BINNINGS[self.combo_binning.currentIndex()],
                encoding=self.combo_encoding.currentIndex(),
                target_temp=self.spin_temp.value() if self.chk_temp.isChecked() else None,
                period_us=self._period_us,
                trigmode=self._trigmode,
            )
            self._log(f"SPTT settings applied: {self.cam.w}x{self.cam.h}", "info")
        except Exception as e:
            QMessageBox.critical(self, "Configure Error", str(e))
            return
        if was_previewing:
            self._on_preview_start()

    def _on_exp_changed(self):
        if self.cam:
            try:
                self.cam.set_exposure(self.spin_exp.value())
            except Exception as e:
                # A refused exposure would otherwise truncate the frame silently.
                self._log(f"Exposure not applied: {e}", "warn")

    def _on_gain_changed(self):
        if self.cam:
            try:
                self.cam.set_gain(self.spin_gain.value())
            except Exception:
                pass

    def _on_meas_start(self):
        if not self.cam:
            QMessageBox.warning(self, "No camera", "Connect SPTT camera first.")
            return
        output_dir = self.le_output.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "No output", "Select output directory.")
            return

        if self.capture_thread:
            self._on_preview_stop()

        status_dir = self._cfg.get("status_dir") or HOME_STATUS_DIR
        if not _make_dirs(self, output_dir, status_dir):
            return

        from cameras.common.sun_gate import SunSettingsError, gate_from_config
        from cameras.sptt_driver import SpttWorkerConsole, intensity_settings
        from utils import get_node_name
        preflight, overexposure, problems = intensity_settings(self._sptt_cfg)
        for problem in problems:
            self._log(problem, "warn")
        try:
            sun_gate = gate_from_config(
                self._sptt_cfg,
                start_angle=preflight.sun_start_angle if preflight.enabled else None)
        except SunSettingsError as exc:
            QMessageBox.warning(self, "Sun settings", f"sptt: {exc}")
            return

        self._claim = claim_instance_name(
            self.le_instance.text().strip() or get_instance_name("SPTT", self._cfg))
        instance_name = self._claim.name
        self._service, self._server = _start_tab_server(
            self._cfg, "sptt", instance_name, output_dir)

        self.scheduled_worker = ConsoleRun(SpttWorkerConsole(
            cam=self.cam,
            output_dir=output_dir,
            instance_name=instance_name,
            status_dir=status_dir,
            capture_seconds=self._capture_seconds,
            service=self._service,
            node_name=get_node_name(self._cfg),
            debayer=self._sptt_cfg.get("debayer", False),
            sun_gate=sun_gate,
            preflight=preflight,
            overexposure=overexposure,
        ), self._service, self._log)
        self.scheduled_worker.status.connect(self._on_run_status)
        self.scheduled_worker.finished.connect(self._on_meas_finished)
        self.scheduled_worker.start()

        self.btn_meas_start.setEnabled(False)
        self.btn_meas_stop.setEnabled(True)
        self.btn_preview_start.setEnabled(False)
        # Apply stops and reconfigures the camera, which would pull it out
        # from under the running schedule. The exposure belongs to the worker while
        # it runs (split and preflight plan from it): focus_app changes it through
        # the worker. Gain stays live: the driver serialises it against frame reads.
        self.btn_apply.setEnabled(False)
        self.btn_reconnect.setEnabled(False)
        self.spin_exp.setEnabled(False)

    def _on_meas_stop(self):
        if self.scheduled_worker and self.scheduled_worker.is_running():
            self.scheduled_worker.stop()
        if self._server:
            self._server.stop()
            self._server = None
        _release_claim(self)
        self.btn_meas_stop.setEnabled(False)
        if self.scheduled_worker and self.scheduled_worker.is_running():
            # Still writing queued frames: _on_meas_finished frees the camera.
            self.lbl_meas_status.setText("Stopping…")
            return
        self.btn_meas_start.setEnabled(self.cam is not None)
        self.btn_preview_start.setEnabled(self.cam is not None)
        self.lbl_meas_status.setText("Idle")
        self.lbl_meas_countdown.setText("")

    def _on_meas_finished(self):
        self.btn_meas_stop.setEnabled(False)
        self.btn_meas_start.setEnabled(self.cam is not None)
        self.btn_preview_start.setEnabled(self.cam is not None)
        self.btn_apply.setEnabled(self.cam is not None)
        self.btn_reconnect.setEnabled(self.cam is not None)
        self.spin_exp.setEnabled(True)
        self.lbl_meas_status.setText("Idle")
        self.lbl_meas_countdown.setText("")

    def _on_run_status(self, snap):
        from cameras.sptt_driver import seconds_to_next_capture
        self.lbl_meas_shots.setText(f"Shots: {snap.get('shots_taken', 0)}")
        status = snap.get("status")
        if status:
            self.lbl_meas_status.setText(run_status_text(snap))
        if status == "running" and self._capture_seconds:
            remaining = seconds_to_next_capture(dt.now(), self._capture_seconds)
            self.lbl_meas_countdown.setText(f"Next: {remaining:.0f}s")
        elif status == "waiting" and snap.get("sun_altitude") is not None:
            self.lbl_meas_countdown.setText(
                f"Sun {snap['sun_altitude']:+.1f}° — waiting for the night")
        else:
            self.lbl_meas_countdown.setText("")

    def cleanup(self):
        self._on_meas_stop()
        self._on_preview_stop()
        if self.cam:
            self.cam.close()


class InfraCaptureThread(QThread):
    frame_ready = pyqtSignal(np.ndarray)
    fps_updated = pyqtSignal(float)
    error_occurred = pyqtSignal(str)
    roi_changed = pyqtSignal(int, int)

    def __init__(self, camera):
        super().__init__()
        self.camera = camera
        self._running = False
        self._pending_exposure = None
        self._pending_gain = None
        self._pending_roi = None
        self._lock = threading.Lock()

    def run(self):
        self._running = True
        frame_count = 0
        fps_timer = time.monotonic()
        consecutive_errors = 0

        while self._running:
            try:
                self._process_pending()
                frame = self.camera.grab_frame()
                self.frame_ready.emit(frame)
                consecutive_errors = 0

                frame_count += 1
                now = time.monotonic()
                elapsed = now - fps_timer
                if elapsed >= 1.0:
                    self.fps_updated.emit(frame_count / elapsed)
                    frame_count = 0
                    fps_timer = now

            except Exception as e:
                consecutive_errors += 1
                self.error_occurred.emit(str(e))
                if consecutive_errors >= 10:
                    self.error_occurred.emit("10 consecutive errors, capture stopped")
                    break

    def stop(self):
        self._running = False
        self.wait(5000)

    def request_exposure(self, microseconds):
        with self._lock:
            self._pending_exposure = microseconds

    def request_gain(self, gain):
        with self._lock:
            self._pending_gain = gain

    def request_roi(self, width, height):
        with self._lock:
            self._pending_roi = (width, height)

    def _process_pending(self):
        with self._lock:
            exposure = self._pending_exposure
            gain = self._pending_gain
            roi = self._pending_roi
            self._pending_exposure = None
            self._pending_gain = None
            self._pending_roi = None

        if roi is not None:
            self.camera.set_roi(*roi)
            self.roi_changed.emit(*roi)
        if exposure is not None:
            self.camera.set_exposure(exposure)
        if gain is not None:
            self.camera.set_gain(gain)


class InfraTab(QWidget):
    def __init__(self, cfg, log_fn):
        super().__init__()
        self._cfg = cfg
        self._log = log_fn
        self.cam = None
        self.capture_thread = None
        self.worker = None
        self.last_frame = None
        self.frame_count = 0
        self.fps_time = time.time()
        self.fps = 0.0
        self._server = None
        self._service = None
        self._build_ui()
        self._load_config()

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(200)
        self._countdown_timer.timeout.connect(self._update_idle_countdown)
        self._countdown_timer.start()

    def _build_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(2, 2, 2, 2)

        self.image_label = QLabel("Camera not connected")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(200, 150)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.setStyleSheet("background-color: #1a1a2e; color: #888; font-size: 14px;")
        main_layout.addWidget(self.image_label, stretch=3)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        scroll.setMinimumWidth(250)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)

        grp_conn = QGroupBox("Infra Camera (SW1300)")
        gl = QVBoxLayout(grp_conn)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self._on_connect)
        gl.addWidget(self.btn_connect)
        self.lbl_cam_status = QLabel("Not connected")
        self.lbl_cam_status.setStyleSheet("color:#cc0000; font-weight:bold;")
        gl.addWidget(self.lbl_cam_status)

        h = QHBoxLayout()
        self.btn_preview_start = QPushButton("Live Preview")
        self.btn_preview_stop = QPushButton("Stop Preview")
        self.btn_preview_start.setEnabled(False)
        self.btn_preview_stop.setEnabled(False)
        self.btn_preview_start.clicked.connect(self._on_preview_start)
        self.btn_preview_stop.clicked.connect(self._on_preview_stop)
        h.addWidget(self.btn_preview_start)
        h.addWidget(self.btn_preview_stop)
        gl.addLayout(h)

        self.lbl_fps = QLabel("FPS: --")
        self.lbl_fps.setStyleSheet("font-weight:bold;")
        gl.addWidget(self.lbl_fps)
        layout.addWidget(grp_conn)

        grp_exp = QGroupBox("Exposure / Gain")
        g = QGridLayout(grp_exp)

        g.addWidget(QLabel("Exposure (us):"), 0, 0)
        self.exp_slider = QSlider(Qt.Horizontal)
        self.exp_slider.setRange(15, 100000)
        self.exp_slider.setValue(1000)
        g.addWidget(self.exp_slider, 1, 0, 1, 2)
        self.exp_spinbox = QDoubleSpinBox()
        self.exp_spinbox.setRange(15, 60_000_000)
        self.exp_spinbox.setValue(1000)
        self.exp_spinbox.setSuffix(" us")
        self.exp_spinbox.setDecimals(1)
        g.addWidget(self.exp_spinbox, 0, 1)
        self.btn_set_exp = QPushButton("Set")
        self.btn_set_exp.clicked.connect(self._on_set_exposure)
        g.addWidget(self.btn_set_exp, 0, 2)

        self.exp_slider.valueChanged.connect(lambda v: self.exp_spinbox.setValue(v))
        self.exp_spinbox.valueChanged.connect(
            lambda v: self.exp_slider.setValue(int(v)) if v <= self.exp_slider.maximum() else None
        )

        g.addWidget(QLabel("Gain (0-120):"), 2, 0)
        self.gain_slider = QSlider(Qt.Horizontal)
        self.gain_slider.setRange(0, 120)
        self.gain_slider.setValue(0)
        g.addWidget(self.gain_slider, 3, 0, 1, 2)
        self.gain_spinbox = QSpinBox()
        self.gain_spinbox.setRange(0, 120)
        self.gain_spinbox.setValue(0)
        g.addWidget(self.gain_spinbox, 2, 1)
        self.btn_set_gain = QPushButton("Set")
        self.btn_set_gain.clicked.connect(self._on_set_gain)
        g.addWidget(self.btn_set_gain, 2, 2)

        self.gain_slider.valueChanged.connect(self.gain_spinbox.setValue)
        self.gain_spinbox.valueChanged.connect(self.gain_slider.setValue)

        layout.addWidget(grp_exp)

        grp_roi = QGroupBox("ROI")
        roi_lay = QHBoxLayout(grp_roi)
        self.roi_combo = QComboBox()
        self.roi_combo.addItems(["1280x1024", "1280x256"])
        roi_lay.addWidget(self.roi_combo)
        self.btn_set_roi = QPushButton("Set ROI")
        self.btn_set_roi.clicked.connect(self._on_set_roi)
        roi_lay.addWidget(self.btn_set_roi)
        layout.addWidget(grp_roi)

        grp_fmt = QGroupBox("Save Format")
        fmt_lay = QHBoxLayout(grp_fmt)
        self.combo_format = QComboBox()
        self.combo_format.addItems(["tiff", "png", "fits"])
        fmt_lay.addWidget(QLabel("Format:"))
        fmt_lay.addWidget(self.combo_format)
        layout.addWidget(grp_fmt)

        grp_meas = QGroupBox("Scheduled Measurement")
        meas_lay = QVBoxLayout(grp_meas)

        m_grid = QGridLayout()
        m_grid.addWidget(QLabel("Instance:"), 0, 0)
        self.le_instance = QLineEdit()
        m_grid.addWidget(self.le_instance, 0, 1)
        m_grid.addWidget(QLabel("Output dir:"), 1, 0)
        self.le_output = QLineEdit()
        btn_browse = QPushButton("...")
        btn_browse.setMaximumWidth(40)
        btn_browse.clicked.connect(lambda: self._browse_dir(self.le_output))
        m_grid.addWidget(self.le_output, 1, 1)
        m_grid.addWidget(btn_browse, 1, 2)
        m_grid.addWidget(QLabel("Capture seconds:"), 2, 0)
        self.le_cap_seconds = QLineEdit("0, 30")
        self.le_cap_seconds.setToolTip("Comma-separated seconds of each minute")
        m_grid.addWidget(self.le_cap_seconds, 2, 1, 1, 2)
        meas_lay.addLayout(m_grid)

        self.sched_table = QTableWidget(0, 2)
        self.sched_table.setHorizontalHeaderLabels(["Start (UTC)", "End (UTC)"])
        self.sched_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.sched_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sched_table.setMinimumHeight(80)
        self.sched_table.setMaximumHeight(140)
        meas_lay.addWidget(self.sched_table)

        sched_btns = QHBoxLayout()
        btn_add = QPushButton("+ Add")
        btn_add.clicked.connect(self._add_schedule_row)
        btn_del = QPushButton("- Remove")
        btn_del.clicked.connect(self._del_schedule_row)
        btn_load = QPushButton("Load...")
        btn_load.clicked.connect(self._load_schedule)
        btn_save_sched = QPushButton("Save...")
        btn_save_sched.clicked.connect(self._save_schedule)
        sched_btns.addWidget(btn_add)
        sched_btns.addWidget(btn_del)
        sched_btns.addStretch()
        sched_btns.addWidget(btn_load)
        sched_btns.addWidget(btn_save_sched)
        meas_lay.addLayout(sched_btns)

        meas_ctrl = QHBoxLayout()
        self.btn_meas_start = QPushButton("START")
        self.btn_meas_start.setEnabled(False)
        self.btn_meas_start.setStyleSheet(
            "background:#2a7a2a; color:white; font-weight:bold; padding:6px 18px;")
        self.btn_meas_stop = QPushButton("STOP")
        self.btn_meas_stop.setEnabled(False)
        self.btn_meas_stop.setStyleSheet(
            "background:#7a2a2a; color:white; font-weight:bold; padding:6px 18px;")
        self.btn_meas_start.clicked.connect(self._on_meas_start)
        self.btn_meas_stop.clicked.connect(self._on_meas_stop)
        meas_ctrl.addWidget(self.btn_meas_start)
        meas_ctrl.addWidget(self.btn_meas_stop)
        meas_lay.addLayout(meas_ctrl)

        self.lbl_meas_status = QLabel("Idle")
        self.lbl_meas_status.setStyleSheet("font-weight:bold; color:#555;")
        self.lbl_meas_shots = QLabel("Shots: 0")
        self.lbl_meas_errors = QLabel("")
        self.lbl_meas_countdown = QLabel("")
        info_lay = QHBoxLayout()
        info_lay.addWidget(QLabel("Status:"))
        info_lay.addWidget(self.lbl_meas_status)
        info_lay.addSpacing(8)
        info_lay.addWidget(self.lbl_meas_shots)
        info_lay.addWidget(self.lbl_meas_errors)
        info_lay.addStretch()
        info_lay.addWidget(self.lbl_meas_countdown)
        meas_lay.addLayout(info_lay)

        layout.addWidget(grp_meas)
        layout.addStretch()

        scroll.setWidget(panel)
        main_layout.addWidget(scroll, stretch=1)

    def _load_config(self):
        c = self._cfg.get("infra", {})
        self.le_instance.setText(c.get("instance_name")
                                 or get_instance_name("Infra", self._cfg))
        self.le_output.setText(c.get("output_dir", ""))
        secs = c.get("capture_seconds", [0, 30])
        self.le_cap_seconds.setText(", ".join(str(s) for s in secs))
        self._load_configured_schedule(c)
        self.exp_spinbox.setValue(c.get("exposure_us", 1000.0))
        self.gain_spinbox.setValue(c.get("gain", 0))
        roi = c.get("roi", "1280x1024")
        idx = self.roi_combo.findText(roi)
        if idx >= 0:
            self.roi_combo.setCurrentIndex(idx)
        fmt = c.get("save_format", "tiff")
        idx_fmt = self.combo_format.findText(fmt)
        if idx_fmt >= 0:
            self.combo_format.setCurrentIndex(idx_fmt)

    def _load_configured_schedule(self, section):
        if not str(section.get("schedule_file") or "").strip():
            return
        entries, errors, reason = load_schedule_safe(section["schedule_file"])
        if reason:
            self._log(f"Schedule: {reason}", "warn")
        _fill_schedule_table(self.sched_table, entries)

    def config_updates(self):
        updates = {"output_dir": self.le_output.text().strip(),
                   "save_format": self.combo_format.currentText()}
        secs = self._parse_capture_seconds()
        if secs:
            updates["capture_seconds"] = secs
        return {"infra": updates}

    def _browse_dir(self, le):
        d = QFileDialog.getExistingDirectory(self, "Select directory")
        if d:
            le.setText(d)

    def _parse_capture_seconds(self):
        text = self.le_cap_seconds.text()
        try:
            secs = [int(s.strip()) for s in text.split(",") if s.strip()]
            return sorted({s for s in secs if 0 <= s < 60})
        except ValueError:
            return []

    def _on_connect(self):
        self._log("Connecting to Infra camera...", "info")
        self.btn_connect.setEnabled(False)
        self.lbl_cam_status.setText("Connecting...")
        self.lbl_cam_status.setStyleSheet("color:#888; font-weight:bold;")
        QApplication.processEvents()

        try:
            from infra_driver import TanhoCamera
            self.cam = TanhoCamera()
            self.cam.connect()
            roi_text = self.roi_combo.currentText()
            w, h = [int(x) for x in roi_text.split("x")]
            self.cam.set_roi(w, h)
            self.cam.set_exposure(self.exp_spinbox.value())
            self.cam.set_gain(self.gain_spinbox.value())

            self.lbl_cam_status.setText(f"Connected: {self.cam.roi_width}x{self.cam.roi_height}")
            self.lbl_cam_status.setStyleSheet("color:#007700; font-weight:bold;")
            self.btn_preview_start.setEnabled(True)
            self.btn_meas_start.setEnabled(True)
            self._log(f"Infra camera connected: {self.cam.roi_width}x{self.cam.roi_height}", "info")
        except Exception as e:
            self.lbl_cam_status.setText("Connection failed")
            self.lbl_cam_status.setStyleSheet("color:#cc0000; font-weight:bold;")
            self.btn_connect.setEnabled(True)
            QMessageBox.critical(self, "Connection Error", str(e))
            self._log(f"Infra connection failed: {e}", "error")

    def _on_preview_start(self):
        if not self.cam:
            return
        self.capture_thread = InfraCaptureThread(self.cam)
        self.capture_thread.frame_ready.connect(self._on_frame)
        self.capture_thread.fps_updated.connect(self._on_fps)
        self.capture_thread.error_occurred.connect(self._on_preview_error)
        self.capture_thread.roi_changed.connect(self._on_roi_changed)
        self.capture_thread.start()
        self.btn_preview_start.setEnabled(False)
        self.btn_preview_stop.setEnabled(True)
        self._log("Live preview started", "info")

    def _on_preview_stop(self):
        if self.capture_thread:
            self.capture_thread.stop()
            self.capture_thread = None
        self.btn_preview_start.setEnabled(self.cam is not None)
        self.btn_preview_stop.setEnabled(False)

    def _on_frame(self, frame_16):
        self.last_frame = frame_16
        frame_8 = np.ascontiguousarray(
            frame_archive.to_uint8(frame_16, stretch="raw"))
        h, w = frame_8.shape
        qimg = QImage(frame_8.data, w, h, w, QImage.Format_Grayscale8)
        label_size = self.image_label.size()
        pixmap = QPixmap.fromImage(qimg).scaled(
            label_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(pixmap)

    def _on_fps(self, fps):
        self.lbl_fps.setText(f"FPS: {fps:.1f}")

    def _on_preview_error(self, msg):
        self._log(f"Preview error: {msg}", "error")

    def _on_roi_changed(self, w, h):
        self.lbl_cam_status.setText(f"Connected: {w}x{h}")

    def _measuring(self):
        """The measurement thread owns the camera; a set from here would reach the USB
        link in the middle of its grab."""
        if self.worker and self.worker.is_running():
            self._log("Stop the measurement first, or change it from focus_app.py.",
                      "warn")
            return True
        return False

    def _on_set_exposure(self):
        if self._measuring():
            return
        value = self.exp_spinbox.value()
        if self.capture_thread:
            self.capture_thread.request_exposure(value)
        elif self.cam:
            self.cam.set_exposure(value)
        self._log(f"Exposure set: {value} us", "info")

    def _on_set_gain(self):
        if self._measuring():
            return
        value = self.gain_spinbox.value()
        if self.capture_thread:
            self.capture_thread.request_gain(value)
        elif self.cam:
            self.cam.set_gain(value)
        self._log(f"Gain set: {value}", "info")

    def _on_set_roi(self):
        if self._measuring():
            return
        roi_text = self.roi_combo.currentText()
        w, h = [int(x) for x in roi_text.split("x")]
        if self.capture_thread:
            self.capture_thread.request_roi(w, h)
        elif self.cam:
            self.cam.set_roi(w, h)
        self._log(f"ROI set: {w}x{h}", "info")

    def _add_schedule_row(self):
        row = self.sched_table.rowCount()
        self.sched_table.insertRow(row)
        now = schedule_now().replace(second=0, microsecond=0)
        end = now.replace(hour=23, minute=59)
        self.sched_table.setItem(row, 0, QTableWidgetItem(now.strftime(SCHEDULE_DT_FMT)))
        self.sched_table.setItem(row, 1, QTableWidgetItem(end.strftime(SCHEDULE_DT_FMT)))

    def _del_schedule_row(self):
        rows = sorted({idx.row() for idx in self.sched_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.sched_table.removeRow(r)

    def _load_schedule(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load schedule", "", "Text files (*.txt);;All (*)")
        if not path:
            return
        entries, errors = load_schedule_file(path)
        if errors:
            QMessageBox.warning(self, "Parse errors", "\n".join(errors))
        self.sched_table.setRowCount(0)
        for e in entries:
            row = self.sched_table.rowCount()
            self.sched_table.insertRow(row)
            self.sched_table.setItem(row, 0, QTableWidgetItem(e.start.strftime(SCHEDULE_DT_FMT)))
            self.sched_table.setItem(row, 1, QTableWidgetItem(e.end.strftime(SCHEDULE_DT_FMT)))
        self._log(f"Loaded {len(entries)} schedule intervals", "info")

    def _save_schedule(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save schedule", "", "Text files (*.txt);;All (*)")
        if not path:
            return
        entries, errors = parse_schedule_text(self._table_to_text())
        if errors:
            QMessageBox.warning(self, "Errors", "\n".join(errors))
            return
        save_schedule_file(path, entries)
        self._log(f"Saved {len(entries)} intervals", "info")

    def _table_to_text(self):
        lines = []
        for row in range(self.sched_table.rowCount()):
            s = self.sched_table.item(row, 0)
            e = self.sched_table.item(row, 1)
            if s and e:
                lines.append(f"{s.text().strip()} - {e.text().strip()}")
        return "\n".join(lines)

    def _on_meas_start(self):
        if not self.cam:
            QMessageBox.warning(self, "No camera", "Connect Infra camera first.")
            return
        output_dir = self.le_output.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "No output", "Select output directory.")
            return

        entries, errors = parse_schedule_text(self._table_to_text())
        if errors:
            QMessageBox.warning(self, "Schedule errors", "\n".join(errors))
            return

        if self.capture_thread:
            self._on_preview_stop()

        status_dir = self._cfg.get("status_dir") or HOME_STATUS_DIR
        if not _make_dirs(self, output_dir, status_dir):
            return

        self._claim = claim_instance_name(
            self.le_instance.text().strip() or get_instance_name("Infra", self._cfg))
        instance_name = self._claim.name
        setup_mode = not entries
        if setup_mode:
            self._log("Schedule is empty — starting in setup mode: live frames "
                      "for focus_app.py, nothing archived.")
        capture_seconds = self._parse_capture_seconds()
        if not capture_seconds:
            QMessageBox.warning(self, "Capture seconds",
                                "Give at least one second of the minute, 0..59.")
            _release_claim(self)
            return
        save_format = self.combo_format.currentText()
        self._service, self._server = _start_tab_server(
            self._cfg, "infra", instance_name, output_dir, setup_mode)

        from cameras.infra_driver import InfraWorkerConsole, overexposure_settings
        from utils import get_node_name
        self.worker = ConsoleRun(InfraWorkerConsole(
            cam=self.cam,
            schedule=entries,
            output_dir=output_dir,
            instance_name=instance_name,
            status_dir=status_dir,
            capture_seconds=capture_seconds,
            save_format=save_format,
            service=self._service,
            node_name=get_node_name(self._cfg),
            setup_mode=setup_mode,
            overexposure=overexposure_settings(self._cfg.get("infra", {})),
        ), self._service, self._log)
        self.worker.status.connect(self._on_run_status)
        self.worker.finished.connect(self._on_meas_finished)
        self.worker.start()

        self.btn_meas_start.setEnabled(False)
        self.btn_meas_stop.setEnabled(True)
        self.btn_preview_start.setEnabled(False)

    def _on_meas_stop(self):
        if self.worker and self.worker.is_running():
            self.worker.stop()
        if self._server:
            self._server.stop()
            self._server = None
        _release_claim(self)
        self.btn_meas_stop.setEnabled(False)
        self.btn_meas_start.setEnabled(self.cam is not None)
        self.btn_preview_start.setEnabled(self.cam is not None)
        self._on_meas_status_msg("Idle")
        self.lbl_meas_countdown.setText("")

    def _on_meas_finished(self):
        self.btn_meas_stop.setEnabled(False)
        self.btn_meas_start.setEnabled(self.cam is not None)
        self.btn_preview_start.setEnabled(self.cam is not None)
        self._on_meas_status_msg("Idle")

    def _on_run_status(self, snap):
        self.lbl_meas_shots.setText(f"Shots: {snap.get('shots_taken', 0)}")
        errors = snap.get("errors") or 0
        self.lbl_meas_errors.setText(f"Errors: {errors}" if errors else "")
        if snap.get("status"):
            self._on_meas_status_msg(run_status_text(snap))

    def _on_meas_status_msg(self, msg):
        colors = {"Running": "#007700", "Waiting for schedule": "#aa6600",
                  "Idle": "#555", "Stopped": "#555"}
        self.lbl_meas_status.setText(msg)
        self.lbl_meas_status.setStyleSheet(f"font-weight:bold; color:{colors.get(msg, '#555')};")

    def _update_idle_countdown(self):
        now = dt.now()
        sec = now.second + now.microsecond / 1_000_000
        cap_secs = self._parse_capture_seconds()
        next_secs = [s for s in cap_secs if s > sec]
        if next_secs:
            remaining = next_secs[0] - sec
        else:
            remaining = (60 - sec) + (cap_secs[0] if cap_secs else 0)
        self.lbl_meas_countdown.setText(f"Next: {remaining:.1f}s")

    def cleanup(self):
        self._on_meas_stop()
        self._on_preview_stop()
        if self.cam:
            self.cam.disconnect()


class AsiTab(QWidget):
    """Runs the console worker, unchanged: a second implementation of the schedule, darks
    and cooling here would drift apart. Status is mirrored through the CameraService;
    log lines arrive through a ``console_ui`` sink.
    """

    CONFIG_SECTION = "asi"
    log_line = pyqtSignal(str, str)

    def __init__(self, cfg: dict, log_fn):
        super().__init__()
        self._cfg = cfg
        self._log = log_fn
        self._worker = None
        self._server = None
        self._service = None
        self.cam = None
        self.log_line.connect(self._append_log)
        self._build_ui()

    def _build_ui(self):
        asi_cfg = self._cfg.get("asi", {})
        camera_cfg = asi_cfg.get("camera", {})
        layout = QVBoxLayout(self)

        cfg_box = QGroupBox("ASI all-sky imager (Princeton PIXIS via PICAM)")
        cfg_grid = QGridLayout(cfg_box)
        cfg_grid.setColumnStretch(1, 1)
        row = 0

        cfg_grid.addWidget(QLabel("Output dir:"), row, 0)
        self.le_output = QLineEdit(asi_cfg.get("output_dir", ""))
        cfg_grid.addWidget(self.le_output, row, 1)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_output)
        cfg_grid.addWidget(btn_browse, row, 2)
        row += 1

        cfg_grid.addWidget(QLabel("Instance name:"), row, 0)
        self.le_instance = QLineEdit(asi_cfg.get("instance_name", ""))
        self.le_instance.setPlaceholderText("auto")
        cfg_grid.addWidget(self.le_instance, row, 1)
        row += 1

        cfg_grid.addWidget(QLabel("Backend:"), row, 0)
        self.cmb_backend = QComboBox()
        self.cmb_backend.addItems(["picam", "sim"])
        self.cmb_backend.setCurrentText(camera_cfg.get("backend", "picam"))
        cfg_grid.addWidget(self.cmb_backend, row, 1)
        row += 1

        cfg_grid.addWidget(QLabel("Schedule:"), row, 0)
        self.lbl_schedule = QLabel("—")
        self.lbl_schedule.setWordWrap(True)
        cfg_grid.addWidget(self.lbl_schedule, row, 1, 1, 2)
        row += 1

        hint = QLabel("Exposures, filters, cooling and the schedule are edited in "
                      "setup_app.py (tab “ASI”).")
        hint.setWordWrap(True)
        cfg_grid.addWidget(hint, row, 0, 1, 3)

        layout.addWidget(cfg_box)

        ctrl_lay = QHBoxLayout()
        self.btn_start = QPushButton("Start Measurements")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setToolTip("Finishes the current frame and shoots the "
                                 "closing dark frames before stopping")
        self.btn_stop.clicked.connect(self._on_stop)
        ctrl_lay.addWidget(self.btn_start)
        ctrl_lay.addWidget(self.btn_stop)
        ctrl_lay.addStretch()
        layout.addLayout(ctrl_lay)

        status_box = QGroupBox("Status")
        status_lay = QGridLayout(status_box)
        status_lay.addWidget(QLabel("Status:"), 0, 0)
        self.lbl_status = QLabel("stopped")
        status_lay.addWidget(self.lbl_status, 0, 1)
        status_lay.addWidget(QLabel("Phase:"), 0, 2)
        self.lbl_phase = QLabel("—")
        status_lay.addWidget(self.lbl_phase, 0, 3)
        status_lay.addWidget(QLabel("Frames (light/dark):"), 1, 0)
        self.lbl_frames = QLabel("0 / 0")
        status_lay.addWidget(self.lbl_frames, 1, 1)
        status_lay.addWidget(QLabel("Sensor temp:"), 1, 2)
        self.lbl_temp = QLabel("—")
        status_lay.addWidget(self.lbl_temp, 1, 3)
        status_lay.addWidget(QLabel("Exposure / binning:"), 2, 0)
        self.lbl_exp = QLabel("—")
        status_lay.addWidget(self.lbl_exp, 2, 1)
        status_lay.addWidget(QLabel("Filter:"), 2, 2)
        self.lbl_filter = QLabel("—")
        status_lay.addWidget(self.lbl_filter, 2, 3)
        status_lay.addWidget(QLabel("Last file:"), 3, 0)
        self.lbl_last = QLabel("—")
        status_lay.addWidget(self.lbl_last, 3, 1, 1, 3)
        layout.addWidget(status_box)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Monospace", 9))
        layout.addWidget(self.log_text, 1)

        self._poll_timer = QTimer()
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._refresh_status)
        self._describe_schedule()

    def _describe_schedule(self):
        from cameras.asi import config as asi_config
        try:
            conf = asi_config.from_dict(self._cfg.get("asi", {}))
        except Exception as exc:
            self.lbl_schedule.setText(f"unreadable: {exc}")
            return
        sched = conf.schedule
        if sched.mode == "time":
            text = (f"time · {len(sched.entries)} slot(s) · period "
                    f"{sched.period:.0f} s from "
                    f"{sched.t_start.strftime('%H:%M') if sched.t_start else '?'}")
        elif sched.mode == "sun_cycle":
            text = (f"sun_cycle ≤ {sched.sun_max_angle:g}° · "
                    f"{len(sched.entries)} slot(s) · period {sched.period:.0f} s"
                    f" · phase "
                    f"{sched.t_start.strftime('%H:%M') + ' UTC' if sched.t_start else 'whole minute'}")
        else:
            text = (f"sun ≤ {sched.sun_max_angle:g}° · "
                    f"{len(sched.entries)} slot(s)")
        if conf.errors:
            text += f"   ⚠ {len(conf.errors)} problem(s)"
        self.lbl_schedule.setText(text)

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "Select output directory",
                                             self.le_output.text())
        if d:
            self.le_output.setText(d)

    def _append_log(self, msg, level):
        colors = {"info": "#000", "warn": "#aa6600", "error": "#cc0000"}
        ts = dt.now().strftime("%H:%M:%S")
        self.log_text.append(
            f'<span style="color:{colors.get(level.lower(), "#000")}">'
            f'[{ts}] {msg}</span>')

    def _log_local(self, msg, level="info"):
        self._append_log(msg, level)
        self._log(msg, level)

    def _on_console_log(self, msg, level):
        # Called from the worker thread — hop to the GUI thread via the signal.
        self.log_line.emit(str(msg), str(level).lower())

    def _on_start(self):
        import console_ui
        from cameras.asi import config as asi_config
        from cameras.asi import schedule as asi_schedule
        from cameras.asi_driver import (
            AsiCamera, AsiWorkerConsole, publish_setpoint_limits,
        )
        from camera_service import CameraService
        from frame_server import start_frame_server
        from utils import get_instance_name, claim_instance_name, get_node_name
        from pathlib import Path

        _reload_section(self._cfg, "asi")
        asi_cfg = dict(self._cfg.get("asi", {}))
        asi_cfg["output_dir"] = self.le_output.text().strip()
        camera_cfg = dict(asi_cfg.get("camera", {}))
        camera_cfg["backend"] = self.cmb_backend.currentText()
        asi_cfg["camera"] = camera_cfg
        self._cfg["asi"] = asi_cfg

        conf = asi_config.from_dict(asi_cfg)
        # Nobody can press Enter in a terminal the GUI may not even have.
        conf.wait_for_enter = False
        for problem in conf.errors:
            self._log_local(problem, "warn")
        if not conf.output_dir:
            self._log_local("Output directory is required.", "error")
            return
        setup_mode = not conf.schedule.entries
        if setup_mode:
            from worker_common import empty_schedule_reason
            self._log_local(
                f"Starting in setup mode — {empty_schedule_reason('asi', asi_cfg.get('schedule') or asi_cfg.get('schedule_file'), conf.errors)}. "
                f"Live frames go to focus_app.py, nothing is archived.", "warn")

        self._claim = claim_instance_name(
            self.le_instance.text().strip() or get_instance_name("ASI", self._cfg))
        instance = self._claim.name
        node_name = get_node_name(self._cfg)
        status_dir = self._cfg.get("status_dir") or str(
            Path.home() / ".every_camera" / "status")
        if not _make_dirs(self, conf.output_dir):
            _release_claim(self)
            return

        console_ui.add_sink(self._on_console_log, owner=lambda: self._worker)
        try:
            self.cam = AsiCamera(conf).open()
        except Exception as exc:
            console_ui.remove_sink(self._on_console_log)
            self._log_local(f"Could not open the camera: {exc}", "error")
            self.cam = None
            _release_claim(self)
            return


        self._service = CameraService("asi", instance, conf.output_dir,
                                      node_name=node_name,
                                      setup_mode=setup_mode)
        self._service.set_schedule(asi_schedule.schedule_snapshot(conf.schedule))
        publish_setpoint_limits(self._service, self.cam)
        self._server = start_frame_server(self._cfg.get("server", {}),
                                          self._service)

        self._worker = AsiWorkerConsole(
            cam=self.cam,
            cfg=conf,
            output_dir=conf.output_dir,
            instance_name=instance,
            status_dir=status_dir,
            service=self._service,
            node_name=node_name,
            setup_mode=setup_mode,
        )
        self._worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._poll_timer.start()
        self._describe_schedule()
        self._log_local(f"ASI measurements started — instance: {instance}")

    def _on_stop(self):
        if self._worker:
            self._worker.request_stop()
            self._log_local("Stopping after the current frame and closing darks…")
        self.btn_stop.setEnabled(False)

    def _refresh_status(self):
        if self._service is None:
            return
        snap = self._service.status()
        self.lbl_status.setText(str(snap.get("status", "—")))
        self.lbl_phase.setText(str(snap.get("phase", "—")))
        self.lbl_frames.setText(f"{snap.get('shots_taken', 0)} / "
                                f"{snap.get('darks_taken', 0)}")
        temp = snap.get("ccd_temp")
        if temp is None:
            self.lbl_temp.setText("—")
        else:
            state = "locked" if snap.get("temp_locked") else "settling"
            self.lbl_temp.setText(f"{temp} °C ({state})")
        binning = snap.get("binning") or 1
        exposure = snap.get("exposure")
        self.lbl_exp.setText(f"{exposure} s · {binning}x{binning}"
                             if exposure else f"— · {binning}x{binning}")
        self.lbl_filter.setText(str(snap.get("filter") or "—"))
        self.lbl_last.setText(str(snap.get("last_file") or "—"))

        if self._worker is not None and not self._worker.is_alive():
            self._on_finished()

    def _on_finished(self):
        import console_ui
        self._poll_timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        console_ui.remove_sink(self._on_console_log)
        if self._server:
            self._server.stop()
            self._server = None
        _release_claim(self)
        if self.cam:
            self.cam.close()
            self.cam = None
        self._worker = None
        self._log_local("ASI measurements stopped")

    def cleanup(self):
        self._poll_timer.stop()
        if self._worker is not None and self._worker.is_alive():
            # Forced: closing the window should not block on a run of darks.
            self._worker.request_stop(force=True)
            # An exposure under way still has to finish: the SDK must not be closed
            # beneath it.
            self._worker.join(timeout=WORKER_CLOSE_WAIT)
            if self._worker.is_alive():
                self._log_local("The worker is still exposing — the camera is left "
                                "to the process exit.", "warn")
                return
        if self._worker is not None:
            self._on_finished()

    def config_updates(self):
        return {self.CONFIG_SECTION: {
            "output_dir": self.le_output.text().strip(),
            "camera": {"backend": self.cmb_backend.currentText()}}}


class JapanTab(QWidget):
    """Same arrangement as :class:`AsiTab`: runs the console worker, unchanged."""

    CONFIG_SECTION = "japan"

    log_line = pyqtSignal(str, str)

    def __init__(self, cfg: dict, log_fn):
        super().__init__()
        self._cfg = cfg
        self._log = log_fn
        self._worker = None
        self._server = None
        self._service = None
        self.cam = None
        self.log_line.connect(self._append_log)
        self._build_ui()

    def _build_ui(self):
        japan_cfg = self._cfg.get("japan", {})
        camera_cfg = japan_cfg.get("camera", {})
        layout = QVBoxLayout(self)

        cfg_box = QGroupBox("Japan all-sky imager (Hamamatsu via DCAM-API)")
        cfg_grid = QGridLayout(cfg_box)
        cfg_grid.setColumnStretch(1, 1)
        row = 0

        cfg_grid.addWidget(QLabel("Output dir:"), row, 0)
        self.le_output = QLineEdit(japan_cfg.get("output_dir", ""))
        self.le_output.setToolTip("Frames are written flat into this directory — "
                                  "one directory per night is usual")
        cfg_grid.addWidget(self.le_output, row, 1)
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse_output)
        cfg_grid.addWidget(btn_browse, row, 2)
        row += 1

        cfg_grid.addWidget(QLabel("Instance name:"), row, 0)
        self.le_instance = QLineEdit(japan_cfg.get("instance_name", ""))
        self.le_instance.setPlaceholderText("auto")
        cfg_grid.addWidget(self.le_instance, row, 1)
        row += 1

        cfg_grid.addWidget(QLabel("Backend:"), row, 0)
        self.cmb_backend = QComboBox()
        self.cmb_backend.addItems(["dcam", "sim"])
        self.cmb_backend.setCurrentText(camera_cfg.get("backend", "dcam"))
        cfg_grid.addWidget(self.cmb_backend, row, 1)
        row += 1

        cfg_grid.addWidget(QLabel("Schedule:"), row, 0)
        self.lbl_schedule = QLabel("—")
        self.lbl_schedule.setWordWrap(True)
        cfg_grid.addWidget(self.lbl_schedule, row, 1, 1, 2)
        row += 1

        hint = QLabel("Exposures, filters and the schedule are edited in "
                      "setup_app.py (tab “Japan”).")
        hint.setWordWrap(True)
        cfg_grid.addWidget(hint, row, 0, 1, 3)

        layout.addWidget(cfg_box)

        ctrl_lay = QHBoxLayout()
        self.btn_start = QPushButton("Start Measurements")
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setToolTip("Finishes the current frame and shoots the "
                                 "closing dark frames before stopping")
        self.btn_stop.clicked.connect(self._on_stop)
        ctrl_lay.addWidget(self.btn_start)
        ctrl_lay.addWidget(self.btn_stop)
        ctrl_lay.addStretch()
        layout.addLayout(ctrl_lay)

        status_box = QGroupBox("Status")
        status_lay = QGridLayout(status_box)
        status_lay.addWidget(QLabel("Status:"), 0, 0)
        self.lbl_status = QLabel("stopped")
        status_lay.addWidget(self.lbl_status, 0, 1)
        status_lay.addWidget(QLabel("Phase:"), 0, 2)
        self.lbl_phase = QLabel("—")
        status_lay.addWidget(self.lbl_phase, 0, 3)
        status_lay.addWidget(QLabel("Frames (light/dark):"), 1, 0)
        self.lbl_frames = QLabel("0 / 0")
        status_lay.addWidget(self.lbl_frames, 1, 1)
        status_lay.addWidget(QLabel("Sensor temp:"), 1, 2)
        self.lbl_temp = QLabel("—")
        status_lay.addWidget(self.lbl_temp, 1, 3)
        status_lay.addWidget(QLabel("Exposure / binning:"), 2, 0)
        self.lbl_exp = QLabel("—")
        status_lay.addWidget(self.lbl_exp, 2, 1)
        status_lay.addWidget(QLabel("Filter:"), 2, 2)
        self.lbl_filter = QLabel("—")
        status_lay.addWidget(self.lbl_filter, 2, 3)
        status_lay.addWidget(QLabel("Last file:"), 3, 0)
        self.lbl_last = QLabel("—")
        status_lay.addWidget(self.lbl_last, 3, 1, 1, 3)
        layout.addWidget(status_box)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Monospace", 9))
        layout.addWidget(self.log_text, 1)

        self._poll_timer = QTimer()
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._refresh_status)
        self._describe_schedule()

    def _describe_schedule(self):
        from cameras.japan import config as japan_config
        try:
            conf = japan_config.from_dict(self._cfg.get("japan", {}))
        except Exception as exc:
            self.lbl_schedule.setText(f"unreadable: {exc}")
            return
        sched = conf.schedule
        if sched.mode == "time":
            text = (f"time · {len(sched.entries)} slot(s) · period "
                    f"{sched.period:.0f} s from "
                    f"{sched.t_start.strftime('%H:%M') if sched.t_start else '?'}")
        elif sched.mode == "sun_cycle":
            text = (f"sun_cycle ≤ {sched.sun_max_angle:g}° · "
                    f"{len(sched.entries)} slot(s) · period {sched.period:.0f} s"
                    f" · phase "
                    f"{sched.t_start.strftime('%H:%M') + ' UTC' if sched.t_start else 'whole minute'}")
        else:
            text = (f"sun ≤ {sched.sun_max_angle:g}° · "
                    f"{len(sched.entries)} slot(s)")
        if conf.errors:
            text += f"   ⚠ {len(conf.errors)} problem(s)"
        self.lbl_schedule.setText(text)

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "Select output directory",
                                             self.le_output.text())
        if d:
            self.le_output.setText(d)

    def _append_log(self, msg, level):
        colors = {"info": "#000", "warn": "#aa6600", "error": "#cc0000"}
        ts = dt.now().strftime("%H:%M:%S")
        self.log_text.append(
            f'<span style="color:{colors.get(level.lower(), "#000")}">'
            f'[{ts}] {msg}</span>')

    def _log_local(self, msg, level="info"):
        self._append_log(msg, level)
        self._log(msg, level)

    def _on_console_log(self, msg, level):
        # Called from the worker thread — hop to the GUI thread via the signal.
        self.log_line.emit(str(msg), str(level).lower())

    def _on_start(self):
        import console_ui
        from cameras.japan import config as japan_config
        from cameras.japan_driver import JapanCamera, JapanWorkerConsole
        from camera_service import CameraService
        from frame_server import start_frame_server
        from utils import get_instance_name, claim_instance_name, get_node_name
        from cameras.common import schedule as common_schedule
        from pathlib import Path

        _reload_section(self._cfg, "japan")
        japan_cfg = dict(self._cfg.get("japan", {}))
        japan_cfg["output_dir"] = self.le_output.text().strip()
        camera_cfg = dict(japan_cfg.get("camera", {}))
        camera_cfg["backend"] = self.cmb_backend.currentText()
        japan_cfg["camera"] = camera_cfg
        self._cfg["japan"] = japan_cfg

        conf = japan_config.from_dict(japan_cfg)
        # Nobody can press Enter in a terminal the GUI may not even have.
        conf.wait_for_enter = False
        for problem in conf.errors:
            self._log_local(problem, "warn")
        if not conf.output_dir:
            self._log_local("Output directory is required.", "error")
            return
        setup_mode = not conf.schedule.entries
        if setup_mode:
            self._log_local("The schedule is empty — starting in setup mode: "
                            "live frames for focus_app.py, nothing archived. "
                            "Add slots in setup_app.py to measure.", "warn")

        self._claim = claim_instance_name(
            self.le_instance.text().strip()
            or get_instance_name("JAPAN", self._cfg))
        instance = self._claim.name
        node_name = get_node_name(self._cfg)
        status_dir = self._cfg.get("status_dir") or str(
            Path.home() / ".every_camera" / "status")
        if not _make_dirs(self, conf.output_dir):
            _release_claim(self)
            return

        console_ui.add_sink(self._on_console_log, owner=lambda: self._worker)
        try:
            self.cam = JapanCamera(conf).open()
        except Exception as exc:
            console_ui.remove_sink(self._on_console_log)
            self._log_local(f"Could not open the camera: {exc}", "error")
            self.cam = None
            _release_claim(self)
            return


        # No setpoint limits to publish: every field in this camera's focus_app schema has
        # a range known without asking the hardware.
        self._service = CameraService("japan", instance, conf.output_dir,
                                      node_name=node_name,
                                      setup_mode=setup_mode)
        self._service.set_schedule(common_schedule.schedule_snapshot(conf.schedule))
        self._server = start_frame_server(self._cfg.get("server", {}),
                                          self._service)

        self._worker = JapanWorkerConsole(
            cam=self.cam,
            cfg=conf,
            output_dir=conf.output_dir,
            instance_name=instance,
            status_dir=status_dir,
            service=self._service,
            node_name=node_name,
            setup_mode=setup_mode,
        )
        self._worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._poll_timer.start()
        self._describe_schedule()
        self._log_local(f"Japan measurements started — instance: {instance}")

    def _on_stop(self):
        if self._worker:
            self._worker.request_stop()
            self._log_local("Stopping after the current frame and closing darks…")
        self.btn_stop.setEnabled(False)

    def _refresh_status(self):
        if self._service is None:
            return
        snap = self._service.status()
        self.lbl_status.setText(str(snap.get("status", "—")))
        self.lbl_phase.setText(str(snap.get("phase", "—")))
        self.lbl_frames.setText(f"{snap.get('shots_taken', 0)} / "
                                f"{snap.get('darks_taken', 0)}")
        temp = snap.get("ccd_temp")
        # A bare reading: there is no setpoint to compare it against.
        self.lbl_temp.setText("—" if temp is None else f"{temp} °C")
        binning = snap.get("binning") or 1
        exposure = snap.get("exposure")
        self.lbl_exp.setText(f"{exposure} s · {binning}x{binning}"
                             if exposure else f"— · {binning}x{binning}")
        self.lbl_filter.setText(str(snap.get("filter") or "—"))
        self.lbl_last.setText(str(snap.get("last_file") or "—"))

        if self._worker is not None and not self._worker.is_alive():
            self._on_finished()

    def _on_finished(self):
        import console_ui
        self._poll_timer.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        console_ui.remove_sink(self._on_console_log)
        if self._server:
            self._server.stop()
            self._server = None
        _release_claim(self)
        if self.cam:
            self.cam.close()
            self.cam = None
        self._worker = None
        self._log_local("Japan measurements stopped")

    def cleanup(self):
        self._poll_timer.stop()
        if self._worker is not None and self._worker.is_alive():
            # Forced: closing the window should not block on a run of darks.
            self._worker.request_stop(force=True)
            # An exposure under way still has to finish: the SDK must not be closed
            # beneath it.
            self._worker.join(timeout=WORKER_CLOSE_WAIT)
            if self._worker.is_alive():
                self._log_local("The worker is still exposing — the camera is left "
                                "to the process exit.", "warn")
                return
        if self._worker is not None:
            self._on_finished()

    def config_updates(self):
        return {self.CONFIG_SECTION: {
            "output_dir": self.le_output.text().strip(),
            "camera": {"backend": self.cmb_backend.currentText()}}}


class MainWindow(QMainWindow):
    def __init__(self, cfg, camera_type=None, config_path=None):
        super().__init__()
        self.setWindowTitle("Every Camera")
        self.setMinimumSize(700, 450)
        self.resize(1100, 750)
        self._cfg = cfg
        # closeEvent rewrites this file, so it must honour --config.
        self._config_path = config_path
        self._tabs = {}
        self._build_ui(camera_type)

    def _build_ui(self, camera_type):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(4)
        root.setContentsMargins(6, 6, 6, 6)


        self.tab_widget = QTabWidget()

        if camera_type is None or camera_type == "cannon":
            self._cannon_tab = CannonTab(self._cfg, self._log)
            self.tab_widget.addTab(self._cannon_tab, "Canon Camera")
            self._tabs["cannon"] = self._cannon_tab

        if camera_type is None or camera_type == "sptt":
            self._sptt_tab = SpttTab(self._cfg, self._log)
            self.tab_widget.addTab(self._sptt_tab, "SPTT Camera")
            self._tabs["sptt"] = self._sptt_tab

        if camera_type is None or camera_type == "infra":
            self._infra_tab = InfraTab(self._cfg, self._log)
            self.tab_widget.addTab(self._infra_tab, "Infra Camera")
            self._tabs["infra"] = self._infra_tab

        if camera_type is None or camera_type == "asi":
            self._asi_tab = AsiTab(self._cfg, self._log)
            self.tab_widget.addTab(self._asi_tab, "ASI Camera")
            self._tabs["asi"] = self._asi_tab

        if camera_type is None or camera_type == "japan":
            self._japan_tab = JapanTab(self._cfg, self._log)
            self.tab_widget.addTab(self._japan_tab, "Japan Camera")
            self._tabs["japan"] = self._japan_tab

        self._monitor = MonitorWidget()
        self.tab_widget.addTab(self._monitor, "Monitor")

        root.addWidget(self.tab_widget, 1)

        log_box = QGroupBox("Log")
        log_lay = QVBoxLayout(log_box)
        log_lay.setContentsMargins(4, 4, 4, 4)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(60)
        self.log_text.setMaximumHeight(150)
        self.log_text.setFont(QFont("Monospace", 9))
        log_lay.addWidget(self.log_text)
        root.addWidget(log_box)

    def _log(self, message, level="info"):
        colors = {"info": "#000", "warn": "#aa6600", "error": "#cc0000"}
        color = colors.get(level, "#000")
        ts = dt.now().strftime("%H:%M:%S")
        self.log_text.append(f'<span style="color:{color}">[{ts}] {message}</span>')
        self.log_text.moveCursor(QTextCursor.End)

    def closeEvent(self, event):
        self._save_tab_settings()
        for tab in self._tabs.values():
            tab.cleanup()
        event.accept()

    def _save_tab_settings(self):
        """Only the fields the tabs own go back, merged into the file as it is now: it
        may have been edited in setup_app meanwhile, and a config that failed to load
        must not be replaced by defaults."""
        if getattr(self._cfg, "load_error", None):
            return
        fresh = load_config(self._config_path)
        if fresh.load_error:
            return
        before = json.dumps(fresh, sort_keys=True)
        for tab in self._tabs.values():
            updates = getattr(tab, "config_updates", None)
            if updates is None:
                continue
            try:
                _set_nested(fresh, updates())
            except Exception as exc:
                self._log(f"Could not collect settings: {exc}", "warn")
        if json.dumps(fresh, sort_keys=True) != before:
            save_config(fresh, self._config_path)


def run_gui(args):
    # Qt platform plugin conflict with OpenCV.
    try:
        import PyQt5 as _pyqt5
        _qt_plugins = os.path.join(os.path.dirname(_pyqt5.__file__), "Qt5", "plugins")
        if os.path.isdir(_qt_plugins):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = _qt_plugins
    except Exception:
        os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)

    config_path = getattr(args, 'config', None)
    cfg = load_config(config_path)
    camera_type = getattr(args, 'type', None)

    app = QApplication(sys.argv)
    import qt_support
    qt_support.install_excepthook()
    app.setStyle("Fusion")

    win = MainWindow(cfg, camera_type, config_path)
    win.show()

    sys.exit(app.exec_())
