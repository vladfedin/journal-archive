import json
import threading
from pathlib import Path

import pytest
import requests

from ljarchive.client import ExportError, Cancelled
from ljarchive.recovery import inventory, read_inventory, summary, fingerprint
from ljarchive.server import Jobs
from ljarchive.storage import Store
from test_progress_repair import BadPagination, SCOPE
from test_sync import Journal
from ljarchive.sync import SyncExporter
from test_render_server import local_server


def test_resume_reports_closed_discussions_by_identity(tmp_path):
    assert SyncExporter(tmp_path, BadPagination(), SCOPE).run()['status'] == 'partial'
    before = read_inventory(tmp_path)
    assert before['units']['discussions'] == {'1'}
    assert SyncExporter(tmp_path, Journal(), SCOPE).run()['status'] == 'complete'
    after = read_inventory(tmp_path)
    row = next(r for r in summary(after, before)['rows'] if r['key'] == 'discussions')
    assert (row['before'], row['resolved'], row['remaining'], row['new']) == (1, 1, 0, 0)
    assert not any(after['units'].values())


def test_same_total_can_mean_old_gap_closed_and_new_gap_found(tmp_path):
    store = Store(tmp_path)
    try:
        with store.db:
            store.save_entry({'itemid': 1, 'poster_userpic_url': ''})
            store.set('bodies_done', True)
            store.set('posts_done', True)
            store.set('entry_materials', {'1': {'urls': ['https://example.org/old?token=PRIVATE']}})
        before = inventory(store)
        with store.db:
            store.set('entry_materials', {'1': {'urls': ['https://example.org/new', 'https://example.org/new']}})
            store.set('media_errors', {'https://example.org/new': {'reason': 'http', 'http_status': 404, 'detail': 'PRIVATE'}})
        result = summary(inventory(store), before)
        row = next(r for r in result['rows'] if r['key'] == 'images')
        assert (row['before'], row['resolved'], row['remaining'], row['new']) == (1, 1, 1, 1)
        assert 'HTTP 404' in result['reasons'][0]['message']
        assert 'PRIVATE' not in json.dumps(result) and 'https://' not in json.dumps(result)
        # A mapping alone is not proof of a saved file.
        with store.db:
            store.set('media', {'https://example.org/new': 'media/absent.png'})
        assert inventory(store)['units']['images'] == {fingerprint('https://example.org/new')}
    finally:
        store.close()


def test_repost_deleted_disabled_video_and_unselected_posts_are_not_gaps(tmp_path):
    store = Store(tmp_path)
    try:
        with store.db:
            store.save_entry({'itemid': 1, 'repost': 1, 'poster_userpic_url': ''})
            store.save_entry({'itemid': 2, 'poster_userpic_url': ''})
            store.save_entry({'itemid': 3})
            store.save_comment({'id': 7, 'jitemid': 2, 'state': 'D', 'source_status': 'body_not_returned'})
            store.set('sync_job', {'phase': 'comments', 'selected': [1, 2]})
            store.set('entry_comments', {'2': {'done': True}})
            store.set('videos', {'https://example.org/off': {'entries': [2], 'status': 'not_requested'},
                                 'https://example.org/old': {'entries': [3], 'status': 'error'}})
        assert not any(inventory(store)['units'].values())
    finally:
        store.close()


def test_personal_media_and_sections_are_in_inventory(tmp_path):
    store = Store(tmp_path)
    try:
        with store.db:
            store.set('personal_requested', {'account': True, 'messages': True})
            store.set('personal_images_requested', True)
            store.set('userpic_catalog', {'defaultpicurl': 'https://example.org/pic'})
            store.db.execute('INSERT INTO messages VALUES (?,?)', (1, json.dumps({'body': '<img src="https://example.org/embedded">'})))
        result = inventory(store)
        assert len(result['units']['personal']) == 10
        assert result['units']['images'] == {fingerprint(u) for u in ('https://example.org/pic', 'https://example.org/embedded')}
    finally:
        store.close()


def test_unavailable_inventory_is_not_empty_and_never_creates_database(tmp_path):
    assert summary(read_inventory(tmp_path))['available'] is False
    assert not list(tmp_path.iterdir())


def test_cancel_during_resume_inventory_stops_before_network_or_writes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    urls = ['https://example.org/a', 'https://example.org/b']
    with store.db:
        store.set('entry_materials', {'1': {'urls': urls}})
        store.save_entry({'itemid': 1, 'poster_userpic_url': ''})
        store.set('media', {url: 'media/absent.png' for url in urls})
    cancel, updates = threading.Event(), []
    original = Path.is_file
    def inspect(path):
        if path.name == 'absent.png':
            cancel.set()
        return original(path)
    monkeypatch.setattr(Path, 'is_file', inspect)
    before = store.db.total_changes
    with pytest.raises(Cancelled):
        read_inventory(tmp_path, cancel=cancel, progress=lambda **v: updates.append(v))
    assert store.db.total_changes == before and updates
    assert any(v.get('total') == 1 for v in updates)
    store.close()


