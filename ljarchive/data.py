"""Portable, lossless source data. No HTML templates, CSS, renderer or network calls."""
import json
from pathlib import Path

from .reactions import reaction_report
from .storage import atomic_write

SCHEMA_VERSION = 1


def snapshot(store):
    return {
        "schema_version": SCHEMA_VERSION,
        "journal": {"username": store.get("username"), "id": store.get("journal_id")},
        "scope": {"entry_limit": store.get("entry_limit"), "started_at": store.get("started_at"),
                  "last_sync": store.get("sync_job", {}).get("scope")},
        "entries": sorted(store.entries(), key=lambda item: int(item["itemid"])),
        "comments": store.comments(),
        "comment_metadata": [dict(row) for row in store.db.execute("SELECT * FROM comment_meta ORDER BY id")],
        "users": [dict(row) for row in store.db.execute("SELECT id,name AS username FROM users ORDER BY id")],
        "media": store.get("media", {}),
        "versions": [{"kind": r[0], "id": r[1], "saved_at": r[2], "record": json.loads(r[3])}
                     for r in store.db.execute("SELECT kind,object_id,saved_at,payload FROM versions ORDER BY saved_at,kind,object_id,digest")],
        "coverage": {"posts_done": bool(store.get("posts_done")), "comments_done": bool(store.get("bodies_done")),
                     "reactions": reaction_report(store)},
    }


def export_json(store, path=None, *, split=False):
    """Can run independently of rendering, including before any HTML exists."""
    data = snapshot(store)
    path = Path(path) if path else store.folder / "data" / "archive.json"
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if split:
        for name, key in (("entries", "entries"), ("comments", "comments"), ("comment-metadata", "comment_metadata")):
            atomic_write(store.folder / "data" / f"{name}.json", json.dumps(data[key], ensure_ascii=False, indent=2) + "\n")
    return path
