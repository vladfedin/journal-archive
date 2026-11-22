"""Read-only inventory of known gaps, and comparison within one resume attempt.

Counts use stable identities, not subtraction of unrelated totals. URLs and source
texts never enter the job status. An unknown inventory is not an empty inventory.
Each gap is either open (will be requested again) or closed: asked for ATTEMPTS
times and treated as absent. Reasons belong to their own category.
"""
from collections import Counter
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from .entries import is_repost
from .client import Cancelled
from .gaps import ATTEMPTS, closed
from .media import plain_reason, settle_browser_answer
from .storage import Store
from .userpics import resolve, picture_url

# Labels name what the person recognises; the count beside each is a number of these things.
LABELS = {'days': 'Дни, за которые ЖЖ выдал не все записи', 'posts': 'Записи',
          'discussions': 'Обсуждения', 'images': 'Изображения и юзерпики',
          'videos': 'Видео', 'reactions': 'Счётчики реакций', 'authors': 'Списки отреагировавших',
          'personal': 'Разделы профиля и переписки'}
NOT_YET = 'ещё не запрашивались'


def image_issue(text):
    return text == 'Не все изображения получены.' or bool(re.fullmatch(
        r'Не удалось сохранить \d+ изображений\. Их ссылки оставлены на страницах\.', text))


def images_only_partial(report):
    issues = report.get('issues') or []
    return (report.get('status') == 'partial' and bool(issues) and all(map(image_issue, issues))
            and not any(report.get(key) for key in ('request_limit_reached', 'pending_days',
                        'pending_comment_entry_ids', 'orphan_entry_ids', 'unknown_encoding_entry_ids')))


def requested_gaps_settled(report, recovery):
    return (recovery.get('discovery_complete') and not any(row['remaining'] for row in recovery['rows']
            if row['key'] != 'videos' or report.get('videos_requested') is not False))


def fingerprint(url):
    return hashlib.sha256(url.encode()).hexdigest()


def inventory(store):
    return inventory_snapshot(metadata_snapshot(store))


def metadata_snapshot(store):
    """Read metadata together; defer JSON decoding and filesystem work until after close."""
    keys = ('sync_job', 'entry_comments', 'entry_materials', 'media', 'media_errors', 'bodies_done',
            'userpic_catalog', 'username', 'personal_requested', 'personal_images_requested', 'videos', 'posts_done',
            'journal_id', 'reaction_count_attempts', 'comments_requested')
    settings = dict(store.db.execute('SELECT key,value FROM settings WHERE key IN (' + ','.join('?' for _ in keys) + ')', keys))
    requested = json.loads(settings.get('personal_requested', '{}'))
    return {'folder': store.folder, 'settings': settings, 'counts': store.counts(),
            'entries': store.db.execute("SELECT id,json_remove(payload,'$.event','$.reactions') FROM entries").fetchall(),
            'comments': store.db.execute("SELECT c.id,c.entry_id,json_remove(c.payload,'$.body','$.reactions'),m.state FROM comments c LEFT JOIN comment_meta m ON m.id=c.id").fetchall(),
            'days': store.db.execute('SELECT day FROM days WHERE done!=1').fetchall(),
            'userpics': store.db.execute("SELECT payload FROM account_sections WHERE id='userpics'").fetchone() if requested.get('account') else None,
            'messages': store.db.execute('SELECT payload FROM messages').fetchall() if requested.get('messages') else [],
            'reactions': store.db.execute("SELECT kind,object_id,json_extract(payload,'$.count'),json_extract(payload,'$.authors.status'),json_remove(json_extract(payload,'$.authors'),'$.users') FROM reactions").fetchall(),
            'sections': store.db.execute("SELECT id, json_extract(payload,'$.status'), json_extract(payload,'$.error') FROM account_sections").fetchall()}


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('Проверка остановлена. Можно продолжить позже.')


