#!/usr/bin/env python3
"""
SW1300 SWIR Camera Viewer
Камера Tanho THCAMSW1300 (Sony IMX990-AABA-C)

Использование:
    python main.py

Требования:
    pip install PyQt6 numpy opencv-python
    Камера SW1300 подключена через USB 3.0
    sudo доступ или udev-правило для libusb
"""

import sys
import os

# Добавить директорию с библиотекой в LD_LIBRARY_PATH
lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "exampleCode", "lib")
if os.path.isdir(lib_dir):
    ld_path = os.environ.get("LD_LIBRARY_PATH", "")
    if lib_dir not in ld_path:
        os.environ["LD_LIBRARY_PATH"] = f"{lib_dir}:{ld_path}"

from PyQt6.QtWidgets import QApplication
from main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SW1300 Camera")
    app.setOrganizationName("InfraCamera")

    # Тёмная тема
    app.setStyleSheet("""
        QMainWindow { background-color: #1e1e2e; }
        QWidget { background-color: #1e1e2e; color: #cdd6f4; }
        QGroupBox {
            border: 1px solid #45475a;
            border-radius: 4px;
            margin-top: 8px;
            padding-top: 12px;
            font-weight: bold;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 8px;
            padding: 0 4px;
        }
        QPushButton {
            background-color: #45475a;
            border: 1px solid #585b70;
            border-radius: 4px;
            padding: 6px 12px;
            color: #cdd6f4;
        }
        QPushButton:hover { background-color: #585b70; }
        QPushButton:pressed { background-color: #313244; }
        QPushButton:disabled { color: #6c7086; }
        QSlider::groove:horizontal {
            height: 6px;
            background: #45475a;
            border-radius: 3px;
        }
        QSlider::handle:horizontal {
            background: #89b4fa;
            width: 16px;
            margin: -5px 0;
            border-radius: 8px;
        }
        QSpinBox, QDoubleSpinBox {
            background-color: #313244;
            border: 1px solid #45475a;
            border-radius: 4px;
            padding: 4px;
            color: #cdd6f4;
        }
        QStatusBar { background-color: #181825; color: #a6adc8; }
        QMenuBar { background-color: #181825; color: #cdd6f4; }
        QMenuBar::item:selected { background-color: #45475a; }
        QMenu { background-color: #313244; color: #cdd6f4; }
        QMenu::item:selected { background-color: #45475a; }
    """)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
