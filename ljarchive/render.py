from __future__ import annotations

import html
import re
import shutil
import threading
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs

import bleach
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .media import Media
from .data import export_json
from .reactions import reaction_report
from .presentation import reaction_view
from .userpics import describe, coverage
from .storage import Store, atomic_write, dumps

ROOT = Path(__file__).parent
ENV = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
TAGS = frozenset("p br div span a b strong i em u s del blockquote pre code ul ol li h1 h2 h3 h4 h5 h6 hr img table thead tbody tr td th caption dl dt dd sub sup details summary figure figcaption".split())
ATTRS = {"a": ["href", "title"], "img": ["src", "alt", "title", "width", "height"],
         "details": ["class", "open"], "summary": ["class"],
         "td": ["colspan", "rowspan"], "th": ["colspan", "rowspan"]}
BLOCK_MARKUP = re.compile(r"<(?:p|div|blockquote|ul|ol|li|table|h[1-6]|pre|figure|figcaption)\b", re.I)
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")
# LJ's own "current" fields, shown under the post the way journal styles did.
CURRENT = (("current_music", "Музыка"), ("current_mood", "Настроение"), ("current_location", "Местоположение"))


def ru_date(value, clock=True, weekday=False):
    """«11 ноября 2026, 21:49» from the ISO-like dates LJ returns; unknown formats pass through."""
    text = str(value or "").strip()
    match = DATE.match(text)
    if not match or not 1 <= int(match[2]) <= 12:
        return text or "Дата неизвестна"
    result = f"{int(match[3])}\u00a0{MONTHS[int(match[2]) - 1]} {match[1]}"
    if weekday:
        from datetime import date
        try:
            result = ("Понедельник Вторник Среда Четверг Пятница Суббота Воскресенье".split()[date.fromisoformat(text[:10]).weekday()] + ", " + result)
        except ValueError:
            pass
    if clock and match[4]:
        result += f", {match[4]}:{match[5]}"
        if re.search(r"(?:UTC|Z|[+-]00:00)$", text):
            result += "\u00a0UTC"
    return result


def plural(number, one, few, many):
    """Russian count agreement: 1 запись, 2 записи, 5 записей."""
    try:
        n = abs(int(number))
    except (TypeError, ValueError):
        return f"{number} {many}"
    if n % 10 == 1 and n % 100 != 11:
        form = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        form = few
    else:
        form = many
    return f"{n} {form}"


