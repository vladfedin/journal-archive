import json

from ljarchive.progress import preview_entry, timeline
from ljarchive.storage import Store
from ljarchive.sync import Scope, SyncExporter
from test_sync import Journal


class Reposts(Journal):
    def entry(self, eid):
        entry = super().entry(eid)
        if eid == 1:
            entry.update(repost=1, reply_count=300, original_entry_url='https://original.livejournal.com/263.html', props={'hasscreened': 1})
        return entry
    def call(self, method, **params):
        assert method != 'getcomments' or params['itemid'] != 1, 'Original discussion must not be fetched'
        return super().call(method, **params)


def test_repost_comments_are_out_of_scope_and_not_missing(tmp_path):
    client = Reposts()
    report = SyncExporter(tmp_path, client, Scope()).run()
    assert report['status'] == 'complete', report
    assert report['comments_saved'] == 2 and report['pending_comment_entry_ids'] == []
    data = json.loads((tmp_path/'data/archive.json').read_text(encoding='utf-8'))
    assert data['entries'][0]['reply_count'] == 300
    assert data['coverage']['discussions']['1']['skipped'] == 'repost'
    page = (tmp_path/'entries/1.html').read_text(encoding='utf-8')
    assert 'Перепост' in page and 'original.livejournal.com' in page
    assert 'id="comments"' not in page and 'Нет комментариев' not in page
    reader = Store.reader(tmp_path)
    try:
        row = timeline(reader)['entries'][0]
        assert row['repost'] and row['state'] == 'ready' and row['expected_comments'] == 0
        assert 'id="comments"' not in preview_entry(reader, 1)
    finally:
        reader.close()
    stats = json.loads((tmp_path/'data/statistics.json').read_text(encoding='utf-8'))
    assert stats['totals']['reposts'] == 1 and stats['totals']['comments'] == 2


def test_link_in_normal_post_is_not_a_repost(tmp_path):
    from ljarchive.entries import is_repost, repost_url
    assert not is_repost({'event': '<a href="https://other.livejournal.com/1.html">Оригинал</a>'})
    assert not is_repost({'repost': '0'})
    assert repost_url({'repost': 1, 'original_entry_url': 'javascript:alert(1)'}) is None


def test_recent_export_does_not_count_original_reply_count_as_a_gap(tmp_path):
    from ljarchive.exporter import Exporter
    class Recent(Reposts):
        def call(self, method, **params):
            if method == 'getevents' and params['selecttype'] == 'lastn':
                return {'events': [self.entry(1)]}
            return super().call(method, **params)
    report = Exporter(tmp_path, Recent(), entry_limit=1, images=False).run()
    assert report['status'] == 'complete', report
    assert report['comments_saved'] == 0 and report['pending_comment_entry_ids'] == []
