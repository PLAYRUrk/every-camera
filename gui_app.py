"""
Every Camera — GUI application.
Combines Canon camera control, SPTT camera control, and Monitor in tabs.
"""
import os
import sys
import time
import json
import argparse

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

from utils import (
    load_config, save_config, can_use_gui, get_instance_name,
    parse_schedule_text, load_schedule_file, save_schedule_file,
    write_status_file, get_system_info,
    SCHEDULE_DT_FMT, HOME_STATUS_DIR, APP_DIR,
)
from mqtt_client import MQTT_AVAILABLE, MqttPublisher
from monitor import MonitorWidget


# ===========================================================================
# Canon GUI components
# ===========================================================================

class CannonConnectThread(QThread):
    connected = pyqtSignal(object, object, str)  # cam, config, model_name
    failed = pyqtSignal(str)

    def run(self):
        try:
            from cannon_driver import release_camera_usb, detect_model, apply_camcfg
            import gphoto2cffi as gp
            release_camera_usb()
            cam = gp.Camera()
            config = cam._get_config()
            model_name = detect_model(config)
            apply_camcfg(config, model_name)
            self.connected.emit(cam, config, model_name)
        except Exception as e:
            self.failed.emit(str(e))


class CannonWorkerQt(QThread):
    log_msg = pyqtSignal(str, str)
    shot_taken = pyqtSignal(int)
    status_msg = pyqtSignal(str)
    countdown = pyqtSignal(str)
    error_count = pyqtSignal(int)
    finished = pyqtSignal()

    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, cam, config, schedule, output_dir, instance_name,
                 status_dir, capture_seconds, mqtt_publisher=None, mqtt_prefix="every_camera"):
        super().__init__()
        self.cam = cam
        self.config = config
        self.schedule = schedule
        self.output_dir = output_dir
        self.instance_name = instance_name
        self.status_dir = status_dir
        self.capture_seconds = sorted(capture_seconds)
        self._mqtt = mqtt_publisher
        self._mqtt_topic = f"{mqtt_prefix}/{instance_name}/status"
        self._stop = False
        self._shots = 0
        self._errors = 0
        self._last_shot = None
        self._active_until = None
        self._status_path = os.path.join(status_dir, f"{os.getpid()}.json")

    def request_stop(self):
        self._stop = True

    def run(self):
        from cannon_driver import capture_image, get_camera_settings_info

        last_fired = (-1, -1)
        consecutive_errors = 0
        os.makedirs(self.status_dir, exist_ok=True)

        self.log_msg.emit("Measurement started", "info")
        self.status_msg.emit("Running")
        self._save_status("running")

        while not self._stop:
            now = dt.now()

            active_end = None
            for entry in self.schedule:
                if entry.start <= now <= entry.end:
                    active_end = entry.end
                    break

            if active_end is None:
                self.status_msg.emit("Waiting for schedule")
                self._save_status("waiting")
                self.countdown.emit("--")
                self.msleep(500)
                continue

            self._active_until = active_end

            # Countdown
            sec = now.second + now.microsecond / 1_000_000
            next_secs = [s for s in self.capture_seconds if s > sec]
            if next_secs:
                remaining = next_secs[0] - sec
            else:
                remaining = (60 - sec) + self.capture_seconds[0]
            self.countdown.emit(f"{remaining:.1f}s")

            fire_key = (now.minute, now.second)
            if now.second in self.capture_seconds and fire_key != last_fired:
                last_fired = fire_key
                timestamp = now.strftime("%Y%m%dT%H%M%S")
                filepath = os.path.join(self.output_dir, f"{timestamp}.jpeg")
                try:
                    img_data = capture_image(self.cam)
                    with open(filepath, "wb") as f:
                        f.write(img_data)
                    self.log_msg.emit(f"Shot saved: {os.path.basename(filepath)}", "info")
                    consecutive_errors = 0
                    self._shots += 1
                    self._last_shot = now
                    self.shot_taken.emit(self._shots)
                    self.status_msg.emit("Running")
                    self._save_status("running")
                except Exception as e:
                    self.log_msg.emit(f"Capture error: {e}", "error")
                    consecutive_errors += 1
                    self._errors += 1
                    self.error_count.emit(self._errors)
                    self._save_status("error")
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        self.log_msg.emit(
                            f"Stopped: {consecutive_errors} consecutive errors", "error")
                        break
            elif now.second not in self.capture_seconds:
                last_fired = (-1, -1)

            self.msleep(100)

        self._save_status("stopped")
        self._delete_status()
        self.log_msg.emit("Measurement stopped", "info")
        self.finished.emit()

    def _save_status(self, status):
        from cannon_driver import get_camera_settings_info
        cam_info = {}
        try:
            cam_info = get_camera_settings_info(self.config)
        except Exception:
            pass
        payload = {
            "instance_name": self.instance_name,
            "camera_type": "cannon",
            "pid": os.getpid(),
            "status": status,
            "shots_taken": self._shots,
            "last_shot": self._last_shot.isoformat() if self._last_shot else None,
            "active_until": self._active_until.isoformat() if self._active_until else None,
            "errors": self._errors,
            "capture_seconds": self.capture_seconds,
            "last_update": dt.now().isoformat(),
        }
        payload.update(cam_info)
        try:
            payload["system"] = get_system_info(self.output_dir)
        except Exception:
            pass
        try:
            write_status_file(self._status_path, payload)
        except Exception:
            pass
        if self._mqtt:
            try:
                self._mqtt.publish(self._mqtt_topic, json.dumps(payload), retain=True)
            except Exception:
                pass

    def _delete_status(self):
        try:
            os.remove(self._status_path)
        except FileNotFoundError:
            pass


