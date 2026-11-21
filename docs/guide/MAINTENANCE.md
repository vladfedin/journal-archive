# Обновление гайда

Пользовательская версия — [README.md](README.md). `guide.html` содержит изображения внутри файла и работает без сети; `guide.pdf` подготовлен для печати. Эти материалы не входят в исполняемые файлы приложения и не увеличивают их размер.

Скриншоты получены через Chromium из текущего интерфейса. Временный архив создаётся функцией `create_demo`; содержимое журнала, пароль, путь Windows и состояния задачи — учебные. Вход в ЖЖ и запросы к нему не выполняются. Реальные архивы пользователей не читаются. Снимок ссылок на пакеты получен с публичной страницы загрузки на staerum.ru. `images/02-windows.svg` — явно подписанная схема SmartScreen, а не снимок настоящего окна Windows.

Из корня репозитория:

```bash
python -m pip install -e '.[browser]'
python -m pip install 'Markdown>=3.7,<4'
python -m playwright install chromium
PYTHONPATH=. python tools/capture_guide.py
python tools/build_guide.py
```

`Markdown` нужен только для документации; он не добавлен в зависимости приложения. На Windows вместо `PYTHONPATH=.` можно использовать установленный пакет из предыдущей команды.

При смене версии сверьте текст и имена файлов в README, схему Windows и подпись PDF в `tools/build_guide.py`. Затем переснимите интерфейс, пересоберите документы и просмотрите страницы PDF. Сборка проверяет наличие всех изображений, ссылки оглавления и отсутствие горизонтального переполнения при ширине 390 и 1280 пикселей.

Инструкции по SmartScreen сверены 17 ноября 2026 года с [документацией Microsoft о репутации приложений](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation) и [описанием More info → Run anyway](https://learn.microsoft.com/en-us/troubleshoot/microsoft-edge/development/ie-mode-blocks-addon#check-uac-and-windows-defender-smartscreen-settings). Корпоративные ограничения и Smart App Control могут запрещать продолжение; гайд не предлагает отключать эти средства защиты.
