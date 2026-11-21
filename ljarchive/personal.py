"""Read-only personal export. Source coverage is explicit; credentials never enter data."""
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from .client import APIError, HTTPError, AuthenticationRequired, HTTPRateLimited, APIRateLimited, IncompleteData, ExportError, Cancelled, RequestLimitReached
from .storage import dumps
from .social import SOCIAL_LABELS, SUBSCRIPTIONS, social_lists, social_profile_url, subscription_data


PROFILE_ISSUE = 'Личные данные сохранены частично. Подробности — в разделе «Профиль».'


class Preferences(HTMLParser):
    """Only preference controls, never passwords, hidden tokens or arbitrary text fields."""
    def __init__(self):
        super().__init__()
        self.controls, self.select, self.option = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        name = a.get('name', '')
        # LiveJournal Setting classes use named LJ__Setting__ controls.
        allowed = name.startswith('LJ__Setting__') and not any(x in name.lower() for x in ('password', 'token', 'secret', 'auth'))
        if tag == 'input' and allowed and a.get('type', '').lower() in ('checkbox', 'radio'):
            self.controls.append({'name': name, 'type': a['type'].lower(), 'value': a.get('value', 'on'), 'checked': 'checked' in a})
        elif tag == 'select':
            self.select = {'name': name, 'type': 'select', 'multiple': 'multiple' in a, 'options': []} if allowed else None
        elif tag == 'option' and self.select is not None:
            self.option = {'value': a.get('value'), 'selected': 'selected' in a, 'label': ''}
            self.select['options'].append(self.option)

    def handle_data(self, data):
        if self.option is not None:
            self.option['label'] += data

    def handle_endtag(self, tag):
        if tag == 'option':
            self.option = None
        elif tag == 'select' and self.select is not None:
            options = self.select['options']
            for o in options:
                o['label'] = o['label'].strip()
                if o['value'] is None:
                    o['value'] = o['label']
            if options and not self.select['multiple'] and not any(o['selected'] for o in options):
                options[0]['selected'] = True
            self.controls.append(self.select)
            self.select, self.option = None, None


def account_data(store):
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {'sections': {r[0]: json.loads(r[1]) for r in store.db.execute('SELECT id,payload FROM account_sections ORDER BY id')} if 'account_sections' in names else {},
            'messages': [json.loads(r[0]) for r in store.db.execute('SELECT payload FROM messages ORDER BY id')] if 'messages' in names else [],
            'userpic_catalog': store.get('userpic_catalog', {})}


