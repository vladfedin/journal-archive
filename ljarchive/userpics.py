"""Resolve the picture attached to each item; never substitute a comment author's current avatar."""
from urllib.parse import urlsplit


def picture_url(value):
    if not isinstance(value, str) or not value:
        return None
    if value.startswith('//'):
        value = 'https:' + value
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password or parts.port not in (None, 80, 443):
            return None
    except ValueError:
        return None
    return value


def resolve(item, kind, catalog=None, media=None, owner=None):
    props = item.get('props') if isinstance(item.get('props'), dict) else {}
    keyword = props.get('picture_keyword')
    result = {'url': None, 'path': None, 'keyword': keyword, 'selection': 'unknown'}
    # Extra=1 returns the userpic selected for this particular comment.
    for field in ('poster_userpic_url', 'userpic'):
        if field in item:
            value = item[field]
            present = bool(value)
            if isinstance(value, dict):
                value = value.get('url')
            url = picture_url(value)
            result.update(url=url, selection='selected' if url else 'unknown' if present else 'none')
            break
    else:
        if kind == 'comment':
            if not item.get('posterid') or item.get('state') in ('D', 'X'):
                result['selection'] = 'none'
        elif catalog is not None and item.get('poster', owner) == owner:
            keywords, urls = catalog.get('pickws') or [], catalog.get('pickwurls') or []
            if keyword:
                matches = [picture_url(url) for key, url in zip(keywords, urls) if key == keyword]
                if matches and matches[0]:
                    result.update(url=matches[0], selection='keyword')
            else:
                url = picture_url(catalog.get('defaultpicurl'))
                result.update(url=url, selection='default' if url else 'none')
    result['path'] = (media or {}).get(result['url']) if result['url'] else None
    return result


def describe(store, entries=None, comments=None, media=None):
    catalog, owner = store.get('userpic_catalog'), store.get('username')
    mapping = store.get('media', {}) if media is None else media
    return {kind: {str(item[key]): resolve(item, kind, catalog, mapping, owner) for item in items}
            for kind, key, items in (('post', 'itemid', store.entries() if entries is None else entries),
                                     ('comment', 'id', store.comments() if comments is None else comments))}


def coverage(pictures):
    items = [value for group in pictures.values() for value in group.values()]
    expected = [item for item in items if item['url']]
    unknown = sum(item['selection'] == 'unknown' for item in items)
    saved = sum(bool(item['path']) for item in expected)
    return {'total': len(expected), 'saved': saved, 'missing': len(expected) - saved, 'unknown': unknown,
            'complete': saved == len(expected) and not unknown}
