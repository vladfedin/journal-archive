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
