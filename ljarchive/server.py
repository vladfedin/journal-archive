from __future__ import annotations

import json
import math
import mimetypes
import secrets
import threading
import webbrowser
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryFile
from urllib.parse import unquote, urlsplit

from . import __version__
from .client import Cancelled, ExportError, RequestBudget, username
from .demo import create_demo
from .exporter import archive_slug, export, validate_entry_limit
from .render import ROOT, plural
from .sync import sync, validate_scope, target_slug

TERMINAL = {"complete", "partial", "cancelled", "error", "demo", "idle"}


class Jobs:
    PARAMS = ("username", "archive", "mode", "date_from", "date_to", "urls", "entry_limit", "images", "delay", "max_requests", "account", "messages")

    def __init__(self, output: Path):
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.tasks = self.output / ".jobs"
        self.tasks.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.state = {"phase": "idle"}
        self.cancel = threading.Event()
        self.thread = None
        self.stopping = False
        self.current = None
        records = []
        for file in self.tasks.glob("*.json"):
            try:
                record = json.loads(file.read_text(encoding="utf-8"))
                self.task_path(record["parameters"]["archive"])
                records.append(record)
            except (OSError, ValueError, KeyError, TypeError, ExportError):
                continue
        if records:
            latest = max(records, key=lambda item: item.get("updated_at", ""))
            self.state = latest.get("state", {"phase": "idle"})
            if self.state.get("phase") not in TERMINAL:
                self.state.update(phase="cancelled", message="Загрузка прервана. Можно продолжить.", cancelling=False)
            self.current = latest["parameters"]
            self.persist()

    def task_path(self, slug):
        import re
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9_]{1,15}(?:-latest-[1-9][0-9]?)?", slug):
            raise ExportError("Некорректный архив.")
        return self.tasks / (slug + ".json")

    def persist(self):
        if self.current:
            from .storage import atomic_write, dumps
            from .exporter import now
            atomic_write(self.task_path(self.current["archive"]), dumps({
                "parameters": self.current, "state": self.state, "updated_at": now()}))

    def legacy_parameters(self, slug):
        self.task_path(slug)
        report = json.loads((self.output / slug / "export-report.json").read_text(encoding="utf-8"))
        scope = report.get("sync", {}).get("scope", {})
        return {"username": report["username"], "archive": slug,
                "mode": "latest" if report.get("entry_limit") else scope.get("mode", "full"),
                "entry_limit": report.get("entry_limit"), "date_from": scope.get("start"),
                "date_to": scope.get("end"), "urls": scope.get("urls", []),
                "images": bool(report.get("images_requested")), "delay": 3,
                "max_requests": report.get("request_limit", 50 if report.get("entry_limit") else None)}

    def refresh_format(self, slug):
        """Rebuild derived pages on first opening an older archive, without network access."""
        self.task_path(slug)
        folder = self.output / slug
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            path = folder / "export-report.json"
            if not path.is_file() or not (folder / "archive.sqlite3").is_file():
                return
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("format_version") == "0.6.3":
                return
            from .storage import Store
            from .render import build_archive
            store = Store(folder)
            try:
                build_archive(store, report, images=False)
            finally:
                store.close()

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def progress(self, **values):
        with self.lock:
            previous = self.state.get("phase")
            if "phase" in values and values["phase"] != previous:
                for key in ("completed", "total", "retry_at"):
                    self.state.pop(key, None)
            # Reports may be large; the archive retains the complete report separately.
            report = values.pop("report", None)
            self.state.update(values)
            if report:
                self.state.update(request_limit_reached=bool(report.get("request_limit_reached")))
            if self.state.get("phase") != previous or self.state.get("phase") in TERMINAL:
                self.persist()

    def start(self, data, demo=False):
        with self.lock:
            if self.stopping:
                raise ExportError("Приложение завершается. Откройте новую версию.")
            if self.thread and self.thread.is_alive():
                raise ExportError("Другая загрузка ещё выполняется.")
            if not demo:
                if data.get("resume"):
                    try:
                        task = self.task_path(data["resume"])
                        if task.exists():
                            record = json.loads(task.read_text(encoding="utf-8"))
                        else:
                            record = {"parameters": self.legacy_parameters(data["resume"]), "state": {"phase": "partial"}}
                    except (OSError, ValueError):
                        raise ExportError("Сохранённая задача не найдена.") from None
                    if record["state"].get("phase") == "complete":
                        raise ExportError("Загрузка завершена. Выберите «Обновить».")
                    data = {**record["parameters"], "password": data.get("password")}
                else:
                    data = dict(data)
                if not isinstance(data.get("username"), str):
                    raise ExportError("Введите имя пользователя.")
                data["username"] = username(data["username"])
                if not isinstance(data.get("password"), str) or not 0 < len(data["password"]) <= 1024:
                    raise ExportError("Введите пароль ЖЖ.")
                delay = data.get("delay", 3)
                if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or not 1 <= delay <= 60:
                    raise ExportError("Пауза должна быть от 1 до 60 секунд.")
                if not isinstance(data.get("images", False), bool):
                    raise ExportError("Некорректная настройка изображений.")
                data["entry_limit"] = validate_entry_limit(data.get("entry_limit"))
                default_limit = 50 if data["entry_limit"] is not None else None
                data["max_requests"] = RequestBudget(data.get("max_requests", default_limit)).limit
                if data["entry_limit"] is not None and data["max_requests"] is None:
                    raise ExportError("Для теста задайте лимит запросов.")
                if data["entry_limit"] is None:
                    data["mode"] = data.get("mode") or "full"
                    scope = validate_scope(data["username"], data["mode"], data.get("date_from"), data.get("date_to"), data.get("urls"))
                    data.update(mode=scope.mode, date_from=scope.start, date_to=scope.end, urls=list(scope.urls))
                    data["archive"] = target_slug(data["username"], data.get("archive"))
                    data["sync_scope"] = scope.data()
                else:
                    data["mode"] = "latest"
                    data["archive"] = archive_slug(data["username"], data["entry_limit"])
                for key in ("account", "messages"):
                    data.setdefault(key, False)
                    if not isinstance(data[key], bool):
                        raise ExportError("Некорректная настройка личных данных.")
                data.setdefault("images", False)
                data.setdefault("delay", 3)
            self.cancel = threading.Event()
            slug = "_demo" if demo else data["archive"]
            self.state = {"phase": "render" if demo else "login", "message": "", "entries": 0, "comments": 0,
                          "username": "example_journal" if demo else data["username"], "slug": slug, "cancelling": False, "job_id": secrets.token_hex(8)}
            self.current = None if demo else {key: data.get(key) for key in self.PARAMS}
            if not demo:
                self.state.update(requests_used=0, request_limit=data["max_requests"], entry_limit=data["entry_limit"])
                if (self.output / slug / "index.html").is_file():
                    self.state["archive_url"] = f"/archive/{slug}/index.html"
                self.persist()
            self.thread = threading.Thread(target=self.run, args=(data, demo), daemon=True)
            self.thread.start()
            return dict(self.state)

    def run(self, data, demo):
        slug = "_demo" if demo else data["archive"]
        try:
            if demo:
                report = create_demo(self.output / slug, progress=self.progress)
            else:
                password = data.pop("password")
                if data.get("sync_scope"):
                    scope = data["sync_scope"]
                    report = sync(data["username"], password, self.output, images=data["images"],
                                  delay=data["delay"], cancel=self.cancel, progress=self.progress,
                                  max_requests=data["max_requests"], mode=scope["mode"], start=scope["start"],
                                  end=scope["end"], urls=scope["urls"], archive=slug, account=data.get("account", False), messages=data.get("messages", False))
                else:
                    report = export(data["username"], password, self.output, images=data["images"],
                                    delay=data["delay"], cancel=self.cancel, progress=self.progress,
                                    entry_limit=data["entry_limit"], max_requests=data["max_requests"], account=data.get("account", False), messages=data.get("messages", False))
                password = ""
            message = "Лимит запросов" if report.get("request_limit_reached") else ""
            if report["status"] not in ("complete", "demo") and not message:
                message = next(iter(report.get("issues", [])), "Загрузка не завершена.")
            self.progress(phase=report["status"], entries=report["entries_saved"], comments=report["comments_saved"],
                          requests_used=report.get("requests_used", 0), request_limit=report.get("request_limit"),
                          request_limit_reached=bool(report.get("request_limit_reached")), cancelling=False,
                          message=message, archive_url=f"/archive/{slug}/index.html")
        except Cancelled:
            self.progress(phase="cancelled", message="Загрузка остановлена.", cancelling=False)
        except ExportError as exc:
            self.progress(phase="error", message=str(exc), cancelling=False)
        except Exception as exc:
            self.progress(phase="error", message=f"Не удалось завершить загрузку ({type(exc).__name__}). Проверьте папку и свободное место.", cancelling=False)
        finally:
            data.clear()
            with self.lock:
                self.persist()

    def archives(self):
        found = {}
        for path in sorted(self.output.glob("*/export-report.json")):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                slug = path.parent.name
                if not (path.parent / "index.html").is_file():
                    continue
                found[slug] = {key: report.get(key) for key in ("username", "status", "entries_saved", "comments_saved", "created_at", "entry_limit", "requests_used", "request_limit")}
                found[slug].update(slug=slug, url=f"/archive/{slug}/index.html", report_url=f"/archive/{slug}/report.html",
                                  zip_url=f"/download/{slug}.zip", json_url=f"/download/{slug}.json",
                                  activitystreams_url=f"/download/{slug}.activitystreams.json", resumable=False)
                if slug != "_demo":
                    found[slug].update(parameters=self.legacy_parameters(slug), resumable=report.get("status") not in ("complete", "demo"))
            except (OSError, ValueError, KeyError, TypeError, ExportError):
                continue
        for file in self.tasks.glob("*.json"):
            try:
                record = json.loads(file.read_text(encoding="utf-8"))
                params, state = record["parameters"], record["state"]
                slug = params["archive"]
                self.task_path(slug)
                row = found.setdefault(slug, {"slug": slug, "username": params["username"], "entries_saved": 0,
                                               "comments_saved": 0, "entry_limit": params["entry_limit"]})
                row.update(parameters=params, status=state["phase"], created_at=record["updated_at"],
                           resumable=state["phase"] != "complete", message=state.get("message", ""))
            except (OSError, ValueError, KeyError, TypeError, ExportError):
                continue
        return {"output": str(self.output), "archives": sorted(found.values(), key=lambda row: row.get("created_at") or "", reverse=True)}


