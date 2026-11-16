"""Exercise the native desktop launcher against the 0.3.0 HTTP contract."""
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests


def check_upgrade(binary, directory, env):
    from ljarchive import __version__
    directory = Path(directory)
    previous_output = directory / 'old-archives'
    previous_output.mkdir()
    marker = previous_output / 'keep.json'
    marker.write_text('{"saved":true}', encoding='utf-8')
    checkpointed = threading.Event()
    class Legacy(BaseHTTPRequestHandler):
        server_version = 'LJLocalArchive/0.3'
        def log_message(self, *_):
            pass
        def send(self, data, status=200, mime='application/json'):
            body = data.encode('utf-8') if isinstance(data, str) else json.dumps(data).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Set-Cookie', 'ljarchive_session=legacy; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path == '/':
                return self.send('<footer class="footer"><span>Архив ЖЖ · 0.3.0</span><button id="quit">Завершить приложение</button></footer>', mime='text/html')
            if self.path == '/api/archives' and 'ljarchive_session=legacy' in self.headers.get('Cookie', ''):
                return self.send({'output': str(previous_output), 'archives': []})
            self.send({}, 404)
        def do_POST(self):
            valid = (self.path == '/api/quit' and self.headers.get('Origin') == base
                     and 'ljarchive_session=legacy' in self.headers.get('Cookie', '')
                     and self.headers.get('Content-Type') == 'application/json')
            if not valid:
                return self.send({}, 403)
            self.rfile.read(int(self.headers['Content-Length']))
            self.send({})
            def save_and_stop():
                # Emulate a running export that commits its checkpoint on cancellation.
                time.sleep(.2)
                jobs = previous_output / '.jobs'
                jobs.mkdir()
                record = {'parameters': {'username':'fixture','archive':'fixture','mode':'full','entry_limit':None,
                                         'images':False,'delay':3,'max_requests':None},
                          'state': {'phase':'cancelled','slug':'fixture','entries':348,'comments':1234},
                          'updated_at':'2026-11-16T20:00:00Z'}
                (jobs / 'fixture.json').write_text(json.dumps(record), encoding='utf-8')
                checkpointed.set()
                old.shutdown()
            threading.Thread(target=save_and_stop, daemon=True).start()
    old = ThreadingHTTPServer(('127.0.0.1', 0), Legacy)
    base = f'http://127.0.0.1:{old.server_port}'
    def run_old():
        try:
            old.serve_forever(poll_interval=.05)
        finally:
            old.server_close()
    thread = threading.Thread(target=run_old, daemon=True)
    thread.start()
    # This is the same launch() path used by double click, on an isolated test port.
    command = [str(binary), 'desktop', '--no-browser', '--port', str(old.server_port),
               '--output', str(directory / 'new' / 'archives')]
    process = subprocess.Popen(command, cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with requests.Session() as session:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Native desktop launcher exited instead of replacing 0.3.0')
                try:
                    page = session.get(base + '/', timeout=(1, 5))
                    if page.headers.get('X-LJArchive-Version') == __version__:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError('Native desktop upgrade did not complete')
            assert checkpointed.is_set()
            assert f'Архив ЖЖ · {__version__}' in page.text
            info = session.get(base + '/api/app', timeout=5).json()
            assert info['version'] == __version__ and Path(info['output']) == previous_output.resolve()
            state = session.get(base + '/api/status', timeout=5).json()
            assert state['phase'] == 'cancelled' and state['entries'] == 348 and state['comments'] == 1234
            assert json.loads(marker.read_text(encoding='utf-8')) == {'saved':True}
            # A second double click must reuse the new instance rather than replace it.
            subprocess.run(command, cwd=directory, env=env, check=True, timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert session.get(base + '/api/app', timeout=5).json()['instance_id'] == info['instance_id']
            assert session.post(base + '/api/quit', headers={'Origin':base}, json={}, timeout=5).status_code == 200
        assert process.wait(timeout=15) == 0
    finally:
        old.shutdown()
        thread.join(5)
        if process.poll() is None:
            if sys.platform == 'win32':
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'], timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                process.terminate()
            process.wait(timeout=10)
    print('Native desktop: 0.3.0 handover, saved checkpoint, existing folder and repeated launch passed.')
