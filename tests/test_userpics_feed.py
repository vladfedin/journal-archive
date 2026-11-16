import hashlib
import json
import threading
from pathlib import Path

import pytest
import requests

from ljarchive.client import LiveJournalClient, RequestBudget
from ljarchive.data import snapshot
from ljarchive.demo import create_demo
from ljarchive.exporter import normalize_thread
from ljarchive.media import Media
from ljarchive.render import build_archive, safe_content
from ljarchive.storage import Store
from ljarchive.userpics import resolve
from test_limited_export import Response


def test_login_requests_keyword_catalog_without_persisting_other_login_fields(monkeypatch):
    client = LiveJournalClient('fixture', 'password')
    calls = []
    def call(method, **params):
        calls.append((method, params))
        return {'userid': 1, 'pickws': ['улыбка'], 'pickwurls': ['https://example.org/smile.png'],
                'defaultpicurl': 'https://example.org/default.png', 'unrelated_secret': 'never-copy',
                'ljsession': 'test-session'}
    monkeypatch.setattr(client, 'call', call)
    try:
        client.login()
        assert calls[0][1]['getpickws'] == calls[0][1]['getpickwurls'] == 1
        assert 'never-copy' not in json.dumps(client.userpics)
    finally:
        client.close()


def test_selected_comment_pictures_are_preserved_per_reply_and_unknown_is_not_default():
    nodes = [{'dtalkid': 263, 'posterid': 5, 'postername': 'friend', 'body': 'one', 'userpic': 'https://example.org/one.png'},
             {'dtalkid': 519, 'posterid': 5, 'postername': 'friend', 'body': 'two', 'poster_userpic_url': 'https://example.org/two.png'}]
    records, users = normalize_thread(nodes, 1)
    assert users == {5: 'friend'}
    assert resolve(records[0], 'comment')['url'].endswith('/one.png')
    assert resolve(records[1], 'comment')['url'].endswith('/two.png')
    catalog = {'defaultpicurl': 'https://example.org/default.png', 'pickws': ['улыбка'], 'pickwurls': ['https://example.org/smile.png']}
    assert resolve({'props': {'picture_keyword': 'улыбка'}}, 'post', catalog)['url'].endswith('/smile.png')
    assert resolve({'props': {'picture_keyword': 'удалённая'}}, 'post', catalog)['selection'] == 'unknown'
    assert resolve({'posterid': 5}, 'comment', catalog)['url'] is None
    assert resolve({'userpic': '', 'posterid': 5}, 'comment', catalog)['selection'] == 'none'
    assert resolve({'userpic': 'file:///etc/passwd'}, 'comment')['url'] is None


def test_userpics_download_without_post_images_and_resume_only_missing_file(tmp_path, monkeypatch):
    report = create_demo(tmp_path)
    store = Store(tmp_path)
    first, second = 'https://example.org/first.png', 'https://example.org/second.png'
    with store.db:
        entry = store.entries()[0]; entry['poster_userpic_url'] = first; store.save_entry(entry)
        for comment in store.comments()[:2]:
            comment['poster_userpic_url'] = second; store.save_comment(comment)
    source_before = [(e['itemid'], e['event']) for e in store.entries()]
    calls = []
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda self, seconds: True)
    def get(self, url, **kwargs):
        calls.append(url)
        if url == second:
            raise requests.HTTPError('404 unavailable')
        return Response(content=b'picture', headers={'Content-Type': 'image/png'})
    monkeypatch.setattr('requests.Session.get', get)
    budget = RequestBudget(2)
    try:
        build_archive(store, report, images=False, userpics=True, budget=budget)
        assert calls == [first, second] and budget.used == 2
        data = snapshot(store)
        assert data['userpics']['post'][str(entry['itemid'])]['path'].startswith('media/')
        assert data['userpics']['comment']['1']['path'] is None
        calls.clear()
        monkeypatch.setattr('requests.Session.get', lambda self, url, **kw: calls.append(url) or Response(content=b'recovered', headers={'Content-Type': 'image/png'}))
        build_archive(store, report, images=False, userpics=True, budget=RequestBudget(1))
        assert calls == [second]
        data = snapshot(store)
        assert data['userpics']['comment']['1']['path'] == data['userpics']['comment']['2']['path']
        assert [(e['itemid'], e['event']) for e in store.entries()] == source_before
        assert 'class="userpic"' in (tmp_path / 'entries/1.html').read_text(encoding="utf-8")
    finally:
        store.close()


