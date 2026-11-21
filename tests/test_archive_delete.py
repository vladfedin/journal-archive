import pytest
import requests

from ljarchive import deletion
from ljarchive.client import ExportError
from ljarchive.demo import create_demo
from ljarchive.server import Jobs
from ljarchive.storage import Store
from test_render_server import local_server


def save_job(jobs, slug):
    jobs.current = {'archive': slug, 'username': 'writer', 'entry_limit': None}
    jobs.state = {'phase': 'complete', 'slug': slug}
    jobs.persist()


def test_delete_archive_from_library(local_server, tmp_path):
    server, base = local_server
    folder = tmp_path / 'writer'
    create_demo(folder)
    sibling = tmp_path / 'writer-latest-10'
    create_demo(sibling)
    save_job(server.jobs, sibling.name)
    save_job(server.jobs, folder.name)
    url = base + '/api/archives/delete'
    with requests.Session() as session:
        session.get(base).raise_for_status()
        assert session.post(url, json={'archive': 'writer'}).status_code == 403
        def remove(slug, token=None):
            return session.post(url, json={'archive': slug, 'token': token}, headers={'Origin': base})
        assert remove('../writer').status_code == 400
        assert folder.is_dir()
        # Build the search cache on a different HTTP request thread before deleting.
        session.get(base + '/api/search', params={'q': 'журнал'}).raise_for_status()
        assert remove('writer').status_code == 400
        preview = session.post(url + '-preview', json={'archive': 'writer'}, headers={'Origin': base}).json()
        assert preview['path'] == str(folder)
        assert remove('writer', preview['token']).json() == {'deleted': 'writer'}
        assert not folder.exists()
        assert not server.jobs.task_path('writer').exists()
        for page in ('index.html', 'statistics.html', 'discussions.html', 'search.html'):
            assert session.get(base + '/archive/writer/' + page).status_code == 404
        assert session.get(base + '/download/writer.zip').status_code == 404
        rows = session.get(base + '/api/archives').json()['archives']
        assert [row['slug'] for row in rows] == [sibling.name]
        assert (sibling / 'archive.sqlite3').is_file()
        assert server.jobs.state == {'phase': 'idle'}
        assert Jobs(tmp_path).current['archive'] == sibling.name
        create_demo(folder)
        assert session.get(base + '/archive/writer/index.html').status_code == 200


def test_delete_busy_archive_and_unrelated_folders(tmp_path):
    jobs = Jobs(tmp_path)
    folder = tmp_path / 'writer'
    with pytest.raises(ExportError, match='не найден'):
        jobs.delete_archive('writer', 'expired')
    store = Store(folder)
    try:
        with pytest.raises(ExportError, match='другим процессом'):
            jobs.delete_archive('writer', jobs.deletion_target('writer')['token'])
    finally:
        store.close()
    jobs.formatting.add('writer')
    with pytest.raises(ExportError, match='завершения'):
        jobs.delete_archive('writer', jobs.deletion_target('writer')['token'])
    jobs.formatting.clear()
    jobs.materials.states['image'] = {'_identity': ('writer', 1, 'image', 'file')}
    jobs.materials.queue.append(('image', {'archive': 'writer'}))
    with pytest.raises(ExportError, match='очереди'):
        jobs.delete_archive('writer', jobs.deletion_target('writer')['token'])
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'document').write_text('keep')
    with pytest.raises(ExportError, match='нет базы'):
        jobs.delete_archive('other', jobs.deletion_target('other')['token'])
    save_job(jobs, 'early')
    jobs.delete_archive('early', jobs.deletion_target('early')['token'])
    assert not jobs.task_path('early').exists()
    assert (folder / 'archive.sqlite3').is_file()
    try:
        (tmp_path / 'alias').symlink_to(folder, target_is_directory=True)
        (folder / 'linked').symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlinks unavailable')
    with pytest.raises(ExportError):
        jobs.delete_archive('alias', jobs.deletion_target('alias')['token'])
    jobs.materials.queue.clear()
    jobs.delete_archive('writer', jobs.deletion_target('writer')['token'])
    assert not jobs.materials.states
    assert (other / 'document').read_text() == 'keep'


def test_restart_after_interrupted_deletion(tmp_path, monkeypatch):
    jobs = Jobs(tmp_path)
    folder = tmp_path / 'writer'
    Store(folder).close()
    save_job(jobs, 'writer')
    with monkeypatch.context() as patch:
        def fail(path):
            raise PermissionError('busy')
        patch.setattr(deletion.shutil, 'rmtree', fail)
        with pytest.raises(ExportError, match='осталась'):
            jobs.delete_archive('writer', jobs.deletion_target('writer')['token'])
    assert not folder.exists()
    assert (jobs.tasks / 'writer.deleting/archive.sqlite3').exists()
    restarted = Jobs(tmp_path)
    assert restarted.archives()['archives'] == [
        {'slug': 'writer', 'username': 'writer', 'deletion_pending': True}]
    target = restarted.deletion_target('writer')
    assert target['path'] == str(jobs.tasks / 'writer.deleting')
    restarted.delete_archive('writer', target['token'])
    assert list(jobs.tasks.iterdir()) == []
    assert restarted.archives()['archives'] == []


def test_confirmation_expires_after_move_or_replacement(tmp_path):
    jobs = Jobs(tmp_path / 'library')
    Store(jobs.output / 'writer').close()
    token = jobs.deletion_target('writer')['token']
    jobs.move(str(tmp_path / 'new'))
    jobs.move_thread.join(10)
    assert jobs.output == tmp_path / 'new'
    with pytest.raises(ExportError, match='заново'):
        jobs.delete_archive('writer', token)
    token = jobs.deletion_target('writer')['token']
    folder = jobs.output / 'writer'
    folder.rename(jobs.output / 'backup')
    Store(folder).close()
    with pytest.raises(ExportError, match='заново'):
        jobs.delete_archive('writer', token)
    assert (folder / 'archive.sqlite3').is_file()
