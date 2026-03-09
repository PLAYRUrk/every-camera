# Every Camera

Программа управления камерами Canon (gphoto2) и SPTT (CSDU-429).
Два режима: консольный (headless) и графический (PyQt5).
Мониторинг — отдельная программа.

## Структура проекта

```
main.py           — точка входа (измерения)
monitor_app.py    — точка входа (мониторинг, отдельная программа)
utils.py          — конфигурация, расписание, сеть, системная информация
mqtt_client.py    — MQTT публикация и подписка (консоль + GUI)
cannon_driver.py  — драйвер Canon: подключение, настройка, съёмка
sptt_driver.py    — драйвер SPTT/CSDU-429: USB, прошивка, FITS
gui_app.py        — графический интерфейс (вкладки Canon, SPTT, Monitor)
monitor.py        — виджет мониторинга (MQTT + локальные файлы)
config.json       — конфигурация
requirements.txt  — зависимости
cannon-camera/    — субмодуль (оригинальный код Canon)
SPTT-CAM/         — субмодуль (прошивки и оригинальный код SPTT)
```

## Установка

```bash
git clone --recurse-submodules <url>
cd every-camera
pip install -r requirements.txt
```

### Зависимости по камерам

| Камера | Обязательные пакеты |
|--------|-------------------|
| Canon  | `gphoto2-cffi`, `Pillow`, `numpy` |
| SPTT   | `pyusb`, `astropy`, `numpy` |
| Общие  | `paho-mqtt` (для MQTT) |
| GUI    | `PyQt5` |

Если используется только одна камера — устанавливать зависимости второй не нужно.

## Быстрый старт

### Консольный режим (без дисплея)

```bash
# Canon камера
python main.py --type cannon

# SPTT камера
python main.py --type sptt
```

При первом запуске, если конфигурация не заполнена, программа предложит **интерактивную настройку** прямо в терминале.

### Графический режим

```bash
# Автоматически (если есть дисплей)
python main.py

# Явно
python main.py --gui

# Только Canon
python main.py --gui --type cannon

# Только SPTT
python main.py --gui --type sptt
```

### Мониторинг (отдельная программа)

```bash
python monitor_app.py
```

## Конфигурация

Файл `config.json` в корне проекта. Создаётся автоматически с значениями по умолчанию.

```json
{
  "cannon": {
    "instance_name": "",
    "output_dir": "/path/to/output",
    "schedule_file": "/path/to/schedule.txt",
    "capture_seconds": [0, 30],
    "camcfg_file": ""
  },
  "sptt": {
    "instance_name": "",
    "output_dir": "/path/to/output",
    "exposure": 0.88,
    "gain": 100,
    "binning": 0,
    "encoding": 1,
    "target_temp": null,
    "firmware_dir": ""
  },
  "mqtt": {
    "enabled": false,
    "host": "broker.hivemq.com",
    "port": 1883,
    "user": "",
    "password": "",
    "prefix": "every_camera",
    "tls": false
  },
  "status_dir": ""
}
```

### Параметры

#### Canon (`cannon`)
- **instance_name** — имя экземпляра (если пусто — генерируется автоматически: `Cannon_<IP>`)
- **output_dir** — директория для сохранения JPEG снимков
- **schedule_file** — путь к файлу расписания
- **capture_seconds** — секунды каждой минуты для съёмки (по умолчанию `[0, 30]`)
- **camcfg_file** — путь к .ini файлу настроек камеры (генерируется автоматически)

#### SPTT (`sptt`)
- **instance_name** — имя экземпляра (если пусто — `SPTT_<IP>`)
- **output_dir** — директория для сохранения FITS файлов
- **exposure** — экспозиция в секундах (по умолчанию `0.88`)
- **gain** — усиление (0–1023, по умолчанию `100`)
- **binning** — биннинг (`0`=1x1, `1`=2x2, `3`=4x4)
- **encoding** — кодирование (`0`=8 бит, `1`=12 бит)
- **target_temp** — целевая температура ПЗС (`null` = не задана)

SPTT всегда снимает на **:00 и :30 каждой минуты** (без файла расписания).

#### MQTT (`mqtt`)
- **enabled** — включить/выключить MQTT
- **host** — адрес брокера
- **port** — порт (обычно `1883` или `8883` с TLS)
- **user** / **password** — логин и пароль (хранятся открытым текстом)
- **prefix** — префикс топиков (по умолчанию `every_camera`)
- **tls** — использовать TLS-соединение

Топик статуса: `{prefix}/{instance_name}/status`

#### Общие
- **status_dir** — директория для файлов статуса (по умолчанию `~/.every_camera/status`)

### Интерактивная настройка

При запуске в консольном режиме без заполненной конфигурации программа запустит мастер настройки:

```
$ python main.py --type cannon

--- Canon Camera Configuration ---

Output directory for images []: /home/user/photos
Schedule file path []: /home/user/schedule.txt
Instance name (auto if empty) []:
Capture seconds (comma-separated) [0, 30]:
Configure MQTT? [y/N]: y
MQTT broker host [broker.hivemq.com]: mybroker.com
MQTT port [1883]: 8883
MQTT username (optional) []:
MQTT password (optional) []:
MQTT topic prefix [every_camera]:
Use TLS? [y/N]: y

Configuration saved.
```

Можно также указать другой файл конфигурации: `python main.py --type cannon --config /path/to/config.json`

## Файл расписания (только Canon)

Формат: одна строка = один интервал измерений.

```
# Measurement Schedule
# Format: %Y-%m-%d %H:%M:%S - %Y-%m-%d %H:%M:%S

2026-03-08 12:00:00 - 2026-03-08 23:59:00
2026-03-09 08:00:00 - 2026-03-09 18:00:00
```

- Строки с `#` — комментарии
- Камера снимает только когда текущее время попадает в один из интервалов
- Вне интервалов программа ждёт (статус `waiting`)

## Мониторинг

Программа `monitor_app.py` показывает состояние всех запущенных камер:

- **Вкладка MQTT** — подключается к MQTT брокеру и отображает статусы в реальном времени
- **Вкладка Local files** — читает JSON-файлы статуса из директории `~/.every_camera/status`

Таблица показывает: имя экземпляра, тип камеры, PID, статус, количество снимков, последний снимок, ошибки, доп. информацию (ISO, экспозиция, температура, свободное место на диске).

Статусы:
- **RUNNING** (зелёный) — камера активно снимает
- **WAITING** (оранжевый) — ожидание расписания
- **ERROR** (красный) — ошибка при съёмке
- **STOPPED** (серый) — остановлена
- **STALE** (оранжевый) — нет обновлений более 30 секунд

## Остановка

- Консольный режим: `Ctrl+C`
- GUI: закрыть окно (конфигурация сохранится автоматически)
- Программа корректно завершает все потоки и отключается от MQTT

## USB-права (Linux)

Для работы с камерами без root:

### Canon (gphoto2)
```bash
# Обычно работает из коробки. Если нет — отключить gvfs:
sudo pkill gvfsd-gphoto2
```

### SPTT (CSDU-429)
```bash
# Создать правило udev
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="04b4", MODE="0666"' | sudo tee /etc/udev/rules.d/99-sptt.rules
sudo udevadm control --reload-rules
```
