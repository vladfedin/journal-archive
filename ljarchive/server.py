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

from .client import Cancelled, ExportError, RequestBudget, username
from .demo import create_demo
from .exporter import archive_slug, export, validate_entry_limit
from .render import ROOT, plural
from .sync import sync, validate_scope, target_slug

TERMINAL = {"complete", "partial", "cancelled", "error", "demo", "idle"}


class Jobs:
    def __init__(self, output: Path):
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.state = {"phase": "idle"}
        self.cancel = threading.Event()
        self.thread = None

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def progress(self, **values):
        with self.lock:
            if "phase" in values and values["phase"] != self.state.get("phase"):
                self.state.pop("completed", None)
                self.state.pop("total", None)
            self.state.update(values)

    def start(self, data, demo=False):
        if not demo:
            if not isinstance(data.get("username"), str):
                raise ExportError("Введите имя пользователя.")
            data["username"] = username(data["username"])
            if not isinstance(data.get("password"), str) or not 0 < len(data["password"]) <= 1024:
                raise ExportError("Введите пароль Живого Журнала.")
            delay = data.get("delay", 2)
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or not 1 <= delay <= 60:
                raise ExportError("Пауза должна быть от 1 до 60 секунд.")
            if not isinstance(data.get("images", True), bool):
                raise ExportError("Некорректная настройка изображений.")
            data["entry_limit"] = validate_entry_limit(data.get("entry_limit", 10))
            data["max_requests"] = RequestBudget(data.get("max_requests", 50)).limit
            if data["entry_limit"] is None and not data.get("mode"):
                data["mode"] = "full"
            if data.get("mode"):
                scope = validate_scope(data["username"], data["mode"], data.get("date_from"), data.get("date_to"), data.get("urls"))
                data["sync_scope"] = scope.data()
                data["archive"] = target_slug(data["username"], data.get("archive"))
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ExportError("Уже идёт выгрузка. Дождитесь её завершения или остановите её.")
            self.cancel = threading.Event()
            self.state = {"phase": "render" if demo else "login", "message": "Подготавливаем пример…" if demo else "Подключаемся к ЖЖ…", "entries": 0, "comments": 0}
            if not demo:
                self.state.update(requests_used=0, request_limit=data["max_requests"], entry_limit=data["entry_limit"])
            self.thread = threading.Thread(target=self.run, args=(data, demo), daemon=True)
            self.thread.start()
            return dict(self.state)

    def run(self, data, demo):
        slug = "_demo" if demo else data.get("archive") or archive_slug(data["username"], data["entry_limit"])
        try:
            if demo:
                report = create_demo(self.output / slug, progress=self.progress)
            else:
                password = data.pop("password")
                if data.get("sync_scope"):
                    scope = data["sync_scope"]
                    report = sync(data["username"], password, self.output, images=data.get("images", False),
                                  delay=data.get("delay", 3), cancel=self.cancel, progress=self.progress,
                                  max_requests=data["max_requests"], mode=scope["mode"], start=scope["start"],
                                  end=scope["end"], urls=scope["urls"], archive=data["archive"])
                else:
                    report = export(data["username"], password, self.output, images=data.get("images", False),
                                    delay=data.get("delay", 2), cancel=self.cancel, progress=self.progress,
                                    entry_limit=data["entry_limit"], max_requests=data["max_requests"])
                password = ""
            self.progress(phase=report["status"], entries=report["entries_saved"], comments=report["comments_saved"],
                          message=("Демонстрационный архив готов." if demo else
                                   f"Сохранено: {plural(report['entries_saved'], 'запись', 'записи', 'записей')}, {plural(report['comments_saved'], 'комментарий', 'комментария', 'комментариев')}. HTTP-запросов: {report.get('requests_used', 0)} / {report.get('request_limit', 50)}."
                                   + (" Достигнут предел запросов." if report.get("request_limit_reached") else "")),
                          archive_url=f"/archive/{slug}/index.html")
        except Cancelled as exc:
            self.progress(phase="cancelled", message=str(exc))
        except ExportError as exc:
            self.progress(phase="error", message=str(exc))
        except Exception as exc:
            # Do not serialize exceptions/tracebacks with user passwords or remote responses.
            self.progress(phase="error", message=f"Не удалось завершить выгрузку ({type(exc).__name__}). Проверьте свободное место и права на папку. Полученные данные остались на диске.")
        finally:
            data.clear()

    def archives(self):
        found = []
        for path in sorted(self.output.glob("*/export-report.json")):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                slug = path.parent.name
                if not (path.parent / "index.html").is_file():
                    continue
                found.append({key: report.get(key) for key in ("username", "status", "entries_saved", "comments_saved", "created_at", "entry_limit", "requests_used", "request_limit")} |
                             {"slug": slug, "url": f"/archive/{slug}/index.html", "report_url": f"/archive/{slug}/report.html",
                              "zip_url": f"/download/{slug}.zip", "json_url": f"/download/{slug}.json"})
            except (OSError, ValueError):
                continue
        return {"output": str(self.output), "archives": found}


