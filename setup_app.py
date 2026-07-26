#!/usr/bin/env python3
"""
Every Camera — Configuration Wizard.

Standalone application for configuring every-camera.  Reads and writes
the same config.json used by main.py and gui_app.py.

GUI mode (default when a display is available):
    python setup_app.py                       # all cameras
    python setup_app.py --type cannon         # Canon tab only
    python setup_app.py --config path.json    # custom config file

Console mode:
    python setup_app.py --console             # interactive prompts, all cameras
    python setup_app.py --console --type sptt # configure SPTT only
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import load_config, save_config, DEFAULT_CONFIG, can_use_gui, LOCAL_CONFIG_FILE


# ---------------------------------------------------------------------------
# Console wizard
# ---------------------------------------------------------------------------
def run_console_wizard(config_path=None, camera_type=None):
    """Interactive terminal configuration wizard."""
    from utils import (
        configure_console_cannon,
        configure_console_sptt,
        configure_console_infra,
        configure_console_sentry,
        _configure_mqtt,
        _ask_bool,
    )

    cfg = load_config(config_path)

    CONFIGURATORS = {
        "cannon": configure_console_cannon,
        "sptt":   configure_console_sptt,
        "infra":  configure_console_infra,
        "sentry": configure_console_sentry,
    }

    print("\n" + "=" * 52)
    print("  Every Camera — Configuration Wizard (console)")
    print("=" * 52 + "\n")

    if camera_type:
        fn = CONFIGURATORS.get(camera_type)
        if fn is None:
            print(f"Error: unknown camera type '{camera_type}'")
            sys.exit(1)
        fn(cfg, config_path)
        return

    # Select which cameras to configure
    to_configure = []
    print("Select cameras to configure:\n")
    for name in CONFIGURATORS:
        if _ask_bool(f"  Configure {name}?", False):
            to_configure.append(name)

    if not to_configure:
        print("\nNothing selected — no changes made.\n")
        return

    for name in to_configure:
        CONFIGURATORS[name](cfg, config_path)

    # MQTT settings
    _configure_mqtt(cfg)
    save_config(cfg, config_path)
    print("\nAll configuration saved.\n")


# ---------------------------------------------------------------------------
# GUI wizard helpers
# ---------------------------------------------------------------------------
def _browse_dir(parent, le):
    from PyQt5.QtWidgets import QFileDialog
    d = QFileDialog.getExistingDirectory(parent, "Select directory", le.text())
    if d:
        le.setText(d)


def _browse_file(parent, le, caption="Select file", flt="All files (*)"):
    from PyQt5.QtWidgets import QFileDialog
    f, _ = QFileDialog.getOpenFileName(parent, caption, le.text(), flt)
    if f:
        le.setText(f)


def _add_dir_row(grid, row, label, le, parent=None):
    """label | line_edit | Browse button  →  placed in grid at (row, 0..2)."""
    from PyQt5.QtWidgets import QLabel, QPushButton
    grid.addWidget(QLabel(label), row, 0)
    grid.addWidget(le, row, 1)
    btn = QPushButton("Browse…")
    btn.setMaximumWidth(80)
    btn.clicked.connect(lambda: _browse_dir(parent, le))
    grid.addWidget(btn, row, 2)


def _add_file_row(grid, row, label, le, caption="Select file", flt="All (*)", parent=None):
    from PyQt5.QtWidgets import QLabel, QPushButton
    grid.addWidget(QLabel(label), row, 0)
    grid.addWidget(le, row, 1)
    btn = QPushButton("Browse…")
    btn.setMaximumWidth(80)
    btn.clicked.connect(lambda: _browse_file(parent, le, caption, flt))
    grid.addWidget(btn, row, 2)


def _add_label_row(grid, row, label, widget):
    from PyQt5.QtWidgets import QLabel
    grid.addWidget(QLabel(label), row, 0)
    grid.addWidget(widget, row, 1)


def _scrolled(widget):
    """Wrap widget in a QScrollArea and return the scroll area."""
    from PyQt5.QtWidgets import QScrollArea
    from PyQt5.QtCore import Qt
    sa = QScrollArea()
    sa.setWidget(widget)
    sa.setWidgetResizable(True)
    sa.setFrameShape(sa.NoFrame)
    return sa


def _group_grid(title, col_stretch=1):
    """Return (QGroupBox, QGridLayout inside it)."""
    from PyQt5.QtWidgets import QGroupBox, QGridLayout
    box = QGroupBox(title)
    lay = QGridLayout(box)
    lay.setColumnStretch(1, col_stretch)
    lay.setHorizontalSpacing(8)
    lay.setVerticalSpacing(6)
    return box, lay


# ---------------------------------------------------------------------------
# Canon config tab
# ---------------------------------------------------------------------------
class CannonConfigTab:
    """Builds and reads the Canon camera configuration form."""

    def __init__(self, cfg: dict):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QLineEdit,
        )
        c = cfg.get("cannon", {})
        self.widget = QWidget()
        root = QVBoxLayout(self.widget)
        root.setContentsMargins(8, 8, 8, 8)

        box, grid = _group_grid("Canon DSLR (gphoto2)")
        row = 0

        self.le_output = QLineEdit(c.get("output_dir", ""))
        _add_dir_row(grid, row, "Output directory:", self.le_output); row += 1

        self.le_schedule = QLineEdit(c.get("schedule_file", ""))
        _add_file_row(grid, row, "Schedule file:", self.le_schedule,
                      "Schedule file", "Text files (*.txt *.conf);;All (*)"); row += 1

        self.le_camcfg = QLineEdit(c.get("camcfg_file", ""))
        _add_file_row(grid, row, "Camera cfg file:", self.le_camcfg,
                      "Camera config", "All (*)"); row += 1

        self.le_instance = QLineEdit(c.get("instance_name", ""))
        self.le_instance.setPlaceholderText("auto (hostname_IP)")
        _add_label_row(grid, row, "Instance name:", self.le_instance); row += 1

        self.le_capture_secs = QLineEdit(
            ", ".join(str(s) for s in c.get("capture_seconds", [0, 30]))
        )
        self.le_capture_secs.setToolTip("Seconds within each minute to publish (e.g. 0, 30)")
        _add_label_row(grid, row, "Capture seconds:", self.le_capture_secs); row += 1

        root.addWidget(box)
        root.addStretch()

    def get_config(self) -> dict:
        try:
            secs = [int(s.strip()) for s in self.le_capture_secs.text().split(",") if s.strip()]
        except ValueError:
            secs = [0, 30]
        return {
            "output_dir":      self.le_output.text().strip(),
            "schedule_file":   self.le_schedule.text().strip(),
            "camcfg_file":     self.le_camcfg.text().strip(),
            "instance_name":   self.le_instance.text().strip(),
            "capture_seconds": secs,
        }


# ---------------------------------------------------------------------------
# SPTT config tab
# ---------------------------------------------------------------------------
class SpttConfigTab:
    """Builds and reads the SPTT (CSDU-429) camera configuration form."""

    def __init__(self, cfg: dict):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QLineEdit,
            QDoubleSpinBox, QSpinBox, QComboBox, QCheckBox,
        )
        c = cfg.get("sptt", {})
        self.widget = QWidget()
        root = QVBoxLayout(self.widget)
        root.setContentsMargins(8, 8, 8, 8)

        box, grid = _group_grid("SPTT Camera (CSDU-429)")
        row = 0

        self.le_output = QLineEdit(c.get("output_dir", ""))
        _add_dir_row(grid, row, "Output directory:", self.le_output); row += 1

        self.le_firmware = QLineEdit(c.get("firmware_dir", ""))
        _add_dir_row(grid, row, "Firmware directory:", self.le_firmware); row += 1

        self.le_instance = QLineEdit(c.get("instance_name", ""))
        self.le_instance.setPlaceholderText("auto")
        _add_label_row(grid, row, "Instance name:", self.le_instance); row += 1

        self.sb_exposure = QDoubleSpinBox()
        self.sb_exposure.setRange(0.001, 3600.0)
        self.sb_exposure.setDecimals(3)
        self.sb_exposure.setSuffix(" s")
        self.sb_exposure.setValue(c.get("exposure", 0.88))
        _add_label_row(grid, row, "Exposure:", self.sb_exposure); row += 1

        self.sb_gain = QSpinBox()
        self.sb_gain.setRange(0, 1023)
        self.sb_gain.setValue(c.get("gain", 100))
        _add_label_row(grid, row, "Gain (0–1023):", self.sb_gain); row += 1

        self.cb_binning = QComboBox()
        self.cb_binning.addItems(["1×1 (0)", "2×2 (1)", "4×4 (3)"])
        binning_map = {0: 0, 1: 1, 3: 2}
        self.cb_binning.setCurrentIndex(binning_map.get(c.get("binning", 0), 0))
        _add_label_row(grid, row, "Binning:", self.cb_binning); row += 1

        self.cb_encoding = QComboBox()
        self.cb_encoding.addItems(["8-bit (0)", "12-bit (1)"])
        self.cb_encoding.setCurrentIndex(1 if c.get("encoding", 1) == 1 else 0)
        _add_label_row(grid, row, "Encoding:", self.cb_encoding); row += 1

        # target_temp: None or float
        from PyQt5.QtWidgets import QHBoxLayout
        temp_w = QWidget()
        temp_lay = QHBoxLayout(temp_w)
        temp_lay.setContentsMargins(0, 0, 0, 0)
        self.cb_temp_none = QCheckBox("None (no cooling)")
        self.sb_temp = QDoubleSpinBox()
        self.sb_temp.setRange(-60.0, 40.0)
        self.sb_temp.setSuffix(" °C")
        tt = c.get("target_temp")
        if tt is None:
            self.cb_temp_none.setChecked(True)
            self.sb_temp.setValue(-10.0)
            self.sb_temp.setEnabled(False)
        else:
            self.cb_temp_none.setChecked(False)
            self.sb_temp.setValue(float(tt))
        self.cb_temp_none.stateChanged.connect(
            lambda s: self.sb_temp.setEnabled(not bool(s)))
        temp_lay.addWidget(self.cb_temp_none)
        temp_lay.addWidget(self.sb_temp)
        temp_lay.addStretch()
        _add_label_row(grid, row, "Target temp:", temp_w); row += 1

        self.le_capture_secs = QLineEdit(
            ", ".join(str(s) for s in c.get("capture_seconds", [0, 30]))
        )
        _add_label_row(grid, row, "Capture seconds:", self.le_capture_secs); row += 1

        root.addWidget(box)
        root.addStretch()

    def get_config(self) -> dict:
        binning_vals = [0, 1, 3]
        try:
            secs = [int(s.strip()) for s in self.le_capture_secs.text().split(",") if s.strip()]
        except ValueError:
            secs = [0, 30]
        return {
            "output_dir":    self.le_output.text().strip(),
            "firmware_dir":  self.le_firmware.text().strip(),
            "instance_name": self.le_instance.text().strip(),
            "exposure":      self.sb_exposure.value(),
            "gain":          self.sb_gain.value(),
            "binning":       binning_vals[self.cb_binning.currentIndex()],
            "encoding":      self.cb_encoding.currentIndex(),
            "target_temp":   None if self.cb_temp_none.isChecked() else self.sb_temp.value(),
            "capture_seconds": secs,
        }


# ---------------------------------------------------------------------------
# Infra config tab
# ---------------------------------------------------------------------------
class InfraConfigTab:
    """Builds and reads the Infra (SW1300 SWIR) camera configuration form."""

    def __init__(self, cfg: dict):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QLineEdit,
            QDoubleSpinBox, QSpinBox, QComboBox,
        )
        c = cfg.get("infra", {})
        self.widget = QWidget()
        root = QVBoxLayout(self.widget)
        root.setContentsMargins(8, 8, 8, 8)

        box, grid = _group_grid("Infra Camera (Tanho SW1300 SWIR)")
        row = 0

        self.le_output = QLineEdit(c.get("output_dir", ""))
        _add_dir_row(grid, row, "Output directory:", self.le_output); row += 1

        self.le_schedule = QLineEdit(c.get("schedule_file", ""))
        _add_file_row(grid, row, "Schedule file:", self.le_schedule,
                      "Schedule file", "Text files (*.txt *.conf);;All (*)"); row += 1

        self.le_instance = QLineEdit(c.get("instance_name", ""))
        self.le_instance.setPlaceholderText("auto")
        _add_label_row(grid, row, "Instance name:", self.le_instance); row += 1

        self.sb_exposure = QDoubleSpinBox()
        self.sb_exposure.setRange(1.0, 1_000_000.0)
        self.sb_exposure.setDecimals(1)
        self.sb_exposure.setSuffix(" µs")
        self.sb_exposure.setValue(c.get("exposure_us", 1000.0))
        _add_label_row(grid, row, "Exposure:", self.sb_exposure); row += 1

        self.sb_gain = QSpinBox()
        self.sb_gain.setRange(0, 120)
        self.sb_gain.setValue(c.get("gain", 0))
        _add_label_row(grid, row, "Gain (0–120):", self.sb_gain); row += 1

        self.cb_roi = QComboBox()
        self.cb_roi.addItems(["1280×1024 (full)", "1280×256 (strip)"])
        self.cb_roi.setCurrentIndex(0 if c.get("roi", "1280x1024") == "1280x1024" else 1)
        _add_label_row(grid, row, "ROI:", self.cb_roi); row += 1

        self.cb_fmt = QComboBox()
        self.cb_fmt.addItems(["tiff", "png", "fits"])
        fmt_idx = {"tiff": 0, "png": 1, "fits": 2}.get(c.get("save_format", "tiff"), 0)
        self.cb_fmt.setCurrentIndex(fmt_idx)
        _add_label_row(grid, row, "Save format:", self.cb_fmt); row += 1

        self.le_capture_secs = QLineEdit(
            ", ".join(str(s) for s in c.get("capture_seconds", [0, 30]))
        )
        _add_label_row(grid, row, "Capture seconds:", self.le_capture_secs); row += 1

        root.addWidget(box)
        root.addStretch()

    def get_config(self) -> dict:
        roi_vals = ["1280x1024", "1280x256"]
        fmt_vals = ["tiff", "png", "fits"]
        try:
            secs = [int(s.strip()) for s in self.le_capture_secs.text().split(",") if s.strip()]
        except ValueError:
            secs = [0, 30]
        return {
            "output_dir":      self.le_output.text().strip(),
            "schedule_file":   self.le_schedule.text().strip(),
            "instance_name":   self.le_instance.text().strip(),
            "exposure_us":     self.sb_exposure.value(),
            "gain":            self.sb_gain.value(),
            "roi":             roi_vals[self.cb_roi.currentIndex()],
            "save_format":     fmt_vals[self.cb_fmt.currentIndex()],
            "capture_seconds": secs,
        }


# ---------------------------------------------------------------------------
# Sentry config tab (with imaging-slot editor)
# ---------------------------------------------------------------------------
class SentryConfigTab:
    """Builds and reads the Sentry (imagerd_rt) camera configuration form."""

    # Table columns for the imaging slots editor
    SLOT_HEADERS  = ["Filter #", "Start (ms)", "Exposure (ms)", "Binning", "Gain", "Readout (ms)"]
    SLOT_KEYS     = ["filter_num", "start_ms",  "exposure_ms",   "binning", "gain", "readout_ms"]
    SLOT_DEFAULTS = [1,            0,            55000,           2,         3,      5000]

    def __init__(self, cfg: dict):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
            QDoubleSpinBox, QSpinBox, QComboBox, QPushButton,
            QTableWidget, QTableWidgetItem, QHeaderView,
            QAbstractItemView,
        )
        c = cfg.get("sentry", {})
        self.widget = QWidget()
        root = QVBoxLayout(self.widget)
        root.setContentsMargins(8, 8, 8, 8)

        # ── Main settings ──────────────────────────────────────────────────
        box, grid = _group_grid("Sentry Camera (imagerd_rt supervisor)")
        row = 0

        self.le_dir = QLineEdit(c.get("imagerd_rt_dir", "/usr/local/imagerd_rt"))
        _add_dir_row(grid, row, "imagerd_rt directory:", self.le_dir); row += 1

        self.le_output = QLineEdit(c.get("output_dir", ""))
        _add_dir_row(grid, row, "Output directory:", self.le_output); row += 1

        self.le_instance = QLineEdit(c.get("instance_name", ""))
        self.le_instance.setPlaceholderText("auto")
        _add_label_row(grid, row, "Instance name:", self.le_instance); row += 1

        self.le_device_id = QLineEdit(c.get("device_id", "ASI1"))
        _add_label_row(grid, row, "Device ID:", self.le_device_id); row += 1

        self.le_site_id = QLineEdit(c.get("site_id", "SITE"))
        _add_label_row(grid, row, "Site ID:", self.le_site_id); row += 1

        self.sb_zenith = QDoubleSpinBox()
        self.sb_zenith.setRange(-90.0, 90.0)
        self.sb_zenith.setDecimals(1)
        self.sb_zenith.setSuffix("°")
        self.sb_zenith.setToolTip("Solar zenith angle at which imaging starts (negative = sun below horizon)")
        self.sb_zenith.setValue(c.get("zenith_angle_start", -10))
        _add_label_row(grid, row, "Night start zenith angle:", self.sb_zenith); row += 1

        self.sb_sched_len = QSpinBox()
        self.sb_sched_len.setRange(5000, 86_400_000)
        self.sb_sched_len.setSuffix(" ms")
        self.sb_sched_len.setToolTip("Duration of one imaging cycle in milliseconds (1 440 000 = 24 min)")
        self.sb_sched_len.setValue(c.get("schedule_len_ms", 1440000))
        _add_label_row(grid, row, "Schedule cycle length:", self.sb_sched_len); row += 1

        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["schedule", "rapid"])
        mode_val = c.get("imaging_mode", c.get("image_mode", "schedule"))
        if isinstance(mode_val, int):
            mode_val = "schedule" if mode_val == 0 else "rapid"
        self.cb_mode.setCurrentText(mode_val)
        self.cb_mode.setToolTip("'schedule' = timed slots; 'rapid' = continuous capture")
        _add_label_row(grid, row, "Imaging mode:", self.cb_mode); row += 1

        self.le_capture_secs = QLineEdit(
            ", ".join(str(s) for s in c.get("capture_seconds", [0, 30]))
        )
        self.le_capture_secs.setToolTip("Seconds within each minute to poll daemon status / publish MQTT")
        _add_label_row(grid, row, "Capture seconds:", self.le_capture_secs); row += 1

        root.addWidget(box)

        # ── Imaging slots editor ───────────────────────────────────────────
        from PyQt5.QtWidgets import QGroupBox
        slot_box = QGroupBox("Imaging Slots  (one row per filter position)")
        slot_root = QVBoxLayout(slot_box)

        self._slot_table = QTableWidget(0, len(self.SLOT_HEADERS))
        self._slot_table.setHorizontalHeaderLabels(self.SLOT_HEADERS)
        self._slot_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._slot_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._slot_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)
        self._slot_table.setMinimumHeight(140)

        # Populate existing slots
        for slot in c.get("slots", []):
            self._append_slot_row(slot)

        slot_root.addWidget(self._slot_table)

        # Slot toolbar
        btn_bar = QHBoxLayout()
        btn_add = QPushButton("+ Add slot")
        btn_add.clicked.connect(self._add_slot)
        btn_del = QPushButton("− Remove selected")
        btn_del.clicked.connect(self._remove_slot)
        btn_up = QPushButton("↑ Move up")
        btn_up.clicked.connect(self._move_up)
        btn_dn = QPushButton("↓ Move down")
        btn_dn.clicked.connect(self._move_down)
        for b in (btn_add, btn_del, btn_up, btn_dn):
            btn_bar.addWidget(b)
        btn_bar.addStretch()
        slot_root.addLayout(btn_bar)

        root.addWidget(slot_box)
        root.addStretch()

    # ── Slot table helpers ─────────────────────────────────────────────────

    def _append_slot_row(self, slot=None):
        from PyQt5.QtWidgets import QTableWidgetItem
        row = self._slot_table.rowCount()
        self._slot_table.insertRow(row)
        vals = slot or {}
        for col, (key, default) in enumerate(zip(self.SLOT_KEYS, self.SLOT_DEFAULTS)):
            # handle both old and new key names for backward compat
            if key == "start_ms":
                v = vals.get("start_ms", vals.get("start_time_ms", default))
            elif key == "gain":
                v = vals.get("gain", vals.get("ccd_gain", default))
            elif key == "readout_ms":
                v = vals.get("readout_ms", vals.get("prep_time_ms", default))
            else:
                v = vals.get(key, default)
            self._slot_table.setItem(row, col, QTableWidgetItem(str(v)))

    def _add_slot(self):
        self._append_slot_row()

    def _remove_slot(self):
        row = self._slot_table.currentRow()
        if row >= 0:
            self._slot_table.removeRow(row)

    def _move_up(self):
        row = self._slot_table.currentRow()
        if row > 0:
            self._swap_rows(row, row - 1)
            self._slot_table.selectRow(row - 1)

    def _move_down(self):
        row = self._slot_table.currentRow()
        if 0 <= row < self._slot_table.rowCount() - 1:
            self._swap_rows(row, row + 1)
            self._slot_table.selectRow(row + 1)

    def _swap_rows(self, a, b):
        from PyQt5.QtWidgets import QTableWidgetItem
        for col in range(self._slot_table.columnCount()):
            ia = self._slot_table.item(a, col)
            ib = self._slot_table.item(b, col)
            ta = ia.text() if ia else ""
            tb = ib.text() if ib else ""
            self._slot_table.setItem(a, col, QTableWidgetItem(tb))
            self._slot_table.setItem(b, col, QTableWidgetItem(ta))

    def _read_slots(self) -> list:
        slots = []
        for row in range(self._slot_table.rowCount()):
            slot = {}
            for col, (key, default) in enumerate(zip(self.SLOT_KEYS, self.SLOT_DEFAULTS)):
                item = self._slot_table.item(row, col)
                raw = item.text().strip() if item else str(default)
                try:
                    slot[key] = int(raw)
                except ValueError:
                    try:
                        slot[key] = float(raw)
                    except ValueError:
                        slot[key] = default
            slots.append(slot)
        return slots

    def get_config(self) -> dict:
        try:
            secs = [int(s.strip()) for s in self.le_capture_secs.text().split(",") if s.strip()]
        except ValueError:
            secs = [0, 30]
        return {
            "imagerd_rt_dir":  self.le_dir.text().strip(),
            "output_dir":      self.le_output.text().strip(),
            "instance_name":   self.le_instance.text().strip(),
            "device_id":       self.le_device_id.text().strip(),
            "site_id":         self.le_site_id.text().strip(),
            "zenith_angle_start": self.sb_zenith.value(),
            "schedule_len_ms": self.sb_sched_len.value(),
            "imaging_mode":    self.cb_mode.currentText(),
            "capture_seconds": secs,
            "slots":           self._read_slots(),
        }


# ---------------------------------------------------------------------------
# MQTT config tab
# ---------------------------------------------------------------------------
class MqttConfigTab:
    """Builds and reads the MQTT broker configuration form."""

    def __init__(self, cfg: dict):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QLineEdit, QSpinBox, QCheckBox,
        )
        m = cfg.get("mqtt", {})
        self.widget = QWidget()
        root = QVBoxLayout(self.widget)
        root.setContentsMargins(8, 8, 8, 8)

        box, grid = _group_grid("MQTT Broker")
        row = 0

        self.cb_enabled = QCheckBox("Enable MQTT publishing")
        self.cb_enabled.setChecked(m.get("enabled", False))
        grid.addWidget(self.cb_enabled, row, 0, 1, 2); row += 1

        self.le_host = QLineEdit(m.get("host", "broker.hivemq.com"))
        _add_label_row(grid, row, "Broker host:", self.le_host); row += 1

        self.sb_port = QSpinBox()
        self.sb_port.setRange(1, 65535)
        try:
            self.sb_port.setValue(int(m.get("port", 1883)))
        except (ValueError, TypeError):
            self.sb_port.setValue(1883)
        _add_label_row(grid, row, "Port:", self.sb_port); row += 1

        self.le_user = QLineEdit(m.get("user", ""))
        self.le_user.setPlaceholderText("(leave blank if not required)")
        _add_label_row(grid, row, "Username:", self.le_user); row += 1

        self.le_pass = QLineEdit(m.get("password", ""))
        self.le_pass.setEchoMode(QLineEdit.Password)
        self.le_pass.setPlaceholderText("(leave blank if not required)")
        _add_label_row(grid, row, "Password:", self.le_pass); row += 1

        self.le_prefix = QLineEdit(m.get("prefix", "every_camera"))
        self.le_prefix.setToolTip("MQTT topic prefix, e.g. every_camera/<instance>/status")
        _add_label_row(grid, row, "Topic prefix:", self.le_prefix); row += 1

        self.cb_tls = QCheckBox("Use TLS (port 8883)")
        self.cb_tls.setChecked(m.get("tls", False))
        self.cb_tls.stateChanged.connect(
            lambda s: self.sb_port.setValue(8883 if s else 1883))
        grid.addWidget(self.cb_tls, row, 0, 1, 2); row += 1

        root.addWidget(box)
        root.addStretch()

    def get_config(self) -> dict:
        return {
            "enabled":  self.cb_enabled.isChecked(),
            "host":     self.le_host.text().strip(),
            "port":     self.sb_port.value(),
            "user":     self.le_user.text().strip(),
            "password": self.le_pass.text(),
            "prefix":   self.le_prefix.text().strip(),
            "tls":      self.cb_tls.isChecked(),
        }


# ---------------------------------------------------------------------------
# LAN frame server config tab
# ---------------------------------------------------------------------------
class ServerConfigTab:
    """Builds and reads the LAN frame-server configuration form."""

    def __init__(self, cfg: dict):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QLineEdit, QSpinBox, QCheckBox, QLabel,
        )
        s = cfg.get("server", {})
        self.widget = QWidget()
        root = QVBoxLayout(self.widget)
        root.setContentsMargins(8, 8, 8, 8)

        blurb = QLabel(
            "The LAN frame server lets <b>viewer_app.py</b> browse every "
            "captured frame and <b>focus_app.py</b> watch a live stream, over "
            "the local network.<br>It runs on its own threads and never "
            "delays a measurement — if the port is busy it just logs a "
            "warning.<br><b>Access is unauthenticated</b>: use it on a "
            "trusted network only.")
        blurb.setWordWrap(True)
        root.addWidget(blurb)

        box, grid = _group_grid("LAN Frame Server")
        row = 0

        self.cb_enabled = QCheckBox("Enable LAN frame server")
        self.cb_enabled.setChecked(s.get("enabled", True))
        grid.addWidget(self.cb_enabled, row, 0, 1, 2); row += 1

        self.sb_port = QSpinBox()
        self.sb_port.setRange(1, 65535)
        try:
            self.sb_port.setValue(int(s.get("port", 8765)))
        except (ValueError, TypeError):
            self.sb_port.setValue(8765)
        _add_label_row(grid, row, "HTTP port:", self.sb_port); row += 1

        self.le_bind = QLineEdit(s.get("bind", "0.0.0.0"))
        self.le_bind.setToolTip(
            "0.0.0.0 = reachable from the whole LAN.\n"
            "127.0.0.1 = this machine only.")
        _add_label_row(grid, row, "Bind address:", self.le_bind); row += 1

        self.cb_discovery = QCheckBox("Answer UDP discovery broadcasts")
        self.cb_discovery.setChecked(s.get("discovery", True))
        self.cb_discovery.setToolTip(
            "Lets the viewer find this camera automatically instead of "
            "requiring its IP address.")
        grid.addWidget(self.cb_discovery, row, 0, 1, 2); row += 1

        self.sb_disc_port = QSpinBox()
        self.sb_disc_port.setRange(1, 65535)
        try:
            self.sb_disc_port.setValue(int(s.get("discovery_port", 45455)))
        except (ValueError, TypeError):
            self.sb_disc_port.setValue(45455)
        _add_label_row(grid, row, "Discovery UDP port:", self.sb_disc_port); row += 1

        self.sb_focus_ttl = QSpinBox()
        self.sb_focus_ttl.setRange(5, 600)
        self.sb_focus_ttl.setSuffix(" s")
        try:
            self.sb_focus_ttl.setValue(int(s.get("focus_ttl", 60)))
        except (ValueError, TypeError):
            self.sb_focus_ttl.setValue(60)
        self.sb_focus_ttl.setToolTip(
            "How long focus mode stays on after the last request from "
            "focus_app.py. It switches itself off after this, so a crashed "
            "viewer cannot leave the camera free-running.")
        _add_label_row(grid, row, "Focus mode timeout:", self.sb_focus_ttl); row += 1

        root.addWidget(box)
        root.addStretch()

    def get_config(self) -> dict:
        return {
            "enabled":        self.cb_enabled.isChecked(),
            "bind":           self.le_bind.text().strip() or "0.0.0.0",
            "port":           self.sb_port.value(),
            "discovery":      self.cb_discovery.isChecked(),
            "discovery_port": self.sb_disc_port.value(),
            "focus_ttl":      self.sb_focus_ttl.value(),
            "max_list":       2000,
        }


# ---------------------------------------------------------------------------
# General config tab
# ---------------------------------------------------------------------------
class GeneralConfigTab:
    """Builds and reads the general (non-camera) configuration form."""

    def __init__(self, cfg: dict):
        from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLineEdit
        self.widget = QWidget()
        root = QVBoxLayout(self.widget)
        root.setContentsMargins(8, 8, 8, 8)

        box, grid = _group_grid("General Settings")
        self.le_status_dir = QLineEdit(cfg.get("status_dir", ""))
        self.le_status_dir.setPlaceholderText("~/.every_camera/status  (default)")
        self.le_status_dir.setToolTip(
            "Directory where per-process status JSON files are written.\n"
            "Leave blank to use the default (~/.every_camera/status)."
        )
        _add_dir_row(grid, 0, "Status directory:", self.le_status_dir)

        root.addWidget(box)
        root.addStretch()

    def get_config(self) -> dict:
        return {"status_dir": self.le_status_dir.text().strip()}


# ---------------------------------------------------------------------------
# Main config wizard window
# ---------------------------------------------------------------------------
class ConfigWizardWindow:
    """PyQt5 configuration wizard window."""

    TAB_ALL = ["cannon", "sptt", "infra", "sentry", "mqtt", "server", "general"]

    def __init__(self, cfg: dict, camera_type=None, config_path: str = LOCAL_CONFIG_FILE):
        from PyQt5.QtWidgets import (
            QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
            QTabWidget, QPushButton, QStatusBar, QMessageBox,
        )
        from PyQt5.QtGui import QFont

        self._cfg = cfg
        self._config_path = config_path

        self.window = QMainWindow()
        self.window.setWindowTitle("Every Camera — Setup")
        self.window.setMinimumSize(640, 500)
        self.window.resize(820, 600)

        central = QWidget()
        self.window.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # ── Tabs ──────────────────────────────────────────────────────────
        self._tabs_widget = QTabWidget()
        self._camera_tabs = {}

        BUILDERS = {
            "cannon":  (CannonConfigTab,  "Canon"),
            "sptt":    (SpttConfigTab,    "SPTT"),
            "infra":   (InfraConfigTab,   "Infra"),
            "sentry":  (SentryConfigTab,  "Sentry"),
            "mqtt":    (MqttConfigTab,    "MQTT"),
            "server":  (ServerConfigTab,  "LAN Server"),
            "general": (GeneralConfigTab, "General"),
        }

        shown = [camera_type] if camera_type else self.TAB_ALL
        for key in shown:
            if key not in BUILDERS:
                continue
            cls, label = BUILDERS[key]
            tab = cls(cfg)
            self._camera_tabs[key] = tab
            self._tabs_widget.addTab(_scrolled(tab.widget), label)

        root.addWidget(self._tabs_widget, 1)

        # ── Button bar ────────────────────────────────────────────────────
        btn_bar = QHBoxLayout()

        btn_load = QPushButton("Load config…")
        btn_load.setToolTip("Load a different config.json file")
        btn_load.clicked.connect(self._on_load)

        btn_reset = QPushButton("Reset to defaults")
        btn_reset.setToolTip("Restore all settings to factory defaults")
        btn_reset.clicked.connect(self._on_reset)

        btn_save = QPushButton("Save")
        btn_save.setDefault(True)
        btn_save.setToolTip(f"Save to {config_path}")
        btn_save.clicked.connect(self._on_save)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.window.close)

        btn_bar.addWidget(btn_load)
        btn_bar.addWidget(btn_reset)
        btn_bar.addStretch()
        btn_bar.addWidget(btn_save)
        btn_bar.addWidget(btn_close)
        root.addLayout(btn_bar)

        # ── Status bar ────────────────────────────────────────────────────
        self._status = QStatusBar()
        self.window.setStatusBar(self._status)
        self._status.showMessage(f"Config: {config_path}")

    # ── Slots ──────────────────────────────────────────────────────────────

    def _collect_config(self) -> dict:
        """Merge values from all visible tabs into a full config dict."""
        cfg = {k: v for k, v in self._cfg.items()}  # start with full existing config

        KEY_MAP = {
            "cannon":  "cannon",
            "sptt":    "sptt",
            "infra":   "infra",
            "sentry":  "sentry",
            "mqtt":    "mqtt",
            "server":  "server",
        }
        for tab_key, cfg_key in KEY_MAP.items():
            tab = self._camera_tabs.get(tab_key)
            if tab is not None:
                cfg[cfg_key] = tab.get_config()

        general_tab = self._camera_tabs.get("general")
        if general_tab is not None:
            cfg.update(general_tab.get_config())

        return cfg

    def _on_save(self):
        from PyQt5.QtWidgets import QMessageBox
        cfg = self._collect_config()
        try:
            save_config(cfg, self._config_path)
            self._status.showMessage(f"Saved: {self._config_path}")
        except Exception as exc:
            QMessageBox.critical(self.window, "Save failed", str(exc))

    def _on_load(self):
        from PyQt5.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(
            self.window, "Load config file", self._config_path,
            "JSON files (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            new_cfg = load_config(path)
        except Exception as exc:
            QMessageBox.critical(self.window, "Load failed", str(exc))
            return
        # Rebuild window with new config (simplest approach: close + reopen)
        QMessageBox.information(
            self.window, "Config loaded",
            f"Loaded:\n{path}\n\nRestart the wizard to see the new values."
        )
        self._config_path = path
        self._cfg = new_cfg
        self._status.showMessage(f"Config: {path}  (restart to reload UI)")

    def _on_reset(self):
        from PyQt5.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self.window, "Reset to defaults",
            "Reset all fields to factory defaults?\nUnsaved changes will be lost.",
            QMessageBox.Yes | QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        # Rebuild the UI with default config
        new_win = ConfigWizardWindow(
            DEFAULT_CONFIG,
            config_path=self._config_path,
        )
        new_win.show()
        self.window.close()

    def show(self):
        self.window.show()


# ---------------------------------------------------------------------------
# GUI entry point
# ---------------------------------------------------------------------------
def run_gui_wizard(config_path=None, camera_type=None):
    """Launch the PyQt5 configuration wizard."""
    from PyQt5.QtWidgets import QApplication

    cfg = load_config(config_path)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    win = ConfigWizardWindow(cfg, camera_type=camera_type,
                             config_path=config_path or LOCAL_CONFIG_FILE)
    win.show()
    sys.exit(app.exec_())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Every Camera — Configuration Wizard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python setup_app.py                      # GUI wizard, all cameras
  python setup_app.py --type sentry        # GUI wizard, Sentry tab only
  python setup_app.py --console            # console wizard, all cameras
  python setup_app.py --console --type infra  # console wizard, Infra only
  python setup_app.py --config /my/cfg.json   # use custom config file
        """,
    )
    parser.add_argument(
        "--type", choices=["cannon", "sptt", "infra", "sentry"],
        help="Show/configure only this camera type",
    )
    parser.add_argument(
        "--console", action="store_true",
        help="Run in console (terminal) mode instead of GUI",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to config.json (default: config.json next to script)",
    )

    args = parser.parse_args()

    if args.console:
        run_console_wizard(config_path=args.config, camera_type=args.type)
    elif can_use_gui():
        run_gui_wizard(config_path=args.config, camera_type=args.type)
    else:
        print("No display available — falling back to console wizard.")
        print("Use --console explicitly to suppress this message.\n")
        run_console_wizard(config_path=args.config, camera_type=args.type)


if __name__ == "__main__":
    main()
