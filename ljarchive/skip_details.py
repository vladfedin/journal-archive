"""Local report links for resources an export attempted to collect."""
from collections import defaultdict
from itertools import chain
from urllib.parse import urlsplit

from .comment_state import DELETED
from .media import error_message


def source_link(value):
    try:
        url = urlsplit(value)
        if url.scheme in ('http', 'https') and url.hostname and not (url.username or url.password):
            return value
    except ValueError:
        pass
    return None


def skipped_items(report, materials, entries, comments, *, count_attempts=None):
    from .render import original_url, plain
    from .gaps import ATTEMPTS
    from .client import ExportError
    from .sync import url_id
    posts = {int(e['itemid']): e for e in entries}
    by_comment = {int(c['id']): c for c in comments}
    locations = defaultdict(dict)
    for row in materials:
        for ref in row['references']:
            place = (ref['entry_id'], ref.get('comment_id'))
            locations[row['url']][place] = None

    def item(label, reason, source='', places=()):
        links = []
        for eid, cid in places:
            if eid not in posts or by_comment.get(cid, {}).get('state') in DELETED:
                continue
            title = plain(posts[eid].get('subject')) or f'Запись #{eid}'
            links.append({'href': f'entries/{eid}.html' + (f'#comment-{cid}' if cid else ''),
                          'label': f'{title} · комментарий #{cid}' if cid else title})
        return {'label': label, 'reason': reason, 'url': source_link(source), 'links': links}

    def evidence(row, record):
        from .browser_bridge import safe_url
        row['checked_at'] = record.get('checked_at') or record.get('updated_at')
        row['attempts'] = record.get('attempts')
        row['trace'] = [{'status': step.get('http_status'), 'transport': step.get('transport'),
                         'url': source_link(safe_url(step.get('url') or ''))}
                        for step in record.get('trace', [])[-8:]]
        return row

    rows = []
    for url, error in report.get('media_errors', {}).items():
        if error.get('availability') == 'unavailable' or error.get('reason') in ('not_requested', 'unknown'):
            continue
        row = item('Изображение', error.get('message') or error_message(error), url, locations[url])
        rows.append(evidence(row, error))

    for group in ('missing', 'unavailable'):
        for key, video in (report.get('videos') or {}).get(group, {}).items():
            if video.get('status') == 'not_requested' or report.get('videos_requested') is False:
                continue
            reason = (video.get('error') or {}).get('message') or 'Видео не загружено'
            places = locations[key] or [(eid, None) for eid in video.get('entries', [])]
            row = item('Видео', reason, video.get('url') or key, places)
            rows.append(evidence(row, video))

    for missing in report.get('missing_comments', []):
        comment = by_comment.get(missing['id'])
        if not comment or comment.get('state') in DELETED:
            continue
        eid, cid = comment['jitemid'], comment['id']
        url = original_url(report['username'], posts[eid]) if eid in posts else ''
        if url and comment.get('dtalkid'):
            url += '?thread=' + str(comment['dtalkid'])
        rows.append(item('Комментарий', 'ЖЖ не вернул текст', url, [(eid, cid)]))
    for eid in sorted(set(report.get('pending_comment_entry_ids', [])) | set(report.get('unavailable_comment_entry_ids', []))):
        if eid in posts:
            rows.append(item('Обсуждение', 'ЖЖ не подтвердил полноту обсуждения',
                             original_url(report['username'], posts[eid]), [(eid, None)]))
    requested = {}
    for url in ((report.get('sync') or {}).get('scope') or {}).get('urls', []):
        try:
            requested[url_id(report['username'], url) // 256] = url
        except ExportError:
            continue
    for eid in report.get('unavailable_entry_ids', []):
        post = posts.get(eid)
        rows.append(item(f'Запись #{eid}', 'ЖЖ не вернул запись',
                         requested.get(eid) or (original_url(report['username'], post) if post else ''), [(eid, None)]))
    for record in chain(entries, comments):
        cid = record.get('id') if 'jitemid' in record else None
        eid = record['jitemid'] if cid else record.get('itemid')
        authors = (record.get('reactions') or {}).get('authors') or {}
        if record.get('state') in DELETED or eid not in posts:
            continue
        url = original_url(report['username'], posts[eid])
        if cid and record.get('dtalkid'):
            url += '?thread=' + str(record['dtalkid'])
        key = f'comment:{cid}' if cid else f'post:{eid}'
        if (count_attempts or {}).get(key, 0) >= ATTEMPTS:
            reason = 'ЖЖ не вернул счётчик реакций'
            if 'reactions' in record:
                reason += ' · сохранено прежнее значение'
            rows.append(item('Реакции', reason, url, [(eid, cid)]))
        if authors.get('status') in ('partial', 'unavailable'):
            rows.append(item('Реакции', 'ЖЖ вернул неполный список авторов', url, [(eid, cid)]))
    return rows