class Personal:
    def __init__(self, exporter):
        self.exporter, self.store, self.client = exporter, exporter.store, exporter.client
        self.subscription_tabs = None
        self.cycle = getattr(exporter, 'personal_cycle', None) or self.store.get('started_at')

    def section(self, key):
        row = self.store.db.execute('SELECT payload FROM account_sections WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, key, *, status, data=None, error=None, source=None, retained_fields=None, latest_response=None):
        from .exporter import now
        old = self.section(key)
        record = {'status': status, 'checked_at': now(), 'captured_at': now() if data is not None else old.get('captured_at'), 'cycle': self.cycle, 'source': source or old.get('source'),
                  'data': data if data is not None else old.get('data'), 'error': error}
        for name, value in (('retained_fields', retained_fields), ('latest_response', latest_response)):
            if value is not None:
                record[name] = value
            elif data is None and name in old:
                record[name] = old[name]
        if key == 'friends' and data is None and old.get('cycle') != self.cycle and old.get('data'):
            record['retained_fields'] = list(old['data'])
            record.pop('latest_response', None)
        with self.store.db:
            self.store._version('account', 'account_sections', key, record)
            self.store.db.execute('INSERT OR REPLACE INTO account_sections VALUES (?,?)', (key, dumps(record)))

    def capture(self, key, operation, source):
        previous = self.section(key)
        if previous.get('cycle') == self.cycle and previous.get('status') in ('captured', 'source_exhausted'):
            return
        self.exporter.update('personal', {**SOCIAL_LABELS, 'profile': 'Профиль', 'friends': 'Друзья и подписки', 'tags': 'Метки',
                                         'userpics': 'Юзерпики'}.get(key, 'Настройки'))
        try:
            data = operation()
            if not key.startswith('messages_'):
                self.save(key, status='captured', data=data, source=source)
        except HTTPError as exc:
            self.save(key, status='unavailable' if exc.status in (404, 410) else 'partial', error=str(exc), source=source)
        except APIError as exc:
            if exc.code not in (100, 201, 203, 209, 300, 500):
                raise
            self.save(key, status='unavailable', error=str(exc), source=source)
        except IncompleteData as exc:
            self.save(key, status='partial', error=str(exc), source=source)
        except (HTTPRateLimited, APIRateLimited) as exc:
            self.save(key, status='partial', error=str(exc), source=source)
            raise
        except (Cancelled, RequestLimitReached):
            raise
        except AuthenticationRequired as exc:
            reason = f'HTTP {exc.status} · {exc}' if exc.status else str(exc)
            self.save(key, status='partial', error=reason, source=source)
        except ExportError as exc:
            self.save(key, status='partial', error=str(exc), source=source)

    def friends(self):
        result = self.client.call('getfriends', includegroups=1, includefriendof=1, includebdays=1)
        if not isinstance(result.get('friends'), list) or not isinstance(result.get('friendofs'), list):
            previous = self.section('friends').get('data') or {}
            missing = {key for key in ('friends', 'friendofs', 'friendgroups') if not isinstance(result.get(key), list)}
            fresh = {key: value for key, value in result.items() if key not in missing}
            retained = sorted(set(previous) - set(fresh))
            self.save('friends', status='partial', data={**previous, **fresh}, source='LJ.XMLRPC.getfriends',
                      retained_fields=retained, latest_response=result)
            raise IncompleteData('ЖЖ не вернул полный список связей.')
        return result

    def save_subscription_page(self):
        self.subscription_tabs = self.client.subscription_preview()
        tabs, _ = self.subscription_tabs
        # Both directions arrive in one response. Checkpoint both before requesting more.
        for key, name in SUBSCRIPTIONS.items():
            previous = self.section(key)
            if previous.get('cycle') == self.cycle and previous.get('status') == 'captured':
                continue
            try:
                data = subscription_data(tabs.get(name), name)
            except IncompleteData:
                continue
            if len(data['users']) == data['total']:
                self.save(key, status='captured', data=data, source='profile/subslist/')
            else:
                self.save_partial_subscriptions(key, data)

    def save_partial_subscriptions(self, key, data):
        previous = self.section(key).get('data') or {}
        known = {p['username']: p for p in previous.get('users', [])}
        known.update({p['username']: p for p in data['users']})
        self.save(key, status='partial', data={**data, 'users': list(known.values())},
                  source='profile/subslist/ · profile.get_friends',
                  retained_fields=['users'] if len(known) > len(data['users']) else [],
                  error='Получена часть списка подписок.')

    def subscriptions(self, key):
        if self.subscription_tabs is None:
            self.save_subscription_page()
        tabs, token = self.subscription_tabs
        name = SUBSCRIPTIONS[key]
        data = subscription_data(tabs.get(name), name)
        if len(data['users']) == data['total']:
            return data
        full = self.client.full_subscription_list(name, token)
        if len(full['users']) != full['total']:
            self.save_partial_subscriptions(key, full)
            raise IncompleteData('ЖЖ вернул только часть списка подписок.')
        return full

    def preferences(self, category):
        parser = Preferences()
        parser.feed(self.client.personal_page('/manage/settings/?cat=' + category).decode('utf-8', 'replace'))
        parser.close()
        if not parser.controls:
            raise IncompleteData('Настройки не найдены в ответе ЖЖ.')
        return {'category': category, 'controls': parser.controls,
                'coverage': 'Checkbox, radio and select controls exposed by this settings page.'}

    def xml_feed(self, name):
        from defusedxml import ElementTree
        from defusedxml.common import DefusedXmlException
        try:
            raw = self.client.personal_feed(name)
            root = ElementTree.fromstring(raw)
            source = raw.decode('utf-8')
            return root, source
        except (ElementTree.ParseError, DefusedXmlException, UnicodeError):
            raise IncompleteData('ЖЖ не вернул корректный XML профиля.') from None

    def foaf(self):
        root, source = self.xml_feed('foaf')
        ns = {'foaf': 'http://xmlns.com/foaf/0.1/'}
        person = root.find('foaf:Person', ns)
        if person is None or person.findtext('foaf:nick', namespaces=ns) != self.client.user:
            raise IncompleteData('Профиль FOAF не подтверждает владельца.')
        return {'format': 'application/rdf+xml', 'document': source,
                'fields': [{'name': child.tag, 'value': child.text or '', 'attributes': dict(child.attrib)}
                           for child in person if not len(child)],
                'coverage': 'Profile fields exposed by the FOAF feed. Friends may be truncated; use getfriends for the graph.'}

    def userpics(self):
        ns = '{http://www.w3.org/2005/Atom}'
        root, source = self.xml_feed('userpics')
        if root.tag != ns + 'feed' or root.findtext(ns + 'id') != f'urn:lj:livejournal.com:atom1:{self.client.user}:userpics':
            raise IncompleteData('Каталог юзерпиков не подтверждает владельца.')
        pictures = []
        incomplete = False
        from .userpics import picture_url
        for entry in root.findall(ns + 'entry'):
            content = entry.find(ns + 'content')
            url = picture_url(content.get('src')) if content is not None else None
            if not url:
                incomplete = True
                continue
            pictures.append({'url': url, 'id': entry.findtext(ns + 'id'), 'title': entry.findtext(ns + 'title'),
                             'updated': entry.findtext(ns + 'updated'), 'summary': entry.findtext(ns + 'summary'),
                             'keywords': [c.get('term') for c in entry.findall(ns + 'category')]})
        if incomplete:
            previous = self.section('userpics').get('data') or {}
            merged = {pic['url']: pic for pic in previous.get('pictures', [])}
            merged.update({pic['url']: pic for pic in pictures})
            response = {'document': source, 'pictures': pictures}
            self.save('userpics', status='partial', data={**previous, **response, 'pictures': list(merged.values())},
                      source='data/userpics', latest_response=response, retained_fields=['pictures'])
            raise IncompleteData('В каталоге есть юзерпик без адреса; ранее сохранённые изображения оставлены.')
        return {'catalog': self.store.get('userpic_catalog', {}), 'pictures': pictures, 'document': source, 'format': 'application/atom+xml'}

    def save_message(self, item):
        row = self.store.db.execute('SELECT payload FROM messages WHERE id=?', (item['qid'],)).fetchone()
        record = dict(item)
        if item.get('body') is None:
            old = json.loads(row[0]) if row else {}
            if 'body' in old:
                record['body'] = old['body']
            record['source_status'] = 'body_not_returned'
        self.store._version('message', 'messages', item['qid'], record)
        self.store.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?)', (item['qid'], dumps(record)))

    def finish_inbox(self, key, event_type):
        missing = self.store.db.execute("""SELECT DISTINCT json_extract(payload,'$.when') FROM messages
            WHERE json_extract(payload,'$.type')=? AND (json_type(payload,'$.body') IS NULL
            OR json_extract(payload,'$.source_status')='body_not_returned')""", (event_type,)).fetchall()
        # Retry only the timestamp windows containing missing bodies. Skip is bounded by LJ.
        for row in missing:
            for skip in (0, 100):
                self.client.check()
                result = self.client.call('getinbox', gettype=[event_type], extended=1, itemshow=100,
                                          before=row[0], lastsync=row[0], skip=skip)
                items = result.get('items')
                if not isinstance(items, list):
                    break
                with self.store.db:
                    for item in items:
                        if (isinstance(item, dict) and type(item.get('qid')) is int
                                and item.get('when') == row[0] and item.get('type') == event_type):
                            self.save_message(item)
                if len(items) < 100:
                    break
        missing_count = self.store.db.execute("""SELECT COUNT(*) FROM messages WHERE json_extract(payload,'$.type')=?
            AND (json_type(payload,'$.body') IS NULL OR json_extract(payload,'$.source_status')='body_not_returned')""", (event_type,)).fetchone()[0]
        self.save(key, status='partial' if missing_count else 'source_exhausted',
                  error=f'Не получены тексты сообщений: {missing_count}.' if missing_count else None)

    def inbox(self, direction, event_type):
        key = 'messages_' + direction
        previous = self.section(key)
        if previous.get('cycle') == self.cycle and previous.get('status') == 'source_exhausted':
            return
        checkpoint_key = 'inbox_cursor_' + direction
        cursor = self.store.get(checkpoint_key, {})
        if cursor.get('cycle') != self.cycle:
            cursor = {'cycle': self.cycle, 'before': int(time.time()), 'skip': 0, 'edge_ids': []}
        self.save(key, status='partial', source='LJ.XMLRPC.getinbox', data={'coverage': 'Messages retained in the server notification inbox; historical completeness is not guaranteed.'})
        while True:
            self.client.check()
            self.exporter.update('personal', 'Переписка · ' + ('входящие' if event_type == 18 else 'исходящие'))
            result = self.client.call('getinbox', gettype=[event_type], itemshow=100, extended=1,
                                      before=cursor['before'], skip=cursor['skip'])
            items = result.get('items')
            if not isinstance(items, list) or any(not isinstance(i, dict) or type(i.get('qid')) is not int
                    or type(i.get('when')) is not int or i['when'] <= 0 or i['when'] > cursor['before']
                    or i.get('type') != event_type for i in (items or [])):
                raise IncompleteData('ЖЖ вернул некорректную страницу переписки.')
            with self.store.db:
                for item in items:
                    self.save_message(item)
            if not items:
                self.finish_inbox(key, event_type)
                return
            with self.store.db:
                oldest = min(i['when'] for i in items)
                edge = set(cursor['edge_ids']) if oldest == cursor['before'] else set()
                fresh = {i['qid'] for i in items if i['when'] == oldest}
                next_edge = sorted(edge | fresh)
                next_cursor = {'cycle': self.cycle, 'before': oldest, 'skip': min(100, len(next_edge)), 'edge_ids': next_edge}
                stalled = next_cursor == cursor or (oldest == cursor['before'] and not (fresh - edge))
                if not stalled:
                    self.store.set(checkpoint_key, next_cursor)
            if len(items) < 100:
                self.finish_inbox(key, event_type)
                return
            if stalled:
                raise IncompleteData('Переписка сохранена до границы выдачи ЖЖ; сервер повторяет страницу. Полнота не подтверждена.')
            cursor = next_cursor

    def run(self, account, messages):
        if account:
            with self.store.db:
                self.store.set('profile_social_requested', True)
            self.capture('profile', lambda: getattr(self.client, 'profile', {}), 'LJ.XMLRPC.login (profile fields)')
            self.capture('foaf', self.foaf, 'data/foaf (FOAF/RDF)')
            self.capture('friends', self.friends, 'LJ.XMLRPC.getfriends')
            self.capture('tags', lambda: self.client.call('getusertags'), 'LJ.XMLRPC.getusertags')
            self.capture('userpics', self.userpics, 'data/userpics (Atom) + login')
            for category in ('display', 'privacy', 'notifications'):
                self.capture('settings_' + category, lambda c=category: self.preferences(c), 'manage/settings/?cat=' + category)
            for key in SUBSCRIPTIONS:
                self.capture(key, lambda key=key: self.subscriptions(key), 'profile/subslist/ · profile.get_friends')
            self.capture('managed_communities',
                         lambda: {'users': [{'username': name} for name in self.client.managed_communities(confirm_empty=True)]},
                         'community/manage.bml')
        if messages:
            for direction, event_type in (('incoming', 18), ('outgoing', 19)):
                self.capture('messages_' + direction, lambda d=direction, t=event_type: self.inbox(d, t), 'LJ.XMLRPC.getinbox')
        from .media import Media
        from .userpics import picture_url
        from .progress import Images
        media = getattr(self.exporter, '_media', None)
        if media is None:
            media = self.exporter._media = Media(self.store.folder, self.exporter.images, self.client.cancel, self.client.budget, client=self.client)
            media.load_state(self.store)
        urls, embeds = set(), set()
        if account:
            urls.update(self.store.get('userpic_catalog', {}).get('pickwurls') or [])
            urls.add(self.store.get('userpic_catalog', {}).get('defaultpicurl'))
            urls.update(p['url'] for p in (self.section('userpics').get('data') or {}).get('pictures', []))
        if messages:
            for row in self.store.db.execute('SELECT payload FROM messages'):
                item = json.loads(row[0])
                urls.add(picture_url(item.get('picture')))
                if self.exporter.images:
                    parser = Images('https://www.livejournal.com/')
                    parser.feed(item.get('body') if isinstance(item.get('body'), str) else '')
                    parser.close()
                    embeds.update(parser.urls)
        for url, category in ([(u, 'userpics') for u in sorted(set(filter(None, (picture_url(u) for u in urls))))]
                              + [(u, 'images') for u in sorted(embeds)]):
            self.client.check()
            self.exporter.update('personal', 'Материалы профиля и переписки')
            media.get(url, required=category == 'userpics', category=category)
            with self.store.db:
                media.save_state(self.store)


