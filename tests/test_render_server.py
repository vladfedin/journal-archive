import io
import json
import threading
import zipfile
from pathlib import Path

import pytest
import requests

from ljarchive.demo import create_demo
from ljarchive.media import Media, public_url
from ljarchive.render import safe_content
from ljarchive.server import make_server


def test_html_is_sanitized_and_internal_urls_rewritten(tmp_path):
    media = Media(tmp_path, False, threading.Event())
    source = '<script>alert(1)</script><img src="https://example.org/a.jpg" onerror="steal()"><a href="javascript:alert(1)">bad</a><p onclick="steal()" style="color:red">Hello</p><lj user="test_user"><a href="https://my-journal.livejournal.com/263.html?thread=519">thread</a><iframe src="https://example.org/video"></iframe>'
    result = str(safe_content(source, "https://my-journal.livejournal.com/263.html", {("my-journal.livejournal.com", "/263.html"): "1.html"}, media, {2}))
    assert "<script" not in result and "onerror" not in result and "onclick" not in result and "javascript:" not in result
    assert 'href="1.html#comment-2"' in result
    assert "test-user.livejournal.com" in result
    assert "Открыть встроенный материал" in result
    assert "Изображение в оригинале" in result
    media.close()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/image", "http://[::1]/x", "http://192.168.1.1/x", "https://example.com:22/x", "http://user:pass@example.com/x"])
def test_media_blocks_local_urls(url):
    with pytest.raises(ValueError):
        public_url(url)


@pytest.fixture
def local_server(tmp_path):
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield server, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_local_server_origin_cookie_host_and_path_guards(local_server):
    server, base = local_server
    session = requests.Session()
    assert session.get(base + "/api/status").status_code == 403
    assert session.get(base + "/").status_code == 200
    assert session.get(base + "/api/status").status_code == 200
    assert session.get(base + "/", headers={"Host": "evil.example"}).status_code == 403
    assert session.post(base + "/api/demo", json={}).status_code == 403
    assert session.post(base + "/api/demo", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert session.get(base + "/archive/%2e%2e/%2e%2e/etc/passwd").status_code == 403
    assert session.get(base + "/static/%2e%2e/templates/app.html").status_code == 403
    assert session.post(base + "/api/export", json={"username": "../escape", "password": "x"}, headers={"Origin": base}).status_code == 400
    session.close()


def test_demo_archive_and_zip_served_without_db_or_credentials(local_server):
    server, base = local_server
    session = requests.Session()
    session.get(base)
    response = session.post(base + "/api/demo", json={}, headers={"Origin": base})
    assert response.status_code == 202
    server.jobs.thread.join(timeout=10)
    status = session.get(base + "/api/status").json()
    assert status["phase"] == "demo", status
    assert session.get(base + status["archive_url"]).status_code == 200
    library = session.get(base + "/api/archives").json()
    archive = library["archives"][0]
    json_response = session.get(base + archive["json_url"])
    assert json_response.status_code == 200
    assert json_response.headers["Content-Disposition"] == 'attachment; filename="_demo.json"'
    data = json_response.json()
    from ljarchive.activitystreams import source_snapshot
    activity_response = session.get(base + archive["activitystreams_url"])
    assert activity_response.status_code == 200
    assert activity_response.headers['Content-Type'].startswith('application/activity+json')
    assert source_snapshot(activity_response.json()) == data
    assert data["schema_version"] == 1 and len(data["entries"]) == 6 and len(data["comments"]) == 12
    assert data["entries"][0]["reactions"]["authors"]["status"] == "complete"
    response = session.get(base + archive["zip_url"])
    assert response.status_code == 200
    zipped = zipfile.ZipFile(io.BytesIO(response.content))
    names = zipped.namelist()
    assert "_demo/index.html" in names
    assert "_demo/data/comments.json" in names
    assert "_demo/data/archive.json" in names
    assert "_demo/data/activitystreams-context.json" in names
    assert source_snapshot(json.loads(zipped.read('_demo/data/activitystreams.json'))) == data
    assert not any("sqlite" in n or n.endswith(".lock") for n in names)
    assert session.get(base + "/archive/_demo/archive.sqlite3").status_code == 403
    session.close()


@pytest.mark.parametrize("limit", [0, -1, True, 3.5, "50", None, 10001])
def test_http_rejects_invalid_request_limit_before_login(local_server, limit):
    server, base = local_server
    with requests.Session() as session:
        session.get(base)
        response = session.post(base + "/api/export", headers={"Origin": base}, json={
            "username": "fixture", "password": "fixture-password", "entry_limit": 10,
            "max_requests": limit, "images": False})
        assert response.status_code == 400
        assert server.jobs.snapshot()["phase"] == "idle"
        assert server.jobs.thread is None


def test_activitystreams_download_from_legacy_zip_without_database(local_server, monkeypatch):
    from ljarchive.activitystreams import source_snapshot
    server, base = local_server
    folder = server.jobs.output / 'fixture'
    create_demo(folder)
    source = (folder / 'data/archive.json').read_bytes()
    (folder / 'archive.sqlite3').unlink()
    (folder / 'data/activitystreams.json').unlink()
    monkeypatch.setattr('ljarchive.storage.Store', lambda *a, **kw: pytest.fail('No database should be created'))
    with requests.Session() as session:
        session.get(base)
        response = session.get(base + '/download/fixture.activitystreams.json')
        assert response.status_code == 200
        assert source_snapshot(response.json()) == json.loads(source)
        assert (folder / 'data/archive.json').read_bytes() == source
        assert not (folder / 'archive.sqlite3').exists()
        (folder / 'data/activitystreams.json').unlink()
        (folder / 'data/archive.json').write_text('{broken', encoding='utf-8')
        assert session.get(base + '/download/fixture.activitystreams.json').status_code == 409
