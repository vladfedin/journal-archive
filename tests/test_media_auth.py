import json
import threading

import pytest
import requests

from ljarchive.client import ExportError, LiveJournalClient, RequestBudget
from ljarchive.media import Media
from ljarchive.on_demand import NeedsLogin, download_one
from ljarchive.sessions import Sessions
from ljarchive.storage import Store
from test_media_errors import ImageResponse
from test_on_demand import fixture, wait
from test_render_server import local_server

PICTURE = 'https://ic.pics.livejournal.com/fixture/1/2/2_800.png'
SESSION = 'unit-test-session'


def authenticated(client):
    client._cookie = SESSION
    client.source['access']['status'] = 'verified'
    client.journal_id = client.source['id'] = 123
    if client.session_ready:
        client.session_ready(client)


def network(monkeypatch, responder):
    calls = []
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda self, _: not self.cancel.is_set())
    monkeypatch.setattr(LiveJournalClient, 'ensure_web_session', lambda self: False)
    def get(self, url, **kwargs):
        calls.append((url, kwargs['headers']))
        return responder(url, kwargs['headers'])
    monkeypatch.setattr(requests.Session, 'get', get)
    return calls


def image(status=200):
    return ImageResponse(status=status, content=b'private-test-image', headers={'Content-Type': 'image/png'})


def private_fixture(folder):
    fixture(folder)
    store = Store(folder)
    with store.db:
        entry = store.entries()[0]
        entry['event'] = f'<img src="{PICTURE}">'
        entry['security'] = 'private'
        store.save_entry(entry)
    store.close()


def test_authenticated_image_uses_post_referer_and_preserves_cookie_scope(tmp_path, monkeypatch):
    client = LiveJournalClient('fixture', '')
    authenticated(client)
    client.session.cookies.set('ljloggedin', 'signed-by-test-server', domain='.livejournal.com', secure=True)
    client.session.cookies.set('ljmastersession', 'www-only', domain='www.livejournal.com', secure=True)
    client.session.cookies.set('ljloggedin', 'wrong-domain', domain='unrelated.example', secure=True)
    client.session.cookies.set('unrelated', 'tracking', domain='.livejournal.com')
    calls = network(monkeypatch, lambda url, headers: image(200 if SESSION in headers.get('Cookie', '') else 403))
    media = Media(tmp_path, True, threading.Event(), client=client)
    try:
        path = media.get(PICTURE, referer='https://fixture.livejournal.com/257.html')
        assert (tmp_path / path).read_bytes() == b'private-test-image'
        assert calls[0][1]['Referer'] == 'https://fixture.livejournal.com/257.html'
        assert calls[0][1]['Cookie'] == 'ljloggedin=signed-by-test-server; ljsession=' + SESSION
        assert media.session.trust_env is False
    finally:
        media.close(); client.close()


@pytest.mark.parametrize('target', [
    'https://images.example/private.png', 'http://images.example/private.png',
    'https://ic.pics.livejournal.com.evil.example/private.png',
    'https://unrelated.livejournal.com/private.png',
])
def test_redirect_drops_authorization_and_does_not_leak_cookie_jar(tmp_path, monkeypatch, target):
    client = LiveJournalClient('fixture', ''); authenticated(client)
    media = Media(tmp_path, True, threading.Event(), client=client)
    def respond(url, headers):
        if url == PICTURE:
            media.session.cookies.set('ljsession', 'injected', domain='.example')
            return ImageResponse(status=302, headers={'Location': target})
        assert not list(media.session.cookies)
        return image()
    calls = network(monkeypatch, respond)
    try:
        assert media.get(PICTURE)
        assert 'Cookie' in calls[0][1]
        assert calls[1] == (target, {})
    finally:
        media.close(); client.close()


