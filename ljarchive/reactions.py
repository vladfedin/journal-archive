"""Read and archive the per-type reaction counts used by LiveJournal's own UI."""
from datetime import datetime, timezone
from collections import Counter

from .client import Cancelled, ExportError

BATCH_SIZE = 50
AUTHOR_BATCH_SIZE = 10
AUTHOR_SOURCE = "https://www.livejournal.com/__api/ (likes.get_user_list)"
SOURCE = "https://likes.services.livejournal.com/get"


def display_id(entry):
    if entry.get("ditemid"):
        return int(entry["ditemid"])
    if "anum" in entry:
        return int(entry["itemid"]) * 256 + int(entry["anum"])
    return None


def parse_counts(rows, requested):
    """Match full display IDs: a post and its comment never share a key."""
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (row.get("journal"), row.get("item"), row.get("thread", 0))
        if any(type(part) is not int for part in key) or key not in requested or key in result:
            continue
        count, groups = row.get("count"), row.get("groups")
        if (type(count) is not int or count < 0 or not isinstance(groups, list)
                or any(type(n) is not int or n < 0 for n in groups) or sum(groups) != count):
            continue
        result[key] = {"count": count, "groups": groups, "source": SOURCE,
                       "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return result


def parse_authors(rows, counts):
    users, seen, invalid = [], set(), False
    for row in rows:
        if not isinstance(row, dict):
            invalid = True
            continue
        uid, kind, name = row.get("id"), row.get("type"), row.get("username")
        if (type(uid) is not int or uid <= 0 or type(kind) is not int or kind < 0
                or not isinstance(name, str) or not name or uid in seen):
            invalid = True
            continue
        seen.add(uid)
        user = {"id": uid, "username": name, "type": kind}
        # Profile URLs are source data; the presentation layer validates link schemes.
        for key in ("display_name", "profile_url", "journal_url"):
            if isinstance(row.get(key), str):
                user[key] = row[key]
        users.append(user)
    actual = Counter(u["type"] for u in users)
    expected = Counter({kind: n for kind, n in enumerate(counts["groups"]) if n})
    return {"status": "complete" if not invalid and actual == expected else "partial",
            "users": users, "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": AUTHOR_SOURCE}


def capture_authors(store, client, targets, progress=None):
    pending = []
    saved = {"post": store.reactions("post"), "comment": store.reactions("comment")}
    with store.db:
        for key, kind, object_id in targets:
            counts = saved[kind].get(object_id)
            if counts is None:
                continue
            if counts["count"] == 0:
                counts["authors"] = parse_authors([], counts)
                store.save_reactions(kind, object_id, counts)
            elif counts.get("authors", {}).get("status") != "complete":
                pending.append((key, kind, object_id, counts))
    for start in range(0, len(pending), AUTHOR_BATCH_SIZE):
        client.check()
        if progress:
            progress(phase="reactions", message=f"Сохраняем авторов реакций: {start} из {len(pending)}…",
                     completed=start, total=len(pending))
        batch = pending[start:start + AUTHOR_BATCH_SIZE]
        entities = [{"journal": key[0], "item": key[1], **({"thread": key[2]} if key[2] else {})}
                    for key, _, _, _ in batch]
        responses = client.reaction_authors(entities)
        with store.db:
            for index, (_, kind, object_id, counts) in enumerate(batch):
                response = responses[index] if index < len(responses) else {}
                if "users" in response:
                    counts["authors"] = parse_authors(response["users"], counts)
                else:
                    counts["authors"] = {"status": "unavailable", "users": [], "source": AUTHOR_SOURCE,
                                         "error": response.get("error", "Ответ с авторами не получен.")}
                store.save_reactions(kind, object_id, counts)


def capture_reactions(store, client, progress=None, *, entry_ids=None, checkpoint=None):
    entries, comments = store.entries(), store.comments()
    if entry_ids is not None:
        entries = [e for e in entries if int(e["itemid"]) in entry_ids]
        comments = [c for c in comments if int(c.get("jitemid", 0)) in entry_ids and c.get("source_status") != "not_returned"]
    if not entries:
        return True
    issues = []
    complete = False
    try:
        journal_id = client.journal_id or store.get("journal_id")
        if not journal_id:
            first = next((display_id(e) for e in entries if display_id(e)), None)
            if first is None:
                raise ExportError("В архиве нет ID записей, необходимых для загрузки реакций.")
            journal_id = client.discover_journal_id(first)
        with store.db:
            store.set("journal_id", journal_id)
        targets = []
        by_id = {int(e["itemid"]): display_id(e) for e in entries}
        for entry in entries:
            did = by_id[int(entry["itemid"])]
            if did:
                targets.append(((journal_id, did, 0), "post", int(entry["itemid"])))
        for comment in comments:
            did = by_id.get(int(comment.get("jitemid", 0)))
            if did:
                thread = int(comment.get("dtalkid") or int(comment["id"]) * 256 + did % 256)
                targets.append(((journal_id, did, thread), "comment", int(comment["id"])))
        done = set(store.get(checkpoint, [])) if checkpoint else set()
        pending = [t for t in targets if f"{t[1]}:{t[2]}" not in done]
        counts_error = None
        for start in range(0, len(pending), BATCH_SIZE):
            client.check()
            if progress:
                progress(phase="reactions", message=f"Сохраняем реакции: {start} из {len(targets)}…",
                         completed=start, total=len(targets))
            batch = pending[start:start + BATCH_SIZE]
            entities = [{"journal": key[0], "item": key[1], **({"thread": key[2]} if key[2] else {})}
                        for key, _, _ in batch]
            try:
                counts = parse_counts(client.reaction_counts(entities), {key for key, _, _ in batch})
            except Cancelled:
                raise
            except ExportError as exc:
                counts_error = exc
                break
            with store.db:
                for key, kind, object_id in batch:
                    if key in counts:
                        store.save_reactions(kind, object_id, counts[key])
                        done.add(f"{kind}:{object_id}")
                if checkpoint:
                    store.set(checkpoint, sorted(done))
            if len(counts) != len(batch):
                issues.append(f"ЖЖ не вернул корректные счётчики для {len(batch) - len(counts)} объектов.")
        if counts_error:
            issues.append(str(counts_error))
        fresh = [t for t in targets if f"{t[1]}:{t[2]}" in done]
        capture_authors(store, client, fresh, progress)
        saved = {"post": store.reactions("post"), "comment": store.reactions("comment")}
        incomplete = [t for t in fresh if saved[t[1]][t[2]].get("authors", {}).get("status") != "complete"]
        complete = len(fresh) == len(targets) and not incomplete and not issues
        if checkpoint:
            # A changing counter can disagree with the author list; refresh that object next time.
            with store.db:
                store.set(checkpoint, sorted(done - {f"{t[1]}:{t[2]}" for t in incomplete}))
    except Cancelled:
        raise
    except ExportError as exc:
        issues.append(str(exc))
    finally:
        with store.db:
            store.set("reaction_issues", issues)
    return complete


def reaction_report(store):
    entries, comments = store.entries(), store.comments()
    posts_saved = sum("reactions" in item for item in entries)
    comments_saved = sum("reactions" in item for item in comments)
    posts_authors = sum(item.get("reactions", {}).get("authors", {}).get("status") == "complete" for item in entries)
    comments_authors = sum(item.get("reactions", {}).get("authors", {}).get("status") == "complete" for item in comments)
    return {"posts_saved": posts_saved, "posts_total": len(entries),
            "comments_saved": comments_saved, "comments_total": len(comments),
            "complete": posts_saved == len(entries) and comments_saved == len(comments),
            "author_posts_saved": posts_authors, "author_comments_saved": comments_authors,
            "authors_complete": posts_authors == len(entries) and comments_authors == len(comments),
            "authors_saved": sum(len(item.get("reactions", {}).get("authors", {}).get("users", [])) for item in entries + comments),
            "issues": store.get("reaction_issues", []), "source": SOURCE}
