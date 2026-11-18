"""A master API token must be exchanged for server-issued photo cookies."""
import json
import threading

import requests

from ljarchive.client import LiveJournalClient, RequestBudget
from ljarchive.media import Media
from ljarchive.sessions import Sessions
from ljarchive.storage import Store
from test_media_auth import PICTURE, authenticated
from test_media_errors import ImageResponse

PASSWORD = 'synthetic-web-password'
PHOTO_COOKIE = 'synthetic-server-photo-signature'
LOGIN_COOKIE = 'synthetic-server-login-signature'


def web_network(monkeypatch, client, *, accepted=True):
    calls = []
    monkeypatch.setattr(client, '_wait', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda *a: True)
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    def request(method, url, **kwargs):
        calls.append((method, url))
        assert url == 'https://www.livejournal.com/login.bml'
        assert kwargs['allow_redirects'] is False
        if method == 'GET':
            return ImageResponse(content=b'<form action="/login.bml"><input name="lj_form_auth" value="synthetic-form-token"></form>')
        assert kwargs['data'] == {'user': 'fixture', 'password': PASSWORD,
                                  'lj_form_auth': 'synthetic-form-token', 'returnto': '/'}
        assert kwargs['headers']['Origin'] == 'https://www.livejournal.com'
        if accepted:
            client.session.cookies.set('ljmastersession', 'synthetic-master-cookie', domain='www.livejournal.com', secure=True)
            client.session.cookies.set('ljloggedin', LOGIN_COOKIE, domain='.livejournal.com', secure=True)
            client.session.cookies.set('ljsession', PHOTO_COOKIE, domain='.livejournal.com', secure=True)
        return ImageResponse(status=302, headers={'Location': 'https://unrelated.example/'})
    monkeypatch.setattr(client.session, 'request', request)
    def image(session, url, **kwargs):
        calls.append(('IMAGE', url))
        cookie = kwargs['headers'].get('Cookie', '')
        valid = f'ljsession={PHOTO_COOKIE}' in cookie and f'ljloggedin={LOGIN_COOKIE}' in cookie
        assert 'synthetic-master-cookie' not in cookie
        return ImageResponse(status=200 if valid else 403,
                             content=b'private-photo' if valid else b'placeholder',
                             headers={'Content-Type': 'image/jpeg'})
    monkeypatch.setattr(requests.Session, 'get', image)
    return calls


def test_web_login_downloads_photo_remembers_cookies_and_never_saves_password(tmp_path, monkeypatch):
    updates, sessions = [], Sessions()
    client = LiveJournalClient('fixture', PASSWORD, budget=RequestBudget(10),
                               progress=lambda **v: updates.append(v), session_ready=sessions.remember)
    authenticated(client)
    calls = web_network(monkeypatch, client)
    media = Media(tmp_path, True, threading.Event(), client.budget, client)
    store = Store(tmp_path)
    try:
        path = media.get(PICTURE)
        assert path and (tmp_path / path).read_bytes() == b'private-photo'
        assert [m for m, _ in calls] == ['IMAGE', 'GET', 'POST', 'IMAGE']
        assert client.budget.used == 4 and client._web_password == ''
        assert media.session_types[PICTURE] == 'web'
        with store.db: media.save_state(store)
        persisted = json.dumps([list(r) for r in store.db.execute('SELECT * FROM settings')]) + json.dumps(updates)
        assert all(secret not in persisted for secret in (PASSWORD, PHOTO_COOKIE, LOGIN_COOKIE))
        other = LiveJournalClient('fixture', '')
        try:
            assert sessions.restore(other)
            again = Media(tmp_path, True, threading.Event(), other.budget, other)
            try:
                assert again.get(PICTURE + '?second')
                assert len(calls) == 5  # No second login and no API cookie sent as the photo cookie.
            finally: again.close()
        finally: other.close()
    finally:
        media.close(); client.close(); store.close(); sessions.clear()


def test_rejected_web_login_stays_pending_without_saving_error_image(tmp_path, monkeypatch):
    client = LiveJournalClient('fixture', PASSWORD); authenticated(client)
    calls = web_network(monkeypatch, client, accepted=False)
    media = Media(tmp_path, True, threading.Event(), client.budget, client)
    try:
        assert media.get(PICTURE) is None
        assert media.errors[PICTURE]['reason'] == 'web_login'
        assert media.errors[PICTURE]['status'] == 'pending'
        assert not media.errors[PICTURE].get('attempts') and not list((tmp_path / 'media').glob('*'))
        media.retry_failed()
        assert len(calls) == 3
    finally: media.close(); client.close()


