#!/usr/bin/env python3
"""
Every Camera — Monitor (standalone).

Usage:
    python monitor_app.py                  # GUI monitor with MQTT and local tabs
    python monitor_app.py --config path    # Use custom config file
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="Every Camera — Monitor")
    parser.add_argument("--config", default=None, help="Path to config.json")
    args = parser.parse_args()

    from utils import load_config, can_use_gui

    if not can_use_gui():
        print("Error: No display available. Monitor requires a graphical environment.")
        sys.exit(1)

    cfg = load_config(args.config)

    # Fix Qt platform plugin conflict with OpenCV
    try:
        import PyQt5 as _pyqt5
        _qt_plugins = os.path.join(os.path.dirname(_pyqt5.__file__), "Qt5", "plugins")
        if os.path.isdir(_qt_plugins):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = _qt_plugins
    except Exception:
        os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)

    from PyQt5.QtWidgets import QApplication, QMainWindow
    from monitor import MonitorWidget

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    win = QMainWindow()
    win.setWindowTitle("Every Camera — Monitor")
    win.resize(1100, 500)
    win.setCentralWidget(MonitorWidget(cfg.get("mqtt", {})))
    win.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
