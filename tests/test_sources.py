import json
import xmlrpc.client

import pytest
import requests

from ljarchive.activitystreams import source_snapshot, to_activitystreams
from ljarchive.client import ExportError, LiveJournalClient, RequestBudget
from ljarchive.data import snapshot
from ljarchive.exporter import Exporter
from ljarchive.sources import bind_source, managed_communities, source, stored_source
from ljarchive.storage import Store
from ljarchive.sync import Scope, SyncExporter
from test_limited_export import Response
from test_render_server import local_server


def community_home(journal='bookclub', account='manager', *, icon=True):
    url = f'https://{journal}.livejournal.com/263.html'
    first = {'itemid': 263, 'journalid': 900, 'url': url}
    image = '<img src="https://l-stat.livejournal.net/img/icon_sticky.png?v=1">'
    return f"""<div id="asset-{journal}-263" class="post-asset"><div class="asset-header">
        <h2 class="asset-name"><span class="lj-entry-securityicon">{image if icon else ''}</span>
        <a href="{url}" class="subj-link">First post</a></h2></div>
        <div class="asset-body">{image}<b data-widget-options='{{"sticky":false}}'>cut</b></div></div>
        <script>
        Site.page = {json.dumps({'styleLayout': 'Expressive', 'entries': [first]})};
        Site.journal = {json.dumps({'username': journal, 'id': 900})};
        Site.remote = {json.dumps({'username': account})};
        </script>""".encode()


class CommunityWire:
    """Actual client authentication, routing and budgets against synthetic responses."""
    def __init__(self, *, manager='manager', journal='bookclub', managed=True, caps=True, denied=False, reactors=False):
        self.manager, self.journal = manager, journal
        self.managed, self.caps, self.denied = managed, caps, denied
        self.reactors = reactors
        self.calls = []

    def entry(self, eid):
        return {'itemid': eid, 'ditemid': eid * 256 + 7, 'anum': 7, 'journalid': 900,
                'eventtime': f'2026-01-0{eid} 12:00:00', 'subject': f'Post {eid}', 'event': f'Text {eid}',
                'reply_count': 2, 'poster': 'alice' if eid == 1 else 'bob', 'poster_id': 100 + eid,
                'poster_userpic_url': '', 'security': 'usemask', 'allowmask': 1, 'props': {'hasscreened': 1},
                'url': f'https://{self.journal}.livejournal.com/{eid * 256 + 7}.html'}

    def request(self, method, url, **kwargs):
        if method == 'GET' and url in (f'https://{self.journal}.livejournal.com/',
                                       f'https://{self.journal}.livejournal.com/?style=mine'):
            self.calls.append(('feed_page', {}))
            assert kwargs['headers']['Cookie'] == 'ljsession=test-session'
            return Response(content=community_home(self.journal, self.manager))
        if method == 'GET' and url == 'https://www.livejournal.com/':
            self.calls.append(('reaction_token', {}))
            return Response(content=b'<script>Site={"auth_token":"test-token"}</script>')
        if url == 'https://www.livejournal.com/__api/':
            batch = json.loads(kwargs['data'])
            self.calls.append(('reaction_authors', batch))
            return Response(content=json.dumps([{'id': item['id'], 'result': {'result': [
                {'id': 102, 'username': 'bob', 'type': 0}]}} for item in batch]).encode())
        if url.endswith('/community/manage.bml'):
            self.calls.append(('manage', {}))
            assert kwargs['headers']['Cookie'] == 'ljsession=test-session'
            link = 'settings' if self.managed else 'moderate'
            return Response(content=f'<table><tr><td><a href="/community/{link}.bml?authas={self.journal}">Manage</a></td></tr></table>'.encode())
        if url.endswith('/export_comments.bml'):
            self.calls.append(('export_comments', kwargs['params']))
            assert kwargs['params']['authas'] == self.journal
            return Response(content=b'<livejournal><comments/><maxid>0</maxid></livejournal>')
        if url == 'https://likes.services.livejournal.com/get':
            items = json.loads(kwargs['data'])['items']
            self.calls.append(('reactions', items))
            assert all(item['journal'] == 900 for item in items)
            return Response(content=json.dumps([{**item, 'count': 1 if self.reactors else 0, 'groups': [1] if self.reactors else []} for item in items]).encode())
        assert url == 'https://www.livejournal.com/interface/xmlrpc', url
        args, method_name = xmlrpc.client.loads(kwargs['data'])
        name = method_name.rsplit('.', 1)[-1]
        params = args[0] if args else {}
        self.calls.append((name, params))
        if name == 'getchallenge':
            return Response({'challenge': 'test-challenge'})
        assert params['username'] == self.manager
        assert params['auth_method'] == 'challenge'
        if name == 'login':
            if params.get('usejournal'):
                assert params['usejournal'] == self.journal
                return Response({'username': self.journal, 'userid': 900, **({'caps': 0} if self.caps else {})})
            assert 'getpickws' not in params and 'getpickwurls' not in params
            return Response({'username': self.manager, 'userid': 55, 'fullname': 'Personal name must stay out',
                             'usejournals': [self.journal], 'friendgroups': [{'name': 'Personal group'}]})
        if name == 'sessiongenerate':
            assert 'usejournal' not in params
            return Response({'ljsession': 'test-session'})
        if name in ('getevents', 'getdaycounts', 'syncitems'):
            assert params['usejournal'] == self.journal
        if name == 'getdaycounts':
            return Response({'daycounts': [{'date': '2026-01-01', 'count': 1}, {'date': '2026-01-02', 'count': 1}]})
        if name == 'getevents':
            ids = [1, 2] if params['selecttype'] == 'lastn' else [params.get('day') or params['itemid']]
            return Response({'events': [self.entry(eid) for eid in ids]})
        if name == 'getcomments':
            assert params['journal'] == self.journal
            if self.denied:
                return Response(content=xmlrpc.client.dumps(xmlrpc.client.Fault(300, 'Access denied')).encode())
            eid = params['itemid']
            return Response({'page': 1, 'pages': 1, 'comments': [
                {'dtalkid': (eid * 10) * 256 + 7, 'posterid': 101, 'postername': 'alice',
                 'body': 'Hidden', 'state': 'S', 'userpic': '', 'is_loaded': 1, 'children': [
                    {'dtalkid': (eid * 10 + 1) * 256 + 7, 'parentdtalkid': eid * 10 * 256 + 7,
                     'posterid': 102, 'postername': 'bob', 'body': 'Reply', 'state': 'A', 'userpic': '', 'is_loaded': 1}]}]})
        raise AssertionError(name)


