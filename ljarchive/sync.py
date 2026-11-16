"""Resumable, non-destructive updates of one archive by dates, URL or full audit."""
import json
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .client import Cancelled, ExportError, LiveJournalClient, RequestBudget, username
from .exporter import Exporter, now
from .reactions import capture_reactions
from .storage import atomic_write, dumps


@dataclass(frozen=True)
class Scope:
    mode: str = "full"
    start: str | None = None
    end: str | None = None
    urls: tuple[str, ...] = ()

    def includes(self, day):
        return (not self.start or day >= self.start) and (not self.end or day <= self.end)

    def data(self):
        return {**asdict(self), "urls": list(self.urls)}


def validate_scope(user, mode="full", start=None, end=None, urls=None):
    user = username(user)
    if mode not in ("full", "period", "urls"):
        raise ExportError("Выберите весь журнал, период или URL записей.")
    for value in (start, end):
        if value:
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ExportError("Дата должна иметь вид ГГГГ-ММ-ДД.") from None
    if start and end and start > end:
        raise ExportError("Начало периода позже его окончания.")
    if mode == "period" and not (start or end):
        raise ExportError("Укажите начало или конец периода.")
    if isinstance(urls, str):
        urls = urls.split()
    if urls is not None and (not isinstance(urls, (list, tuple)) or any(not isinstance(url, str) for url in urls)):
        raise ExportError("URL должны быть списком строк или текстом по одному адресу на строке.")
    urls = tuple(dict.fromkeys(urls or ()))
    if mode == "urls":
        if not urls or len(urls) > 100:
            raise ExportError("Укажите от 1 до 100 URL записей своего журнала.")
        for url in urls:
            url_id(user, url)
    return Scope(mode, start if mode == "period" else None, end if mode == "period" else None,
                 urls if mode == "urls" else ())


def url_id(user, url):
    try:
        parts = urlsplit(url)
        host = user.replace("_", "-") + ".livejournal.com"
        match = re.fullmatch(r"/([1-9][0-9]*)\.html", parts.path)
        if parts.scheme not in ("http", "https") or parts.hostname not in (host, user + ".livejournal.com") or parts.username or parts.password or parts.port not in (None, 80, 443) or not match:
            raise ValueError()
        did = int(match[1])
        if did // 256 <= 0:
            raise ValueError()
        return did
    except (ValueError, TypeError):
        raise ExportError("Нужен URL записи своего журнала: https://имя.livejournal.com/12345.html. Параметр thread допустим.") from None


