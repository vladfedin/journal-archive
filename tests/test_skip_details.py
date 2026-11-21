from unittest.mock import patch

from ljarchive.client import AuthenticationRequired, RequestLimitReached
from ljarchive.on_demand import image_failures
from ljarchive.skip_details import skipped_items
from ljarchive.storage import Store
from ljarchive.sync import Scope, SyncExporter
from test_sync import Journal


class ProfileRedirect(Journal):
    def subscription_preview(self):
        return {k: {'list_name': k, 'count': 0, 'list': []} for k in ('subscribers', 'subscribersof')}, None

    def managed_communities(self, *, confirm_empty=False):
        return []

    def personal_feed(self, name):
        raise AuthenticationRequired('ЖЖ перенаправил запрос.', status=302)

    def call(self, method, **params):
        if method == 'getfriends':
            return {'friends': [], 'friendofs': [], 'friendgroups': []}
        if method == 'getusertags':
            return {'tags': []}
        return super().call(method, **params)

    def personal_page(self, path):
        return b'<input type="checkbox" name="LJ__Setting__Example">'


def test_optional_profile_redirect_continues_to_video_and_images(tmp_path):
    exporter = SyncExporter(tmp_path, ProfileRedirect(), Scope('urls', urls=('https://fixture.livejournal.com/263.html',)),
                            account=True, videos=True, images=True)
    with patch.object(exporter, 'material_stage', wraps=exporter.material_stage) as videos, \
         patch.object(exporter, 'retry_materials', wraps=exporter.retry_materials) as images:
        report = exporter.run()
    videos.assert_called_once()
    images.assert_called_once()
    assert report['finished_with_skips']
    assert report['personal']['sections']['foaf']['error'].startswith('HTTP 302')
    page = (tmp_path / 'report.html').read_text()
    assert 'Архив готов' in page and 'Загрузка не завершена' not in page
    assert 'id="skipped"' in page and 'HTTP 302' in page
    assert 'Архив неполный' not in (tmp_path / 'index.html').read_text()


def test_profile_request_limit_keeps_interrupted_status(tmp_path):
    class Limited(ProfileRedirect):
        def personal_feed(self, name):
            self.budget.exhausted = True
            raise RequestLimitReached('Предел запросов')
    report = SyncExporter(tmp_path, Limited(), Scope('urls', urls=('https://fixture.livejournal.com/263.html',)), account=True).run()
    assert not report['finished_with_skips'] and report['request_limit_reached']


def test_skips_link_to_comment_and_source_and_omit_confirmed_absences(tmp_path):
    url = 'https://example.com/missing.png'
    blocked = 'https://example.com/denied.png'
    post = {'itemid': 1, 'ditemid': 263, 'subject': 'Запись'}
    live = {'id': 11, 'dtalkid': 2823, 'jitemid': 1}
    deleted = {'id': 12, 'state': 'D', 'jitemid': 1}
    errors = {url: {'reason': 'tls'}, blocked: {'reason': 'browser_http', 'availability': 'unavailable', 'closed': True}}
    rows = skipped_items({'username': 'fixture', 'missing_comments': [live, deleted], 'media_errors': errors},
                         [{'url': url, 'references': [{'entry_id': 1, 'comment_id': 11}]}], [post], [live, deleted])
    assert len(rows) == 2
    assert [r['url'] for r in rows] == [url, 'https://fixture.livejournal.com/263.html?thread=2823']
    assert all(r['links'][0]['href'] == 'entries/1.html#comment-11' for r in rows)
    store = Store(tmp_path)
    try:
        with store.db:
            store.set('media_errors', errors)
        assert [r['url'] for r in image_failures(store)] == [url]
    finally:
        store.close()


def test_finished_profile_gap_does_not_reuse_queue_on_url_update(tmp_path):
    import json
    scope = Scope('urls', urls=('https://fixture.livejournal.com/263.html',))
    report = SyncExporter(tmp_path, ProfileRedirect(), scope, account=True).run()
    assert report['finished_with_skips'] and report['sync']['done']
    for revision in (1, 2, 3):
        if revision >= 2:
            # State written by the version that did not finish the render checkpoint.
            store = Store(tmp_path)
            try:
                with store.db:
                    state = store.get('sync_job')
                    state.update(phase='render', done=False)
                    store.set('sync_job', state)
            finally:
                store.close()
            path = tmp_path / 'export-report.json'
            old_report = json.loads(path.read_text())
            old_report.update(finished_with_skips=revision == 2, status='partial' if revision == 2 else 'complete', sync=state)
            path.write_text(json.dumps(old_report))
        client = Journal(revision=revision)
        SyncExporter(tmp_path, client, scope).run()
        assert {'getevents', 'getcomments'} <= {method for method, _ in client.calls}
        snapshot = json.loads((tmp_path / 'data/archive.json').read_text())
        assert snapshot['entries'][0]['event'].endswith(f'revision {revision}')
        assert snapshot['comments'][0]['body'].endswith(f'revision {revision}')


