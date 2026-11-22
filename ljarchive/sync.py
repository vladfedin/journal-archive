"""Resumable, non-destructive updates of one archive by dates, URL or full audit."""
import json
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .client import HTTPRateLimited, APIRateLimited, Cancelled, ExportError, IncompleteData, LiveJournalClient, RequestBudget, username
from .exporter import Exporter, now
from .entries import is_repost
from .storage import atomic_write, dumps
from .sources import archive_base, bind_source, journal_base, source
from .retry import restore_cooldowns
from .gaps import ATTEMPTS, closed, fail, reopen


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
            raise ExportError("Укажите от 1 до 100 URL записей выбранного журнала.")
        for url in urls:
            url_id(user, url)
    return Scope(mode, start if mode == "period" else None, end if mode == "period" else None,
                 urls if mode == "urls" else ())


def url_id(user, url):
    try:
        parts = urlsplit(url)
        host = user.replace("_", "-") + ".livejournal.com"
        path = parts.path
        hosts = (host, user + ".livejournal.com")
        if journal_base(user).startswith("https://users.livejournal.com/") and parts.hostname == "users.livejournal.com":
            hosts = ("users.livejournal.com",)
            if not path.startswith("/" + user + "/"):
                raise ValueError()
            path = path[len(user) + 1:]
        match = re.fullmatch(r"/([1-9][0-9]*)\.html", path)
        if parts.scheme not in ("http", "https") or parts.hostname not in hosts or parts.username or parts.password or parts.port not in (None, 80, 443) or not match:
            raise ValueError()
        did = int(match[1])
        if did // 256 <= 0:
            raise ValueError()
        return did
    except (ValueError, TypeError):
        raise ExportError("Нужен URL записи выбранного журнала: https://имя.livejournal.com/12345.html. Параметр thread допустим.") from None


