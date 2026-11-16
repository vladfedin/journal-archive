import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from ljarchive import __version__
from ljarchive.client import ExportError
from ljarchive.launcher import prepare_server, version_tuple
from ljarchive.server import make_server
from ljarchive.storage import atomic_write, dumps


@contextmanager
def running(server):
    def run():
        try:
            server.serve_forever(poll_interval=.02)
        finally:
            server.server_close()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(5)
        assert not thread.is_alive()


def legacy_server(output, version='0.3.0', *, header='LJLocalArchive/0.3', refuse=False, save=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = header
        def log_message(self, *_):
            pass
        def response(self, status, data, content_type='application/json'):
            body = data.encode('utf-8') if isinstance(data, str) else json.dumps(data).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Set-Cookie', 'ljarchive_session=legacy; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path == '/':
                return self.response(200, f'<footer class="footer"><span>Архив ЖЖ · {version}</span><button id="quit">Завершить приложение</button></footer>', 'text/html')
            if 'ljarchive_session=legacy' not in self.headers.get('Cookie', ''):
                return self.response(403, {})
            if self.path == '/api/archives':
                return self.response(200, {'output': str(output), 'archives': []})
            return self.response(404, {})
        def do_POST(self):
            self.server.posts.append(self.path)
            length = int(self.headers['Content-Length'])
            assert isinstance(json.loads(self.rfile.read(length)), dict)
            assert self.headers['Origin'] == f'http://127.0.0.1:{self.server.server_port}'
            assert 'ljarchive_session=legacy' in self.headers['Cookie']
            assert self.headers['Content-Type'] == 'application/json'
            if self.path != '/api/quit' or refuse:
                return self.response(409, {})
            self.response(200, {})
            def stop():
                if save:
                    save()
                self.server.shutdown()
            threading.Thread(target=stop, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.posts = []
    return server


def test_legacy_030_is_replaced_on_same_port_using_existing_archive_directory(tmp_path):
    existing = tmp_path / 'old-location' / 'archives'
    existing.mkdir(parents=True)
    marker = existing / 'keep.txt'; marker.write_text('saved')
    old = legacy_server(existing)
    with running(old):
        server, url = prepare_server(tmp_path / 'desktop' / 'archives', old.server_port, timeout=2)
        try:
            assert server.server_port == old.server_port
            assert url == f'http://127.0.0.1:{old.server_port}/'
            assert server.jobs.output == existing.resolve()
            assert old.posts == ['/api/quit']
            assert marker.read_text() == 'saved'
            with running(server), requests.Session() as session:
                response = session.get(url)
                assert response.headers['X-LJArchive-Version'] == __version__
                assert f'Архив ЖЖ · {__version__}' in response.text
                assert session.get(url + 'api/app').json()['version'] == __version__
        finally:
            server.server_close()


def test_stale_protocol_header_uses_footer_version_and_never_downgrades(tmp_path):
    old = legacy_server(tmp_path, '99.0.0', header='LJLocalArchive/0.4')
    with running(old):
        server, url = prepare_server(tmp_path / 'archives', old.server_port)
        assert server is None and str(old.server_port) in url
        assert old.posts == []


def test_current_instance_is_reused_and_guarded_app_info_is_accurate(tmp_path):
    with running(make_server(tmp_path / 'archives', 0)) as current:
        base = f'http://127.0.0.1:{current.server_port}'
        with requests.Session() as session:
            assert session.get(base + '/api/app').status_code == 403
            response = session.get(base + '/')
            assert response.headers['Server'].startswith('LJLocalArchive/' + __version__)
            info = session.get(base + '/api/app').json()
            assert info == {'application':'journal-archive','version':__version__, 'instance_id':current.instance_id,
                            'output':str(current.jobs.output),'stopping':False}
            assert session.post(base + '/api/quit', json={'expected_instance':'wrong'}, headers={'Origin':base}).status_code == 409
            assert not current.jobs.stopping and not current.jobs.cancel.is_set()
        server, url = prepare_server(tmp_path / 'archives', current.server_port)
        assert server is None and url == base + '/'
        assert not current.jobs.stopping


def test_active_legacy_job_finishes_checkpoint_before_new_jobs_load(tmp_path):
    output = tmp_path / 'archives'; output.mkdir()
    saved = threading.Event()
    def checkpoint():
        record = {'parameters':{'archive':'fixture','username':'fixture','mode':'full','images':True,
                               'entry_limit':None,'delay':3,'max_requests':None},
                  'state':{'phase':'cancelled','slug':'fixture','entries':348,'comments':1234},
                  'updated_at':'2026-11-16T20:00:00Z'}
        atomic_write(output / '.jobs/fixture.json', dumps(record))
        saved.set()
    old = legacy_server(output, save=checkpoint)
    with running(old):
        server, _ = prepare_server(output, old.server_port, timeout=2)
        try:
            assert saved.is_set()
            assert server.jobs.snapshot()['entries'] == 348
            assert server.jobs.snapshot()['comments'] == 1234
            assert server.jobs.current['images'] is True
            assert server.jobs.archives()['archives'][0]['resumable']
        finally:
            server.server_close()


def test_refused_or_slow_shutdown_never_starts_second_writer(tmp_path):
    old = legacy_server(tmp_path, refuse=True)
    with running(old), patch('ljarchive.server.make_server', side_effect=AssertionError('Must not start a second writer')):
        with pytest.raises(ExportError, match='завершить'):
            prepare_server(tmp_path / 'archives', old.server_port, timeout=.01)
    unblock = threading.Event()
    old = legacy_server(tmp_path, save=lambda: unblock.wait(2))
    try:
        with running(old), patch('ljarchive.server.make_server', side_effect=AssertionError('Must wait for the writer')):
            with pytest.raises(ExportError, match='сохраняет'):
                prepare_server(tmp_path / 'archives', old.server_port, timeout=.01)
    finally:
        unblock.set()


def test_unrelated_port_is_left_alone_and_alternate_port_is_reused(tmp_path):
    foreign = legacy_server(tmp_path, header='OtherService/1.0')
    output = tmp_path / 'archives'
    with running(foreign):
        server, url = prepare_server(output, foreign.server_port)
        assert server.server_port != foreign.server_port and foreign.posts == []
        with running(server):
            second, second_url = prepare_server(output, foreign.server_port)
            assert second is None and second_url == url
            assert foreign.posts == []


def test_stale_registry_on_default_port_does_not_hide_an_older_instance(tmp_path):
    old = legacy_server(tmp_path)
    atomic_write(tmp_path / 'desktop-instance.json', dumps({'port':old.server_port,'instance_id':'stale'}))
    with running(old):
        server, _ = prepare_server(tmp_path / 'archives', old.server_port, timeout=2)
        try:
            assert server.server_port == old.server_port and old.posts == ['/api/quit']
        finally:
            server.server_close()


def test_unknown_version_does_not_silently_open_old_ui(tmp_path):
    old = legacy_server(tmp_path, 'unknown')
    with running(old):
        with pytest.raises(ExportError, match='версию'):
            prepare_server(tmp_path / 'archives', old.server_port)
        assert old.posts == []


def test_shutdown_rejects_new_exports_while_saving(tmp_path):
    from ljarchive import server as module
    # Simulate a newer launcher taking over this server.
    with patch.object(module, '__version__', '0.6.1'):
        old = make_server(tmp_path / 'archives', 0)
    finished = threading.Event()
    def worker():
        old.jobs.cancel.wait(3)
        finished.set()
        with pytest.raises(ExportError, match='завершается'):
            old.jobs.start({}, demo=True)
    old.jobs.thread = threading.Thread(target=worker)
    old.jobs.thread.start()
    with running(old):
        replacement, _ = prepare_server(tmp_path / 'archives', old.server_port, timeout=2)
        try:
            assert finished.is_set() and not old.jobs.thread.is_alive()
            assert old.jobs.stopping
        finally:
            replacement.server_close()


def test_version_comparison_is_numeric():
    assert version_tuple('0.10.0') > version_tuple('0.6.2')
    assert version_tuple('0.3') == (0,3,0)


def test_adopted_archive_directory_survives_a_later_cold_start(tmp_path):
    previous = tmp_path / 'previous' / 'archives'; previous.mkdir(parents=True)
    output = tmp_path / 'desktop' / 'archives'
    old = legacy_server(previous)
    with running(old):
        replacement, _ = prepare_server(output, old.server_port, timeout=2)
        replacement.server_close()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        free = sock.getsockname()[1]
    restarted, _ = prepare_server(output, free)
    try:
        assert restarted.jobs.output == previous
    finally:
        restarted.server_close()


def test_filesystem_failure_does_not_retry_on_another_port(tmp_path):
    with patch('ljarchive.server.make_server', side_effect=PermissionError('Read-only directory')) as create:
        with pytest.raises(PermissionError):
            prepare_server(tmp_path / 'archives', 0)
        assert create.call_count == 1


def test_double_click_uses_the_version_aware_launcher(tmp_path, monkeypatch):
    import io
    from ljarchive import desktop
    monkeypatch.delenv('LJARCHIVE_DIAGNOSTIC_LOG', raising=False)
    monkeypatch.setattr('sys.argv', ['JournalArchive.exe'])
    monkeypatch.setattr('sys.stdout', io.StringIO())
    monkeypatch.setattr('sys.stderr', io.StringIO())
    monkeypatch.setattr('ljarchive.paths.desktop_output', lambda: tmp_path / 'archives')
    calls = []
    monkeypatch.setattr(desktop, 'launch', lambda output: calls.append(output))
    desktop.main()
    assert calls == [tmp_path / 'archives']


def test_windowed_startup_failure_is_visible(monkeypatch):
    import io
    from ljarchive import desktop
    monkeypatch.delenv('LJARCHIVE_DIAGNOSTIC_LOG', raising=False)
    monkeypatch.setattr('sys.argv', ['JournalArchive.exe'])
    monkeypatch.setattr('sys.stdout', io.StringIO())
    monkeypatch.setattr('sys.stderr', io.StringIO())
    def failed(output):
        raise ExportError('Старая версия ещё сохраняет данные.')
    errors = []
    monkeypatch.setattr(desktop, 'launch', failed)
    monkeypatch.setattr(desktop, 'show_startup_error', errors.append)
    with pytest.raises(SystemExit) as exc:
        desktop.main()
    assert exc.value.code == 1 and errors == ['Старая версия ещё сохраняет данные.']