def test_web_login_respects_request_budget_and_does_not_follow_redirect(tmp_path, monkeypatch):
    client = LiveJournalClient('fixture', PASSWORD, budget=RequestBudget(2)); authenticated(client)
    calls = web_network(monkeypatch, client)
    media = Media(tmp_path, True, threading.Event(), client.budget, client)
    try:
        assert media.get(PICTURE) is None
        assert [m for m, _ in calls] == ['IMAGE', 'GET']
        assert media.errors[PICTURE]['reason'] == 'request_limit'
        assert not media.errors[PICTURE].get('attempts')
    finally: media.close(); client.close()


def test_old_forbidden_api_attempts_are_reopened_for_web_login(tmp_path):
    store = Store(tmp_path)
    with store.db:
        store.set('media_errors', {PICTURE: {'status': 'failed', 'reason': 'http', 'http_status': 403,
                                          'authentication': 'sent', 'attempts': 3, 'closed': True}})
    media = Media(tmp_path, True, threading.Event())
    try:
        media.load_state(store)
        assert not media.errors[PICTURE].get('closed') and not media.errors[PICTURE].get('attempts')
    finally: media.close(); store.close()


def test_photo_redirect_uses_www_cookie_and_keeps_issued_photo_cookie(tmp_path, monkeypatch):
    client = LiveJournalClient('fixture', PASSWORD); authenticated(client)
    calls = web_network(monkeypatch, client)
    gateway = 'https://www.livejournal.com/misc/session.bml?return=synthetic'
    seen = []
    def image(session, url, **kwargs):
        seen.append(url)
        cookie = kwargs['headers'].get('Cookie', '')
        if url == gateway:
            assert 'ljmastersession=synthetic-master-cookie' in cookie
            response = ImageResponse(status=302, headers={'Location': PICTURE})
            response.cookies = requests.cookies.RequestsCookieJar()
            response.cookies.set('ljsession', 'synthetic-photo-domain', domain='.livejournal.com', secure=True)
            return response
        if 'synthetic-photo-domain' in cookie:
            return ImageResponse(content=b'picture-through-login', headers={'Content-Type': 'image/png'})
        return ImageResponse(status=302, headers={'Location': gateway})
    monkeypatch.setattr(requests.Session, 'get', image)
    media = Media(tmp_path, True, threading.Event(), client.budget, client)
    try:
        path = media.get(PICTURE)
        assert (tmp_path / path).read_bytes() == b'picture-through-login'
        assert seen == [PICTURE, gateway, PICTURE]
        assert [m for m, _ in calls] == ['GET', 'POST']
        assert client.budget.used == 5
        assert media.session_types[PICTURE] == 'web'
        assert not media.errors
    finally: media.close(); client.close()


def test_unchanged_login_redirect_stops_once_and_does_not_repeat_known_images(tmp_path, monkeypatch):
    client = LiveJournalClient('fixture', PASSWORD); authenticated(client)
    calls = web_network(monkeypatch, client)
    gateway = 'https://www.livejournal.com/misc/session.bml'
    seen = []
    def image(session, url, **kwargs):
        seen.append(url)
        if url.endswith('/public.png'):
            return ImageResponse(content=b'public', headers={'Content-Type': 'image/png'})
        return ImageResponse(status=302, headers={'Location': PICTURE if url == gateway else gateway})
    monkeypatch.setattr(requests.Session, 'get', image)
    media = Media(tmp_path, True, threading.Event(), client.budget, client)
    try:
        assert media.get(PICTURE) is None
        assert seen == [PICTURE, gateway, PICTURE]
        assert media.errors[PICTURE]['reason'] == 'web_login'
        assert media.errors[PICTURE]['status'] == 'pending'
        assert media.errors[PICTURE]['authentication'] == 'sent'
        assert not media.errors[PICTURE].get('closed')
        media.retry_failed()
        old = PICTURE + '?old'
        media.errors[old] = {'reason': 'redirects', 'attempts': 2, 'status': 'failed'}
        assert media.get(old) is None and len(seen) == 3
        assert media.get('https://ic.pics.livejournal.com/public.png')
        assert len(seen) == 4
    finally: media.close(); client.close()


def test_old_login_redirect_loop_is_reopened_without_affecting_external_links(tmp_path):
    store = Store(tmp_path)
    old = {'status': 'failed', 'reason': 'redirects', 'http_status': 302, 'attempts': 3, 'closed': True}
    with store.db: store.set('media_errors', {PICTURE: old, 'https://images.example/loop': old})
    media = Media(tmp_path, True, threading.Event())
    try:
        media.load_state(store)
        assert not media.errors[PICTURE].get('closed')
        assert media.errors['https://images.example/loop']['closed']
    finally: media.close(); store.close()
