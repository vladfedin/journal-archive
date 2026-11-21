import json
import threading
import time

import pytest
import requests

from ljarchive.client import IncompleteData, LiveJournalClient
from test_limited_export import Response
from test_render_server import local_server
from test_sources import CommunityWire


def test_lookup_progress_while_journal_is_silent(local_server, monkeypatch):
    server, base = local_server
    wire = CommunityWire()
    waiting, release = threading.Event(), threading.Event()
    real_init = LiveJournalClient.__init__
    attempts = []
    def init(client, *args, **kwargs):
        real_init(client, *args, **kwargs)
        def send(method, url, **options):
            if url.endswith('/manage.bml'):
                attempts.append(url)
                if len(attempts) == 1:
                    raise requests.Timeout()
                waiting.set()
                assert release.wait(5)
            return wire.request(method, url, **options)
        client.session.request = send
    monkeypatch.setattr(LiveJournalClient, '__init__', init)
    monkeypatch.setattr(LiveJournalClient, '_wait', lambda self, seconds: self.check())
    with requests.Session() as session:
        session.get(base).raise_for_status()
        response = session.post(base+'/api/communities', headers={'Origin':base},
            json={'username':'manager','password':'test-password','background':True})
        assert response.status_code == 202
        url = base+'/api/communities/'+response.json()['id']
        try:
            assert waiting.wait(3)
            assert requests.get(url).status_code == 403
            pending = session.get(url).json()
            assert pending['events'][-1]['kind'] == 'request'
            assert pending['events'][-1]['method'] == 'GET'
            assert pending['events'][-1]['request_url'].endswith('/community/manage.bml')
            assert pending['events'][-1]['attempt'] == 2
            assert any(e.get('reason') == 'timeout' and e['kind']=='wait' for e in pending['events'])
        finally:
            release.set()
        for _ in range(40):
            result = session.get(url).json()
            if result['phase'] == 'complete':
                break
            time.sleep(.025)
        assert result['communities'] == [wire.journal]
        assert result['events'][-1]['http_status'] == 200 and result['events'][-1]['bytes'] > 0
        for secret in ('test-password', 'test-session', 'test-challenge', 'Cookie', 'auth_response'):
            assert secret not in json.dumps(result)
        assert not list(server.jobs.tasks.glob('*.json'))


def test_guest_form_then_managed_page(monkeypatch):
    client = LiveJournalClient('manager', 'test-password')
    client._cookie = 'api-session'
    form = b'<form action="/login.bml"><input name="lj_form_auth" value="form-token"></form>'
    calls = []
    def send(method, url, **options):
        calls.append((method, url.split('.com')[-1]))
        if len(calls) <= 2:
            return Response(content=form)
        if method == 'POST':
            assert options['data']['lj_form_auth'] == 'form-token'
            for name in ('ljsession','ljloggedin'):
                client.session.cookies.set(name, 'fixture', domain='www.livejournal.com', secure=True)
            return Response(content=b'')
        assert 'Cookie' not in options.get('headers', {})
        return Response(content=b'<a href="/community/settings.bml?authas=bookclub">Settings</a>')
    monkeypatch.setattr(client.session, 'request', send)
    monkeypatch.setattr(client, '_wait', lambda _: None)
    try:
        assert client.managed_communities(confirm_empty=True) == ['bookclub']
        assert calls == [('GET','/community/manage.bml'),('GET','/login.bml'),
                         ('POST','/login.bml'),('GET','/community/manage.bml')]
        monkeypatch.setattr(client.session, 'request', lambda *a,**k:Response(content=b'<p>Unexpected page</p>'))
        with pytest.raises(IncompleteData):
            client.managed_communities(confirm_empty=True)
    finally:
        client.close()


def test_slow_response_lookup_expires_and_ignores_late_success(monkeypatch):
    from ljarchive import communities
    hold = threading.Event()
    monkeypatch.setattr(communities, 'LOOKUP_TIMEOUT', .1)
    def blocked(self, **kwargs):
        hold.wait(4)
        return ['late']
    monkeypatch.setattr(LiveJournalClient, 'managed_communities', blocked)
    tasks = communities.CommunityLookups()
    key = tasks.start('manager', 'fixture')['id']
    try:
        for _ in range(60):
            result = tasks.status(key)
            if result['phase'] == 'error':
                break
            time.sleep(.025)
        assert 'Истекло время' in result['message']
        assert tasks.workers == 1
    finally:
        hold.set()
    for _ in range(60):
        if not tasks.workers:
            break
        time.sleep(.025)
    assert tasks.workers == 0 and tasks.status(key)['phase'] == 'error'
