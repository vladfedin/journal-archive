import json
import threading

from ljarchive.client import LiveJournalClient, RequestBudget
from ljarchive.media import Media
from ljarchive.reactions import capture_reactions, parse_counts, reaction_report
from ljarchive.presentation import reaction_view
from ljarchive.render import needs_autoformat, safe_content
from ljarchive.storage import Store


def test_rich_html_source_indentation_does_not_become_visible_blank_lines(tmp_path):
    source = '<p>Первый абзац</p>\n\n   <div>\n      <figcaption>Подпись</figcaption>\n   </div>\n\n<p>Второй</p><p>&nbsp;<br></p>'
    assert not needs_autoformat(source)
    assert needs_autoformat('Две строки\nи ещё строка')
    assert not needs_autoformat('Строки\nбез переноса', {"opt_preformatted": "1"})
    with_media = Media(tmp_path, False, threading.Event())
    try:
        result = str(safe_content(source, 'https://example.livejournal.com/263.html', {}, with_media, set()))
        assert '<p>&nbsp;' not in result
        code = '<pre><code>a\n\n\n    indented</code></pre>'
        assert code == str(safe_content(code, '', {}, with_media, set()))
        lj = '<lj displayUsername="Имя &amp; имя" username="test_user" user="test_user" userheadURL="https://example.org/1"/>'
        result = str(safe_content(lj, '', {}, with_media, set()))
        assert 'href="https://test-user.livejournal.com/"' in result
        assert 'Имя &amp; имя</a>' in result
    finally:
        with_media.close()


def test_real_service_shape_matches_full_ids_and_preserves_zero_unknown_types():
    # Same response shape as the public LJ service; IDs and counts are synthetic.
    rows = [
        {"journal": 123, "item": 263, "thread": 0, "count": 12, "groups": [12] + [0]*24},
        {"journal": 123, "item": 263, "thread": 519, "count": 0, "groups": [0]*25},
        {"journal": 123, "item": 263, "thread": 775, "count": 2, "groups": [0]*24 + [2]},
        {"journal": 999, "item": 263, "thread": 0, "count": 7, "groups": [7]},
        {"journal": 123, "item": 263, "thread": 1031, "count": 2, "groups": [0]},
    ]
    counts = parse_counts(rows, {(123, 263, thread) for thread in (0, 519, 775, 1031, 1287)})
    assert len(counts) == 3
    assert counts[(123, 263, 0)]["count"] == 12
    assert reaction_view(counts[(123, 263, 519)])["badges"] == []
    assert reaction_view(counts[(123, 263, 775)])["badges"][0]["label"] == "Реакция #24"
    assert (123, 263, 1031) not in counts and (123, 263, 1287) not in counts
    assert reaction_view(None) is None
    assert parse_counts([{"journal": [], "item": 263, "count": 0, "groups": []}], set()) == {}


def test_reaction_batches_stop_on_shared_limit_and_keep_first_batch(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = LiveJournalClient('fixture', 'not-sent', budget=RequestBudget(1))
    client.journal_id = 123
    monkeypatch.setattr(client, '_wait', lambda _: client.check())
    calls = []
    class Response:
        status_code, headers = 200, {}
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def iter_content(self, *_):
            yield json.dumps([{**e, "thread": e.get("thread", 0), "count": 0, "groups": [0]} for e in calls[-1]]).encode()
    def request(method, url, **kwargs):
        assert url == 'https://likes.services.livejournal.com/get'
        assert not kwargs['allow_redirects']
        assert 'not-sent' not in str(kwargs)
        calls.append(json.loads(kwargs['data'])['items'])
        return Response()
    monkeypatch.setattr(client.session, 'request', request)
    try:
        with store.db:
            store.save_entry({"itemid": 1, "anum": 7})
            for cid in range(1, 102):
                store.save_comment({"id": cid, "jitemid": 1, "state": 'S'})
        capture_reactions(store, client)
        assert client.budget.used == 1 and client.budget.exhausted
        assert len(calls) == 1 and len(calls[0]) == 50
        assert calls[0][1]['thread'] == 263  # Full-mode comments reconstruct display ID.
        report = reaction_report(store)
        assert report['posts_saved'] == 1 and report['comments_saved'] == 49
        assert report['comments_total'] == 101 and not report['complete']
        assert store.comments()[48]['reactions']['count'] == 0
        assert 'reactions' not in store.comments()[49]
    finally:
        client.close()
        store.close()
