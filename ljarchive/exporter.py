from __future__ import annotations

import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .client import APIError, Cancelled, ExportError, IncompleteData, LiveJournalClient, RequestBudget, username
from .storage import Store, atomic_write, dumps
from .comment_state import DELETED, merge_comment
from .entries import is_repost
from .sources import bind_source


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
        for key in ("userpic", "poster_userpic_url", "datepostunix", "is_show", "is_loaded"):
            if key in node:
                item[key] = node[key]
        if "body" in node and node["body"] is not None:
            item["body"] = node["body"]
        elif item["state"] not in DELETED and node.get("is_loaded", node.get("_loaded")) in (1, "1", True):
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
    def __init__(self, folder: Path, client: LiveJournalClient, *, images=True, progress=None, entry_limit=None, account=False, messages=False, videos=False):
        self.folder, self.client, self.images = folder, client, images
        self.videos = videos
        self.progress = progress or (lambda **kwargs: None)
        self.entry_limit = validate_entry_limit(entry_limit)
        self.store = Store(folder)
        self.account, self.messages = account, messages

    def update(self, phase, message, **extra):
        self.progress(phase=phase, message=message, **self.store.counts(), **extra)

    def retry_missing(self, operation, label):
        # HTTP failures already have their own retry policy. Only partial successful
        # responses get additional passes; access refusals and caps propagate immediately.
        import time
        for attempt in range(3):
            self.client.check()
            if attempt:
                self.update("retrying", f"Докачка: {label} · проход {attempt + 1} из 3")
            try:
                return operation()
            except IncompleteData:
                if attempt == 2:
                    raise
                deadline = max(time.time() + 30 * 2 ** attempt, getattr(self.client, "not_before", 0))
                wait_info = {"reason": "incomplete", "category": "livejournal", "retry_at": deadline,
                             "attempt": attempt + 1, "max_attempts": 3}
                with self.store.db:
                    self.store.set("request_not_before", deadline)
                    self.store.set("request_wait", wait_info)
                    self.store.set("repair", {"stage": label, "pass": attempt + 2, "retry_at": deadline})
                self.update("waiting", f"Докачка: {label} · следующий проход {attempt + 2} из 3", retry_at=deadline, wait=wait_info)
                if hasattr(self.client, "_wait"):
                    self.client._wait(deadline - time.time())
                elif self.client.cancel.wait(max(0, deadline - time.time())):
                    self.client.check()

    def userpic_metadata(self):
        # Upgrade old archives without repeating already enriched discussions.
        from .userpics import describe
        pictures = describe(self.store)
        requested = set(self.store.get("userpic_comments_requested", []))
        selected = getattr(self, "selected_ids", None)
        pending = {int(c["jitemid"]) for c in self.store.comments()
                   if pictures["comment"][str(c["id"])]["selection"] == "unknown"
                   and int(c["jitemid"]) not in requested
                   and (selected is None or int(c["jitemid"]) in selected)}
        if not pending:
            return
        statuses = self.store.get("entry_comments", {})
        for eid in pending:
            statuses[str(eid)] = {"next_page": 1, "done": False}
        with self.store.db:
            self.store.set("entry_comments", statuses)
        if hasattr(self, "checkpoint"):
            self.checkpoint(phase="comments")
            self.retry_missing(self.comments, "юзерпики комментариев")
        else:
            self.retry_missing(self.recent_comments, "юзерпики комментариев")

    def reactions(self, *, entry_ids=None, checkpoint="trial_reaction_counts"):
        from .reactions import capture_reactions
        def capture():
            complete = capture_reactions(self.store, self.client, self.progress,
                                         entry_ids=entry_ids, checkpoint=checkpoint)
            if not complete and self.store.get("reaction_retryable"):
                raise IncompleteData("Не все реакции или имена авторов получены. Можно продолжить загрузку.")
            return complete
        return self.retry_missing(capture, "реакции")

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
            raise IncompleteData(f"Запрошены последние {expected} записей, ЖЖ вернул {len(entries)}.")

    def discussion_complete(self, entry, status):
        if is_repost(entry):
            return True
        store, eid = self.store, int(entry["itemid"])
        saved = store.db.execute("SELECT payload FROM comments WHERE entry_id=?", (eid,)).fetchall()
        import json
        records = [json.loads(row[0]) for row in saved]
        seen = set(status.get("seen_ids", []))
        missing_bodies = any(("body" not in c or c.get("source_status") == "body_not_returned")
                             and c.get("state") not in ("D", "X") for c in records if c["id"] in seen)
        missing_screened = entry.get("props", {}).get("hasscreened") in (1, "1", True) and not any(
            c.get("state") == "S" and c["id"] in seen for c in records)
        return not (missing_bodies or missing_screened or len(seen) < int(entry.get("reply_count", 0) or 0))

    def recent_comments(self):
        store = self.store
        statuses = store.get("entry_comments", {})
        incomplete = []
        entries = sorted(store.entries(), key=lambda e: (e.get("eventtime", ""), int(e["itemid"])))
        selected = getattr(self, "selected_ids", None)
        if selected is not None:
            entries = [e for e in entries if int(e["itemid"]) in selected]
        for entry in entries:
            self.client.check()
            eid = int(entry["itemid"])
            status = statuses.setdefault(str(eid), {"next_page": 1, "done": False})
            if status.get("done") and (not is_repost(entry) or status.get("skipped") == "repost"):
                continue
            try:
                self.discussion(entry, status, statuses)
                status.pop("error", None)
            except (IncompleteData, APIError) as exc:
                if isinstance(exc, APIError) and exc.code not in (203, 300):
                    raise
                status.update(done=False, error=str(exc), next_page=min(status.get("next_page", 1), max(1, status.get("pages", 1))))
                incomplete.append(eid)
            finally:
                with store.db:
                    store.set("entry_comments", statuses)
                self.update("comments", f"Запись #{eid}: " + ("комментарии сохранены" if status.get("done") else "есть пропуски"),
                            entry_id=eid)
            self.entry_materials(entry)
        with store.db:
            store.set("meta_done", True)
            store.set("bodies_done", not incomplete)
        if incomplete:
            raise IncompleteData(f"Не подтверждена полнота обсуждений: {len(incomplete)}. Остальные записи обработаны; пропуски сохранены для повтора.")

    def discussion(self, entry, status, statuses):
        store, eid = self.store, int(entry["itemid"])
        if is_repost(entry):
            status.clear()
            status.update(done=True, scope="original-journal", skipped="repost", seen_ids=[])
            with store.db:
                store.set("entry_comments", statuses)
            return
        page = max(1, int(status.get("next_page", 1)))
        while True:
            self.update("comments", f"Комментарии к записи #{eid} · страница {page}", entry_id=eid,
                        comment_page=page, comment_pages=status.get("pages"))
            response = self.client.call("getcomments", journal=self.client.user, itemid=eid,
                                        page=page, page_size=500, expand_strategy="expand_all", format="thread", extra=1)
            nodes = response.get("comments")
            if not isinstance(nodes, list):
                raise IncompleteData(f"ЖЖ не вернул список комментариев к записи #{eid}.")
            try:
                items, users = normalize_thread(nodes, eid)
            except ExportError as exc:
                raise IncompleteData(str(exc)) from exc
            atomic_write(self.folder / "raw" / f"comments-{eid}-page-{page}.json", dumps(response))
            self.save_thread(items, users)
            seen_before = set(status.get("seen_ids", []))
            status["seen_ids"] = sorted(seen_before | {int(c["id"]) for c in items})
            # Persist received data and the retry cursor even when pagination is broken.
            with store.db:
                store.set("entry_comments", statuses)
            try:
                pages, returned_page = int(response["pages"]), int(response["page"])
                if any(type(response[k]) not in (int, str) for k in ("pages", "page")) or pages < 0 or (pages and page > pages):
                    raise ValueError()
                if returned_page != page and not (pages == 0 and returned_page == 0 and not items):
                    raise ValueError()
                if pages == 0 and items:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                # LJ may omit pagination for an explicitly empty discussion.
                if page == 1 and not items and entry.get("reply_count") in (0, "0") and self.discussion_complete(entry, status):
                    pages = 0
                    status["pagination"] = "empty-discussion"
                else:
                    raise IncompleteData(f"ЖЖ не подтвердил номер/число страниц комментариев к записи #{eid}. Тексты сохранены; запись отложена.") from None
            if not items and page < pages:
                raise IncompleteData(f"Пустая страница комментариев к записи #{eid} до конца списка.")
            current_ids = {int(c["id"]) for c in items}
            valid_ids = set(status.get("valid_seen_ids", status.get("last_valid_ids", [])))
            if page > 1 and items and status.get("last_valid_page") != page and not (current_ids - valid_ids):
                raise IncompleteData(f"ЖЖ повторяет страницу комментариев к записи #{eid}. Запись отложена.")
            status.update(pages=pages, next_page=page if page >= pages else page + 1, last_valid_page=page, last_valid_ids=sorted(current_ids), valid_seen_ids=sorted(valid_ids | current_ids))
            if page >= pages:
                missing = store.db.execute("""SELECT payload FROM comments WHERE entry_id=?
                    AND (json_type(payload,'$.body') IS NULL OR json_extract(payload,'$.source_status')='body_not_returned')
                    AND COALESCE(json_extract(payload,'$.state'),'A') NOT IN ('D','X')""", (eid,)).fetchall()
                import json
                for row in missing:
                    item = json.loads(row[0])
                    if item["id"] not in status["seen_ids"]:
                        continue
                    loaded = store.db.execute("SELECT json_type(payload,'$.body'),json_extract(payload,'$.source_status') FROM comments WHERE id=?", (item["id"],)).fetchone()
                    if loaded[0] is not None and loaded[1] != "body_not_returned":
                        continue
                    if not item.get("dtalkid"):
                        continue
                    self.update("comments", f"Разворачиваем комментарий #{item['id']} к записи #{eid}…", entry_id=eid)
                    expanded = self.client.call("getcomments", journal=self.client.user, itemid=eid,
                                                dtalkid=item["dtalkid"], page=1, page_size=500,
                                                expand_strategy="expand_all", format="thread", extra=1)
                    if not isinstance(expanded.get("comments"), list):
                        raise IncompleteData("ЖЖ не вернул развёрнутую ветку.")
                    atomic_write(self.folder / "raw" / f"comments-{eid}-thread-{item['id']}.json", dumps(expanded))
                    try:
                        expanded_items, expanded_users = normalize_thread(expanded["comments"], eid)
                    except ExportError as exc:
                        raise IncompleteData(str(exc)) from exc
                    self.save_thread(expanded_items, expanded_users)
                    status["seen_ids"] = sorted(set(status["seen_ids"]) | {int(c["id"]) for c in expanded_items})
                self.confirm_deleted(eid, status)
                status["done"] = self.discussion_complete(entry, status)
                with store.db:
                    store.set("userpic_comments_requested", sorted(set(store.get("userpic_comments_requested", [])) | {eid}))
                if not status["done"]:
                    status.update(next_page=1, seen_ids=[], last_valid_page=0, last_valid_ids=[], valid_seen_ids=[])
                    raise IncompleteData(f"Не все комментарии к записи #{eid} получены.")
            with store.db:
                store.set("entry_comments", statuses)
            if page >= pages:
                return
            page += 1

    def entry_materials(self, entry):
        # Filled by the per-entry media stage; the final renderer also uses this cache.
        from .progress import capture_materials
        capture_materials(self, entry)
        from .video import capture_videos
        capture_videos(self, entry)

    def video_stage(self):
        if not self.videos:
            return
        from .video import capture_videos
        selected = getattr(self, 'selected_ids', None)
        for entry in self.store.entries():
            if selected is None or int(entry['itemid']) in selected:
                self.client.check()
                capture_videos(self, entry)

    def save_thread(self, items, users, *, evidence="getcomments"):
        store = self.store
        with store.db:
            import json
            for item in items:
                row = store.db.execute("SELECT payload FROM comments WHERE id=?", (item["id"],)).fetchone()
                current = merge_comment(json.loads(row[0]) if row else None, item, evidence=evidence)
                store.save_comment(current)
                store.db.execute("INSERT OR REPLACE INTO comment_meta VALUES (?,?,?)",
                                 (current["id"], current["posterid"], current["state"]))
            for uid, name in users.items():
                store.db.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (uid, name))

    def confirm_deleted(self, eid, status):
        """Check bodyless nodes against explicit export metadata, never infer deletion.

        At most three windows per discussion pass, through the shared budget.
        Normal discussions and explicit tombstones incur no additional requests.
        """
        if not callable(getattr(self.client, "comments", None)):
            return
        pending = {c['id']: c for c in self.store.comments(eid)
                   if c['id'] in status.get('seen_ids', []) and c.get('state') not in DELETED
                   and ('body' not in c or c.get('source_status') == 'body_not_returned')}
        for _ in range(3):
            if not pending:
                break
            start = min(pending)
            page, raw = self.client.comments('comment_meta', start)
            atomic_write(self.folder / 'raw' / f'deletion-meta-{eid}-{start}.xml', raw)
            matched = []
            for meta in page.comments:
                if meta['id'] in pending:
                    old = pending.pop(meta['id'])
                    if meta.get('state') in DELETED:
                        item = {**old, **meta}
                        item.pop('source_status', None)
                        item.pop('body', None)
                        matched.append(item)
            self.save_thread(matched, page.users, evidence="comment_meta")
            # A window that does not confirm the requested ID is not a deletion.
            boundary = max((c['id'] for c in page.comments), default=start)
            pending = {cid: c for cid, c in pending.items() if cid > boundary}

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

    def personal(self):
        if getattr(self.client, 'source', {}).get('type') == 'community':
            with self.store.db:
                self.store.set('personal_requested', {'account': False, 'messages': False})
            return
        from .personal import Personal
        with self.store.db:
            self.store.set("personal_requested", {"account": self.account, "messages": self.messages})
            self.store.set("personal_images_requested", self.images)
        if self.account or self.messages:
            Personal(self).run(self.account, self.messages)

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
                if is_repost(entry):
                    continue
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
                "issues": issues, "images_requested": self.images, "videos_requested": self.videos,
                "scope": (f"Пробный архив: последние {entry_limit} записей по дате публикации на момент выбора и их комментарии. Проверяются страницы getcomments и наличие текстов, включая скрытые."
                          if entry_limit is not None else "Полнота журнала проверяется по календарю и метаданным API ЖЖ на момент выгрузки."),
                "limitations": "Удалённые тексты и данные, которые ЖЖ не отдаёт владельцу, восстановить нельзя. Изменения во время выгрузки могут потребовать нового снимка."}

    def run(self):
        from .render import build_archive
        store = self.store
        try:
            if hasattr(self.client, "not_before"):
                self.client.not_before = store.get("request_not_before", 0)
                def save_cooldown(deadline):
                    with store.db:
                        store.set("request_not_before", deadline)
                        store.set("request_wait", self.client.budget.wait_info)
                self.client.save_cooldown = save_cooldown
                self.client.budget.save_cooldown = save_cooldown
                self.client.budget.not_before = self.client.not_before
                self.client.budget.wait_info = store.get("request_wait", {})
            old_user = store.get('username')
            if old_user and store.get('entry_limit') != self.entry_limit:
                raise ExportError('Папка содержит архив с другим объёмом выгрузки. Используйте отдельную папку.')
            with store.db:
                bind_source(store, self.client)
                store.set('entry_limit', self.entry_limit)
            self.update("login", "Входим в ЖЖ и проверяем доступ…")
            try:
                self.client.login()
            finally:
                with store.db:
                    bind_source(store, self.client)
            # A new complete run is a new snapshot. Old readable files remain until rebuilt.
            with store.db:
                if store.get("finished", False):
                    for table in ("entries", "comments", "comment_meta", "users", "days", "settings", "reactions"):
                        store.db.execute(f"DELETE FROM {table}")
                bind_source(store, self.client)
                if getattr(self.client, "journal_id", None):
                    store.set("journal_id", self.client.journal_id)
                if getattr(self.client, "userpics", None) is not None:
                    store.set("userpic_catalog", self.client.userpics)
                store.set("entry_limit", self.entry_limit)
                if not store.get("started_at"):
                    store.set("started_at", now())
                if store.get("bodies_done") and self.report()["issues"]:
                    # A partial run may have missed screened bodies. Re-fetch on retry.
                    store.set("body_cursor", 1)
                    store.set("bodies_done", False)
                    if self.entry_limit is not None:
                        store.set("entry_comments", {})
            error, cancelled = None, False
            try:
                failures = []
                def stage(operation, label):
                    try:
                        self.retry_missing(operation, label)
                    except IncompleteData as exc:
                        failures.append(str(exc))
                stage(self.posts, "записи")
                if self.entry_limit is not None:
                    stage(self.recent_comments, "комментарии")
                    if store.get("bodies_done"):
                        stage(self.userpic_metadata, "юзерпики")
                else:
                    self.comment_metadata()
                    self.comment_bodies()
                try:
                    self.reactions()
                except IncompleteData as exc:
                    failures.append(str(exc))
                self.personal()
                self.video_stage()
                error = " ".join(failures) or None
            except Cancelled as exc:
                error, cancelled = str(exc), True
            except ExportError as exc:
                error = str(exc)
            with store.db:
                source = bind_source(store, self.client)
            report = self.report(error, cancelled)
            report['source'] = source
            budget = getattr(self.client, "budget", None)
            if budget:
                with store.db:
                    store.set("last_request_stats", budget.snapshot())
            self.update("render", "Собираем страницы, поиск и отчёт о полноте…")
            report = build_archive(store, report, images=self.images and not cancelled, userpics=not cancelled,
                                   client_cancel=self.client.cancel, progress=self.progress, budget=budget, media=getattr(self, "_media", None))
            with store.db:
                store.set("finished", report["status"] == "complete")
                if budget:
                    store.set("last_request_stats", budget.snapshot())
            self.update(report["status"], "Архив готов." if report["status"] == "complete" else "Сохранён частичный архив. Подробности — в отчёте.", report=report)
            return report
        finally:
            if getattr(self, "_media", None):
                self._media.close()
            self.client.close()
            if getattr(self, "_videos", None): self._videos.close()
            store.close()


def export(user, password, output, *, images=False, delay=2, cancel=None, progress=None,
           entry_limit=10, max_requests=50, account=False, messages=False, videos=False, journal=None, source_type=None):
    user = username(user)
    entry_limit = validate_entry_limit(entry_limit)
    budget = RequestBudget(max_requests, progress=progress)
    client = LiveJournalClient(user, password, delay=delay, cancel=cancel, budget=budget, progress=progress, journal=journal, source_type=source_type)
    try:
        return Exporter(Path(output) / archive_slug(client.user, entry_limit), client, images=images,
                        progress=progress, entry_limit=entry_limit, account=account, messages=messages, videos=videos).run()
    finally:
        client.close()
