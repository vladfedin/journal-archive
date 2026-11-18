"""A real Chromium session and a local protected-image server, no LJ traffic."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import secrets
import threading
import time

import pytest

from ljarchive.browser_bridge import BrowserBridge, BrowserFailure, browser_executable
from ljarchive.client import Cancelled, RequestBudget, RequestLimitReached

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==')


@pytest.fixture
def protected_browser():
    executable = browser_executable()
    if not executable:
        choices = sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux*/chrome'))
        executable = choices[-1] if choices else None
    if not executable:
        pytest.skip('Chrome/Edge not installed')
    token, calls = secrets.token_hex(20), []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            calls.append(self.path)
            headers = {}
            if self.path == '/login':
                status, mime, body = 200, 'text/html', b'<link rel="icon" href="data:,"><form action="/signed-in"><button>Sign in</button></form>'
            elif self.path == '/signed-in':
                headers['Set-Cookie'] = 'session=' + token + '; HttpOnly; SameSite=Lax; Path=/'
                status, mime, body = 200, 'text/html', b'<link rel="icon" href="data:,">Signed in'
            elif self.path == '/redirect':
                headers['Location'] = '/private.png?auth=' + token
                status, mime, body = 302, 'text/plain', b''
            elif self.path.startswith('/private.png') or self.path == '/login_user.png':
                allowed = self.headers.get('Cookie') == 'session=' + token
                status, mime, body = (200, 'image/png', PNG) if allowed else (403, 'text/html', b'Sign in')
            elif self.path == '/private.html':
                allowed = self.headers.get('Cookie') == 'session=' + token
                status, mime, body = (200, 'text/html', b'<div data-rambler-player-id="123">Private player</div><script>fetch("/unexpected")</script>') if allowed else (403, 'text/html', b'Sign in')
            elif self.path == '/empty-check':
                status, mime, body = 200, 'text/plain', b''
            elif self.path == '/missing.png':
                status, mime, body = 404, 'text/html', b'Missing'
            elif self.path == '/denied.png':
                status, mime, body = 403, 'text/html', b'Access denied'
            elif self.path == '/unauthorized.png':
                status, mime, body = 401, 'text/html', b'Access denied'
            elif self.path == '/limited.png':
                headers['Retry-After'] = '7'
                status, mime, body = 429, 'text/html', b'Wait'
            elif self.path == '/active.html':
                status, mime, body = 200, 'text/html', b'<script>new WebSocket("ws://" + location.host + "/unbudgeted")</script>'
            else:
                status, mime, body = 200, 'text/html', b'Not an image'
            self.send_response(status)
            for key, value in headers.items(): self.send_header(key, value)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass  # Expected when a request is cancelled.
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    def validate(url):
        if not url.startswith(base + '/'):
            raise ValueError('outside fixture')
    bridge = BrowserBridge(executable=executable, headless=True, validate_url=validate, login_url=base + '/login')
    try:
        bridge.open_login('fixture')
        pages = [tab for tab in bridge._call('Target.getTargets')['targetInfos'] if tab['type'] == 'page']
        assert len(pages) == 1 and pages[0]['targetId'] == bridge.contexts['fixture']['target']
        yield bridge, base, calls, token
    finally:
        bridge.close(); server.shutdown(); server.server_close(); thread.join()


def sign_in(bridge, base, calls):
    tab = bridge.contexts['fixture']['target']
    session = bridge._call('Target.attachToTarget', {'targetId': tab, 'flatten': True})['sessionId']
    bridge._call('Page.navigate', {'url': base + '/signed-in'}, session)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if '/signed-in' in calls:
            result = bridge._call('Runtime.evaluate', {'expression': 'document.body.innerText'}, session)
            if result.get('result', {}).get('value') == 'Signed in':
                bridge.confirm('fixture')
                return
        time.sleep(.02)
    pytest.fail('Browser login did not complete')


def budget(limit=10):
    value = RequestBudget(limit)
    value.delay = 0
    return value


def test_real_browser_uses_login_and_redirects_without_exporting_credentials(protected_browser):
    bridge, base, calls, token = protected_browser
    bridge.confirm('fixture')
    with pytest.raises(BrowserFailure) as failure:
        bridge.image('fixture', base + '/private.png', base + '/', budget(), threading.Event())
    assert failure.value.http_status == 403
    assert failure.value.trace[-1]['http_status'] == 403
    sign_in(bridge, base, calls)
    counter = budget()
    body, ext, trace = bridge.image('fixture', base + '/redirect', base + '/', counter, threading.Event())
    assert body == PNG and ext == '.png'
    assert counter.used == 2
    assert [item['http_status'] for item in trace] == [302, 200]
    assert token not in str(trace) and all('?' not in item['url'] for item in trace)
    # A different archive account cannot reuse this login.
    bridge.open_login('different'); bridge.confirm('different')
    with pytest.raises(BrowserFailure) as other:
        bridge.image('different', base + '/private.png', '', budget(), threading.Event())
    assert other.value.http_status == 403


def test_browser_waits_for_a_temporarily_locked_port_file(protected_browser, monkeypatch):
    import ljarchive.browser_bridge as module
    bridge, _, _, _ = protected_browser
    bridge.close()
    read = Path.read_text
    attempts = []
    def locked(path, *args, **kwargs):
        if path.name == 'DevToolsActivePort':
            attempts.append(1)
            if len(attempts) == 1: raise PermissionError('browser is writing')
            if len(attempts) == 2: return '9222\n'  # A partially written file.
            if len(attempts) == 3: return '9222\n/devtools/browser/123'
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', locked)
    connect = module.connect
    connections = []
    def not_listening(*args, **kwargs):
        connections.append(1)
        if len(connections) == 1: raise ConnectionRefusedError('browser is starting')
        return connect(*args, **kwargs)
    monkeypatch.setattr(module, 'connect', not_listening)
    bridge.open_login('fixture')
    assert len(attempts) >= 5 and len(connections) == 2 and bridge.contexts['fixture']['target']


def test_real_browser_obeys_budget_cancel_and_http_errors(protected_browser):
    bridge, base, calls, _ = protected_browser
    sign_in(bridge, base, calls)
    calls.clear()
    with pytest.raises(RequestLimitReached) as limited:
        bridge.image('fixture', base + '/redirect', '', budget(1), threading.Event())
    assert limited.value.http_status == 302
    assert limited.value.trace[-1]['http_status'] == 302
    assert '/redirect' in calls and not any(p.startswith('/private.png') for p in calls)
    cancel = threading.Event(); cancel.set()
    with pytest.raises(Cancelled):
        bridge.image('fixture', base + '/private.png', '', budget(), cancel)
    assert not any(p.startswith('/private.png') for p in calls)
    with pytest.raises(BrowserFailure) as missing:
        bridge.image('fixture', base + '/missing.png', '', budget(), threading.Event())
    assert missing.value.http_status == 404
    with pytest.raises(BrowserFailure) as wrong:
        bridge.image('fixture', base + '/page', '', budget(), threading.Event())
    assert wrong.value.reason == 'browser_type' and wrong.value.http_status == 200


def test_real_browser_keeps_pacing_across_files_and_server_deadline(protected_browser):
    bridge, base, calls, _ = protected_browser
    sign_in(bridge, base, calls)
    counter = budget(); counter.delay = .3
    bridge.image('fixture', base + '/private.png', '', counter, threading.Event())
    started = time.monotonic()
    bridge.image('fixture', base + '/private.png', '', counter, threading.Event())
    assert time.monotonic() - started >= .2
    deadlines = []; counter.save_cooldown = deadlines.append
    with pytest.raises(BrowserFailure) as limited:
        bridge.image('fixture', base + '/limited.png', '', counter, threading.Event())
    assert limited.value.http_status == 429
    assert deadlines[-1] >= time.time() + 6
    assert counter.wait_info['http_status'] == 429 and counter.wait_info['server_delay']


def test_download_target_blocks_html_script_even_when_controller_is_delayed(protected_browser, monkeypatch):
    bridge, base, calls, _ = protected_browser
    sign_in(bridge, base, calls)
    # A real page in the login window can still run scripts. This also proves
    # that the fixture would observe the WebSocket handshake if it escaped.
    target = bridge.contexts['fixture']['target']
    session = bridge._call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
    bridge._call('Page.navigate', {'url': base + '/active.html'}, session)
    deadline = time.monotonic() + 5
    while '/unbudgeted' not in calls and time.monotonic() < deadline:
        time.sleep(.02)
    assert '/unbudgeted' in calls
    calls.clear()
    send = bridge._send
    def delayed_send(method, params=None, session=None):
        result = send(method, params, session)
        if method == 'Fetch.continueRequest':
            # Let Chromium parse/execute the response while Python has not yet
            # processed responseReceived and rejected its HTML content type.
            time.sleep(.3)
        return result
    monkeypatch.setattr(bridge, '_send', delayed_send)
    counter = budget(1)
    with pytest.raises(BrowserFailure) as wrong:
        bridge.image('fixture', base + '/active.html', '', counter, threading.Event())
    assert wrong.value.reason == 'browser_type' and wrong.value.http_status == 200
    assert counter.used == 1 and calls.count('/active.html') == 1
    assert '/unbudgeted' not in calls
    body, ext, trace = bridge.image('fixture', base + '/private.png', '', budget(1), threading.Event())
    assert body == PNG and ext == '.png' and trace[-1]['http_status'] == 200


def test_browser_download_persists_file_json_and_html_then_skips_it(protected_browser, tmp_path, monkeypatch):
    import json
    from ljarchive.on_demand import download_one, download_browser_images
    from ljarchive.sessions import Sessions
    from ljarchive.storage import Store
    from ljarchive.render import build_archive
    bridge, base, calls, token = protected_browser
    sign_in(bridge, base, calls)
    sessions = Sessions(); sessions.browser = bridge
    monkeypatch.setattr('ljarchive.media.authenticated_image_host', lambda u: u.startswith(base + '/'))
    monkeypatch.setattr('ljarchive.on_demand.authenticated_image_host', lambda u: u.startswith(base + '/'))
    store = Store(tmp_path)
    url = base + '/private.png'
    try:
        with store.db:
            store.set('username', 'fixture')
            store.save_entry({'itemid': 1, 'anum': 1, 'subject': 'Protected picture', 'eventtime': '2024-01-01 00:00:00', 'security': 'private', 'event': f'<img src="{url}">', 'extra': {'untouched': True}})
            store.set('media', {url: None})
            store.set('entry_materials', {'1': {'urls': [url]}})
            store.set('media_errors', {url: {'reason': 'web_login', 'http_status': 302, 'requests': 4}})
        updates = []
        # The same session is reused; no password or cookie passes through application forms.
        result = download_one(store, {'entry_id': 1, 'kind': 'image', 'value': url, 'transport': 'browser', 'delay': 1}, threading.Event(), lambda **k: updates.append(k), sessions)
        assert (tmp_path / result['path']).read_bytes() == PNG
        assert not store.get('media_errors')
        evidence = store.get('media_downloads')[url]
        assert evidence['transport'] == 'browser' and evidence['trace'][-1]['http_status'] == 200
        report = {'username': 'fixture', 'status': 'partial', 'created_at': '2026-01-01T00:00:00Z', 'entries_saved': 1, 'comments_saved': 0, 'missing_comments': []}
        build_archive(store, report)
        data = json.loads((tmp_path / 'data/archive.json').read_text())
        assert data['media'][url] == result['path'] and data['media_downloads'][url] == evidence
        assert data['entries'][0]['security'] == 'private' and data['entries'][0]['extra']['untouched']
        assert token not in json.dumps(data)
        assert '../' + result['path'] in (tmp_path / 'entries/1.html').read_text()
        from ljarchive.activitystreams import source_snapshot
        assert source_snapshot(json.loads((tmp_path / 'data/activitystreams.json').read_text())) == data
        calls.clear()
        again = download_browser_images(store, {'delay': 1}, threading.Event(), lambda **k: None, sessions)
        assert again['total'] == 0 and not calls
    finally:
        store.close()


def test_browser_access_denial_and_dns_are_not_password_or_disk_errors(protected_browser, tmp_path, monkeypatch):
    import socket
    from ljarchive.client import ExportError, LiveJournalClient
    from ljarchive.media import Media
    from ljarchive.on_demand import download_one, NeedsLogin
    from ljarchive.sessions import Sessions
    from ljarchive.storage import Store
    bridge, base, _, _ = protected_browser
    bridge.confirm('fixture')
    sessions = Sessions(); sessions.browser = bridge
    monkeypatch.setattr('ljarchive.media.authenticated_image_host', lambda u: u.startswith(base + '/'))
    monkeypatch.setattr('ljarchive.on_demand.authenticated_image_host', lambda u: u.startswith(base + '/'))
    store = Store(tmp_path); url = base + '/private.png'
    try:
        with store.db:
            store.set('username', 'fixture')
            store.save_entry({'itemid': 1, 'event': f'<img src="{url}">'})
        with pytest.raises(ExportError) as refused:
            download_one(store, {'entry_id': 1, 'kind': 'image', 'value': url, 'transport': 'browser'}, threading.Event(), lambda **k: None, sessions)
        assert not isinstance(refused.value, NeedsLogin)
        error = store.get('media_errors')[url]
        assert error['http_status'] == 403 and error['session_type'] == 'browser'
        def dns_failure(url): raise socket.gaierror('fixture DNS failure')
        bridge.validate_url = dns_failure
        client = LiveJournalClient('fixture', ''); client.browser = bridge
        media = Media(tmp_path, True, threading.Event(), client=client)
        try:
            assert media.get(url) is None
            assert media.errors[url]['reason'] == 'browser_dns' and not media.stopped
            assert media.errors[url]['requests'] == 0
        finally: media.close(); client.close()
    finally: store.close()


def test_first_http_login_failure_continues_same_file_in_browser(protected_browser, tmp_path, monkeypatch):
    from ljarchive.client import LiveJournalClient, WebLoginRequired
    from ljarchive.media import Media
    bridge, base, calls, _ = protected_browser
    client = LiveJournalClient('fixture', '')
    client.browser = bridge
    confirmations = []
    def confirm():
        confirmations.append(True)
        sign_in(bridge, base, calls)
    client.request_browser = confirm
    media = Media(tmp_path, True, threading.Event(), budget(), client)
    monkeypatch.setattr('ljarchive.media.authenticated_image_host', lambda u: u.startswith(base + '/'))
    monkeypatch.setattr('ljarchive.media.public_url', lambda u: None)
    def http_login_failed(*args, **kwargs): raise WebLoginRequired()
    monkeypatch.setattr(media.session, 'get', http_login_failed)
    try:
        path = media.get(base + '/private.png', referer=base + '/')
        assert (tmp_path / path).read_bytes() == PNG
        assert confirmations == [True] and not media.auth_unavailable
        assert not media.errors and media.downloads[base + '/private.png']['transport'] == 'browser'
    finally:
        media.close(); client.close()


def test_browser_batch_continues_denials_and_keeps_them_closed_on_reload(protected_browser, tmp_path, monkeypatch):
    from ljarchive.on_demand import download_browser_images
    from ljarchive.sessions import Sessions
    from ljarchive.storage import Store
    from ljarchive.media import Media
    bridge, base, calls, _ = protected_browser
    sign_in(bridge, base, calls); calls.clear()
    sessions = Sessions(); sessions.browser = bridge
    monkeypatch.setattr('ljarchive.media.authenticated_image_host', lambda u: u.startswith(base + '/'))
    monkeypatch.setattr('ljarchive.on_demand.authenticated_image_host', lambda u: u.startswith(base + '/'))
    urls = [base + p for p in ('/denied.png', '/private.png', '/unauthorized.png')]
    store = Store(tmp_path)
    try:
        with store.db:
            store.set('username', 'fixture')
            store.save_entry({'itemid': 1, 'event': ''.join(f'<img src="{u}">' for u in urls)})
            store.set('entry_materials', {'1': {'urls': urls}})
        result = download_browser_images(store, {'delay': 1, 'max_requests': 7}, threading.Event(), lambda **k: None, sessions)
        assert result == {'kind': 'images', 'saved': 1, 'failed': 2, 'total': 3}
        assert calls.index('/private.png') > calls.index('/denied.png')
        assert calls.count('/denied.png') == calls.count('/unauthorized.png') == 3
        assert calls.count('/private.png') == 1
        media = Media(tmp_path, True, threading.Event())
        try:
            media.load_state(store)
            assert all(e['attempts'] == 3 and e['closed'] and e['status'] == 'failed' for e in media.errors.values())
            media.retry_failed()
            assert calls.count('/denied.png') == 3  # Closed browser refusals do not reopen on restart.
        finally: media.close()
    finally: store.close()


def test_authenticated_document_and_empty_check_use_browser_without_subrequests(protected_browser):
    bridge, base, calls, token = protected_browser
    sign_in(bridge, base, calls); calls.clear()
    counter = budget(2)
    body, trace = bridge.document('fixture', base + '/private.html', base + '/', counter, threading.Event())
    assert b'Private player' in body and trace[-1]['http_status'] == 200
    empty, trace2 = bridge.document('fixture', base + '/empty-check', base + '/', counter, threading.Event())
    assert empty == b'' and trace2[-1]['http_status'] == 200
    assert calls == ['/private.html', '/empty-check'] and counter.used == 2
    assert token not in str(trace + trace2)


def test_image_name_containing_login_is_not_a_login_page(protected_browser):
    bridge, base, calls, _ = protected_browser
    sign_in(bridge, base, calls)
    # The authenticated image can belong to an account/path containing 'login'.
    body, ext, trace = bridge.image('fixture', base + '/login_user.png', '', budget(), threading.Event())
    assert body == PNG and ext == '.png' and trace[-1]['http_status'] == 200
