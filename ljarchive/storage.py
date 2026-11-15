"""Transactional checkpoints and lossless normalized source records."""
from __future__ import annotations

import json
import os
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .client import ExportError


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def atomic_write(path: Path, data: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


class Store:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = (folder / "archive.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lock.seek(0)
                if not self._lock.read(1):
                    self._lock.write(b"0")
                    self._lock.flush()
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock.close()
            raise ExportError("Этот архив уже открыт другим процессом. Дождитесь завершения другой выгрузки.") from exc
        self.db = sqlite3.connect(folder / "archive.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY, expected INTEGER, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS comment_meta(id INTEGER PRIMARY KEY, posterid INTEGER, state TEXT);
            CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY, entry_id INTEGER, parent_id INTEGER, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS comments_entry ON comments(entry_id);
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE IF NOT EXISTS reactions(kind TEXT, object_id INTEGER, payload TEXT NOT NULL,
                PRIMARY KEY(kind, object_id));
            CREATE TABLE IF NOT EXISTS record_hashes(kind TEXT, object_id INTEGER, digest TEXT NOT NULL,
                PRIMARY KEY(kind,object_id));
            CREATE TABLE IF NOT EXISTS versions(kind TEXT, object_id INTEGER, digest TEXT, saved_at TEXT, payload TEXT,
                PRIMARY KEY(kind,object_id,digest));
        """)
        with self.db:
            for kind, table in (("post", "entries"), ("comment", "comments")):
                for row in self.db.execute(f"SELECT id,payload FROM {table} WHERE id NOT IN (SELECT object_id FROM record_hashes WHERE kind=?)", (kind,)):
                    self.db.execute("INSERT INTO record_hashes VALUES (?,?,?)", (kind, row[0], hashlib.sha256(row[1].encode()).hexdigest()))

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, dumps(value)))

    def save_entry(self, item):
        self._version("post", "entries", int(item["itemid"]), item)
        self.db.execute("INSERT OR REPLACE INTO entries VALUES (?,?)", (int(item["itemid"]), dumps(item)))

    def save_comment(self, item):
        self._version("comment", "comments", int(item["id"]), item)
        self.db.execute("INSERT OR REPLACE INTO comments VALUES (?,?,?,?)",
                        (int(item["id"]), int(item.get("jitemid", 0)), int(item.get("parentid", 0)), dumps(item)))

    def _version(self, kind, table, object_id, item):
        payload = dumps(item)
        old = self.db.execute(f"SELECT payload FROM {table} WHERE id=?", (object_id,)).fetchone()
        if old and json.loads(old[0]) != item:
            self.db.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?)",
                            (kind, object_id, hashlib.sha256(old[0].encode()).hexdigest(),
                             datetime.now(timezone.utc).isoformat(timespec="seconds"), old[0]))
        self.db.execute("INSERT OR REPLACE INTO record_hashes VALUES (?,?,?)",
                        (kind, object_id, hashlib.sha256(payload.encode()).hexdigest()))

    def counts(self):
        return {table: self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("entries", "comments", "comment_meta")}

    def entries(self):
        items = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM entries")]
        reactions = self.reactions("post")
        for item in items:
            if int(item["itemid"]) in reactions:
                item["reactions"] = reactions[int(item["itemid"])]
        return items

    def reactions(self, kind):
        return {row[0]: json.loads(row[1]) for row in self.db.execute(
            "SELECT object_id,payload FROM reactions WHERE kind=?", (kind,))}

    def save_reactions(self, kind, object_id, payload):
        self.db.execute("INSERT OR REPLACE INTO reactions VALUES (?,?,?)", (kind, object_id, dumps(payload)))

    def comments(self):
        rows = self.db.execute("""SELECT c.payload, m.state, m.posterid, u.name FROM comments c
            LEFT JOIN comment_meta m ON m.id=c.id
            LEFT JOIN users u ON u.id=COALESCE(m.posterid,json_extract(c.payload,'$.posterid')) ORDER BY c.id""")
        result = []
        reactions = self.reactions("comment")
        for row in rows:
            item = json.loads(row[0])
            item["state"] = row[1] or item.get("state", "A")
            poster = row[2] if row[2] is not None else item.get("posterid", 0)
            item["posterid"] = poster
            item["user"] = row[3] or None
            if int(item["id"]) in reactions:
                item["reactions"] = reactions[int(item["id"])]
            result.append(item)
        return result

    def close(self):
        self.db.close()
        self._lock.close()
