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
            # Each discussion below answers with one comment, matching LJ's reply count.
            self.posts.append({**self.entry(eid), 'eventtime': str(stamp), 'logtime': str(stamp), 'reply_count': 1,
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


def test_short_reader_discussion_is_not_called_complete(tmp_path, monkeypatch):
    wire = ReaderWire(count=1)
    wire.posts[0].update(reply_count=7, props={})
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader')
    report = sync('reader', 'test-secret', tmp_path, **options)
    # After the repeats what LJ gave is kept and the discussion is reported as incomplete, never as empty.
    assert sum(name == 'getcomments' for name, _ in wire.calls) > 1
    assert report['unavailable_comment_entry_ids'] == [1] and report['pending_comment_entry_ids'] == []
    folder = tmp_path / 'bookclub-by-reader'
    assert 'A visible reply' in (folder / 'entries/1.html').read_text()
    assert 'Обсуждения, которые ЖЖ отдал не полностью' in (folder / 'report.html').read_text()
    trial = export('reader', 'test-secret', tmp_path, max_requests=90, **options)
    assert trial['unavailable_comment_entry_ids'] == [1] and trial['pending_comment_entry_ids'] == []


def test_comments_off_update_keeps_saved_discussions(tmp_path, monkeypatch):
    from ljarchive.progress import timeline
    from ljarchive.storage import Store
    wire = ReaderWire(count=4)
    later = wire.posts.pop()
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader')
    first = sync('reader', 'test-secret', tmp_path, **options)
    assert first['status'] == 'complete' and first['comments_saved'] == 3
    wire.posts.append(later)
    wire.calls.clear()
    report = sync('reader', 'test-secret', tmp_path, mode='period', start='2025-01-01', end='2025-12-31',
                  include_comments=False, **options)
    assert report['status'] == 'complete' and not any(name == 'getcomments' for name, _ in wire.calls)
    assert report['comments_saved'] == 3
    assert report['reactions']['comments_total'] == first['reactions']['comments_total'] == 3
    folder = tmp_path / 'bookclub-by-reader'
    store = Store.reader(folder)
    try:
        items = {item['id']: item for item in timeline(store)['entries']}
    finally:
        store.close()
    assert [items[eid]['comments_skipped'] for eid in (1, 2, 3, 4)] == [False, False, False, True]
    assert all(items[eid]['comments'] == 1 and items[eid]['comments_complete'] for eid in (1, 2, 3))
    assert 'A visible reply' in (folder / 'entries/1.html').read_text()
    assert 'Комментарии не запрашивались' in (folder / 'entries/4.html').read_text()
    assert 'Не запрашивались · сохранённые ранее оставлены' in (folder / 'report.html').read_text()


def test_trial_window_shift_is_not_reported_as_missing(tmp_path, monkeypatch):
    wire = ReaderWire(count=15)
    later = wire.posts[12:]
    del wire.posts[12:]
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader', max_requests=90)
    assert export('reader', 'test-secret', tmp_path, **options)['status'] == 'complete'
    wire.posts = [p for p in wire.posts + later if p['itemid'] != 8]
    report = export('reader', 'test-secret', tmp_path, **options)
    archive = json.loads((tmp_path / 'bookclub-by-reader-latest-10/data/archive.json').read_text())
    absent = sorted(p['itemid'] for p in archive['entries'] if p.get('source_status') == 'not_returned')
    # 3 and 4 only left the last-ten window; 8 vanished from inside it.
    assert absent == [8] and report['unavailable_entry_ids'] == [8]


def test_entries_gone_from_reader_view_reach_the_report(tmp_path, monkeypatch):
    wire = ReaderWire(count=3)
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader')
    assert sync('reader', 'test-secret', tmp_path, **options)['status'] == 'complete'
    wire.posts = [p for p in wire.posts if p['itemid'] != 2]
    report = sync('reader', 'test-secret', tmp_path, mode='period', start='2025-01-01', end='2025-12-31', **options)
    assert report['status'] == 'complete' and report['unavailable_entry_ids'] == [2]
    folder = tmp_path / 'bookclub-by-reader'
    assert 'Записи, которые ЖЖ больше не возвращает' in (folder / 'report.html').read_text()
    assert 'Text 2' in (folder / 'entries/2.html').read_text()


class MissingJournal(ReaderWire):
    def request(self, method, url, **kwargs):
        if url == f'https://{self.journal}.livejournal.com/':
            return Response(status=404, content=b'not found')
        return super().request(method, url, **kwargs)


def test_mistyped_journal_leaves_no_archive(tmp_path, monkeypatch):
    from ljarchive.client import JournalNotFound
    from ljarchive.server import Jobs
    install(monkeypatch, MissingJournal())
    jobs = Jobs(tmp_path)
    jobs.start({'username': 'reader', 'journal': 'bookclub', 'source_type': 'reader', 'password': 'test-secret', 'mode': 'full'})
    jobs.thread.join(30)
    assert jobs.state['phase'] == 'error' and 'не нашёл журнал bookclub' in jobs.state['message']
    assert not (tmp_path / 'bookclub-by-reader').exists()
    assert not any(a['slug'].startswith('bookclub') for a in jobs.archives()['archives'])
    with pytest.raises(JournalNotFound):
        export('reader', 'test-secret', tmp_path, journal='bookclub', source_type='reader', max_requests=20)
    assert not (tmp_path / 'bookclub-by-reader-latest-10').exists()


def test_reader_archive_has_no_correspondence_or_owner_profile(tmp_path, monkeypatch):
    install(monkeypatch, ReaderWire(count=1))
    sync('reader', 'test-secret', tmp_path, journal='bookclub', source_type='reader', account=True, messages=True)
    folder = tmp_path / 'bookclub-by-reader'
    assert not (folder / 'messages.html').exists()
    assert 'messages.html' not in (folder / 'index.html').read_text()
    account = (folder / 'account.html').read_text()
    assert 'messages.html' not in account and 'Профиль журнала не входит в архив читателя' in account


def test_comments_off_keeps_discussions_whose_status_was_reset(tmp_path, monkeypatch):
    from ljarchive.storage import Store
    wire = ReaderWire(count=3)
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader', max_requests=90)
    trial = export('reader', 'test-secret', tmp_path, **options)
    assert trial['reactions']['comments_total'] == 3
    trial = export('reader', 'test-secret', tmp_path, include_comments=False, **options)
    assert trial['reactions']['comments_total'] == 3 and trial['status'] == 'complete'
    del options['max_requests']
    assert sync('reader', 'test-secret', tmp_path, **options)['comments_saved'] == 3
    folder = tmp_path / 'bookclub-by-reader'
    store = Store(folder)
    with store.db:  # an interrupted refresh with comments on leaves the statuses reset
        store.set('entry_comments', {str(eid): {'next_page': 1, 'done': False} for eid in (1, 2, 3)})
    store.close()
    report = sync('reader', 'test-secret', tmp_path, include_comments=False, **options)
    assert report['reactions']['comments_total'] == 3
    store = Store.reader(folder)
    try:
        assert not any(s.get('skipped') for s in store.get('entry_comments').values())
        assert len(store.comments_for_download()) == 3
    finally:
        store.close()


def test_community_trial_window_follows_posting_time(tmp_path, monkeypatch):
    wire = ReaderWire(community=True, count=12)
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader', max_requests=90)
    export('reader', 'test-secret', tmp_path, **options)
    backdated = {**wire.entry(13), 'eventtime': '2010-05-05 10:00:00', 'logtime': '2025-03-01 10:00:00', 'reply_count': 1, 'security': 'public'}
    latest = {**wire.entry(14), 'eventtime': '2025-03-02 10:00:00', 'logtime': '2025-03-02 10:00:00', 'reply_count': 1, 'security': 'public'}
    wire.posts += [backdated, latest]
    report = export('reader', 'test-secret', tmp_path, **options)
    assert report['unavailable_entry_ids'] == []


@pytest.mark.parametrize('community', [False, True])
@pytest.mark.parametrize('moved_to', ['2019-01-01 00:00:00', '2026-06-01 00:00:00'])
def test_entry_moved_out_of_period_is_not_missing(tmp_path, monkeypatch, community, moved_to):
    wire = ReaderWire(count=63, community=community)
    for index, post in enumerate(wire.posts[3:]):
        post['eventtime'] = post['logtime'] = str(datetime(2024, 1, 1) + timedelta(days=index))
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader')
    sync('reader', 'test-secret', tmp_path, **options)
    # Far beyond the page where the personal-journal scan of the period stops.
    wire.posts[2]['eventtime'] = moved_to
    wire.posts = [p for p in wire.posts if p['itemid'] != 2]
    report = sync('reader', 'test-secret', tmp_path, mode='period', start='2025-01-01', end='2025-12-31', **options)
    assert report['unavailable_entry_ids'] == [2]
    assert report['status'] == 'complete'


class InterruptedWire(ReaderWire):
    interrupt_at = None

    def request(self, method, url, **kwargs):
        if url.endswith('/interface/xmlrpc') and self.interrupt_at:
            args = xmlrpc.client.loads(kwargs['data'])[0]
            if args and args[0].get('beforedate') == self.interrupt_at:
                self.interrupt_at = None
                raise ExportError('test interruption')
        return super().request(method, url, **kwargs)


def test_community_period_update_reads_new_posts_and_the_period(tmp_path, monkeypatch):
    wire = InterruptedWire(community=True, count=0)
    def post(eid, logtime, eventtime=None):
        return {**wire.entry(eid), 'logtime': logtime, 'eventtime': eventtime or logtime, 'reply_count': 0, 'security': 'public'}
    # A post every two days from 2015 to 2024, and one posted in 2021 under a 2018 date.
    wire.posts = [post(i + 1, str(datetime(2015, 1, 1) + timedelta(days=2 * i))) for i in range(1826)]
    wire.posts.append(post(2000, '2021-06-01 12:00:00', '2018-07-01 12:00:00'))
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader', mode='period', start='2018-01-01', end='2018-12-31',
                   include_comments=False)
    def requests():
        calls = [p for m, p in wire.calls if m == 'getevents']
        wire.calls.clear()
        return [p for p in calls if p['selecttype'] == 'lastn'], sorted(p['itemid'] for p in calls if p['selecttype'] == 'one')
    first = sync('reader', 'test-secret', tmp_path, **options)
    in_period = {p['itemid'] for p in wire.posts if p['eventtime'].startswith('2018')}
    assert first['status'] == 'complete' and first['entries_saved'] == len(in_period)
    # Everything posted since the period was read, but nothing well before it.
    assert min(p['beforedate'] for p in requests()[0] if 'beforedate' in p) > '2017-12-01'
    edited, deleted = sorted(in_period - {2000})[100:102]
    next(p for p in wire.posts if p['itemid'] == edited)['event'] = 'Edited text'
    wire.posts = [p for p in wire.posts if p['itemid'] != deleted]
    wire.posts += [post(2001, '2025-02-01 10:00:00', '2018-03-03 10:00:00'), post(2002, '2025-02-02 10:00:00')]
    wire.interrupt_at = '2019-01-08 00:00:00'
    assert sync('reader', 'test-secret', tmp_path, **options)['status'] != 'complete'
    pages, _ = requests()
    # Only the newest page was needed for posts made since the first read.
    assert len(pages) == 1 and 'beforedate' not in pages[0]
    report = sync('reader', 'test-secret', tmp_path, **options)
    pages, _ = requests()
    # The resumed update goes on with the period: the new posts were already read.
    assert all('beforedate' in p for p in pages) and len(pages) <= 6
    assert report['status'] == 'complete' and report['unavailable_entry_ids'] == [deleted]
    folder = tmp_path / 'bookclub-by-reader'
    archive = {e['itemid']: e for e in json.loads((folder / 'data/archive.json').read_text())['entries']}
    assert archive[2001]['eventtime'].startswith('2018') and 2002 not in archive
    assert archive[edited]['event'] == 'Edited text' and archive[2000].get('source_status') != 'not_returned'
    # The next update looks once more at the deleted entry and at the one posted outside the spans read.
    report = sync('reader', 'test-secret', tmp_path, **options)
    pages, singles = requests()
    assert len(pages) <= 6 and singles == sorted([2000, deleted])
    assert report['status'] == 'complete' and report['unavailable_entry_ids'] == [deleted]


def test_community_period_read_starts_before_the_period(tmp_path, monkeypatch):
    wire = ReaderWire(community=True, count=0)
    def post(eid, logtime, eventtime=None):
        return {**wire.entry(eid), 'logtime': logtime, 'eventtime': eventtime or logtime, 'reply_count': 0, 'security': 'public'}
    # A page of posts within the period, a page just before it, then one posted before the period under a date in it.
    wire.posts = [post(i + 1, str(datetime(2018, 1, 1) + timedelta(days=i))) for i in range(50)]
    wire.posts += [post(i + 51, str(datetime(2017, 12, 31, 23) - timedelta(hours=i))) for i in range(50)]
    wire.posts += [post(101, '2017-12-28 12:00:00', '2018-01-05 12:00:00'), post(102, '2017-06-01 12:00:00')]
    install(monkeypatch, wire)
    report = sync('reader', 'test-secret', tmp_path, journal=wire.journal, source_type='reader', mode='period',
                  start='2018-01-01', end='2018-12-31', include_comments=False)
    assert report['status'] == 'complete' and report['entries_saved'] == 51


class GoneJournal(ReaderWire):
    gone = False

    def request(self, method, url, **kwargs):
        if self.gone and url == f'https://{self.journal}.livejournal.com/':
            return Response(status=410, content=b'gone')
        return super().request(method, url, **kwargs)


def test_archive_of_a_journal_gone_later_keeps_data_and_says_why(tmp_path, monkeypatch):
    wire = GoneJournal(count=2)
    install(monkeypatch, wire)
    options = dict(journal=wire.journal, source_type='reader')
    sync('reader', 'test-secret', tmp_path, **options)
    wire.gone = True
    report = sync('reader', 'test-secret', tmp_path, **options)
    assert report['status'] == 'partial' and report['entries_saved'] == 2
    assert any('не нашёл журнал bookclub' in issue for issue in report['issues'])
    folder = tmp_path / 'bookclub-by-reader'
    assert json.loads((folder / 'export-report.json').read_text())['status'] == 'partial'
    assert 'Text 1' in (folder / 'entries/1.html').read_text()


def test_pages_rendered_by_the_app_know_the_reader_source(tmp_path, monkeypatch):
    from ljarchive.live import preview_index
    from ljarchive.progress import preview_entry
    from ljarchive.render import ENV
    from ljarchive.sources import folder_source
    from ljarchive.storage import Store
    install(monkeypatch, ReaderWire(count=1))
    sync('reader', 'test-secret', tmp_path, journal='bookclub', source_type='reader')
    folder = tmp_path / 'bookclub-by-reader'
    assert folder_source(folder)['type'] == 'reader' and folder_source(tmp_path / 'missing') is None
    search = ENV.get_template('search.html').render(user='bookclub-by-reader', report={'source': folder_source(folder)}, search_api=True)
    store = Store.reader(folder)
    try:
        pages = [search, preview_index(store), preview_entry(store, 1)]
    finally:
        store.close()
    assert all('messages.html' not in page for page in pages)
