# Обновление

`update.py` получает последнюю версию из git-репозитория и заменяет только разрешённые файлы.

```bash
python update.py             # обновить
python update.py --dry-run   # показать изменения, ничего не применяя
python update.py --force     # перезаписать и локально изменённые файлы
```

| Что | Обновляется |
|---|---|
| `.py` и `.md` в корне программы | да |
| `requirements*.txt` | да |
| `firmware/`, `docs/` | да |
| `cameras/`, `systemd/`, `infra_lib/` | да |
| `run.sh`, `run.cmd`, `env.*.example`, `mail_account.json.example` | да |
| `schedule_generator.py` | да |
| `config.json`, `.gitignore`, `.gitattributes` | нет |
| файлы с `camcfg_` и `schedule` в имени | нет |

Переименования файлов обрабатываются. Файлы, которые на станциях не нужны, `update.py` не трогает и перечисляет в списке «Not used on stations».

`mail_account.json` и `env.sh` обновление не затрагивает.

После обновления перезапустите камеру или службу.

## См. также

- [Службы systemd](Systemd.md)
- [Конфигурация](Config.md)
