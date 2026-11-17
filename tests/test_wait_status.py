import json
import threading

import requests

from ljarchive.client import LiveJournalClient, RequestBudget
from ljarchive.media import Media
from ljarchive.server import Jobs
from test_limited_export import Response


def test_wait_accounts_actual_time_and_request_clears_pause(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('ljarchive.server.time.monotonic', lambda: clock[0])
    jobs = Jobs(tmp_path)
    jobs.progress(phase='comments')
    clock[0] += 10
    jobs.progress(phase='waiting', retry_at=500, wait={'reason': 'timeout'})
    clock[0] += 60
    state = jobs.snapshot()
    assert state['working_seconds'] == 10 and state['waiting_seconds'] == 60
    budget = RequestBudget(None, progress=jobs.progress)
    budget.event('request', 'images', 'https://user:secret@example.org/private?token=secret', attempt=2, max_attempts=6)
    state = jobs.snapshot()
    assert state['phase'] == 'loading' and 'retry_at' not in state and 'wait' not in state
    assert 'secret' not in json.dumps(state) and 'private' not in json.dumps(state)
    budget.event('success', 'images')
    assert jobs.snapshot()['last_success_at']
    clock[0] += 5
    jobs.progress(phase='partial')
    clock[0] += 1000
    assert jobs.snapshot()['working_seconds'] == 15
    assert jobs.snapshot()['waiting_seconds'] == 60


def test_image_timeout_is_not_reported_as_server_rate_limit(tmp_path, monkeypatch):
    updates = []
    budget = RequestBudget(10, progress=lambda **v: updates.append(v))
    media = Media(tmp_path, True, threading.Event(), budget)
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(media, 'wait', lambda _: True)
    def get(*a, **kw):
        raise requests.Timeout()
    monkeypatch.setattr(media.session, 'get', get)
    assert media.get('https://example.org/private.png?secret=x') is None
    waits = [v for v in updates if v.get('phase') == 'waiting']
    assert not waits  # Retry happens in the final pass, with no invented server deadline.
    failures = [v['activity'] for v in updates if v.get('activity', {}).get('kind') == 'failure']
    assert len(failures) == 1 and failures[0]['reason'] == 'timeout'
    assert 'secret' not in json.dumps(updates)
    media.close()


def test_restored_cooldown_is_visible_before_first_request(monkeypatch):
    updates, waits = [], []
    budget = RequestBudget(10, progress=lambda **v: updates.append(v))
    client = LiveJournalClient('fixture', 'secret', budget=budget, progress=budget.progress)
    import time
    budget.not_before = client.not_before = time.time() + 7200
    budget.wait_info = {'reason': 'http', 'http_status': 429, 'server_delay': True, 'attempt': 3, 'max_attempts': 6}
    monkeypatch.setattr(client, '_wait', waits.append)
    monkeypatch.setattr(client.session, 'request', lambda *a, **k: Response(content=b'ok'))
    assert client._request('GET', '/test') == b'ok'
    assert updates[0]['phase'] == 'waiting' and updates[0]['wait']['http_status'] == 429
    assert max(waits) > 7190
    assert updates[-1]['activity']['kind'] == 'success'
    client.close()


def test_recent_events_are_bounded_and_checkpoint_survives_restart(tmp_path):
    jobs = Jobs(tmp_path)
    jobs.current = {'archive': 'fixture', 'username': 'fixture'}
    jobs.progress(phase='comments')
    budget = RequestBudget(None, progress=jobs.progress)
    for _ in range(100):
        budget.event('success', 'livejournal')
    jobs.progress(phase='waiting', retry_at=9999999999, wait={'reason': 'api_limit'})
    assert len(jobs.snapshot()['events']) == 30
    restarted = Jobs(tmp_path).snapshot()
    assert restarted['phase'] == 'cancelled' and restarted['worker_alive'] is False
    assert restarted['last_success_at']
