# Общее у ASI и Japan

Программа ASI написана на основе программы Hamamatsu. Совпадающий код лежит в `cameras/common/` в одном экземпляре.

| Модуль | Что это |
|---|---|
| `filterwheel.py`, `filterwheel_sim.py` | контроллер Animatics SmartMotor: `GOSUB5` — домой, `g=<n>` — переход, `GOSUB4` — опрос позиции, `d=` + `GOSUB6` — затвор |
| `schedule.py` | слоты, тайминг цикла, оценка длительности тёмных кадров, разбор `schedule.txt` |
| `sun.py`, `timeutil.py` | высота Солнца и единое определение UTC для имён, шапок и расчётов |
| `cfgparse.py` | чтение значений из `config.json` без исключений |
| `fits.py` | 18 общих карт, 16 карт `imagerd_rt` и запись файла; карты об инструменте можно опустить (`instrument=False`) |
| `archive_paths.py`, `seqno.py` | дерево UTC `YYYY/MM/DD`, имена `imagerd_rt`, счётчик `SEQNO`: ASI всегда, Japan в `sun_cycle` |
| `filters.py` | таблица колеса: позиция, тег длины волны, описание |
| `exposure.py` | арифметика управления интенсивностью, без камеры, часов и конфига |
| `exposure_config.py` | секции `preflight` и `overexposure`: значения по умолчанию и проверка. Сообщения начинаются с `asi.` или `japan.` |

`schedule.py` намеренно содержит объединение ключей обеих камер: `gain` в слоте относится только к ASI. Что принимает камера, решает её `config.py`, поэтому конфиг, скопированный от ASI, вызывает понятное сообщение.

Режимы у камер одинаковые, `sun_cycle` у обеих привязан к `t_start` в UTC (`schedule.utc_cycle_anchor`).

В `cameras/asi/` остались однострочные реэкспорты под старыми именами, чтобы работали импорты вроде `from cameras.asi import schedule`. Пакет `cameras/japan/` импортирует из `cameras/common/` напрямую.

## Чем Japan отличается от ASI

| | ASI | Japan |
|---|---|---|
| Камера | Princeton PIXIS через PICAM | Hamamatsu через DCAM-API |
| Режимы | `sun`, `time`, `sun_cycle` | `sun`, `time`, `sun_cycle` |
| Охлаждение | уставка, ожидание, отогрев на выходе | выключатель кулера `cooling`, по умолчанию не используется |
| Усиление | `gain` 1–3 | нет |
| `preflight` | в `sun_cycle` | в `sun_cycle` |
| `overexposure` | в `time` и `sun_cycle` | только в `sun_cycle` |
| Биннинг | 1–4 | 1–8 |
| Скорость считывания | МГц (`2.0`, `0.1`) | настройка `1` или `2` |
| Имена кадров | дерево `YYYY/MM/DD`, имена `imagerd_rt` | `sun`, `time`: плоско, `YYYYmmddTHHMMSS_<фильтр>[_bg].fits`; `sun_cycle`: как у ASI |
| Шапка FITS | общие карты, карты PIXIS, `SKYMEAN`, `SPLIT*`, 16 карт `imagerd_rt` | `sun`, `time`: общие карты; `sun_cycle`: набор ASI без карт об инструменте, плюс `NAME` |
| Счётчик `SEQNO` | есть | только в `sun_cycle` |
| Windows | нет: в `picam.py` нет кандидата `Picam.dll` | да, целиком |

## Почему разбиение слота у Japan только в sun_cycle

Режим `time` этой камеры — программа из `japan-camera`, и архив станции, которая её ведёт, состоит из одного кадра на слот. Добавить туда подкадры значило бы изменить смысл накопленных данных. В `sun_cycle` такой истории нет.

## См. также

- [Камера ASI](Camera-ASI.md)
- [Камера Japan](Camera-Japan.md)
- [Структура проекта](Project-Structure.md)
