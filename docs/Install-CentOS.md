# Установка на CentOS 7

Инструкция для CentOS 7.8 и Python 3.11.7.

## Почему у версий есть верхние границы

У каждой строки в `requirements-*.txt` указан диапазон версий. На CentOS 7 стоит glibc 2.17, а часть свежих пакетов публикует колёса только под glibc 2.28. Собрать их из исходников системным gcc 4.8 нельзя. Диапазоны подобраны так, чтобы pip выбирал последнюю версию с колесом `manylinux2014_x86_64`.

## Проверка до установки

Убедитесь, что всё поставится колёсами:

```bash
python3.11 -m pip download -r requirements-asi.txt -d /tmp/wheels --only-binary=:all:
```

Если пакет не скачался, опустите верхнюю границу в его строке. Если колесом ставится более новая версия, границу можно поднять. Комментарий у строки поправьте вместе с ней.

## gphoto2-cffi

Колёс у него нет, пакет собирается из исходников:

```bash
sudo yum install gcc libffi-devel libgphoto2-devel
```

## ASI

```bash
# PICAM SDK: установить libpicam.so в /usr/local/lib или указать путь
export PICAM_LIB=/opt/PrincetonInstruments/picam/runtime/libpicam.so

# доступ к последовательному порту колеса фильтров
sudo usermod -aG dialout $USER      # и перелогиниться
```

Без PICAM SDK драйвер запускается с `asi.camera.backend = "sim"` и `asi.filter_wheel.port = "sim"`. В этом режиме работает всё, кроме реального оборудования: расписание, тёмные кадры, запись FITS, консоль, LAN-сервер и живой просмотр.

## Интерпретатор для служб

`python3` на CentOS 7 — это 3.6. В нём нет `dataclasses`, и служба с ним не стартует. Укажите нужный интерпретатор в `env.sh` или через `install.sh --python`: см. [Скрипт запуска](Run-Script.md) и [Службы systemd](Systemd.md#проверьте-интерпретатор).

## См. также

- [Установка](Install.md)
- [Камера ASI](Camera-ASI.md)
