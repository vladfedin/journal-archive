"""Chronological progress derived from durable checkpoints; no network on reads."""
import json
from collections import Counter, defaultdict
from html.parser import HTMLParser
from urllib.parse import urljoin

from .storage import atomic_write, dumps


class Images(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.urls = base, set()

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            src = dict(attrs).get('src')
            if src:
                self.urls.add(urljoin(self.base, src))


def capture_materials(exporter, entry):
    from .media import Media
    from .render import original_url, text_value
    from .userpics import resolve
    store, eid = exporter.store, int(entry['itemid'])
    media = getattr(exporter, '_media', None)
    if media is None:
        media = exporter._media = Media(store.folder, exporter.images, exporter.client.cancel,
                                       getattr(exporter.client, 'budget', None))
        media.mapping.update(store.get('media', {}))
    comments = store.comments(eid)
    parser = Images(original_url(exporter.client.user, entry))
    pictures = set()
    for kind, items in (('post', [entry]), ('comment', comments)):
        for item in items:
            parser.feed(text_value(item.get('event' if kind == 'post' else 'body')))
            pic = resolve(item, kind, store.get('userpic_catalog'), owner=exporter.client.user)
            if pic['url']:
                pictures.add(pic['url'])
    parser.close()
    requested = [(url, 'userpics') for url in sorted(pictures)]
    if exporter.images:
        requested += [(url, 'images') for url in sorted(parser.urls - pictures)]
    state = store.get('entry_materials', {})
    def checkpoint():
        state[str(eid)] = {'images': len(parser.urls), 'userpics': len(pictures),
                          'urls': [u for u, _ in requested], 'requested': len(requested), 'saved': sum(bool(media.mapping.get(u)) for u, _ in requested)}
        with store.db:
            store.set('entry_materials', state)
            store.set('media', {**store.get('media', {}), **media.mapping})
    checkpoint()
    for url, category in requested:
        exporter.client.check()
        exporter.update('materials', f'Материалы записи #{eid}', entry_id=eid)
        media.get(url, required=category == 'userpics', category=category)
        checkpoint()


def timeline(store, *, task=False):
    statuses = store.get('entry_comments', {})
    materials = store.get('entry_materials', {})
    sync_state = store.get('sync_job', {})
    selected = sync_state.get('selected')
    selected = set(selected) if selected is not None else None
    bodies_done = store.get('bodies_done', False)
    mapping = store.get('media', {})
    counts, authors = defaultdict(int), defaultdict(Counter)
    # Aggregate in SQLite; neither HTML bodies nor full threads enter the progress API.
    for row in store.db.execute('''SELECT c.entry_id, u.name, COUNT(*) FROM comments c
            LEFT JOIN comment_meta m ON m.id=c.id
            LEFT JOIN users u ON u.id=COALESCE(m.posterid,json_extract(c.payload,'$.posterid'))
            GROUP BY c.entry_id,u.name'''):
        counts[row[0]] += row[2]
        if row[1]:
            authors[row[0]][row[1]] += row[2]
    items, by_day = [], Counter()
    from .render import plain
    for row in store.db.execute('''SELECT id,json_extract(payload,'$.eventtime'),
            json_extract(payload,'$.subject'),json_extract(payload,'$.reply_count') FROM entries ORDER BY 2,id'''):
        eid, date, title, expected = row
        if task and selected is not None and sync_state.get('phase') != 'posts' and eid not in selected:
            continue
        if task and sync_state.get('phase') == 'posts' and sync_state.get('scope', {}).get('mode') == 'period':
            scope = sync_state['scope']
            if (scope.get('start') and (date or '')[:10] < scope['start']) or (scope.get('end') and (date or '')[:10] > scope['end']):
                continue
        date = date or ''
        by_day[date[:10]] += 1
        status, mat = statuses.get(str(eid), {}), dict(materials.get(str(eid), {}))
        if 'urls' in mat:
            mat['saved'] = sum(bool(mapping.get(u)) for u in mat.pop('urls'))
        done = status.get('done', bodies_done)
        state = 'pending' if not done else 'ready'
        if status.get('error') or (done and mat.get('saved', 0) < mat.get('requested', 0)):
            state = 'partial'
        elif not done and counts[eid]:
            state = 'loading'
        items.append({'id': eid, 'date': date, 'title': plain(title) or 'Без заголовка',
                      'url': f'entries/{eid}.html', 'state': state, 'error': status.get('error'),
                      'comments': counts[eid], 'expected_comments': expected, 'pages': status.get('pages'),
                      'page': status.get('last_valid_page', max(0, status.get('next_page', 1) - 1)), 'materials': mat,
                      'top': authors[eid].most_common(3)})
    gaps = []
    for row in store.db.execute('SELECT day,expected FROM days ORDER BY day'):
        missing = max(0, row[1] - by_day[row[0]])
        if missing:
            gaps.append({'date': row[0], 'count': missing})
    items.sort(key=lambda item: (item['date'], item['id']))
    return {'entries': items, 'gaps': gaps, 'known': len(items), 'expected': len(items) + sum(g['count'] for g in gaps),
            'ready': sum(i['state'] == 'ready' for i in items), 'partial': sum(i['state'] == 'partial' for i in items)}


def write_timeline(store, report):
    from .render import ENV
    data = timeline(store)
    atomic_write(store.folder / 'assets' / 'timeline-data.js', 'window.TIMELINE=' + dumps(data).replace('<', '\\u003c') + ';')
    atomic_write(store.folder / 'timeline.html', ENV.get_template('timeline.html').render(user=report['username'], report=report))


def preview_entry(store, eid):
    """Render one committed post while the writer continues. Never fetch media here."""
    from .render import ENV, safe_content, original_url, plain, threaded, needs_autoformat
    from .userpics import describe
    from .presentation import reaction_view
    from .media import Media
    import threading
    row = store.db.execute('SELECT payload FROM entries WHERE id=?', (eid,)).fetchone()
    if not row:
        return None
    entry, user = json.loads(row[0]), store.get('username')
    comments = threaded(store.comments(eid))
    pictures = describe(store, [entry], comments)
    media = Media(store.folder, False, threading.Event())
    media.mapping = store.get('media', {})
    url = original_url(user, entry)
    try:
        names = {c['id']: c.get('user') or 'Аноним' for c in comments}
        for c in comments:
            c.update(user=names[c['id']], parent_user=names.get(c.get('parentid')), by_owner=names[c['id']] == user,
                     userpic_view=pictures['comment'].get(str(c['id'])), state_label={'S': 'Скрыт', 'D': 'Удалён', 'X': 'Удалён'}.get(c['state'], ''),
                     subject_text=plain(c.get('subject')), autoformat=needs_autoformat(c.get('body'), c.get('props')),
                     reaction_view=reaction_view(c.get('reactions')),
                     html=safe_content(c.get('body', 'Текст пока не получен.'), url, {}, media, set()))
        security = entry.get('security', 'public')
        summary = {'title': plain(entry.get('subject')) or 'Без заголовка', 'date': entry.get('eventtime', ''),
                   'security': security, 'security_label': {'private': 'Личная', 'usemask': 'Для друзей / группы'}.get(security, security),
                   'userpic': pictures['post'].get(str(eid)), 'tags': [], 'reactions': reaction_view(store.reactions('post').get(eid))}
        return ENV.get_template('entry.html').render(user=user, entry=entry, summary=summary, comments=comments,
                     content=safe_content(entry.get('event'), url, {}, media, set(), expand_cuts=True), original=url,
                     post_autoformat=needs_autoformat(entry.get('event'), entry.get('props')), current=[], newer=None, older=None,
                     preview=True, report={'created_at': store.get('started_at', '')})
    finally:
        media.close()
