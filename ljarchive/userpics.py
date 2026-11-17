"""Resolve the picture attached to each item; never substitute a comment author's current avatar.

When LJ does not say which userpic the journal owner chose, the owner's default userpic is
shown and marked as assumed. Other authors' defaults are unknown without extra requests, so
their pages show a neutral placeholder. Neither case is a gap to download again.
"""
from urllib.parse import urlsplit
from .entries import author


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


def resolve(item, kind, catalog=None, media=None, owner=None, owner_id=None):
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
        elif catalog is not None and author(item, owner) == owner:
            keywords, urls = catalog.get('pickws') or [], catalog.get('pickwurls') or []
            if keyword:
                matches = [picture_url(url) for key, url in zip(keywords, urls) if key == keyword]
                if matches and matches[0]:
                    result.update(url=matches[0], selection='keyword')
            else:
                url = picture_url(catalog.get('defaultpicurl'))
                result.update(url=url, selection='default' if url else 'none')
    if result['selection'] == 'unknown' and catalog is not None:
        mine = (author(item, owner) == owner) if kind == 'post' else (
            owner_id is not None and str(item.get('posterid')) == str(owner_id))
        default = picture_url(catalog.get('defaultpicurl')) if mine else None
        if default:
            result.update(url=default, selection='assumed_default')
    result['path'] = (media or {}).get(result['url']) if result['url'] else None
    return result


def owner_id(store, catalog):
    # The keyword catalog belongs to the login account; it is the journal itself only for own journals.
    return store.get('journal_id') if catalog is not None else None


def describe(store, entries=None, comments=None, media=None):
    catalog, owner = store.get('userpic_catalog'), store.get('username')
    uid = owner_id(store, catalog)
    mapping = store.get('media', {}) if media is None else media
    return {kind: {str(item[key]): resolve(item, kind, catalog, mapping, owner, uid) for item in items}
            for kind, key, items in (('post', 'itemid', store.entries() if entries is None else entries),
                                     ('comment', 'id', store.comments() if comments is None else comments))}


def coverage(pictures, errors=None):
    from .gaps import closed
    items = [value for group in pictures.values() for value in group.values()]
    expected = [item for item in items if item['url']]
    unknown = sum(item['selection'] == 'unknown' for item in items)
    assumed = sum(item['selection'] == 'assumed_default' for item in items)
    saved = sum(bool(item['path']) for item in expected)
    unavailable = sum(not item['path'] and closed((errors or {}).get(item['url'])) for item in expected)
    missing = len(expected) - saved - unavailable
    return {'total': len(expected), 'saved': saved, 'missing': missing, 'unavailable': unavailable,
            'unknown': unknown, 'assumed': assumed, 'complete': not missing}
