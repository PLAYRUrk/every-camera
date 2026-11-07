# Установка

Нужен Python 3.11. Станции работают на Linux (в том числе CentOS 7.8) и Windows.

```bash
git clone https://github.com/PLAYRUrk/every-camera.git
cd every-camera
pip install -r requirements-<роль>.txt
```

## Файлы зависимостей по ролям

Ставится файл своей роли. Общее ядро (`requirements.txt`: `numpy`, `Pillow`) он подключает сам. Узлу с одной камерой зависимости остальных камер не нужны, а безголовому узлу не нужен `PyQt5`.

| Файл | Для чего | Что добавляет к ядру |
|---|---|---|
| `requirements-cannon.txt` | узел с камерой Canon | `gphoto2-cffi` |
| `requirements-sptt.txt` | узел с камерой SPTT | `pyusb`, `astropy` |
| `requirements-infra.txt` | узел с камерой Infra | `opencv-python-headless` (необязательно), `astropy` |
| `requirements-asi.txt` | узел с камерой ASI | `pyserial`, `astropy` |
| `requirements-japan.txt` | узел с камерой Japan | `pyserial`, `astropy`; нужен DCAM-API SDK |
| `requirements-sentry.txt` | узел с камерой Sentry | `astropy`; нужен установленный `imagerd_rt` |
| `requirements-gui.txt` | дополнительно для GUI (`gui_app.py`, `setup_app.py`) | `PyQt5`, `PyQt5-Qt5`, `PyQt5-sip` |
| `requirements-observer.txt` | машина наблюдателя | ядро и GUI |

Например, узел с камерой ASI и графической оболочкой:

```bash
pip install -r requirements-asi.txt -r requirements-gui.txt
```

## Что ещё нужно камере

Права на устройства и внешние SDK описаны на странице камеры:

| Камера | Что настроить |
|---|---|
| [Canon](Camera-Canon.md#подключение) | обычно ничего |
| [SPTT](Camera-SPTT.md#подключение) | правило udev |
| [Infra](Camera-Infra.md#подключение) | правило udev |
| [Sentry](Camera-Sentry.md#подключение) | установленный `imagerd_rt` |
| [ASI](Camera-ASI.md#подключение) | PICAM SDK, доступ к последовательному порту |
| [Japan](Camera-Japan.md#подключение) | DCAM-API SDK, доступ к последовательному порту |

## Машина наблюдателя (Windows, Linux)

`viewer_app.py` и `focus_app.py` не импортируют драйверы камер: им не нужны gphoto2, pyusb и `libTanhoAPI.so`. Достаточно скопировать репозиторий и поставить:

```bash
pip install -r requirements-observer.txt
pip install astropy       # необязательно: без него FITS читается встроенным парсером
```

Обнаружение, шлюз, просмотр кадров и почтовые оповещения работают на стандартной библиотеке.

## См. также

- [CentOS 7](Install-CentOS.md)
- [Windows](Install-Windows.md)
- [Быстрый старт](Quick-Start.md)
