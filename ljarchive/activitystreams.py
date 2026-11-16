"""ActivityStreams 2.0 archive profile; conversion is local and independent of HTML views.

The standard projection is accompanied by an embedded JSON Document containing the
entire source snapshot. Its string content survives JSON-LD 1.0 processing, including
unknown source keys, ordered arrays, nulls and keys such as @context or @id.
"""
from collections import defaultdict
from datetime import datetime
from email.utils import parsedate_to_datetime
import hashlib
import html
from html.parser import HTMLParser
import json
import mimetypes
import re
from urllib.parse import quote, urljoin, urlsplit

import bleach

AS_CONTEXT = "https://www.w3.org/ns/activitystreams"
NAMESPACE = "https://github.com/vladfedin/journal-archive/blob/main/docs/activitystreams.md#"
CONTEXT = [AS_CONTEXT, {"ja": NAMESPACE}]
PROFILE_VERSION = 1
TAGS = frozenset("p br div span a b strong i em u s del blockquote pre code ul ol li h1 h2 h3 h4 h5 h6 hr img table thead tbody tr td th caption dl dt dd sub sup details summary figure figcaption".split())
ATTRS = {"a": ["href", "title"], "img": ["src", "alt", "title", "width", "height"],
         "td": ["colspan", "rowspan"], "th": ["colspan", "rowspan"]}


def http_url(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme in ("http", "https") and parts.hostname and not parts.username and not parts.password:
            return value
    except ValueError:
        pass
    return None


def profile(name):
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,15}", name):
        return f"https://{name.replace('_', '-')}.livejournal.com/"


def person(name=None, number=None):
    result = {"type": "Person"}
    if number:
        result["id"] = f"urn:livejournal:user:{quote(str(number), safe='')}"
    elif profile(name):
        result["id"] = profile(name)
    else:
        return None
    if isinstance(name, str) and name:
        result["name"] = name
    if profile(name):
        result["url"] = profile(name)
    return result


