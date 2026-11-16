import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from ljarchive.client import ExportError
from ljarchive.demo import create_demo
from ljarchive.relocation import redirect_file, resolve_output, relocate, target_path
from ljarchive.server import Jobs
from ljarchive.storage import Store
from test_render_server import local_server


def test_verified_move_keeps_database_media_json_and_checkpoint_and_cold_start(tmp_path):
    source, target = tmp_path/'old', tmp_path/'новая папка'
    create_demo(source/'example_journal')
    jobs = Jobs(source)
    checkpoint = {'phase':'comments','selected':[1,2], 'seen':[1]}
    store = Store(source/'example_journal')
    with store.db: store.set('sync_job', checkpoint)
    store.close()
    expected = (source/'example_journal/data/archive.json').read_bytes()
    jobs.move(str(target)); jobs.move_thread.join(10)
    assert jobs.relocation['phase'] == 'complete', jobs.relocation
    assert not source.exists() and jobs.output == target
    assert (target/'example_journal/data/archive.json').read_bytes() == expected
    restored = Jobs(source)
    assert restored.output == target and restored.tasks == target/'.jobs'
    store = Store(restored.output/'example_journal')
    try: assert store.get('sync_job') == checkpoint
    finally: store.close()
    # Repeated moves follow the redirect without recreating empty old libraries.
    third = tmp_path/'third'
    restored.move(str(third)); restored.move_thread.join(10)
    assert resolve_output(source) == third and not target.exists()


def test_move_is_rejected_while_export_or_other_process_holds_archive(tmp_path):
    source = tmp_path/'old'; jobs = Jobs(source)
    block = threading.Event(); jobs.thread = threading.Thread(target=lambda:block.wait(5)); jobs.thread.start()
    try:
        with pytest.raises(ExportError, match='загрузки'): jobs.move(str(tmp_path/'new'))
    finally:
        block.set(); jobs.thread.join()
    store = Store(source/'journal')
    try:
        jobs.move(str(tmp_path/'new')); jobs.move_thread.join(5)
        assert jobs.relocation['phase'] == 'error'
        assert (source/'journal/archive.sqlite3').exists()
        assert not redirect_file(source).exists()
    finally:
        store.close()


def test_copy_failure_never_deletes_or_redirects_original(tmp_path):
    source = tmp_path/'source'; jobs = Jobs(source)
    (source/'precious.bin').write_bytes(b'original')
    with patch('ljarchive.relocation.shutil.copy2', side_effect=OSError('disk full')):
        jobs.move(str(tmp_path/'new')); jobs.move_thread.join(5)
    assert jobs.relocation['phase'] == 'error'
    assert (source/'precious.bin').read_bytes() == b'original'
    assert not redirect_file(source).exists()
    assert jobs.output == source


def test_corrupted_copy_and_missing_destination_are_visible(tmp_path):
    source = tmp_path/'source'; jobs = Jobs(source)
    (source/'precious.bin').write_bytes(b'original')
    with patch('ljarchive.relocation.digest', side_effect=[b'one',b'two']):
        jobs.move(str(tmp_path/'new')); jobs.move_thread.join(5)
    assert jobs.relocation['phase'] == 'error' and source.exists()
    redirect_file(source).write_text(json.dumps({'output':str(tmp_path/'unplugged')}),encoding='utf-8')
    with pytest.raises(ExportError, match='недоступна'): Jobs(source)


def test_destinations_do_not_merge_or_overlap(tmp_path):
    source=tmp_path/'source';source.mkdir()
    for target in [source,source/'child',tmp_path,Path('relative')]:
        with pytest.raises(ExportError):target_path(source,str(target))
    target=tmp_path/'occupied';target.mkdir();(target/'keep').write_text('mine')
    with pytest.raises(ExportError):target_path(source,str(target))
    assert (target/'keep').read_text() == 'mine'


def test_move_endpoint_uses_local_auth_and_preserves_links(local_server, tmp_path):
    server, base=local_server
    source=server.jobs.output
    create_demo(source/'example_journal')
    target=source.parent/(source.name+'-moved')
    with requests.Session() as session:
        assert session.post(base+'/api/storage/move',json={'path':str(target)},headers={'Origin':base}).status_code==403
        session.get(base)
        assert session.post(base+'/api/storage/move',json={'path':str(target)},headers={'Origin':base}).status_code==202
        server.jobs.move_thread.join(10)
        assert session.get(base+'/api/archives').json()['output']==str(target)
        assert session.get(base+'/archive/example_journal/index.html').status_code==200
        assert session.get(base+'/api/app').json()['output']==str(target)
