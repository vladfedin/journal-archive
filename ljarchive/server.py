from __future__ import annotations

import json
import math
import mimetypes
import secrets
import threading
import time
import webbrowser
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryFile
from urllib.parse import unquote, urlsplit, parse_qs

from . import __version__
from .client import Cancelled, ExportError, RequestBudget, username
from .demo import create_demo
from .exporter import archive_slug, export, validate_entry_limit
from .render import ROOT, plural
from .sync import sync, validate_scope, target_slug

TERMINAL = {"complete", "partial", "cancelled", "error", "demo", "idle"}


class Jobs:
    PARAMS = ("username", "journal", "source_type", "archive", "mode", "date_from", "date_to", "urls", "entry_limit", "images", "videos", "delay", "max_requests", "account", "messages")

    def __init__(self, output: Path):
        from .relocation import resolve_output
        self.output = resolve_output(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.tasks = self.output / ".jobs"
        self.tasks.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.state = {"phase": "idle"}
        self.activity_clock = time.monotonic()
        self.persisted_clock = 0
        self.cancel = threading.Event()
        self.thread = None
        self.stopping = False
        self.current = None
        self.recovery_before = None
        self.recovery_time = 0
        self.recovery_lock = threading.Lock()
        self.recovery_thread = None
        self.history = []
        self.move_thread = None
        self.preparation_lock = threading.Lock()
        self.preparations = {}
        self.preparation_threads = []
        self.formatting = set()
        from .sessions import Sessions
        self.sessions = Sessions()
        from .live_statistics import LiveStatistics
        self.statistics_cache = LiveStatistics()
        from .on_demand import MaterialQueue
        self.materials = MaterialQueue(self)
        self.relocation = {"phase": "idle"}
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
                self.state.pop('wait', None)
                self.state.pop('retry_at', None)
            self.current = latest["parameters"]
            self.history = latest.get('history', [])
            self.persist()

    def move(self, value):
        from .relocation import target_path, relocate
        with self.lock:
            if self.stopping or (self.thread and self.thread.is_alive()) or self.materials.running or self.materials.queue or self.formatting:
                raise ExportError("Перенос доступен после завершения загрузки.")
            if self.move_thread and self.move_thread.is_alive():
                raise ExportError("Перенос уже выполняется.")
            target = target_path(self.output, value)
            source = self.output
            self.relocation = {"phase": "copying", "source": str(source), "target": str(target), "completed": 0, "total": 0}
            def update(**values):
                with self.lock:
                    self.relocation.update(values)
            def switch(folder):
                with self.lock:
                    self.output, self.tasks = folder, folder / ".jobs"
            def worker():
                try:
                    warning = relocate(source, target, switch, update)
                    update(phase="complete", message=warning or "Перенесено")
                except (ExportError, OSError) as exc:
                    update(phase="error", message=str(exc))
            self.move_thread = threading.Thread(target=worker, daemon=True)
            self.move_thread.start()
            return dict(self.relocation)

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
                "parameters": self.current, "state": self.state, "history": self.history[-10:], "updated_at": now()}))
            self.persisted_clock = time.monotonic()

    def legacy_parameters(self, slug):
        self.task_path(slug)
        report = json.loads((self.output / slug / "export-report.json").read_text(encoding="utf-8"))
        scope = report.get("sync", {}).get("scope", {})
        identity = report.get('source') or {}
        return {"username": identity.get('login_username') or report["username"], "archive": slug,
                "journal": report['username'], "source_type": identity.get('type', 'own'),
                "mode": "latest" if report.get("entry_limit") else scope.get("mode", "full"),
                "entry_limit": report.get("entry_limit"), "date_from": scope.get("start"),
                "date_to": scope.get("end"), "urls": scope.get("urls", []),
                "images": bool(report.get("images_requested")), "videos": bool(report.get("videos_requested")), "delay": 3,
                "max_requests": report.get("request_limit", 50 if report.get("entry_limit") else None)}

    def archive_busy(self, slug):
        """Called with lock held; a completed task file never blocks new work."""
        if self.relocation['phase'] == 'copying':
            return 'перенос архивов'
        if self.thread and self.thread.is_alive():
            active = (self.current or {}).get('archive') or self.state.get('slug')
            if active is None or active == slug:
                return 'идёт выгрузка этого архива'
        if slug in self.formatting:
            return 'обновляются страницы этого архива'
        return None

    def prepare_format(self, slug):
        """Return immediately; a separate status endpoint stays responsive during rendering."""
        self.task_path(slug)
        folder = self.output / slug
        path = folder / 'export-report.json'
        with self.lock, self.preparation_lock:
            if self.preparations.get(slug, {}).get('phase') == 'preparing':
                return dict(self.preparations[slug])
            if self.stopping or self.archive_busy(slug) or self.materials.running:
                return None
            if not path.is_file() or not (folder / 'archive.sqlite3').is_file():
                return None
            saved_report = json.loads(path.read_text(encoding='utf-8'))
            if saved_report.get('format_version') == __version__ and saved_report.get('calendar_version') == 2:
                return None
            self.preparations[slug] = {'phase': 'preparing', 'message': 'Чтение архива', 'completed': 0, 'total': 0}
            self.formatting.add(slug)
            def update(**values):
                with self.preparation_lock:
                    self.preparations[slug].update({k: v for k, v in values.items() if k != 'phase'})
            def worker():
                try:
                    self.refresh_format(slug, progress=update, reserved=True)
                    state = {'phase': 'complete'}
                except Exception:
                    state = {'phase': 'error', 'message': 'Не удалось подготовить архив. Проверьте папку и свободное место.'}
                with self.preparation_lock:
                    self.preparations[slug].update(state)
            thread = threading.Thread(target=worker, daemon=True)
            self.preparation_threads = [t for t in self.preparation_threads if t.is_alive()]
            self.preparation_threads.append(thread)
            thread.start()
            return dict(self.preparations[slug])

    def preparation(self, slug):
        self.task_path(slug)
        with self.preparation_lock:
            return dict(self.preparations.get(slug, {'phase': 'complete'}))

    def refresh_format(self, slug, progress=None, *, reserved=False):
        """Rebuild derived pages on first opening an older archive, without network access."""
        self.task_path(slug)
        folder = self.output / slug
        with self.lock:
            if not reserved and (self.archive_busy(slug) or self.materials.running):
                return
            self.formatting.add(slug)
        # Never hold the application lock while rendering: status, cancel and the
        # queue must remain responsive. The archive has its own writer lock.
        try:
            path = folder / "export-report.json"
            if not path.is_file() or not (folder / "archive.sqlite3").is_file():
                return
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("format_version") == __version__ and report.get("calendar_version") == 2:
                return
            from .storage import Store
            from .render import build_archive
            store = Store(folder)
            try:
                build_archive(store, report, images=False, progress=progress)
            finally:
                store.close()
        finally:
            with self.lock:
                self.formatting.discard(slug)

    def snapshot(self):
        with self.lock:
            self.tick_activity()
            if (self.recovery_before is not None and time.monotonic() - self.recovery_time >= 5
                    and not (self.recovery_thread and self.recovery_thread.is_alive())):
                self.recovery_thread = threading.Thread(target=self.refresh_recovery, daemon=True)
                self.recovery_thread.start()
            state = dict(self.state)
            if state.get('phase') != 'idle':
                state.update(server_time=time.time(), worker_alive=bool(self.thread and self.thread.is_alive()))
            if self.current:
                state["parameters"] = {key: self.current[key] for key in self.PARAMS if key in self.current}
            return state

    def tick_activity(self):
        now = time.monotonic()
        if self.state.get('phase') not in TERMINAL:
            key = 'waiting_seconds' if self.state.get('phase') == 'waiting' else 'working_seconds'
            self.state[key] = self.state.get(key, 0) + max(0, now - self.activity_clock)
        self.activity_clock = now

    def recovery_plan(self, slug):
        from .recovery import read_inventory, summary
        path = self.task_path(slug)
        folder = (self.output / slug).resolve()
        if not folder.is_relative_to(self.output.resolve()):
            raise ExportError('Нет доступа к архиву.')
        result = summary(read_inventory(folder))
        try:
            result['attempts'] = json.loads(path.read_text(encoding='utf-8')).get('history', [])[-5:]
        except (OSError, ValueError):
            result['attempts'] = []
        return result

    def refresh_recovery(self, force=False):
        if not force and (self.recovery_before is None or time.monotonic() - self.recovery_time < 5):
            return
        if not self.recovery_lock.acquire(blocking=force):
            return
        try:
            from .recovery import read_inventory, summary
            with self.lock:
                if not self.current:
                    return
                job_id, slug, before = self.state.get('job_id'), self.current['archive'], self.recovery_before
                folder = self.output / slug
            current = read_inventory(folder)
            result = summary(current, before)
            with self.lock:
                if self.state.get('job_id') == job_id:
                    self.state['recovery'] = result
                    self.recovery_time = time.monotonic()
        finally:
            self.recovery_lock.release()

    def progress(self, **values):
        with self.lock:
            self.tick_activity()
            previous = self.state.get("phase")
            event = values.pop('activity', None)
            if event:
                self.state['last_activity'] = event
                self.state['events'] = (self.state.get('events', []) + [event])[-30:]
                if event['kind'] == 'success':
                    self.state['last_success_at'] = event['at']
                if event['kind'] == 'request':
                    self.state['request'] = event
                    if previous in ('waiting', 'retrying'):
                        values.update(phase='loading', message='Запрос отправлен. Получаем данные…')
            if values.get('phase') == 'waiting':
                info = values.get('wait', {})
                if info != self.state.get('wait') or previous != 'waiting':
                    self.state['events'] = (self.state.get('events', []) + [{'kind': 'wait', 'at': time.time(), **info}])[-30:]
            if "phase" in values and values["phase"] != previous:
                for key in ("completed", "total", "retry_at", "wait"):
                    self.state.pop(key, None)
            # Reports may be large; the archive retains the complete report separately.
            report = values.pop("report", None)
            if any(values.get(key, 0) > self.state.get(key, 0) for key in ('entries', 'comments')):
                self.state['last_saved_at'] = time.time()
            self.state.update(values)
            self.state['last_progress_at'] = time.time()
            if report:
                self.state.update(request_limit_reached=bool(report.get("request_limit_reached")))
            if self.state.get("phase") != previous or self.state.get("phase") in TERMINAL or time.monotonic() - self.persisted_clock >= 2:
                self.persist()

    def start(self, data, demo=False):
        with self.lock:
            if self.relocation["phase"] == "copying":
                raise ExportError("Дождитесь завершения переноса архивов.")
            if self.stopping:
                raise ExportError("Приложение завершается. Откройте новую версию.")
            if self.thread and self.thread.is_alive():
                raise ExportError("Другая загрузка ещё выполняется.")
            if self.materials.running:
                raise ExportError('Сейчас сохраняется материал из записи. Дождитесь завершения или отмените его загрузку.')
            resuming = bool(data.get('resume')) and not demo
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
                from .sources import source
                identity = source(data['username'], data.get('journal'), data.get('source_type'))
                data.update(journal=identity['username'], source_type=identity['type'])
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
                    scope = validate_scope(data["journal"], data["mode"], data.get("date_from"), data.get("date_to"), data.get("urls"))
                    data.update(mode=scope.mode, date_from=scope.start, date_to=scope.end, urls=list(scope.urls))
                    data["archive"] = target_slug(data["journal"], data.get("archive"))
                    data["sync_scope"] = scope.data()
                else:
                    data["mode"] = "latest"
                    data["archive"] = archive_slug(data["journal"], data["entry_limit"])
                for key in ("account", "messages"):
                    data.setdefault(key, False)
                    if not isinstance(data[key], bool):
                        raise ExportError("Некорректная настройка личных данных.")
                if data['source_type'] == 'community':
                    data['account'] = data['messages'] = False
                data.setdefault("videos", False)
                if not isinstance(data["videos"], bool):
                    raise ExportError("Некорректная настройка видео.")
                data.setdefault("images", False)
                data.setdefault("delay", 3)
            self.cancel = threading.Event()
            slug = "_demo" if demo else data["archive"]
            if slug in self.formatting:
                raise ExportError('Обновляются страницы этого архива. Дождитесь завершения.')
            self.recovery_before = None
            self.recovery_time = 0
            self.history = []
            if not demo:
                try:
                    self.history = json.loads(self.task_path(slug).read_text(encoding='utf-8')).get('history', [])
                except (OSError, ValueError):
                    pass
            data['_resuming'] = resuming
            self.state = {"phase": "render" if demo else "preparing", "message": "Подготовка архива…", "entries": 0, "comments": 0,
                          "username": "example_journal" if demo else data["journal"], "slug": slug, "cancelling": False, "job_id": secrets.token_hex(8),
                          "operation": "resume" if resuming else "export"}
            self.activity_clock = time.monotonic()
            self.state.update(started_at=time.time(), app_version=__version__, working_seconds=0, waiting_seconds=0)
            self.current = None if demo else {key: data.get(key) for key in self.PARAMS}
            if not demo:
                self.state.update(requests_used=0, request_limit=data["max_requests"], entry_limit=data["entry_limit"])
                if (self.output / slug / "index.html").is_file():
                    self.state["archive_url"] = f"/archive/{slug}/index.html"
                self.persist()
            self.thread = threading.Thread(target=self.run, args=(data, demo), daemon=True)
            self.thread.start()
            return self.snapshot()

    def run(self, data, demo):
        slug = "_demo" if demo else data["archive"]
        try:
            if demo:
                report = create_demo(self.output / slug, progress=self.progress)
            else:
                if data.get('_resuming'):
                    from .recovery import read_inventory, summary
                    self.progress(message='Подсчитываем сохранённые пропуски…')
                    before = read_inventory(self.output / slug, cancel=self.cancel, progress=self.progress)
                    with self.lock:
                        self.recovery_before = before
                    self.progress(recovery=summary(before, before), **(before['counts'] if before else {}))
                password = data.pop("password")
                if data.get("sync_scope"):
                    scope = data["sync_scope"]
                    report = sync(data["username"], password, self.output, journal=data['journal'], source_type=data['source_type'], images=data["images"], videos=data.get("videos", False),
                                  delay=data["delay"], cancel=self.cancel, progress=self.progress,
                                  max_requests=data["max_requests"], mode=scope["mode"], start=scope["start"],
                                  end=scope["end"], urls=scope["urls"], archive=slug, account=data.get("account", False), messages=data.get("messages", False),
                                  session_ready=self.sessions.remember, resume=bool(data.get('_resuming')))
                else:
                    report = export(data["username"], password, self.output, journal=data['journal'], source_type=data['source_type'], images=data["images"], videos=data.get("videos", False),
                                    delay=data["delay"], cancel=self.cancel, progress=self.progress,
                                    entry_limit=data["entry_limit"], max_requests=data["max_requests"], account=data.get("account", False), messages=data.get("messages", False),
                                    session_ready=self.sessions.remember)
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
            if not demo and not self.cancel.is_set():
                self.refresh_recovery(force=True)
            with self.lock:
                if not demo and self.state.get('phase') in TERMINAL:
                    from .exporter import now
                    self.history.append({'job_id': self.state.get('job_id'), 'finished_at': now(), 'status': self.state.get('phase'),
                                         'operation': self.state.get('operation'), 'message': self.state.get('message'),
                                         'entries': self.state.get('entries', 0), 'comments': self.state.get('comments', 0),
                                         'recovery': self.state.get('recovery'), 'requests_used': self.state.get('requests_used', 0)})
                    self.history = self.history[-10:]
                self.recovery_before = None
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
                    # Resume defaults are usable for a future run, but aren't evidence of
                    # the settings used by an old archive whose task file is missing.
                    known = [key for key, field in (("images", "images_requested"), ("videos", "videos_requested"),
                             ("max_requests", "request_limit")) if field in report]
                    scope = report.get("sync", {}).get("scope", {})
                    if report.get("entry_limit") or scope.get("mode"):
                        known.append("mode")
                    known.extend(key for key, field in (("date_from", "start"), ("date_to", "end"), ("urls", "urls")) if field in scope)
                    found[slug]["parameters_known"] = known
            except (OSError, ValueError, KeyError, TypeError, ExportError):
                continue
        for file in self.tasks.glob("*.json"):
            try:
                record = json.loads(file.read_text(encoding="utf-8"))
                params, state = {key: value for key, value in record["parameters"].items() if key in self.PARAMS}, record["state"]
                slug = params["archive"]
                self.task_path(slug)
                row = found.setdefault(slug, {"slug": slug, "username": params.get('journal') or params["username"], "entries_saved": 0,
                                               "comments_saved": 0, "entry_limit": params["entry_limit"]})
                row.update(parameters=params, parameters_known=list(params), status=state["phase"], created_at=record["updated_at"],
                           resumable=state["phase"] != "complete", message=state.get("message", ""))
            except (OSError, ValueError, KeyError, TypeError, ExportError):
                continue
        return {"output": str(self.output), "relocation": dict(self.relocation), "archives": sorted(found.values(), key=lambda row: row.get("created_at") or "", reverse=True)}

    def diagnostics(self, slug):
        from .diagnostics import build_diagnostics
        path = self.task_path(slug)
        folder = (self.output / slug).resolve()
        if not folder.is_relative_to(self.output.resolve()):
            raise ExportError('Нет доступа к архиву.')
        state = self.snapshot()
        if state.get('slug') == slug:
            params = state.get('parameters', {})
        else:
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                state, params = record['state'], record['parameters']
                state = {**state, 'worker_alive': False, 'server_time': time.time()}
            except (OSError, ValueError, KeyError):
                state, params = {}, {}
        return build_diagnostics(folder, state, params)

    def statistics(self, slug):
        self.task_path(slug)
        folder = (self.output / slug).resolve()
        if not folder.is_relative_to(self.output.resolve()):
            raise ExportError('Нет доступа к архиву.')
        state = self.snapshot()
        return self.statistics_cache.get(folder, active=state.get('slug') == slug and state.get('phase') not in TERMINAL)


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

        def response_headers(self, code, content_type, length, *, cookie=False, download=None, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-LJArchive-Version", app_version)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; frame-src https://music.yandex.ru; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if cookie:
                self.send_header("Set-Cookie", f"ljarchive_session={token}; HttpOnly; SameSite=Strict; Path=/")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            for key, value in (extra or {}).items():
                self.send_header(key, value)
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
            from .video import VIDEO_SUFFIXES
            if path.suffix in VIDEO_SUFFIXES:
                import re
                size = path.stat().st_size
                start, end, code = 0, size - 1, 200
                headers = {'Accept-Ranges': 'bytes'}
                if self.headers.get('Range'):
                    match = re.fullmatch(r'bytes=(\d*)-(\d*)', self.headers['Range'])
                    if match and any(match.groups()):
                        if match[1]:
                            start = int(match[1]); end = min(int(match[2]), size - 1) if match[2] else size - 1
                        else:
                            start = max(0, size - int(match[2]))
                    else:
                        start = size
                    if start >= size or end < start:
                        self.response_headers(416, mime, 0, extra={'Content-Range': f'bytes */{size}'})
                        return
                    code = 206; headers['Content-Range'] = f'bytes {start}-{end}/{size}'
                remaining = max(0, end - start + 1)
                self.response_headers(code, mime, remaining, extra=headers)
                with path.open('rb') as stream:
                    stream.seek(start)
                    while remaining:
                        block = stream.read(min(65536, remaining))
                        if not block: break
                        try: self.wfile.write(block)
                        except (BrokenPipeError, ConnectionResetError): break
                        remaining -= len(block)
                return
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
            if path.startswith('/api/diagnostics/'):
                try:
                    return self.json(jobs.diagnostics(path.removeprefix('/api/diagnostics/')))
                except ExportError as exc:
                    return self.json({'error': str(exc)}, 400)
            if path.startswith('/api/statistics/'):
                try:
                    from .live_statistics import public_statistics, discussion_statistics
                    parts = path.removeprefix('/api/statistics/').split('/')
                    if len(parts) > 2 or len(parts) == 2 and parts[1] != 'discussions':
                        raise ExportError('Некорректный адрес статистики.')
                    result = jobs.statistics(parts[0])
                    query = parse_qs(urlsplit(self.path).query)
                    data = result.pop('data')
                    if data is not None and query.get('version', [''])[0] != result['version']:
                        result['data'] = discussion_statistics(data) if len(parts) == 2 else public_statistics(data)
                    return self.json(result)
                except ExportError as exc:
                    return self.json({'error': str(exc)}, 400)
            if path.startswith('/api/material/'):
                try: return self.json(jobs.materials.status(path.removeprefix('/api/material/')))
                except ExportError as exc: return self.json({'error': str(exc)}, 404)
            if path.startswith('/api/preparation/'):
                try:
                    return self.json(jobs.preparation(path.removeprefix('/api/preparation/')))
                except ExportError as exc:
                    return self.json({'error': str(exc)}, 400)
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
            if path.startswith('/api/recovery/'):
                try:
                    return self.json(jobs.recovery_plan(path.removeprefix('/api/recovery/')))
                except ExportError as exc:
                    return self.json({'error': str(exc)}, 400)
            if path.startswith("/static/"):
                target = (ROOT / "static" / path.removeprefix("/static/")).resolve()
                if not target.is_relative_to((ROOT / "static").resolve()):
                    return self.json({"error": "Нет доступа."}, 403)
                return self.file(target)
            if path.startswith("/archive/"):
                relative = Path(path.removeprefix("/archive/"))
                target = (jobs.output / relative).resolve()
                icon = relative.parts[1:] in {("assets", "favicon.svg"), ("assets", "favicon.ico")}
                if any(part.startswith(".") for part in relative.parts) or not target.is_relative_to(jobs.output) or (not icon and target.suffix not in {".html", ".css", ".js", ".json", ".jpg", ".png", ".gif", ".webp", ".avif", ".mp4", ".webm", ".ogv", ".mov", ".m4v"}):
                    return self.json({"error": "Нет доступа."}, 403)
                if len(relative.parts) == 2 and target.suffix == '.html':
                    try:
                        preparation = jobs.prepare_format(relative.parts[0])
                        if preparation:
                            from .render import ENV
                            body = ENV.get_template('preparing.html').render(app=True, source=True,
                                user='', report={}, slug=relative.parts[0]).encode('utf-8')
                            self.response_headers(200, 'text/html; charset=utf-8', len(body))
                            self.wfile.write(body)
                            return
                    except (ExportError, OSError, ValueError):
                        return self.json({"error": "Не удалось обновить оформление архива. Проверьте папку и свободное место."}, 409)
                snapshot = jobs.snapshot()
                active = snapshot.get("slug") == relative.parts[0] and snapshot.get("phase") not in TERMINAL
                if relative.parts[0] != '_demo' and relative.parts[1:] in {
                    ('statistics.html',), ('discussions.html',), ('assets', 'statistics-data.js'),
                    ('assets', 'discussions-data.js'), ('data', 'statistics.json')}:
                    from .statistics import statistics
                    from .live_statistics import public_statistics, discussion_statistics
                    from .storage import dumps
                    from .render import ENV
                    try:
                        result = jobs.statistics(relative.parts[0])
                    except ExportError as exc:
                        return self.json({'error': str(exc)}, 400)
                    data = result['data'] or statistics({})
                    if target.name == 'statistics.json':
                        if result['data'] is None:
                            return self.json({'error': 'Статистика пересчитывается.'}, 503)
                        return self.json(data)
                    if target.suffix == '.js':
                        name = 'ARCHIVE_STATS' if target.name == 'statistics-data.js' else 'ARCHIVE_DISCUSSIONS'
                        payload = public_statistics(data) if name == 'ARCHIVE_STATS' else discussion_statistics(data)
                        body = ('window.' + name + '=' + dumps(payload).replace('<', '\\u003c') + ';').encode('utf-8')
                        mime = 'text/javascript'
                    else:
                        body = ENV.get_template(target.name).render(user=data.get('journal', {}).get('username') or relative.parts[0],
                            report={'status': 'partial', 'created_at': data.get('generated_at', '')}, stats=data).encode('utf-8')
                        body = body.replace(b'<body>', b'<body data-library-url="/">', 1)
                        mime = 'text/html'
                    self.response_headers(200, mime + '; charset=utf-8', len(body))
                    self.wfile.write(body)
                    return
                if len(relative.parts) == 3 and relative.parts[1] == 'assets' and relative.parts[2] in {'statistics.js', 'statistics-live.js', 'discussions.js'}:
                    return self.file(ROOT / 'static' / relative.parts[2])
                if len(relative.parts) == 3 and relative.parts[1] == "assets" and relative.parts[2] in {"archive.css", "archive.js", "theme.js", "common.js", "timeline.js", "calendar.js", "calendar.css", "material-actions.js", "profile.js", "favicon.svg", "favicon.ico"}:
                    if active or not target.exists():
                        return self.file(ROOT / "static" / relative.parts[2])
                live_data = len(relative.parts) == 3 and relative.parts[1] == 'assets' and target.name in ('search-data.js', 'calendar-data.js')
                live_feed = len(relative.parts) == 4 and relative.parts[1:3] == ('assets', 'posts') and target.stem.isdigit() and target.suffix == '.js'
                live_page = (len(relative.parts) == 3 and relative.parts[1] == "entries" and target.stem.isdigit() and target.suffix == ".html") or (len(relative.parts) == 2 and target.name in ("index.html", "timeline.html", "calendar.html"))
                if (active or not target.exists()) and (live_page or live_data or live_feed):
                    from .storage import Store
                    from .progress import preview_entry
                    from .render import ENV
                    from .live import preview_index, preview_search, preview_feed
                    import sqlite3
                    try:
                        store = Store.reader(jobs.output / relative.parts[0])
                        try:
                            if live_feed:
                                html = preview_feed(store, int(target.stem))
                            elif live_data:
                                if target.name == 'search-data.js':
                                    html = preview_search(store)
                                else:
                                    from .calendar import calendar_script
                                    html = calendar_script(store)
                            elif relative.parts[1] == "entries":
                                html = preview_entry(store, int(target.stem))
                            elif target.name == 'index.html':
                                html = preview_index(store)
                            elif target.name == 'calendar.html':
                                html = ENV.get_template('calendar.html').render(user=store.get('username'), report={'created_at': store.get('started_at', '')})
                            else:
                                html = ENV.get_template("timeline.html").render(user=store.get("username"), report={"created_at": store.get("started_at", "")}, source="/api/timeline/" + relative.parts[0])
                        finally:
                            store.close()
                        if html:
                            script = live_data or live_feed
                            body = (html if script else html.replace('<body>', '<body data-library-url="/">', 1)).encode("utf-8")
                            self.response_headers(200, ('text/javascript' if script else 'text/html') + '; charset=utf-8', len(body))
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
                        if jobs.archive_busy(slug) or jobs.materials.running:
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
                    if jobs.archive_busy(slug) or jobs.materials.running:
                        return self.json({"error": "Дождитесь окончания выгрузки перед скачиванием ZIP."}, 409)
                    try:
                        jobs.refresh_format(slug)
                    except (ExportError, OSError, ValueError):
                        return self.json({"error": "Не удалось обновить оформление архива."}, 409)
                    with TemporaryFile() as buffer:
                        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                            for member in folder.rglob("*"):
                                if member.is_file() and not member.is_symlink() and member.suffix not in (".sqlite3", ".tmp", ".lock") and member.relative_to(folder).parts[0] not in ("quarantine", ".video-work") and member.resolve().is_relative_to(folder):
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
                if self.path == '/api/profile':
                    return self.json(jobs.materials.start({**data, 'kind': 'profile', 'entry_id': 0, 'value': 'profile'}), 202)
                if self.path == '/api/material':
                    return self.json(jobs.materials.start(data), 202)
                if self.path == '/api/material/cancel':
                    return self.json(jobs.materials.stop(data.get('id')))
                if self.path == "/api/storage/move":
                    return self.json(jobs.move(data.get("path")), 202)
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
                        jobs.materials.shutdown()
                    def shutdown():
                        if jobs.thread and jobs.thread.is_alive():
                            jobs.thread.join()
                        if jobs.move_thread and jobs.move_thread.is_alive():
                            jobs.move_thread.join()
                        for thread in jobs.preparation_threads:
                            thread.join()
                        if jobs.materials.thread: jobs.materials.thread.join()
                        jobs.sessions.clear()
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
        server.jobs.stopping = True
        server.jobs.materials.shutdown()
        if server.jobs.thread and server.jobs.thread.is_alive():
            print("Сохраняем прогресс…", flush=True)
            server.jobs.thread.join(timeout=65)
    finally:
        if server.jobs.move_thread and server.jobs.move_thread.is_alive():
            server.jobs.move_thread.join()
        for thread in server.jobs.preparation_threads:
            thread.join()
        if server.jobs.materials.thread: server.jobs.materials.thread.join()
        server.jobs.sessions.clear()
        server.server_close()
