"""Finite retrieval: one collection and two repeats, counted across runs; then settled."""
import threading

import requests

from ljarchive.gaps import ATTEMPTS, closed
from ljarchive.media import Media
from ljarchive.recovery import inventory, summary
from ljarchive.storage import Store
from ljarchive.sync import Scope, SyncExporter
from test_sync import Journal

ONE = Scope('urls', urls=('https://fixture.livejournal.com/263.html',))


def test_stop_and_request_limit_are_not_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda self, seconds: True)
    monkeypatch.setattr('requests.Session.get', lambda *a, **k: (_ for _ in ()).throw(requests.HTTPError('403')))
    media = Media(tmp_path, True, threading.Event())
    url = 'https://example.org/a.png'
    try:
        media.get(url)
        assert media.errors[url]['attempts'] == 1 and not closed(media.errors[url])
        media.record_error(url, 'cancelled', status='pending')
        assert media.errors[url]['attempts'] == 1
        media.retry_failed()
        assert media.errors[url]['attempts'] == 1, 'a pending record is not a failed answer'
        media.errors[url]['status'] = 'failed'
        media.retry_failed()
        assert media.errors[url]['attempts'] == ATTEMPTS and closed(media.errors[url])
        calls = []
        monkeypatch.setattr('requests.Session.get', lambda *a, **k: calls.append(1))
        media.attempted.clear(); media.mapping.clear()
        assert media.get(url) is None and calls == []
    finally:
        media.close()


def test_entry_not_returned_is_settled_and_counted_across_runs(tmp_path):
    report = SyncExporter(tmp_path, Journal(removed=True), ONE).run()
    assert report['status'] == 'complete' and report['unavailable_entry_ids'] == [1]
    assert report['sync']['unavailable_attempts'] == {'1': ATTEMPTS}
    store = Store(tmp_path)
    try:
        row = next(r for r in summary(inventory(store))['rows'] if r['key'] == 'posts')
        assert (row['remaining'], row['unavailable']) == (0, 1)
        assert row['reasons'][0]['final'] and 'удалена или скрыта' in row['reasons'][0]['message']
    finally:
        store.close()


def test_partial_reaction_authors_are_settled_and_keep_attempts_with_fresh_counters(tmp_path):
    class Hidden(Journal):
        def reaction_counts(self, entities):
            self.calls.append(('reactions', entities))
            return [{**e, 'thread': e.get('thread', 0), 'count': 1, 'groups': [1]} for e in entities]
        def reaction_authors(self, entities):
            self.calls.append(('reaction_authors', entities))
            return [{'users': []} for _ in entities]
    client = Hidden()
    report = SyncExporter(tmp_path, client, ONE).run()
    assert report['status'] == 'complete', report['issues']
    assert report['reactions']['authors_unavailable'] == report['reactions']['posts_total'] + report['reactions']['comments_total']
    asked = len([1 for m, _ in client.calls if m == 'reaction_authors'])
    store = Store(tmp_path)
    try:
        authors = store.reactions('post')[1]['authors']
        assert authors['attempts'] == ATTEMPTS and authors['closed']
        row = next(r for r in summary(inventory(store))['rows'] if r['key'] == 'authors')
        assert row['remaining'] == 0 and row['reasons'][0] == {'message': 'ЖЖ показал не всех', 'final': True, 'count': row['unavailable']}
        state = store.get('sync_job'); state.update(phase='reactions', done=False)
        with store.db:
            store.set('sync_job', state)
    finally:
        store.close()
    again = Hidden()
    SyncExporter(tmp_path, again, ONE).run()
    assert not any(m == 'reaction_authors' for m, _ in again.calls)
    assert asked