def profile_fields(sections):
    labels = {'nick': 'Имя пользователя', 'name': 'Имя', 'birthday': 'День рождения',
              'dateOfBirth': 'Дата рождения', 'interest': 'Интересы', 'based_near': 'Местоположение',
              'homepage': 'Сайт', 'schoolHomepage': 'Учёба', 'workplaceHomepage': 'Работа',
              'icqChatID': 'ICQ', 'jabberID': 'Jabber', 'mbox': 'Почта'}
    fields = []
    for field in (sections.get('foaf', {}).get('data') or {}).get('fields', []):
        key = field['name'].split('}')[-1]
        value = field.get('value') or field.get('attributes', {}).get('{http://purl.org/dc/elements/1.1/}title') or field.get('attributes', {}).get('{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource')
        if value:
            fields.append({'label': labels.get(key, key), 'value': value})
    return fields


def profile_overview(sections):
    from .userpics import picture_url
    info = {'facts': [], 'interests': []}
    labels = {'birthday': 'День рождения', 'dateOfBirth': 'Дата рождения', 'country': 'Страна',
              'city': 'Город', 'based_near': 'Местоположение', 'homepage': 'Сайт',
              'school': 'Учёба', 'schoolHomepage': 'Учёба', 'workplaceHomepage': 'Работа',
              'icqChatID': 'ICQ', 'jabberID': 'Jabber', 'mbox': 'Почта'}
    for field in (sections.get('foaf', {}).get('data') or {}).get('fields', []):
        key = field['name'].split('}')[-1]
        attrs = {k.split('}')[-1]: v for k, v in field.get('attributes', {}).items()}
        resource = attrs.get('resource', '')
        url = picture_url(resource) or picture_url(unquote(resource))
        if key == 'mbox' and resource.lower().startswith('mailto:'):
            url = resource
        value = (field.get('value') or attrs.get('title') or url or '').strip()
        if key == 'mbox' and value.lower().startswith('mailto:'):
            url = value
            value = value[7:]
        if not value:
            continue
        if key in ('name', 'journaltitle', 'journalsubtitle', 'bio'):
            info[key] = value
        elif key == 'interest':
            info['interests'].append({'value': value, 'url': url})
        elif key in labels:
            info['facts'].append({'label': labels[key], 'value': unquote(value) if key == 'city' else value,
                                  'url': url if key in ('homepage', 'school', 'schoolHomepage', 'workplaceHomepage', 'mbox') else None})
    return info