class CannonTab(QWidget):
    """Canon camera measurement control tab."""

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
        self._mqtt_pub = None
        self._build_ui()
        self._load_config()

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(200)
        self._countdown_timer.timeout.connect(self._update_idle_countdown)
        self._countdown_timer.start()

    def _build_ui(self):
        lay = QVBoxLayout(self)

        # Camera connection
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

        # Session settings
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

        # Schedule
        sched_box = QGroupBox("Measurement Schedule")
        sched_lay = QVBoxLayout(sched_box)
        self.sched_table = QTableWidget(0, 2)
        self.sched_table.setHorizontalHeaderLabels(["Start (YYYY-MM-DD HH:MM:SS)",
                                                     "End (YYYY-MM-DD HH:MM:SS)"])
        self.sched_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.sched_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sched_table.setMinimumHeight(100)
        sched_lay.addWidget(self.sched_table)

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
        lay.addWidget(sched_box)

        # Control
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

    def _load_config(self):
        c = self._cfg.get("cannon", {})
        self.le_instance.setText(c.get("instance_name") or get_instance_name("Cannon"))
        self.le_output.setText(c.get("output_dir", ""))
        secs = c.get("capture_seconds", [0, 30])
        self.le_cap_seconds.setText(", ".join(str(s) for s in secs))

    def _browse_dir(self, line_edit):
        d = QFileDialog.getExistingDirectory(self, "Select directory")
        if d:
            line_edit.setText(d)

    def _parse_capture_seconds(self):
        text = self.le_cap_seconds.text()
        try:
            secs = [int(s.strip()) for s in text.split(",") if s.strip()]
            return [s for s in secs if 0 <= s < 60]
        except ValueError:
            return [0, 30]

    # Camera connection
    def _on_connect(self):
        self.btn_connect.setEnabled(False)
        self.lbl_model.setText("Connecting...")
        self.lbl_model.setStyleSheet("color:#888; font-weight:bold;")
        self._log("Connecting to Canon camera...", "info")
        self.connect_thread = CannonConnectThread()
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

    # Schedule
    def _add_schedule_row(self):
        row = self.sched_table.rowCount()
        self.sched_table.insertRow(row)
        now = dt.now().replace(second=0, microsecond=0)
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

    # Measurement control
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

        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(status_dir, exist_ok=True)

        instance_name = self.le_instance.text().strip() or get_instance_name("Cannon")
        capture_seconds = self._parse_capture_seconds()
        mqtt_cfg = self._cfg.get("mqtt", {})

        # MQTT publisher
        self._mqtt_pub = None
        if mqtt_cfg.get("enabled") and MQTT_AVAILABLE:
            try:
                self._mqtt_pub = MqttPublisher(
                    host=mqtt_cfg.get("host", ""),
                    port=mqtt_cfg.get("port", 1883),
                    user=mqtt_cfg.get("user", ""),
                    password=mqtt_cfg.get("password", ""),
                    use_tls=mqtt_cfg.get("tls", False),
                )
                self._mqtt_pub.connect_broker()
            except Exception as e:
                self._log(f"MQTT failed: {e}", "warn")
                self._mqtt_pub = None

        self.worker = CannonWorkerQt(
            cam=self.cam, config=self.config, schedule=entries,
            output_dir=output_dir, instance_name=instance_name,
            status_dir=status_dir, capture_seconds=capture_seconds,
            mqtt_publisher=self._mqtt_pub,
            mqtt_prefix=mqtt_cfg.get("prefix", "every_camera"),
        )
        self.worker.log_msg.connect(self._log)
        self.worker.shot_taken.connect(lambda n: self.lbl_shots.setText(f"Shots: {n}"))
        self.worker.status_msg.connect(self._on_status_msg)
        self.worker.countdown.connect(lambda t: self.lbl_countdown.setText(f"Next: {t}"))
        self.worker.error_count.connect(
            lambda n: self.lbl_errors.setText(f"Errors: {n}" if n else ""))
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

    def _on_stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(5000)
        if self._mqtt_pub:
            self._mqtt_pub.disconnect_broker()
            self._mqtt_pub = None
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(self.cam is not None)
        self._on_status_msg("Idle")
        self.lbl_countdown.setText("")

    def _on_worker_finished(self):
        self.btn_stop.setEnabled(False)
        self.btn_start.setEnabled(self.cam is not None)
        self._on_status_msg("Idle")
        self.lbl_countdown.setText("")

    def _on_status_msg(self, msg):
        colors = {"Running": "#007700", "Waiting for schedule": "#aa6600",
                  "Idle": "#555", "Stopped": "#555"}
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(f"font-weight:bold; color:{colors.get(msg, '#555')};")

    def _update_idle_countdown(self):
        if self.worker and self.worker.isRunning():
            return
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