def excerpt(text, limit=220):
    """Cut at a word boundary so a card never ends mid-word."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—–-(«\"") + "…"


ENV.policies["json.dumps_kwargs"] = {"sort_keys": True, "ensure_ascii": False}
ENV.filters["ru_date"] = ru_date
ENV.filters["plural"] = plural
ENV.filters["profile_url"] = lambda value: (f"https://{value.replace('_', '-')}.livejournal.com/" if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", value) else "")
def task_label(report):
    if report.get("entry_limit"):
        return f"Тест · {report['entry_limit']} записей"
    scope = report.get("sync", {}).get("scope", {})
    return {"full": "Весь журнал", "period": "За период", "urls": "По ссылкам"}.get(scope.get("mode"), "Весь журнал")
ENV.globals["task_label"] = task_label


def needs_autoformat(value, props=None):
    """HTML paragraphs already define line breaks; their source indentation is not prose."""
    return (props or {}).get("opt_preformatted") not in (1, "1", True) and not BLOCK_MARKUP.search(text_value(value))


class LJUserMarkup(HTMLParser):
    """Recognize LJ user attributes in any order, including editor-generated tags."""
    def __init__(self, expand_cuts=False):
        super().__init__(convert_charrefs=False)
        self.out = []
        self.cut_depth = 0
        self.expand_cuts = expand_cuts

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        user = values.get("user") or values.get("username")
        if tag == "lj-cut":
            label = values.get("text") or "Читать дальше"
            self.out.append('<details class="lj-cut"' + (" open" if self.expand_cuts else "") + "><summary>" + html.escape(label) + "</summary>")
            self.cut_depth += 1
        elif tag == "lj" and user and re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", user):
            label = values.get("displayusername") or user
            self.out.append(f'<a href="https://{user.replace("_", "-")}.livejournal.com/">{html.escape(label)}</a>')
        else:
            self.out.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "lj-cut":
            if self.cut_depth:
                self.out.append("</details>")
                self.cut_depth -= 1
        elif tag != "lj":
            self.out.append(f"</{tag}>")

    def handle_data(self, value):
        self.out.append(value)

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")


def text_value(value):
    if isinstance(value, dict):
        return "[Неизвестная кодировка. Исходные байты сохранены в data/entries.json.]"
    return str(value or "")


def plain(value):
    return html.unescape(bleach.clean(text_value(value), tags=[], strip=True))


def original_url(user, entry):
    candidate = entry.get("url", "")
    if isinstance(candidate, str) and urlsplit(candidate).scheme in ("http", "https"):
        return candidate
    ditemid = entry.get("ditemid")
    if not ditemid and "anum" in entry:
        ditemid = int(entry["itemid"]) * 256 + int(entry["anum"])
    host = user.replace("_", "-") + ".livejournal.com"
    return f"https://{host}/{ditemid}.html" if ditemid else f"https://{host}/"


class Content(HTMLParser):
    """Rewrite already-sanitized markup, keeping local references relative."""
    def __init__(self, source, base, links, media, comment_ids):
        super().__init__(convert_charrefs=False)
        self.out, self.base, self.links, self.media = [], base, links, media
        self.comment_ids = comment_ids
        self.preserve_depth = 0
        self.feed(source)
        self.close()

    def local_link(self, value):
        url = urljoin(self.base, value)
        parts = urlsplit(url)
        key = (parts.hostname, parts.path)
        if key in self.links:
            target = self.links[key]
            query = parse_qs(parts.query)
            thread = query.get("thread", [None])[0]
            if thread and thread.isdigit() and int(thread) // 256 in self.comment_ids:
                target += "#comment-" + str(int(thread) // 256)
            elif parts.fragment.startswith("t") and parts.fragment[1:].isdigit() and int(parts.fragment[1:]) // 256 in self.comment_ids:
                target += "#comment-" + str(int(parts.fragment[1:]) // 256)
            return target
        return url if parts.scheme in ("http", "https", "mailto") else "#"

    def handle_starttag(self, tag, attrs):
        if tag in ("pre", "code"):
            self.preserve_depth += 1
        attrs = dict(attrs)
        if tag == "img":
            src = urljoin(self.base, attrs.get("src", ""))
            local = self.media.get(src)
            if local:
                self.out.append(f'<img src="../{html.escape(local, quote=True)}" alt="{html.escape(attrs.get("alt", ""), quote=True)}" loading="lazy">')
            else:
                self.out.append('<a class="media-link" href="' + html.escape(self.local_link(src), quote=True) + '">↗ ' + html.escape(attrs.get("alt") or "Изображение в оригинале") + '</a>')
            return
        if tag == "a":
            attrs["href"] = self.local_link(attrs.get("href", ""))
            attrs["rel"] = "noreferrer noopener"
        self.out.append("<" + tag + "".join(f' {k}="{html.escape(v or "", quote=True)}"' for k, v in attrs.items()) + ">")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("pre", "code"):
            self.preserve_depth = max(0, self.preserve_depth - 1)
        if tag not in ("img", "br", "hr"):
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.preserve_depth:
            data = re.sub(r"\n[ \t]*(?:\n[ \t]*){2,}", "\n\n", data)
        self.out.append(data)

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")


def safe_content(value, base, links, media, comment_ids, *, expand_cuts=False):
    source = text_value(value)
    # Preserve LJ user links and cut contents; scripts and embeds never run offline.
    lj_markup = LJUserMarkup(expand_cuts)
    lj_markup.feed(source)
    lj_markup.close()
    source = "".join(lj_markup.out) + "</details>" * lj_markup.cut_depth
    source = re.sub(r'<(script|style)\b[^>]*>.*?</\1\s*>', "", source, flags=re.I | re.S)
    source = re.sub(r'<(?:iframe|embed)\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*>',
                    lambda m: '<p><a href="' + html.escape(m[1], quote=True) + '">Открыть встроенный материал в оригинале ↗</a></p>', source, flags=re.I)
    cleaned = bleach.clean(source, tags=TAGS, attributes=ATTRS, protocols=["https", "http", "mailto"], strip=True)
    cleaned = re.sub(r"<p>(?:\s|&nbsp;|&#160;|<br\s*/?>)*</p>", "", cleaned, flags=re.I)
    return Markup("".join(Content(cleaned, base, links, media, comment_ids).out))


def threaded(comments):
    """Iterative tree walk: preserve orphan/cyclic comments, never recurse on input."""
    by_id = {int(c["id"]): c for c in comments}
    children = defaultdict(list)
    for item in comments:
        parent = int(item.get("parentid", 0))
        children[parent if parent in by_id and parent != item["id"] else 0].append(item)
    for group in children.values():
        group.sort(key=lambda c: (c.get("date", ""), int(c["id"])))
    visited, result = set(), []
    def walk(roots):
        stack = [(item, 0) for item in reversed(roots)]
        while stack:
            item, depth = stack.pop()
            cid = int(item["id"])
            if cid in visited:
                continue
            visited.add(cid)
            result.append({**item, "depth": depth, "visual_depth": min(depth, 6),
                           "has_children": bool(children[cid]),
                           "missing_parent": bool(item.get("parentid") and int(item["parentid"]) not in by_id)})
            stack.extend((child, depth + 1) for child in reversed(children[cid]) if child["id"] not in visited)
    walk(children[0])
    # Corrupt parent pointers must not make comments disappear.
    for item in comments:
        if item["id"] not in visited:
            walk([item])
    return result


def build_archive(store: Store, report: dict, *, images=False, userpics=False, client_cancel=None, progress=None, budget=None, media=None):
    folder, user = store.folder, report["username"]
    cancel = client_cancel or threading.Event()
    media = media or Media(folder, images, cancel, budget=budget)
    media.enabled = images
    media.load_state(store)
    entries, comments = store.entries(), store.comments()
    report["format_version"] = "0.7.3"
    report.setdefault("issues", [])
    report["reactions"] = reaction_report(store)
    if report.get("status") == "complete" and not (report["reactions"]["complete"] and report["reactions"]["authors_complete"]):
        report["status"] = "partial"
        report["issues"].append("Не все реакции или имена авторов получены.")
    pictures = describe(store, entries, comments)
    selected = report.get("sync", {}).get("selected")
    selected = None if selected is None else set(selected)
    comment_entries = {str(c["id"]): int(c.get("jitemid", 0)) for c in comments}
    task_pictures = {"post": {}, "comment": {}}
    requested_pics = set()
    total_pics = len({pic["url"] for group in pictures.values() for pic in group.values() if pic["url"]})
    for kind, group in pictures.items():
        for key, pic in group.items():
            in_scope = selected is None or (int(key) if kind == "post" else comment_entries[key]) in selected
            if pic["url"]:
                if progress and userpics and in_scope and pic["url"] not in requested_pics:
                    requested_pics.add(pic["url"])
                    progress(phase="userpics", message="Юзерпики", completed=len(requested_pics) - 1, total=total_pics)
                pic["path"] = media.get(pic["url"], required=userpics and in_scope, category="userpics")
            if in_scope:
                task_pictures[kind][key] = pic
    for group in pictures.values():
        for pic in group.values():
            if pic["url"]:
                pic["path"] = media.mapping.get(pic["url"])
    report["userpics"] = coverage(task_pictures)
    if userpics and not report["userpics"]["complete"] and report.get("status") == "complete":
        report["status"] = "partial"
        report["issues"].append("Не все юзерпики получены. Можно продолжить загрузку.")
    by_entry = defaultdict(list)
    for item in comments:
        by_entry[int(item.get("jitemid", 0))].append(item)
    real_ids = {int(e["itemid"]) for e in entries}
    for itemid in by_entry:
        if itemid not in real_ids:
            entries.append({"itemid": itemid, "subject": f"Запись #{itemid} · текст недоступен",
                            "event": "Текст записи не получен от ЖЖ. Ниже сохранены её комментарии.",
                            "eventtime": "", "unavailable": True})
    entries.sort(key=lambda e: (e.get("eventtime", ""), int(e["itemid"])), reverse=True)
    links = {}
    for entry in entries:
        url = urlsplit(original_url(user, entry))
        if url.path != "/":
            links[(url.hostname, url.path)] = str(entry["itemid"]) + ".html"
    comment_ids = {int(c["id"]) for c in comments}
    (folder / "assets").mkdir(exist_ok=True)
    for asset in ("archive.css", "archive.js", "theme.js", "common.js", "favicon.svg", "favicon.ico", "timeline.js", "statistics.js"):
        shutil.copyfile(ROOT / "static" / asset, folder / "assets" / asset)
    search, summaries = [], []
    labels = {"public": "Открытая", "private": "Личная", "usemask": "Для друзей / группы"}
    from .comment_state import comment_view
    from .entries import is_repost, repost_url
    from .progress import material_progress, coverage_notice
    comment_statuses, material_records = store.get("entry_comments", {}), store.get("entry_materials", {})
    saved_media = store.get("media", {})
    try:
        for index, entry in enumerate(entries):
            if progress:
                progress(phase="render", message=f"Собираем страницу {index + 1} из {len(entries)}…", completed=index, total=len(entries))
            eid = int(entry["itemid"])
            title = plain(entry.get("subject")) or "Без заголовка"
            url = original_url(user, entry)
            entry_comments = [] if is_repost(entry) else threaded(by_entry[eid])
            props = entry.get("props") if isinstance(entry.get("props"), dict) else {}
            security = entry.get("security", "public")
            tags = [t.strip() for t in text_value(entry.get("props", {}).get("taglist", "")).split(",") if t.strip()]
            date = entry.get("eventtime", "")
            summary = {"repost": is_repost(entry), "repost_url": repost_url(entry), "userpic": pictures["post"].get(str(eid)), "id": eid, "title": title, "date": date, "year": date[:4], "tags": tags,
                       "security": security, "security_label": labels.get(security, security),
                       "comments": len(entry_comments), "screened": sum(c["state"] == "S" for c in entry_comments),
                       "excerpt": excerpt(plain(entry.get("event"))), "url": f"entries/{eid}.html",
                       "reactions": reaction_view(entry.get("reactions"))}
            summaries.append(summary)
            search.append({"entry": eid, "title": title, "text": plain(entry.get("event")), "url": summary["url"], "kind": "post"})
            for item in entry_comments:
                item["user"] = item.get("user") or (f"Пользователь #{item['posterid']}" if item.get("posterid") else "Аноним")
            authors = {int(item["id"]): item["user"] for item in entry_comments}
            for item in entry_comments:
                item["userpic_view"] = pictures["comment"].get(str(item["id"]))
                item["by_owner"] = item["user"] == user
                item["parent_user"] = authors.get(int(item.get("parentid") or 0))
                item.update(comment_view(item))
                item["html"] = safe_content(item["display_body"], url, links, media, comment_ids)
                item["subject_text"] = plain(item.get("subject"))
                item["autoformat"] = needs_autoformat(item.get("body"), item.get("props"))
                item["reaction_view"] = reaction_view(item.get("reactions"))
                search.append({"entry": eid, "title": title, "text": f'{item["user"]} {item["subject_text"]} {plain(item.get("body"))}',
                               "url": f'entries/{eid}.html#comment-{item["id"]}', "kind": "comment"})
            content = safe_content(entry.get("event"), url, links, media, comment_ids, expand_cuts=True)
            current = [(label, plain(props[key])) for key, label in CURRENT if plain(props.get(key)).strip()]
            neighbours = [{"itemid": other["itemid"], "title": plain(other.get("subject")) or "Без заголовка"} if other else None
                          for other in (entries[index - 1] if index else None,
                                        entries[index + 1] if index + 1 < len(entries) else None)]
            rendered = ENV.get_template("entry.html").render(user=user, entry=entry, summary=summary,
                        content=content, comments=entry_comments, original=url,
                        post_autoformat=needs_autoformat(entry.get("event"), entry.get("props")),
                        current=current, newer=neighbours[0], older=neighbours[1],
                        coverage_notice=coverage_notice(comment_statuses.get(str(eid), {}),
                            material_progress(material_records.get(str(eid), {}), {**saved_media, **media.mapping}), repost=is_repost(entry)),
                        report=report)
            atomic_write(folder / "entries" / f"{eid}.html", rendered)
            feed = ENV.get_template("feed-post.html").render(content=safe_content(entry.get("event"), url, links, media, comment_ids),
                    current=current, post_autoformat=needs_autoformat(entry.get("event"), entry.get("props")))
            # Rewrite only internal links and local images for the archive root. Original text is unchanged.
            feed = re.sub(r'href="([0-9]+\.html(?:#[^"\s]*)?)"', r'href="entries/\1"', feed)
            feed = feed.replace('src="../media/', 'src="media/')
            atomic_write(folder / "assets" / "posts" / f"{eid}.js",
                         "window.ArchivePost(" + str(eid) + "," + dumps(str(feed)).replace("<", "\\u003c") + ");")
    finally:
        media.close()
    image_map = {url: media.mapping[url] for url in media.used_urls["images"]}
    report["images_saved"] = sum(bool(path) for path in image_map.values())
    from .media import error_message
    report["media_errors"] = {url: {**media.errors.get(url, {"status": "unknown", "reason": "unknown"}),
                                    "message": error_message(media.errors.get(url, {}))}
                              for url in sorted(media.mapping) if not media.mapping[url]}
    report["images_failed"] = [url for url, error in report["media_errors"].items() if error["status"] == "failed"]
    report["images_unavailable"] = sum(path is None for path in image_map.values())
    report["issues"] = [issue for issue in report["issues"] if not re.fullmatch(
        r"Не удалось сохранить \d+ изображений\. Их ссылки оставлены на страницах\.", issue)]
    if report["images_failed"]:
        report["issues"].append(f"Не удалось сохранить {len(report['images_failed'])} изображений. Их ссылки оставлены на страницах.")
    if (images or userpics) and cancel.is_set():
        report["status"] = "cancelled"
        report["issues"].append("Скачивание изображений остановлено. Полученные тексты сохранены.")
    # Content completeness and media availability are reported separately.
    report["media_complete"] = not report["images_unavailable"]
    if images and not report["media_complete"] and report["status"] == "complete":
        report["status"] = "partial"
        report["issues"].append("Не все изображения получены.")
    if budget:
        report.update(budget.snapshot())
        if budget.exhausted and images and report["images_unavailable"]:
            report["issues"].append("Скачивание изображений прекращено по общему пределу HTTP-запросов.")
    with store.db:
        media.save_state(store)
    if progress:
        progress(phase='render', message='Профиль и переписка', completed=0, total=0)
    from .personal import render_personal
    render_personal(store, report)
    from .progress import write_timeline
    write_timeline(store, report)
    if progress:
        progress(phase='render', message='Статистика', completed=0, total=0)
    from .statistics import write_statistics
    write_statistics(store, report)
    if progress:
        progress(phase='render', message='JSON', completed=0, total=0)
    export_json(store, split=True)
    atomic_write(folder / "export-report.json", dumps(report))
    # JS file rather than fetch(): search also works when opened through file://.
    atomic_write(folder / "assets" / "search-data.js", "window.ARCHIVE=" + dumps({"entries": summaries, "documents": search}).replace("<", "\\u003c") + ";")
    atomic_write(folder / "index.html", ENV.get_template("index.html").render(user=user, entries=summaries, report=report))
    atomic_write(folder / "report.html", ENV.get_template("report.html").render(user=user, report=report))
    atomic_write(folder / "help.html", ENV.get_template("help.html").render(user=user, report=report))
    from .integrity import write_manifest
    if progress:
        progress(phase='render', message='Проверка файлов', completed=0, total=0)
    write_manifest(folder)
    return report
