import hashlib
import io
import json
import threading
import time

import pytest
import requests

from ljarchive.client import ExportError
from ljarchive.on_demand import download_one
from ljarchive.render import build_archive
from ljarchive.storage import Store
from test_media_errors import ImageResponse
from test_render_server import local_server

URL = 'https://images.example/music.png'


def fixture(folder):
    store = Store(folder)
    with store.db:
        store.set('username', 'fixture')
        store.save_entry({'itemid': 1, 'anum': 1, 'subject': 'Музыка', 'eventtime': '2024-02-29 12:00:00',
                          'event': f'<p>Обложка</p><img src="{URL}" alt="Обложка"><lj-cut><lj-embed id="235"/></lj-cut>'})
        store.save_comment({'id': 10, 'jitemid': 1, 'body': '<a href="https://youtu.be/D-7x7LlUXms">Трек</a>'})
    report = {'username': 'fixture', 'status': 'partial', 'created_at': '2026-01-01T00:00:00Z',
              'entries_saved': 1, 'comments_saved': 1, 'missing_comments': []}
    build_archive(store, report)
    store.close()


def wait(queue, task_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = queue.status(task_id)
        if result['phase'] in ('complete', 'error', 'needs_auth', 'cancelled') and not queue.running: return result
        time.sleep(0.02)
    pytest.fail(str(queue.status(task_id)))


def image_network(monkeypatch):
    calls = []
    monkeypatch.setattr('ljarchive.media.public_url', lambda url: None)
    monkeypatch.setattr('ljarchive.media.Media.wait', lambda self, _: not self.cancel.is_set())
    def get(self, url, **kwargs):
        calls.append(url)
        return ImageResponse(content=b'fixture-picture', headers={'Content-Type': 'image/png'})
    monkeypatch.setattr(requests.Session, 'get', get)
    return calls


def test_download_selected_image_persists_html_json_and_original_texts(tmp_path, monkeypatch):
    fixture(tmp_path)
    original = json.loads((tmp_path / 'data/archive.json').read_text())
    calls = image_network(monkeypatch)
    store = Store(tmp_path)
    try:
        update = []
        result = download_one(store, {'entry_id': 1, 'kind': 'image', 'value': URL}, threading.Event(), lambda **k: update.append(k))
        assert (tmp_path / result['path']).read_bytes() == b'fixture-picture'
        assert update[-1]['phase'] == 'saving' and calls == [URL]
        build_archive(store, json.loads((tmp_path / 'export-report.json').read_text()))
        source = json.loads((tmp_path / 'data/archive.json').read_text())
        assert source['entries'] == original['entries'] and source['comments'] == original['comments']
        assert source['media'][URL] == result['path']
        assert '../' + result['path'] in (tmp_path / 'entries/1.html').read_text()
        download_one(store, {'entry_id': 1, 'kind': 'image', 'value': URL}, threading.Event(), lambda **k: None)
        assert calls == [URL]  # Cached material does not consume more network requests.
    finally: store.close()


def test_lj_wrapper_resolves_music_widget_without_archiving_audio_or_tokens(tmp_path, monkeypatch):
    fixture(tmp_path)
    calls = []
    def page(client, method, path, **kwargs):
        calls.append(('post', path, kwargs.get('headers')))
        return b'<iframe src="https://l.lj-toys.com/?journalid=5&moduleid=235&auth_token=SECRET"></iframe>'
    def wrapper(manager, request):
        calls.append(('wrapper', request))
        return io.BytesIO(b'<iframe src="https://music.yandex.ru/iframe/album/37511474?utm_source=lj"></iframe>')
    monkeypatch.setattr('ljarchive.client.LiveJournalClient._request', page)
    monkeypatch.setattr('ljarchive.video.VideoArchive.open', wrapper)
    monkeypatch.setattr('ljarchive.video.VideoArchive.download', lambda *a: pytest.fail('A music widget is not a saved video'))
    store = Store(tmp_path)
    try:
        result = download_one(store, {'entry_id': 1, 'kind': 'embed', 'value': '235'}, threading.Event(), lambda **k: None)
        assert result['kind'] == 'external'
        assert result['status'] == 'online_only' and len(calls) == 2
        assert 'SECRET' not in json.dumps(store.get('external_embeds'))
        assert not store.get('videos')
        build_archive(store, json.loads((tmp_path / 'export-report.json').read_text()))
        body = (tmp_path / 'entries/1.html').read_text()
        assert 'external-widget' in body and 'аудиофайл не сохранён' in body and '<iframe' not in body
        from ljarchive.activitystreams import source_snapshot
        assert source_snapshot(json.loads((tmp_path/'data/activitystreams.json').read_text())) == json.loads((tmp_path/'data/archive.json').read_text())
    finally: store.close()


def test_queue_waits_for_export_and_deduplicates_clicks(local_server, monkeypatch):
    server, _ = local_server
    folder = server.jobs.output / 'fixture'; fixture(folder)
    calls = image_network(monkeypatch)
    gate = threading.Event()
    server.jobs.thread = threading.Thread(target=lambda: gate.wait(10)); server.jobs.thread.start()
    request = {'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': URL}
    first = server.jobs.materials.start(request)
    second = server.jobs.materials.start(request)
    assert first['id'] == second['id']
    assert server.jobs.materials.status(first['id'])['phase'] == 'queued' and calls == []
    gate.set(); server.jobs.thread.join()
    result = wait(server.jobs.materials, first['id'])
    assert result['phase'] == 'complete', result
    assert calls == [URL]
    assert server.jobs.snapshot() == {'phase': 'idle'}
    source = json.loads((folder/'data/archive.json').read_text())
    assert source['media'][URL] == result['asset']['path']


def test_queue_failure_remains_terminal_after_rebuilding_pages(local_server, monkeypatch):
    server, _ = local_server
    fixture(server.jobs.output / 'fixture')
    def refuse(*args, **kwargs): raise ExportError('Fixture HTTP 403')
    monkeypatch.setattr('ljarchive.on_demand.download_one', refuse)
    request = {'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': URL}
    task = server.jobs.materials.start(request)
    result = wait(server.jobs.materials, task['id'])
    assert result['phase'] == 'error' and result['message'] == 'Fixture HTTP 403'
    again = server.jobs.materials.start(request)
    assert again['id'] != task['id']
    assert wait(server.jobs.materials, again['id'])['phase'] == 'error'


@pytest.mark.parametrize('content_complete', [True, False])
def test_last_image_finishes_only_a_media_only_partial_archive(local_server, monkeypatch, content_complete):
    server, _ = local_server
    folder = server.jobs.output / 'fixture'; fixture(folder)
    store = Store(folder)
    with store.db:
        entry = store.entries()[0]; entry.pop('anum', None); entry['event'] = f'<img src="{URL}">'
        store.save_entry(entry)
        store.set('posts_done', True); store.set('bodies_done', content_complete)
        store.set('entry_materials', {'1': {'urls': [URL]}})
    store.close()
    report = json.loads((folder / 'export-report.json').read_text())
    report.update(status='partial', images_requested=True, issues=['Не все изображения получены.'])
    (folder / 'export-report.json').write_text(json.dumps(report))
    image_network(monkeypatch)
    task = server.jobs.materials.start({'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': URL})
    assert wait(server.jobs.materials, task['id'])['phase'] == 'complete'
    updated = json.loads((folder / 'export-report.json').read_text())
    assert updated['media_complete']
    assert updated['status'] == ('complete' if content_complete else 'partial')
    if content_complete: assert 'Не все изображения получены.' not in updated['issues']


def test_material_api_rejects_foreign_urls_cross_origin_and_bad_ids(local_server):
    server, base = local_server
    fixture(server.jobs.output/'fixture')
    data = {'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': 'http://127.0.0.1/secret'}
    with requests.Session() as session:
        session.get(base)
        assert session.post(base+'/api/material', json=data, headers={'Origin':'https://evil.example'}).status_code == 403
        assert session.post(base+'/api/material', json=data, headers={'Origin':base}).status_code == 400
        data.update(kind='embed', value='999')
        assert session.post(base+'/api/material', json=data, headers={'Origin':base}).status_code == 400
        assert session.post(base+'/api/material/cancel', json={'id':{}}, headers={'Origin':base}).status_code == 400
        assert not server.jobs.materials.states


def test_video_download_from_a_comment_link_keeps_an_offline_player(tmp_path, monkeypatch):
    fixture(tmp_path)
    url='https://www.youtube.com/watch?v=D-7x7LlUXms'
    def download(manager, value, eid):
        assert value == url and eid == 1
        path='media/'+hashlib.sha256(value.encode()).hexdigest()+'.mp4'
        (tmp_path/'media').mkdir(exist_ok=True);(tmp_path/path).write_bytes(b'video')
        manager.records[value]={'status':'saved','path':path,'entries':[eid]};manager.save()
    monkeypatch.setattr('ljarchive.video.VideoArchive.download',download)
    store=Store(tmp_path)
    try:
        result=download_one(store,{'entry_id':1,'kind':'video','value':url},threading.Event(),lambda **k:None)
        assert result['kind']=='video'
        build_archive(store,json.loads((tmp_path/'export-report.json').read_text()))
        assert 'data-video-path="../'+result['path']+'"' in (tmp_path/'entries/1.html').read_text()
    finally:store.close()


@pytest.mark.parametrize('outcome', ['cancelled', 'request_limit'])
def test_image_stop_is_not_misreported_as_success(tmp_path, monkeypatch, outcome):
    fixture(tmp_path)
    calls = image_network(monkeypatch)
    cancel = threading.Event()
    if outcome == 'cancelled': cancel.set()
    else:
        def get(self, url, **kwargs):
            calls.append(url)
            return ImageResponse(status=302, headers={'Location': URL+'?next=1'})
        monkeypatch.setattr(requests.Session, 'get', get)
    store = Store(tmp_path)
    try:
        from ljarchive.client import Cancelled
        with pytest.raises(Cancelled if outcome == 'cancelled' else ExportError):
            download_one(store, {'entry_id':1,'kind':'image','value':URL,'max_requests':1}, cancel, lambda **k:None)
        assert store.get('media_errors')[URL]['reason'] == outcome
        assert len(calls) == (0 if outcome == 'cancelled' else 1)
        assert not store.get('media')[URL]
    finally: store.close()


def test_authentication_is_optional_and_password_is_not_retained(tmp_path, monkeypatch):
    fixture(tmp_path)
    from ljarchive.client import AuthenticationRequired
    calls=[]
    monkeypatch.setattr('ljarchive.client.LiveJournalClient.login', lambda client: calls.append('login'))
    def resolve(manager, entry, modules):
        if not calls: raise AuthenticationRequired('ЖЖ отказал в доступе (HTTP 401/403).')
        manager.external['https://fixture.livejournal.com/257.html#lj-embed-235'] = {
            'provider':'Яндекс Музыка','kind':'audio_service','embed_url':'https://music.yandex.ru/iframe/album/1',
            'url':'https://music.yandex.ru/album/1','status':'online_only'}
        manager.save()
    monkeypatch.setattr('ljarchive.video.VideoArchive.resolve', resolve)
    store=Store(tmp_path)
    request={'entry_id':1,'kind':'embed','value':'235'}
    updates=[]
    try:
        with pytest.raises(AuthenticationRequired):download_one(store,request,threading.Event(),lambda **k:updates.append(k))
        request['password']='test-local-secret'
        result=download_one(store,request,threading.Event(),lambda **k:updates.append(k))
        assert result['kind']=='external' and calls==['login']
        assert 'password' not in request and 'test-local-secret' not in json.dumps(updates)
        assert 'test-local-secret' not in json.dumps(store.get('external_embeds'))
    finally: store.close()


def test_resolved_module_video_can_be_selected_without_auth_again(tmp_path, monkeypatch):
    fixture(tmp_path)
    from ljarchive.on_demand import validate
    store=Store(tmp_path)
    try:
        url='https://www.youtube.com/watch?v=XXXXXXXXXXX'
        with store.db:store.set('video_embeds',{'https://fixture.livejournal.com/257.html#lj-embed-235':url})
        assert validate(store,{'entry_id':1,'kind':'video','value':url})['itemid']==1
    finally:store.close()


def test_cancel_queued_request_drops_password_without_network(local_server, monkeypatch):
    server,_=local_server
    fixture(server.jobs.output/'fixture')
    gate=threading.Event()
    server.jobs.thread=threading.Thread(target=lambda:gate.wait(10));server.jobs.thread.start()
    try:
        request=server.jobs.materials.start({'archive':'fixture','entry_id':1,'kind':'embed','value':'235','password':'local-secret'})
        assert 'local-secret' not in json.dumps(request)
        server.jobs.materials.stop(request['id'])
        assert not server.jobs.materials.queue
        assert server.jobs.materials.status(request['id'])['phase']=='cancelled'
    finally:
        gate.set();server.jobs.thread.join()
        if server.jobs.materials.thread:server.jobs.materials.thread.join(2)
