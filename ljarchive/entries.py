"""Protocol-backed entry classifications, independent of HTML presentation."""
from urllib.parse import urlsplit


def author(entry, default=None):
    return entry.get('poster') or entry.get('postername') or default


def author_id(entry):
    return entry.get('posterid') or entry.get('poster_id')


def is_repost(entry):
    return entry.get('repost') in (1, '1', True)


def repost_url(entry):
    if not is_repost(entry):
        return None
    for key in ('original_entry_url', 'url'):
        value = entry.get(key)
        if not isinstance(value, str):
            continue
        try:
            parts = urlsplit(value)
            if parts.scheme in ('http', 'https') and parts.hostname and not parts.username and not parts.password:
                return value
        except ValueError:
            pass
    return None
