"""Read-only inventory of known gaps, and comparison within one resume attempt.

Counts use stable identities, not subtraction of unrelated totals. URLs and source
texts never enter the job status. An unknown inventory is not an empty inventory.
"""
from collections import Counter
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .entries import is_repost
from .media import error_message
from .storage import Store
from .userpics import resolve, picture_url

LABELS = {'days': 'Дни с неполной выдачей записей', 'posts': 'Не получены записи по ID',
          'discussions': 'Неполные обсуждения', 'images': 'Изображения и юзерпики',
          'videos': 'Видео', 'reactions': 'Счётчики реакций', 'authors': 'Авторы реакций',
          'pictures': 'Выбор юзерпиков',
          'personal': 'Разделы личных данных'}


def fingerprint(url):
    return hashlib.sha256(url.encode()).hexdigest()


def inventory(store):
    return inventory_snapshot(metadata_snapshot(store))


def metadata_snapshot(store):
    """Read metadata together; defer JSON decoding and filesystem work until after close."""
    keys = ('sync_job', 'entry_comments', 'entry_materials', 'media', 'media_errors', 'bodies_done',
            'userpic_catalog', 'username', 'personal_requested', 'personal_images_requested', 'videos', 'posts_done')
    settings = dict(store.db.execute('SELECT key,value FROM settings WHERE key IN (' + ','.join('?' for _ in keys) + ')', keys))
    requested = json.loads(settings.get('personal_requested', '{}'))
    return {'folder': store.folder, 'settings': settings, 'counts': store.counts(),
            'entries': store.db.execute("SELECT id,json_remove(payload,'$.event','$.reactions') FROM entries").fetchall(),
            'comments': store.db.execute("SELECT c.id,c.entry_id,json_remove(c.payload,'$.body','$.reactions'),m.state FROM comments c LEFT JOIN comment_meta m ON m.id=c.id").fetchall(),
            'days': store.db.execute('SELECT day FROM days WHERE done!=1').fetchall(),
            'userpics': store.db.execute("SELECT payload FROM account_sections WHERE id='userpics'").fetchone() if requested.get('account') else None,
            'messages': store.db.execute('SELECT payload FROM messages').fetchall() if requested.get('messages') else [],
            'reactions': store.db.execute("SELECT kind,object_id,json_extract(payload,'$.count'),json_extract(payload,'$.authors.status') FROM reactions").fetchall(),
            'captured': store.db.execute("SELECT id FROM account_sections WHERE json_extract(payload,'$.status') IN ('captured','source_exhausted')").fetchall()}


def inventory_snapshot(data):
    settings = {key: json.loads(value) for key, value in data['settings'].items()}
    units = {key: set() for key in LABELS}
    reasons = Counter()
    sync = settings.get('sync_job', {})
    selected = sync.get('selected') if sync.get('phase') != 'posts' else None
    selected = set(selected) if selected is not None else None
    statuses, materials, mapping = (settings.get(key, {}) for key in ('entry_comments', 'entry_materials', 'media'))
    errors = settings.get('media_errors', {})
    bodies_done = settings.get('bodies_done', False)
    catalog, owner = settings.get('userpic_catalog'), settings.get('username')
    entries, urls, reaction_targets = set(), set(), set()
    def picture(item, kind, oid):
        pic = resolve(item, kind, catalog, owner=owner)
        if pic['selection'] == 'unknown':
            units['pictures'].add(f'{kind}:{oid}')
        if pic['url']:
            urls.add(pic['url'])
    for row in data['entries']:
        eid, entry = row[0], json.loads(row[1])
        if selected is not None and eid not in selected:
            continue
        entries.add(eid)
        if not is_repost(entry) and not statuses.get(str(eid), {}).get('done', bodies_done):
            units['discussions'].add(str(eid))
        picture(entry, 'post', eid)
        urls.update(materials.get(str(eid), {}).get('urls', []))
        if entry.get('ditemid') or entry.get('anum') is not None:
            reaction_targets.add(('post', eid))
    for row in data['comments']:
        if row[1] in entries:
            item = json.loads(row[2])
            item['state'] = row[3] or item.get('state', 'A')
            if item['state'] in ('D', 'X'):
                item.pop('source_status', None)
            picture(item, 'comment', row[0])
            if item.get('source_status') != 'not_returned' and ('post', row[1]) in reaction_targets:
                reaction_targets.add(('comment', row[0]))
    units['days'] = {row[0] for row in data['days']}
    units['posts'] = {str(eid) for eid in sync.get('unavailable', [])}
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
            item = json.loads(row[0])
            urls.add(picture_url(item.get('picture')))
            if images_requested:
                parser = Images('https://www.livejournal.com/')
                parser.feed(item.get('body') if isinstance(item.get('body'), str) else '')
                urls.update(parser.urls)
    urls.discard(None); urls.discard('')
    for url in urls:
        path = mapping.get(url)
        if not path or not (data['folder'] / path).is_file():
            units['images'].add(fingerprint(url))
            if url in errors:
                reasons[error_message(errors[url])] += 1
    for url, record in settings.get('videos', {}).items():
        if record.get('status') == 'not_requested' or not entries.intersection(record.get('entries', [])):
            continue
        if record.get('status') != 'saved' or not record.get('path') or not (data['folder'] / record['path']).is_file():
            units['videos'].add(fingerprint(url))
            error = record.get('error', {})
            if error.get('reason') == 'http':
                reasons[error_message(error)] += 1
            elif error.get('reason'):
                reasons[{'embed_unresolved': 'Не получен адрес видео', 'cancelled': 'Загрузка остановлена',
                         'request_limit': 'Достигнут лимит запросов'}.get(error['reason'], 'Не удалось скачать видео')] += 1
    saved = {(r[0], r[1]): (r[2], r[3]) for r in data['reactions']}
    for target in reaction_targets:
        key = f'{target[0]}:{target[1]}'
        record = saved.get(target)
        if record is None or record[0] is None:
            units['reactions'].add(key)
        if record is None or record[1] != 'complete':
            units['authors'].add(key)
    required = set()
    if requested.get('account'):
        required.update(('profile', 'foaf', 'friends', 'tags', 'userpics', 'settings_display', 'settings_privacy', 'settings_notifications'))
    if requested.get('messages'):
        required.update(('messages_incoming', 'messages_outgoing'))
    captured = {r[0] for r in data['captured']}
    units['personal'] = required - captured
    if units['discussions']:
        reasons['Не подтверждена полнота обсуждения'] += len(units['discussions'])
    if units['pictures']:
        reasons['ЖЖ не сообщил, какой юзерпик использован'] += len(units['pictures'])
    return {'units': units, 'reasons': reasons, 'discovery_complete': bool(settings.get('posts_done')), 'counts': data['counts']}


def read_inventory(folder):
    try:
        store = Store.reader(folder)
        try:
            data = metadata_snapshot(store)
        finally:
            store.close()
        return inventory_snapshot(data)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def summary(current, before=None):
    if current is None:
        return {'available': False, 'message': 'Список пропусков пока недоступен.'}
    rows = []
    for key, label in LABELS.items():
        remaining = current['units'][key]
        initial = before['units'][key] if before else set()
        if remaining or initial:
            rows.append({'key': key, 'label': label, 'before': len(initial) if before else None,
                         'resolved': len(initial - remaining) if before else None,
                         'remaining': len(remaining), 'new': len(remaining - initial) if before else 0})
    return {'available': True, 'rows': rows, 'discovery_complete': current['discovery_complete'],
            'reasons': [{'message': message, 'count': count} for message, count in current['reasons'].most_common()],
            'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
