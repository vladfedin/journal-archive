"""Verify the native artifact works without a Python environment or a network export."""
import json
import os
import subprocess
import sys
import tempfile
import socket
import time
from pathlib import Path
import requests

root = Path(__file__).resolve().parents[1]
if sys.platform == "darwin":
    binary = root / "dist/LiveJournalArchive.app/Contents/MacOS/LiveJournalArchive"
else:
    binary = root / "dist" / ("LiveJournalArchive.exe" if sys.platform == "win32" else "LiveJournalArchive")
with tempfile.TemporaryDirectory(prefix="ljarchive-ЖЖ-") as directory:
    folder = Path(directory) / "demo"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    diagnostic_log = Path(directory) / 'native.log'
    env['LJARCHIVE_DIAGNOSTIC_LOG'] = str(diagnostic_log)
    subprocess.run([str(binary), "demo", "--output", str(folder)], cwd=directory, env=env, check=True, timeout=90)
    data = json.loads((folder / "data/archive.json").read_text(encoding="utf-8"))
    assert len(data["entries"]) == 6 and len(data["comments"]) == 12
    assert (folder / "assets/archive.css").is_file()
    assert (folder / "index.html").is_file()
    assert (folder / "help.html").is_file()
    assert (folder / "assets/posts/6.js").is_file()
    assert all(pic["path"] for pic in data["userpics"]["post"].values())
    assert 'class="lj-cut"' in (folder / "entries/6.html").read_text(encoding="utf-8")
    assert (folder / "assets/common.js").is_file()
    assert (folder / "assets/favicon.svg").is_file()
    assert (folder / "assets/favicon.ico").is_file()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([str(binary), "serve", "--no-browser", "--port", str(port), "--output", directory],
                               cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        with requests.Session() as session:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Native HTTP server exited before startup")
                try:
                    response = session.get(base + "/", timeout=(1, 5))
                    break
                except (requests.ConnectionError, requests.Timeout):
                    time.sleep(0.25)
            else:
                raise RuntimeError("Native HTTP server did not start")
            assert response.status_code == 200 and 'date-from' in response.text
            assert session.get(base + '/static/archive.css', timeout=5).status_code == 200
            assert session.get(base + '/static/common.js', timeout=5).status_code == 200
            assert session.get(base + '/favicon.ico', timeout=5).content == (folder / 'assets/favicon.ico').read_bytes()
            assert session.get(base + '/archive/demo/assets/favicon.svg', timeout=5).content == (folder / 'assets/favicon.svg').read_bytes()
            assert 'Паузы и лимиты' in session.get(base + '/help', timeout=5).text
            assert session.get(base + '/archive/demo/data/archive.json', timeout=5).json()['schema_version'] == 1
            assert session.post(base + '/api/quit', headers={'Origin':base}, json={}, timeout=5).status_code == 200
        assert process.wait(timeout=10) == 0
    except Exception:
        if diagnostic_log.exists():
            print(diagnostic_log.read_text(encoding='utf-8'), flush=True)
        raise
    finally:
        if process.poll() is None:
            if sys.platform == "win32":
                # The one-file bootloader owns a child process; clean up both on failure.
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            else:
                process.terminate()
            process.wait(timeout=10)
    print("Native bundle: HTML/CSS, SQLite, JSON, HTTP startup and graceful shutdown passed.")
