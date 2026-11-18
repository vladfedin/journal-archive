import json
import threading

from ljarchive.gallery import inventory, write_gallery
from ljarchive.personal import download_profile, refresh_profile
from ljarchive.storage import Store
from test_personal import PersonalClient


def test_gallery_links_images_and_videos_to_every_post_and_comment(tmp_path):
    image = 'https://example.org/image.png'
    video = 'https://www.youtube.com/watch?v=D-7x7LlUXms'
    store = Store(tmp_path)
    try:
        (tmp_path / 'media').mkdir()
        (tmp_path / 'media/picture.png').write_bytes(b'picture')
        with store.db:
            store.set('username', 'fixture')
            store.set('media', {image: 'media/picture.png'})
            for eid in (1, 2):
                store.save_entry({'itemid': eid, 'anum': 1, 'subject': f'Post {eid}',
                                  'eventtime': f'2025-10-0{eid} 12:00:00', 'event': f'<img src="{image}">'})
            store.save_comment({'id': 10, 'jitemid': 1, 'body': f'<img src="{image}"><a href="{video}">Watch</a>'})
        rows = inventory(store)
        picture = next(r for r in rows if r['kind'] == 'image')
        assert picture['status'] == 'saved'
        assert {r['href'] for r in picture['references']} == {'entries/1.html', 'entries/2.html', 'entries/1.html#comment-10'}
        clip = next(r for r in rows if r['kind'] == 'video')
        assert clip['status'] == 'missing' and clip['references'][0]['comment_id'] == 10
        write_gallery(store, {'username': 'fixture'})
        content = (tmp_path / 'materials.html').read_text()
        assert '<a href="entries/2.html"><img' in content
        assert 'data-material-kind="video"' in content and 'entries/1.html#comment-10' in content
        assert json.loads((tmp_path / 'data/materials.json').read_text())['materials'] == rows
        with store.db: store.set('media', {image: '../outside.png'})
        assert next(r for r in inventory(store) if r['kind'] == 'image')['path'] is None
    finally: store.close()


def test_gallery_pagination_does_not_leave_obsolete_pages(tmp_path):
    store = Store(tmp_path)
    try:
        with store.db:
            store.set('username', 'fixture')
            store.save_entry({'itemid': 1, 'event': ''.join(f'<img src="https://example.org/{n}.png">' for n in range(49))})
        write_gallery(store, {'username': 'fixture'})
        assert (tmp_path / 'materials-image-2.html').exists()
        with store.db: store.save_entry({'itemid': 1, 'event': ''})
        write_gallery(store, {'username': 'fixture'})
        assert not (tmp_path / 'materials-image-2.html').exists()
        assert (tmp_path / 'materials.html').exists()
    finally: store.close()


def test_profile_download_preserves_post_queue_and_refreshes_only_personal_pages(tmp_path, monkeypatch):
    fixture = PersonalClient()
    def login(client):
        client.profile = fixture.profile; client.userpics = {}; client.journal_id = 123
        client._cookie = 'fixture-session'; client.source['id'] = 123; client.source['access']['status'] = 'verified'
    monkeypatch.setattr('ljarchive.client.LiveJournalClient.login', login)
    monkeypatch.setattr('ljarchive.client.LiveJournalClient.call', lambda self, method, **kw: fixture.call(method, **kw))
    monkeypatch.setattr('ljarchive.client.LiveJournalClient.personal_page', lambda self, path: fixture.personal_page(path))
    monkeypatch.setattr('ljarchive.client.LiveJournalClient.personal_feed', lambda self, name: fixture.personal_feed(name))
    monkeypatch.setattr('ljarchive.media.Media.get', lambda *a, **k: None)
    store = Store(tmp_path)
    try:
        state = {'phase': 'comments', 'seen': [1], 'scope': {'mode': 'full'}}
        with store.db:
            store.set('username', 'fixture'); store.set('sync_job', state)
            store.save_entry({'itemid': 1, 'event': 'Original post'})
            store.save_comment({'id': 2, 'jitemid': 1, 'body': 'Original comment'})
        (tmp_path / 'entries').mkdir(); (tmp_path / 'entries/1.html').write_text('existing HTML')
        request = {'password': 'do-not-persist', 'max_requests': 50}
        asset = download_profile(store, request, threading.Event(), lambda **v: None)
        assert asset['kind'] == 'profile' and 'password' not in request
        assert store.get('sync_job') == state
        assert all(method not in ('getevents', 'getcomments', 'getinbox') for method, _ in fixture.calls)
        refresh_profile(store)
        assert (tmp_path / 'entries/1.html').read_text() == 'existing HTML'
        assert store.get('sync_job') == state
        content = (tmp_path / 'account.html').read_text()
        assert 'reader' in content and 'Часовой пояс' in content and 'UTC' in content
        assert '<pre' not in content and 'do-not-persist' not in content
        data = json.loads((tmp_path / 'data/archive.json').read_text())
        assert data['entries'][0]['event'] == 'Original post'
        assert data['comments'][0]['body'] == 'Original comment'
        assert data['account']['sections']['friends']['data']['friends'][0]['username'] == 'reader'
    finally: store.close()


def test_completed_profile_clears_its_own_incomplete_status_only(tmp_path):
    from ljarchive.personal import Personal
    from types import SimpleNamespace
    from ljarchive.storage import atomic_write, dumps
    store = Store(tmp_path)
    try:
        with store.db:
            store.set('username', 'fixture'); store.set('personal_requested', {'account': True})
        atomic_write(tmp_path / 'export-report.json', dumps({'username': 'fixture', 'status': 'complete', 'issues': []}))
        refresh_profile(store)
        report = json.loads((tmp_path / 'export-report.json').read_text())
        assert report['status'] == 'partial' and report['personal']['complete_baseline']
        collector = Personal(SimpleNamespace(store=store, client=None))
        for key in ('profile', 'foaf', 'friends', 'tags', 'userpics', 'settings_display', 'settings_privacy', 'settings_notifications'):
            collector.save(key, status='captured', data={})
        refresh_profile(store)
        report = json.loads((tmp_path / 'export-report.json').read_text())
        assert report['personal']['complete'] and report['status'] == 'complete'
        assert not report['issues']
        # A separate content failure must not be erased by updating a profile.
        collector.save('friends', status='partial', error='Missing')
        refresh_profile(store)
        report = json.loads((tmp_path / 'export-report.json').read_text())
        report['issues'].append('Не все комментарии получены.')
        atomic_write(tmp_path / 'export-report.json', dumps(report))
        collector.save('friends', status='captured', data={'friends': [], 'friendofs': []})
        refresh_profile(store)
        report = json.loads((tmp_path / 'export-report.json').read_text())
        assert report['personal']['complete'] and report['status'] == 'partial'
        assert report['issues'] == ['Не все комментарии получены.']
    finally: store.close()
