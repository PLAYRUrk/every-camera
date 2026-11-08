# Структура проекта

## Точки входа

| Файл | Назначение |
|---|---|
| `main.py` | измерения, консоль и GUI |
| `monitor_app.py` | монитор камер в сети |
| `viewer_app.py` | просмотрщик кадров |
| `focus_app.py` | настройка резкости |
| `setup_app.py` | мастер настройки |
| `sentinel.py` | сторож: письма и контроль процессов |
| `schedule_generator.py` | генератор расписания по углу Солнца |
| `update.py` | обновление программы |
| `discovery.py` | также CLI поиска камер |
| `frame_server.py` | также запуск сервера над архивом |
| `run.sh`, `run.cmd` | запуск и машинное окружение |

## Общие модули

| Файл | Назначение |
|---|---|
| `utils.py` | `DEFAULT_CONFIG`, чтение и сохранение конфига, расписание, сеть, системная информация |
| `console_ui.py` | живой экран и лог |
| `telemetry.py` | автоматическая запись [телеметрии](Telemetry.md) в CSV |
| `filterwheel_bench.py` | [проверка колеса и Omega на столе](Camera-ASI.md#проверка-колеса-на-столе) |
| `worker_common.py` | обвязка воркеров: статусы, публикация кадров |
| `camera_service.py` | потокобезопасная прослойка между воркером и сетью, `PARAM_SCHEMAS` |
| `frame_server.py` | HTTP-сервер кадров |
| `tunnel.py` | [туннель Cloudflare](Tunnel.md): поиск и скачивание `cloudflared`, слежение за процессом |
| `frame_archive.py` | чтение архива, JPEG, метрика резкости |
| `frame_grouping.py` | группировка кадров по циклам и фильтрам |
| `discovery.py` | обнаружение камер по UDP |
| `peers.py` | реестр увиденных узлов |
| `gateway.py` | `/api/nodes` и проксирование к соседям |
| `net_client.py` | HTTP-клиент и Qt-помощники программ наблюдателя |
| `alerts.py` | какие строки лога стоят письма |
| `mailer.py` | отправка почты и очередь на диске |
| `intensity.py` | приведение к шкале 0–65535 |
| `cycles.py` | разностный кадр по фильтру: выбор трёх кадров и арифметика |
| `qt_support.py` | хук исключений Qt-программ (`gui_app`, `setup_app`, `viewer_app`, `focus_app`, `monitor_app`): неожиданная ошибка показывается в окне и печатается, программа не завершается |
| `gui_app.py` | графический интерфейс |
| `monitor.py` | виджет статусов камер этой машины |

## Пакет cameras

```
cameras/
  cannon_driver.py      Canon: gphoto2
  sptt_driver.py        CSDU-429/423/285C: USB, съёмка, FITS, дебайер
  sptt_load_firmware.py модели CSDU, загрузчик прошивки (FX2 + FPGA)
  sptt_spt.py           чтение вендорских скриптов .spt
  sptt_timing.py        экспозиция и период кадра, без pyusb
  infra_driver.py       Tanho SW1300: USB, съёмка, TIFF/PNG/FITS
  sentry_driver.py      супервизор imagerd_rt
  asi_driver.py         ASI: цикл измерений, тёмные кадры, расписание
  japan_driver.py       Japan: режимы sun, time, sun_cycle
  common/               общее для asi и japan
  asi/                  оборудование ASI
    picam.py            ctypes-биндинг PICAM SDK
    camera.py           PixisCamera
    camera_sim.py       симулятор
    devices.py          выбор бэкенда
    config.py           секция asi в виде объектов
    fits.py             запись FITS
    filterwheel.py, filterwheel_sim.py, omega.py, schedule.py, sun.py, timeutil.py,
    paths.py, seqno.py, exposure.py   реэкспорт из common/
  japan/                оборудование Japan
    dcamsdk/            обёртки DCAM-API и ленивая загрузка библиотеки
      vendor/           dcam.py и dcamapi4.py Hamamatsu
    camera.py           HamamatsuCamera
    camera_sim.py       симулятор
    devices.py          выбор бэкенда
    config.py           секция japan в виде объектов
    fits.py             запись FITS
    paths.py            плоские имена sun и time
```

Состав `common/` описан на странице [Общее у ASI и Japan](ASI-Japan-Shared.md).

Файлы `cannon_driver.py`, `sptt_driver.py`, `infra_driver.py`, `sentry_driver.py`, `asi_driver.py`, `japan_driver.py`, `sptt_load_firmware.py` в корне — шимы для совместимости, логика в `cameras/`.

## Данные и окружение

| Путь | Назначение |
|---|---|
| `config.json` | [настройки станции](Config.md) |
| `env.sh.example`, `env.cmd.example` | образцы машинного окружения |
| `mail_account.json.example` | образец учётной записи отправителя |
| `requirements*.txt` | зависимости по ролям |
| `schedules/` | примеры расписаний |
| `firmware/` | прошивки SPTT |
| `infra_lib/` | `libTanhoAPI.so` |
| `systemd/` | юниты, `install.sh`, правило udev |
| `docs/` | эта документация |

## Правила границ

- HTTP-обработчики вызывают только `CameraService` или диск, но не драйвер.
- В драйверах ASI и Japan любое ожидание идёт через `_service_tick`.
- Плановая съёмка важнее фокуса.
- `viewer_app.py` и `focus_app.py` импортируются без gphoto2, pyusb и `libTanhoAPI`.
- Код на пути станции работает и на Windows: без `os.statvfs`, `fcntl`, `/proc`.

## См. также

- [Разработка](Development.md)
- [Общее у ASI и Japan](ASI-Japan-Shared.md)
