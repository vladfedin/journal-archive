import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests

from ljarchive.client import LiveJournalClient, RequestBudget
from ljarchive.media import Media
from ljarchive.render import safe_content
from ljarchive.storage import Store
from ljarchive.video import VideoArchive, VideoRefs, canonical_video, embed_key


@pytest.fixture
def video_source():
    payload = (Path(__file__).parent / 'fixtures/video.mp4').read_bytes()
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            calls.append((self.path, self.headers.get('Range')))
            start = int(self.headers.get('Range', 'bytes=0-').split('=')[1].split('-')[0])
            self.send_response(206 if start else 200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(payload) - start))
            if start: self.send_header('Content-Range', f'bytes {start}-{len(payload)-1}/{len(payload)}')
            self.end_headers(); self.wfile.write(payload[start:])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/video.mp4', calls, payload
    finally: server.shutdown(); server.server_close(); thread.join()


def manager(folder, monkeypatch, limit=10):
    store = Store(folder)
    client = LiveJournalClient('fixture', 'unused', budget=RequestBudget(limit))
    video = VideoArchive(store, client, lambda **kw: None)
    monkeypatch.setattr(video, 'wait', lambda _: None)
    monkeypatch.setattr('ljarchive.video.public_url', lambda _: None)
    return video


def close(video):
    video.close(); video.client.close(); video.store.close()


def test_download_uses_shared_budget_and_cached_video_survives_restart(tmp_path, monkeypatch, video_source):
    url, calls, payload = video_source
    video = manager(tmp_path, monkeypatch)
    try:
        video.download(url, 1)
        row = video.records[url]
        assert row['status'] == 'saved', row
        assert (tmp_path / row['path']).read_bytes() == payload
        assert video.client.budget.used == len(calls)
        first = len(calls)
    finally: close(video)
    video = manager(tmp_path, monkeypatch)
    try:
        video.download(url, 1)
        assert len(calls) == first and video.client.budget.used == 0
    finally: close(video)


def test_video_limit_and_cancel_do_not_turn_into_success(tmp_path, monkeypatch, video_source):
    url, calls, _ = video_source
    video = manager(tmp_path, monkeypatch, limit=1)
    video.client.budget.consume('livejournal')
    try:
        video.download(url, 1)
        assert not calls
        assert video.records[url]['status'] == 'pending'
        assert video.records[url]['error']['reason'] == 'request_limit'
        assert video.client.budget.used == 1
    finally: close(video)
    video = manager(tmp_path, monkeypatch)
    video.client.cancel.set()
    try:
        video.download(url, 1)
        assert not calls and video.records[url]['error']['reason'] == 'cancelled'
    finally: close(video)


def test_lj_embed_resolution_is_saved_without_tokens_or_changing_source(tmp_path, monkeypatch):
    video = manager(tmp_path, monkeypatch)
    base = 'https://fixture.livejournal.com/258.html'
    entry = {'itemid': 1, 'anum': 2, 'event': '<lj-embed id="45"/><lj-cut><lj-embed id="46"/></lj-cut>'}
    raw = entry['event']; calls = []
    video.client._cookie = 'ownersession'
    def request(*a, **kw):
        calls.append(a)
        assert kw['headers']['Cookie'] == 'ljsession=ownersession'
        return b'<iframe src="https://l.lj-toys.com/?auth_token=secret&amp;moduleid=45&amp;source=youtube&amp;vid=D-7x7LlUXms"></iframe><iframe src="https://l.lj-toys.com/?moduleid=46&amp;source=youtube&amp;vid=bxghf4uOkWA"></iframe>'
    monkeypatch.setattr(video.client, '_request', request)
    monkeypatch.setattr(video, 'download', lambda *a: None)
    try:
        video.capture(entry, True); video.capture(entry, True)
        assert len(calls) == 1 and entry['event'] == raw
        assert video.embeds[embed_key(base, '45')] == 'https://www.youtube.com/watch?v=D-7x7LlUXms'
        assert 'secret' not in json.dumps(video.store.get('video_embeds'))
        media = Media(tmp_path, False, threading.Event()); media.load_state(video.store)
        rendered = str(safe_content(raw, base, {}, media, set()))
        assert rendered.count('Видео в оригинале') == 2 and 'D-7x7LlUXms' in rendered
        assert 'iframe' not in rendered and 'auth_token' not in rendered
        media.close()
    finally: close(video)


def test_local_player_preserves_cut_and_never_embeds_remote_players(tmp_path):
    url = 'https://www.youtube.com/watch?v=D-7x7LlUXms'
    media = Media(tmp_path, False, threading.Event())
    (tmp_path / 'media').mkdir()
    (tmp_path / 'media/video.mp4').write_bytes(b'fixture')
    media.videos[url] = {'status': 'saved', 'path': 'media/video.mp4'}
    body = '<lj-cut><iframe src="https://www.youtube.com/embed/D-7x7LlUXms" onload="bad()"></iframe></lj-cut>'
    result = str(safe_content(body, 'https://fixture.livejournal.com/258.html', {}, media, set()))
    assert '<video controls preload="none" src="../media/video.mp4"></video>' in result
    assert '<details class="lj-cut">' in result
    assert 'iframe' not in result and 'onload' not in result
    media.close()


