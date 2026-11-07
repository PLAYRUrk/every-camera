# Службы systemd

Программа работает под `systemctl` и стартует после перезагрузки машины. Запуск руками при этом не меняется. У службы `Type=simple`: процессом владеет systemd, PID-файл не нужен.

## Установка

```bash
systemd/install.sh asi --dry-run --user alex   # посмотреть, что будет записано
sudo systemd/install.sh asi                    # установка
```

Скрипт:

- подставляет пути этой копии программы в шаблон `systemd/every-camera@.service` и кладёт юнит в `/etc/systemd/system/`;
- копирует `config.json` в `/etc/every-camera/asi.json`, если файла там ещё нет;
- для `sptt` ставит правило udev;
- ставит сторожа `every-camera-sentinel`, одного на машину; юнит сторожа передаёт ему `--config-dir` с каталогом конфигов установщика;
- выполняет `systemctl enable --now`.

| Параметр | Назначение |
|---|---|
| `<тип>` | `asi`, `japan`, `sentry`, `cannon`, `sptt`, `infra`; допускается метка: `sptt-north` |
| `--user NAME` | чей домашний каталог получает логи и статусы; по умолчанию тот, кто вызвал `sudo` |
| `--python PATH` | интерпретатор; записывается в юнит только если указан, иначе его выбирает `run.sh` (`env.sh`, затем `venv/` или `.venv/`, затем `python3` из `PATH`) |
| `--config-dir DIR` | каталог конфигов, по умолчанию `/etc/every-camera` |
| `--dry-run` | показать юнит и ничего не менять |
| `--uninstall` | остановить службу и удалить юнит |

Юнит шаблонный, имя после `@` — тип камеры, поэтому на машине можно держать несколько камер:

```bash
sudo systemd/install.sh asi
sudo systemd/install.sh japan
systemctl status every-camera@asi
journalctl -u every-camera@asi -f
```

Если камера отказалась от работы (5 неудачных кадров подряд или упал цикл измерений), процесс завершается с кодом 1. Поэтому `Restart=on-failure` перезапускает службу, а сторож сообщает об аварии, а не о штатной остановке: см. [Оповещения](Alerts.md#о-чём-приходит-письмо).

Каждая служба читает свой `/etc/every-camera/<тип>.json`. [Имена экземпляров](Multi-Instance.md) разводятся сами.

## Несколько камер SPTT на одном ПК

Камеры CSDU одной модели выглядят на шине одинаково: серийного номера у них нет. Поэтому камера выбирается по физическому USB-порту. Он не меняется, пока камеру не переткнули в другой разъём.

1. Посмотрите, какие камеры подключены. Команда ничего не открывает и безопасна при работающих камерах. В `setup_app.py` то же делает кнопка **Find…** рядом с полем USB port.

   ```bash
   python3 main.py --type sptt --probe
   # USB port    Model      State
   # 1-1.2       CSDU-429   firmware loaded
   # 1-1.3       CSDU-285C  no firmware yet
   ```

2. Дайте каждой камере свою службу. Метка после типа становится частью имени службы и файла конфигурации.

   ```bash
   sudo systemd/install.sh sptt-north
   sudo systemd/install.sh sptt-south
   ```

3. В `/etc/every-camera/sptt-north.json` и `sptt-south.json` задайте свои `sptt.usb_port`, `sptt.output_dir` и `sptt.instance_name`, затем перезапустите:

   ```bash
   sudo systemctl restart every-camera@sptt-north every-camera@sptt-south
   ```

Вручную то же самое: `python3 main.py --type sptt --config north.json` и `--config south.json` в двух терминалах.

Важно:

- **`output_dir` у каждой камеры свой.** Имя файла — время снимка, и две камеры, снимающие в `:00` и `:30`, писали бы в один файл.
- Если камер несколько, а `usb_port` не задан, программа останавливается и перечисляет порты. Она не выбирает первую попавшуюся: после перезагрузки это могла бы оказаться другая камера, и архивы перепутались бы.
- Порт LAN-сервера каждый экземпляр находит сам, а явный `instance_name` помогает различать камеры в архиве и мониторе.
- `gui_app.py` ведёт одну камеру SPTT — ту, что указана в `usb_port`. Для нескольких нужны консольные экземпляры или службы.

## Что важно знать

### Остановка не мгновенная

По `systemctl stop` драйвер доснимает кадр и пишет финальные тёмные кадры, а ASI ещё и отогревает сенсор. На отогрев отводится до `asi.cooling.warm_timeout` (900 с). Стандартные 90 с systemd прервали бы процесс, поэтому в юните стоит `TimeoutStopSec=1800`. Уменьшайте его осознанно.

`SIGTERM` обрабатывается так же, как `Ctrl+C`. Повторный сигнал, который systemd шлёт по исчерпании таймаута, означает выход без финальных тёмных кадров. Днём, между ночами, [финальных тёмных кадров нет](Darks-And-Phases.md#3-финальные-тёмные-кадры), и остановка проходит быстро.

### Домашний каталог оператора

Служба работает от root, но пишет логи, статусы и блокировки имён в `$HOME/.every_camera/` оператора: в юните стоит `Environment=HOME=/home/<user>`. Иначе файлы попали бы в `/root/.every_camera/`, и `monitor_app.py`, запущенный пользователем, не увидел бы камеру.

### Проверьте интерпретатор

Без `--python` интерпретатор выбирает `run.sh`: `env.sh`, затем `venv/` или `.venv/`, затем `python3` из `PATH`. Последний часто оказывается anaconda в домашнем каталоге, а служба стартует до того, как `/home` гарантированно готов. Поэтому укажите системный интерпретатор или python из venv (тогда путь запишется в юнит):

```bash
sudo systemd/install.sh asi --python /usr/bin/python3
```

Зависимости должны быть установлены именно для него.

### Переменные окружения

Переменные логин-шелла службе не достаются. Прописывайте их в [`env.sh`](Run-Script.md#envsh), а не в юните. После правки: `systemctl restart every-camera@asi`.

### Живой экран и Enter

Когда вывод не терминал, живой экран заменяется строками лога, поэтому `--verbose` службе не нужен. Ожидание Enter в режиме `time` тоже отключается.

## Удаление

```bash
sudo systemd/install.sh asi --uninstall
```

Скрипт останавливает службу штатно, с финальными тёмными кадрами и отогревом, снимает автозапуск и удаляет файл юнита, если им не пользуется другая камера. Конфиг в `/etc/every-camera/`, архив и логи остаются.

Вручную:

```bash
sudo systemctl disable --now every-camera@asi
sudo rm /etc/systemd/system/every-camera@.service
sudo systemctl daemon-reload
```

## Юнит вручную

Минимальный вариант, пути свои:

```ini
[Unit]
Description=Every Camera — ASI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/alex/every-camera
ExecStart=/usr/bin/python3 /home/alex/every-camera/main.py --type asi \
          --config /etc/every-camera/asi.json
Environment=HOME=/home/alex
Restart=on-failure
RestartSec=30
KillSignal=SIGTERM
TimeoutStopSec=1800

[Install]
WantedBy=multi-user.target
```

`--config` указывайте абсолютным путём: по умолчанию конфиг ищется рядом со скриптом.

## См. также

- [Скрипт запуска](Run-Script.md)
- [Оповещения](Alerts.md#установка-службы)
- [Несколько экземпляров](Multi-Instance.md)
