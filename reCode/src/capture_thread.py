"""
Поток захвата кадров с камеры SW1300.
"""

import time
import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal, QMutex


class CaptureThread(QThread):
    """Поток непрерывного захвата кадров с камеры."""

    frame_ready = pyqtSignal(np.ndarray)  # 16-bit numpy array
    fps_updated = pyqtSignal(float)
    error_occurred = pyqtSignal(str)
    roi_changed = pyqtSignal(int, int)  # width, height

    def __init__(self, camera, parent=None):
        super().__init__(parent)
        self.camera = camera
        self._running = False
        self._mutex = QMutex()

        # Очередь команд
        self._pending_exposure = None
        self._pending_gain = None
        self._pending_roi = None  # (width, height)

    def run(self):
        self._running = True
        frame_count = 0
        fps_timer = time.monotonic()
        consecutive_errors = 0

        while self._running:
            try:
                self._process_pending_commands()

                frame = self.camera.grab_frame()
                self.frame_ready.emit(frame)
                consecutive_errors = 0

                # FPS
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
                    self.error_occurred.emit(
                        "10 ошибок подряд, захват остановлен"
                    )
                    break

    def stop(self):
        self._running = False
        self.wait(5000)

    def request_exposure(self, microseconds: float):
        self._mutex.lock()
        self._pending_exposure = microseconds
        self._mutex.unlock()

    def request_gain(self, gain: int):
        self._mutex.lock()
        self._pending_gain = gain
        self._mutex.unlock()

    def request_roi(self, width: int, height: int):
        self._mutex.lock()
        self._pending_roi = (width, height)
        self._mutex.unlock()

    def _process_pending_commands(self):
        self._mutex.lock()
        exposure = self._pending_exposure
        gain = self._pending_gain
        roi = self._pending_roi
        self._pending_exposure = None
        self._pending_gain = None
        self._pending_roi = None
        self._mutex.unlock()

        if roi is not None:
            self.camera.set_roi(*roi)
            self.roi_changed.emit(*roi)
        if exposure is not None:
            self.camera.set_exposure(exposure)
        if gain is not None:
            self.camera.set_gain(gain)
