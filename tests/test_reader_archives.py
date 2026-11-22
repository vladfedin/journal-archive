import json
import xmlrpc.client
from datetime import datetime, timedelta

import pytest

from ljarchive.client import ExportError, LiveJournalClient
from ljarchive.exporter import export
from ljarchive.sources import archive_base, bind_source, source
from ljarchive.sync import Scope, SyncExporter, sync, url_id
from test_sources import CommunityWire, Response, community_home


class ReaderWire(CommunityWire):
    def __init__(self, *, manager='reader', community=False, count=3):
        super().__init__(manager=manager, journal='bookclub', managed=False)
        self.community = community
        self.screened = False
        self.posts = []
        for eid in range(1, count + 1):
            stamp = datetime(2025, 1, 1) + timedelta(days=(eid - 1) // 2)
            self.posts.append({**self.entry(eid), 'eventtime': str(stamp), 'logtime': str(stamp),
                               'security': 'usemask' if eid == 2 else 'public'})

    def request(self, method, url, **kwargs):
        if url == f'https://{self.journal}.livejournal.com/':
            self.calls.append(('feed_page', {}))
            assert kwargs['headers']['Cookie'] == 'ljsession=test-session'
            page = community_home(self.journal, self.manager).replace(
                b'Site.journal = {', b'Site.journal = {"is_community":' + json.dumps(self.community).encode() + b',')
            return Response(content=page)
        if url.endswith('/interface/xmlrpc'):
            args, method_name = xmlrpc.client.loads(kwargs['data'])
            name = method_name.rsplit('.', 1)[-1]
            if name in ('getevents', 'getcomments'):
                params = args[0]
                assert params['username'] == self.manager and params['auth_response']
                assert params['journal' if name == 'getcomments' else 'usejournal'] == self.journal
                self.calls.append((name, params))
                if name == 'getcomments':
                    if self.screened:
                        return Response({'pages': 1, 'page': 1, 'comments': [
                            {'dtalkid': params['itemid'] * 256 + 7, 'state': 'S', 'is_show': 0}]})
                    return Response({'pages': 1, 'page': 1, 'comments': [
                        {'dtalkid': (params['itemid'] * 10) * 256 + 7, 'body': 'A visible reply'}]})
                entries = [p for p in self.posts if self.manager == 'reader' or p['security'] == 'public']
                if params['selecttype'] == 'one':
                    return Response({'events': [p for p in entries if p['itemid'] == params['itemid']]})
                key = 'logtime' if self.community else 'eventtime'
                entries = sorted(entries, key=lambda p: (p[key], p['itemid']), reverse=True)
                if 'beforedate' in params:
                    entries = [p for p in entries if p[key] < params['beforedate']]
                offset = params.get('skip', 0)
                return Response({'events': entries[offset:offset + params['howmany']]})
            assert name in ('getchallenge', 'login', 'sessiongenerate')
        assert not any(part in url for part in ('export_comments', 'community/manage', 'profile/'))
        return super().request(method, url, **kwargs)


def install(monkeypatch, wire):
    monkeypatch.setattr(LiveJournalClient, '_wait', lambda c, _: c.check())
    monkeypatch.setattr('requests.Session.request', lambda session, *a, **kw: wire.request(*a, **kw))


def test_read_accounts_isolated_and_trial_retains_history(tmp_path, monkeypatch):
    for account, expected in [('reader', 3), ('visitor', 2)]:
        wire = ReaderWire(manager=account)
        install(monkeypatch, wire)
        options = dict(journal='bookclub', source_type='reader', max_requests=90, account=True, messages=True)
        report = export(account, 'test-secret', tmp_path, **options)
        assert report['status'] == 'complete', report['issues']
        assert report['entries_saved'] == report['comments_saved'] == expected
        folder = tmp_path / f'bookclub-by-{account}-latest-10'
        archive = json.loads((folder / 'data/archive.json').read_text())
        assert any(p['security'] == 'usemask' for p in archive['entries']) == (account == 'reader')
        assert archive['source']['login_username'] == account and archive['journal']['id'] == 900
        assert 'Personal name' not in json.dumps(archive)
        wire.posts.clear()
        report = export(account, 'test-secret', tmp_path, **options)
        assert report['status'] == 'complete' and report['entries_saved'] == expected
        archive = json.loads((folder / 'data/archive.json').read_text())
        assert all(p['source_status'] == 'not_returned' for p in archive['entries'])


def test_period_then_urls_changes_comment_choice(tmp_path, monkeypatch):
    wire = ReaderWire(community=True)
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader', max_requests=100)
    wire.posts[0]['eventtime'] = '2010-12-31 12:00:00'
    report = sync('reader', 'test-secret', tmp_path, mode='period', start='2010-12-31', end='2010-12-31',
                  include_comments=False, **options)
    assert report['status'] == 'complete' and report['entries_saved'] == 1
    assert not any(m == 'getcomments' for m, _ in wire.calls)
    folder = tmp_path / 'bookclub-by-reader'
    assert 'Комментарии не запрашивались' in (folder / 'entries/1.html').read_text()
    report = sync('reader', 'test-secret', tmp_path, mode='urls', urls=[wire.posts[0]['url']], **options)
    assert report['status'] == 'complete' and report['comments_saved'] == 1
    assert 'A visible reply' in (folder / 'entries/1.html').read_text()
    assert report['source']['is_community']


def test_reader_cursor_ties_resume_and_identity_guards(tmp_path, monkeypatch):
    wire = ReaderWire(count=104)
    install(monkeypatch, wire)
    # Split a second across pages, including a page entirely inside that second.
    for p in wire.posts[:63]:
        p['eventtime'] = wire.posts[0]['eventtime']
    def start():
        client = LiveJournalClient('reader', 'test-secret', journal=wire.journal, source_type='reader')
        job = SyncExporter(tmp_path, client, Scope())
        job.prepare()
        client.login()
        return job
    job = start()
    original = job.client.call
    def interrupt(method, **args):
        if args.get('beforedate'):
            raise ExportError('test interruption')
        return original(method, **args)
    monkeypatch.setattr(job.client, 'call', interrupt)
    with pytest.raises(ExportError, match='test interruption'):
        job.posts()
    assert len(job.state['seen']) == 50
    job.store.close()
    job.client.close()
    job = start()
    try:
        job.posts()
        assert job.selected_ids == set(range(1, 105))
        with pytest.raises(ExportError):
            job.client.call('getinbox')
        with pytest.raises(ExportError):
            job.client.call('getevents', journal='someone_else')
        with pytest.raises(ExportError):
            bind_source(job.store, LiveJournalClient('visitor', '', journal='bookclub', source_type='reader'))
    finally:
        job.store.close()
        job.client.close()
    assert archive_base(source('reader', 'bookclub', 'reader')) == 'bookclub-by-reader'
    assert url_id('_books_', 'https://users.livejournal.com/_books_/263.html') == 263

def test_unknown_journal_type_cannot_skip_community_entries(tmp_path, monkeypatch):
    wire = ReaderWire(community=True, count=51)
    install(monkeypatch, wire)
    for post in wire.posts:
        post['logtime'] = str(datetime.fromisoformat(post['eventtime']) + timedelta(hours=3))
    client = LiveJournalClient('reader', '', journal=wire.journal, source_type='reader')
    job = SyncExporter(tmp_path, client, Scope())
    try:
        job.prepare()
        client.login()
        client.source.pop('is_community')
        with pytest.raises(ExportError, match='тип журнала'):
            job.posts()
        assert not job.store.get('posts_done')
    finally:
        job.store.close()
        client.close()

def test_screened_for_reader_is_an_observed_result(tmp_path, monkeypatch):
    wire = ReaderWire(count=1)
    wire.screened = True
    install(monkeypatch, wire)
    report = sync('reader', 'test-secret', tmp_path, journal=wire.journal, source_type='reader')
    assert report['status'] == 'complete' and report['missing_comments'] == []
    assert 'Текст скрыт от читателя' in (tmp_path / 'bookclub-by-reader/entries/1.html').read_text()


def test_reader_retries_html_login_page_once(monkeypatch):
    wire = ReaderWire()
    install(monkeypatch, wire)
    client = LiveJournalClient('reader', 'test-secret', journal=wire.journal, source_type='reader')
    reads = []
    original = client._request
    def challenge(method, path, **params):
        if path != '/':
            return original(method, path, **params)
        reads.append(params)
        if len(reads) == 1:
            return b'<form action="/login.bml"><input name="lj_form_auth" value="form"></form>'
        return community_home(wire.journal, wire.manager)
    monkeypatch.setattr(client, '_request', challenge)
    monkeypatch.setattr(client, 'ensure_web_session', lambda url: url == 'https://bookclub.livejournal.com/')
    try:
        client.login()
        assert client.source['id'] == 900 and len(reads) == 2
        assert 'headers' not in reads[1]
    finally:
        client.close()


@pytest.mark.parametrize('mode', ['latest', 'full'])
def test_reader_offline_build_after_interruption(tmp_path, monkeypatch, mode):
    import sys
    from ljarchive.__main__ import main
    from ljarchive.storage import Store
    folder = tmp_path / 'bookclub-by-reader'
    store = Store(folder)
    wire = ReaderWire(count=1)
    with store.db:
        store.set('username', wire.journal)
        store.set('source', source('reader', wire.journal, 'reader'))
        store.save_entry(wire.posts[0])
        if mode == 'full':
            store.set('sync_job', {'scope': Scope().data(), 'seen': [1], 'phase': 'posts'})
        else:
            store.set('entry_limit', 10)
    store.close()
    monkeypatch.setattr(sys, 'argv', ['ljarchive', 'build', str(folder)])
    main()
    report = json.loads((folder / 'export-report.json').read_text())
    assert report['status'] == 'partial' and report['source']['login_username'] == 'reader'
    assert 'Text 1' in (folder / 'entries/1.html').read_text()


def test_cli_refresh_uses_saved_reader(tmp_path, monkeypatch):
    import sys
    from ljarchive.__main__ import main
    wire = ReaderWire(count=1)
    install(monkeypatch, wire)
    wire.screened = True
    before = export('reader', 'test-secret', tmp_path, journal=wire.journal, source_type='reader', max_requests=60)
    assert before['status'] == 'complete'
    wire.calls.clear()
    folder = tmp_path / 'bookclub-by-reader-latest-10'
    monkeypatch.setattr(sys, 'argv', ['ljarchive', 'refresh-reactions', str(folder)])
    monkeypatch.setattr('getpass.getpass', lambda _: 'test-secret')
    main()
    assert any(name == 'login' and args['username'] == 'reader' for name, args in wire.calls)
    origin = json.loads((folder / 'data/archive.json').read_text())['source']
    assert origin['login_username'] == 'reader'
    assert origin['capabilities'] == before['source']['capabilities']
    assert all('thread' not in item for name, batch in wire.calls if name == 'reactions' for item in batch)
