from __future__ import annotations

import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .client import Cancelled, ExportError, LiveJournalClient, RequestBudget, username
from .storage import Store, atomic_write, dumps


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_entry_limit(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
        raise ExportError("Число последних записей должно быть от 1 до 50.")
    return value


def archive_slug(user, entry_limit):
    return user if entry_limit is None else f"{user}-latest-{entry_limit}"


def normalize_thread(nodes, entry_id):
    """Convert the getcomments protocol's display IDs to archive IDs, iteratively."""
    result, users, stack = [], {}, list(reversed(nodes))
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            raise ExportError("ЖЖ вернул некорректный комментарий.")
        try:
            cid = int(node["dtalkid"]) // 256
            parent = int(node.get("parentdtalkid", 0)) // 256 if "parentdtalkid" in node else int(node.get("parenttalkid", 0))
            poster = int(node.get("posterid", node.get("userid", 0)) or 0)
            if cid <= 0:
                raise ValueError()
        except (KeyError, ValueError, TypeError) as exc:
            raise ExportError("ЖЖ вернул комментарий без корректного ID.") from exc
        item = {"id": cid, "jitemid": entry_id, "parentid": parent, "posterid": poster,
                "state": node.get("state") or "A", "subject": node.get("subject", ""),
                "date": node.get("datepost", ""), "props": node.get("props", {}),
                "dtalkid": int(node["dtalkid"])}
        if "body" in node and node["body"] is not None:
            item["body"] = node["body"]
        elif node.get("is_loaded", node.get("_loaded")) in (1, "1", True):
            item["body"] = ""
        name = node.get("postername", node.get("user"))
        if poster and isinstance(name, str) and name:
            users[poster] = name
        children = node.get("children") or []
        if not isinstance(children, list):
            raise ExportError("ЖЖ вернул некорректную ветку комментариев.")
        result.append(item)
        stack.extend(reversed(children))
    return result, users


class Exporter:
    def __init__(self, folder: Path, client: LiveJournalClient, *, images=True, progress=None, entry_limit=None):
        self.folder, self.client, self.images = folder, client, images
        self.progress = progress or (lambda **kwargs: None)
        self.entry_limit = validate_entry_limit(entry_limit)
        self.store = Store(folder)

    def update(self, phase, message, **extra):
        self.progress(phase=phase, message=message, **self.store.counts(), **extra)

    def posts(self):
        store = self.store
        if self.entry_limit is not None:
            return self.recent_posts()
        if not store.get("days_loaded"):
            self.update("posts", "Получаем календарь всех записей…")
            response = self.client.call("getdaycounts")
            if "daycounts" not in response or not isinstance(response["daycounts"], list):
                raise ExportError("ЖЖ не вернул календарь записей.")
            with store.db:
                for day in response["daycounts"]:
                    date = datetime.strptime(day["date"], "%Y-%m-%d").strftime("%Y-%m-%d")
                    store.db.execute("INSERT OR REPLACE INTO days(day,expected) VALUES (?,?)",
                                     (date, int(day["count"])))
                store.set("days_loaded", True)
        days = store.db.execute("SELECT * FROM days ORDER BY day").fetchall()
        total = len(days)
        for index, day in enumerate(days):
            self.client.check()
            if day["done"]:
                continue
            self.update("posts", f"Записи за {day['day']} · день {index + 1} из {total}",
                        completed=index, total=total)
            year, month, date = map(int, day["day"].split("-"))
            response = self.client.call("getevents", selecttype="day", year=year, month=month, day=date,
                                        lineendings="unix", noprops=0, prefersubject=0, truncate=0)
            if "events" not in response or not isinstance(response["events"], list):
                raise ExportError("ЖЖ не вернул список записей за день.")
            entries = response["events"]
            if any(not isinstance(e, dict) or "event" not in e or "itemid" not in e or
                   not isinstance(e.get("eventtime"), str) for e in entries):
                raise ExportError("ЖЖ вернул запись без текста, даты или ID. Этот день не отмечен завершённым.")
            ids = {int(e["itemid"]) for e in entries}
            with store.db:
                for entry in entries:
                    store.save_entry(entry)
                # A truncated day remains pending, never silently considered complete.
                valid_day = len(ids) == day["expected"] and all(e["eventtime"][:10] == day["day"] for e in entries)
                store.db.execute("UPDATE days SET done=? WHERE day=?", (valid_day, day["day"]))
            atomic_write(self.folder / "raw" / f"posts-{day['day']}.json", dumps(response))
        # In rare journals a day exceeds the server's getevents limit. Discover exact IDs
        # through syncitems, then fetch missing records individually.
        if store.db.execute("SELECT 1 FROM days WHERE done=0").fetchone():
            self.reconcile_posts()
        with store.db:
            store.set("posts_done", not bool(store.db.execute("SELECT 1 FROM days WHERE done=0").fetchone()))

    def recent_posts(self):
        store = self.store
        if store.get("posts_done"):
            return
        self.update("posts", f"Получаем последние {self.entry_limit} записей…")
        response = self.client.call("getevents", selecttype="lastn", howmany=self.entry_limit,
                                    lineendings="unix", noprops=0, prefersubject=0, truncate=0)
        entries = response.get("events")
        if not isinstance(entries, list) or any(not isinstance(e, dict) or "itemid" not in e or
                "event" not in e or not isinstance(e.get("eventtime"), str) for e in entries):
            raise ExportError("ЖЖ не вернул корректный список последних записей.")
        entries = sorted({int(e["itemid"]): e for e in entries}.values(),
                         key=lambda e: (e["eventtime"], int(e["itemid"])), reverse=True)[:self.entry_limit]
        with store.db:
            for entry in entries:
                store.save_entry(entry)
            store.set("recent_expected", self.entry_limit)
        atomic_write(self.folder / "raw" / "recent-posts.json", dumps(response))
        expected = self.entry_limit
        if len(entries) < expected:
            calendar = self.client.call("getdaycounts").get("daycounts")
            if not isinstance(calendar, list):
                raise ExportError("ЖЖ вернул меньше запрошенных записей; проверить размер журнала не удалось.")
            expected = min(expected, sum(int(d["count"]) for d in calendar))
        with store.db:
            store.set("recent_expected", expected)
            store.set("posts_done", len(entries) == expected)
        if len(entries) != expected:
            raise ExportError(f"Запрошены последние {expected} записей, ЖЖ вернул {len(entries)}.")

    def recent_comments(self):
        store = self.store
        statuses = store.get("entry_comments", {})
        entries = sorted(store.entries(), key=lambda e: (e["eventtime"], int(e["itemid"])), reverse=True)
        for entry in entries:
            eid = int(entry["itemid"])
            status = statuses.setdefault(str(eid), {"next_page": 1, "done": False})
            if status["done"]:
                continue
            page = status["next_page"]
            while True:
                self.update("comments", f"Комментарии к записи #{eid} · страница {page}")
                response = self.client.call("getcomments", journal=self.client.user, itemid=eid,
                                            page=page, page_size=500, expand_strategy="expand_all", format="thread")
                nodes = response.get("comments")
                if not isinstance(nodes, list):
                    raise ExportError(f"ЖЖ не вернул комментарии к записи #{eid}.")
                items, users = normalize_thread(nodes, eid)
                atomic_write(self.folder / "raw" / f"comments-{eid}-page-{page}.json", dumps(response))
                self.save_thread(items, users)
                try:
                    pages = int(response["pages"])
                    returned_page = int(response["page"])
                    if pages < 0 or returned_page != page and not (pages == 0 and returned_page == 0):
                        raise ValueError()
                except (KeyError, TypeError, ValueError) as exc:
                    raise ExportError(f"ЖЖ не подтвердил номер/число страниц комментариев к записи #{eid}. Полученные тексты сохранены.") from exc
                if not items and page < pages:
                    raise ExportError(f"Пустая страница комментариев к записи #{eid} до конца списка.")
                status.update(pages=pages, next_page=page + 1)
                if page >= pages:
                    # Some responses contain unexpanded placeholders even with expand_all.
                    # Retry those threads once, never silently drop their bodies.
                    missing = store.db.execute("""SELECT payload FROM comments WHERE entry_id=?
                        AND json_type(payload,'$.body') IS NULL AND json_extract(payload,'$.state') NOT IN ('D','X')""", (eid,)).fetchall()
                    import json
                    for row in missing:
                        item = json.loads(row[0])
                        # Earlier expansion may already have loaded this descendant.
                        if store.db.execute("SELECT json_type(payload,'$.body') FROM comments WHERE id=?", (item["id"],)).fetchone()[0] is not None:
                            continue
                        self.update("comments", f"Разворачиваем комментарий #{item['id']} к записи #{eid}…")
                        expanded = self.client.call("getcomments", journal=self.client.user, itemid=eid,
                                                    dtalkid=item["dtalkid"], page=1, page_size=500,
                                                    expand_strategy="expand_all", format="thread")
                        if not isinstance(expanded.get("comments"), list):
                            raise ExportError("ЖЖ не вернул развёрнутую ветку.")
                        atomic_write(self.folder / "raw" / f"comments-{eid}-thread-{item['id']}.json", dumps(expanded))
                        self.save_thread(*normalize_thread(expanded["comments"], eid))
                    status["done"] = True
                with store.db:
                    store.set("entry_comments", statuses)
                if status["done"]:
                    break
                page += 1
        with store.db:
            store.set("meta_done", True)
            store.set("bodies_done", True)

    def save_thread(self, items, users):
        store = self.store
        with store.db:
            for item in items:
                store.db.execute("INSERT OR REPLACE INTO comment_meta VALUES (?,?,?)",
                                 (item["id"], item["posterid"], item["state"]))
                previous = store.db.execute("SELECT json_type(payload,'$.body') FROM comments WHERE id=?", (item["id"],)).fetchone()
                if "body" in item or not previous or previous[0] is None:
                    store.save_comment(item)
            for uid, name in users.items():
                store.db.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (uid, name))

    def reconcile_posts(self):
        self.update("posts", "Проверяем записи по списку синхронизации ЖЖ…")
        cursor, seen = "1970-01-01 00:00:00", set()
        while True:
            self.client.check()
            result = self.client.call("syncitems", lastsync=cursor)
            items = result.get("syncitems")
            if not isinstance(items, list):
                raise ExportError("ЖЖ не вернул список синхронизации. Часть дней требует повторной проверки.")
            if not items:
                break
            for item in items:
                key = item.get("item", "")
                if not key.startswith("L-") or key in seen or item.get("action") == "delete":
                    continue
                seen.add(key)
                itemid = int(key[2:])
                if not self.store.db.execute("SELECT 1 FROM entries WHERE id=?", (itemid,)).fetchone():
                    response = self.client.call("getevents", selecttype="one", itemid=itemid, lineendings="unix")
                    with self.store.db:
                        for entry in response.get("events", []):
                            self.store.save_entry(entry)
            next_cursor = max(item["time"] for item in items)
            if next_cursor <= cursor:
                raise ExportError("Список синхронизации ЖЖ перестал продвигаться. Неполные дни отмечены в отчёте.")
            # Overlap the boundary second so entries with equal timestamps are not lost.
            from datetime import timedelta
            overlap = (datetime.strptime(next_cursor, "%Y-%m-%d %H:%M:%S") - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            if overlap <= cursor:
                # A page full of the same timestamp cannot be safely skipped.
                break
            cursor = overlap
        counts = Counter(e.get("eventtime", "")[:10] for e in self.store.entries())
        with self.store.db:
            for day in self.store.db.execute("SELECT * FROM days").fetchall():
                self.store.db.execute("UPDATE days SET done=? WHERE day=?", (counts[day["day"]] == day["expected"], day["day"]))

    def comment_metadata(self):
        store = self.store
        cursor = store.get("meta_cursor", 1)
        while not store.get("meta_done", False):
            self.update("comments", f"Получаем сведения о комментариях, начиная с #{cursor}…")
            page, raw = self.client.comments("comment_meta", cursor)
            if page.maxid is None:
                raise ExportError("ЖЖ не сообщил верхнюю границу комментариев; полнота не подтверждена.")
            nextid = page.nextid or (max((c["id"] for c in page.comments), default=cursor - 1) + 1)
            if nextid <= cursor and cursor <= page.maxid:
                raise ExportError("Страница метаданных комментариев пуста до конца списка. Прогресс сохранён, повторите позже.")
            atomic_write(self.folder / "raw" / f"comment-meta-{cursor}.xml", raw)
            with store.db:
                for item in page.comments:
                    store.db.execute("INSERT OR REPLACE INTO comment_meta VALUES (?,?,?)",
                                     (item["id"], item.get("posterid", 0), item.get("state", "A")))
                for uid, name in page.users.items():
                    store.db.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (uid, name))
                store.set("comment_maxid", page.maxid)
                store.set("meta_cursor", nextid)
                store.set("meta_done", nextid > page.maxid)
            cursor = nextid

    def comment_bodies(self):
        store = self.store
        cursor, maximum = store.get("body_cursor", 1), store.get("comment_maxid", 0)
        while cursor <= maximum:
            self.update("comments", f"Скачиваем тексты и ветки комментариев · ID {cursor} / {maximum}",
                        completed=cursor - 1, total=maximum)
            page, raw = self.client.comments("comment_body", cursor)
            # LJ uses ID windows, not counts of returned bodies. Target the next known
            # metadata ID after an empty window to skip gaps without skipping a comment.
            last = max((c["id"] for c in page.comments), default=cursor - 1)
            nextid = page.nextid or last + 1
            if nextid <= cursor:
                next_known = store.db.execute("SELECT MIN(id) FROM comment_meta WHERE id>?", (cursor,)).fetchone()[0]
                nextid = next_known if next_known is not None else maximum + 1
            atomic_write(self.folder / "raw" / f"comment-body-{cursor}.xml", raw)
            with store.db:
                for item in page.comments:
                    store.save_comment(item)
                for uid, name in page.users.items():
                    store.db.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (uid, name))
                store.set("body_cursor", nextid)
            cursor = nextid
        with store.db:
            store.set("bodies_done", True)

    def report(self, error=None, cancelled=False):
        store = self.store
        entry_limit = store.get("entry_limit")
        missing = [dict(row) for row in store.db.execute("""SELECT m.id,m.state FROM comment_meta m
            LEFT JOIN comments c ON c.id=m.id WHERE (c.id IS NULL OR json_type(c.payload,'$.body') IS NULL)
            AND m.state NOT IN ('D','X') ORDER BY m.id""")]
        pending = [dict(row) for row in store.db.execute("SELECT day,expected FROM days WHERE done=0 ORDER BY day")]
        counts = store.counts()
        expected_entries = store.db.execute("SELECT COALESCE(SUM(expected),0) FROM days").fetchone()[0]
        if entry_limit is not None:
            expected_entries = store.get("recent_expected", entry_limit)
        deleted = store.db.execute("SELECT COUNT(*) FROM comment_meta WHERE state IN ('D','X')").fetchone()[0]
        screened = store.db.execute("SELECT COUNT(*) FROM comment_meta WHERE state='S'").fetchone()[0]
        orphaned = [r[0] for r in store.db.execute("SELECT DISTINCT c.entry_id FROM comments c LEFT JOIN entries e ON c.entry_id=e.id WHERE e.id IS NULL")]
        unknown_encoding = [e["itemid"] for e in store.entries() if isinstance(e.get("event"), dict) or isinstance(e.get("subject"), dict)]
        issues = []
        if error:
            issues.append(error)
        if pending:
            issues.append(f"Не подтверждена полнота записей за {len(pending)} дн.")
        if counts["entries"] != expected_entries:
            source = "выбранным объёмом" if entry_limit is not None else "календарём ЖЖ"
            issues.append(f"Число сохранённых записей ({counts['entries']}) не совпадает с {source} ({expected_entries}).")
        if missing:
            issues.append(f"ЖЖ не отдал текст {len(missing)} неудалённых комментариев, в том числе скрытых: {sum(c['state'] == 'S' for c in missing)}.")
        if orphaned:
            issues.append(f"Комментарии относятся к {len(orphaned)} записям, текст которых не получен. Для них созданы отдельные страницы.")
        if unknown_encoding:
            issues.append(f"Не определена кодировка {len(unknown_encoding)} записей. Исходные байты сохранены в JSON (base64).")
        pending_comments = []
        if entry_limit is not None:
            statuses = store.get("entry_comments", {})
            for entry in store.entries():
                eid = int(entry["itemid"])
                if not statuses.get(str(eid), {}).get("done"):
                    pending_comments.append(eid)
                count = store.db.execute("SELECT COUNT(*) FROM comments WHERE entry_id=?", (eid,)).fetchone()[0]
                if count < int(entry.get("reply_count", 0) or 0):
                    issues.append(f"К записи #{eid} получено {count} комментариев; ЖЖ сообщил как минимум {entry['reply_count']}.")
                has_screened = entry.get("props", {}).get("hasscreened")
                if has_screened in (1, "1", True) and not store.db.execute("""SELECT 1 FROM comments c
                        JOIN comment_meta m ON m.id=c.id WHERE c.entry_id=? AND m.state='S'""", (eid,)).fetchone():
                    issues.append(f"ЖЖ отметил скрытые комментарии у записи #{eid}, но не вернул их. Полнота не подтверждена.")
            if pending_comments:
                issues.append(f"Обход комментариев не завершён для {len(pending_comments)} записей.")
        required = ("posts_done", "meta_done", "bodies_done") if entry_limit is not None else ("days_loaded", "posts_done", "meta_done", "bodies_done")
        done = all(store.get(k) for k in required)
        budget = getattr(getattr(self, "client", None), "budget", None)
        return {"username": store.get("username"), "created_at": now(), "started_at": store.get("started_at"),
                "status": "cancelled" if cancelled else "complete" if done and not issues else "partial",
                "entries_expected": expected_entries,
                "entries_saved": counts["entries"], "comments_saved": counts["comments"],
                "comments_metadata": counts["comment_meta"], "screened_comments": screened,
                "deleted_comments": deleted, "missing_comments": missing, "pending_days": pending,
                "orphan_entry_ids": orphaned, "unknown_encoding_entry_ids": unknown_encoding,
                "entry_limit": entry_limit, "pending_comment_entry_ids": pending_comments,
                **(budget.snapshot() if budget else store.get("last_request_stats", {})),
                "issues": issues, "images_requested": self.images,
                "scope": (f"Пробный архив: последние {entry_limit} записей по дате публикации на момент выбора и их комментарии. Проверяются страницы getcomments и наличие текстов, включая скрытые."
                          if entry_limit is not None else "Личный журнал владельца. Полнота проверяется по календарю и метаданным API ЖЖ на момент выгрузки."),
                "limitations": "Удалённые тексты и данные, которые ЖЖ не отдаёт владельцу, восстановить нельзя. Изменения во время выгрузки могут потребовать нового снимка."}

    def run(self):
        from .render import build_archive
        store = self.store
        try:
            self.update("login", "Входим в ЖЖ и получаем сессию владельца…")
            self.client.login()
            old_user = store.get("username")
            if old_user and old_user != self.client.user:
                raise ExportError("Эта папка принадлежит другому журналу.")
            if old_user and store.get("entry_limit") != self.entry_limit:
                raise ExportError("Папка содержит архив с другим объёмом выгрузки. Используйте отдельную папку.")
            # A new complete run is a new snapshot. Old readable files remain until rebuilt.
            with store.db:
                if store.get("finished", False):
                    for table in ("entries", "comments", "comment_meta", "users", "days", "settings", "reactions"):
                        store.db.execute(f"DELETE FROM {table}")
                store.set("username", self.client.user)
                if getattr(self.client, "journal_id", None):
                    store.set("journal_id", self.client.journal_id)
                store.set("entry_limit", self.entry_limit)
                if not store.get("started_at"):
                    store.set("started_at", now())
                if store.get("bodies_done"):
                    # A partial run may have missed screened bodies. Re-fetch on retry.
                    store.set("body_cursor", 1)
                    store.set("bodies_done", False)
                    if self.entry_limit is not None:
                        store.set("entry_comments", {})
            error, cancelled = None, False
            try:
                self.posts()
                if self.entry_limit is not None:
                    self.recent_comments()
                else:
                    self.comment_metadata()
                    self.comment_bodies()
                from .reactions import capture_reactions
                capture_reactions(store, self.client, self.progress)
            except Cancelled as exc:
                error, cancelled = str(exc), True
            except ExportError as exc:
                error = str(exc)
            report = self.report(error, cancelled)
            budget = getattr(self.client, "budget", None)
            if budget:
                with store.db:
                    store.set("last_request_stats", budget.snapshot())
            self.update("render", "Собираем страницы, поиск и отчёт о полноте…")
            report = build_archive(store, report, images=self.images and not cancelled,
                                   client_cancel=self.client.cancel, progress=self.progress, budget=budget)
            with store.db:
                store.set("finished", report["status"] == "complete")
                if budget:
                    store.set("last_request_stats", budget.snapshot())
            self.update(report["status"], "Архив готов." if report["status"] == "complete" else "Сохранён частичный архив. Подробности — в отчёте.", report=report)
            return report
        finally:
            self.client.close()
            store.close()


def export(user, password, output, *, images=False, delay=2, cancel=None, progress=None,
           entry_limit=10, max_requests=50):
    user = username(user)
    entry_limit = validate_entry_limit(entry_limit)
    budget = RequestBudget(max_requests, progress=progress)
    client = LiveJournalClient(user, password, delay=delay, cancel=cancel, budget=budget)
    try:
        return Exporter(Path(output) / archive_slug(user, entry_limit), client, images=images,
                        progress=progress, entry_limit=entry_limit).run()
    finally:
        client.close()
