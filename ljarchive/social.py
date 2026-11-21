"""LiveJournal distinguishes adding a friend, subscribing, joining and managing."""
import json
import re
from urllib.parse import urlsplit

from .client import IncompleteData


SUBSCRIPTIONS = {'subscriptions': 'subscribers', 'subscribers': 'subscribersof'}
SOCIAL_LABELS = {'subscriptions': 'Мои подписки', 'subscribers': 'Мои подписчики',
                 'managed_communities': 'Управляю сообществами'}


def subscription_page(raw, user):
    text = raw.decode('utf-8', errors='replace')
    def site_object(name):
        match = re.search(r'\bSite\.' + name + r'\s*=\s*(\{)', text)
        return json.JSONDecoder().raw_decode(text[match.start(1):])[0] if match else {}
    try:
        if site_object('journal').get('username') != user:
            raise ValueError()
        groups = site_object('page')['subs']
        if not isinstance(groups, list):
            raise ValueError()
        tabs = {v['list_name']: v for v in groups if isinstance(v, dict) and v.get('list_name') in SUBSCRIPTIONS.values()}
    except (ValueError, KeyError) as exc:
        raise IncompleteData('ЖЖ не подтвердил списки подписок этого профиля.') from exc
    match = re.search(r'"auth_token"\s*:\s*("(?:[^"\\]|\\.)*")', text)
    return tabs, json.loads(match[1]) if match else None


def subscription_data(tab, name):
    if not isinstance(tab, dict) or tab.get('list_name') != name or not isinstance(tab.get('list'), list):
        raise IncompleteData('ЖЖ не вернул запрошенный список.')
    total = tab.get('count')
    if type(total) is not int or total < 0:
        raise IncompleteData('ЖЖ не сообщил размер списка.')
    users = {}
    for row in tab['list']:
        if isinstance(row, dict) and isinstance(row.get('lc_name'), str) and row['lc_name']:
            login = row['lc_name']
            users[login] = {'username': login, 'fullname': row.get('display_name', ''),
                            **{k: row[k] for k in ('profile_url', 'is_invisible') if k in row}}
    return {'users': list(users.values()), 'total': total}


def social_profile_url(person, login):
    url = person.get('profile_url')
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
            host = parts.hostname or ''
            if (parts.scheme == 'https' and parts.username is None and parts.password is None
                    and parts.port in (None, 443) and re.fullmatch(r'[a-z0-9_-]+\.livejournal\.com', host)
                    and parts.path.rstrip('/').endswith('/profile')):
                return url
        except ValueError:
            pass
    if re.fullmatch(r'[a-zA-Z0-9_-]{1,15}', login):
        if login.startswith('_') or login.endswith('_'):
            return f'https://users.livejournal.com/{login}/profile/'
        return f"https://{login.replace('_', '-')}.livejournal.com/profile/"
    return None


def social_lists(sections):
    """Construct the same directories and anchors for the profile and its search index."""
    section = sections.get('friends', {})
    graph = section.get('data') or {}
    groups = []

    def add(key, title, rows, *, family='Люди', retained=False, total=None):
        if rows is not None:
            users = []
            for row in rows:
                login = str(row.get('username') or row.get('user') or '')
                users.append({**row, 'username': login, 'href': social_profile_url(row, login)})
            rows = users
        groups.append({'id': key, 'label': title, 'family': family, 'users': rows,
                       'retained': retained, 'total': total})

    for key, title in [('friends', 'Мои друзья'), ('friendofs', 'Добавили меня в друзья')]:
        rows = graph.get(key)
        retained = rows is not None and (key in section.get('retained_fields', [])
                                         if 'retained_fields' in section else bool(section.get('error')))
        people = [p for p in rows if p.get('type') not in ('community', 'syndicated')] if rows is not None else None
        add(key, title, people, retained=retained)
        communities = [p for p in rows if p.get('type') == 'community'] if rows is not None else None
        if key == 'friends':
            add('watched-communities', 'Читаю сообщества', communities, family='Сообщества', retained=retained)
            feeds = [p for p in rows or [] if p.get('type') == 'syndicated']
            if feeds:
                add('feeds', 'Читаю RSS', feeds, family='Ленты', retained=retained)
        else:
            add('member-communities', 'Состою в сообществах', communities, family='Сообщества', retained=retained)
            feeds = [p for p in rows or [] if p.get('type') == 'syndicated']
            if feeds:
                add('incoming-feeds', 'Добавили в друзья · RSS', feeds, family='Ленты', retained=retained)
    for key, label in SOCIAL_LABELS.items():
        section = sections.get(key, {})
        data = section.get('data') or {}
        add(key, label, data.get('users'), total=data.get('total'),
            retained=bool(section.get('error') or section.get('retained_fields')),
            family='Сообщества' if key == 'managed_communities' else 'Люди')
    return groups
