import hashlib
import json
from pathlib import Path

from ljarchive.client import ExportError
from ljarchive.integrity import verify_archive
from ljarchive.on_demand import download_one
from ljarchive.render import build_archive
from ljarchive.server import Jobs
from ljarchive.storage import Store
from test_on_demand import image_network, wait, URL


def prepare(root):
    folder = root / 'fixture'; store = Store(folder)
    with store.db:
        store.set('username', 'fixture')
        for eid in range(1, 9):
            store.save_entry({'itemid': eid, 'subject': str(eid), 'eventtime': f'2024-01-{eid:02d} 12:00:00',
                              'event': f'<img src="{URL}">' if eid in (1, 4) else 'Unchanged',
                              'reactions': {'groups': [], 'authors': {'status': 'complete'}, 'total': 0}})
        store.set('entry_materials', {'1': {'urls': [URL]}, '4': {'urls': [URL]}})
    report = {'username': 'fixture', 'status': 'complete', 'entries_saved': 8, 'comments_saved': 0}
    build_archive(store, report); store.close()
    return folder


def test_only_references_change_and_manifest_json_feed_remain_consistent(tmp_path, monkeypatch):
    folder = prepare(tmp_path)
    before = {p.relative_to(folder).as_posix(): (p.stat().st_mtime_ns, p.read_bytes()) for p in folder.rglob('*') if p.is_file() and p.suffix in ('.html', '.js', '.json')}
    calls = image_network(monkeypatch)
    hashed = []
    from ljarchive.integrity import file_hash
    def track(path, **kwargs):
        hashed.append(path.relative_to(folder).as_posix())
        return file_hash(path, **kwargs)
    monkeypatch.setattr('ljarchive.integrity.file_hash', track)
    jobs = Jobs(tmp_path)
    task = jobs.materials.start({'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': URL})
    result = wait(jobs.materials, task['id'])
    assert result['phase'] == 'complete', result
    assert calls == [URL]
    for name in ('entries/2.html','entries/3.html','assets/posts/2.js','assets/search-data.js','statistics.html','assets/statistics-data.js','calendar.html','account.html'):
        assert (folder/name).stat().st_mtime_ns == before[name][0], name
        assert name not in hashed
    for eid in (1, 4):
        assert result['asset']['path'] in (folder/f'entries/{eid}.html').read_text()
        assert result['asset']['path'] in (folder/f'assets/posts/{eid}.js').read_text()
    source = json.loads((folder/'data/archive.json').read_text())
    assert source['media'][URL] == result['asset']['path']
    from ljarchive.activitystreams import source_snapshot
    assert source_snapshot(json.loads((folder/'data/activitystreams.json').read_text())) == source
    store = Store(folder)
    try: assert verify_archive(store)['ok']
    finally: store.close()


def test_failed_material_does_not_rebuild_posts_or_hide_error(tmp_path, monkeypatch):
    folder = prepare(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in (folder/'entries').glob('*.html')}
    monkeypatch.setattr('ljarchive.on_demand.download_one', lambda *a, **k: (_ for _ in ()).throw(ExportError('HTTP 404 · file')))
    jobs = Jobs(tmp_path)
    task = jobs.materials.start({'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': URL})
    state = wait(jobs.materials, task['id'])
    assert state['phase'] == 'error' and state['message'] == 'HTTP 404 · file'
    assert all(p.stat().st_mtime_ns == stamp for p, stamp in before.items())
    store = Store(folder)
    try: assert verify_archive(store)['ok']
    finally: store.close()


def test_scoped_render_keeps_orphan_comment_image_gaps_and_missing_files(tmp_path):
    store = Store(tmp_path)
    with store.db:
        store.set('username','fixture')
        store.save_comment({'id':10,'jitemid':99,'body':f'<img src="{URL}">'})
    report = {'username':'fixture','status':'partial','entries_saved':0,'comments_saved':1}
    try:
        build_archive(store,report)
        assert report['images_unavailable']==1 and not report['media_complete']
        build_archive(store,report,entry_ids=set())
        assert report['images_unavailable']==1 and not report['media_complete']
        with store.db:store.set('media',{URL:'media/missing.png'})
        build_archive(store,report,entry_ids=set())
        assert report['images_unavailable']==1 and not report['media_complete']
        assert verify_archive(store)['ok']
    finally:store.close()


