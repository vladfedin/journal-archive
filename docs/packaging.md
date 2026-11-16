# Сборка и выпуск

Готовые пакеты: [GitHub Releases](https://github.com/vladfedin/journal-archive/releases/latest). Они включают Python, зависимости, сертификаты TLS, шаблоны и статические файлы. Архивы пользователей хранятся отдельно.

## Локальная сборка

На целевой ОС:

```bash
python -m pip install -e ".[test,package]"
python -m pytest -q
python -m build
python packaging/build.py
python packaging/smoke.py
```

`dist/` содержит wheel, sdist и рабочее приложение. `release/` — версионный пакет и SHA-256:

- `JournalArchive-VERSION-windows-x64.exe`
- `JournalArchive-VERSION-macos-arm64.zip`
- `JournalArchive-VERSION-macos-x64.zip`
- `JournalArchive-VERSION-linux-x64.tar.gz`

Нативная проверка запускает приложение вне проекта, без `VIRTUAL_ENV`/`PYTHONPATH`, в папке с кириллицей. Проверяет создание тестового архива, HTML, помощь, CSS/JS, JSON, HTTP и завершение процесса. Запросов к ЖЖ нет. PyInstaller требует сборки на целевой системе: [документация](https://pyinstaller.org/en/stable/operating-mode.html).

## GitHub Actions

Матрица: Ubuntu 22.04 x86_64, Windows Server 2022 x64, macOS 15 Apple Silicon и Intel. Workflow запускается на `main`, PR, теге `v*` и вручную. Все платформы проходят тесты, сборку и запуск готового пакета. Linux дополнительно проходит браузерные проверки Chromium.

Для выпуска обновите версии в `pyproject.toml` и `ljarchive/__init__.py`, добавьте `docs/releases/VERSION.md`. После успешных проверок `main` отправьте тег `vVERSION`. Задача публикации ждёт успеха всех четырёх сборок, проверяет имена и SHA-256, затем создаёт GitHub Release с восемью файлами (четыре пакета и четыре контрольные суммы). Если сборка или проверка не пройдёт, публикации не будет.

Пакеты доступны из Releases без входа в GitHub. Право `contents: write` предоставлено только задаче публикации; сборки используют `contents: read`. Используются [GitHub CLI](https://cli.github.com/manual/gh_release_create) и [токен workflow](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token).

## Подпись

Пакеты пока не имеют доверенной подписи Windows и Apple Developer ID/notarization. ОС может потребовать подтверждение запуска; это указано в описании выпуска. Сертификаты не входят в репозиторий. Подпись для широкого распространения можно подключить отдельным этапом перед вычислением SHA-256.

Архивы, SQLite, виртуальное окружение, `.jobs` и локальные данные не включаются в сборку. `MANIFEST.in` исключает пользовательские данные из sdist; PyInstaller получает пакет `ljarchive` и его ресурсы.