def profile_pictures(catalog, section, mapping, folder):
    """Join the Atom descriptions to the login keyword catalog, including older exports."""
    from .userpics import picture_url
    source = section.get('data') or {}
    catalog = {**(source.get('catalog') or {}), **catalog}
    default = picture_url(catalog.get('defaultpicurl'))
    pictures = {}

    def add(url, names=(), title=None):
        url = picture_url(url)
        if not url:
            return
        if url not in pictures:
            path = mapping.get(url)
            if path and not ((folder / path).is_file() and (folder / path).resolve().is_relative_to(folder.resolve())):
                path = None
            pictures[url] = {'url': url, 'path': path, 'keywords': [], 'default': url == default, 'title': None}
        pic = pictures[url]
        for name in names:
            if name and name not in pic['keywords']:
                pic['keywords'].append(name)
        if title:
            pic['title'] = title

    add(default)
    keywords = catalog.get('pickws') or []
    for i, url in enumerate(catalog.get('pickwurls') or []):
        add(url, keywords[i:i + 1])
    for entry in source.get('pictures', []):
        add(entry.get('url'), entry.get('keywords') or [], entry.get('title'))
    return list(pictures.values())


def preference_rows(sections):
    import re
    labels = {'TimeZone_timezone': 'Часовой пояс', 'Privacy_comments': 'Кто может комментировать',
              'CommentScreening_comments': 'Скрытие комментариев', 'AdultContent_content': 'Возрастная категория'}
    rows = {}
    for key, section in sections.items():
        if not key.startswith('settings_'):
            continue
        group = []
        for control in (section.get('data') or {}).get('controls', []):
            name = control['name'].removeprefix('LJ__Setting__')
            if control['type'] == 'select':
                value = ', '.join(str(o['label']) for o in control['options'] if o.get('selected')) or '—'
            elif control['type'] == 'radio':
                if not control.get('checked'):
                    continue
                value = str(control.get('value', ''))
            else:
                value = 'Да' if control.get('checked') else 'Нет'
            group.append({'label': labels.get(name, re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name).replace('_', ' · ')), 'value': value})
        rows[key] = group
    return rows