class SyncExporter(Exporter):
    def __init__(self, folder, client, scope, *, images=False, progress=None):
        super().__init__(folder, client, images=images, progress=progress)
        self.scope = scope
        self.selected_ids = set()
        self.state = {}

    def checkpoint(self, **changes):
        self.state.update(changes)
        with self.store.db:
            self.store.set("sync_job", self.state)

    def prepare(self):
        store = self.store
        if store.get("username") not in (None, self.client.user):
            raise ExportError("Архив принадлежит другому журналу.")
        old = store.get("sync_job", {})
        if old and not old.get("done") and old.get("scope") == self.scope.data():
            self.state = old
        else:
            self.state = {"scope": self.scope.data(), "started_at": now(), "phase": "posts", "done": False,
                          "calendar_loaded": False, "seen": [], "unavailable": [], "url_done": [], "absent_checked": []}
            with store.db:
                store.db.execute("DELETE FROM days")
                for key, value in (("days_loaded", False), ("posts_done", False), ("bodies_done", False),
                                   ("meta_done", False), ("entry_comments", {}), ("sync_reaction_counts", []),
                                   ("sync_candidates", []), ("sync_cursor", "1970-01-01 00:00:00"), ("sync_scan_done", False)):
                    store.set(key, value)
                store.set("sync_job", self.state)
        with store.db:
            store.set("username", self.client.user)
            store.set("entry_limit", None)
            store.set("started_at", self.state["started_at"])
            store.set("journal_id", getattr(self.client, "journal_id", None) or store.get("journal_id"))

    def accept(self, entry):
        if not isinstance(entry, dict) or "event" not in entry or type(entry.get("itemid")) is not int or not isinstance(entry.get("eventtime"), str):
            raise ExportError("ЖЖ вернул запись без ID, даты или текста.")
        if self.scope.mode == "period" and not self.scope.includes(entry["eventtime"][:10]):
            return
        with self.store.db:
            self.store.save_entry(entry)
        self.checkpoint(seen=sorted(set(self.state["seen"]) | {entry["itemid"]}))

    def fetch_one(self, eid, expected_did=None):
        result = self.client.call("getevents", selecttype="one", itemid=eid, lineendings="unix", noprops=0, truncate=0)
        events = result.get("events")
        if not isinstance(events, list):
            raise ExportError("ЖЖ не вернул список записей при обновлении по ID.")
        for entry in events:
            if entry.get("itemid") != eid:
                raise ExportError("ЖЖ вернул запись с другим ID.")
            if expected_did and int(entry.get("ditemid", eid * 256 + int(entry.get("anum", -1)))) != expected_did:
                raise ExportError("ID в URL не совпадает с записью, которую вернул ЖЖ.")
            self.accept(entry)
        if not events:
            self.checkpoint(unavailable=sorted(set(self.state["unavailable"]) | {eid}))
            row = self.store.db.execute("SELECT payload FROM entries WHERE id=?", (eid,)).fetchone()
            if row:
                entry = json.loads(row[0])
                entry["source_status"] = "not_returned"
                with self.store.db:
                    self.store.save_entry(entry)

    def posts(self):
        store = self.store
        if self.scope.mode == "urls":
            for url in self.scope.urls:
                did = url_id(self.client.user, url)
                if did not in self.state["url_done"]:
                    self.update("posts", f"Обновляем запись по URL · #{did // 256}")
                    self.fetch_one(did // 256, did)
                    self.checkpoint(url_done=self.state["url_done"] + [did])
        else:
            if not self.state["calendar_loaded"]:
                self.update("posts", "Получаем календарь и выбираем дни…")
                days = self.client.call("getdaycounts").get("daycounts")
                if not isinstance(days, list):
                    raise ExportError("ЖЖ не вернул календарь.")
                with store.db:
                    for day in days:
                        value = date.fromisoformat(day["date"]).isoformat()
                        if self.scope.includes(value):
                            store.db.execute("INSERT OR REPLACE INTO days(day,expected) VALUES (?,?)", (value, int(day["count"])))
                    store.set("days_loaded", True)
                self.checkpoint(calendar_loaded=True)
            for day in store.db.execute("SELECT * FROM days WHERE done=0 ORDER BY day DESC").fetchall():
                self.update("posts", f"Записи за {day['day']}…")
                year, month, number = map(int, day["day"].split("-"))
                result = self.client.call("getevents", selecttype="day", year=year, month=month, day=number,
                                          lineendings="unix", noprops=0, truncate=0)
                entries = result.get("events")
                if not isinstance(entries, list):
                    raise ExportError("ЖЖ не вернул записи за день.")
                for entry in entries:
                    if entry.get("eventtime", "")[:10] != day["day"]:
                        raise ExportError("ЖЖ вернул запись за другой день.")
                    self.accept(entry)
                complete = len({e["itemid"] for e in entries}) == day["expected"]
                with store.db:
                    store.db.execute("UPDATE days SET done=? WHERE day=?", (1 if complete else 2, day["day"]))
            if store.db.execute("SELECT 1 FROM days WHERE done=2").fetchone():
                self.reconcile_posts()
            # Recheck saved entries absent from the current calendar response. Keep their old text.
            for entry in store.entries():
                eid = int(entry["itemid"])
                if self.scope.includes(entry.get("eventtime", "")[:10]) and eid not in self.state["seen"] and eid not in self.state["absent_checked"]:
                    self.fetch_one(eid)
                    self.checkpoint(absent_checked=self.state["absent_checked"] + [eid])
        self.selected_ids = set(self.state["seen"])
        with store.db:
            store.set("posts_done", True)
        self.checkpoint(phase="comments", selected=sorted(self.selected_ids))

    def reconcile_posts(self):
        """Persist discovery and individual fetches when a day response is truncated."""
        store = self.store
        while not store.get("sync_scan_done"):
            cursor = store.get("sync_cursor")
            result = self.client.call("syncitems", lastsync=cursor)
            items = result.get("syncitems")
            if not isinstance(items, list):
                raise ExportError("ЖЖ не вернул список синхронизации.")
            candidates = set(store.get("sync_candidates", []))
            for item in items:
                key = item.get("item", "")
                if re.fullmatch(r"L-[1-9][0-9]*", key) and item.get("action") != "delete":
                    eid = int(key[2:])
                    if eid not in self.state["seen"]:
                        candidates.add(eid)
            with store.db:
                store.set("sync_candidates", sorted(candidates))
            if not items:
                with store.db:
                    store.set("sync_scan_done", True)
                break
            latest = max(item["time"] for item in items)
            next_cursor = (datetime.strptime(latest, "%Y-%m-%d %H:%M:%S") - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            if next_cursor <= cursor:
                raise ExportError("Синхронизация ЖЖ не продвигается; полнота переполненного дня не подтверждена.")
            with store.db:
                store.set("sync_cursor", next_cursor)
        while store.get("sync_candidates"):
            pending = store.get("sync_candidates")
            self.fetch_one(pending[0])
            with store.db:
                store.set("sync_candidates", pending[1:])
        from collections import Counter
        counts = Counter(e.get("eventtime", "")[:10] for e in store.entries() if int(e["itemid"]) in self.state["seen"])
        for day in store.db.execute("SELECT * FROM days WHERE done=2").fetchall():
            if counts[day["day"]] != day["expected"]:
                raise ExportError(f"Полнота дня {day['day']} пока не подтверждена.")
            with store.db:
                store.db.execute("UPDATE days SET done=1 WHERE day=?", (day["day"],))

    def comments(self):
        self.recent_comments()
        statuses = self.store.get("entry_comments", {})
        missing = []
        comments = self.store.comments()
        screened_ids = {c["id"] for c in comments if c["state"] == "S"}
        with self.store.db:
            for item in comments:
                eid = int(item.get("jitemid", 0))
                if eid not in self.selected_ids:
                    continue
                if item["id"] not in statuses[str(eid)].get("seen_ids", []):
                    # Absence is not proof of deletion; retain earlier text explicitly.
                    row = self.store.db.execute("SELECT payload FROM comments WHERE id=?", (item["id"],)).fetchone()
                    original = json.loads(row[0])
                    original["source_status"] = "not_returned"
                    self.store.save_comment(original)
                elif ("body" not in item or item.get("source_status") == "body_not_returned") and item.get("state") not in ("D", "X"):
                    missing.append(item["id"])
        for entry in self.store.entries():
            eid = int(entry["itemid"])
            if eid in self.selected_ids and len(statuses[str(eid)].get("seen_ids", [])) < int(entry.get("reply_count", 0) or 0):
                missing.append(f"entry-{eid}")
            if eid in self.selected_ids and entry.get("props", {}).get("hasscreened") in (1, "1", True):
                fresh_ids = set(statuses[str(eid)].get("seen_ids", []))
                if not fresh_ids & screened_ids:
                    missing.append(f"screened-{eid}")
        if missing:
            with self.store.db:
                self.store.set("entry_comments", {})
            raise ExportError(f"Не подтверждена полнота комментариев: {len(missing)} пропусков. Следующий запуск повторит проверку веток.")
        self.checkpoint(phase="reactions")

    def save_thread(self, items, users):
        super().save_thread(items, users)
        with self.store.db:
            for item in items:
                if "body" not in item and item.get("state") not in ("D", "X"):
                    row = self.store.db.execute("SELECT payload FROM comments WHERE id=?", (item["id"],)).fetchone()
                    current = json.loads(row[0])
                    current.update(item)
                    current["source_status"] = "body_not_returned"
                    self.store.save_comment(current)

    def report(self, error=None, cancelled=False):
        counts = self.store.counts()
        comments = self.store.comments()
        missing = [{"id": c["id"], "state": c["state"]} for c in comments if "body" not in c and c["state"] not in ("D", "X")]
        unavailable = self.state.get("unavailable", [])
        issues = [error] if error else []
        if unavailable:
            issues.append(f"ЖЖ не вернул {len(unavailable)} записей. Ранее сохранённые тексты оставлены в архиве.")
        if self.state.get("phase") not in ("render", "done") and not error:
            issues.append("Обновление ещё не завершено. Повторный запуск того же режима продолжит очередь.")
        return {"username": self.client.user, "created_at": now(), "started_at": self.state.get("started_at"),
                "status": "cancelled" if cancelled else "partial" if issues else "complete",
                "entries_expected": counts["entries"], "entries_saved": counts["entries"],
                "comments_saved": counts["comments"], "comments_metadata": counts["comment_meta"],
                "screened_comments": sum(c["state"] == "S" for c in comments),
                "deleted_comments": sum(c["state"] in ("D", "X") for c in comments), "missing_comments": missing,
                "pending_days": [dict(r) for r in self.store.db.execute("SELECT day,expected FROM days WHERE done!=1")],
                "orphan_entry_ids": [], "pending_comment_entry_ids": [], "unknown_encoding_entry_ids": [],
                "entry_limit": None, "issues": issues, "images_requested": self.images,
                "scope": "Накопительный архив. Последнее обновление: " + {"full": "весь журнал с проверкой", "period": f"период {self.scope.start or 'с начала'} — {self.scope.end or 'по сегодня'}", "urls": "записи по указанным URL"}[self.scope.mode] + ". Даты относятся к публикации записей; их комментарии загружаются за всё время.",
                "sync": {**self.state, "selected_count": len(self.selected_ids)},
                "integrity_before": self.store.get("integrity_before", {}),
                "limitations": "Отсутствующие на сервере тексты восстановить нельзя. Сохранённые ранее версии не удаляются. Снимок не атомарный: журнал может изменяться во время чтения.",
                **self.client.budget.snapshot()}

    def run(self):
        from .render import build_archive
        from .integrity import verify_archive
        store = self.store
        error, cancelled = None, False
        try:
            self.prepare()
            self.client.not_before = store.get("request_not_before", 0)
            def save_cooldown(deadline):
                with store.db:
                    store.set("request_not_before", deadline)
            self.client.save_cooldown = save_cooldown
            self.client.budget.save_cooldown = save_cooldown
            self.client.budget.not_before = self.client.not_before
            if self.scope.mode == "full":
                audit = verify_archive(store)
                with store.db:
                    store.set("integrity_before", audit)
                for issue in audit["issues"]:
                    if issue.get("kind") == "file" and Path(issue.get("path", "")).parts[:1] == ("media",):
                        path = store.folder / issue["path"]
                        if path.is_file():
                            quarantine = store.folder / "quarantine" / path.name
                            quarantine.parent.mkdir(exist_ok=True)
                            path.replace(quarantine)
            try:
                self.update("login", "Входим в журнал владельца…")
                self.client.login()
                with store.db:
                    store.set("journal_id", self.client.journal_id)
                if self.state["phase"] == "posts":
                    self.posts()
                self.selected_ids = set(self.state.get("selected", []))
                if self.state["phase"] == "comments":
                    self.comments()
                if self.state["phase"] == "reactions":
                    if capture_reactions(store, self.client, self.progress, entry_ids=self.selected_ids, checkpoint="sync_reaction_counts"):
                        self.checkpoint(phase="render")
            except Cancelled as exc:
                error, cancelled = str(exc), True
            except ExportError as exc:
                error = str(exc)
            report = self.report(error, cancelled)
            report = build_archive(store, report, images=self.images and not cancelled,
                                   client_cancel=self.client.cancel, progress=self.progress, budget=self.client.budget)
            complete = self.state["phase"] == "render" and not error and (not self.images or report["media_complete"])
            if complete:
                self.checkpoint(phase="done", done=True, finished_at=now())
            elif self.images and not report["media_complete"] and report["status"] == "complete":
                report["status"] = "partial"
                report["issues"].append("Не все изображения получены. Повторный запуск продолжит с сохранённых файлов.")
            report["sync"] = {**self.state, "selected_count": len(self.selected_ids)}
            from .render import ENV
            from .integrity import write_manifest
            atomic_write(self.folder / "export-report.json", dumps(report))
            atomic_write(self.folder / "report.html", ENV.get_template("report.html").render(user=self.client.user, report=report))
            write_manifest(self.folder)
            with store.db:
                store.set("last_request_stats", self.client.budget.snapshot())
            self.update(report["status"], "Обновление сохранено.", report=report)
            return report
        finally:
            self.client.close()
            store.close()


def sync(user, password, output, *, mode="full", start=None, end=None, urls=None, images=False,
         delay=3, max_requests=None, cancel=None, progress=None, archive=None):
    user = username(user)
    scope = validate_scope(user, mode, start, end, urls)
    slug = target_slug(user, archive)
    budget = RequestBudget(max_requests, progress)
    client = LiveJournalClient(user, password, delay=delay, cancel=cancel, budget=budget, progress=progress)
    return SyncExporter(Path(output) / slug, client, scope, images=images, progress=progress).run()


def target_slug(user, archive=None):
    if not archive:
        return user
    if archive != user and not re.fullmatch(re.escape(user) + r"-latest-(?:[1-9]|[1-4][0-9]|50)", archive):
        raise ExportError("Выберите архив этого же пользователя.")
    return archive