def test_profile_refresh_and_recovery_report_the_actual_result(tmp_path):
    from types import SimpleNamespace
    from ljarchive.personal import Personal, refresh_profile
    from ljarchive.recovery import inventory, summary
    scope = Scope('urls', urls=('https://fixture.livejournal.com/263.html',))
    SyncExporter(tmp_path, ProfileRedirect(), scope, account=True).run()
    store = Store(tmp_path)
    try:
        row = next(r for r in summary(inventory(store))['rows'] if r['key'] == 'personal')
        assert row['reasons'] and all('HTTP 302' in r['message'] for r in row['reasons'])
        collector = Personal(SimpleNamespace(store=store, client=None))
        for key in ('foaf', 'userpics'):
            collector.save(key, status='captured', data={})
        repaired = refresh_profile(store)
        assert repaired['status'] == 'complete' and not repaired['finished_with_skips']
        collector.save('foaf', status='partial', error='HTTP 302')
        partial = refresh_profile(store)
        assert partial['status'] == 'partial' and partial['finished_with_skips']
        assert 'Загрузка не завершена' not in (tmp_path / 'report.html').read_text()
        assert 'Архив неполный' not in (tmp_path / 'index.html').read_text()
    finally:
        store.close()


def test_exhausted_http_429_preserves_stop_and_cooldown(tmp_path):
    from unittest.mock import MagicMock
    from ljarchive.client import LiveJournalClient
    client = ProfileRedirect()
    http = LiveJournalClient('fixture', '', max_attempts=2, budget=client.budget)
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code, response.headers = 429, {'Retry-After': '900'}
    def feed(name):
        assert name == 'foaf'
        http.save_cooldown = client.save_cooldown
        return http._request('GET', '/data/foaf', category='personal')
    scope = Scope('urls', urls=('https://fixture.livejournal.com/263.html',))
    exporter = SyncExporter(tmp_path, client, scope, account=True, images=True, videos=True)
    try:
        with patch.object(client, 'personal_feed', feed), patch.object(http, '_wait'), \
             patch.object(http.session, 'request', return_value=response) as calls, \
             patch.object(exporter, 'material_stage') as video:
            report = exporter.run()
        assert calls.call_count == 2 and not video.called
        assert report['status'] == 'partial' and not report['finished_with_skips'] and not report['sync']['done']
        assert '429' in report['issues'][0] and '429' in report['personal']['sections']['foaf']['error']
        store = Store.reader(tmp_path)
        try:
            assert store.get('request_wait')['http_status'] == 429
            assert store.get('request_wait')['server_delay']
        finally:
            store.close()
    finally:
        http.close()


def test_exhausted_counters_and_missing_url_have_source_links(tmp_path):
    class EmptyCounts(Journal):
        def reaction_counts(self, entities):
            return []
    url = 'https://fixture.livejournal.com/263.html?thread=2823'
    scope = Scope('urls', urls=(url,))
    report = SyncExporter(tmp_path / 'reactions', EmptyCounts(), scope).run()
    gaps = [r for r in report['skipped_items'] if r['reason'] == 'ЖЖ не вернул счётчик реакций']
    assert len(gaps) == report['reactions']['unavailable'] == 3
    assert {r['links'][0]['href'] for r in gaps} == {'entries/1.html','entries/1.html#comment-11','entries/1.html#comment-12'}
    report = SyncExporter(tmp_path / 'absent', Journal(removed=True), scope).run()
    absent = next(r for r in report['skipped_items'] if r['label'] == 'Запись #1')
    assert absent['url'] == url and not absent['links']


def test_retained_counters_are_listed_on_failed_refresh(tmp_path):
    class EmptyCounts(Journal):
        def reaction_counts(self, entities):
            return []
    scope = Scope('urls', urls=('https://fixture.livejournal.com/263.html',))
    SyncExporter(tmp_path, Journal(), scope).run()
    report = SyncExporter(tmp_path, EmptyCounts(), scope).run()
    assert report['reactions']['counts_retained'] == report['reactions']['unavailable'] == 3
    assert len([r for r in report['skipped_items'] if 'прежнее значение' in r['reason']]) == 3