def download_profile(store, data, cancel, update, sessions=None):
    from types import SimpleNamespace
    import secrets
    from .client import LiveJournalClient, RequestBudget
    from .on_demand import NeedsLogin
    from .sources import stored_source, bind_source
    from .retry import restored_deadline
    identity = stored_source(store)
    if identity['type'] != 'own':
        raise ExportError('Личный профиль доступен только в архиве своего журнала.')
    if not data.get('password'):
        raise NeedsLogin('Введите пароль ЖЖ для загрузки профиля.')
    budget = RequestBudget(data.get('max_requests', 50), progress=update)
    client = LiveJournalClient(identity['login_username'], data['password'], budget=budget,
                               delay=data.get('delay', 3), cancel=cancel, progress=update,
                               session_ready=sessions.remember if sessions else None)
    client.not_before = budget.not_before = restored_deadline(store)
    def checkpoint(deadline):
        with store.db:
            store.set('request_not_before', deadline)
            store.set('request_wait', budget.wait_info)
    client.save_cooldown = budget.save_cooldown = checkpoint
    exporter = SimpleNamespace(store=store, client=client, images=False,
        personal_cycle=secrets.token_hex(12),
        update=lambda phase, message, **values: update(phase=phase, message=message, **values))
    try:
        client.login()
        with store.db:
            bind_source(store, client)
            store.set('userpic_catalog', client.userpics)
            store.set('personal_requested', {**store.get('personal_requested', {}), 'account': True})
        Personal(exporter).run(True, False)
        sections = account_data(store)['sections']
        complete = all(s['status'] in ('captured', 'source_exhausted') for key, s in sections.items() if not key.startswith('messages_'))
        return {'kind': 'profile', 'complete': complete}
    finally:
        if getattr(exporter, '_media', None): exporter._media.close()
        client.close()
        data.pop('password', None)


