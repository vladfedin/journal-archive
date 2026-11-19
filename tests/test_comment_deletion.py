import json
import pytest

from ljarchive.activitystreams import source_snapshot
from ljarchive.client import APIRateLimited, CommentPage, HTTPRateLimited
from ljarchive.comment_state import comment_view
from ljarchive.exporter import normalize_thread
from ljarchive.progress import preview_entry
from ljarchive.storage import Store
from ljarchive.sync import Scope, SyncExporter
from test_sync import Journal

SCOPE = Scope('urls', urls=('https://fixture.livejournal.com/263.html',))


class Deleted(Journal):
    def call(self, method, **params):
        response = super().call(method, **params)
        if method == 'getcomments' and params['page'] == 1:
            c = response['comments'][0]
            c.pop('body', None)
            c.update(state='D', is_loaded=0)
        return response


def test_initial_deleted_is_complete_no_expansion_or_reactions_and_both_views(tmp_path):
    client = Deleted()
    report = SyncExporter(tmp_path, client, SCOPE).run()
    assert report['status'] == 'complete', report
    assert report['deleted_comments'] == 1 and report['missing_comments'] == []
    assert len([p for m, p in client.calls if m == 'getcomments']) == 2
    entities = [e for m, batch in client.calls if m == 'reactions' for e in batch]
    assert not any(e.get('thread') == 11 * 256 + 7 for e in entities)
    data = json.loads((tmp_path / 'data/archive.json').read_text(encoding='utf-8'))
    deleted = data['comments'][0]
    assert deleted['state'] == 'D' and 'body' not in deleted and 'source_status' not in deleted
    assert deleted['deletion']['after_archive'] is False
    page = (tmp_path / 'entries/1.html').read_text(encoding='utf-8')
    assert 'Комментарий удалён.' in page and 'Ранее сохранённая версия' not in page
    reader = Store.reader(tmp_path)
    try:
        assert 'Комментарий удалён.' in preview_entry(reader, 1)
    finally:
        reader.close()
    activity = json.loads((tmp_path / 'data/activitystreams.json').read_text(encoding='utf-8'))
    assert source_snapshot(activity) == data
    tombstone = next(i for i in activity['items'] if i['type'] == 'Tombstone')
    assert tombstone['ja:contentStatus'] == 'deleted'
    assert 'deleted' not in tombstone  # Observation time is not deletion time.


def test_later_deletion_retains_text_and_versions_and_does_not_change_observation_on_retry(tmp_path):
    SyncExporter(tmp_path, Journal(), SCOPE).run()
    report = SyncExporter(tmp_path, Deleted(), SCOPE).run()
    assert report['status'] == 'complete', report
    data = json.loads((tmp_path / 'data/archive.json').read_text(encoding='utf-8'))
    c = data['comments'][0]
    assert c['body'] == 'Comment 11 revision 0'
    assert c['state'] == 'D' and c['deletion']['after_archive']
    assert any(v['id'] == 11 and v['record']['state'] == 'S' for v in data['versions'])
    assert 'Удалён впоследствии' in (tmp_path / 'entries/1.html').read_text(encoding='utf-8')
    SyncExporter(tmp_path, Deleted(), SCOPE).run()
    updated = json.loads((tmp_path / 'data/archive.json').read_text(encoding='utf-8'))
    assert updated['comments'][0] == c
    activity = json.loads((tmp_path / 'data/activitystreams.json').read_text(encoding='utf-8'))
    assert source_snapshot(activity) == updated
    tombstone = next(i for i in activity['items'] if i['type'] == 'Tombstone')
    assert tombstone['ja:deletedAfterArchive'] is True
    assert tombstone['content'] == c['body']


def test_hidden_missing_denied_and_deleted_are_not_conflated(tmp_path):
    client = Journal(no_body=True)
    report = SyncExporter(tmp_path, client, SCOPE).run()
    # Asked for three times and not given: settled as unavailable, never as deleted.
    assert report['status'] == 'complete' and report['deleted_comments'] == 0
    assert report['unavailable_comment_entry_ids'] == [1] and report['missing_comments']
    page = (tmp_path / 'entries/1.html').read_text(encoding='utf-8')
    assert 'ЖЖ не отдал текст этого комментария.' in page and 'Ранее сохранённая версия' not in page
    assert 'удалён' not in page.split('ЖЖ не отдал текст')[0][-200:].lower()
    assert comment_view({'state': 'S'})['state_label'] == 'Скрыт'
    assert not comment_view({'state': 'A', 'source_status': 'not_returned'})['deleted']


def test_metadata_confirms_deletion_when_thread_omits_it(tmp_path):
    class Confirmed(Journal):
        def __init__(self):
            super().__init__(no_body=True)
            self.windows = []
        def comments(self, kind, start):
            self.windows.append((kind, start))
            assert kind == 'comment_meta'
            return CommentPage([{'id': 11, 'state': 'D'}, {'id': 12, 'state': 'D'}], {}, 12, 13), b'<fixture/>'
    client = Confirmed()
    report = SyncExporter(tmp_path, client, SCOPE).run()
    assert report['status'] == 'complete', report
    assert client.windows == [('comment_meta', 11)]
    assert report['deleted_comments'] == 2
    assert len([p for m, p in client.calls if m == 'getcomments']) == 4


def test_metadata_absence_is_not_deletion(tmp_path):
    class Absent(Journal):
        def comments(self, kind, start):
            return CommentPage([], {}, 12, 13), b'<fixture/>'
    report = SyncExporter(tmp_path, Absent(no_body=True), SCOPE).run()
    assert report['deleted_comments'] == 0 and report['unavailable_comment_entry_ids'] == [1]