def test_legacy_http_images_are_upgraded_before_sending_session(tmp_path, monkeypatch):
    client = LiveJournalClient('fixture', ''); authenticated(client)
    calls = network(monkeypatch, lambda *a: image())
    media = Media(tmp_path, True, threading.Event(), client=client)
    try:
        old = PICTURE.replace('https:', 'http:')
        assert media.get(old)
        assert calls == [(PICTURE, {'Referer': 'https://fixture.livejournal.com/', 'Cookie': 'ljsession=' + SESSION})]
        assert old in media.mapping  # Source JSON keeps the original URL.
        assert not media.headers(old)
    finally:
        media.close(); client.close()


def test_click_requests_login_then_downloads_and_reuses_session_without_persisting_it(tmp_path, monkeypatch):
    private_fixture(tmp_path)
    sessions = Sessions()
    monkeypatch.setattr(LiveJournalClient, 'login', authenticated)
    calls = network(monkeypatch, lambda url, headers: image(200 if SESSION in headers.get('Cookie', '') else 403))
    store = Store(tmp_path)
    updates = []
    request = {'entry_id': 1, 'kind': 'image', 'value': PICTURE}
    try:
        with pytest.raises(NeedsLogin):
            download_one(store, request, threading.Event(), lambda **v: updates.append(v), sessions)
        assert store.get('media_errors')[PICTURE]['authentication'] == 'missing'
        request['password'] = 'unit-test-password'
        asset = download_one(store, request, threading.Event(), lambda **v: updates.append(v), sessions)
        assert asset['kind'] == 'image' and len(calls) == 2
        assert 'password' not in request and not store.get('media_errors')
        (tmp_path / asset['path']).unlink()
        asset = download_one(store, request, threading.Event(), lambda **v: updates.append(v), sessions)
        assert (tmp_path / asset['path']).is_file() and len(calls) == 3
        persisted = json.dumps([list(row) for row in store.db.execute('SELECT * FROM settings')])
        assert SESSION not in persisted + json.dumps(updates)
        assert 'unit-test-password' not in persisted + json.dumps(updates)
    finally:
        store.close(); sessions.clear()


@pytest.mark.parametrize('stage', ['entry', 'render'])
def test_export_passes_authenticated_client_to_both_material_stages(tmp_path, monkeypatch, stage):
    private_fixture(tmp_path)
    client = LiveJournalClient('fixture', '', budget=RequestBudget(4)); authenticated(client)
    calls = network(monkeypatch, lambda url, headers: image(200 if SESSION in headers.get('Cookie', '') else 403))
    from ljarchive.exporter import Exporter
    from ljarchive.render import build_archive
    exporter = Exporter(tmp_path, client, images=True)
    try:
        if stage == 'entry':
            exporter.material_stage()
        else:
            report = json.loads((tmp_path / 'export-report.json').read_text())
            build_archive(exporter.store, report, images=True, client=client, budget=client.budget)
        assert exporter.store.get('media')[PICTURE]
        assert len(calls) == 1 and calls[0][1]['Referer'] == 'https://fixture.livejournal.com/257.html'
    finally:
        if getattr(exporter, '_media', None): exporter._media.close()
        exporter.store.close(); client.close()


def test_real_access_denial_after_fresh_login_is_an_error_without_login_loop(tmp_path, monkeypatch):
    private_fixture(tmp_path)
    monkeypatch.setattr(LiveJournalClient, 'login', authenticated)
    calls = network(monkeypatch, lambda *a: image(403))
    store = Store(tmp_path)
    try:
        with pytest.raises(ExportError, match='HTTP 403') as exc:
            download_one(store, {'entry_id': 1, 'kind': 'image', 'value': PICTURE, 'password': 'test'},
                         threading.Event(), lambda **v: None)
        assert not isinstance(exc.value, NeedsLogin) and len(calls) == 1
        assert store.get('media_errors')[PICTURE]['authentication'] == 'sent'
    finally:
        store.close()