def test_inventory_knows_sources_and_ignores_non_video_embeds():
    refs = VideoRefs('https://fixture.livejournal.com/258.html')
    refs.feed('<iframe src="https://other.livejournal.com/123.html?embed"></iframe><video><source src="https://cdn.example/clip.mp4"></video><a href="https://youtu.be/D-7x7LlUXms">watch</a>')
    assert refs.urls == {'https://cdn.example/clip.mp4', 'https://www.youtube.com/watch?v=D-7x7LlUXms'}
    assert canonical_video('javascript:alert(1)') is None


def test_partial_video_is_resumed_with_range(tmp_path, monkeypatch, video_source):
    import hashlib
    url, calls, payload = video_source
    work = tmp_path / '.video-work' / hashlib.sha256(url.encode()).hexdigest()
    work.mkdir(parents=True)
    (work / 'video.mp4.part').write_bytes(payload[:1000])
    video = manager(tmp_path, monkeypatch)
    try:
        video.download(url, 1)
        assert video.records[url]['status'] == 'saved', video.records[url]
        assert any(value == 'bytes=1000-' for _, value in calls), calls
        assert (tmp_path / video.records[url]['path']).read_bytes() == payload
    finally: close(video)


def test_video_data_round_trip_and_activitystreams_attachment(tmp_path):
    from ljarchive.data import snapshot
    from ljarchive.activitystreams import to_activitystreams, source_snapshot
    store = Store(tmp_path)
    url = 'https://www.youtube.com/watch?v=D-7x7LlUXms'
    with store.db:
        store.set('username', 'fixture')
        store.set('videos', {url: {'status': 'saved', 'path': 'media/video.mp4', 'title': 'Track'}})
        store.set('video_embeds', {embed_key('https://fixture.livejournal.com/258.html', '45'): url})
        store.save_entry({'itemid': 1, 'anum': 2, 'event': '<lj-embed id="45"/>'})
    try:
        data = snapshot(store); standard = to_activitystreams(data)
        assert source_snapshot(standard) == data
        objects = standard.get('orderedItems', standard.get('items', []))
        assert any(a['type'] == 'Video' and a['ja:localPath'] == 'media/video.mp4' for o in objects for a in o.get('attachment', [])), standard.keys()
    finally: store.close()


def test_redirect_and_429_attempts_count_and_keep_cooldown(tmp_path, monkeypatch):
    import io
    from requests import Response
    from yt_dlp.networking import Request
    from yt_dlp.networking.exceptions import HTTPError
    video = manager(tmp_path, monkeypatch)
    calls, waits = [], []
    monkeypatch.setattr(video, 'wait', waits.append)
    def request(method, url, **kw):
        calls.append(url)
        from urllib3.response import HTTPResponse
        r = Response(); r.url = url; r.raw = HTTPResponse(body=io.BytesIO(b'ok'), preload_content=False)
        if len(calls) == 1:
            r.status_code = 302; r.headers['Location'] = 'https://cdn.example/clip.mp4'
        elif len(calls) == 2:
            r.status_code = 429; r.headers['Retry-After'] = '3600'
        else: r.status_code = 200
        return r
    monkeypatch.setattr(video.session, 'request', request)
    try:
        with pytest.raises(HTTPError) as failure:
            video.open(Request('https://example.org/clip.mp4'))
        failure.value.response.close()
        with video.open(Request('https://example.org/clip.mp4')) as response:
            assert response.read() == b'ok'
        assert len(calls) == video.client.budget.used == 3
        assert max(waits) > 3590
    finally: close(video)


def test_video_redirect_to_private_network_is_rejected_before_request(tmp_path, monkeypatch):
    import io
    from requests import Response
    from yt_dlp.networking import Request
    from ljarchive.media import public_url
    video = manager(tmp_path, monkeypatch)
    checked, calls = [], []
    def check(url):
        checked.append(url)
        if '127.0.0.1' in url: public_url(url)
    monkeypatch.setattr('ljarchive.video.public_url', check)
    def request(method, url, **kw):
        calls.append(url)
        r = Response(); r.status_code = 302; r.raw = io.BytesIO()
        r.headers['Location'] = 'http://127.0.0.1/secret'; return r
    monkeypatch.setattr(video.session, 'request', request)
    try:
        with pytest.raises(ValueError): video.open(Request('https://example.org/clip.mp4'))
        assert len(calls) == video.client.budget.used == 1 and len(checked) == 2
    finally: close(video)
