import json
from collections import Counter

from ljarchive.sync import Scope, SyncExporter
from test_sync import Journal


SCOPE = Scope('urls', urls=('https://fixture.livejournal.com/263.html', 'https://fixture.livejournal.com/519.html'))


class PartialJournal(Journal):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.seen = Counter()
    def call(self, method, **params):
        response = super().call(method, **params)
        if method == 'getcomments':
            key = (params['itemid'], params['page'])
            self.seen[key] += 1
            if key == (1, 1) and self.seen[key] <= 2:
                response['comments'][0].pop('body')
                response['comments'][0]['is_loaded'] = 0
        return response


def test_partial_comment_pass_retries_only_incomplete_discussion(tmp_path):
    client = PartialJournal()
    report = SyncExporter(tmp_path, client, SCOPE).run()
    assert report['status'] == 'complete', report
    assert client.seen[(2, 1)] == client.seen[(2, 2)] == 1
    assert client.seen[(1, 1)] == 3
    assert all(p.get('extra') == 1 for m, p in client.calls if m == 'getcomments')
    data = json.loads((tmp_path / 'data/archive.json').read_text(encoding="utf-8"))
    assert all('body' in c for c in data['comments'])


def test_empty_post_response_is_retried_and_recovered_without_new_login(tmp_path):
    class BrieflyAbsent(Journal):
        def call(self, method, **params):
            response = super().call(method, **params)
            if method == 'getevents' and len([1 for m, _ in self.calls if m == 'getevents']) == 1:
                response['events'] = []
            return response
    client = BrieflyAbsent()
    report = SyncExporter(tmp_path, client, Scope('urls', urls=SCOPE.urls[:1])).run()
    assert report['status'] == 'complete', report
    assert report['entries_saved'] == 1 and report['comments_saved'] == 2
    assert report['sync']['unavailable'] == []


def test_incomplete_reactions_repeat_only_missing_targets(tmp_path):
    class PartialReactions(Journal):
        def reaction_counts(self, entities):
            data = super().reaction_counts(entities)
            if len([1 for m, _ in self.calls if m == 'reactions']) == 1:
                return data[1:]
            return data
    client = PartialReactions()
    report = SyncExporter(tmp_path, client, Scope('urls', urls=SCOPE.urls[:1])).run()
    assert report['status'] == 'complete', report
    calls = [data for method, data in client.calls if method == 'reactions']
    assert len(calls) == 2 and len(calls[0]) == 3 and len(calls[1]) == 1


def test_persistently_missing_comments_stop_after_finite_passes(tmp_path):
    client = Journal(no_body=True)
    report = SyncExporter(tmp_path, client, Scope('urls', urls=SCOPE.urls[:1])).run()
    assert report['status'] == 'partial'
    pages = [p for m, p in client.calls if m == 'getcomments' and p['page'] == 2]
    assert len(pages) == 3
    state = report['sync']
    assert not state['done'] and state['phase'] == 'comments'


def test_missing_avatar_resume_does_not_repeat_posts_comments_or_reactions(tmp_path, monkeypatch):
    import requests
    from ljarchive.media import Media
    from test_limited_export import Response
    class WithAvatar(Journal):
        userpics = {'defaultpicurl': 'https://example.org/owner.png'}
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda self, seconds: True)
    requests_made = []
    def unavailable(self, url, **kw):
        requests_made.append(url)
        raise requests.HTTPError('404')
    monkeypatch.setattr('requests.Session.get', unavailable)
    scope = Scope('urls', urls=SCOPE.urls[:1])
    first = SyncExporter(tmp_path, WithAvatar(), scope).run()
    assert first['status'] == 'partial' and not first['sync']['done']
    assert first['sync']['phase'] == 'render'
    assert first['userpics']['missing'] == 1 and len(requests_made) == 1
    requests_made.clear()
    monkeypatch.setattr('requests.Session.get', lambda self, url, **kw: requests_made.append(url) or Response(content=b'pic', headers={'Content-Type': 'image/png'}))
    client = WithAvatar()
    second = SyncExporter(tmp_path, client, scope).run()
    assert second['status'] == 'complete', second
    assert second['userpics']['saved'] == 1 and len(requests_made) == 1
    assert client.calls == []


def test_old_comment_metadata_is_enriched_without_reloading_posts(tmp_path, monkeypatch):
    from ljarchive.storage import Store
    from ljarchive.media import Media
    from test_limited_export import Response
    scope = Scope('urls', urls=SCOPE.urls[:1])
    SyncExporter(tmp_path, Journal(), scope).run()
    store = Store(tmp_path)
    try:
        with store.db:
            for comment in store.comments():
                comment.pop('userpic', None); store.save_comment(comment)
            store.set('userpic_comments_requested', [])
            state = store.get('sync_job'); state.update(phase='render', done=False); store.set('sync_job', state)
    finally:
        store.close()
    class Enriched(Journal):
        def call(self, method, **params):
            result = super().call(method, **params)
            if method == 'getcomments':
                assert params['extra'] == 1
                result['comments'][0]['userpic'] = 'https://example.org/comment.png'
            return result
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda self, seconds: True)
    monkeypatch.setattr('requests.Session.get', lambda *a, **kw: Response(content=b'pic', headers={'Content-Type': 'image/png'}))
    client = Enriched()
    report = SyncExporter(tmp_path, client, scope).run()
    assert report['status'] == 'complete', report
    assert all(method == 'getcomments' for method, _ in client.calls)
    assert report['userpics']['saved'] == 2
