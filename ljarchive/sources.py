"""Journal identity and observed access, independent of login credentials."""
from copy import deepcopy
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit


def source(account, journal=None, kind=None):
    from .client import ExportError, username
    if not isinstance(account, str) or (journal is not None and not isinstance(journal, str)):
        raise ExportError('Имя аккаунта и журнала должны быть строками.')
    account = username(account)
    journal = username(journal) if journal else account
    kind = kind or ('own' if account == journal else 'community')
    if kind not in ('own', 'community', 'reader') or (kind == 'own') != (account == journal):
        raise ExportError('Проверьте тип источника и имя журнала.')
    return {'type': kind, 'username': journal, 'id': None, 'login_username': account,
            'access': {'status': 'unverified', 'role': {'own': 'owner', 'community': 'manager', 'reader': 'reader'}[kind]},
            'capabilities': {key: 'unknown' for key in ('calendar', 'posts', 'comments', 'sync')}}


ARCHIVE_PATTERN = r"[a-z0-9_]{1,15}(?:-by-[a-z0-9_]{1,15})?(?:-latest-[1-9][0-9]?)?"


def journal_base(user):
    if user.startswith('_') or user.endswith('_'):
        return 'https://users.livejournal.com/' + user
    return f"https://{user.replace('_', '-')}.livejournal.com"


def archive_base(identity):
    if identity['type'] == 'reader':
        return f"{identity['username']}-by-{identity['login_username']}"
    return identity['username']


def stored_source(store):
    value = store.get('source')
    if value:
        return deepcopy(value)
    user = store.get('username')
    if not user:
        return None
    value = source(user)
    value['id'] = store.get('journal_id')
    return value


def bind_source(store, client):
    """One journal per database; IDs are only meaningful within that journal."""
    from .client import ExportError
    new = deepcopy(getattr(client, 'source', None) or source(client.user))
    new['id'] = getattr(client, 'journal_id', None)
    old = stored_source(store)
    if old and (old['username'] != new['username'] or old['type'] != new['type']):
        raise ExportError('Архив принадлежит другому журналу или типу источника.')
    if old and new['type'] == 'reader' and old.get('login_username') != new['login_username']:
        raise ExportError('У этого архива другой аккаунт читателя.')
    if old and 'is_community' in old:
        new.setdefault('is_community', old['is_community'])
    if old and old.get('id') and new.get('id') and old['id'] != new['id']:
        raise ExportError('ID журнала изменился. Создайте отдельный архив; прежние данные сохранены.')
    new['id'] = new.get('id') or (old or {}).get('id')
    store.set('source', new)
    store.set('username', new['username'])
    if new['id']:
        store.set('journal_id', new['id'])
    return new


class ManagedCommunities(HTMLParser):
    """Only administration links from the authenticated management page count.

    Posting and moderation alone do not confer management access. Neither the
    HTML nor account settings are retained in the archive.
    """
    def __init__(self):
        super().__init__()
        self.names = set()

    def handle_starttag(self, tag, attrs):
        from .client import ExportError, username
        if tag != 'a':
            return
        try:
            url = urlsplit(urljoin('https://www.livejournal.com/community/manage.bml', dict(attrs).get('href', '')))
            if url.scheme != 'https' or url.netloc != 'www.livejournal.com' or url.path != '/community/settings.bml':
                return
            names = parse_qs(url.query).get('authas', [])
            if len(names) == 1:
                self.names.add(username(names[0]))
        except (ValueError, TypeError, ExportError):
            return


def managed_communities(html):
    parser = ManagedCommunities()
    parser.feed(html.decode('utf-8', errors='replace'))
    return parser.names
