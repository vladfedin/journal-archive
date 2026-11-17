import threading

import requests

from ljarchive.live_statistics import LiveStatistics
from ljarchive.storage import Store
from test_render_server import local_server


def seed(folder):
    store = Store(folder)
    with store.db:
        store.set('username', 'fixture')
        store.save_entry({'itemid': 1, 'eventtime': '2012-01-01 00:00:00', 'subject': 'Old'})
        store.save_comment({'id': 1, 'jitemid': 1, 'date': '2012-01-02', 'body': 'A', 'user': 'fixture'})
    return store


def join(cache):
    for value in list(cache.records.values()):
        value['thread'].join(10)
        assert not value['thread'].is_alive()


def test_background_snapshot_is_consistent_and_does_not_lock_writer(tmp_path, monkeypatch):
    store = seed(tmp_path)
    cache = LiveStatistics(interval=0)
    entered, release = threading.Event(), threading.Event()
    from ljarchive.live_statistics import statistics
    calls = []
    def slow(source):
        calls.append(source)
        entered.set()
        assert release.wait(10)
        return statistics(source)
    monkeypatch.setattr('ljarchive.live_statistics.statistics', slow)
    try:
        assert cache.get(tmp_path, active=True)['phase'] == 'updating'
        assert entered.wait(2)
        # Calculation has released its read transaction; new commits proceed immediately.
        with store.db:
            store.save_entry({'itemid': 2, 'eventtime': '2022-01-01', 'subject': 'New'})
            store.save_comment({'id': 2, 'jitemid': 2, 'date': '2022-01-02', 'body': 'B', 'user': 'fixture'})
        assert cache.get(tmp_path, active=True)['phase'] == 'updating' and len(calls) == 1
        release.set(); join(cache)
        old = cache.get(tmp_path, active=True)
        assert old['data']['totals']['posts'] == old['data']['totals']['comments'] == 1
        join(cache)
        new = cache.get(tmp_path, active=True)
        assert new['data']['totals']['posts'] == new['data']['totals']['comments'] == 2
        assert new['data']['years'][-1] == '2022' and new['version'] != old['version']
        assert not (tmp_path / 'statistics.html').exists()
    finally:
        release.set(); join(cache); store.close()


def test_unchanged_polls_reuse_snapshot_and_finish_bypasses_throttle(tmp_path):
    store = seed(tmp_path)
    cache = LiveStatistics(interval=3600)
    cache.get(tmp_path, active=True); join(cache)
    first = cache.get(tmp_path, active=True)
    for _ in range(10):
        assert cache.get(tmp_path, active=True)['version'] == first['version']
    with store.db:
        store.save_entry({'itemid': 2, 'eventtime': '2026-01-01'})
    assert cache.get(tmp_path, active=True)['phase'] == 'ready'
    assert cache.get(tmp_path, active=False)['phase'] == 'updating'
    join(cache)
    assert cache.get(tmp_path)['data']['totals']['posts'] == 2
    store.close()


def test_failed_refresh_keeps_previous_snapshot(tmp_path, monkeypatch):
    store = seed(tmp_path)
    cache = LiveStatistics(interval=3600)
    cache.get(tmp_path, active=True); join(cache)
    first = cache.get(tmp_path, active=True)
    def fail(_):
        raise RuntimeError('example')
    monkeypatch.setattr('ljarchive.live_statistics.statistics', fail)
    cache.get(tmp_path, active=False); join(cache)
    current = cache.get(tmp_path)
    assert current['phase'] == 'error' and current['data'] == first['data']
    store.close()


def test_http_statistics_before_first_render_and_versioned_poll(local_server):
    server, base = local_server
    folder = server.jobs.output / 'fixture'
    store = seed(folder)
    server.jobs.state = {'phase': 'comments', 'slug': 'fixture'}
    try:
        assert requests.get(base + '/api/statistics/fixture').status_code == 403
        with requests.Session() as session:
            session.get(base)
            page = session.get(base + '/archive/fixture/statistics.html')
            assert page.status_code == 200 and 'stats-live' in page.text
            join(server.jobs.statistics_cache)
            state = session.get(base + '/api/statistics/fixture').json()
            assert state['active'] and state['data']['totals']['comments'] == 1
            assert 'discussions' not in state['data']
            assert 'data' not in session.get(base + '/api/statistics/fixture?version=' + state['version']).json()
            details = session.get(base + '/api/statistics/fixture/discussions').json()
            assert details['data']['comments'][0]['excerpt'] == 'A'
            assert session.get(base + '/archive/fixture/data/statistics.json').json()['totals']['posts'] == 1
            assert session.get(base + '/archive/fixture/assets/statistics-live.js').status_code == 200
        assert not (folder / 'statistics.html').exists()
    finally:
        join(server.jobs.statistics_cache); store.close()


def test_large_journal_keeps_all_years_and_reuses_result(tmp_path):
    store = Store(tmp_path)
    with store.db:
        store.set('username', 'fixture')
        store.db.execute("INSERT INTO users VALUES (7,'reader')")
        for eid in range(1, 10001):
            stamp = f'{2000 + (eid - 1) // 500}-01-01'
            store.save_entry({'itemid': eid, 'eventtime': stamp, 'subject': str(eid)})
            for cid in (eid * 2, eid * 2 + 1):
                store.save_comment({'id': cid, 'jitemid': eid, 'posterid': 7, 'date': stamp, 'body': 'Comment'})
    cache = LiveStatistics()
    cache.get(tmp_path, active=True); join(cache)
    result = cache.get(tmp_path, active=True)
    assert result['data']['totals']['posts'] == 10000
    assert result['data']['totals']['comments'] == 20000
    assert result['data']['years'] == list(map(str, range(2000, 2020)))
    assert result['data']['people'][0]['comments'] == 20000
    assert cache.get(tmp_path, active=True)['version'] == result['version']
    store.close()