# ===========================================================================
# SPTT GUI components
# ===========================================================================

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


class SpttScheduledWorkerQt(QThread):
    """SPTT scheduled capture worker: captures at :00 and :30, saves as FITS."""
    log_msg = pyqtSignal(str, str)
    shot_taken = pyqtSignal(int)
    status_msg = pyqtSignal(str)
    countdown = pyqtSignal(str)
    error_count = pyqtSignal(int)
    finished = pyqtSignal()

    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, cam, output_dir, instance_name, status_dir,
                 mqtt_publisher=None, mqtt_prefix="every_camera"):
        super().__init__()
        self.cam = cam
        self.output_dir = output_dir
        self.instance_name = instance_name
        self.status_dir = status_dir
        self._mqtt = mqtt_publisher
        self._mqtt_topic = f"{mqtt_prefix}/{instance_name}/status"
        self._stop = False
        self._shots = 0
        self._errors = 0
        self._last_shot = None
        self._status_path = os.path.join(status_dir, f"{os.getpid()}_sptt.json")

    def request_stop(self):
        self._stop = True

    def run(self):
        from sptt_driver import save_fits, ENCODING_12BPP, SPTT_CAPTURE_SECONDS

        last_fired = (-1, -1)
        consecutive_errors = 0
        os.makedirs(self.status_dir, exist_ok=True)

        self.log_msg.emit("SPTT measurement started", "info")
        self.status_msg.emit("Running")
        self._save_status("running")

        try:
            self.cam.start()
        except Exception as e:
            self.log_msg.emit(f"Failed to start camera: {e}", "error")
            self._save_status("error")
            self.finished.emit()
            return

        while not self._stop:
            now = dt.now()

            sec = now.second + now.microsecond / 1_000_000
            remaining = min((s - sec) for s in SPTT_CAPTURE_SECONDS if s > sec) if any(
                s > sec for s in SPTT_CAPTURE_SECONDS) else (60 - sec + SPTT_CAPTURE_SECONDS[0])
            self.countdown.emit(f"{remaining:.1f}s")

            fire_key = (now.minute, now.second)
            if now.second in SPTT_CAPTURE_SECONDS and fire_key != last_fired:
                last_fired = fire_key
                timestamp = now.strftime("%Y%m%dT%H%M%S")
                filepath = os.path.join(self.output_dir, f"{timestamp}.fit")
                try:
                    frame = self.cam.grab_frame()
                    cam_status = self.cam.get_status_info()
                    metadata = {
                        "DATE-OBS": now.isoformat(),
                        "INSTRUME": "CSDU-429",
                        "EXPTIME": self.cam.exposure,
                        "GAIN": self.cam.gain,
                        "BINNING": self.cam.binning,
                        "ENCODING": "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit",
                    }
                    if cam_status:
                        metadata["CCDTEMP"] = cam_status.get("temp_ccd", 0)
                        metadata["SINKTEMP"] = cam_status.get("temp_sink", 0)
                        metadata["TRGTEMP"] = cam_status.get("temp_target", 0)
                    save_fits(filepath, frame, metadata)
                    self.log_msg.emit(f"Frame saved: {os.path.basename(filepath)}", "info")
                    consecutive_errors = 0
                    self._shots += 1
                    self._last_shot = now
                    self.shot_taken.emit(self._shots)
                    self.status_msg.emit("Running")
                    self._save_status("running")
                except Exception as e:
                    self.log_msg.emit(f"Capture error: {e}", "error")
                    consecutive_errors += 1
                    self._errors += 1
                    self.error_count.emit(self._errors)
                    self._save_status("error")
                    if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                        self.log_msg.emit(f"Stopped: {consecutive_errors} consecutive errors", "error")
                        break
            elif now.second not in SPTT_CAPTURE_SECONDS:
                last_fired = (-1, -1)

            self.msleep(100)

        self.cam.stop()
        self._save_status("stopped")
        self._delete_status()
        self.log_msg.emit("SPTT measurement stopped", "info")
        self.finished.emit()

    def _save_status(self, status):
        from sptt_driver import ENCODING_12BPP
        cam_status = {}
        try:
            cam_status = self.cam.get_status_info()
        except Exception:
            pass
        payload = {
            "instance_name": self.instance_name,
            "camera_type": "sptt",
            "pid": os.getpid(),
            "status": status,
            "shots_taken": self._shots,
            "last_shot": self._last_shot.isoformat() if self._last_shot else None,
            "errors": self._errors,
            "frame_size": f"{self.cam.w}x{self.cam.h}",
            "exposure_s": self.cam.exposure,
            "gain": self.cam.gain,
            "binning": self.cam.binning,
            "encoding": "12bit" if self.cam.encoding == ENCODING_12BPP else "8bit",
            "last_update": dt.now().isoformat(),
        }
        payload.update({f"cam_{k}": v for k, v in cam_status.items()})
        try:
            payload["system"] = get_system_info(self.output_dir)
        except Exception:
            pass
        try:
            write_status_file(self._status_path, payload)
        except Exception:
            pass
        if self._mqtt:
            try:
                self._mqtt.publish(self._mqtt_topic, json.dumps(payload), retain=True)
            except Exception:
                pass

    def _delete_status(self):
        try:
            os.remove(self._status_path)
        except FileNotFoundError:
            pass


