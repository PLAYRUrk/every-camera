# Разработка

Окружение разработки разворачивается скриптом из приватного репозитория:

```bash
gh api repos/PLAYRUrk/every-camera-dev/contents/dev-setup.sh -H 'Accept: application/vnd.github.raw' | bash
```

## Новый параметр камеры

Значение по умолчанию добавляется в `DEFAULT_CONFIG` (`utils.py`), а изменяемые на лету поля — в `PARAM_SCHEMAS` (`camera_service.py`).

## Документация

Эта Wiki собирается из папки `docs/` основного репозитория. Править её нужно там, в одном коммите с кодом, а не в веб-редакторе.

- Страницы — обычный Markdown. Файлы названы латиницей, а имя страницы в Wiki берётся из заголовка `# H1`.
- Ссылки относительные, с `.md`: `[Камера ASI](Camera-ASI.md#параметры)`. Они работают и в дереве репозитория, и офлайн на станции.
- Новую страницу нужно добавить в `docs/_Sidebar.md`.
- `README.md` — самостоятельная копия: изменения поведения вносятся и туда.

При push в `main` с изменениями в `docs/` workflow `.github/workflows/wiki.yml` запускает `.github/scripts/publish_wiki.py` и заменяет содержимое Wiki целиком. Скрипт останавливает публикацию при битой ссылке или якоре.

Проверить локально:

```bash
python .github/scripts/publish_wiki.py docs /tmp/wiki-preview
```

## См. также

- [Структура проекта](Project-Structure.md)
