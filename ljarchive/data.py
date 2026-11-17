"""Portable, lossless source data. No HTML templates, CSS, renderer or network calls."""
import json
from pathlib import Path

from .reactions import reaction_report
from .storage import atomic_write
from .userpics import describe

SCHEMA_VERSION = 1


def snapshot(store):
    from .personal import account_data
    from .sources import stored_source
    return {
        "source": stored_source(store),
        "account": account_data(store),
        "schema_version": SCHEMA_VERSION,
        "journal": {"username": store.get("username"), "id": store.get("journal_id")},
        "scope": {"entry_limit": store.get("entry_limit"), "started_at": store.get("started_at"),
                  "last_sync": store.get("sync_job", {}).get("scope")},
        "entries": sorted(store.entries(), key=lambda item: int(item["itemid"])),
        "comments": store.comments(),
        "comment_metadata": [dict(row) for row in store.db.execute("SELECT * FROM comment_meta ORDER BY id")],
        "users": [dict(row) for row in store.db.execute("SELECT id,name AS username FROM users ORDER BY id")],
        "media": store.get("media", {}),
        "media_errors": store.get("media_errors", {}),
        "videos": store.get("videos", {}),
        "video_embeds": store.get("video_embeds", {}),
        "external_embeds": store.get("external_embeds", {}),
        "userpics": describe(store),
        "versions": [{"kind": r[0], "id": r[1], "saved_at": r[2], "record": json.loads(r[3])}
                     for r in store.db.execute("SELECT kind,object_id,saved_at,payload FROM versions ORDER BY saved_at,kind,object_id,digest")],
        "coverage": {"posts_done": bool(store.get("posts_done")), "comments_done": bool(store.get("bodies_done")),
                     "reactions": reaction_report(store), "discussions": store.get("entry_comments", {}),
                     "personal_requested": store.get("personal_requested", {})},
    }


def export_json(store, path=None, *, split=False, format="source"):
    """Can run independently of rendering, including before any HTML exists."""
    data = snapshot(store)
    from .activitystreams import to_activitystreams
    if format not in ("source", "activitystreams"):
        raise ValueError("Unknown JSON format")
    document = to_activitystreams(data) if format == "activitystreams" else data
    filename = "activitystreams.json" if format == "activitystreams" else "archive.json"
    path = Path(path) if path else store.folder / "data" / filename
    atomic_write(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    if split:
        for name, key in (("entries", "entries"), ("comments", "comments"), ("comment-metadata", "comment_metadata")):
            atomic_write(store.folder / "data" / f"{name}.json", json.dumps(data[key], ensure_ascii=False, indent=2) + "\n")
        activity = document if format == "activitystreams" else to_activitystreams(data)
        atomic_write(store.folder / "data" / "activitystreams.json", json.dumps(activity, ensure_ascii=False, indent=2) + "\n")
        atomic_write(store.folder / "data" / "activitystreams-context.json",
                     (Path(__file__).parent / "schemas" / "activitystreams-context.json").read_bytes())
    return path
