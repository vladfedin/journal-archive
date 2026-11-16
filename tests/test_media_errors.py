import json
import threading

import pytest
import requests

from ljarchive.client import RequestBudget
from ljarchive.media import Media
from ljarchive.render import build_archive
from ljarchive.storage import Store
from test_limited_export import Response


class ImageResponse(Response):
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def downloader(folder, monkeypatch, limit=50):
    media = Media(folder, True, threading.Event(), RequestBudget(limit))
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(media, 'wait', lambda _: True)
    return media


def test_forbidden_image_does_not_block_other_images_on_same_host(tmp_path, monkeypatch):
    media = downloader(tmp_path, monkeypatch)
    calls = []
    denied, good = 'https://images.example/locked.png', 'https://images.example/open.png'
    def get(url, **kwargs):
        calls.append(url)
        return ImageResponse(status=403 if url == denied else 200,
                             content=b'image', headers={'Content-Type': 'image/png'})
    monkeypatch.setattr(media.session, 'get', get)
    assert media.get(denied) is None
    assert media.get(good).startswith('media/')
    assert media.get(denied) is None  # One attempt per URL during this run.
    assert calls == [denied, good] and media.budget.used == 2
    assert media.errors[denied]['reason'] == 'http'
    assert media.errors[denied]['http_status'] == 403
    assert media.errors[denied]['requests'] == 1
    assert (tmp_path / media.mapping[good]).read_bytes() == b'image'
    media.close()


@pytest.mark.parametrize('failure,reason,code', [
    (ImageResponse(status=503), 'http', 503),
    (requests.Timeout(), 'timeout', None),
    (ImageResponse(headers={'Content-Type': 'text/html'}), 'unsupported_type', 200),
])
def test_diagnostics_keep_actual_failure_not_redirect_error(tmp_path, monkeypatch, failure, reason, code):
    media = downloader(tmp_path, monkeypatch)
    def get(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(media.session, 'get', get)
    url = 'https://images.example/a.png'
    assert media.get(url) is None
    assert media.errors[url]['reason'] == reason
    assert media.errors[url]['http_status'] == code
    assert media.errors[url]['requests'] == (1 if reason == 'unsupported_type' else 6)
    if reason != 'unsupported_type':
        pending = 'https://images.example/b.png'
        assert media.get(pending) is None
        assert media.errors[pending]['reason'] == 'paused'
        assert media.errors[pending]['checked_at'] is None
    media.close()


def test_budget_and_cancel_are_not_http_failures(tmp_path, monkeypatch):
    media = downloader(tmp_path, monkeypatch, limit=1)
    media.budget.consume('livejournal')
    monkeypatch.setattr(media.session, 'get', lambda *a, **kw: pytest.fail('No request allowed'))
    for url in ['https://images.example/first.png', 'https://images.example/next.png']:
        assert media.get(url) is None
        assert media.errors[url]['status'] == 'pending'
        assert media.errors[url]['reason'] == 'request_limit'
        assert media.errors[url]['requests'] == 0
    media.cancel.set()
    assert media.get('https://images.example/cancel.png') is None
    assert media.errors['https://images.example/cancel.png']['reason'] == 'cancelled'
    media.close()


def test_missing_cached_file_is_downloaded_again(tmp_path, monkeypatch):
    media = downloader(tmp_path, monkeypatch)
    url = 'https://images.example/a.png'
    media.mapping[url] = 'media/missing.png'
    monkeypatch.setattr(media.session, 'get', lambda *a, **kw: ImageResponse(content=b'image', headers={'Content-Type': 'image/png'}))
    path = media.get(url)
    assert path != 'media/missing.png' and (tmp_path / path).read_bytes() == b'image'
    assert media.budget.used == 1
    media.close()


def test_image_error_survives_rebuild_restart_and_clears_after_recovery(tmp_path, monkeypatch):
    url = 'https://images.example/locked.png'
    store = Store(tmp_path)
    with store.db:
        store.set('username', 'fixture')
        store.save_entry({'itemid': 1, 'event': f'<img src="{url}">', 'eventtime': '2020-01-01 00:00:00'})
    report = {'username': 'fixture', 'status': 'partial', 'created_at': '2026-01-01T00:00:00Z',
              'entries_saved': 1, 'comments_saved': 0, 'missing_comments': []}
    media = downloader(tmp_path, monkeypatch)
    monkeypatch.setattr(media.session, 'get', lambda *a, **kw: ImageResponse(status=403))
    media.get(url)
    with store.db:
        media.save_state(store)  # The checkpoint must survive even before rendering.
    error = dict(media.errors[url])
    media.close()
    store.close()
    store = Store(tmp_path)
    monkeypatch.setattr(requests.Session, 'get', lambda *a, **kw: pytest.fail('Offline rebuild'))
    build_archive(store, report)
    assert report['images_failed'] == [url]
    assert report['media_errors'][url]['checked_at'] == error['checked_at']
    assert 'HTTP 403' in (tmp_path / 'report.html').read_text(encoding='utf-8')
    source = json.loads((tmp_path / 'data/archive.json').read_text(encoding='utf-8'))
    assert source['media_errors'][url] == error
    from ljarchive.activitystreams import source_snapshot
    assert source_snapshot(json.loads((tmp_path / 'data/activitystreams.json').read_text(encoding='utf-8'))) == source
    media = downloader(tmp_path, monkeypatch)
    monkeypatch.setattr(media.session, 'get', lambda *a, **kw: ImageResponse(content=b'image', headers={'Content-Type': 'image/png'}))
    build_archive(store, report, images=True, media=media)
    assert not report['images_failed'] and not report['media_errors']
    assert not store.get('media_errors') and report['images_saved'] == 1
    assert not any('Не удалось сохранить' in issue for issue in report['issues'])
    store.close()
