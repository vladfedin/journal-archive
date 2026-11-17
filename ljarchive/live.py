"""Read-only journal previews while the export writer owns the archive."""
import json
import threading
from urllib.parse import urlsplit

from .entries import is_repost
from .media import Media
from .render import ENV, entry_summary, original_url, plain, render_feed
from .storage import dumps
from .userpics import describe


def summaries(store):
    entries = store.entries()
    pictures = describe(store, entries, [])['post']
    counts = {row[0]: row[1:] for row in store.db.execute('''
        SELECT c.entry_id, COUNT(*), SUM(COALESCE(m.state,json_extract(c.payload,'$.state'))='S')
        FROM comments c LEFT JOIN comment_meta m ON m.id=c.id GROUP BY c.entry_id''')}
    result = []
    for entry in entries:
        eid = int(entry['itemid'])
        count, screened = (0, 0) if is_repost(entry) else counts.get(eid, (0, 0))
        result.append(entry_summary(entry, count, screened or 0, pictures.get(str(eid))))
    result.sort(key=lambda e: (e['date'], e['id']), reverse=True)
    return entries, result


def preview_index(store):
    _, rows = summaries(store)
    report = {'created_at': store.get('started_at', ''), 'status': 'partial',
              'entries_saved': len(rows), 'comments_saved': sum(e['comments'] for e in rows)}
    return ENV.get_template('index.html').render(user=store.get('username'), entries=rows, report=report, preview=True)


def preview_search(store):
    entries, rows = summaries(store)
    by_id = {e['id']: e for e in rows}
    documents = [{'entry': int(e['itemid']), 'title': by_id[int(e['itemid'])]['title'],
                  'text': plain(e.get('event')), 'url': by_id[int(e['itemid'])]['url'], 'kind': 'post'} for e in entries]
    for c in store.comments():
        entry = by_id.get(int(c.get('jitemid', 0)))
        if not entry or entry['repost']:
            continue
        documents.append({'entry': entry['id'], 'title': entry['title'],
                          'text': f"{c.get('user') or 'Аноним'} {plain(c.get('subject'))} {plain(c.get('body'))}",
                          'url': entry['url'] + '#comment-' + str(c['id']), 'kind': 'comment'})
    return 'window.ARCHIVE=' + dumps({'entries': rows, 'documents': documents}).replace('<', '\\u003c') + ';'


def preview_feed(store, eid):
    row = store.db.execute('SELECT payload FROM entries WHERE id=?', (eid,)).fetchone()
    if not row:
        return None
    entry, user = json.loads(row[0]), store.get('username')
    links = {}
    for itemid, url, ditemid, anum in store.db.execute("""SELECT id,json_extract(payload,'$.url'),
            json_extract(payload,'$.ditemid'),json_extract(payload,'$.anum') FROM entries"""):
        item = {'itemid': itemid, 'url': url, 'ditemid': ditemid}
        if anum is not None:
            item['anum'] = anum
        address = urlsplit(original_url(user, item))
        if address.path != '/':
            links[address.hostname, address.path] = f'{itemid}.html'
    comment_ids = {row[0] for row in store.db.execute('SELECT id FROM comments')}
    media = Media(store.folder, False, threading.Event())
    media.load_state(store)
    try:
        body = render_feed(entry, user, links, media, comment_ids)
        return 'window.ArchivePost(' + str(eid) + ',' + dumps(body).replace('<', '\\u003c') + ');'
    finally:
        media.close()
