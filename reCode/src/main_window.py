"""
Главное окно приложения для камеры SW1300.
"""

import os
import time
import numpy as np
import cv2

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap, QAction
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSlider, QSpinBox, QDoubleSpinBox, QPushButton,
    QStatusBar, QGroupBox, QFormLayout, QFileDialog,
    QMessageBox, QToolBar, QSizePolicy, QComboBox,
)

from camera import TanhoCamera, FRAME_WIDTH, FRAME_HEIGHT, ADC_MAX, ROI_MODES, DEFAULT_ROI
from capture_thread import CaptureThread


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SW1300 SWIR Camera — Tanho THCAMSW1300")
        self.setMinimumSize(900, 700)

        self.camera = TanhoCamera()
        self.capture_thread = None
        self.last_frame = None  # Последний захваченный кадр (uint16)

        self._build_ui()
        self._build_menu()
        self._build_statusbar()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)

        # === Область видео ===
        self.video_label = QLabel("Камера не подключена")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet(
            "QLabel { background-color: #1a1a2e; color: #888; "
            "font-size: 18px; border: 1px solid #333; }"
        )
        self.video_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.video_label.setMinimumSize(640, 512)
        layout.addWidget(self.video_label, stretch=1)

        # === Панель управления ===
        controls_layout = QHBoxLayout()
        layout.addLayout(controls_layout)

        # -- Кнопки подключения --
        btn_group = QGroupBox("Камера")
        btn_layout = QVBoxLayout(btn_group)

        self.btn_connect = QPushButton("Подключить")
        self.btn_connect.setStyleSheet(
            "QPushButton { padding: 8px 16px; font-weight: bold; }"
        )
        self.btn_connect.clicked.connect(self._on_connect)
        btn_layout.addWidget(self.btn_connect)

        self.btn_stop = QPushButton("Остановить")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        btn_layout.addWidget(self.btn_stop)

        controls_layout.addWidget(btn_group)

        # -- Экспозиция --
        exp_group = QGroupBox("Экспозиция (мкс)")
        exp_layout = QVBoxLayout(exp_group)

        self.exp_slider = QSlider(Qt.Orientation.Horizontal)
        self.exp_slider.setRange(15, 100000)
        self.exp_slider.setValue(1000)
        self.exp_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        exp_layout.addWidget(self.exp_slider)

        exp_value_layout = QHBoxLayout()
        self.exp_spinbox = QDoubleSpinBox()
        self.exp_spinbox.setRange(15, 60_000_000)
        self.exp_spinbox.setValue(1000)
        self.exp_spinbox.setSuffix(" мкс")
        self.exp_spinbox.setDecimals(1)
        exp_value_layout.addWidget(self.exp_spinbox)

        self.btn_set_exp = QPushButton("Установить")
        self.btn_set_exp.clicked.connect(self._on_set_exposure)
        exp_value_layout.addWidget(self.btn_set_exp)
        exp_layout.addLayout(exp_value_layout)

        self.exp_slider.valueChanged.connect(
            lambda v: self.exp_spinbox.setValue(v)
        )
        self.exp_spinbox.valueChanged.connect(
            lambda v: self.exp_slider.setValue(int(v))
            if v <= self.exp_slider.maximum() else None
        )

        controls_layout.addWidget(exp_group, stretch=1)

        # -- Усиление --
        gain_group = QGroupBox("Усиление")
        gain_layout = QVBoxLayout(gain_group)

        self.gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.gain_slider.setRange(0, 120)
        self.gain_slider.setValue(0)
        self.gain_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.gain_slider.setTickInterval(10)
        gain_layout.addWidget(self.gain_slider)

        gain_value_layout = QHBoxLayout()
        self.gain_spinbox = QSpinBox()
        self.gain_spinbox.setRange(0, 120)
        self.gain_spinbox.setValue(0)
        gain_value_layout.addWidget(self.gain_spinbox)

        self.btn_set_gain = QPushButton("Установить")
        self.btn_set_gain.clicked.connect(self._on_set_gain)
        gain_value_layout.addWidget(self.btn_set_gain)
        gain_layout.addLayout(gain_value_layout)

        self.gain_slider.valueChanged.connect(self.gain_spinbox.setValue)
        self.gain_spinbox.valueChanged.connect(self.gain_slider.setValue)

        controls_layout.addWidget(gain_group, stretch=1)

        # -- ROI --
        roi_group = QGroupBox("ROI")
        roi_layout = QVBoxLayout(roi_group)

        self.roi_combo = QComboBox()
        for mode_name in ROI_MODES:
            self.roi_combo.addItem(mode_name)
        self.roi_combo.setCurrentText(DEFAULT_ROI)
        roi_layout.addWidget(self.roi_combo)

        self.btn_set_roi = QPushButton("Установить")
        self.btn_set_roi.clicked.connect(self._on_set_roi)
        roi_layout.addWidget(self.btn_set_roi)

        controls_layout.addWidget(roi_group)

        # -- Сохранение --
        save_group = QGroupBox("Сохранение")
        save_layout = QVBoxLayout(save_group)

        self.btn_save_png = QPushButton("Сохранить PNG")
        self.btn_save_png.clicked.connect(self._on_save_png)
        self.btn_save_png.setEnabled(False)
        save_layout.addWidget(self.btn_save_png)

        self.btn_save_tiff = QPushButton("Сохранить TIFF (16-bit)")
        self.btn_save_tiff.clicked.connect(self._on_save_tiff)
        self.btn_save_tiff.setEnabled(False)
        save_layout.addWidget(self.btn_save_tiff)

        controls_layout.addWidget(save_group)

    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("Файл")
        save_action = QAction("Сохранить кадр...", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self._on_save_png)
        file_menu.addAction(save_action)

        exit_action = QAction("Выход", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        camera_menu = menubar.addMenu("Камера")
        connect_action = QAction("Подключить", self)
        connect_action.triggered.connect(self._on_connect)
        camera_menu.addAction(connect_action)

        stop_action = QAction("Остановить", self)
        stop_action.triggered.connect(self._on_stop)
        camera_menu.addAction(stop_action)

    def _build_statusbar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.fps_label = QLabel("FPS: —")
        self.fps_label.setStyleSheet("font-weight: bold; padding: 0 10px;")
        self.status_bar.addPermanentWidget(self.fps_label)

        w, h = ROI_MODES[DEFAULT_ROI]
        self.resolution_label = QLabel(f"Разрешение: {w}×{h}")
        self.status_bar.addPermanentWidget(self.resolution_label)

        self.status_bar.showMessage("Готово. Подключите камеру для начала работы.")

    # === Обработчики ===

    def _on_connect(self):
        try:
            self.status_bar.showMessage("Подключение камеры...")
            self.btn_connect.setEnabled(False)

            self.camera.connect()

            self.capture_thread = CaptureThread(self.camera)
            self.capture_thread.frame_ready.connect(self._on_frame_ready)
            self.capture_thread.fps_updated.connect(self._on_fps_updated)
            self.capture_thread.error_occurred.connect(self._on_error)
            self.capture_thread.roi_changed.connect(self._on_roi_changed)
            self.capture_thread.start()

            self.btn_stop.setEnabled(True)
            self.btn_save_png.setEnabled(True)
            self.btn_save_tiff.setEnabled(True)
            self.status_bar.showMessage("Камера подключена. Захват кадров...")

        except Exception as e:
            self.btn_connect.setEnabled(True)
            QMessageBox.critical(self, "Ошибка подключения", str(e))
            self.status_bar.showMessage(f"Ошибка: {e}")

    def _on_stop(self):
        if self.capture_thread and self.capture_thread.isRunning():
            self.capture_thread.stop()
            self.capture_thread = None

        self.camera.disconnect()

        self.btn_connect.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_save_png.setEnabled(False)
        self.btn_save_tiff.setEnabled(False)
        self.video_label.setText("Камера отключена")
        self.fps_label.setText("FPS: —")
        self.status_bar.showMessage("Камера отключена.")

    def _on_set_exposure(self):
        value = self.exp_spinbox.value()
        if self.capture_thread:
            self.capture_thread.request_exposure(value)
            self.status_bar.showMessage(f"Экспозиция: {value} мкс")

    def _on_set_gain(self):
        value = self.gain_spinbox.value()
        if self.capture_thread:
            self.capture_thread.request_gain(value)
            self.status_bar.showMessage(f"Усиление: {value}")

    def _on_set_roi(self):
        mode = self.roi_combo.currentText()
        w, h = ROI_MODES[mode]
        if self.capture_thread:
            self.capture_thread.request_roi(w, h)
            self.status_bar.showMessage(f"ROI: {w}×{h}")

    def _on_roi_changed(self, width: int, height: int):
        self.resolution_label.setText(f"Разрешение: {width}×{height}")

    def _on_frame_ready(self, frame_16: np.ndarray):
        """Обработка нового кадра."""
        self.last_frame = frame_16

        # Нормализация 16-bit → 8-bit по фиксированному диапазону ADC (0-4094)
        frame_8 = np.clip(frame_16 * (255.0 / ADC_MAX), 0, 255).astype(np.uint8)

        # Конвертация в QImage для отображения
        h, w = frame_8.shape
        qimage = QImage(
            frame_8.data, w, h, w,
            QImage.Format.Format_Grayscale8
        )

        # Масштабирование под размер виджета
        label_size = self.video_label.size()
        pixmap = QPixmap.fromImage(qimage).scaled(
            label_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.video_label.setPixmap(pixmap)

    def _on_fps_updated(self, fps: float):
        self.fps_label.setText(f"FPS: {fps:.1f}")

    def _on_error(self, message: str):
        self.status_bar.showMessage(f"Ошибка: {message}")

    def _on_save_png(self):
        if self.last_frame is None:
            QMessageBox.warning(self, "Нет кадра", "Нет захваченного кадра для сохранения.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить PNG", f"frame_{int(time.time())}.png",
            "PNG (*.png)"
        )
        if path:
            # Нормализация для PNG
            frame = self.last_frame
            fmin, fmax = frame.min(), frame.max()
            if fmax > fmin:
                img = ((frame - fmin) / (fmax - fmin) * 255).astype(np.uint8)
            else:
                img = np.zeros_like(frame, dtype=np.uint8)
            cv2.imwrite(path, img)
            self.status_bar.showMessage(f"Кадр сохранён: {path}")

    def _on_save_tiff(self):
        if self.last_frame is None:
            QMessageBox.warning(self, "Нет кадра", "Нет захваченного кадра для сохранения.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить TIFF (16-bit)", f"frame_{int(time.time())}.tiff",
            "TIFF (*.tiff *.tif)"
        )
        if path:
            cv2.imwrite(path, self.last_frame)
            self.status_bar.showMessage(f"Кадр 16-bit сохранён: {path}")

    def closeEvent(self, event):
        """Корректное завершение при закрытии окна."""
        if self.capture_thread and self.capture_thread.isRunning():
            self.capture_thread.stop()
        self.camera.disconnect()
        event.accept()