def make_server(output: Path, port=8787):
    jobs = Jobs(output)
    token = secrets.token_urlsafe(32)
    instance_id = secrets.token_hex(16)
    app_version = __version__

    class Handler(BaseHTTPRequestHandler):
        server_version = "LJLocalArchive/" + app_version

        def log_message(self, *_):
            pass

        def host_ok(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def authorized(self):
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                value = cookies.get("ljarchive_session")
                return value is not None and secrets.compare_digest(value.value, token)
            except Exception:
                return False

        def response_headers(self, code, content_type, length, *, cookie=False, download=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-LJArchive-Version", app_version)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie", f"ljarchive_session={token}; HttpOnly; SameSite=Strict; Path=/")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()

        def json(self, value, code=200):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.response_headers(code, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def file(self, path, cookie=False, download=None, archive=False):
            if not path.is_file():
                return self.json({"error": "Файл не найден."}, 404)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.name == "activitystreams.json":
                mime = "application/activity+json"
            if path.suffix in (".html", ".css", ".js", ".json"):
                mime += "; charset=utf-8"
            if archive and path.suffix == ".html":
                # App navigation belongs to the served page, not the portable archive.
                body = path.read_bytes().replace(b"<body>", b'<body data-library-url="/">', 1)
                self.response_headers(200, mime, len(body), cookie=cookie, download=download)
                self.wfile.write(body)
                return
            with path.open("rb") as stream:
                import os
                self.response_headers(200, mime, os.fstat(stream.fileno()).st_size, cookie=cookie, download=download)
                import shutil
                shutil.copyfileobj(stream, self.wfile)

        def do_GET(self):
            if not self.host_ok():
                return self.json({"error": "Недопустимый Host."}, 403)
            path = unquote(urlsplit(self.path).path)
            if path == "/":
                return self.file(ROOT / "templates" / "app.html", cookie=True)
            if path == "/favicon.ico":
                return self.file(ROOT / "static" / "favicon.ico")
            if path == "/help":
                from .render import ENV
                body = ENV.get_template("help.html").render(app=True, user="", report={}).encode("utf-8")
                self.response_headers(200, "text/html; charset=utf-8", len(body), cookie=True)
                self.wfile.write(body)
                return
            if not self.authorized():
                return self.json({"error": "Сначала откройте главную страницу приложения."}, 403)
            if path == "/api/app":
                return self.json({"application": "journal-archive", "version": app_version,
                                  "instance_id": instance_id, "output": str(jobs.output),
                                  "stopping": jobs.stopping})
            if path == "/api/status":
                return self.json(jobs.snapshot())
            if path == "/api/archives":
                return self.json(jobs.archives())
            if path.startswith("/api/timeline/"):
                slug = path.removeprefix("/api/timeline/")
                from .storage import Store
                from .progress import timeline
                import sqlite3
                try:
                    jobs.task_path(slug)
                    folder = (jobs.output / slug).resolve()
                    if not folder.is_relative_to(jobs.output):
                        return self.json({"error": "Нет доступа."}, 403)
                    store = Store.reader(folder)
                    try:
                        data = timeline(store, task=True)
                    finally:
                        store.close()
                    state = jobs.snapshot()
                    data["active"] = state.get("entry_id") if state.get("slug") == slug and state.get("phase") not in TERMINAL else None
                    return self.json(data)
                except (ExportError, OSError, sqlite3.Error):
                    return self.json({"entries": [], "gaps": [], "known": 0, "expected": 0, "ready": 0, "partial": 0})
            if path.startswith("/static/"):
                target = (ROOT / "static" / path.removeprefix("/static/")).resolve()
                if not target.is_relative_to((ROOT / "static").resolve()):
                    return self.json({"error": "Нет доступа."}, 403)
                return self.file(target)
            if path.startswith("/archive/"):
                relative = Path(path.removeprefix("/archive/"))
                target = (jobs.output / relative).resolve()
                icon = relative.parts[1:] in {("assets", "favicon.svg"), ("assets", "favicon.ico")}
                if any(part.startswith(".") for part in relative.parts) or not target.is_relative_to(jobs.output) or (not icon and target.suffix not in {".html", ".css", ".js", ".json", ".jpg", ".png", ".gif", ".webp", ".avif"}):
                    return self.json({"error": "Нет доступа."}, 403)
                if relative.parts and target.name == "index.html":
                    try:
                        jobs.refresh_format(relative.parts[0])
                    except (ExportError, OSError, ValueError):
                        return self.json({"error": "Не удалось обновить оформление архива. Проверьте папку и свободное место."}, 409)
                active = jobs.snapshot().get("slug") == relative.parts[0] and jobs.snapshot().get("phase") not in TERMINAL
                if len(relative.parts) == 3 and relative.parts[1] == "assets" and relative.parts[2] in {"archive.css", "archive.js", "theme.js", "common.js", "timeline.js", "favicon.svg", "favicon.ico"}:
                    if active or not target.exists():
                        return self.file(ROOT / "static" / relative.parts[2])
                if (active or not target.exists()) and ((len(relative.parts) == 3 and relative.parts[1] == "entries" and target.stem.isdigit() and target.suffix == ".html") or (len(relative.parts) == 2 and target.name in ("index.html", "timeline.html"))):
                    from .storage import Store
                    from .progress import preview_entry
                    from .render import ENV
                    import sqlite3
                    try:
                        store = Store.reader(jobs.output / relative.parts[0])
                        try:
                            if relative.parts[1] == "entries":
                                html = preview_entry(store, int(target.stem))
                            else:
                                html = ENV.get_template("timeline.html").render(user=store.get("username"), report={"created_at": store.get("started_at", "")}, source="/api/timeline/" + relative.parts[0])
                        finally:
                            store.close()
                        if html:
                            body = html.replace('<body>', '<body data-library-url="/">', 1).encode("utf-8")
                            self.response_headers(200, "text/html; charset=utf-8", len(body))
                            self.wfile.write(body)
                            return
                    except (sqlite3.Error, OSError):
                        pass
                return self.file(target, archive=True)
            if path.startswith("/download/") and path.endswith((".zip", ".json")):
                extension = ".activitystreams.json" if path.endswith(".activitystreams.json") else Path(path).suffix
                slug = path.removeprefix("/download/")[:-len(extension)]
                import re
                if not re.fullmatch(r"[a-z0-9_]{1,15}(?:-latest-[1-9][0-9]?)?", slug):
                    return self.json({"error": "Некорректное имя архива."}, 400)
                folder = (jobs.output / slug).resolve()
                if not folder.is_relative_to(jobs.output) or not (folder / "index.html").is_file():
                    return self.json({"error": "Архив не найден."}, 404)
                if extension in (".json", ".activitystreams.json"):
                    activity = extension == ".activitystreams.json"
                    filename = "activitystreams.json" if activity else "archive.json"
                    with jobs.lock:
                        if jobs.thread and jobs.thread.is_alive():
                            return self.json({"error": "Дождитесь окончания выгрузки перед скачиванием JSON."}, 409)
                        if not (folder / "archive.sqlite3").is_file():
                            target = folder / "data" / filename
                            if activity and not target.is_file():
                                from .activitystreams import to_activitystreams
                                from .storage import atomic_write
                                source = folder / "data" / "archive.json"
                                if not source.is_file():
                                    return self.json({"error": "Исходные данные архива не найдены."}, 404)
                                try:
                                    document = to_activitystreams(json.loads(source.read_text(encoding="utf-8")))
                                    atomic_write(target, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
                                except (OSError, ValueError, KeyError, TypeError):
                                    return self.json({"error": "Не удалось прочитать исходный JSON архива."}, 409)
                            return self.file(target, download=slug + ".json")
                        from .data import export_json
                        from .storage import Store
                        try:
                            store = Store(folder)
                            try:
                                target = export_json(store, format="activitystreams" if activity else "source")
                            finally:
                                store.close()
                        except ExportError as exc:
                            return self.json({"error": str(exc)}, 409)
                        return self.file(target, download=slug + ".json")
                # Hold the job lock through ZIP construction to avoid mixing snapshots.
                with jobs.lock:
                    if jobs.thread and jobs.thread.is_alive():
                        return self.json({"error": "Дождитесь окончания выгрузки перед скачиванием ZIP."}, 409)
                    try:
                        jobs.refresh_format(slug)
                    except (ExportError, OSError, ValueError):
                        return self.json({"error": "Не удалось обновить оформление архива."}, 409)
                    with TemporaryFile() as buffer:
                        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                            for member in folder.rglob("*"):
                                if member.is_file() and not member.is_symlink() and member.suffix not in (".sqlite3", ".tmp", ".lock") and member.relative_to(folder).parts[0] != "quarantine" and member.resolve().is_relative_to(folder):
                                    archive.write(member, str(Path(slug) / member.relative_to(folder)))
                        length = buffer.tell()
                        buffer.seek(0)
                        self.response_headers(200, "application/zip", length, download=slug + ".zip")
                        import shutil
                        shutil.copyfileobj(buffer, self.wfile)
                return
            return self.json({"error": "Страница не найдена."}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            expected = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if not self.host_ok() or not self.authorized() or origin not in expected:
                return self.json({"error": "Запрос должен исходить из локального приложения."}, 403)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.json({"error": "Требуется JSON."}, 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    return self.json({"error": "Недопустимый размер запроса."}, 400)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                return self.json({"error": "Некорректный JSON."}, 400)
            try:
                if self.path == "/api/export":
                    return self.json(jobs.start(data), 202)
                if self.path == "/api/demo":
                    return self.json(jobs.start({}, demo=True), 202)
                if self.path == "/api/cancel":
                    jobs.cancel.set()
                    jobs.progress(cancelling=True)
                    return self.json(jobs.snapshot())
                if self.path == "/api/quit":
                    with jobs.lock:
                        if data.get("expected_instance") not in (None, instance_id):
                            return self.json({"error": "Экземпляр приложения уже изменился."}, 409)
                        if jobs.stopping:
                            return self.json({"message": "Приложение завершается."})
                        jobs.stopping = True
                        jobs.cancel.set()
                    def shutdown():
                        if jobs.thread and jobs.thread.is_alive():
                            jobs.thread.join()
                        self.server.shutdown()
                    self.json({"message": "Сохраняем прогресс и завершаем приложение."})
                    self.wfile.flush()
                    threading.Thread(target=shutdown, daemon=True).start()
                    return
                return self.json({"error": "Неизвестный запрос."}, 404)
            except ExportError as exc:
                return self.json({"error": str(exc)}, 400)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.jobs = jobs
    server.instance_id = instance_id
    return server


def serve(output, port=8787, open_browser=True):
    server = make_server(Path(output), port)
    run_server(server, open_browser)


def run_server(server, open_browser=True):
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Журнал / архив — {url}\nПапка архивов: {server.jobs.output}\nДля остановки: Ctrl+C", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.jobs.cancel.set()
        if server.jobs.thread and server.jobs.thread.is_alive():
            print("Сохраняем прогресс…", flush=True)
            server.jobs.thread.join(timeout=65)
    finally:
        server.server_close()
