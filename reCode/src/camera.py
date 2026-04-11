"""
Обёртка над libTanhoAPI.so для камеры SW1300 (THCAMSW1300).

Камера: Sony IMX990-AABA-C, 1280x1024, 12-bit ADC, 16-bit output.
USB: Cypress FX3, VID=0xaa55, PID=0x8866.

Формат данных:
  Камера отдаёт raw-буфер uint16, чересстрочный:
  чётные строки = левая половина (cols 0-639),
  нечётные строки = правая половина (cols 640-1279).

  ROI 1280×256:  raw 640×512,   655,360 байт
  ROI 1280×1024: raw 640×2048, 2,621,440 байт

  SDK (GetFrameData) захардкожен на копирование 655,360 байт,
  поэтому для полного разрешения читаем USB через libusb напрямую.
"""

import ctypes
import numpy as np
from pathlib import Path

# Ширина половины кадра (raw)
RAW_HALF_WIDTH = 640
BYTES_PER_PIXEL = 2  # 16-bit

# Поддерживаемые ROI (ширина, высота)
ROI_MODES = {
    "1280x256": (1280, 256),
    "1280x1024": (1280, 1024),
}
DEFAULT_ROI = "1280x1024"

# Деинтерлейсенные размеры (начальные — переопределяются при set_roi)
FRAME_WIDTH = 1280
FRAME_HEIGHT = 1024

# 12-bit ADC максимум
ADC_MAX = 4094

# Протокол команд
CMD_PACKET_SIZE = 32
CMD_FOOTER_POS = 31
CMD_FOOTER_VAL = 0x15

# Коды команд
CMD_EXPOSURE = 0xFF
CMD_EXPOSURE_ALT = 0xF5
CMD_GAIN = 0xFE
CMD_ROI = 0xD0

# USB-протокол (из дизассемблера GetFrameData)
USB_EP_IN = 0x81        # bulk IN endpoint
USB_CHUNK_SIZE = 0x4000 # 16KB на чанк
USB_TIMEOUT = 200       # мс на один bulk transfer
SYNC_MARKER = b'\x55\xFF\xAA\xCC'
SYNC_HEADER_SIZE = 0x200  # 512 байт заголовок после sync

# Максимум попыток захвата кадра при ошибке sync
MAX_GRAB_RETRIES = 3


