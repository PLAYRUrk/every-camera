# Камера Infra

Камера Tanho THCAMSW1300 (сенсор Sony IMX990-AABA-C), SWIR-диапазон, 12-битный АЦП. Снимает на заданных секундах каждой минуты, пока текущее время попадает в интервал [файла расписания](Schedules.md#файл-интервалов-canon-и-infra).

```bash
python main.py --type infra
```

Отсчёты приводятся к общей [шкале 0–65535](Intensity-Scale.md) сразу после деинтерливинга.

## Подключение

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="aa55", ATTR{idProduct}=="8866", MODE="0666", GROUP="plugdev"' \
    | sudo tee /etc/udev/rules.d/99-sw1300-camera.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Библиотека `libTanhoAPI.so` лежит в `infra_lib/`. На Windows камера не поддерживается.

## Параметры

| Ключ | По умолчанию | Описание |
|---|---|---|
| `instance_name` | `""` | имя экземпляра; пусто — `Infra_<узел>` |
| `output_dir` | `""` | каталог для снимков |
| `schedule_file` | `""` | путь к файлу расписания |
| `capture_seconds` | `[0, 30]` | секунды каждой минуты для съёмки |
| `exposure_us` | `1000.0` | экспозиция, мкс |
| `gain` | `0` | усиление, 0–120 |
| `roi` | `1280x1024` | область захвата: полный кадр или полоса `1280x256` |
| `save_format` | `tiff` | `tiff` — 16 бит, `png` — 8 бит с нормализацией, `fits` — с метаданными |

## Live Preview в GUI

На вкладке **Infra Camera** есть встроенный поток для настройки резкости и параметров:

1. Нажмите **Connect**.
2. Нажмите **Live Preview**.
3. Настройте экспозицию, усиление и ROI. Изменения применяются немедленно.
4. Нажмите **Stop Preview**.

Live Preview и измерения по расписанию работают независимо.

## См. также

- [Расписания](Schedules.md)
- [Настройка резкости](Focus.md)
- [Режим предпросмотра](Preview.md)