def published(value):
    """Never invent UTC for LJ's local eventtime or for a date without an offset."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return moment.isoformat() if moment.tzinfo is not None and moment.utcoffset() is not None else None


class Content(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=False)
        self.base, self.parts, self.images = base, [], []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "lj" and profile(values.get("user") or values.get("username")):
            name = values.get("user") or values["username"]
            self.parts.append(f'<a href="{profile(name)}">{html.escape(values.get("displayusername") or name)}</a>')
        elif tag == "lj-cut":
            # Standard consumers see the entire post. The exact cut is in the source.
            return
        else:
            for key in ("src", "href"):
                if key in values:
                    value = http_url(urljoin(self.base, values[key] or ""))
                    if value:
                        values[key] = value
                        if tag == "img" and key == "src" and value not in self.images:
                            self.images.append(value)
                    else:
                        del values[key]
            attributes = "".join(f' {k}="{html.escape(v or "", quote=True)}"' for k, v in values.items())
            self.parts.append(f"<{tag}{attributes}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag not in ("lj", "lj-cut"):
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")


def image(url, media):
    result = {"type": "Image", "url": url}
    path = media.get(url)
    if path:
        result["ja:localPath"] = path
    mime = mimetypes.guess_type(path or urlsplit(url).path)[0]
    if mime and mime.startswith("image/"):
        result["url"] = {"type": "Link", "href": url, "mediaType": mime}
    return result


def to_activitystreams(data):
    """Project a complete archive.json snapshot without altering or discarding it."""
    journal = data["journal"]
    user = journal["username"]
    base = profile(user) or "https://www.livejournal.com/"
    namespace = "urn:livejournal:journal:" + quote(str(user), safe="") + ":"
    identity = lambda kind, number: namespace + kind + ":" + quote(str(number), safe="")
    media = data.get("media", {})
    owner = person(user, journal.get("id"))
    entries = {e["itemid"]: e for e in data.get("entries", [])}
    items, objects, replies = [], {}, defaultdict(list)

    def entry_url(entry):
        if http_url(entry.get("url")):
            return entry["url"]
        number = entry.get("ditemid")
        if not number and "anum" in entry:
            number = int(entry["itemid"]) * 256 + int(entry["anum"])
        return urljoin(base, f"{number}.html") if number else None

    def message(record, kind, metadata_only=False):
        is_post = kind == "post"
        number = record["itemid" if is_post else "id"]
        oid = identity(kind, number)
        state = record.get("state") if not is_post else None
        obj = {"id": oid, "type": "Article" if is_post else "Note", "ja:kind": kind}
        if metadata_only:
            obj["ja:metadataOnly"] = True
        if state in ("D", "X"):
            obj.update(type="Tombstone", formerType="Note")
        if isinstance(state, str):
            obj["ja:state"] = state
        if is_post:
            author = person(record.get("poster"), record.get("posterid")) or owner
            url = entry_url(record)
            if isinstance(record.get("security"), str):
                obj["ja:security"] = record["security"]
                if record["security"] == "public":
                    obj["to"] = [AS_CONTEXT + "#Public"]
                elif record["security"] == "private" and owner:
                    obj["to"] = [owner["id"]]
            if isinstance(record.get("allowmask"), int):
                obj["ja:allowmask"] = record["allowmask"]
        else:
            author = person(record.get("user"), record.get("posterid"))
            eid, parent = record.get("jitemid"), record.get("parentid")
            url = None
            if eid is not None:
                obj["context"] = identity("post", eid)
                # Missing parent information is not the same as a root comment.
                if parent is not None:
                    obj["inReplyTo"] = identity("comment", parent) if parent else identity("post", eid)
                    replies[obj["inReplyTo"]].append(oid)
                root_url = entry_url(entries[eid]) if eid in entries else None
                if root_url and record.get("dtalkid"):
                    url = root_url + "?thread=" + str(record["dtalkid"])
        if author:
            obj["attributedTo"] = author
        if url:
            obj["url"] = url
        subject = record.get("subject")
        if isinstance(subject, str):
            obj["name"] = html.unescape(bleach.clean(subject, tags=[], strip=True))
        date = record.get("eventtime" if is_post else "date")
        when = published(date)
        if when:
            obj["published"] = when
        elif isinstance(date, str) and date:
            obj["ja:localPublished"] = date
        body = record.get("event" if is_post else "body")
        props = record.get("props") if isinstance(record.get("props"), dict) else {}
        if isinstance(body, str):
            parser = Content(url or base)
            parser.feed(body)
            parser.close()
            obj["content"] = bleach.clean("".join(parser.parts), tags=TAGS, attributes=ATTRS,
                                           protocols=["http", "https"], strip=True)
            if props.get("opt_preformatted") not in (1, "1", True) and not re.search(r"<(?:p|div|blockquote|ul|ol|li|table|h[1-6]|pre|figure|figcaption)\b", body, re.I):
                obj["content"] = obj["content"].replace("\n", "<br>\n")
            obj["mediaType"] = "text/html"
            if parser.images:
                obj["attachment"] = [image(value, media) for value in parser.images]
        else:
            obj["ja:contentStatus"] = "unavailable" if body is None else "unknown-encoding"
        tags = props.get("taglist")
        if isinstance(tags, str):
            obj["tag"] = [{"type": "Object", "name": name.strip()} for name in tags.split(",") if name.strip()]
        pic = data.get("userpics", {}).get(kind, {}).get(str(number)) or {}
        if http_url(pic.get("url")):
            obj["icon"] = image(pic["url"], media)
            if pic.get("path"):
                obj["icon"]["ja:localPath"] = pic["path"]
        if isinstance(pic.get("selection"), str):
            obj["ja:userpicSelection"] = pic["selection"]
        if isinstance(pic.get("keyword"), str):
            obj["ja:userpicKeyword"] = pic["keyword"]
        reactions = record.get("reactions")
        if isinstance(reactions, dict):
            if isinstance(reactions.get("count"), int):
                obj["ja:reactionCount"] = reactions["count"]
            if isinstance(reactions.get("groups"), list):
                obj["ja:reactionCounts"] = [{"type": "Object", "ja:reactionType": rid, "ja:count": count}
                                             for rid, count in enumerate(reactions["groups"])]
            authors = reactions.get("authors") or {}
            obj["ja:reactionAuthorsStatus"] = authors.get("status", "not_loaded")
            reaction_ids = []
            for who in authors.get("users", []):
                rid = who.get("type")
                actor = person(who.get("username"), who.get("id"))
                if actor is None:
                    continue
                reaction = {"id": oid + ":reaction:" + quote(str(rid), safe="") + ":" + quote(actor["id"], safe=""),
                            "type": "Like" if rid == 0 else "Dislike" if rid == 4 else ["Activity", "ja:Reaction"],
                            "actor": actor, "object": oid}
                if isinstance(rid, int):
                    reaction["ja:reactionType"] = rid
                when_captured = published(authors.get("captured_at") or reactions.get("captured_at"))
                if when_captured:
                    reaction["ja:capturedAt"] = when_captured
                items.append(reaction)
                reaction_ids.append(reaction["id"])
            if reaction_ids:
                obj["ja:reactions"] = {"type": "Collection", "items": [{"id": key, "type": "Activity"} for key in reaction_ids],
                                       "totalItems": len(reaction_ids)}
        objects[oid] = obj
        items.append(obj)

    for record in data.get("entries", []):
        message(record, "post")
    known_comments = set()
    for record in data.get("comments", []):
        message(record, "comment")
        known_comments.add(record["id"])
    users = {u["id"]: u.get("username") for u in data.get("users", [])}
    for meta in data.get("comment_metadata", []):
        if meta["id"] not in known_comments:
            message({**meta, "user": users.get(meta.get("posterid"))}, "comment", metadata_only=True)
    for oid, children in replies.items():
        if oid in objects:
            objects[oid]["replies"] = {"type": "Collection", "totalItems": len(children), "items": children}
    source = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return {"@context": CONTEXT, "id": namespace + "archive", "type": "Collection", "name": user,
            "totalItems": len(items), "items": items, "ja:profileVersion": PROFILE_VERSION,
            "ja:source": {"type": "Document", "name": "archive.json", "mediaType": "application/json", "content": source,
                          "ja:sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}}


def source_snapshot(document):
    """Recover the exact source JSON values; reject an unsupported/damaged payload."""
    if document.get("ja:profileVersion") != PROFILE_VERSION:
        raise ValueError("Unsupported archive profile version")
    source = document.get("ja:source", {})
    content = source.get("content")
    if source.get("mediaType") != "application/json" or not isinstance(content, str):
        raise ValueError("Missing source JSON document")
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != source.get("ja:sha256"):
        raise ValueError("Source JSON checksum mismatch")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Invalid source snapshot")
    return result
