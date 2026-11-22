from __future__ import annotations

import argparse
import getpass
import threading
from pathlib import Path

from .client import ExportError


def main():
    parser = argparse.ArgumentParser(description="Локальный архив ЖЖ: посты, комментарии и поиск.")
    sub = parser.add_subparsers(dest="command")
    web = sub.add_parser("serve", help="Открыть локальное приложение")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument("--no-browser", action="store_true")
    web.add_argument("--output", type=Path, default=Path("archives"))
    desktop = sub.add_parser("desktop", help="Запустить приложение с обновлением работающей версии")
    desktop.add_argument("--port", type=int, default=8787)
    desktop.add_argument("--no-browser", action="store_true")
    desktop.add_argument("--output", type=Path, default=None)
    cli = sub.add_parser("export", help="Выгрузить свой журнал через терминал")
    cli.add_argument("username")
    cli.add_argument("--output", type=Path, default=Path("archives"))
    image_mode = cli.add_mutually_exclusive_group()
    image_mode.add_argument("--images", dest="images", action="store_true", help="Скачивать изображения в пределах общего лимита")
    image_mode.add_argument("--no-images", dest="images", action="store_false")
    cli.set_defaults(images=False)
    scope = cli.add_mutually_exclusive_group()
    scope.add_argument("--limit", type=int, help="Тест: последние N записей (1–50)")
    scope.add_argument("--all", action="store_true", help="Весь журнал")
    cli.add_argument("--max-requests", type=int, help="Общий лимит запросов; по умолчанию без лимита, для теста — 50")
    cli.add_argument("--delay", type=float, default=3)
    demo = sub.add_parser("demo", help="Создать пример архива без входа")
    demo.add_argument("--output", type=Path, default=Path("archives/_demo"))
    build = sub.add_parser("build", help="Пересобрать HTML из сохранённой базы, без сети")
    build.add_argument("folder", type=Path)
    build.add_argument('--journal-page', type=Path, help='Уточнить закреп по сохранённой главной странице ЖЖ')
    refresh = sub.add_parser("refresh-reactions", help="Дозагрузить реакции и их авторов в существующий архив")
    refresh.add_argument("folder", type=Path)
    refresh.add_argument("--max-requests", type=int, default=50)
    json_export = sub.add_parser("json", help="Экспортировать данные из базы в JSON без сети и сборки HTML")
    json_export.add_argument("folder", type=Path)
    json_export.add_argument("--output", type=Path, help="Путь к файлу; по умолчанию data/archive.json в архиве")
    json_export.add_argument("--format", choices=["source", "activitystreams"], default="source", help="Формат JSON; по умолчанию source")
    update = sub.add_parser("sync", help="Создать или обновить накопительный архив с продолжением очереди")
    update.add_argument("username")
    update.add_argument("--mode", choices=["full", "period", "urls"], default="full")
    update.add_argument("--from", dest="date_from")
    update.add_argument("--to", dest="date_to")
    update.add_argument("--url", action="append", default=[])
    update.add_argument("--archive", help="Имя существующей папки архива этого пользователя")
    update.add_argument("--output", type=Path, default=Path("archives"))
    update.add_argument("--max-requests", type=int)
    update.add_argument("--delay", type=float, default=3)
    update.add_argument("--images", action="store_true")
    verify = sub.add_parser("verify", help="Проверить базу и контрольные суммы файлов без сети")
    verify.add_argument("folder", type=Path)
    for command in (cli, update):
        command.add_argument('--journal', help='Имя журнала или сообщества; username — аккаунт для входа')
        command.add_argument('--source-type', choices=('own', 'community', 'reader'), help='reader — читать со своего аккаунта')
        command.add_argument('--no-comments', action='store_true', help='Пропустить обсуждения в режиме reader')
        command.add_argument("--videos", action="store_true", help="Скачивать встроенные видео")
        command.add_argument("--account", action="store_true", help="Профиль, друзья, настройки, каталог юзерпиков")
        command.add_argument("--messages", action="store_true", help="Входящие и исходящие из доступной выдачи ЖЖ")
    args = parser.parse_args()
    try:
        if args.command in (None, "serve"):
            from .server import serve
            serve(getattr(args, "output", Path("archives")), getattr(args, "port", 8787), not getattr(args, "no_browser", False))
        elif args.command == "desktop":
            from .desktop import launch
            from .paths import desktop_output
            launch(args.output or desktop_output(), args.port, not args.no_browser)
        elif args.command == "demo":
            from .demo import create_demo
            create_demo(args.output)
            print((args.output / "index.html").resolve())
        elif args.command == "json":
            from .data import export_json
            from .storage import Store
            if not (args.folder / "archive.sqlite3").is_file():
                raise ExportError("В папке нет archive.sqlite3.")
            store = Store(args.folder)
            try:
                print(export_json(store, args.output, format=args.format).resolve())
            finally:
                store.close()
        elif args.command in ("build", "refresh-reactions"):
            import json
            from .storage import Store
            from .render import build_archive
            from .exporter import Exporter
            if not (args.folder / "archive.sqlite3").is_file():
                raise ExportError("В папке нет archive.sqlite3.")
            store = Store(args.folder)
            try:
                report_file = args.folder / "export-report.json"
                if report_file.exists():
                    report = json.loads(report_file.read_text(encoding="utf-8"))
                else:
                    # Recover readable pages after a hard interruption before first rendering.
                    from .client import LiveJournalClient
                    from .sources import stored_source
                    from .sync import Scope, SyncExporter
                    identity = stored_source(store)
                    if not identity:
                        raise ExportError("В базе нет данных авторизованного журнала.")
                    client = LiveJournalClient(identity['login_username'], '', journal=identity['username'],
                                               source_type=identity['type'])
                    client.source = identity
                    state = store.get('sync_job', {})
                    if state.get('scope'):
                        exporter = SyncExporter(store, client, Scope(**state['scope']))
                        exporter.state = state
                    else:
                        exporter = Exporter(store, client, images=False, entry_limit=store.get('entry_limit'))
                    try:
                        report = exporter.report("Архив восстановлен после прерывания. Требуется продолжить выгрузку.")
                        report['source'] = identity
                        if exporter.reader:
                            report['comments_requested'] = store.get('comments_requested', True)
                            report['scope'] = f"Сохранённая часть журнала · читатель {identity['login_username']}."
                    finally:
                        client.close()
                if not report.get("username"):
                    raise ExportError("В базе нет данных авторизованного журнала.")
                if args.command == 'build' and args.journal_page:
                    from .feed_pin import page_pin
                    from .sources import stored_source
                    identity = stored_source(store)
                    did = page_pin(args.journal_page.read_bytes(), identity['username'], identity['id'], identity['login_username'])
                    if did is None:
                        raise ExportError('На этой странице не удалось проверить закрепление для данного архива.')
                    with store.db:
                        store.observe_feed_pin(did)
                if args.command == "refresh-reactions":
                    from .client import LiveJournalClient, RequestBudget
                    from .exporter import now
                    from .reactions import capture_reactions
                    budget = RequestBudget(args.max_requests)
                    from .sources import bind_source, stored_source
                    identity = stored_source(store)
                    reader = identity and identity['type'] == 'reader'
                    if reader:
                        password = getpass.getpass(f"Пароль ЖЖ для {identity['login_username']}: ")
                        if not password:
                            raise ExportError("Введите пароль читателя.")
                        client = LiveJournalClient(identity['login_username'], password, journal=identity['username'],
                                                   source_type='reader', budget=budget)
                    else:
                        client = LiveJournalClient(report['username'], '', budget=budget)
                    try:
                        selected = None
                        if reader:
                            client.login()
                            client.source['capabilities'].update(identity.get('capabilities', {}))
                            with store.db:
                                bind_source(store, client)
                            selected = {p['itemid'] for p in store.entries() if p.get('source_status') != 'not_returned'}
                        capture_reactions(store, client, lambda **v: print(v.get("message", ""), flush=True), entry_ids=selected)
                    finally:
                        client.close()
                    report["reaction_refresh"] = {"created_at": now(), **budget.snapshot()}
                build_archive(store, report, images=False)
            finally:
                store.close()
            print((args.folder / "index.html").resolve())
        elif args.command == "verify":
            import json
            from .integrity import verify_archive
            from .storage import Store
            if not (args.folder / "archive.sqlite3").is_file():
                raise ExportError("В папке нет archive.sqlite3.")
            store = Store(args.folder)
            try:
                result = verify_archive(store)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            finally:
                store.close()
            if not result["ok"]:
                raise SystemExit(2)
        elif args.command in ("export", "sync"):
            import math
            import signal
            from .exporter import archive_slug, export, validate_entry_limit
            from .client import RequestBudget
            entry_limit = validate_entry_limit(getattr(args, "limit", None))
            full_export = args.command == "export" and entry_limit is None
            if entry_limit is not None and args.max_requests is None:
                args.max_requests = 50
            RequestBudget(args.max_requests)
            if not math.isfinite(args.delay) or not 1 <= args.delay <= 60:
                raise ExportError("Пауза должна быть от 1 до 60 секунд.")
            from .sources import archive_base, source
            base = archive_base(source(args.username, args.journal, args.source_type))
            if args.no_comments and args.source_type != 'reader':
                raise ExportError('--no-comments применяется к режиму reader.')
            if args.command == "sync" or full_export:
                from .sync import sync, validate_scope, target_slug
                from .client import username
                args.username = username(args.username)
                if args.command == "sync":
                    validate_scope(args.journal or args.username, args.mode, args.date_from, args.date_to, args.url)
                    target_slug(base, args.archive)
            password = getpass.getpass("Пароль ЖЖ (не сохраняется): ")
            if not password:
                raise ExportError("Пароль не может быть пустым.")
            cancel = threading.Event()
            signal.signal(signal.SIGINT, lambda *_: cancel.set())
            last_message = None
            def progress(**values):
                nonlocal last_message
                message = values.get("message")
                if message and message != last_message:
                    print(message, flush=True)
                    last_message = message
            if args.command == "sync":
                report = sync(args.username, password, args.output, journal=args.journal, source_type=args.source_type, include_comments=not args.no_comments, mode=args.mode, start=args.date_from, end=args.date_to,
                              urls=args.url, archive=args.archive, images=args.images, videos=args.videos, delay=args.delay,
                              cancel=cancel, progress=progress, max_requests=args.max_requests, account=args.account, messages=args.messages)
            elif full_export:
                report = sync(args.username, password, args.output, journal=args.journal, source_type=args.source_type, include_comments=not args.no_comments, mode="full", images=args.images, videos=args.videos,
                              delay=args.delay, cancel=cancel, progress=progress, max_requests=args.max_requests, account=args.account, messages=args.messages)
            else:
                report = export(args.username, password, args.output, journal=args.journal, source_type=args.source_type, include_comments=not args.no_comments, images=args.images, videos=args.videos,
                                delay=args.delay, cancel=cancel, progress=progress,
                                entry_limit=entry_limit, max_requests=args.max_requests, account=args.account, messages=args.messages)
            password = ""
            slug = target_slug(base, args.archive) if args.command == "sync" else archive_slug(base, entry_limit)
            print((args.output / slug / "index.html").resolve())
            print(f"HTTP-запросов: {report['requests_used']}" + (f" / {report['request_limit']}" if report["request_limit"] else ""))
            if report["status"] != "complete":
                raise SystemExit(2)
    except ExportError as exc:
        parser.exit(1, str(exc) + "\n")
    except OSError as exc:
        parser.exit(1, f"Ошибка файлов или сети: {exc.strerror or type(exc).__name__}\n")


if __name__ == "__main__":
    main()
