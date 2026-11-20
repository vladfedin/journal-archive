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

from . import __version__
from .media import Media
from .data import export_json
from .reactions import reaction_report
from .presentation import reaction_view
from .userpics import describe, coverage
from .gaps import closed
from .storage import Store, atomic_write, dumps

# Bump when previously generated offline pages or assets need rebuilding.
ARCHIVE_FORMAT_VERSION = "0.9.0.2"
ROOT = Path(__file__).parent
ENV = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
TAGS = frozenset("p br div span a b strong i em u s del blockquote pre code ul ol li h1 h2 h3 h4 h5 h6 hr img video table thead tbody tr td th caption dl dt dd sub sup details summary figure figcaption".split())
ATTRS = {"a": ["href", "title"], "video": ["src", "data-lj-module"], "img": ["src", "alt", "title", "width", "height"],
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
ENV.globals.update(task_label=task_label, app_version=__version__)


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
    def __init__(self, source, base, links, media, comment_ids, thread_id=None):
        super().__init__(convert_charrefs=False)
        self.out, self.base, self.links, self.media = [], base, links, media
        self.comment_ids = comment_ids
        self.thread_id = thread_id
        self.preserve_depth = 0
        self.link_sources = []
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
        if tag == 'video':
            from .video import canonical_video, embed_key, external_embed, lj_video_id
            module = attrs.get('data-lj-module', '')
            if module.isdigit():
                widget = self.media.external_embeds.get(embed_key(self.base, module))
                if widget and external_embed(widget.get('embed_url')):
                    self.out.append('<span class="external-widget" data-widget-url="' + html.escape(widget['embed_url'], quote=True) + '"><a href="' + html.escape(widget['url'], quote=True) + '" target="_blank" rel="noreferrer noopener">Яндекс Музыка ↗</a><small>Онлайн · аудиофайл не сохранён</small></span>')
                else:
                    self.out.append(self.material_slot('embed', module, self.base, 'Встроенный материал ↗'))
                return
            url = canonical_video(attrs.get('src'), self.base)
            original = url
            if url and lj_video_id(url):
                original = self.base
                if self.thread_id and str(self.thread_id).isdigit():
                    original = urlsplit(self.base)._replace(query=f'thread={self.thread_id}', fragment='').geturl()
            record = self.media.videos.get(url, {})
            path = record.get('path')
            local = self.media.folder / path if path else None
            if (record.get('status') == 'saved' and local and local.is_file()
                    and local.resolve().is_relative_to(self.media.folder.resolve())):
                self.out.append('<video controls preload="none" src="../' + html.escape(path, quote=True) + '"></video>')
            elif url:
                self.out.append(self.material_slot('video', url, original, 'Видео в оригинале ↗'))
                return
            if url:
                self.out.append('<a class="video-original" href="' + html.escape(original, quote=True) +
                                '" target="_blank" rel="noreferrer noopener">Видео в оригинале ↗</a>')
            return
        if tag == "img":
            src = urljoin(self.base, attrs.get("src", ""))
            local = self.media.get(src, referer=self.base)
            if local:
                self.out.append(f'<img src="../{html.escape(local, quote=True)}" alt="{html.escape(attrs.get("alt", ""), quote=True)}" loading="lazy">')
            else:
                self.out.append(self.material_slot('image', src, self.local_link(src), attrs.get('alt') or 'Изображение в оригинале ↗'))
            return
        if tag == "a":
            from .video import canonical_video
            original_link = urljoin(self.base, attrs.get('href', ''))
            video_url = canonical_video(attrs.get('href'), self.base)
            self.link_sources.append(original_link if video_url else None)
            attrs["href"] = self.local_link(attrs.get("href", ""))
            attrs["rel"] = "noreferrer noopener"
            if video_url:
                # Keep authored link text; the local UI adds a download control beside it.
                attrs['data-video-url'] = video_url
                record = self.media.videos.get(video_url, {})
                path = record.get('path')
                if record.get('status') == 'saved' and path and (self.media.folder / path).is_file() and (self.media.folder / path).resolve().is_relative_to(self.media.folder.resolve()):
                    attrs['data-video-path'] = '../' + path
                    self.link_sources[-1] = None
        self.out.append("<" + tag + "".join(f' {k}="{html.escape(v or "", quote=True)}"' for k, v in attrs.items()) + ">")

    def material_slot(self, kind, value, original, label):
        esc = lambda s: html.escape(str(s), quote=True)
        error = self.media.errors.get(value) if kind == 'image' else None
        if kind == 'video':
            record = self.media.videos.get(value, {})
            if record.get('error'):
                from .video import video_error_message
                error = {**record['error'], 'message': video_error_message(record), 'trace': record.get('trace', []),
                         'requests': record.get('requests'), 'updated_at': record.get('checked_at')}
        detail = ''
        if error:
            from .media import error_message
            detail = '<span class="material-last-result">' + esc(error.get('message') or error_message(error)) + '</span>'
        source = '<span class="material-source">' + esc(original) + '</span>'
        if urlsplit(original).scheme in ('http', 'https'):
            source = '<a class="material-source" href="' + esc(original) + '" target="_blank" rel="noreferrer noopener">' + esc(original) + '</a>'
        return ('<span class="material-slot" data-material-kind="' + kind + '" data-material-value="' + esc(value) +
                '"' + (' data-material-error="' + esc(dumps(error)) + '"' if error else '') +
                '><a class="media-link" href="' + esc(original) + '" target="_blank" rel="noreferrer noopener">' +
                html.escape(label) + '</a>' + source + detail + '</span>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in ("pre", "code"):
            self.preserve_depth = max(0, self.preserve_depth - 1)
        if tag not in ("img", "br", "hr", "video"):
            self.out.append(f"</{tag}>")
        if tag == 'a' and self.link_sources:
            source = self.link_sources.pop()
            if source:
                escaped = html.escape(source, quote=True)
                self.out.append(f'<a class="material-source" href="{escaped}" target="_blank" rel="noreferrer noopener">{escaped}</a>')

    def handle_data(self, data):
        if not self.preserve_depth:
            data = re.sub(r"\n[ \t]*(?:\n[ \t]*){2,}", "\n\n", data)
        self.out.append(data)

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")


def safe_content(value, base, links, media, comment_ids, *, expand_cuts=False, thread_id=None):
    source = text_value(value)
    source = re.sub(r'<(script|style)\b[^>]*>.*?</\1\s*>', "", source, flags=re.I | re.S)
    from .video import video_markup
    source = video_markup(source, base, media)
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
    return Markup("".join(Content(cleaned, base, links, media, comment_ids, thread_id).out))


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


def entry_summary(entry, comments, screened, picture):
    from .entries import is_repost, repost_url, author
    eid = int(entry['itemid'])
    props = entry.get('props') if isinstance(entry.get('props'), dict) else {}
    security = entry.get('security', 'public')
    date = text_value(entry.get('eventtime'))
    return {'repost': is_repost(entry), 'repost_url': repost_url(entry), 'userpic': picture, 'author': author(entry),
            'id': eid, 'title': plain(entry.get('subject')) or 'Без заголовка', 'date': date, 'year': date[:4],
            'tags': [t.strip() for t in text_value(props.get('taglist')).split(',') if t.strip()],
            'security': security, 'security_label': {'public': 'Открытая', 'private': 'Личная', 'usemask': 'Для друзей / группы'}.get(security, security),
            'comments': comments, 'screened': screened, 'excerpt': excerpt(plain(entry.get('event'))),
            'url': f'entries/{eid}.html', 'reactions': reaction_view(entry.get('reactions'))}


def order_feed(rows, sticky_entry_id):
    for row in rows:
        row['sticky'] = row['id'] == sticky_entry_id
    rows.sort(key=lambda row: (row['sticky'], row['date'], row['id']), reverse=True)


def render_feed(entry, user, links, media, comment_ids):
    props = entry.get('props') if isinstance(entry.get('props'), dict) else {}
    current = [(label, plain(props[key])) for key, label in CURRENT if plain(props.get(key)).strip()]
    feed = ENV.get_template('feed-post.html').render(
        content=safe_content(entry.get('event'), original_url(user, entry), links, media, comment_ids),
        current=current, post_autoformat=needs_autoformat(entry.get('event'), props))
    feed = re.sub(r'href="([0-9]+\.html(?:#[^"\s]*)?)"', r'href="entries/\1"', feed)
    return feed.replace('src="../media/', 'src="media/').replace('data-video-path="../media/', 'data-video-path="media/')


def build_archive(store: Store, report: dict, *, images=False, userpics=False, client_cancel=None, progress=None, budget=None, media=None, client=None, complete_after_media=False, entry_ids=None, changed_media=()):
    from .recovery import images_only_partial, requested_gaps_settled, inventory, summary as recovery_summary
    repair_image_status = images_only_partial(report)
    finished = report.get("status") == "complete" or report.get("finished_with_skips", False) or repair_image_status
    folder, user = store.folder, report["username"]
    entry_ids = None if entry_ids is None else set(entry_ids)
    update_assets = (entry_ids is None or report.get('format_version') != ARCHIVE_FORMAT_VERSION
                     or report.get('gallery_version') != 4)
    changed_assets = []
    cancel = client_cancel or threading.Event()
    media = media or Media(folder, images, cancel, budget=budget, client=client)
    media.enabled = images
    media.load_state(store)
    entries, comments = store.entries(), store.comments()
    if entry_ids is not None:
        from .progress import Images
        from .entries import is_repost
        by_id = {int(e['itemid']): e for e in entries}
        for item in [*entries, *comments]:
            eid = int(item.get('itemid') or item.get('jitemid') or 0)
            if 'jitemid' in item and is_repost(by_id.get(eid, {})):
                continue
            parser = Images(original_url(user, by_id.get(eid, {'itemid': eid})))
            parser.feed(text_value(item.get('event', item.get('body')))); parser.close()
            for url in parser.urls:
                media.used_urls['images'].add(url)
                path = media.mapping.get(url)
                media.mapping[url] = path if path and (folder / path).is_file() else None
    if entry_ids is None:
        report["format_version"] = ARCHIVE_FORMAT_VERSION
    from .sources import stored_source
    report['source'] = stored_source(store)
    if entry_ids is None:
        report["calendar_version"] = 2
        report["gallery_version"] = 4
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
    report["userpics"] = coverage(task_pictures, media.errors)
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
    if update_assets:
        for asset in ("archive.css", "typography.css", "archive.js", "theme.js", "common.js", "favicon.svg", "favicon.ico", "timeline.js", "statistics.js", "statistics-live.js", "calendar.js", "calendar.css", "material-actions.js", "gallery.js", "profile.js"):
            shutil.copyfile(ROOT / "static" / asset, folder / "assets" / asset)
            changed_assets.append('assets/' + asset)
    search, summaries = [], []
    from .comment_state import comment_view
    from .entries import is_repost, repost_url
    from .progress import material_progress, coverage_notice
    comment_statuses, material_records = store.get("entry_comments", {}), store.get("entry_materials", {})
    saved_media = store.get("media", {})
    from .video import missing_by_entry
    missing_videos = missing_by_entry(store)
    try:
        rendered_count = 0
        render_total = len(entries) if entry_ids is None else len(entry_ids & {int(e['itemid']) for e in entries})
        for index, entry in enumerate(entries):
            eid = int(entry["itemid"])
            if entry_ids is not None and eid not in entry_ids:
                raw_comments = [] if is_repost(entry) else by_entry[eid]
                summaries.append(entry_summary(entry, len(raw_comments), sum(c.get('state') == 'S' for c in raw_comments), pictures['post'].get(str(eid))))
                continue
            rendered_count += 1
            if progress:
                message = f"Обновляем запись {rendered_count} из {render_total}…" if entry_ids is not None else f"Собираем страницу {rendered_count} из {render_total}…"
                progress(phase="render", message=message, completed=rendered_count - 1, total=render_total)
            title = plain(entry.get("subject")) or "Без заголовка"
            url = original_url(user, entry)
            entry_comments = [] if is_repost(entry) else threaded(by_entry[eid])
            props = entry.get("props") if isinstance(entry.get("props"), dict) else {}
            summary = entry_summary(entry, len(entry_comments), sum(c['state'] == 'S' for c in entry_comments), pictures['post'].get(str(eid)))
            summaries.append(summary)
            search.append({"entry": eid, "title": title, "text": plain(entry.get("event")), "url": summary["url"], "kind": "post"})
            for item in entry_comments:
                item["user"] = item.get("user") or (f"Пользователь #{item['posterid']}" if item.get("posterid") else "Аноним")
            authors = {int(item["id"]): item["user"] for item in entry_comments}
            for item in entry_comments:
                item["userpic_view"] = pictures["comment"].get(str(item["id"]))
                item["by_owner"] = item["user"] == (summary.get('author') or user)
                item["parent_user"] = authors.get(int(item.get("parentid") or 0))
                item.update(comment_view(item, settled=closed(comment_statuses.get(str(eid)))))
                item["html"] = safe_content(item["display_body"], url, links, media, comment_ids, thread_id=item.get("dtalkid"))
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
                            material_progress(material_records.get(str(eid), {}), {**saved_media, **media.mapping}, media.errors), repost=is_repost(entry), videos=missing_videos.get(eid, 0)),
                        report=report)
            atomic_write(folder / "entries" / f"{eid}.html", rendered)
            feed = render_feed(entry, user, links, media, comment_ids)
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
    # Asked for several times and not given: final, shown as original links, not a reason to resume.
    report["images_closed"] = sum(path is None and closed(media.errors.get(url)) for url, path in image_map.items())
    open_failed = [url for url in report["images_failed"] if not closed(media.errors.get(url))]
    report["issues"] = [issue for issue in report["issues"] if not re.fullmatch(
        r"Не удалось сохранить \d+ изображений\. Их ссылки оставлены на страницах\.", issue)]
    if open_failed:
        report["issues"].append(f"Не удалось сохранить {len(open_failed)} изображений. Их ссылки оставлены на страницах.")
    if (images or userpics) and cancel.is_set():
        report["status"] = "cancelled"
        report["issues"].append("Скачивание изображений остановлено. Полученные тексты сохранены.")
    # Content completeness and media availability are reported separately.
    from .video import video_report
    report['videos'] = video_report(store, selected)
    report["media_complete"] = report["images_unavailable"] == report["images_closed"] and not (report.get('videos_requested') and report['videos']['missing'])
    if repair_image_status and report['media_complete'] and not complete_after_media:
        with store.db:
            media.save_state(store)
        complete_after_media = requested_gaps_settled(report, recovery_summary(inventory(store)))
    if complete_after_media and report['media_complete']:
        report['status'] = 'complete'
        report['issues'] = [issue for issue in report['issues'] if issue != 'Не все изображения получены.']
    if report.get('videos_requested') and report['videos']['missing']:
        if report['status'] == 'complete': report['status'] = 'partial'
        issue = 'Не все видео получены. Можно продолжить загрузку.'
        if issue not in report['issues']: report['issues'].append(issue)
    if images and not report["media_complete"] and report["status"] == "complete":
        report["status"] = "partial"
        report["issues"].append("Не все изображения получены.")
    if budget:
        report.update(budget.snapshot())
        if budget.exhausted and images and report["images_unavailable"]:
            report["issues"].append("Скачивание изображений прекращено по общему пределу HTTP-запросов.")
    with store.db:
        media.save_state(store)
    if entry_ids is None:
        if progress:
            progress(phase='render', message='Профиль и переписка', completed=0, total=0)
        from .personal import render_personal
        render_personal(store, report)
    from .gallery import write_gallery
    previous_gallery = {path.relative_to(folder).as_posix() for path in folder.glob('materials*.html')}
    from .personal import account_data
    from .fulltext import PARTS, write_journal, personal_docs, material_docs, write_part, write_page
    materials = write_gallery(store, report)
    write_part(folder, 'materials', material_docs(materials, entries, comments, user))
    from .skip_details import skipped_items
    report['skipped_items'] = skipped_items(report, materials, entries, comments,
                                            count_attempts=store.get('reaction_count_attempts', {}))
    report['finished_with_skips'] = bool(finished and report['status'] == 'partial'
        and not (cancel.is_set() or report.get('request_limit_reached'))
        and not any(e.get('reason') == 'storage' for e in report['media_errors'].values())
        and not any((v.get('error') or {}).get('reason') == 'storage' for v in report['videos']['missing'].values()))
    write_journal(store, entries, comments, entry_ids)
    if entry_ids is None or not (folder / 'assets/fulltext-personal.js').exists():
        write_part(folder, 'personal', personal_docs(user, account_data(store)))
    write_page(folder, report)
    from .progress import write_timeline
    write_timeline(store, report)
    if entry_ids is None:
        from .calendar import write_calendar
        write_calendar(store, report)
        if progress:
            progress(phase='render', message='Статистика', completed=0, total=0)
        from .statistics import write_statistics
        write_statistics(store, report)
    if progress:
        progress(phase='render', message='JSON', completed=0, total=0)
    export_json(store, split=True)
    order_feed(summaries, store.sticky_entry_id())
    atomic_write(folder / "export-report.json", dumps(report))
    atomic_write(folder / 'assets/feed-data.js', 'window.ARCHIVE=' + dumps({'entries': summaries}).replace('<', '\\u003c') + ';')
    # JS file rather than fetch(): search also works when opened through file://.
    if entry_ids is None:
        atomic_write(folder / "assets" / "search-data.js", "window.ARCHIVE=" + dumps({"entries": summaries, "documents": search}).replace("<", "\\u003c") + ";")
    atomic_write(folder / "index.html", ENV.get_template("index.html").render(user=user, entries=summaries, report=report))
    atomic_write(folder / "report.html", ENV.get_template("report.html").render(user=user, report=report))
    if entry_ids is None:
        atomic_write(folder / "help.html", ENV.get_template("help.html").render(user=user, report=report))
    from .integrity import write_manifest
    if progress:
        progress(phase='render', message='Проверка файлов', completed=0, total=0)
    changed = None
    if entry_ids is not None:
        changed = [*changed_media, *changed_assets, 'search.html', 'assets/fulltext.js', 'assets/feed-data.js',
                   *[f'assets/fulltext-{part}.js' for part in PARTS], 'index.html', 'report.html', 'export-report.json', 'timeline.html', 'assets/timeline-data.js',
                   *[f'entries/{eid}.html' for eid in entry_ids], *[f'assets/posts/{eid}.js' for eid in entry_ids],
                   *previous_gallery, *[path.relative_to(folder).as_posix() for path in folder.glob('materials*.html')],
                   *[f'data/{name}.json' for name in ('archive', 'activitystreams', 'activitystreams-context', 'entries', 'comments', 'comment-metadata', 'materials')]]
    write_manifest(folder, changed=changed)
    return report