def make_server(output: Path, port=8787):
    jobs = Jobs(output)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "LJLocalArchive/0.2"

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

        def file(self, path, cookie=False, download=None):
            if not path.is_file():
                return self.json({"error": "Файл не найден."}, 404)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix in (".html", ".css", ".js", ".json"):
                mime += "; charset=utf-8"
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
            if not self.authorized():
                return self.json({"error": "Сначала откройте главную страницу приложения."}, 403)
            if path == "/api/status":
                return self.json(jobs.snapshot())
            if path == "/api/archives":
                return self.json(jobs.archives())
            if path.startswith("/static/"):
                target = (ROOT / "static" / path.removeprefix("/static/")).resolve()
                if not target.is_relative_to((ROOT / "static").resolve()):
                    return self.json({"error": "Нет доступа."}, 403)
                return self.file(target)
            if path.startswith("/archive/"):
                relative = Path(path.removeprefix("/archive/"))
                target = (jobs.output / relative).resolve()
                if not target.is_relative_to(jobs.output) or target.suffix not in {".html", ".css", ".js", ".json", ".jpg", ".png", ".gif", ".webp", ".avif"}:
                    return self.json({"error": "Нет доступа."}, 403)
                return self.file(target)
            if path.startswith("/download/") and path.endswith((".zip", ".json")):
                extension = Path(path).suffix
                slug = path.removeprefix("/download/")[:-len(extension)]
                import re
                if not re.fullmatch(r"[a-z0-9_]{1,15}(?:-latest-[1-9][0-9]?)?", slug):
                    return self.json({"error": "Некорректное имя архива."}, 400)
                folder = (jobs.output / slug).resolve()
                if not folder.is_relative_to(jobs.output) or not (folder / "index.html").is_file():
                    return self.json({"error": "Архив не найден."}, 404)
                if extension == ".json":
                    with jobs.lock:
                        if jobs.thread and jobs.thread.is_alive():
                            return self.json({"error": "Дождитесь окончания выгрузки перед скачиванием JSON."}, 409)
                        if not (folder / "archive.sqlite3").is_file():
                            return self.file(folder / "data" / "archive.json", download=slug + ".json")
                        from .data import export_json
                        from .storage import Store
                        try:
                            store = Store(folder)
                            try:
                                target = export_json(store)
                            finally:
                                store.close()
                        except ExportError as exc:
                            return self.json({"error": str(exc)}, 409)
                        return self.file(target, download=slug + ".json")
                # Hold the job lock through ZIP construction to avoid mixing snapshots.
                with jobs.lock:
                    if jobs.thread and jobs.thread.is_alive():
                        return self.json({"error": "Дождитесь окончания выгрузки перед скачиванием ZIP."}, 409)
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
    return server


def serve(output, port=8787, open_browser=True):
    server = make_server(Path(output), port)
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
