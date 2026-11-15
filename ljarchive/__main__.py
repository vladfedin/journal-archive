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
    cli = sub.add_parser("export", help="Выгрузить свой журнал через терминал")
    cli.add_argument("username")
    cli.add_argument("--output", type=Path, default=Path("archives"))
    image_mode = cli.add_mutually_exclusive_group()
    image_mode.add_argument("--images", dest="images", action="store_true", help="Скачивать изображения в пределах общего лимита")
    image_mode.add_argument("--no-images", dest="images", action="store_false")
    cli.set_defaults(images=False)
    scope = cli.add_mutually_exclusive_group()
    scope.add_argument("--limit", type=int, default=10, help="Последние N записей (1–50), по умолчанию 10")
    scope.add_argument("--all", action="store_true", help="Весь журнал")
    cli.add_argument("--max-requests", type=int, default=50, help="Жёсткий предел всех внешних HTTP-запросов за запуск (по умолчанию 50)")
    cli.add_argument("--delay", type=float, default=2)
    demo = sub.add_parser("demo", help="Создать пример архива без входа")
    demo.add_argument("--output", type=Path, default=Path("archives/_demo"))
    build = sub.add_parser("build", help="Пересобрать HTML из сохранённой базы, без сети")
    build.add_argument("folder", type=Path)
    refresh = sub.add_parser("refresh-reactions", help="Дозагрузить реакции и их авторов в существующий архив")
    refresh.add_argument("folder", type=Path)
    refresh.add_argument("--max-requests", type=int, default=50)
    json_export = sub.add_parser("json", help="Экспортировать данные из базы в JSON без сети и сборки HTML")
    json_export.add_argument("folder", type=Path)
    json_export.add_argument("--output", type=Path, help="Путь к файлу; по умолчанию data/archive.json в архиве")
    update = sub.add_parser("sync", help="Создать или обновить накопительный архив с продолжением очереди")
    update.add_argument("username")
    update.add_argument("--mode", choices=["full", "period", "urls"], default="full")
    update.add_argument("--from", dest="date_from")
    update.add_argument("--to", dest="date_to")
    update.add_argument("--url", action="append", default=[])
    update.add_argument("--archive", help="Имя существующей папки архива этого пользователя")
    update.add_argument("--output", type=Path, default=Path("archives"))
    update.add_argument("--max-requests", type=int, default=50)
    update.add_argument("--delay", type=float, default=3)
    update.add_argument("--images", action="store_true")
    verify = sub.add_parser("verify", help="Проверить базу и контрольные суммы файлов без сети")
    verify.add_argument("folder", type=Path)
    args = parser.parse_args()
    try:
        if args.command in (None, "serve"):
            from .server import serve
            serve(getattr(args, "output", Path("archives")), getattr(args, "port", 8787), not getattr(args, "no_browser", False))
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
                print(export_json(store, args.output).resolve())
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
                    exporter = object.__new__(Exporter)
                    exporter.store, exporter.images = store, False
                    report = exporter.report("Архив восстановлен после прерывания. Требуется продолжить выгрузку.")
                if not report.get("username"):
                    raise ExportError("В базе нет данных авторизованного журнала.")
                if args.command == "refresh-reactions":
                    from .client import LiveJournalClient, RequestBudget
                    from .exporter import now
                    from .reactions import capture_reactions
                    budget = RequestBudget(args.max_requests)
                    client = LiveJournalClient(report["username"], "", budget=budget)
                    try:
                        capture_reactions(store, client, lambda **v: print(v.get("message", ""), flush=True))
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
            entry_limit = validate_entry_limit(None if getattr(args, "all", False) else getattr(args, "limit", 10))
            RequestBudget(args.max_requests)
            if not math.isfinite(args.delay) or not 1 <= args.delay <= 60:
                raise ExportError("Пауза должна быть от 1 до 60 секунд.")
            if args.command == "sync" or getattr(args, "all", False):
                from .sync import sync, validate_scope, target_slug
                from .client import username
                args.username = username(args.username)
                if args.command == "sync":
                    validate_scope(args.username, args.mode, args.date_from, args.date_to, args.url)
                    target_slug(args.username, args.archive)
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
                report = sync(args.username, password, args.output, mode=args.mode, start=args.date_from, end=args.date_to,
                              urls=args.url, archive=args.archive, images=args.images, delay=args.delay,
                              cancel=cancel, progress=progress, max_requests=args.max_requests)
            elif getattr(args, "all", False):
                report = sync(args.username, password, args.output, mode="full", images=args.images,
                              delay=args.delay, cancel=cancel, progress=progress, max_requests=args.max_requests)
            else:
                report = export(args.username, password, args.output, images=args.images,
                                delay=args.delay, cancel=cancel, progress=progress,
                                entry_limit=entry_limit, max_requests=args.max_requests)
            password = ""
            slug = target_slug(args.username, args.archive) if args.command == "sync" else archive_slug(report["username"], entry_limit)
            print((args.output / slug / "index.html").resolve())
            print(f"HTTP-запросов: {report['requests_used']} / {report['request_limit']}")
            if report["status"] != "complete":
                raise SystemExit(2)
    except ExportError as exc:
        parser.exit(1, str(exc) + "\n")
    except OSError as exc:
        parser.exit(1, f"Ошибка файлов или сети: {exc.strerror or type(exc).__name__}\n")


if __name__ == "__main__":
    main()
