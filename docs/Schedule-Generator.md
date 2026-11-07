# Генератор расписания

`schedule_generator.py` рассчитывает время захода и восхода Солнца через заданный угол высоты и создаёт [файл интервалов](Schedules.md#файл-интервалов-canon-и-infra) для камер Canon и Infra.

Нужен `astropy`, он входит в зависимости SPTT, Infra, ASI, Japan и Sentry.

## Использование

```bash
# интерактивный режим с выбором точки наблюдения
python schedule_generator.py

# все параметры в командной строке
python schedule_generator.py --lat 51.81 --lon 103.08 --alt 680 \
    --angle -10 --start 2026-01-01 --days 100 --output schedule.txt

# предустановленная точка
python schedule_generator.py --site GPHO_TK --angle -10 --start 2026-01-01 --days 100 -o schedule.txt

# графический режим
python schedule_generator.py --gui
```

Время в создаваемом файле — UTC; камеры Canon и Infra читают его как UTC.

## Параметры

| Параметр | Описание |
|---|---|
| `--lat` | широта, градусы |
| `--lon` | долгота, градусы |
| `--alt` | высота над уровнем моря, м |
| `--angle` | пороговый угол высоты Солнца, градусы, например `-10` |
| `--start` | начальная дата, `YYYY-MM-DD` |
| `--days` | количество дней |
| `--output`, `-o` | выходной файл |
| `--site` | предустановленная точка |
| `--gui` | графический режим |

## Предустановленные точки

| Код | Название | Широта | Долгота | Высота |
|---|---|---|---|---|
| `GPHO_TK` | GPHO Tory | 51.8106° | 103.0755° | 680 м |
| `KLNG_1` | Kaliningrad | 54.6033° | 20.2111° | 30 м |
| `MAGNITKA` | Magnitka | 55.9305° | 48.7449° | 95 м |
| `SURA_ISZF` | SURA ISZF | 56.1471° | 46.0985° | 172 м |
| `SURA_NNGU` | SURA NNGU | 56.1502° | 46.1050° | 184 м |
| `SURA` | SURA | 56.1437° | 46.0990° | 173 м |
| `PEREVOZ` | Perevoz | 55.5426° | 44.5357° | 155 м |

## См. также

- [Расписания](Schedules.md)
- [Солнце и IERS](Sun-And-IERS.md)