class SpttTab(QWidget):
    """SPTT camera control tab — live preview + scheduled measurement."""

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
        self._mqtt_pub = None
        self._build_ui()
        self._load_config()

    def _build_ui(self):
        main_layout = QHBoxLayout(self)

        # Left: image display
        self.image_label = QLabel("No image")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(400, 300)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.setStyleSheet("background-color: #1a1a1a; color: #888;")
        main_layout.addWidget(self.image_label, stretch=3)

        # Right: controls
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumWidth(360)
        panel = QWidget()
        layout = QVBoxLayout(panel)

        # Connection
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

        # Exposure / Gain
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

        # Capture params
        grp_cap = QGroupBox("Capture Settings (Apply)")
        g2 = QGridLayout(grp_cap)
        g2.addWidget(QLabel("Binning:"), 0, 0)
        self.combo_binning = QComboBox()
        self.combo_binning.addItems(["0 - 1x1", "1 - 2x2", "2 - 3x3", "3 - 4x4"])
        g2.addWidget(self.combo_binning, 0, 1)
        g2.addWidget(QLabel("Encoding:"), 1, 0)
        self.combo_encoding = QComboBox()
        self.combo_encoding.addItems(["0 - 8 bit", "1 - 12 bit"])
        self.combo_encoding.setCurrentIndex(1)
        g2.addWidget(self.combo_encoding, 1, 1)
        g2.addWidget(QLabel("Target Temp:"), 2, 0)
        self.spin_temp = QSpinBox()
        self.spin_temp.setRange(-30, 30)
        self.spin_temp.setValue(0)
        g2.addWidget(self.spin_temp, 2, 1)
        self.btn_apply = QPushButton("Apply")
        self.btn_apply.setEnabled(False)
        g2.addWidget(self.btn_apply, 3, 0, 1, 2)
        layout.addWidget(grp_cap)

        # Scheduled measurement
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

        # Status bar
        self.lbl_statusbar = QLabel("")

        # Connect signals
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
        self.le_instance.setText(c.get("instance_name") or get_instance_name("SPTT"))
        self.le_output.setText(c.get("output_dir", ""))
        self.spin_exp.setValue(c.get("exposure", 0.88))
        self.spin_gain.setValue(c.get("gain", 100))
        self.combo_binning.setCurrentIndex(c.get("binning", 0))
        self.combo_encoding.setCurrentIndex(c.get("encoding", 1))
        if c.get("target_temp") is not None:
            self.spin_temp.setValue(c["target_temp"])

    def _browse_dir(self, le):
        d = QFileDialog.getExistingDirectory(self, "Select directory")
        if d:
            le.setText(d)

    def _on_connect(self):
        self._log("Initializing SPTT camera...", "info")
        self.btn_connect.setEnabled(False)
        QApplication.processEvents()

        try:
            from sptt_driver import SpttCamera, ensure_firmware_loaded, find_libusb_backend
            backend = find_libusb_backend()
            ensure_firmware_loaded(backend)
            time.sleep(1.5)
            self.cam = SpttCamera(backend)
            self.cam.open()
            self.cam.configure(
                exposure=self.spin_exp.value(),
                gain=self.spin_gain.value(),
                binning=self.combo_binning.currentIndex(),
                encoding=self.combo_encoding.currentIndex(),
                target_temp=self.spin_temp.value() if self.spin_temp.value() != 0 else None,
            )
            self.btn_reconnect.setEnabled(True)
            self.btn_preview_start.setEnabled(True)
            self.btn_apply.setEnabled(True)
            self.btn_meas_start.setEnabled(True)
            self._log(f"SPTT connected: {self.cam.w}x{self.cam.h}", "info")
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
        from sptt_driver import ENCODING_12BPP
        self.current_frame = frame
        self.frame_count += 1
        now = time.time()
        elapsed = now - self.fps_time
        if elapsed >= 1.0:
            self.fps = self.frame_count / elapsed
            self.frame_count = 0
            self.fps_time = now

        if self.cam and self.cam.encoding == ENCODING_12BPP:
            display = (frame.astype(np.float32) / 4095.0 * 255).astype(np.uint8)
        else:
            display = frame
        h, w = display.shape
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
                binning=self.combo_binning.currentIndex(),
                encoding=self.combo_encoding.currentIndex(),
                target_temp=self.spin_temp.value() if self.spin_temp.value() != 0 else None,
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
            except Exception:
                pass

    def _on_gain_changed(self):
        if self.cam:
            try:
                self.cam.set_gain(self.spin_gain.value())
            except Exception:
                pass

    # Scheduled measurement
    def _on_meas_start(self):
        if not self.cam:
            QMessageBox.warning(self, "No camera", "Connect SPTT camera first.")
            return
        output_dir = self.le_output.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "No output", "Select output directory.")
            return

        # Stop preview if running
        if self.capture_thread:
            self._on_preview_stop()

        status_dir = self._cfg.get("status_dir") or HOME_STATUS_DIR
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(status_dir, exist_ok=True)

        instance_name = self.le_instance.text().strip() or get_instance_name("SPTT")
        mqtt_cfg = self._cfg.get("mqtt", {})

        self._mqtt_pub = None
        if mqtt_cfg.get("enabled") and MQTT_AVAILABLE:
            try:
                self._mqtt_pub = MqttPublisher(
                    host=mqtt_cfg.get("host", ""),
                    port=mqtt_cfg.get("port", 1883),
                    user=mqtt_cfg.get("user", ""),
                    password=mqtt_cfg.get("password", ""),
                    use_tls=mqtt_cfg.get("tls", False),
                )
                self._mqtt_pub.connect_broker()
            except Exception as e:
                self._log(f"MQTT failed: {e}", "warn")
                self._mqtt_pub = None

        self.scheduled_worker = SpttScheduledWorkerQt(
            cam=self.cam,
            output_dir=output_dir,
            instance_name=instance_name,
            status_dir=status_dir,
            mqtt_publisher=self._mqtt_pub,
            mqtt_prefix=mqtt_cfg.get("prefix", "every_camera"),
        )
        self.scheduled_worker.log_msg.connect(self._log)
        self.scheduled_worker.shot_taken.connect(
            lambda n: self.lbl_meas_shots.setText(f"Shots: {n}"))
        self.scheduled_worker.status_msg.connect(
            lambda s: self.lbl_meas_status.setText(s))
        self.scheduled_worker.countdown.connect(
            lambda t: self.lbl_meas_countdown.setText(f"Next: {t}"))
        self.scheduled_worker.finished.connect(self._on_meas_finished)
        self.scheduled_worker.start()

        self.btn_meas_start.setEnabled(False)
        self.btn_meas_stop.setEnabled(True)
        self.btn_preview_start.setEnabled(False)

    def _on_meas_stop(self):
        if self.scheduled_worker and self.scheduled_worker.isRunning():
            self.scheduled_worker.request_stop()
            self.scheduled_worker.wait(5000)
        if self._mqtt_pub:
            self._mqtt_pub.disconnect_broker()
            self._mqtt_pub = None
        self.btn_meas_stop.setEnabled(False)
        self.btn_meas_start.setEnabled(self.cam is not None)
        self.btn_preview_start.setEnabled(self.cam is not None)
        self.lbl_meas_status.setText("Idle")
        self.lbl_meas_countdown.setText("")

    def _on_meas_finished(self):
        self.btn_meas_stop.setEnabled(False)
        self.btn_meas_start.setEnabled(self.cam is not None)
        self.btn_preview_start.setEnabled(self.cam is not None)
        self.lbl_meas_status.setText("Idle")

    def cleanup(self):
        self._on_meas_stop()
        self._on_preview_stop()
        if self.cam:
            self.cam.close()


