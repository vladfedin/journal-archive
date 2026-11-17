"""Regressions for the boundary between unavailable data and retryable actions."""
import json
import threading

import pytest
import requests

from ljarchive.gaps import ATTEMPTS, closed
from ljarchive.media import Media
from ljarchive.on_demand import download_one
from ljarchive.storage import Store
from ljarchive.sync import SyncExporter
from test_finite_gaps import ONE
from test_media_auth import PICTURE, authenticated, private_fixture
from test_media_errors import ImageResponse
from test_sync import Journal


def unavailable(**extra):
    return {'status': 'failed', 'attempts': ATTEMPTS, 'closed': True, **extra}


def test_click_reopens_a_closed_image_after_local_login(tmp_path, monkeypatch):
    private_fixture(tmp_path)
    monkeypatch.setattr('ljarchive.client.LiveJournalClient.login', authenticated)
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda *a: True)
    calls = []
    def get(session, url, **kwargs):
        calls.append(kwargs['headers'])
        return ImageResponse(content=b'image', headers={'Content-Type': 'image/png'})
    monkeypatch.setattr(requests.Session, 'get', get)
    store = Store(tmp_path)
    try:
        with store.db:
            store.set('media_errors', {PICTURE: unavailable(reason='http', http_status=403, authentication='sent')})
        result = download_one(store, {'entry_id': 1, 'kind': 'image', 'value': PICTURE, 'password': 'test'},
                              threading.Event(), lambda **v: None)
        assert (tmp_path / result['path']).is_file() and len(calls) == 1
        assert 'Cookie' in calls[0] and not store.get('media_errors')
    finally:
        store.close()


def test_url_refresh_reopens_only_selected_videos_and_reactions(tmp_path):
    SyncExporter(tmp_path, Journal(), ONE).run()
    store = Store(tmp_path)
    try:
        with store.db:
            store.set('videos', {'https://video.example/a.mp4': unavailable(entries=[1]),
                                 'https://video.example/b.mp4': unavailable(entries=[2])})
            store.set('reaction_count_attempts', {'post:1': ATTEMPTS, 'comment:11': ATTEMPTS, 'post:2': ATTEMPTS})
            for eid in (1, 2):
                store.save_reactions('post', eid, {'count': 1, 'groups': [1], 'authors': unavailable(users=[])})
    finally:
        store.close()
    exporter = SyncExporter(tmp_path, Journal(), ONE)
    try:
        exporter.prepare()
        assert not closed(exporter.store.get('videos')['https://video.example/a.mp4'])
        assert closed(exporter.store.get('videos')['https://video.example/b.mp4'])
        assert exporter.store.get('reaction_count_attempts') == {'post:2': ATTEMPTS}
        assert not closed(exporter.store.reactions('post')[1]['authors'])
        assert closed(exporter.store.reactions('post')[2]['authors'])
    finally:
        exporter.store.close()


def test_failed_author_response_retains_previously_received_people(tmp_path):
    from ljarchive.reactions import capture_authors
    class Client(Journal):
        def reaction_authors(self, entities): return [{'error': 'No author list'}]
    store = Store(tmp_path)
    person = {'id': 8, 'username': 'reader', 'type': 0}
    try:
        with store.db:
            store.save_reactions('post', 1, {'count': 2, 'groups': [2],
                                 'authors': {'status': 'partial', 'users': [person]}})
        capture_authors(store, Client(), [((123, 263, 0), 'post', 1)])
        authors = store.reactions('post')[1]['authors']
        assert authors['users'] == [person]
        assert authors['status'] != 'complete' and authors['attempts'] == 1
    finally:
        store.close()


@pytest.mark.parametrize('failure', ['storage', 'login'])
def test_local_storage_and_missing_login_never_become_remote_absence(tmp_path, monkeypatch, failure):
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda *a: True)
    monkeypatch.setattr(requests.Session, 'get', lambda *a, **k: ImageResponse(
        status=403 if failure == 'login' else 200, content=b'image', headers={'Content-Type': 'image/png'}))
    if failure == 'storage':
        def fail_write(*a): raise OSError('Disk full')
        monkeypatch.setattr('ljarchive.media.atomic_write', fail_write)
    media = Media(tmp_path, True, threading.Event())
    try:
        media.get(PICTURE); media.retry_failed()
        assert not closed(media.errors[PICTURE])
        assert media.errors[PICTURE].get('attempts', 0) == 0
    finally:
        media.close()


def test_http_failures_have_one_durable_attempt_per_pass(tmp_path, monkeypatch):
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda *a: True)
    calls = []
    def get(*a, **k):
        calls.append(1); return ImageResponse(status=503)
    monkeypatch.setattr(requests.Session, 'get', get)
    media = Media(tmp_path, True, threading.Event())
    store = Store(tmp_path)
    try:
        media.get(PICTURE); media.retry_failed()
        assert len(calls) == ATTEMPTS
        assert closed(media.errors[PICTURE])
        with store.db: media.save_state(store)
        again = Media(tmp_path, True, threading.Event())
        try:
            again.load_state(store); again.get(PICTURE); again.retry_failed()
            assert len(calls) == ATTEMPTS
        finally: again.close()
    finally:
        media.close(); store.close()


def test_video_repeats_finish_in_current_run(tmp_path, monkeypatch):
    from yt_dlp.utils import DownloadError
    class WithVideo(Journal):
        def entry(self, eid):
            return {**super().entry(eid), 'event': '<a href="https://youtu.be/XXXXXXXXXXX">Video</a>'}
    calls = []
    def extract(*a, **k):
        calls.append(1); raise DownloadError('Fixture video unavailable')
    monkeypatch.setattr('yt_dlp.YoutubeDL.extract_info', extract)
    report = SyncExporter(tmp_path, WithVideo(), ONE, videos=True).run()
    assert len(calls) == ATTEMPTS
    assert report['status'] == 'complete' and report['videos']['unavailable']


def test_recovery_attempt_limit_does_not_collide_with_history(tmp_path):
    from ljarchive.server import Jobs
    SyncExporter(tmp_path / 'fixture', Journal(), ONE).run()
    plan = Jobs(tmp_path).recovery_plan('fixture')
    assert isinstance(plan['attempts'], list)
    assert plan['max_attempts'] == ATTEMPTS


def test_invalid_image_address_does_not_interrupt_remaining_downloads(tmp_path):
    media = Media(tmp_path, True, threading.Event())
    try:
        assert media.get('https://[invalid/image.png') is None
        assert media.errors['https://[invalid/image.png']['reason'] == 'invalid_url'
        assert not media.stopped
    finally:
        media.close()