def inventory_snapshot(data, *, cancel=None, progress=None, whole=False):
    def tick(completed, total, message):
        check_cancel(cancel)
        if progress and (completed == 1 or completed % 100 == 0 or completed == total):
            progress(message=message, completed=completed, total=total)
    check_cancel(cancel)
    settings = {key: json.loads(value) for key, value in data['settings'].items()}
    units = {key: set() for key in LABELS}
    shut = {key: set() for key in LABELS}
    reasons = {key: Counter() for key in LABELS}
    def gap(key, identity, record=None, reason=None):
        """Open or closed by the owning record; the reason stays with its category."""
        final = closed(record)
        (shut if final else units)[key].add(identity)
        if reason:
            reasons[key][(reason, final)] += 1
    pictures = Counter()
    # Settled answers, not gaps: the browser with the user's login was told the file is gone or closed.
    confirmed = Counter()
    sync = settings.get('sync_job', {})
    # A resume plan covers the entries of the current task; the library card covers the whole archive.
    selected = sync.get('selected') if sync.get('phase') != 'posts' and not whole else None
    selected = set(selected) if selected is not None else None
    statuses, materials, mapping = (settings.get(key, {}) for key in ('entry_comments', 'entry_materials', 'media'))
    errors = settings.get('media_errors', {})
    bodies_done = settings.get('bodies_done', False)
    catalog, owner = settings.get('userpic_catalog'), settings.get('username')
    uid = settings.get('journal_id') if catalog is not None else None
    entries, urls, reaction_targets = set(), set(), set()
    def picture(item, kind, oid):
        pic = resolve(item, kind, catalog, owner=owner, owner_id=uid)
        if pic['selection'] in ('unknown', 'assumed_default'):
            pictures[pic['selection']] += 1
        if pic['url']:
            urls.add(pic['url'])
    records = len(data['entries']) + len(data['comments'])
    for index, row in enumerate(data['entries'], 1):
        tick(index, records, 'Проверяем записи и комментарии…')
        eid, entry = row[0], json.loads(row[1])
        if selected is not None and eid not in selected:
            continue
        entries.add(eid)
        status = statuses.get(str(eid), {})
        if not is_repost(entry) and not status.get('done', bodies_done):
            denied = 'отклонил запрос' in (status.get('error') or '')
            gap('discussions', str(eid), status, 'нет доступа' if denied else
                ('ЖЖ отдал не все комментарии' if closed(status) else 'прошлый ответ был неполным')
                if status.get('error') else NOT_YET)
        picture(entry, 'post', eid)
        urls.update(materials.get(str(eid), {}).get('urls', []))
        if entry.get('ditemid') or entry.get('anum') is not None:
            reaction_targets.add(('post', eid))
    for index, row in enumerate(data['comments'], len(data['entries']) + 1):
        tick(index, records, 'Проверяем записи и комментарии…')
        if row[1] in entries:
            item = json.loads(row[2])
            item['state'] = row[3] or item.get('state', 'A')
            if settings.get('comments_requested') is False or item.get('source_status') == 'hidden_from_reader' or item['state'] in ('D', 'X'):
                continue
            picture(item, 'comment', row[0])
            if item.get('source_status') != 'not_returned' and ('post', row[1]) in reaction_targets:
                reaction_targets.add(('comment', row[0]))
    for row in data['days']:
        gap('days', row[0], None, 'ЖЖ выдал не все записи дня')
    attempts = sync.get('unavailable_attempts', {})
    for eid in sync.get('unavailable', []):
        tried = attempts.get(str(eid), 1)
        gap('posts', str(eid), {'closed': tried >= ATTEMPTS}, 'ЖЖ не вернул запись: удалена или скрыта')
    requested = settings.get('personal_requested', {})
    if requested.get('account'):
        urls.update((catalog or {}).get('pickwurls') or [])
        urls.add((catalog or {}).get('defaultpicurl'))
        record = data['userpics']
        if record:
            urls.update(p['url'] for p in (json.loads(record[0]).get('data') or {}).get('pictures', []))
    if requested.get('messages'):
        from .progress import Images
        images_requested = settings.get('personal_images_requested', False)
        for row in data['messages']:
            check_cancel(cancel)
            item = json.loads(row[0])
            urls.add(picture_url(item.get('picture')))
            if images_requested:
                parser = Images('https://www.livejournal.com/')
                parser.feed(item.get('body') if isinstance(item.get('body'), str) else '')
                urls.update(parser.urls)
    urls.discard(None); urls.discard('')
    for index, url in enumerate(urls, 1):
        tick(index, len(urls), 'Проверяем сохранённые изображения…')
        path = mapping.get(url)
        if not path or not (data['folder'] / path).is_file():
            error = settle_browser_answer(url, errors.get(url, {}))
            if error.get('availability') == 'unavailable':
                # 403 is a refusal; 404/410 means the file is gone. They are told apart, not merged.
                confirmed['images_refused' if error.get('http_status') == 403 else 'images_missing'] += 1
                continue
            gap('images', fingerprint(url), error, plain_reason(error) if error else NOT_YET)
    for url, record in settings.get('videos', {}).items():
        check_cancel(cancel)
        if record.get('status') == 'not_requested' or not entries.intersection(record.get('entries', [])):
            continue
        if record.get('status') != 'saved' or not record.get('path') or not (data['folder'] / record['path']).is_file():
            error = record.get('error', {})
            reason = {'embed_unresolved': 'не найден адрес видео', 'cancelled': NOT_YET, 'request_limit': NOT_YET,
                      'http': plain_reason(error), 'storage': 'не удалось записать на диск',
                      'auth_required': 'нужен вход в ЖЖ'}.get(error.get('reason'), 'видео не скачалось' if error.get('reason') else NOT_YET)
            gap('videos', fingerprint(url), record, reason)
    saved = {(r[0], r[1]): (r[2], r[3], r[4]) for r in data['reactions']}
    given_up = {key for key, n in settings.get('reaction_count_attempts', {}).items() if n >= ATTEMPTS}
    for target in reaction_targets:
        key = f'{target[0]}:{target[1]}'
        record = saved.get(target)
        if record is None or record[0] is None:
            gap('reactions', key, {'closed': key in given_up}, 'ЖЖ не отдал счётчик' if key in given_up else NOT_YET)
            continue
        if key in given_up:
            gap('reactions', key, {'closed': True}, 'сохранено прежнее значение счётчика')
        if record[1] != 'complete':
            authors = json.loads(record[2]) if record[2] else None
            gap('authors', key, authors, 'ЖЖ показал не всех' if record[1] == 'partial' else
                'ЖЖ не отдал список' if record[1] == 'unavailable' else NOT_YET)
    required = set()
    if requested.get('account'):
        required.update(('profile', 'foaf', 'friends', 'tags', 'userpics', 'settings_display', 'settings_privacy', 'settings_notifications'))
    if requested.get('messages'):
        required.update(('messages_incoming', 'messages_outgoing'))
    sections = {key: (status, error) for key, status, error in data['sections']}
    for key in required:
        status, reason = sections.get(key, (None, None))
        if status not in ('captured', 'source_exhausted'):
            gap('personal', key, None, reason or ('раздел получен частично' if status else NOT_YET))
    return {'units': units, 'closed': shut, 'reasons': reasons, 'pictures': dict(pictures), 'confirmed': dict(confirmed),
            'discovery_complete': bool(settings.get('posts_done')), 'counts': data['counts']}