def _find_library() -> str:
    """Найти libTanhoAPI.so относительно текущего модуля."""
    base = Path(__file__).resolve().parent.parent
    candidates = [
        base / "exampleCode" / "lib" / "libTanhoAPI.so.1.0.0",
        base / "exampleCode" / "lib" / "libTanhoAPI.so",
        base / "lib" / "libTanhoAPI.so",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise FileNotFoundError(
        f"libTanhoAPI.so не найдена. Проверьте наличие библиотеки в:\n"
        + "\n".join(f"  - {p}" for p in candidates)
    )


def _raw_size_for_roi(width: int, height: int) -> tuple:
    """Вычислить размеры raw-буфера для заданного ROI."""
    raw_w = width // 2   # 640
    raw_h = height * 2   # 512 или 2048
    return raw_w, raw_h


class TanhoCamera:
    """Обёртка над TanhoAPI SDK для камеры SW1300."""

    def __init__(self):
        self._lib = None
        self._libusb = None
        self._connected = False
        # Текущий ROI
        self._roi_width = 1280
        self._roi_height = 1024
        # USB буферы (создаются в _allocate_buffer)
        self._usb_buffer = None
        self._usb_chunk = None
        self._transferred = None
        self._num_chunks = 0
        self._usb_buf_size = 0
        self._frame_size = 0
        self._raw_w = 0
        self._raw_h = 0
        self._devh_ref = None

    def _load_library(self):
        """Загрузить SDK и libusb, настроить типы функций."""
        lib_path = _find_library()
        self._lib = ctypes.CDLL(lib_path)

        # --- SDK функции ---
        self._driver_init = self._lib._ZN8TanhoAPI19TanhoCam_DriverInitEj
        self._driver_init.argtypes = [ctypes.c_uint]
        self._driver_init.restype = ctypes.c_int

        self._open_driver = self._lib._ZN8TanhoAPI19TanhoCam_OpenDriverEv
        self._open_driver.argtypes = []
        self._open_driver.restype = ctypes.c_int

        self._close_driver = self._lib._ZN8TanhoAPI20TanhoCam_CloseDriverEv
        self._close_driver.argtypes = []
        self._close_driver.restype = ctypes.c_int

        self._execute_cmd = self._lib._ZN8TanhoAPI19TanhoCam_ExecuteCmdEPh
        self._execute_cmd.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        self._execute_cmd.restype = ctypes.c_int

        self._driver_start = self._lib._Z20TanhoCam_DriverStartv
        self._driver_start.argtypes = []
        self._driver_start.restype = ctypes.c_int

        self._driver_stop = self._lib._Z19TanhoCam_DriverStopv
        self._driver_stop.argtypes = []
        self._driver_stop.restype = ctypes.c_int

        # --- libusb (для прямого чтения кадров) ---
        self._libusb = ctypes.CDLL("libusb-1.0.so.0")
        self._bulk_transfer = self._libusb.libusb_bulk_transfer
        self._bulk_transfer.argtypes = [
            ctypes.c_void_p,                  # dev_handle
            ctypes.c_ubyte,                   # endpoint
            ctypes.POINTER(ctypes.c_ubyte),   # data
            ctypes.c_int,                     # length
            ctypes.POINTER(ctypes.c_int),     # transferred
            ctypes.c_uint,                    # timeout
        ]
        self._bulk_transfer.restype = ctypes.c_int

    def connect(self) -> bool:
        """Инициализировать и подключить камеру. Устанавливает ROI 1280×1024."""
        if self._connected:
            return True

        if self._lib is None:
            self._load_library()

        result = self._driver_init(1)
        if result != 0:
            raise RuntimeError(f"TanhoCam_DriverInit ошибка: {result}")

        result = self._open_driver()
        if not result:
            raise RuntimeError(
                "Не удалось открыть камеру. Проверьте:\n"
                "  1. Камера подключена через USB 3.0\n"
                "  2. Есть права доступа (udev-правило или sudo chmod 666 /dev/bus/usb/...)"
            )

        self._driver_start()

        # Кэшируем ссылку на devh один раз
        self._devh_ref = ctypes.c_void_p.in_dll(self._lib, 'devh')

        self._connected = True

        # Установить полное разрешение и выделить буферы
        self.set_roi(1280, 1024)

        # Прогреть: сбросить первые кадры (могут содержать мусор после инициализации)
        self._flush_usb()

        return True

    def disconnect(self):
        """Отключить камеру."""
        if self._connected and self._lib is not None:
            self._driver_stop()
            self._close_driver()
            self._connected = False
            self._usb_buffer = None
            self._usb_chunk = None
            self._devh_ref = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _allocate_buffer(self):
        """Выделить буферы под текущий ROI."""
        raw_w, raw_h = _raw_size_for_roi(self._roi_width, self._roi_height)
        self._raw_w = raw_w
        self._raw_h = raw_h
        self._frame_size = self._roi_width * self._roi_height * BYTES_PER_PIXEL

        # USB буфер: 3 кадра, чтобы гарантированно найти полный кадр после sync
        chunks_per_frame = (self._frame_size + USB_CHUNK_SIZE - 1) // USB_CHUNK_SIZE
        self._num_chunks = chunks_per_frame * 3
        self._usb_buf_size = self._num_chunks * USB_CHUNK_SIZE
        self._usb_buffer = (ctypes.c_ubyte * self._usb_buf_size)()
        self._usb_chunk = (ctypes.c_ubyte * USB_CHUNK_SIZE)()
        self._transferred = ctypes.c_int(0)

    def _flush_usb(self):
        """Сбросить застоявшиеся USB данные (несколько чанков с коротким таймаутом)."""
        if not self._devh_ref:
            return
        devh = self._devh_ref.value
        if not devh:
            return
        flush_chunk = (ctypes.c_ubyte * USB_CHUNK_SIZE)()
        transferred = ctypes.c_int(0)
        for _ in range(20):
            ret = self._bulk_transfer(
                devh, USB_EP_IN, flush_chunk, USB_CHUNK_SIZE,
                ctypes.byref(transferred), 10  # короткий таймаут
            )
            if ret != 0:
                break

    @property
    def roi_width(self) -> int:
        return self._roi_width

    @property
    def roi_height(self) -> int:
        return self._roi_height

    def _read_frame_usb(self) -> bytes:
        """
        Прочитать полный кадр через USB bulk transfer.

        1. Читаем все чанки разом (без поиска в процессе)
        2. Конвертируем буфер в bytes один раз
        3. Ищем sync-маркер 55 FF AA CC
        4. Копируем frame_size байт начиная с sync + 0x200
        """
        devh = self._devh_ref.value
        if not devh:
            raise RuntimeError("USB device handle не инициализирован")

        buf_addr = ctypes.addressof(self._usb_buffer)

        # 1. Читаем все чанки подряд — без промежуточных поисков
        for i in range(self._num_chunks):
            self._bulk_transfer(
                devh, USB_EP_IN, self._usb_chunk, USB_CHUNK_SIZE,
                ctypes.byref(self._transferred), USB_TIMEOUT
            )
            ctypes.memmove(
                buf_addr + i * USB_CHUNK_SIZE,
                self._usb_chunk,
                USB_CHUNK_SIZE
            )

        # 2. Одна конвертация, один поиск
        buf_bytes = ctypes.string_at(buf_addr, self._usb_buf_size)
        pos = buf_bytes.find(SYNC_MARKER)
        if pos < 0:
            return None  # вызывающий сделает retry

        data_start = pos + SYNC_HEADER_SIZE
        data_end = data_start + self._frame_size
        if data_end > self._usb_buf_size:
            return None  # неполный кадр, retry

        return buf_bytes[data_start:data_end]

    def grab_frame(self) -> np.ndarray:
        """
        Захватить один кадр и деинтерлейсить.

        Возвращает numpy uint16 (roi_height, roi_width).
        При неудаче повторяет до MAX_GRAB_RETRIES раз.
        """
        if not self._connected:
            raise RuntimeError("Камера не подключена")

        for attempt in range(MAX_GRAB_RETRIES):
            frame_bytes = self._read_frame_usb()
            if frame_bytes is not None:
                break
        else:
            raise RuntimeError(
                f"Не удалось захватить кадр после {MAX_GRAB_RETRIES} попыток "
                "(sync marker не найден)"
            )

        raw_16 = np.frombuffer(frame_bytes, dtype=np.uint16).reshape(
            self._raw_h, self._raw_w
        )

        # Деинтерлейсинг: чётные строки -> левая половина, нечётные -> правая
        frame = np.empty((self._roi_height, self._roi_width), dtype=np.uint16)
        frame[:, :self._raw_w] = raw_16[0::2]
        frame[:, self._raw_w:] = raw_16[1::2]

        return frame

    def grab_frame_raw(self) -> np.ndarray:
        """Захватить raw кадр без деинтерлейсинга."""
        if not self._connected:
            raise RuntimeError("Камера не подключена")

        for attempt in range(MAX_GRAB_RETRIES):
            frame_bytes = self._read_frame_usb()
            if frame_bytes is not None:
                break
        else:
            raise RuntimeError("Не удалось захватить raw кадр")

        return np.frombuffer(frame_bytes, dtype=np.uint16).reshape(
            self._raw_h, self._raw_w
        ).copy()

    def set_exposure(self, microseconds: float):
        """Установить экспозицию (мкс)."""
        if not self._connected:
            return
        ticks = int(microseconds * 20)
        b = ticks.to_bytes(4, 'little')

        cmd1 = self._make_cmd_packet(CMD_EXPOSURE)
        cmd1[4] = b[1]; cmd1[5] = b[0]
        cmd1[6] = b[3]; cmd1[7] = b[2]
        self._execute_raw_cmd(cmd1)

        cmd2 = self._make_cmd_packet(CMD_EXPOSURE_ALT)
        cmd2[4] = b[1]; cmd2[5] = b[0]
        cmd2[6] = b[3]; cmd2[7] = b[2]
        self._execute_raw_cmd(cmd2)

    def set_gain(self, gain: int):
        """Установить усиление (0-120)."""
        if not self._connected:
            return
        cmd = self._make_cmd_packet(CMD_GAIN)
        cmd[4] = 0x00
        cmd[5] = max(1, gain) & 0xFF
        self._execute_raw_cmd(cmd)

    def set_roi(self, width: int = 1280, height: int = 1024):
        """
        Установить ROI.

        Из Wireshark:
          1280×1024: 00 06 00 D0 00 00 A0 80 ... 15
          byte 6 = width/8, byte 7 = height/8
        """
        if not self._connected:
            return

        cmd = self._make_cmd_packet(CMD_ROI)
        cmd[4] = 0x00
        cmd[5] = 0x00
        cmd[6] = (width // 8) & 0xFF
        cmd[7] = (height // 8) & 0xFF
        self._execute_raw_cmd(cmd)

        self._roi_width = width
        self._roi_height = height
        self._allocate_buffer()

    def execute_raw_cmd(self, data: bytes):
        """Отправить произвольную 32-байт команду."""
        if not self._connected:
            return
        self._execute_raw_cmd(data)

    def _execute_raw_cmd(self, data):
        buf = (ctypes.c_ubyte * CMD_PACKET_SIZE)(*data[:CMD_PACKET_SIZE])
        self._execute_cmd(buf)

    @staticmethod
    def _make_cmd_packet(cmd_code: int) -> bytearray:
        """Создать 32-байтный пакет команды."""
        packet = bytearray(CMD_PACKET_SIZE)
        packet[0] = 0x00
        packet[1] = 0x06
        packet[2] = 0x00
        packet[3] = cmd_code
        packet[CMD_FOOTER_POS] = CMD_FOOTER_VAL
        return packet

    def __del__(self):
        self.disconnect()