def refresh_profile(store, *, interruption=None):
    from .gallery import write_gallery
    from .render import ARCHIVE_FORMAT_VERSION, ENV, build_archive
    from .data import export_json
    from .integrity import write_manifest
    from .storage import atomic_write
    path = store.folder / 'export-report.json'
    report = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {
        'username': store.get('username'), 'status': 'partial', 'entries_saved': store.counts()['entries'],
        'comments_saved': store.counts()['comments'], 'missing_comments': []}
    if (store.folder / 'index.html').is_file() and (report.get('format_version') != ARCHIVE_FORMAT_VERSION
            or not (store.folder / 'assets/feed-data.js').is_file() or not (store.folder / 'search.html').is_file()):
        # Upgrade old templates and their JS/data together. This renders only local data.
        build_archive(store, report)
    previous_stop = (report.get('personal') or {}).get('interruption') or {}
    if previous_stop.get('only_issue') and report.get('status') == 'partial' and not report.get('issues'):
        report['status'] = 'complete'
    was_finished = previous_stop.get('was_finished') or report.get('status') == 'complete' or report.get('finished_with_skips', False)
    previous_profile_only = (report.get('personal') or {}).get('complete_baseline')
    render_personal(store, report)
    if previous_profile_only and any(issue != PROFILE_ISSUE for issue in report.get('issues', [])):
        was_finished = False
    report['finished_with_skips'] = bool(report['status'] == 'partial' and was_finished
        and not report.get('request_limit_reached') and (not report['personal']['complete'] or report.get('issues')))
    if interruption and report['status'] in ('partial', 'complete'):
        report['personal']['interruption'] = {'message': str(interruption), 'was_finished': bool(was_finished),
                                              'only_issue': report['status'] == 'complete'}
        report.update(status='partial', finished_with_skips=False)
    from .fulltext import material_docs, personal_docs, write_part
    materials = write_gallery(store, report)
    write_part(store.folder, 'personal', personal_docs(report['username'], account_data(store)))
    entries, comments = store.entries(), store.comments()
    write_part(store.folder, 'materials', material_docs(materials, entries, comments, report['username']))
    from .skip_details import skipped_items
    report['skipped_items'] = skipped_items(report, materials, entries, comments,
                                            count_attempts=store.get('reaction_count_attempts', {}))
    if (store.folder / 'index.html').is_file():
        # Reuse the feed already published, including retained and orphaned entries.
        try:
            feed = (store.folder / 'assets/feed-data.js').read_text(encoding='utf-8')
            rows = json.loads(feed.removeprefix('window.ARCHIVE=').removesuffix(';'))['entries']
        except (OSError, ValueError, KeyError):
            from .live import summaries
            _, rows = summaries(store)
        atomic_write(store.folder / 'index.html', ENV.get_template('index.html').render(user=report['username'], entries=rows, report=report))
    export_json(store, split=True)
    atomic_write(path, dumps(report))
    atomic_write(store.folder / 'report.html', ENV.get_template('report.html').render(user=report['username'], report=report))
    write_manifest(store.folder)
    return report


