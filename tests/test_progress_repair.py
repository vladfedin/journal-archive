import json
from collections import Counter
from unittest.mock import patch

import pytest

from ljarchive.client import APIError, ExportError
from ljarchive.progress import preview_entry, timeline
from ljarchive.storage import Store
from ljarchive.sync import Scope, SyncExporter
from test_sync import Journal
from test_render_server import local_server

SCOPE = Scope('urls', urls=('https://fixture.livejournal.com/263.html', 'https://fixture.livejournal.com/519.html'))


class BadPagination(Journal):
    def call(self, method, **params):
        result = super().call(method, **params)
        if method == 'getcomments' and params['itemid'] == 1:
            result.pop('pages')
        return result


def test_unconfirmed_pagination_preserves_text_continues_and_retries_only_gap(tmp_path):
    client = BadPagination()
    report = SyncExporter(tmp_path, client, SCOPE).run()
    # One collection and two repeats; then the discussion is settled, not left pending forever.
    assert report['status'] == 'complete', report
    assert report['pending_comment_entry_ids'] == [] and report['unavailable_comment_entry_ids'] == [1]
    calls = Counter((p['itemid'], p['page']) for m, p in client.calls if m == 'getcomments')
    assert calls == {(1, 1): 3, (2, 1): 1, (2, 2): 1}
    assert any(m == 'reactions' for m, _ in client.calls)
    with_reader = Store.reader(tmp_path)
    try:
        data = timeline(with_reader)
        assert data['ready'] == 1 and data['partial'] == 1
        assert data['entries'][0]['top'] == [('reader', 1)]
        assert with_reader.comments(1)[0]['body']
    finally:
        with_reader.close()
    # A continuation does not ask again; an explicit refresh of the entry does.
    resumed = Journal()
    store = Store(tmp_path)
    try:
        with store.db:
            state = store.get('sync_job'); state.update(phase='render', done=False); store.set('sync_job', state)
    finally:
        store.close()
    SyncExporter(tmp_path, resumed, SCOPE).run()
    assert not any(m == 'getcomments' for m, _ in resumed.calls)
    recovery = Journal()
    report = SyncExporter(tmp_path, recovery, Scope('urls', urls=SCOPE.urls[:1])).run()
    assert report['status'] == 'complete', report
    assert report['unavailable_comment_entry_ids'] == []
    assert {p['itemid'] for m, p in recovery.calls if m == 'getcomments'} == {1}


def test_bad_second_page_can_be_retried_without_mistaking_saved_ids_for_loop(tmp_path):
    class Once(Journal):
        failed = False
        def call(self, method, **params):
            result = super().call(method, **params)
            if method == 'getcomments' and params['page'] == 2 and not self.failed:
                self.failed = True
                result.pop('page')
            return result
    client = Once()
    report = SyncExporter(tmp_path, client, Scope('urls', urls=SCOPE.urls[:1])).run()
    assert report['status'] == 'complete'
    assert [p['page'] for m, p in client.calls if m == 'getcomments'] == [1, 2, 2]


def test_empty_discussion_requires_explicit_zero_and_no_screened_flag(tmp_path):
    class Empty(Journal):
        def entry(self, eid):
            return {**super().entry(eid), 'reply_count': 0}
        def call(self, method, **params):
            result = super().call(method, **params)
            return {'comments': []} if method == 'getcomments' else result
    report = SyncExporter(tmp_path, Empty(), Scope('urls', urls=SCOPE.urls[:1])).run()
    assert report['status'] == 'complete'
    class Screened(Empty):
        def entry(self, eid):
            return {**super().entry(eid), 'props': {'hasscreened': 1}}
    report = SyncExporter(tmp_path, Screened(), Scope('urls', urls=SCOPE.urls[:1])).run()
    assert report['status'] == 'complete' and report['unavailable_comment_entry_ids'] == [1]


@pytest.mark.parametrize('code', [203, 300])
def test_object_access_error_does_not_stop_other_posts(tmp_path, code):
    class Denied(Journal):
        def call(self, method, **params):
            if method == 'getcomments' and params['itemid'] == 1:
                raise APIError(method, code)
            return super().call(method, **params)
    report = SyncExporter(tmp_path, Denied(), SCOPE).run()
    # A refusal is not closed by counting attempts: another account may have access.
    assert report['comments_saved'] == 2 and report['pending_comment_entry_ids'] == [1]
    assert report['unavailable_comment_entry_ids'] == []


def test_calendar_is_followed_by_post_discussion_before_next_day(tmp_path):
    client = Journal()
    assert SyncExporter(tmp_path, client, Scope()).run()['status'] == 'complete'
    methods = [m for m, p in client.calls]
    assert methods.index('getcomments') < methods.index('getevents', methods.index('getevents') + 1)


