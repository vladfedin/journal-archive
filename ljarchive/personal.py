"""Read-only personal export. Source coverage is explicit; credentials never enter data."""
import json
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .client import APIError, HTTPError, IncompleteData
from .storage import dumps


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
        self.cycle = self.store.get('started_at')

    def section(self, key):
        row = self.store.db.execute('SELECT payload FROM account_sections WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, key, *, status, data=None, error=None, source=None):
        from .exporter import now
        old = self.section(key)
        record = {'status': status, 'checked_at': now(), 'captured_at': now() if data is not None else old.get('captured_at'), 'cycle': self.cycle, 'source': source or old.get('source'),
                  'data': data if data is not None else old.get('data'), 'error': error}
        with self.store.db:
            self.store._version('account', 'account_sections', key, record)
            self.store.db.execute('INSERT OR REPLACE INTO account_sections VALUES (?,?)', (key, dumps(record)))

    def capture(self, key, operation, source):
        previous = self.section(key)
        if previous.get('cycle') == self.cycle and previous.get('status') in ('captured', 'source_exhausted'):
            return
        self.exporter.update('personal', {'profile': 'Профиль', 'friends': 'Друзья и подписки', 'tags': 'Метки',
                                         'userpics': 'Юзерпики'}.get(key, 'Настройки'))
        try:
            data = operation()
            if not key.startswith('messages_'):
                self.save(key, status='captured', data=data, source=source)
        except HTTPError as exc:
            if exc.status not in (404, 410):
                raise
            self.save(key, status='unavailable', error=str(exc), source=source)
        except APIError as exc:
            if exc.code not in (100, 201, 203, 209, 300, 500):
                raise
            self.save(key, status='unavailable', error=str(exc), source=source)
        except IncompleteData as exc:
            self.save(key, status='partial', error=str(exc), source=source)

    def friends(self):
        result = self.client.call('getfriends', includegroups=1, includefriendof=1, includebdays=1)
        if not isinstance(result.get('friends'), list) or not isinstance(result.get('friendofs'), list):
            self.save('friends', status='partial', data=result, source='LJ.XMLRPC.getfriends')
            raise IncompleteData('ЖЖ не вернул полный список связей.')
        return result

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
        from .userpics import picture_url
        for entry in root.findall(ns + 'entry'):
            content = entry.find(ns + 'content')
            url = picture_url(content.get('src')) if content is not None else None
            if not url:
                self.save('userpics', status='partial', data={'document': source, 'pictures': pictures}, source='data/userpics')
                raise IncompleteData('В каталоге есть юзерпик без адреса.')
            pictures.append({'url': url, 'id': entry.findtext(ns + 'id'), 'title': entry.findtext(ns + 'title'),
                             'updated': entry.findtext(ns + 'updated'), 'summary': entry.findtext(ns + 'summary'),
                             'keywords': [c.get('term') for c in entry.findall(ns + 'category')]})
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
            self.capture('profile', lambda: getattr(self.client, 'profile', {}), 'LJ.XMLRPC.login (profile fields)')
            self.capture('foaf', self.foaf, 'data/foaf (FOAF/RDF)')
            self.capture('friends', self.friends, 'LJ.XMLRPC.getfriends')
            self.capture('tags', lambda: self.client.call('getusertags'), 'LJ.XMLRPC.getusertags')
            self.capture('userpics', self.userpics, 'data/userpics (Atom) + login')
            for category in ('display', 'privacy', 'notifications'):
                self.capture('settings_' + category, lambda c=category: self.preferences(c), 'manage/settings/?cat=' + category)
        if messages:
            for direction, event_type in (('incoming', 18), ('outgoing', 19)):
                self.capture('messages_' + direction, lambda d=direction, t=event_type: self.inbox(d, t), 'LJ.XMLRPC.getinbox')
        from .media import Media
        from .userpics import picture_url
        from .progress import Images
        media = getattr(self.exporter, '_media', None)
        if media is None:
            media = self.exporter._media = Media(self.store.folder, self.exporter.images, self.client.cancel, self.client.budget)
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


def render_personal(store, report):
    from .render import ENV, safe_content
    from .media import Media
    from .storage import atomic_write
    import threading
    data = account_data(store)
    sections, mapping = data['sections'], store.get('media', {})
    requested = store.get('personal_requested', {})
    relevant = {k: v for k, v in sections.items() if requested.get('messages' if k.startswith('messages_') else 'account')}
    report['personal'] = {'requested': requested, 'sections': {k: {'status': v['status'], 'error': v.get('error')} for k, v in sections.items()},
                          'messages_saved': len(data['messages'])}
    missing = [key for key, value in relevant.items() if value['status'] not in ('captured', 'source_exhausted')]
    if requested.get('account'):
        missing += [k for k in ('profile', 'foaf', 'friends', 'tags', 'userpics', 'settings_display', 'settings_privacy', 'settings_notifications') if k not in sections]
    if requested.get('messages'):
        missing += [k for k in ('messages_incoming', 'messages_outgoing') if k not in sections]
    catalog = data['userpic_catalog'] or {}
    urls = set(catalog.get('pickwurls') or [])
    urls.add(catalog.get('defaultpicurl'))
    urls.update(p['url'] for p in (sections.get('userpics', {}).get('data') or {}).get('pictures', []))
    urls.discard(None); urls.discard('')
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
    if not report['personal']['complete'] and report['status'] == 'complete':
        report['status'] = 'partial'
        report['issues'].append('Личные данные сохранены частично. Подробности — в разделе «Профиль».')
    profile = (sections.get('profile', {}).get('data') or {})
    friends = sections.get('friends', {}).get('data') or {}
    pictures = [{'url': u, 'path': mapping.get(u)} for u in sorted(urls)]
    labels = {'profile': 'Профиль', 'foaf': 'Интересы и сведения профиля', 'friends': 'Друзья и подписки', 'tags': 'Метки', 'userpics': 'Юзерпики',
              'settings_display': 'Отображение', 'settings_privacy': 'Приватность', 'settings_notifications': 'Уведомления',
              'messages_incoming': 'Входящие', 'messages_outgoing': 'Исходящие'}
    states = {'captured': 'Сохранено', 'source_exhausted': 'Выдача ЖЖ сохранена', 'partial': 'Есть пропуски', 'unavailable': 'Недоступно'}
    atomic_write(store.folder / 'account.html', ENV.get_template('account.html').render(user=report['username'], report=report,
                 sections=sections, profile=profile, friends=friends, pictures=pictures, labels=labels, states=states))
    media = Media(store.folder, False, threading.Event())
    media.mapping = mapping
    messages = sorted(data['messages'], key=lambda i: (i.get('when', 0), i.get('qid', 0)), reverse=True)
    pages = max(1, (len(messages) + 99) // 100)
    try:
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
