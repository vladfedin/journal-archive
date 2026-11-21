import hashlib
import io
from pathlib import Path
import tarfile
import threading
import time
from unittest.mock import patch

import pytest

from ljarchive import update
from ljarchive.client import ExportError
from ljarchive.launcher import prepare_server
from test_desktop_launcher import running

VERSION = '9.0.1'
PAYLOAD = b'#!/bin/sh\nexit 0\n'


def artifact(fmt, binary=PAYLOAD):
    if fmt.endswith('.exe'):
        return binary
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode='w:gz') as tar:
        member = tarfile.TarInfo('LiveJournalArchive')
        member.size = len(binary)
        tar.addfile(member, io.BytesIO(binary))
    return packed.getvalue()


def release(suffix, payload):
    return {'tag_name':'v' + VERSION, 'draft':False, 'prerelease':False, 'assets':[
        {'name': f'JournalArchive-{VERSION}-{suffix}', 'state':'uploaded',
         'size':len(payload), 'digest':'sha256:' + hashlib.sha256(payload).hexdigest()}]}


def test_select_only_new_stable_matching_artifact():
    meta = release('windows-x64.exe', PAYLOAD)
    assert update.candidate(meta, 'windows-x64.exe')['version'] == VERSION
    assert update.candidate({**meta, 'tag_name':'v0.1.0'}, 'windows-x64.exe') is None
    for key, val in [('tag_name','v../bin'), ('draft', True), ('prerelease', True)]:
        with pytest.raises(ExportError): update.candidate({**meta, key:val}, 'windows-x64.exe')
    with pytest.raises(ExportError): update.candidate(meta, 'macos-arm64.zip')
    meta['assets'][0]['digest'] = None
    with pytest.raises(ExportError): update.candidate(meta, 'windows-x64.exe')


class Response:
    status_code = 200
    def __init__(self, payload, meta=None):
        self.payload, self.meta, self.is_redirect = payload, meta, False

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def iter_content(self, size): yield self.payload
    def json(self): return self.meta


def wait_for(updater, phase):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        state = updater.snapshot()
        if state['phase'] == phase: return state
        time.sleep(.02)
    pytest.fail(str(state))


def test_confirmed_auto_download_and_cold_start(tmp_path, monkeypatch):
    fmt, exe = update.PACKAGES['linux', 'x64']
    root = tmp_path / 'updates'
    payload = artifact(fmt)
    meta = release(fmt, payload)
    requests = []
    def get(session, url, **kwargs):
        requests.append(url)
        assert not session.trust_env
        return Response(payload, meta)
    monkeypatch.setattr(update.requests.Session, 'get', get)
    launched = []
    monkeypatch.setattr(update.subprocess, 'run', lambda args, **kw: launched.append((Path(args[0]).read_bytes(), args[1:])))
    updater = update.DesktopUpdate((fmt, exe), root)
    assert not updater.snapshot()['automatic']
    wait_for(updater, 'available')
    assert len(requests) == 1
    updater.set_automatic(True)
    wait_for(updater, 'ready')
    assert len(requests) == 2
    assert launched == [(PAYLOAD, ['--verify-update-version', VERSION])]
    another = update.DesktopUpdate((fmt, exe), root)
    assert another.snapshot()['automatic']
    wait_for(another, 'ready')
    with patch('ljarchive.update.subprocess.Popen') as start:
        assert another.start_installed(tmp_path/'archives', 9777, False)
        args = start.call_args.args[0]
        assert args == [str(root/VERSION/exe),'desktop','--output',str(tmp_path/'archives'),'--port','9777','--no-browser']
        assert start.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'] == '1'
    assert not another.set_automatic(False)['automatic']


def test_rejected_download_leaves_current_version_runnable(tmp_path, monkeypatch):
    fmt, exe = update.PACKAGES['win32', 'x64']
    meta = release(fmt, PAYLOAD)
    session = update.requests.Session()
    monkeypatch.setattr(session, 'get', lambda *a, **kw: Response(b'X' * len(PAYLOAD)))
    with pytest.raises(ExportError, match='SHA-256'):
        update.stage(session, update.candidate(meta, fmt), tmp_path, exe, lambda n:None)
    assert not update.DesktopUpdate((fmt,exe), tmp_path).installed()
    assert not list(tmp_path.glob('.staging-*'))


def test_second_launch_reuses_running_server_before_checking_installed_version(tmp_path):
    output = tmp_path/'archives'
    server, url = prepare_server(output, 0)
    with running(server):
        server.jobs.thread = threading.current_thread()
        try:
            assert prepare_server(output, server.server_port, cold_start=lambda: pytest.fail('Active job interrupted')) == (None, url)
            assert not server.jobs.stopping
        finally:
            server.jobs.thread = None
    assert prepare_server(output, 0, cold_start=lambda: True) == (None,None)


@pytest.mark.skipif(update.sys.platform != 'linux', reason='Linux desktop executable')
def test_staged_program_runs_with_original_archive_path(tmp_path, monkeypatch):
    import shlex
    marker = tmp_path/'opened.txt'
    script = ('#!/bin/sh\n'
              'if [ "$1" = "--verify-update-version" ]; then exit 0; fi\n'
              'printf "%s\\n" "$@" > ' + shlex.quote(str(marker)) + '\n').encode()
    fmt, exe = update.PACKAGES['linux','x64']
    payload = artifact(fmt, script)
    s = update.requests.Session()
    monkeypatch.setattr(s, 'get', lambda *a, **kw: Response(payload))
    updater = update.DesktopUpdate((fmt, exe), tmp_path/'updates')
    update.stage(s, update.candidate(release(fmt, payload), fmt), updater.root, exe, lambda n:None)
    archives = tmp_path/'Архив журнала'
    assert updater.start_installed(archives, 9876, False)
    deadline = time.monotonic() + 3
    while not marker.is_file() and time.monotonic() < deadline:
        time.sleep(.02)
    assert marker.read_text().splitlines() == ['desktop','--output',str(archives),'--port','9876','--no-browser']