class SyncExporter(Exporter):
    def __init__(self, folder, client, scope, *, images=False, progress=None, account=False, messages=False, videos=False, resume=False, include_comments=True):
        super().__init__(folder, client, images=images, progress=progress, account=account, messages=messages, videos=videos, include_comments=include_comments)
        self.scope = scope
        self.selected_ids = set()
        self.state = {}
        self.resume_requested, self.resuming = resume, False

    def checkpoint(self, **changes):
        self.state.update(changes)
        with self.store.db:
            self.store.set("sync_job", self.state)

    def prepare(self):
        store = self.store
        with store.db:
            bind_source(store, self.client)
        old = store.get("sync_job", {})
        # Older writers left a finished run with optional skips at the render checkpoint.
        if old.get('phase') == 'render' and not self.resume_requested and not old.get('done'):
            try:
                previous = json.loads((self.folder / 'export-report.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                previous = {}
            saved = previous.get('sync') or {}
            finished = previous.get('status') == 'complete' or previous.get('finished_with_skips')
            if (finished and saved.get('phase') == 'render' and not saved.get('done')
                    and saved.get('scope') == old.get('scope') and saved.get('started_at') == old.get('started_at')):
                old = {}
        if old and (self.resume_requested or not old.get("done")) and old.get("scope") == self.scope.data() and old.get("include_comments", True) == self.include_comments:
            self.state = old
            self.resuming = True
            self.state['done'] = False
            if self.state['phase'] == 'done':
                self.state['phase'] = 'comments'
        else:
            self.state = {"scope": self.scope.data(), "include_comments": self.include_comments, "started_at": now(), "phase": "posts", "done": False,
                          "calendar_loaded": False, "seen": [], "unavailable": [], "url_done": [], "absent_checked": []}
            with store.db:
                store.db.execute("DELETE FROM days")
                for key, value in (("days_loaded", False), ("posts_done", False), ("bodies_done", False),
                                   ("meta_done", False), ("sync_reaction_counts", []),
                                   ("sync_candidates", []), ("sync_cursor", "1970-01-01 00:00:00"), ("sync_scan_done", False)):
                    store.set(key, value)
                store.set("sync_job", self.state)
            if self.scope.mode == "urls":
                self.reopen_entries({url_id(self.client.user, url) // 256 for url in self.scope.urls})
        with store.db:
            store.set("username", self.client.user)
            store.set("entry_limit", None)
            if self.reader:
                store.set('comments_requested', self.include_comments)
            store.set("started_at", self.state["started_at"])
            store.set('sync_job', self.state)
            store.set("journal_id", getattr(self.client, "journal_id", None) or store.get("journal_id"))

    def refresh_calendar(self):
        """On resume discover new posts without fetching unchanged days again."""
        if not self.resuming or self.scope.mode == 'urls' or not self.state.get('calendar_loaded'):
            return
        self.update('posts', 'Проверяем свежие записи…')
        days = self.client.call('getdaycounts', refresh=True).get('daycounts')
        if not isinstance(days, list):
            raise ExportError('ЖЖ не вернул календарь для проверки свежих записей.')
        old = {r['day']: r['expected'] for r in self.store.db.execute('SELECT day,expected FROM days')}
        with self.store.db:
            for day in days:
                value = date.fromisoformat(day['date']).isoformat()
                expected = int(day['count'])
                if self.scope.includes(value) and expected > 0 and old.get(value) != expected:
                    self.store.db.execute('INSERT OR REPLACE INTO days(day,expected,done) VALUES (?,?,0)', (value, expected))
                    self.store.set('sync_scan_done', False)
        if self.store.db.execute('SELECT 1 FROM days WHERE done!=1').fetchone():
            self.checkpoint(phase='posts')

    def reopen_entries(self, ids):
        """An explicit refresh of these entries gives their closed gaps new attempts."""
        store = self.store
        statuses, materials = store.get("entry_comments", {}), store.get("entry_materials", {})
        errors = store.get("media_errors", {})
        videos = store.get('videos', {})
        counters = store.get('reaction_count_attempts', {})
        targets = {('post', eid) for eid in ids}
        targets.update(('comment', c['id']) for c in store.comments() if int(c.get('jitemid', 0)) in ids)
        target_names = {f'{kind}:{oid}' for kind, oid in targets}
        for eid in ids:
            if closed(statuses.get(str(eid))):
                reopen(statuses[str(eid)])
            for url in materials.get(str(eid), {}).get("urls", []):
                if url in errors:
                    reopen(errors[url])
        for record in videos.values():
            if ids.intersection(record.get('entries', [])):
                reopen(record)
        with store.db:
            store.set("entry_comments", statuses)
            store.set("media_errors", errors)
            store.set('videos', videos)
            for kind in ('post', 'comment'):
                for oid, record in store.reactions(kind).items():
                    if (kind, oid) in targets and record.get('authors'):
                        reopen(record['authors'])
                        store.save_reactions(kind, oid, record)
            store.set('reaction_count_attempts', {key: value for key, value in counters.items()
                      if key not in target_names})

    def accept(self, entry):
        if not isinstance(entry, dict) or "event" not in entry or type(entry.get("itemid")) is not int or not isinstance(entry.get("eventtime"), str):
            raise ExportError("ЖЖ вернул запись без ID, даты или текста.")
        if self.scope.mode == "period" and not self.scope.includes(entry["eventtime"][:10]):
            return
        with self.store.db:
            self.store.save_entry(entry)
            if entry["itemid"] not in self.state["seen"]:
                statuses = self.store.get("entry_comments", {})
                statuses[str(entry["itemid"])] = {"next_page": 1, "done": False}
                self.store.set("entry_comments", statuses)
        attempts = dict(self.state.get("unavailable_attempts", {}))
        attempts.pop(str(entry["itemid"]), None)
        self.checkpoint(seen=sorted(set(self.state["seen"]) | {entry["itemid"]}),
                        unavailable=sorted(set(self.state["unavailable"]) - {entry["itemid"]}),
                        unavailable_attempts=attempts)

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
            attempts = dict(self.state.get("unavailable_attempts", {}))
            attempts[str(eid)] = attempts.get(str(eid), 1 if eid in self.state["unavailable"] else 0) + 1
            self.checkpoint(unavailable=sorted(set(self.state["unavailable"]) | {eid}), unavailable_attempts=attempts)
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
        elif self.reader:
            self.reader_posts()
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
            # Calendar first, then each day's posts with their discussions.
            for day in store.db.execute("SELECT * FROM days WHERE done!=1 ORDER BY day").fetchall():
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
                    if not (self.resuming and entry.get('itemid') in self.state['seen']):
                        self.accept(entry)
                complete = len({e["itemid"] for e in entries}) == day["expected"]
                with store.db:
                    store.db.execute("UPDATE days SET done=? WHERE day=?", (1 if complete else 2, day["day"]))
                previous_selection = self.selected_ids
                self.selected_ids = {int(e["itemid"]) for e in entries}
                try:
                    self.recent_comments()
                except IncompleteData:
                    pass  # Whole-journal repair passes revisit only the pending discussions.
                finally:
                    self.selected_ids = previous_selection
                    with store.db:
                        store.set("bodies_done", False)
                        store.set("meta_done", False)

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

    def reader_posts(self):
        community = self.client.source.get('is_community')
        cursor = self.state.get('reader_cursor', {})
        if not cursor and community is False and self.scope.end and self.scope.end != '9999-12-31':
            after_end = date.fromisoformat(self.scope.end) + timedelta(days=1)
            cursor = {'beforedate': f'{after_end} 00:00:00'}
        while True:
            self.client.check()
            self.update('posts', f'Читаем журнал · сохранено записей: {len(self.state["seen"])}')
            result = self.client.call('getevents', selecttype='lastn', howmany=50, lineendings='unix',
                                      noprops=0, truncate=0, **cursor)
            events = result.get('events')
            if not isinstance(events, list):
                raise ExportError('ЖЖ не вернул ленту записей.')
            if not events:
                break
            times = []
            batch = []
            for entry in events:
                if not isinstance(entry, dict) or type(entry.get('itemid')) is not int or 'event' not in entry or not isinstance(entry.get('eventtime'), str):
                    raise ExportError('ЖЖ вернул запись без ID, даты или текста.')
                if community is None and entry.get('logtime') != entry['eventtime']:
                    raise ExportError('ЖЖ не сообщил тип журнала. Дату для продолжения ленты определить не удалось.')
                stamp = entry.get('logtime' if community else 'eventtime')
                try:
                    stamp = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S')
                except (ValueError, TypeError) as exc:
                    raise ExportError('ЖЖ не вернул дату для продолжения чтения ленты.') from exc
                times.append(stamp)
                if self.scope.includes(entry['eventtime'][:10]):
                    batch.append(entry)
            seen = set(self.state['seen'])
            statuses = self.store.get('entry_comments', {})
            with self.store.db:
                for entry in batch:
                    eid = entry['itemid']
                    self.store.save_entry(entry)
                    if eid not in seen:
                        statuses[str(eid)] = {'done': False, 'next_page': 1}
                        seen.add(eid)
                self.store.set('entry_comments', statuses)
                self.checkpoint(seen=sorted(seen))
            oldest = min(times)
            if community is False and self.scope.start and oldest.date().isoformat() < self.scope.start:
                break
            # lastn uses a strict upper bound. Skip only the already read rows in
            # the boundary second so simultaneous posts can span multiple pages.
            page_ids = sorted(e['itemid'] for e in events)
            before = (oldest + timedelta(seconds=1)).isoformat(sep=' ')
            skip = times.count(oldest)
            if cursor.get('beforedate') == before:
                skip += cursor.get('skip', 0)
            if (cursor.get('beforedate', before) < before or skip > 500
                    or self.state.get('reader_page') == page_ids):
                raise ExportError('ЖЖ не подтвердил следующую страницу ленты. Позиция сохранена.')
            cursor = {'beforedate': before, 'skip': skip}
            self.checkpoint(reader_cursor=cursor, reader_page=page_ids)
        seen = set(self.state['seen'])
        with self.store.db:
            self.store.mark_absent_entries(seen, includes=self.scope.includes)

    def open_unavailable(self):
        attempts = self.state.get("unavailable_attempts", {})
        return [eid for eid in self.state.get("unavailable", []) if attempts.get(str(eid), 1) < ATTEMPTS]

    def unavailable_posts(self):
        # An entry LJ does not return after the last attempt is treated as deleted or hidden.
        for eid in self.open_unavailable():
            self.fetch_one(eid)
        if self.open_unavailable():
            raise IncompleteData("ЖЖ не вернул часть записей. Ранее сохранённые тексты оставлены в архиве.")
        self.selected_ids = set(self.state["seen"])
        self.checkpoint(selected=sorted(self.selected_ids))

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
                raise IncompleteData(f"Полнота дня {day['day']} пока не подтверждена.")
            with store.db:
                store.db.execute("UPDATE days SET done=1 WHERE day=?", (day["day"],))

    def comments(self):
        if self.reader:
            self.recent_comments()
            self.checkpoint(phase='reactions')
            return
        failure = None
        try:
            self.recent_comments()
        except IncompleteData as exc:
            failure = exc
        statuses = self.store.get("entry_comments", {})
        missing = []
        incomplete_entries = set()
        comments = self.store.comments()
        screened_ids = {c["id"] for c in comments if c["state"] == "S"}
        for item in comments:
            eid = int(item.get("jitemid", 0))
            if eid not in self.selected_ids or not statuses.get(str(eid), {}).get("done") or statuses.get(str(eid), {}).get("skipped") == "repost":
                continue
            if item["id"] not in statuses[str(eid)].get("seen_ids", []):
                continue
            if ("body" not in item or item.get("source_status") == "body_not_returned") and item.get("state") not in ("D", "X"):
                missing.append(item["id"])
                incomplete_entries.add(eid)
        for entry in self.store.entries():
            eid = int(entry["itemid"])
            if is_repost(entry):
                continue
            if not statuses.get(str(eid), {}).get("done"):
                continue
            if eid in self.selected_ids and len(statuses[str(eid)].get("seen_ids", [])) < int(entry.get("reply_count", 0) or 0):
                missing.append(f"entry-{eid}")
                incomplete_entries.add(eid)
            if eid in self.selected_ids and entry.get("props", {}).get("hasscreened") in (1, "1", True):
                fresh_ids = set(statuses[str(eid)].get("seen_ids", []))
                if not fresh_ids & screened_ids:
                    missing.append(f"screened-{eid}")
                    incomplete_entries.add(eid)
        if missing:
            for eid in incomplete_entries:
                previous = statuses.get(str(eid), {})
                statuses[str(eid)] = fail({"next_page": 1, "done": False,
                                           "error": "Не все комментарии получены."}, previous)
            if all(closed(statuses[str(eid)]) for eid in incomplete_entries):
                with self.store.db:
                    self.store.set("entry_comments", statuses)
                if failure:
                    raise failure
                self.checkpoint(phase="reactions")
                return
            with self.store.db:
                self.store.set("entry_comments", statuses)
                self.store.set("bodies_done", False)
            raise IncompleteData(f"Не подтверждена полнота комментариев: {len(missing)} пропусков. Следующий запуск повторит проверку веток.")
        if failure:
            raise failure
        self.checkpoint(phase="reactions")

    def report(self, error=None, cancelled=False):
        counts = self.store.counts()
        statuses = self.store.get("entry_comments", {})
        selected = self.selected_ids or set(self.state.get("selected", self.state.get("seen", [])))
        comments = self.store.comments()
        missing = [{"id": c["id"], "state": c["state"]} for c in comments if "body" not in c and c["state"] not in ("D", "X")
                   and c.get('source_status') != 'hidden_from_reader']
        unavailable = self.open_unavailable()
        issues = ([error] if error else []) + self.access_issues()
        if self.state.get('phase') == 'reactions':
            issues.extend('Реакции: ' + issue for issue in self.store.get('reaction_issues', []))
        if unavailable:
            issues.append(f"ЖЖ не вернул {len(unavailable)} записей. Ранее сохранённые тексты оставлены в архиве.")
        absent = sorted(set(self.state.get("unavailable", [])) - set(unavailable))
        if self.state.get("phase") not in ("render", "done") and not error:
            issues.append("Обновление ещё не завершено. Повторный запуск того же режима продолжит очередь.")
        return {"username": self.client.user, "created_at": now(), "started_at": self.state.get("started_at"),
                "status": "cancelled" if cancelled else "partial" if issues else "complete",
                "entries_expected": counts["entries"], "entries_saved": counts["entries"],
                "comments_saved": counts["comments"], "comments_metadata": counts["comment_meta"],
                "screened_comments": sum(c["state"] == "S" for c in comments),
                "deleted_comments": sum(c["state"] in ("D", "X") for c in comments), "missing_comments": missing,
                "pending_days": [dict(r) for r in self.store.db.execute("SELECT day,expected FROM days WHERE done!=1")],
                "orphan_entry_ids": [], "pending_comment_entry_ids": [eid for eid in sorted(selected) if not statuses.get(str(eid), {}).get("done") and not closed(statuses.get(str(eid)))],
                "unavailable_comment_entry_ids": [eid for eid in sorted(selected) if closed(statuses.get(str(eid)))],
                "unavailable_entry_ids": absent, "unknown_encoding_entry_ids": [],
                "entry_limit": None, "issues": issues, "images_requested": self.images, "videos_requested": self.videos,
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
        text_processed = False
        try:
            self.prepare()
            restore_cooldowns(self.client, store)
            try:
                if self.scope.mode == "full":
                    audit = verify_archive(store, progress=self.progress, check=self.client.check)
                    with store.db:
                        store.set("integrity_before", audit)
                    for issue in audit["issues"]:
                        if issue.get("kind") == "file" and Path(issue.get("path", "")).parts[:1] == ("media",):
                            path = store.folder / issue["path"]
                            if path.is_file():
                                quarantine = store.folder / "quarantine" / path.name
                                quarantine.parent.mkdir(exist_ok=True)
                                path.replace(quarantine)
                self.update("login", "Входим в ЖЖ и проверяем доступ…")
                try:
                    self.client.login()
                finally:
                    with store.db:
                        bind_source(store, self.client)
                with store.db:
                    store.set("journal_id", self.client.journal_id)
                    if getattr(self.client, "userpics", None) is not None:
                        store.set("userpic_catalog", self.client.userpics)
                self.refresh_feed_pin(before_posts=True)
                self.refresh_calendar()
                failures = []
                def stage(operation, label):
                    try:
                        self.retry_missing(operation, label)
                    except IncompleteData as exc:
                        failures.append(str(exc))
                if self.state["phase"] == "posts":
                    stage(self.posts, "записи")
                self.selected_ids = set(self.state.get("selected", self.state.get("seen", [])))
                if self.state.get("unavailable"):
                    stage(self.unavailable_posts, "записи")
                if self.state["phase"] == "comments" or (self.state["phase"] == "posts" and self.selected_ids):
                    stage(self.comments, "комментарии")
                personal = self.personal()
                if self.state["phase"] in ("comments", "reactions"):
                    try:
                        if self.reactions(entry_ids=self.selected_ids, checkpoint="sync_reaction_counts") and not failures:
                            self.checkpoint(phase="render")
                    except IncompleteData as exc:
                        failures.append(str(exc))
                if store.db.execute("SELECT 1 FROM days WHERE done!=1").fetchone():
                    self.checkpoint(phase="posts")
                text_processed = True
                self.material_stage(personal)
                self.refresh_feed_pin(before_posts=False)
                error = " ".join(failures) or None
            except Cancelled as exc:
                error, cancelled = str(exc), True
            except (HTTPRateLimited, APIRateLimited) as exc:
                text_processed, error = False, str(exc)
            except ExportError as exc:
                error = str(exc)
            with store.db:
                identity = bind_source(store, self.client)
            report = self.report(error, cancelled)
            report['source'] = identity
            if self.reader:
                report['comments_requested'] = self.include_comments
                report['scope'] = {'full': 'Все доступные записи', 'period': 'Доступные записи за выбранный период',
                                   'urls': 'Записи по указанным ссылкам'}[self.scope.mode] + f' · читатель {self.client.account}.'
            report = build_archive(store, report, images=self.images and (text_processed and not cancelled), userpics=(text_processed and not cancelled),
                                   client_cancel=self.client.cancel, progress=self.progress, budget=self.client.budget, media=getattr(self, "_media", None), client=self.client)
            complete = self.state["phase"] == "render" and not error and (report["status"] == "complete" or report.get("finished_with_skips"))
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
            if getattr(self, "_media", None):
                self._media.close()
            self.client.close()
            if getattr(self, "_videos", None): self._videos.close()
            store.close()


def sync(user, password, output, *, mode="full", start=None, end=None, urls=None, images=False,
         delay=3, max_requests=None, cancel=None, progress=None, archive=None, account=False, messages=False, videos=False,
         journal=None, source_type=None, session_ready=None, resume=False, include_comments=True):
    user = username(user)
    identity = source(user, journal, source_type)
    scope = validate_scope(identity['username'], mode, start, end, urls)
    slug = target_slug(archive_base(identity), archive)
    budget = RequestBudget(max_requests, progress)
    client = LiveJournalClient(user, password, delay=delay, cancel=cancel, budget=budget, progress=progress, journal=journal, source_type=source_type, session_ready=session_ready)
    return SyncExporter(Path(output) / slug, client, scope, images=images, progress=progress, account=account, messages=messages, videos=videos, resume=resume, include_comments=include_comments).run()


def target_slug(user, archive=None):
    if not archive:
        return user
    if archive != user and not re.fullmatch(re.escape(user) + r"-latest-(?:[1-9]|[1-4][0-9]|50)", archive):
        raise ExportError("Выберите архив этого же пользователя.")
    return archive