def test_memory_sessions_are_scoped_expire_and_survive_original_client_close(monkeypatch):
    sessions = Sessions()
    first = LiveJournalClient('fixture', '', journal='group', session_ready=sessions.remember)
    authenticated(first); first.close()
    same = LiveJournalClient('fixture', '', journal='group')
    others = [LiveJournalClient('another', '', journal='group'), LiveJournalClient('fixture', '')]
    try:
        assert sessions.restore(same) and same._cookie == SESSION and same.journal_id == 123
        assert all(not sessions.restore(client) for client in others)
        import time
        now = time.monotonic()
        monkeypatch.setattr('ljarchive.sessions.time.monotonic', lambda: now + 3601)
        assert not sessions.restore(same)
    finally:
        same.close()
        for client in others: client.close()


def test_queue_uses_export_session_and_ignores_completed_task_state(local_server, monkeypatch):
    server, _ = local_server
    private_fixture(server.jobs.output / 'fixture')
    client = LiveJournalClient('fixture', '', session_ready=server.jobs.sessions.remember)
    authenticated(client); client.close()
    server.jobs.state = {'phase': 'loading', 'slug': 'fixture'}  # Stale state, no live worker.
    calls = network(monkeypatch, lambda url, headers: image(200 if SESSION in headers.get('Cookie', '') else 403))
    task = server.jobs.materials.start({'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': PICTURE})
    result = wait(server.jobs.materials, task['id'])
    assert result['phase'] == 'complete' and len(calls) == 1
    assert SESSION not in json.dumps(result)


def test_queue_downloads_other_archive_while_one_archive_is_exporting(local_server, monkeypatch):
    server, _ = local_server
    private_fixture(server.jobs.output / 'fixture')
    calls = network(monkeypatch, lambda *a: image())
    gate = threading.Event()
    server.jobs.thread = threading.Thread(target=lambda: gate.wait(10))
    server.jobs.current = {'archive': 'another'}
    server.jobs.thread.start()
    try:
        task = server.jobs.materials.start({'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': PICTURE})
        assert wait(server.jobs.materials, task['id'])['phase'] == 'complete'
        assert len(calls) == 1 and server.jobs.thread.is_alive()
    finally:
        gate.set(); server.jobs.thread.join()


def test_preparing_archive_keeps_status_and_queue_responsive_then_downloads(local_server, monkeypatch):
    server, base = local_server
    folder = server.jobs.output / 'fixture'
    private_fixture(folder)
    report = json.loads((folder / 'export-report.json').read_text())
    report['format_version'] = '0.1.0'
    from ljarchive.storage import atomic_write, dumps
    atomic_write(folder / 'export-report.json', dumps(report))
    entered, release = threading.Event(), threading.Event()
    from ljarchive.render import build_archive
    def slow_build(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return build_archive(*args, **kwargs)
    monkeypatch.setattr('ljarchive.render.build_archive', slow_build)
    calls = network(monkeypatch, lambda *a: image())
    try:
        assert server.jobs.prepare_format('fixture')['phase'] == 'preparing'
        assert entered.wait(2)
        # This endpoint takes Jobs.lock, which must not be held during rendering.
        with requests.Session() as browser:
            browser.request('GET', base)
            assert browser.request('GET', base + '/api/status', timeout=2).status_code == 200
            for extension in ('zip', 'json'):
                assert browser.request('GET', base + '/download/fixture.' + extension, timeout=2).status_code == 409
            response = browser.post(base + '/api/material', json={
                'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': PICTURE,
            }, headers={'Origin': base}, timeout=2)
        assert response.status_code == 202
        task = response.json()
        assert task['phase'] == 'queued' and not calls
        release.set()
        assert wait(server.jobs.materials, task['id'])['phase'] == 'complete'
        assert len(calls) == 1 and not server.jobs.formatting
    finally:
        release.set()
        for thread in server.jobs.preparation_threads: thread.join(5)