def community_client(monkeypatch, **kwargs):
    wire = CommunityWire(**kwargs)
    client = LiveJournalClient(wire.manager, 'test-password', journal=wire.journal, budget=RequestBudget(100))
    monkeypatch.setattr(client, '_wait', lambda _: client.check())
    monkeypatch.setattr(client.session, 'request', wire.request)
    return client, wire


def test_community_export_preserves_authors_hidden_threads_and_source(tmp_path, monkeypatch):
    client, wire = community_client(monkeypatch, reactors=True)
    report = SyncExporter(tmp_path, client, Scope(), images=False, account=True, messages=True).run()
    rows = json.loads((tmp_path / 'assets/feed-data.js').read_text()[len('window.ARCHIVE='):-1])['entries']
    assert [(r['id'], r['sticky']) for r in rows] == [(1, True), (2, False)]
    assert sum(name == 'feed_page' for name, _ in wire.calls) == 1
    assert report['status'] == 'complete', report['issues']
    assert report['username'] == 'bookclub'
    assert report['requests_used'] == len(wire.calls)
    data = json.loads((tmp_path / 'data/archive.json').read_text())
    assert data['source']['type'] == 'community'
    assert data['source']['login_username'] == 'manager'
    assert data['source']['id'] == data['journal']['id'] == 900
    assert data['source']['access']['status'] == 'verified'
    assert data['source']['capabilities']['comments'] == 'available'
    assert data['coverage']['personal_requested'] == {'account': False, 'messages': False}
    assert {e['poster'] for e in data['entries']} == {'alice', 'bob'}
    assert data['comments'][0]['state'] == 'S' and data['comments'][1]['parentid'] == 10
    assert data['entries'][0]['security'] == 'usemask' and data['entries'][0]['allowmask'] == 1
    assert data['entries'][0]['reactions']['authors']['users'][0]['username'] == 'bob'
    assert not any(text in json.dumps(data) for text in ('test-password', 'test-session', 'Personal name', 'Personal group'))
    standard = to_activitystreams(data)
    assert standard['attributedTo']['type'] == 'Group'
    articles = [i for i in standard['items'] if i['type'] == 'Article']
    assert {a['attributedTo']['name'] for a in articles} == {'alice', 'bob'}
    assert articles[0]['attributedTo']['id'] == 'urn:livejournal:user:101'
    assert source_snapshot(standard) == data
    html = (tmp_path / 'entries/1.html').read_text()
    assert 'https://alice.livejournal.com/' in html and '>alice</a>' in html