def test_userpics_obey_cap_and_offline_rebuild_never_fetches(tmp_path, monkeypatch):
    report = create_demo(tmp_path)
    store = Store(tmp_path)
    with store.db:
        for e in store.entries():
            e['poster_userpic_url'] = f'https://example.org/{e["itemid"]}.png'; store.save_entry(e)
    calls = []
    monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
    monkeypatch.setattr(Media, 'wait', lambda self, seconds: True)
    monkeypatch.setattr('requests.Session.get', lambda self, url, **kw: calls.append(url) or Response(content=b'pic', headers={'Content-Type': 'image/png'}))
    try:
        budget = RequestBudget(2)
        build_archive(store, report, images=False, userpics=True, budget=budget)
        assert len(calls) == budget.used == 2 and budget.exhausted
        monkeypatch.setattr('requests.Session.get', lambda *a, **k: pytest.fail('Offline rebuild must not fetch'))
        before = snapshot(store)
        build_archive(store, report)
        assert snapshot(store) == before
    finally:
        store.close()


def test_lj_cut_is_collapsible_in_feed_open_in_post_and_keeps_original_json(tmp_path):
    report = create_demo(tmp_path)
    store = Store(tmp_path)
    try:
        entry = next(e for e in store.entries() if e['itemid'] == 6)
        assert '<lj-cut' in entry['event']
        page = (tmp_path / 'entries/6.html').read_text(encoding="utf-8")
        feed = (tmp_path / 'assets/posts/6.js').read_text(encoding="utf-8")
        html = json.loads(feed[len('window.ArchivePost(6,'):-2])
        assert '<details class="lj-cut" open="">' in page
        assert '<details class="lj-cut">' in html and '<details class="lj-cut" open' not in html
        assert 'src="media/' in html and 'src="../media/' not in html
        assert '<lj-cut' in snapshot(store)['entries'][-1]['event']
        media = Media(tmp_path, False, threading.Event())
        try:
            unsafe = '<lj-cut text="&lt;script&gt;">Body<script>bad()</script><lj-cut>Nested</lj-cut>'
            clean = str(safe_content(unsafe, 'https://example.org/', {}, media, set()))
            assert clean.count('<details') == clean.count('</details>') == 2
            assert '<script' not in clean and 'Nested' in clean
        finally:
            media.close()
    finally:
        store.close()


def test_same_avatar_outside_period_does_not_prevent_selected_entry_download(tmp_path, monkeypatch):
    report = create_demo(tmp_path)
    store = Store(tmp_path)
    url = 'https://example.org/shared.png'
    try:
        with store.db:
            for entry in store.entries():
                entry['poster_userpic_url'] = url; store.save_entry(entry)
        report['sync'] = {'selected': [6], 'scope': {'mode': 'urls'}, 'selected_count': 1}
        monkeypatch.setattr('ljarchive.media.public_url', lambda _: None)
        monkeypatch.setattr(Media, 'wait', lambda self, seconds: True)
        calls = []
        monkeypatch.setattr('requests.Session.get', lambda self, value, **kw: calls.append(value) or Response(content=b'pic', headers={'Content-Type': 'image/png'}))
        build_archive(store, report, userpics=True, budget=RequestBudget(1))
        assert calls == [url]
        assert report['userpics']['complete']
        assert 'class="userpic"' in (tmp_path / 'entries/6.html').read_text(encoding="utf-8")
    finally:
        store.close()
