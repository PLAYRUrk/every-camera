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
| `requirements.txt` | да |
| `firmware/`, `docs/` | да |
| `config.json`, `.gitignore`, `.gitattributes` | нет |
| файлы с `camcfg_` и `schedule` в имени | нет |
| `cameras/`, `infra_lib/`, `systemd/`, `requirements-*.txt` | **нет** |

> `update.py` не обновляет пакет `cameras/` и другие вложенные каталоги, поэтому после него драйверы могут отстать от корневых файлов. Для полного обновления используйте `git pull`.

`mail_account.json` и `env.sh` обновление не затрагивает.

После обновления перезапустите камеру или службу.

## См. также

- [Службы systemd](Systemd.md)
- [Конфигурация](Config.md)
