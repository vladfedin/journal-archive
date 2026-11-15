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
    subprocess.run([str(binary), "demo", "--output", str(folder)], cwd=directory, env=env, check=True, timeout=90)
    data = json.loads((folder / "data/archive.json").read_text(encoding="utf-8"))
    assert len(data["entries"]) == 6 and len(data["comments"]) == 12
    assert (folder / "assets/archive.css").is_file()
    assert (folder / "index.html").is_file()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([str(binary), "serve", "--no-browser", "--port", str(port), "--output", directory],
                               cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        with requests.Session() as session:
            for attempt in range(60):
                if process.poll() is not None:
                    raise RuntimeError("Native HTTP server exited before startup")
                try:
                    response = session.get(base + "/", timeout=1)
                    break
                except requests.ConnectionError:
                    time.sleep(0.25)
            else:
                raise RuntimeError("Native HTTP server did not start")
            assert response.status_code == 200 and 'date-from' in response.text
            assert session.get(base + '/static/archive.css', timeout=5).status_code == 200
            assert session.get(base + '/archive/demo/data/archive.json', timeout=5).json()['schema_version'] == 1
            assert session.post(base + '/api/quit', headers={'Origin':base}, json={}, timeout=5).status_code == 200
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
    print("Native bundle: HTML/CSS, SQLite, JSON, HTTP startup and graceful shutdown passed.")