# ===========================================================================
# Main Window
# ===========================================================================

class MainWindow(QMainWindow):
    def __init__(self, cfg, camera_type=None):
        super().__init__()
        self.setWindowTitle("Every Camera")
        self.resize(1100, 750)
        self._cfg = cfg
        self._tabs = {}
        self._build_ui(camera_type)

    def _build_ui(self, camera_type):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # MQTT settings bar
        mqtt_box = QGroupBox("MQTT")
        mqtt_box.setCheckable(True)
        mqtt_box.setChecked(self._cfg.get("mqtt", {}).get("enabled", False))
        self._mqtt_box = mqtt_box
        mqtt_grid = QGridLayout(mqtt_box)
        mqtt_grid.setColumnStretch(1, 1)
        mqtt_grid.addWidget(QLabel("Host:"), 0, 0)
        self.le_mqtt_host = QLineEdit(self._cfg.get("mqtt", {}).get("host", "broker.hivemq.com"))
        mqtt_grid.addWidget(self.le_mqtt_host, 0, 1)
        mqtt_grid.addWidget(QLabel("Port:"), 0, 2)
        self.le_mqtt_port = QLineEdit(str(self._cfg.get("mqtt", {}).get("port", 1883)))
        self.le_mqtt_port.setMaximumWidth(70)
        mqtt_grid.addWidget(self.le_mqtt_port, 0, 3)
        mqtt_grid.addWidget(QLabel("User:"), 0, 4)
        self.le_mqtt_user = QLineEdit(self._cfg.get("mqtt", {}).get("user", ""))
        self.le_mqtt_user.setPlaceholderText("(optional)")
        mqtt_grid.addWidget(self.le_mqtt_user, 0, 5)
        mqtt_grid.addWidget(QLabel("Pass:"), 0, 6)
        self.le_mqtt_pass = QLineEdit(self._cfg.get("mqtt", {}).get("password", ""))
        self.le_mqtt_pass.setEchoMode(QLineEdit.Password)
        mqtt_grid.addWidget(self.le_mqtt_pass, 0, 7)
        mqtt_grid.addWidget(QLabel("Prefix:"), 1, 0)
        self.le_mqtt_prefix = QLineEdit(self._cfg.get("mqtt", {}).get("prefix", "every_camera"))
        mqtt_grid.addWidget(self.le_mqtt_prefix, 1, 1)
        self.cb_mqtt_tls = QCheckBox("TLS")
        self.cb_mqtt_tls.setChecked(self._cfg.get("mqtt", {}).get("tls", False))
        self.cb_mqtt_tls.stateChanged.connect(
            lambda s: self.le_mqtt_port.setText("8883" if s else "1883"))
        mqtt_grid.addWidget(self.cb_mqtt_tls, 1, 2)
        root.addWidget(mqtt_box)

        # Tabs
        self.tab_widget = QTabWidget()

        if camera_type is None or camera_type == "cannon":
            self._cannon_tab = CannonTab(self._cfg, self._log)
            self.tab_widget.addTab(self._cannon_tab, "Canon Camera")
            self._tabs["cannon"] = self._cannon_tab

        if camera_type is None or camera_type == "sptt":
            self._sptt_tab = SpttTab(self._cfg, self._log)
            self.tab_widget.addTab(self._sptt_tab, "SPTT Camera")
            self._tabs["sptt"] = self._sptt_tab

        # Monitor tab
        mqtt_cfg = self._cfg.get("mqtt", {})
        self._monitor = MonitorWidget(mqtt_cfg)
        self.tab_widget.addTab(self._monitor, "Monitor")

        root.addWidget(self.tab_widget, 1)

        # Log
        log_box = QGroupBox("Log")
        log_lay = QVBoxLayout(log_box)
        log_lay.setContentsMargins(4, 4, 4, 4)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFixedHeight(120)
        self.log_text.setFont(QFont("Monospace", 9))
        log_lay.addWidget(self.log_text)
        root.addWidget(log_box)

    def _log(self, message, level="info"):
        colors = {"info": "#000", "warn": "#aa6600", "error": "#cc0000"}
        color = colors.get(level, "#000")
        ts = dt.now().strftime("%H:%M:%S")
        self.log_text.append(f'<span style="color:{color}">[{ts}] {message}</span>')
        self.log_text.moveCursor(QTextCursor.End)

    def _get_mqtt_config(self):
        return {
            "enabled": self._mqtt_box.isChecked(),
            "host": self.le_mqtt_host.text().strip(),
            "port": self.le_mqtt_port.text().strip(),
            "user": self.le_mqtt_user.text().strip(),
            "password": self.le_mqtt_pass.text(),
            "prefix": self.le_mqtt_prefix.text().strip(),
            "tls": self.cb_mqtt_tls.isChecked(),
        }

    def closeEvent(self, event):
        # Save MQTT config back
        self._cfg["mqtt"] = self._get_mqtt_config()
        save_config(self._cfg)
        for tab in self._tabs.values():
            tab.cleanup()
        event.accept()


# ===========================================================================
# Entry point
# ===========================================================================
def run_gui(args):
    """Launch the GUI application."""
    # Fix Qt platform plugin conflict with OpenCV
    try:
        import PyQt5 as _pyqt5
        _qt_plugins = os.path.join(os.path.dirname(_pyqt5.__file__), "Qt5", "plugins")
        if os.path.isdir(_qt_plugins):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = _qt_plugins
    except Exception:
        os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)

    cfg = load_config(args.config if hasattr(args, 'config') else None)
    camera_type = getattr(args, 'type', None)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    win = MainWindow(cfg, camera_type)
    win.show()

    sys.exit(app.exec_())