def test_denied_comments_partial_then_resume_and_scoped_update(tmp_path, monkeypatch):
    first, wire = community_client(monkeypatch, denied=True)
    report = SyncExporter(tmp_path, first, Scope(), images=False).run()
    assert report['status'] == 'partial'
    assert report['source']['capabilities']['comments'] == 'denied'
    assert report['entries_saved'] == 2
    second, wire2 = community_client(monkeypatch, manager='othermanager')
    report = SyncExporter(tmp_path, second, Scope(), images=False).run()
    assert report['status'] == 'complete', report['issues']
    assert report['entries_saved'] == 2 and report['comments_saved'] == 4
    assert report['source']['login_username'] == 'othermanager'
    third, wire3 = community_client(monkeypatch)
    report = SyncExporter(tmp_path, third, Scope('period', '2026-01-02', '2026-01-02'), images=False).run()
    assert report['status'] == 'complete'
    assert report['entries_saved'] == 2
    assert [p['day'] for n, p in wire3.calls if n == 'getevents'] == [2]
    fourth, wire4 = community_client(monkeypatch)
    report = SyncExporter(tmp_path, fourth, Scope('urls', urls=('https://bookclub.livejournal.com/263.html',)), images=False).run()
    assert report['status'] == 'complete'
    assert [p['itemid'] for n, p in wire4.calls if n == 'getevents'] == [1]


@pytest.mark.parametrize('kwargs', [{'managed': False}, {'caps': False}])
def test_posting_or_moderating_is_not_management(tmp_path, monkeypatch, kwargs):
    client, wire = community_client(monkeypatch, **kwargs)
    report = SyncExporter(tmp_path, client, Scope()).run()
    assert report['status'] == 'partial'
    assert report['source']['access']['status'] == 'unconfirmed'
    assert not any(n in ('getevents', 'getdaycounts', 'getcomments') for n, _ in wire.calls)


def test_latest_community_and_bulk_comments_route_to_community(tmp_path, monkeypatch):
    client, wire = community_client(monkeypatch)
    report = Exporter(tmp_path, client, images=False, entry_limit=2).run()
    assert report['status'] == 'complete', report['issues']
    repeated, _ = community_client(monkeypatch)
    assert Exporter(tmp_path, repeated, images=False, entry_limit=2).run()['status'] == 'complete'
    client2, wire2 = community_client(monkeypatch)
    try:
        client2.login()
        assert client2.comments('comment_meta', 1)[0].maxid == 0
        assert wire2.calls[-1][1]['authas'] == 'bookclub'
    finally:
        client2.close()


def test_community_login_budget_can_resume_without_losing_scope(tmp_path, monkeypatch):
    client, wire = community_client(monkeypatch)
    client.budget.limit = 3
    with pytest.raises(ExportError, match='предел'):
        Exporter(tmp_path, client, images=False, entry_limit=2).run()
    assert client.budget.used == len(wire.calls) == 3
    resumed, _ = community_client(monkeypatch)
    assert Exporter(tmp_path, resumed, images=False, entry_limit=2).run()['status'] == 'complete'


