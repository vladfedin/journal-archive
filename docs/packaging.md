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

Для выпуска обновите версии в `pyproject.toml` и `ljarchive/__init__.py`, добавьте `docs/releases/VERSION.md`. После успешных проверок `main` отправьте тег `vVERSION`. Каждая платформа после своих тестов и проверки запуска публикует пакет и SHA-256 в общий GitHub Release. Первая готовая платформа создаёт выпуск, остальные добавляют файлы по мере готовности. Неудачная или ожидающая сборка не задерживает готовые пакеты. Имена, версия и контрольные суммы проверяются до загрузки; уже опубликованный отличающийся файл не перезаписывается.

Пакеты доступны из Releases без входа в GitHub. Шаг публикации выполняется только на теге `v*` и использует `contents: write`; на `main` и PR пакеты остаются артефактами Actions. Используются [GitHub CLI](https://cli.github.com/manual/gh_release_create) и [токен workflow](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token).

Для повторной публикации готовых артефактов запустите **Publish ready release packages**. Workflow также запускается при изменении его файла или скриптов публикации. Он находит сборки текущей версии из того же репозитория с точным совпадением коммита тега, принимает только успешно завершившиеся задачи платформ и публикует Windows первым. Артефакты PR, другого коммита, отменённых или неуспешных задач не используются. Существующие идентичные файлы повторно не загружаются. Истёкшие артефакты требуют новой сборки.

## Подпись

Пакеты пока не имеют доверенной подписи Windows и Apple Developer ID/notarization. ОС может потребовать подтверждение запуска; это указано в описании выпуска. Сертификаты не входят в репозиторий. Подпись для широкого распространения можно подключить отдельным этапом перед вычислением SHA-256.

Архивы, SQLite, виртуальное окружение, `.jobs` и локальные данные не включаются в сборку. `MANIFEST.in` исключает пользовательские данные из sdist; PyInstaller получает пакет `ljarchive` и его ресурсы.