def render_personal(store, report):
    from .render import ENV, safe_content
    from .media import Media
    from .storage import atomic_write
    import threading
    issue = PROFILE_ISSUE
    previous_personal = report.get('personal', {})
    previous_issue = issue in report.get('issues', [])
    report['issues'] = [text for text in report.get('issues', []) if text != issue]
    if (previous_issue and report.get('status') == 'partial' and not report['issues']
            and previous_personal.get('complete_baseline') and not report.get('request_limit_reached')):
        report['status'] = 'complete'
    data = account_data(store)
    sections, mapping = data['sections'], store.get('media', {})
    requested = store.get('personal_requested', {})
    relevant = {k: v for k, v in sections.items() if requested.get('messages' if k.startswith('messages_') else 'account')}
    report['personal'] = {'requested': requested, 'sections': {k: {'status': v['status'], 'error': v.get('error')} for k, v in sections.items()},
                          'messages_saved': len(data['messages'])}
    missing = [key for key, value in relevant.items() if value['status'] not in ('captured', 'source_exhausted')]
    if requested.get('account'):
        missing += [k for k in ('profile', 'foaf', 'friends', 'tags', 'userpics', 'settings_display', 'settings_privacy', 'settings_notifications') if k not in sections]
        if store.get('profile_social_requested'):
            missing.extend(key for key in SOCIAL_LABELS if key not in sections)
    if requested.get('messages'):
        missing += [k for k in ('messages_incoming', 'messages_outgoing') if k not in sections]
    catalog = data['userpic_catalog'] or {}
    pictures = profile_pictures(catalog, sections.get('userpics', {}), mapping, store.folder)
    mapping = {**mapping, **{pic['url']: pic['path'] for pic in pictures}}
    urls = {pic['url'] for pic in pictures}
    report['personal']['userpics'] = {'total': len(urls), 'saved': sum(bool(mapping.get(u)) for u in urls)}
    from .userpics import picture_url
    required_urls = set(urls) if requested.get('account') else set()
    if requested.get('messages'):
        from .progress import Images
        for message in data['messages']:
            url = picture_url(message.get('picture'))
            if url:
                required_urls.add(url)
            if report.get('images_requested'):
                parser = Images('https://www.livejournal.com/')
                parser.feed(message.get('body') if isinstance(message.get('body'), str) else '')
                required_urls.update(parser.urls)
    report['personal']['media'] = {'total': len(required_urls), 'saved': sum(bool(mapping.get(u)) for u in required_urls)}
    report['personal']['complete'] = not missing and all(mapping.get(u) for u in required_urls)
    if not report['personal']['complete']:
        if report['status'] == 'complete':
            report['status'] = 'partial'
            report['personal']['complete_baseline'] = True
        report['issues'].append(issue)
    elif (report.get('finished_with_skips') and report['status'] == 'partial'
            and not report['issues'] and not report.get('request_limit_reached')
            and report.get('media_complete', True)
            and not any(report.get(key) for key in ('pending_days', 'pending_comment_entry_ids',
                                                    'orphan_entry_ids', 'unknown_encoding_entry_ids'))):
        # A legacy profile refresh could remove its last issue but leave a partial result.
        report['status'] = 'complete'
    profile = (sections.get('profile', {}).get('data') or {})
    friends = sections.get('friends', {}).get('data') or {}
    overview = profile_overview(sections)
    labels = {'profile': 'Профиль', 'foaf': 'Интересы и сведения профиля', 'friends': 'Друзья и подписки', 'tags': 'Метки', 'userpics': 'Юзерпики',
              'settings_display': 'Отображение', 'settings_privacy': 'Приватность', 'settings_notifications': 'Уведомления',
              'messages_incoming': 'Входящие', 'messages_outgoing': 'Исходящие'}
    labels.update(SOCIAL_LABELS)
    states = {'captured': 'Сохранено', 'source_exhausted': 'Выдача ЖЖ сохранена', 'partial': 'Есть пропуски', 'unavailable': 'Недоступно'}
    media = Media(store.folder, False, threading.Event())
    media.mapping = mapping
    messages = sorted(data['messages'], key=lambda i: (i.get('when', 0), i.get('qid', 0)), reverse=True)
    pages = max(1, (len(messages) + 99) // 100)
    try:
        from .fulltext import visible
        bio = overview.get('bio', '')
        journal = ENV.filters['profile_url'](report['username'])
        bio_html = str(safe_content(bio, journal, {}, media, set())).replace('src="../media/', 'src="media/')
        lines = re.split(r'<br\s*/?>|</(?:p|div|li|h[1-6]|tr|blockquote|pre)>|\r?\n', bio_html, flags=re.I)
        preview_lines = [text for line in lines if (text := visible(line))]
        excerpt = ''
        if len(visible(bio_html)) > 520 or len(lines) > 9:
            excerpt = '\n'.join(preview_lines[:3])
            if len(excerpt) > 180:
                excerpt = excerpt[:180].rsplit(' ', 1)[0]
        atomic_write(store.folder / 'account.html', ENV.get_template('account.html').render(
            user=report['username'], report=report, sections=sections, profile=profile,
            original_profile=social_profile_url({}, report['username']),
            friends=friends, social=social_lists(sections), pictures=pictures, labels=labels, states=states, overview=overview,
            bio_html=bio_html, bio_excerpt=excerpt, missing_sections=missing,
            has_profile=any(not key.startswith('messages_') and section.get('data') is not None
                            for key, section in sections.items()),
            preferences=preference_rows(sections)))
        for page in range(pages):
            batch = []
            from datetime import datetime, timezone
            for item in messages[page * 100:(page + 1) * 100]:
                rendered = dict(item)
                rendered['date'] = datetime.fromtimestamp(item['when'], timezone.utc).isoformat()
                rendered['picture_path'] = mapping.get(item.get('picture')) if isinstance(item.get('picture'), str) else None
                rendered['html'] = str(safe_content(item.get('body', ''), 'https://www.livejournal.com/', {}, media, set())).replace('src="../media/', 'src="media/')
                batch.append(rendered)
            atomic_write(store.folder / ('messages.html' if page == 0 else f'messages-{page + 1}.html'),
                         ENV.get_template('messages.html').render(user=report['username'], report=report, messages=batch, page=page + 1, pages=pages))
    finally:
        media.close()