def test_community_userpics_and_media_keep_original_authors(tmp_path, monkeypatch):
    import base64
    pic = 'https://l-userpic.livejournal.com/123/101'
    attachment = 'https://example.invalid/image.png'
    store = Store(tmp_path)
    (tmp_path / 'media').mkdir()
    (tmp_path / 'media/picture.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1kAAAAASUVORK5CYII='))
    with store.db:
        store.set('media', {pic: 'media/picture.png', attachment: 'media/picture.png'})
    store.close()
    client, wire = community_client(monkeypatch)
    original = wire.entry
    monkeypatch.setattr(wire, 'entry', lambda eid: {**original(eid), 'poster_userpic_url': pic,
        'event': f'<img src="{attachment}">Text {eid}'})
    report = SyncExporter(tmp_path, client, Scope(), images=True).run()
    assert report['status'] == 'complete', report['issues']
    data = json.loads((tmp_path / 'data/archive.json').read_text())
    assert data['userpics']['post']['1']['path'] == 'media/picture.png'
    assert data['media'][attachment] == 'media/picture.png'
    assert data['entries'][0]['poster'] == 'alice'
    assert '../media/picture.png' in (tmp_path / 'entries/1.html').read_text()


def test_legacy_source_migration_and_identity_guards(tmp_path):
    store = Store(tmp_path)
    with store.db:
        store.set('username', 'owner')
        store.set('journal_id', 11)
        store.save_entry({'itemid': 1, 'event': 'keep'})
    assert snapshot(store)['source']['type'] == 'own'
    store.close()
    store = Store(tmp_path)
    assert store.get('source')['login_username'] == 'owner'
    for client in (LiveJournalClient('manager', '', journal='owner'), LiveJournalClient('owner', '')):
        client.journal_id = 999
        try:
            with pytest.raises(ExportError), store.db:
                bind_source(store, client)
            assert store.entries()[0]['event'] == 'keep' and store.get('journal_id') == 11
        finally:
            client.close()
    store.close()


def test_management_links_cannot_be_substituted_by_posting_or_external_links():
    assert managed_communities(b'''<a href="/update.bml?usejournal=poster">Post</a>
        <a href="/community/moderate.bml?authas=moderator">Moderate</a>
        <a href="https://evil.invalid/community/settings.bml?authas=foreign">Bad</a>
        <a href="/community/settings.bml?authas=good">Settings</a>''') == {'good'}


def test_community_requests_refuse_unverified_access_and_foreign_entries(monkeypatch):
    client, wire = community_client(monkeypatch)
    try:
        with pytest.raises(ExportError, match='не подтверждены'):
            client.call('getevents', selecttype='lastn', howmany=1)
        assert not wire.calls
        client.login()
        with pytest.raises(ExportError, match='другому журналу'):
            client._validate_entries({'events': [{'url': 'https://manager.livejournal.com/263.html'}]})
        with pytest.raises(ExportError, match='другого журнала'):
            client._validate_entries({'events': [{'journalid': 55}]})
        for method in ('getfriends', 'getinbox'):
            with pytest.raises(ExportError, match='Личные данные'):
                client.call(method)
    finally:
        client.close()


def test_server_persists_login_separately_and_resumes_community(local_server, monkeypatch):
    server, base = local_server
    calls = []
    def run(user, password, output, **params):
        calls.append((user, password, params))
        return {'status': 'partial', 'entries_saved': 1, 'comments_saved': 0, 'requests_used': 7}
    monkeypatch.setattr('ljarchive.server.sync', run)
    with requests.Session() as session:
        session.get(base)
        data = {'username': 'manager', 'journal': 'bookclub', 'source_type': 'community',
                'password': 'test-password', 'mode': 'full', 'account': True, 'messages': True}
        response = session.post(base + '/api/export', headers={'Origin': base}, json=data)
        assert response.status_code == 202, response.text
        server.jobs.thread.join(5)
        assert calls[-1][0] == 'manager' and calls[-1][2]['journal'] == 'bookclub'
        assert calls[-1][2]['account'] is False and calls[-1][2]['messages'] is False
        saved = (server.jobs.tasks / 'bookclub.json').read_text()
        assert 'test-password' not in saved
        assert json.loads(saved)['state']['username'] == 'bookclub'
        response = session.post(base + '/api/export', headers={'Origin': base}, json={'resume': 'bookclub', 'password': 'test-password'})
        assert response.status_code == 202, response.text
        server.jobs.thread.join(5)
        assert calls[-1][0] == 'manager' and calls[-1][2]['journal'] == 'bookclub'
        assert server.jobs.archives()['archives'][0]['username'] == 'bookclub'