def test_map_and_preview_read_committed_data_while_writer_keeps_lock(tmp_path):
    writer = Store(tmp_path)
    try:
        with writer.db:
            writer.set('username', 'fixture')
            writer.db.execute('INSERT INTO days VALUES (?,?,?)', ('2005-01-02', 2, 0))
            writer.save_entry({**Journal().entry(1), 'event': '<script>danger()</script><p>Сохранено</p>', 'security': 'private'})
        reader = Store.reader(tmp_path)
        try:
            data = timeline(reader)
            assert data['expected'] == 2 and data['known'] == 1 and data['gaps'][0]['count'] == 1
            with patch('requests.Session.get', side_effect=AssertionError('Read must be offline')):
                html = preview_entry(reader, 1)
            assert 'Сохранено' in html and 'Личная' in html and 'danger()' not in html
            assert preview_entry(reader, 2) is None
        finally:
            reader.close()
    finally:
        writer.close()


def test_repeated_page_is_bounded_and_never_marked_complete(tmp_path):
    class Repeated(Journal):
        def call(self, method, **params):
            result = super().call(method, **params)
            if method == 'getcomments':
                result['comments'][0]['dtalkid'] = 11 * 256 + 7
            return result
    client = Repeated()
    report = SyncExporter(tmp_path, client, Scope('urls', urls=SCOPE.urls[:1])).run()
    # Never marked complete as a discussion: settled as unavailable after the last repeat.
    assert report['status'] == 'complete' and report['unavailable_comment_entry_ids'] == [1]
    assert len([m for m, _ in client.calls if m == 'getcomments']) == 4


def test_live_routes_work_before_archive_has_any_html(local_server):
    import requests
    server, base = local_server
    store = Store(server.jobs.output / 'fixture')
    try:
        with store.db:
            store.set('username', 'fixture')
            store.save_entry(Journal().entry(1))
        server.jobs.state = {'phase': 'comments', 'slug': 'fixture', 'entry_id': 1}
        with requests.Session() as session:
            assert session.get(base + '/api/timeline/fixture').status_code == 403
            session.get(base)
            data = session.get(base + '/api/timeline/fixture').json()
            assert data['known'] == 1 and data['active'] == 1
            result = session.get(base + '/archive/fixture/entries/1.html')
            assert result.status_code == 200 and 'Post 1' in result.text
            assert 'data-library-url="/"' in result.text
            result = session.get(base + '/archive/fixture/index.html')
            assert result.status_code == 200 and 'id="entry-list"' in result.text
            assert 'id="archive-timeline"' not in result.text
            result = session.get(base + '/archive/fixture/timeline.html')
            assert result.status_code == 200 and 'data-source="/api/timeline/fixture"' in result.text
            assert session.get(base + '/archive/fixture/assets/timeline.js').status_code == 200
            assert session.get(base + '/archive/fixture/archive.sqlite3').status_code == 403
    finally:
        store.close()


def test_partial_period_refresh_keeps_other_years_on_offline_timeline(tmp_path):
    SyncExporter(tmp_path, Journal(), Scope()).run()
    SyncExporter(tmp_path, Journal(), Scope('period', '2026-01-01', '2026-12-31')).run()
    reader = Store.reader(tmp_path)
    try:
        assert timeline(reader)['expected'] == 2
        assert timeline(reader)['ready'] == 2
        assert timeline(reader, task=True)['expected'] == 1
    finally:
        reader.close()


def test_interruption_while_expanding_last_page_resumes_that_page(tmp_path):
    class Expand(Journal):
        def __init__(self, interrupt):
            super().__init__()
            self.interrupt = interrupt
        def call(self, method, **params):
            if method == 'getcomments' and params.get('dtalkid') and self.interrupt:
                raise ExportError('connection failed')
            data = super().call(method, **params)
            if method == 'getcomments' and params['page'] == 2 and not params.get('dtalkid'):
                data['comments'][0].pop('body'); data['comments'][0]['is_loaded'] = 0
            if method == 'getcomments' and params.get('dtalkid'):
                data['comments'][0]['dtalkid'] = params['dtalkid']
            return data
    scope = Scope('urls', urls=SCOPE.urls[:1])
    assert SyncExporter(tmp_path, Expand(True), scope).run()['status'] == 'partial'
    client = Expand(False)
    report = SyncExporter(tmp_path, client, scope).run()
    assert report['status'] == 'complete', report
    assert not any(p.get('page') == 3 for m, p in client.calls if m == 'getcomments')


def test_saved_screened_comment_and_missing_images_have_separate_status(tmp_path):
    from ljarchive.render import build_archive
    client = Journal()
    report = SyncExporter(tmp_path, client, Scope('urls', urls=SCOPE.urls[:1])).run()
    store = Store(tmp_path)
    try:
        with store.db:
            store.set('entry_materials', {'1': {'requested': 3, 'saved': 1, 'urls': ['https://example.com/a.png', 'https://example.com/b.png', 'https://example.com/c.png']}})
            store.set('media', {'https://example.com/a.png': 'media/a.png'})
        row = timeline(store)['entries'][0]
        assert row['state'] == 'partial' and row['comments_complete']
        assert row['screened'] == 1 and row['materials']['missing'] == 2
        assert row['notice'] == 'Не загружены изображения: 2'
        assert 'Комментарии получены не полностью' not in preview_entry(store, 1)
        assert row['notice'] in preview_entry(store, 1)
        build_archive(store, report, images=False)
        html = (tmp_path/'entries/1.html').read_text(encoding='utf-8')
        assert row['notice'] in html
        assert 'target="_blank" rel="noreferrer noopener">ЖЖ ↗' in html
    finally:
        store.close()
