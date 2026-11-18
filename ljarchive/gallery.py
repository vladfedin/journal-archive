"""Local material index with references back to the source posts and comments."""
import hashlib
from collections import defaultdict
from urllib.parse import urlsplit

from .progress import Images
from .storage import atomic_write, dumps
from .userpics import describe
from .video import VideoRefs, embed_key


def inventory(store):
    from .render import original_url, plain, text_value
    from .entries import is_repost
    entries = store.entries()
    comments = defaultdict(list)
    for comment in store.comments():
        comments[int(comment.get('jitemid', 0))].append(comment)
    known = {int(entry['itemid']) for entry in entries}
    entries.extend({'itemid': eid, 'subject': f'Запись #{eid} · текст недоступен', 'event': '', 'eventtime': ''}
                   for eid in comments if eid not in known)
    pictures = describe(store)
    mapping, errors = store.get('media', {}), store.get('media_errors', {})
    videos, embeds = store.get('videos', {}), store.get('video_embeds', {})
    rows = {}

    def add(kind, url, reference=None, title=''):
        if not url:
            return
        key = (kind, url)
        if key not in rows:
            record = videos.get(url, {}) if kind == 'video' else errors.get(url, {})
            path = record.get('path') if kind == 'video' else mapping.get(url)
            target = (store.folder / path).resolve() if isinstance(path, str) else None
            saved = bool(target and target.is_relative_to(store.folder.resolve()) and target.is_file())
            rows[key] = {'id': hashlib.sha256((kind + ':' + url).encode()).hexdigest(),
                         'kind': kind, 'url': url, 'path': path if saved else None,
                         'status': 'saved' if saved else 'unavailable' if record.get('closed') else 'missing',
                         'title': record.get('title') or title, 'references': []}
            try:
                parsed = urlsplit(url)
                rows[key]['source_href'] = url if parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password else None
            except ValueError:
                rows[key]['source_href'] = None
            if kind == 'video':
                from .video import video_error_message, lj_video_id
                rows[key]['provider'] = 'ЖЖ' if lj_video_id(url) else 'YouTube' if 'youtube.com/' in url else 'Видео'
                rows[key]['download_status'] = record.get('status', 'not_requested')
                rows[key]['bytes'] = record.get('bytes')
                rows[key]['error'] = {**record.get('error', {}), 'trace': record.get('trace', []),
                                      'requests': record.get('requests'), 'updated_at': record.get('checked_at')}
                rows[key]['message'] = video_error_message(record) if record.get('error') else ''
        if reference and reference not in rows[key]['references']:
            rows[key]['references'].append(reference)

    for entry in entries:
        eid = int(entry['itemid'])
        base = original_url(store.get('username'), entry)
        title = plain(entry.get('subject')) or 'Без заголовка'
        entry_comments = [] if is_repost(entry) else comments[eid]
        for kind, item in [('post', entry), *[('comment', c) for c in entry_comments]]:
            cid = int(item['id']) if kind == 'comment' else None
            reference = {'entry_id': eid, 'comment_id': cid, 'title': title,
                         'date': entry.get('eventtime', ''),
                         'href': f'entries/{eid}.html' + (f'#comment-{cid}' if cid else '')}
            source = text_value(item.get('event' if kind == 'post' else 'body'))
            images = Images(base); images.feed(source); images.close()
            for url in images.urls:
                add('image', url, reference, title)
            refs = VideoRefs(base); refs.feed(source); refs.close()
            for module in refs.modules:
                if embeds.get(embed_key(base, module)):
                    refs.urls.add(embeds[embed_key(base, module)])
            for url in refs.urls:
                add('video', url, reference, title)
            picture = pictures.get(kind, {}).get(str(cid if cid else eid), {})
            add('userpic', picture.get('url'), reference, 'Юзерпик')
    catalog = store.get('userpic_catalog', {}) or {}
    for url in [catalog.get('defaultpicurl'), *(catalog.get('pickwurls') or [])]:
        add('userpic', url, title='Юзерпик')
    for row in store.db.execute("SELECT payload FROM account_sections WHERE id='userpics'"):
        import json
        for pic in (json.loads(row[0]).get('data') or {}).get('pictures', []):
            add('userpic', pic.get('url'), title=pic.get('title') or 'Юзерпик')
    for row in rows.values():
        row['references'].sort(key=lambda r: (r['date'], r['entry_id'], r['comment_id'] or 0), reverse=True)
    return sorted(rows.values(), key=lambda row: (row['references'][0]['date'] if row['references'] else '', row['id']), reverse=True)


def write_gallery(store, report):
    from .render import ENV
    rows = inventory(store)
    atomic_write(store.folder / 'data/materials.json', dumps({'materials': rows}))
    groups = [('all', 'Все', rows), *[(kind, label, [r for r in rows if r['kind'] == kind])
              for kind, label in [('image', 'Изображения'), ('video', 'Видео'), ('userpic', 'Юзерпики')]]]
    def filename(kind, page=1):
        return 'materials' + ('' if kind == 'all' else '-' + kind) + (f'-{page}' if page > 1 else '') + '.html'
    filters = [{'label': label, 'kind': kind, 'count': len(items), 'href': filename(kind)} for kind, label, items in groups]
    produced = set()
    for kind, label, items in groups:
        pages = max(1, (len(items) + 47) // 48)
        for page in range(1, pages + 1):
            name = filename(kind, page); produced.add(name)
            atomic_write(store.folder / name, ENV.get_template('materials.html').render(
                user=report['username'], report=report, rows=items[(page-1)*48:page*48],
                filters=filters, kind=kind, page=page, pages=pages,
                previous=filename(kind, page-1) if page > 1 else None,
                following=filename(kind, page+1) if page < pages else None))
    import re
    for old in store.folder.glob('materials*.html'):
        if re.fullmatch(r'materials(?:-(?:image|video|userpic))?(?:-\d+)?\.html', old.name) and old.name not in produced:
            old.unlink()