def test_community_picker_lists_managed_journals_without_creating_archive(local_server, monkeypatch):
    server, base = local_server
    client, wire = community_client(monkeypatch)
    monkeypatch.setattr('ljarchive.client.LiveJournalClient', lambda *args, **kwargs: client)
    with requests.Session() as session:
        session.get(base)
        response = session.post(base + '/api/communities', headers={'Origin': base},
                                json={'username': wire.manager, 'password': 'test-password'})
    assert response.json() == {'communities': [wire.journal]}
    assert [name for name, _ in wire.calls] == ['getchallenge', 'sessiongenerate', 'manage']
    assert client._cookie is None and not list(server.jobs.tasks.glob('*.json'))

def test_community_pin_evidence_and_offline_build(tmp_path, monkeypatch):
    import sys
    from ljarchive.__main__ import main
    from ljarchive.feed_pin import page_pin

    home = community_home()
    def parse(page):
        return page_pin(page, 'bookclub', 900, 'manager')
    assert parse(home) == 263
    assert parse(community_home(icon=False)) is None
    assert page_pin(community_home(icon=False), 'bookclub', 900, 'manager', requested_home=True) == 0
    local = home.replace(b'https://l-stat.livejournal.net/img/icon_sticky.png?v=1"',
                         './saved_files/icon_sticky.png" alt="Верхняя запись" title="Верхняя запись"'.encode())
    assert parse(local) == 263
    assert parse(community_home(account='guest')) is None
    assert parse(home.replace(b'asset-bookclub-263', b'asset-other-263')) is None
    assert parse(home.replace(b'class="asset-header"', b'class="asset-body"')) is None
    assert parse(home.replace(b'Expressive', b'other')) is None
    assert parse(home.replace(b'icon_sticky.png', b'icon_protected.gif')) is None
    assert parse(b'<p>No entries.</p>') is None

    client, _ = community_client(monkeypatch)
    SyncExporter(tmp_path, client, Scope(), images=False).run()
    store = Store(tmp_path)
    try:
        early, later = sorted(store.entries(), key=lambda e: e['itemid'])
        with store.db:
            store.save_entry(early)
            store.observe_feed_pin(None)
            assert store.sticky_entry_id() == 1
            store.save_entry({**later, 'props': {'sticky': 1}})
            assert store.sticky_entry_id() == 2
            assert store.feed_pin() == {'ditemid': later['ditemid'], 'source': 'getevents'}
    finally:
        store.close()
    saved = tmp_path / 'home.html'
    saved.write_bytes(home)
    monkeypatch.setattr(sys, 'argv', ['lj-archive', 'build', str(tmp_path), '--journal-page', str(saved)])
    main()
    data = json.loads((tmp_path / 'data/archive.json').read_text())
    assert data['journal']['feed_pin'] == {'source': 'journal_page', 'ditemid': 263}
    assert 'sticky' not in data['entries'][0]['props']
    assert source_snapshot(json.loads((tmp_path / 'data/activitystreams.json').read_text())) == data
    store = Store(tmp_path)
    try:
        with store.db:
            store.save_entry({**early, 'props': {'sticky': 0}})
            assert store.sticky_entry_id() == 0
    finally:
        store.close()


def test_pin_outside_current_export_matches_full_id_when_entry_arrives(tmp_path):
    store = Store(tmp_path)
    try:
        with store.db:
            store.set('username', 'bookclub')
            store.observe_feed_pin(263)
            assert store.sticky_entry_id() == 0
            store.save_entry({'itemid': 1, 'anum': 8})
            assert store.sticky_entry_id() == store.get('sticky_entry_id') == 0
            store.save_entry({'itemid': 1, 'url': 'https://bookclub.livejournal.com/263.html'})
            assert store.sticky_entry_id() == store.get('sticky_entry_id') == 1
    finally:
        store.close()

def test_community_pin_can_use_readers_layout(monkeypatch):
    client, wire = community_client(monkeypatch)
    try:
        client.login()
        seen = []
        def request(method, url, **kwargs):
            seen.append(url)
            if url == 'https://bookclub.livejournal.com/':
                return Response(content=community_home().replace(b'Expressive', b'other'))
            return wire.request(method, url, **kwargs)
        monkeypatch.setattr(client.session, 'request', request)
        assert client.journal_feed_pin() == 263
        assert seen == ['https://bookclub.livejournal.com/', 'https://bookclub.livejournal.com/?style=mine']
    finally:
        client.close()