def test_cancelled_preparation_does_not_repeat_inventory_in_finalizer(tmp_path, monkeypatch):
    jobs = Jobs(tmp_path)
    jobs.current = {'username': 'fixture', 'archive': 'fixture', 'entry_limit': None, 'mode': 'full',
                    'max_requests': None, 'images': False, 'videos': False, 'delay': 3}
    jobs.state = {'phase': 'partial', 'slug': 'fixture'}
    jobs.persist()
    calls = []
    def cancel_read(folder, **kwargs):
        calls.append(folder)
        kwargs['cancel'].set()
        raise Cancelled('Stopped')
    monkeypatch.setattr('ljarchive.recovery.read_inventory', cancel_read)
    monkeypatch.setattr('ljarchive.server.sync', lambda *a, **kw: pytest.fail('No network after cancellation'))
    jobs.start({'resume': 'fixture', 'password': 'test-only'})
    jobs.thread.join(3)
    assert not jobs.thread.is_alive() and jobs.snapshot()['phase'] == 'cancelled'
    assert len(calls) == 1


def test_job_resume_saves_attempt_result_across_restart(tmp_path, monkeypatch):
    first = True
    def sync(user, password, output, **options):
        nonlocal first
        client = BadPagination() if first else Journal()
        first = False
        return SyncExporter(output / options['archive'], client, SCOPE).run()
    monkeypatch.setattr('ljarchive.server.sync', sync)
    jobs = Jobs(tmp_path)
    jobs.start({'username': 'fixture', 'password': 'PRIVATE', 'mode': 'urls', 'urls': list(SCOPE.urls)})
    jobs.thread.join(10)
    assert jobs.state['phase'] == 'partial'
    plan = jobs.recovery_plan('fixture')
    assert next(r for r in plan['rows'] if r['key'] == 'discussions')['remaining'] == 1
    restarted = Jobs(tmp_path)
    restarted.start({'resume': 'fixture', 'password': 'PRIVATE'})
    restarted.thread.join(10)
    state = restarted.snapshot()
    assert state['phase'] == 'complete'
    row = next(r for r in state['recovery']['rows'] if r['key'] == 'discussions')
    assert row['resolved'] == 1 and row['remaining'] == 0
    assert Jobs(tmp_path).snapshot()['recovery'] == state['recovery']
    assert len(Jobs(tmp_path).recovery_plan('fixture')['attempts']) == 2
    assert 'PRIVATE' not in (tmp_path / '.jobs/fixture.json').read_text()


def test_status_stays_responsive_during_inventory_scan(tmp_path, monkeypatch):
    jobs = Jobs(tmp_path)
    jobs.current = {'archive': 'fixture'}
    jobs.state = {'phase': 'comments', 'job_id': 'test'}
    jobs.recovery_before = {'units': {}}
    entered, release = threading.Event(), threading.Event()
    def slow_read(folder):
        entered.set()
        release.wait(5)
        return None
    monkeypatch.setattr('ljarchive.recovery.read_inventory', slow_read)
    try:
        assert jobs.snapshot()['phase'] == 'comments'
        assert entered.wait(1)
        assert jobs.snapshot()['phase'] == 'comments'
    finally:
        release.set()
        jobs.recovery_thread.join(5)


def test_slow_filesystem_inventory_does_not_hold_database_read_lock(tmp_path, monkeypatch):
    writer = Store(tmp_path)
    entered, release = threading.Event(), threading.Event()
    result = []
    with writer.db:
        writer.save_entry({'itemid': 1, 'poster_userpic_url': 'https://example.org/picture'})
        writer.set('media', {'https://example.org/picture': 'media/picture.png'})
    check_file = Path.is_file
    def slow_file(path):
        if path == tmp_path / 'media/picture.png':
            entered.set()
            release.wait(5)
            return False
        return check_file(path)
    monkeypatch.setattr(Path, 'is_file', slow_file)
    reader = threading.Thread(target=lambda: result.append(read_inventory(tmp_path)))
    reader.start()
    try:
        assert entered.wait(2)
        writer.db.execute('PRAGMA busy_timeout=100')
        with writer.db:
            writer.set('checkpoint_during_scan', 1)
    finally:
        release.set()
        reader.join(5)
        writer.close()
    assert result[0]['units']['images'] == {fingerprint('https://example.org/picture')}


def test_recovery_endpoint_requires_session_and_rejects_foreign_paths(local_server):
    server, base = local_server
    with requests.Session() as session:
        assert session.get(base + '/api/recovery/fixture').status_code == 403
        session.get(base)
        assert session.get(base + '/api/recovery/fixture').json()['available'] is False
        assert session.get(base + '/api/recovery/bad!').status_code == 400
    with pytest.raises(ExportError):
        server.jobs.recovery_plan('../outside')
