# Every Camera

Программа управления камерами Canon (gphoto2), SPTT (CSDU-429) и Infra (Tanho SW1300 SWIR).
Два режима: консольный (headless) и графический (PyQt5).
Мониторинг — отдельная программа.

## Структура проекта

```
main.py                — точка входа (измерения)
monitor_app.py         — точка входа (мониторинг, отдельная программа)
update.py              — утилита обновления программы
schedule_generator.py  — генератор расписания по углу Солнца
utils.py               — конфигурация, расписание, сеть, системная информация
mqtt_client.py         — MQTT публикация и подписка (консоль + GUI)
cannon_driver.py       — драйвер Canon: подключение, настройка, съёмка
sptt_driver.py         — драйвер SPTT/CSDU-429: USB, съёмка, FITS
sptt_load_firmware.py  — загрузчик прошивки CSDU-429 (FX2 + FPGA)
infra_driver.py        — драйвер Infra/SW1300: USB, съёмка, TIFF/PNG, live preview
gui_app.py             — графический интерфейс (вкладки Canon, SPTT, Infra, Monitor)
monitor.py             — виджет мониторинга (MQTT + локальные файлы)
generate_pdf.py        — генератор PDF-документации из README.md
firmware/              — бинарные файлы прошивки SPTT (fx2, fpga)
infra_lib/             — библиотека libTanhoAPI.so для камеры SW1300
config.json            — конфигурация (не перезаписывается при обновлении)
requirements.txt       — зависимости
cannon-camera/         — субмодуль (оригинальный репозиторий Canon)
SPTT-CAM/              — субмодуль (оригинальный репозиторий SPTT)
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
| Infra  | `numpy`, `opencv-python` или `Pillow`, `astropy` (для FITS) |
| Общие  | `paho-mqtt` (для MQTT) |
| GUI    | `PyQt5` |

Если используется только одна камера — устанавливать зависимости остальных не нужно.

## Быстрый старт

### Консольный режим (без дисплея)

```bash
# Canon камера
python main.py --type cannon

# SPTT камера
python main.py --type sptt

# Infra камера (SW1300 SWIR)
python main.py --type infra
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

# Только Infra
python main.py --gui --type infra
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
  "infra": {
    "instance_name": "",
    "output_dir": "/path/to/output",
    "schedule_file": "/path/to/schedule.txt",
    "capture_seconds": [0, 30],
    "exposure_us": 1000.0,
    "gain": 0,
    "roi": "1280x1024",
    "save_format": "tiff"
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

#### Infra (`infra`)
- **instance_name** — имя экземпляра (если пусто — `Infra_<IP>`)
- **output_dir** — директория для сохранения TIFF/PNG снимков
- **schedule_file** — путь к файлу расписания
- **capture_seconds** — секунды каждой минуты для съёмки (по умолчанию `[0, 30]`)
- **exposure_us** — экспозиция в микросекундах (по умолчанию `1000.0`)
- **gain** — усиление (0–120, по умолчанию `0`)
- **roi** — область захвата (`1280x1024` или `1280x256`)
- **save_format** — формат сохранения (`tiff` — 16-bit raw, `png` — 8-bit нормализованный, `fits` — FITS с метаданными)

Камера Tanho THCAMSW1300 (сенсор Sony IMX990-AABA-C), SWIR-диапазон, 12-bit ADC.

#### MQTT (`mqtt`)
- **enabled** — включить/выключить MQTT
- **host** — адрес брокера
- **port** — порт (обычно `1883` или `8883` с TLS)
- **user** — имя пользователя (опционально)
- **password** — пароль (хранится открытым текстом)
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

## Файл расписания (Canon и Infra)

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

## Режим реального времени (Infra)

В графическом режиме вкладка **Infra Camera** имеет режим **Live Preview** для настройки резкости и параметров камеры в реальном времени:

1. Нажмите **Connect** для подключения к камере
2. Нажмите **Live Preview** для запуска потока видео
3. Настройте **Exposure**, **Gain** и **ROI** — изменения применяются сразу
4. Нажмите **Stop Preview** когда настройка завершена

Режим Live Preview и режим измерений работают независимо: можно настроить камеру через превью, а затем запустить запись по расписанию.

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

### Просмотр последнего кадра

В MQTT-вкладке монитора есть кнопка **View Last Frame**. Для просмотра:

1. Подключитесь к MQTT брокеру
2. Выберите экземпляр камеры в таблице
3. Нажмите **View Last Frame**

Монитор отправит запрос камере через MQTT. Камера ответит последним снятым кадром (JPEG). Передача происходит только по запросу — трафик не расходуется без необходимости.

## Генератор расписания по углу Солнца

Утилита `schedule_generator.py` автоматически рассчитывает время восхода/захода Солнца через заданный угол высоты и создаёт файл расписания для камер Canon и Infra.

### Консольный режим

```bash
# Интерактивный режим (с выбором предустановленной точки наблюдения)
python schedule_generator.py

# С параметрами
python schedule_generator.py --lat 51.81 --lon 103.08 --alt 680 \
    --angle -10 --start 2026-01-01 --days 100 --output schedule.txt

# С предустановленной точкой
python schedule_generator.py --site GPHO_TK --angle -10 --start 2026-01-01 --days 100 -o schedule.txt
```

### Графический режим

```bash
python schedule_generator.py --gui
```

### Параметры

- **--lat** — широта (градусы)
- **--lon** — долгота (градусы)
- **--alt** — высота над уровнем моря (метры)
- **--angle** — пороговый угол высоты Солнца (градусы, например `-10`)
- **--start** — начальная дата (`YYYY-MM-DD`)
- **--days** — количество дней для расчёта
- **--output** / **-o** — путь к выходному файлу расписания
- **--site** — предустановленная точка наблюдения (`GPHO_TK`, `KLNG_1`, `MAGNITKA`, `SURA_ISZF`, `SURA_NNGU`, `SURA`, `PEREVOZ`)

### Предустановленные точки наблюдения

| Код | Название | Широта | Долгота | Высота |
|-----|----------|--------|---------|--------|
| GPHO_TK | GPHO Tory | 51.8106° | 103.0755° | 680 m |
| KLNG_1 | Kaliningrad | 54.6033° | 20.2111° | 30 m |
| MAGNITKA | Magnitka | 55.9305° | 48.7449° | 95 m |
| SURA_ISZF | SURA ISZF | 56.1471° | 46.0985° | 172 m |
| SURA_NNGU | SURA NNGU | 56.1502° | 46.1050° | 184 m |
| SURA | SURA | 56.1437° | 46.0990° | 173 m |
| PEREVOZ | Perevoz | 55.5426° | 44.5357° | 155 m |

Зависимости: `astropy` (входит в требования SPTT-камеры).

## Обновление

Утилита `update.py` обновляет программу до последней версии из git-репозитория.
Обновляются только скрипты (`.py`) и служебные файлы (`requirements.txt`, `firmware/`, `infra_lib/`).
Конфигурация (`config.json`, `camcfg_*.ini`) и расписания (`*schedule*.txt`) не затрагиваются.

```bash
# Обновить до последней версии
python update.py

# Посмотреть что изменится (без применения)
python update.py --dry-run

# Обновить, перезаписав даже локально изменённые файлы
python update.py --force
```

Утилита работает на любой версии программы — достаточно скопировать `update.py` и запустить.

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

### Infra (SW1300)
```bash
# Создать правило udev
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="aa55", ATTR{idProduct}=="8866", MODE="0666", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/99-sw1300-camera.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```