def test_missing_source_urls_are_visible_without_javascript(tmp_path):
    from html.parser import HTMLParser
    import threading
    from ljarchive.media import Media
    from ljarchive.render import safe_content
    media=Media(tmp_path,False,threading.Event())
    youtube='https://youtu.be/D-7x7LlUXms'
    try:
        body=str(safe_content(f'<img src="{URL}" alt="Picture"><iframe src="https://www.youtube.com/embed/XXXXXXXXXXX"></iframe><a href="{youtube}">Song</a>', 'https://fixture.livejournal.com/1.html', {}, media, set()))
        class Sources(HTMLParser):
            def __init__(self):super().__init__();self.sources=[];self.active=None
            def handle_starttag(self, tag, attrs):
                a=dict(attrs)
                if tag=='a' and a.get('class')=='material-source': self.active=[a['href'],'']
            def handle_data(self, text):
                if self.active is not None:self.active[1]+=text
            def handle_endtag(self, tag):
                if tag=='a' and self.active is not None:self.sources.append(self.active);self.active=None
        doc=Sources();doc.feed(body);sources=doc.sources
        assert len(sources)==3 and all(href==text for href,text in sources)
        assert URL in [text for _,text in sources] and youtube in [text for _,text in sources]
    finally:media.close()


def test_scoped_refresh_does_not_claim_full_format_migration(tmp_path):
    folder=prepare(tmp_path)
    report=json.loads((folder/'export-report.json').read_text())
    report.update(format_version='0.1.0',calendar_version=1)
    (folder/'assets/material-actions.js').write_text('old script')
    old=(folder/'entries/2.html').stat().st_mtime_ns
    store=Store(folder)
    try:
        build_archive(store,report,entry_ids={1})
        assert report['format_version']=='0.1.0' and report['calendar_version']==1
        assert (folder/'entries/2.html').stat().st_mtime_ns==old
        assert 'old script' not in (folder/'assets/material-actions.js').read_text()
        assert verify_archive(store)['ok']
    finally:store.close()


def test_shared_image_updates_orphan_post_and_gallery_references(tmp_path,monkeypatch):
    folder=prepare(tmp_path)
    store=Store(folder)
    with store.db:store.save_comment({'id':10,'jitemid':99,'body':f'<img src="{URL}">'})
    report=json.loads((folder/'export-report.json').read_text())
    build_archive(store,report);store.close()
    feed_stamp = (folder/'assets/posts/99.js').stat().st_mtime_ns
    image_network(monkeypatch)
    jobs=Jobs(tmp_path)
    task=jobs.materials.start({'archive':'fixture','entry_id':1,'kind':'image','value':URL})
    result=wait(jobs.materials,task['id'])
    assert result['phase']=='complete',result
    assert result['asset']['path'] in (folder/'entries/99.html').read_text()
    assert (folder/'assets/posts/99.js').stat().st_mtime_ns != feed_stamp
    data=json.loads((folder/'data/materials.json').read_text())
    row=next(row for row in data['materials'] if row['url']==URL)
    assert {ref['entry_id'] for ref in row['references']}=={1,4,99}
    from ljarchive.on_demand import validate
    store=Store(folder)
    try:
        assert verify_archive(store)['ok']
        assert validate(store,{'kind':'image','entry_id':99,'value':URL})['unavailable']
    finally:store.close()


def test_scoped_render_keeps_repost_comment_counts_and_media_inventory(tmp_path):
    store=Store(tmp_path)
    with store.db:
        store.set('username','fixture')
        store.save_entry({'itemid':1,'event':'Repost','repost':1})
        store.save_comment({'id':10,'jitemid':1,'body':f'<img src="{URL}">'})
    report={'username':'fixture','status':'partial','entries_saved':1,'comments_saved':1}
    try:
        build_archive(store,report)
        assert report['images_unavailable']==0 and report['media_complete']
        original=(tmp_path/'index.html').read_text()
        build_archive(store,report,entry_ids=set())
        assert report['images_unavailable']==0 and report['media_complete']
        assert (tmp_path/'index.html').read_text()==original
    finally:store.close()


def test_scoped_manifest_removes_obsolete_gallery_pages(tmp_path):
    from ljarchive.integrity import write_manifest
    folder=prepare(tmp_path)
    stale=folder/'materials-image-2.html';stale.write_text('previous gallery page')
    write_manifest(folder)
    store=Store(folder)
    try:
        assert verify_archive(store)['ok']
        report=json.loads((folder/'export-report.json').read_text())
        build_archive(store,report,entry_ids=set())
        assert not stale.exists()
        assert 'materials-image-2.html' not in json.loads((folder/'integrity.json').read_text())['files']
        assert verify_archive(store)['ok']
    finally:store.close()


def test_gallery_source_links_do_not_execute_archived_urls(tmp_path):
    store=Store(tmp_path)
    with store.db:
        store.set('username','fixture')
        store.save_entry({'itemid':1,'event':'<img src="javascript:alert(1)"><img src="https://images.example/a.png">'})
    try:
        from ljarchive.gallery import write_gallery
        write_gallery(store,{'username':'fixture','status':'partial'})
        page=(tmp_path/'materials-image.html').read_text()
        assert 'javascript:alert(1)' in page and 'href="javascript:' not in page
        assert 'href="https://images.example/a.png"' in page
    finally:store.close()