def test_deleted_loaded_flag_does_not_invent_empty_body():
    items, _ = normalize_thread([{'dtalkid': 256, 'state': 'D', 'is_loaded': 1}], 1)
    assert 'body' not in items[0]


def test_anonymous_tombstone_preserves_zero_ids_and_loaded_flag(tmp_path):
    from ljarchive.comment_state import merge_comment
    item = {'id': 11, 'jitemid': 1, 'parentid': 0, 'posterid': 0, 'state': 'D', 'is_loaded': 0}
    merged = merge_comment(None, item)
    assert merged['posterid'] == 0 and merged['parentid'] == 0 and merged['is_loaded'] == 0
    previous = {**item, 'state': 'A', 'is_loaded': 1, 'posterid': 44, 'body': 'Saved', 'userpic': 'https://example.com/avatar.jpg'}
    merged = merge_comment(previous, item)
    assert merged['posterid'] == 44 and merged['is_loaded'] == 0
    assert merged['body'] == 'Saved' and merged['deletion']['after_archive']


def web_page(ditemid, comments, journal='fixture'):
    page = json.dumps({'comments': comments})
    entry = json.dumps({'ditemid': ditemid, 'journal': journal})
    return f'<script>\nSite.page = {page};\nSite.entry = {entry};\n</script>'.encode()


def test_web_marker_is_bound_to_journal_entry_and_comment():
    from ljarchive.comment_page import deleted_threads
    nodes = [
        {'talkid': 11, 'dtalkid': 2823, 'deleted': 1, 'leafclass': 'deleted'},
        {'talkid': 12, 'dtalkid': 3079, 'deleted': 0, 'leafclass': 'collapsed'},
        {'talkid': 13, 'dtalkid': 3335, 'deleted_poster': 1, 'body': 'Комментарий удалён'},
        {'talkid': 14, 'dtalkid': 3592, 'deleted': 1},  # different entry suffix
        {'talkid': 16, 'dtalkid': 3847, 'deleted': 1},  # inconsistent talkid
    ]
    page = web_page(263, nodes)
    assert deleted_threads(page, 'fixture', 263) == {2823}
    assert not deleted_threads(page, 'other', 263)
    assert not deleted_threads(page, 'fixture', 519)
    assert not deleted_threads(page.replace(b'<script>', b'<p>'), 'fixture', 263)


class WebDeleted(Journal):
    def __init__(self):
        super().__init__(no_body=True)
        self.checked = []

    def web_deleted_threads(self, ditemid, dtalkid):
        from ljarchive.comment_page import deleted_threads
        self.checked.append((ditemid, dtalkid))
        nodes = [{'talkid': cid, 'dtalkid': cid * 256 + 7, 'deleted': 1} for cid in (11, 12)]
        return deleted_threads(web_page(ditemid, nodes), self.user, ditemid)


@pytest.mark.parametrize('limit', [HTTPRateLimited, APIRateLimited])
def test_web_deletion_check_stops_on_rate_limit(tmp_path, limit):
    class Limited(WebDeleted):
        def web_deleted_threads(self, ditemid, dtalkid):
            self.checked.append(dtalkid)
            raise limit('rate limit fixture')

    client = Limited()
    report = SyncExporter(tmp_path, client, SCOPE).run()
    assert client.checked == [2823]
    assert 'rate limit fixture' in report['issues']
    assert not report['sync'].get('done')
    assert not any(method == 'reactions' for method, _ in client.calls)


def test_web_confirmation_closes_gaps_in_json_html_and_activitystreams(tmp_path):
    client = WebDeleted()
    report = SyncExporter(tmp_path, client, SCOPE).run()
    assert report['status'] == 'complete' and report['deleted_comments'] == 2
    assert client.checked == [(263, 2823)]
    assert [p['dtalkid'] for m, p in client.calls if m == 'getcomments' and 'dtalkid' in p] == [2823, 3079]
    data = json.loads((tmp_path / 'data/archive.json').read_text())
    assert all(c['deletion']['evidence'] == 'web:Site.page.comments.deleted:D'
               and not c['deletion']['after_archive'] for c in data['comments'])
    page = (tmp_path / 'entries/1.html').read_text()
    assert page.count('Комментарий удалён.') == 2 and 'ЖЖ не отдал текст' not in page
    activity = json.loads((tmp_path / 'data/activitystreams.json').read_text())
    assert sum(i['type'] == 'Tombstone' for i in activity['items']) == 2


def test_web_deletion_retains_earlier_text_across_unloaded_api_updates(tmp_path):
    SyncExporter(tmp_path, Journal(), SCOPE).run()
    SyncExporter(tmp_path, WebDeleted(), SCOPE).run()
    path = tmp_path / 'data/archive.json'
    before = json.loads(path.read_text())['comments']
    assert all(c['deletion']['after_archive'] and c['body'] for c in before)
    SyncExporter(tmp_path, Journal(no_body=True), SCOPE).run()
    assert json.loads(path.read_text())['comments'] == before


def test_web_access_denied_can_still_confirm_deletion_via_metadata(tmp_path):
    from ljarchive.client import AuthenticationRequired

    class Restricted(Journal):
        def web_deleted_threads(self, ditemid, dtalkid):
            raise AuthenticationRequired('HTTP 403', status=403)

        def comments(self, kind, start):
            return CommentPage([{'id': 11, 'state': 'D'}], {}, 12, 13), b'<fixture/>'

    report = SyncExporter(tmp_path, Restricted(no_body=True), SCOPE).run()
    assert report['deleted_comments'] == 1 and report['unavailable_comment_entry_ids'] == [1]
    records = json.loads((tmp_path / 'data/archive.json').read_text())['comments']
    assert records[0]['deletion']['evidence'] == 'comment_meta:D'
    assert 'deletion' not in records[1]