def read_inventory(folder, *, cancel=None, progress=None, whole=False):
    check_cancel(cancel)
    if progress:
        progress(message='Читаем сохранённый прогресс…', completed=0, total=0)
    try:
        store = Store.reader(folder)
        try:
            if cancel is not None:
                store.db.set_progress_handler(lambda: int(cancel.is_set()), 10000)
            data = metadata_snapshot(store)
        finally:
            store.close()
        return inventory_snapshot(data, cancel=cancel, progress=progress, whole=whole)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        check_cancel(cancel)
        return None


def summary(current, before=None):
    """Rows per category: open gaps, closed ones, and what this attempt resolved.

    before/resolved/new compare identities with the inventory taken when a resume began.
    A gap that moved from open to closed is not resolved: it is still absent.
    """
    if current is None:
        return {'available': False, 'message': 'Список пропусков пока недоступен.'}
    def missing(inventory, key):
        return set(inventory['units'][key]) | set(inventory.get('closed', {}).get(key, set()))
    rows = []
    for key, label in LABELS.items():
        remaining, shut = current['units'][key], current['closed'][key]
        now = remaining | shut
        initial = missing(before, key) if before else set()
        if now or initial:
            reasons = sorted(current['reasons'][key].items(), key=lambda item: (-item[1], item[0][0]))
            rows.append({'key': key, 'label': label, 'before': len(initial) if before else None,
                         'resolved': len(initial - now) if before else None,
                         'remaining': len(remaining), 'unavailable': len(shut),
                         'new': len(now - initial) if before else 0,
                         'reasons': [{'message': message, 'final': final, 'count': count}
                                     for (message, final), count in reasons]})
    pictures = current.get('pictures', {})
    return {'available': True, 'rows': rows, 'discovery_complete': current['discovery_complete'],
            'remaining': sum(r['remaining'] for r in rows), 'unavailable': sum(r['unavailable'] for r in rows),
            'resolved': sum(r['resolved'] or 0 for r in rows), 'max_attempts': ATTEMPTS,
            'pictures': {'assumed': pictures.get('assumed_default', 0), 'unknown': pictures.get('unknown', 0)},
            'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
