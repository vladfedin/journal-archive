"""Read and archive the per-type reaction counts used by LiveJournal's own UI."""
from datetime import datetime, timezone
from collections import Counter

from .client import Cancelled, ExportError
from .gaps import ATTEMPTS, closed, fail, reopen

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
            elif counts.get("authors", {}).get("status") != "complete" and not closed(counts.get("authors")):
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
                previous = counts.get("authors") or {}
                if "users" in response:
                    counts["authors"] = parse_authors(response["users"], counts)
                else:
                    counts["authors"] = {"status": "unavailable", "users": [], "source": AUTHOR_SOURCE,
                                         "error": response.get("error", "Ответ с авторами не получен.")}
                if counts["authors"]["status"] != "complete":
                    fresh = counts['authors']['users']
                    seen = {user['id'] for user in fresh}
                    retained = [user for user in previous.get('users', []) if user['id'] not in seen]
                    counts['authors']['users'] = fresh + retained
                    if retained:
                        counts['authors']['retained_user_ids'] = [user['id'] for user in retained]
                    fail(counts["authors"], previous)
                store.save_reactions(kind, object_id, counts)


def capture_reactions(store, client, progress=None, *, entry_ids=None, checkpoint=None):
    entries, comments = store.entries(), store.comments_for_download()
    comments = [c for c in comments if c.get("state") not in ("D", "X")]
    if entry_ids is not None:
        entries = [e for e in entries if int(e["itemid"]) in entry_ids]
        comments = [c for c in comments if int(c.get("jitemid", 0)) in entry_ids and c.get("source_status") != "not_returned"]
    if not entries:
        return True
    issues = []
    complete = False
    retryable = True
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
        # A counter LJ never returns is asked for ATTEMPTS times, then treated as absent.
        missed = store.get("reaction_count_attempts", {})
        given_up = {key for key, n in missed.items() if n >= ATTEMPTS}
        pending = [t for t in targets if f"{t[1]}:{t[2]}" not in done and f"{t[1]}:{t[2]}" not in given_up]
        known = {"post": store.reactions("post"), "comment": store.reactions("comment")}
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
                retryable = False
                counts_error = exc
                break
            with store.db:
                for key, kind, object_id in batch:
                    name = f"{kind}:{object_id}"
                    if key in counts:
                        prior = known[kind].get(object_id) or {}
                        old = prior.get("authors")
                        if old:
                            # A fresh counter keeps the author list's attempt history.
                            counts[key]["authors"] = dict(old)
                            if (prior.get('count'), prior.get('groups')) != (counts[key]['count'], counts[key]['groups']):
                                counts[key]['authors']['status'] = 'partial'
                                reopen(counts[key]['authors'])
                        store.save_reactions(kind, object_id, counts[key])
                        done.add(name)
                        missed.pop(name, None)
                    else:
                        missed[name] = missed.get(name, 0) + 1
                        if missed[name] >= ATTEMPTS:
                            given_up.add(name)
                if checkpoint:
                    store.set(checkpoint, sorted(done))
                store.set("reaction_count_attempts", missed)
            unanswered = [t for t in batch if t[0] not in counts and f"{t[1]}:{t[2]}" not in given_up]
            if unanswered:
                issues.append(f"ЖЖ не вернул корректные счётчики для {len(unanswered)} объектов.")
        if counts_error:
            issues.append(str(counts_error))
        fresh = [t for t in targets if f"{t[1]}:{t[2]}" in done]
        capture_authors(store, client, fresh, progress)
        saved = {"post": store.reactions("post"), "comment": store.reactions("comment")}
        incomplete = [t for t in fresh if (saved[t[1]].get(t[2]) or {}).get("authors", {}).get("status") != "complete"
                      and not closed((saved[t[1]].get(t[2]) or {}).get("authors"))]
        settled = [t for t in targets if f"{t[1]}:{t[2]}" in given_up and f"{t[1]}:{t[2]}" not in done]
        complete = len(fresh) + len(settled) == len(targets) and not incomplete and not issues
        # Missing authors do not invalidate a counter already received in this run.
    except Cancelled:
        raise
    except ExportError as exc:
        retryable = False
        issues.append(str(exc))
    finally:
        with store.db:
            store.set("reaction_issues", issues)
            store.set("reaction_retryable", retryable)
    return complete


def reaction_report(store):
    entries, comments = store.entries(), store.comments_for_download()
    comments = [c for c in comments if c.get("state") not in ("D", "X")]
    given_up = {key for key, n in store.get("reaction_count_attempts", {}).items() if n >= ATTEMPTS}
    posts_saved = sum("reactions" in item for item in entries)
    comments_saved = sum("reactions" in item for item in comments)
    counts_unavailable = sum("reactions" not in item and f"{kind}:{item[key]}" in given_up
                             for kind, key, items in (("post", "itemid", entries), ("comment", "id", comments)) for item in items)
    retained = sum('reactions' in item and f'{kind}:{item[key]}' in given_up
                   for kind, key, items in (('post', 'itemid', entries), ('comment', 'id', comments)) for item in items)
    posts_authors = sum(item.get("reactions", {}).get("authors", {}).get("status") == "complete" for item in entries)
    comments_authors = sum(item.get("reactions", {}).get("authors", {}).get("status") == "complete" for item in comments)
    authors_unavailable = sum(closed(item.get("reactions", {}).get("authors")) for item in entries + comments)
    # Settled means saved, or asked for several times and treated as absent.
    return {"posts_saved": posts_saved, "posts_total": len(entries),
            "comments_saved": comments_saved, "comments_total": len(comments),
            "unavailable": counts_unavailable + retained, "counts_retained": retained, "authors_unavailable": authors_unavailable,
            "complete": posts_saved + comments_saved + counts_unavailable == len(entries) + len(comments),
            "author_posts_saved": posts_authors, "author_comments_saved": comments_authors,
            "authors_complete": posts_authors + comments_authors + authors_unavailable + counts_unavailable
                                >= len(entries) + len(comments),
            "authors_saved": sum(len(item.get("reactions", {}).get("authors", {}).get("users", [])) for item in entries + comments),
            "issues": store.get("reaction_issues", []), "source": SOURCE}
