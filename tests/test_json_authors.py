import json
import subprocess
import sys

from ljarchive.client import LiveJournalClient, RequestBudget, RequestLimitReached
from ljarchive.data import export_json, snapshot
from ljarchive.demo import create_demo
from ljarchive.presentation import reaction_view
from ljarchive.reactions import parse_authors
from ljarchive.render import build_archive
from ljarchive.storage import Store
import pytest


def test_authors_match_types_dedupe_and_report_partial():
    counts = {"count": 3, "groups": [1, 0, 2]}
    users = [{"id": 7, "username": "one", "type": 0}, {"id": 8, "username": "two", "type": 2},
             {"id": 9, "username": "three", "type": 2}]
    complete = parse_authors(users, counts)
    assert complete["status"] == "complete"
    partial = parse_authors(users[:-1], counts)
    assert partial["status"] == "partial"
    duplicate = parse_authors(users[:2] + [users[1]], counts)
    assert len(duplicate["users"]) == 2 and duplicate["status"] == "partial"
    assert parse_authors([{**u, "type": 0} for u in users], counts)["status"] == "partial"
    invalid = parse_authors([{"id": None, "username": "x", "type": 0}], counts)
    assert invalid["status"] == "partial" and invalid["users"] == []
    view = reaction_view({**counts, "authors": complete})
    assert [u["name"] for u in view["badges"][0]["users"]] == ["one"]
    assert [u["name"] for u in view["badges"][1]["users"]] == ["two", "three"]
    assert "badges" not in counts and "icon" not in complete["users"][0]


def test_author_links_reject_unsafe_source_urls():
    data = {"count": 1, "groups": [1], "authors": {"status": "complete", "users": [
        {"id": 1, "username": '<script>alert(1)</script>', "type": 0, "profile_url": 'javascript:alert(1)'}]}}
    assert reaction_view(data)["badges"][0]["users"][0]["url"] is None


def test_json_export_is_independent_and_keeps_raw_content(tmp_path):
    folder = tmp_path / 'source'
    store = Store(folder)
    try:
        with store.db:
            store.set('username', 'fixture')
            store.save_entry({'itemid': 1, 'event': '<p style="color:red">Оригинал</p>', 'props': {'taglist': 'один'}})
            store.save_comment({'id': 2, 'jitemid': 1, 'parentid': 0, 'body': 'Строка\nСтрока', 'state': 'S'})
            counts = {'count': 1, 'groups': [1], 'captured_at': '2026-11-15T00:00:00Z'}
            store.save_reactions('post', 1, {**counts, 'authors': parse_authors([{'id': 7, 'username': 'reader', 'type': 0}], counts)})
        exported = export_json(store, tmp_path / 'standalone.json')
        assert not (folder / 'index.html').exists()
        data = json.loads(exported.read_text(encoding="utf-8"))
        assert data['schema_version'] == 1
        assert data['entries'][0]['event'] == '<p style="color:red">Оригинал</p>'
        assert data['comments'][0]['body'] == 'Строка\nСтрока'
        assert data['comments'][0]['user'] is None
        assert data['entries'][0]['reactions']['authors']['users'][0]['username'] == 'reader'
        assert not any(k in str(data) for k in ['visual_depth', 'reaction_view', 'badge', 'autoformat'])
    finally:
        store.close()
    result = subprocess.run([sys.executable, '-c', '''import sys
from ljarchive.__main__ import main
sys.argv = ['ljarchive', 'json', sys.argv[1], '--output', sys.argv[2]]
main()
assert 'ljarchive.render' not in sys.modules
assert 'jinja2' not in sys.modules
''', str(folder), str(tmp_path / 'cli.json')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / 'cli.json').read_text(encoding="utf-8")) == data


def test_rendering_does_not_mutate_exported_data(tmp_path):
    report = create_demo(tmp_path)
    store = Store(tmp_path)
    try:
        before = snapshot(store)
        build_archive(store, report)
        assert snapshot(store) == before
        assert json.loads((tmp_path / 'data/archive.json').read_text(encoding="utf-8")) == before
        for c in before['comments']:
            assert not {'html', 'depth', 'visual_depth', 'reaction_view', 'state_label'} & c.keys()
    finally:
        store.close()


def test_author_token_fetch_counts_and_limit_blocks_batch(monkeypatch):
    from test_limited_export import Response
    client = LiveJournalClient('owner', 'never-write-this', budget=RequestBudget(1))
    monkeypatch.setattr(client, '_wait', lambda _: None)
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url))
        return Response(content=b'<script>Site={"auth_token":"secret-read-token"}</script>')
    monkeypatch.setattr(client.session, 'request', request)
    try:
        with pytest.raises(RequestLimitReached):
            client.reaction_authors([{'journal': 1, 'item': 263}])
        assert len(calls) == 1 and calls[0][0] == 'GET'
        assert client.budget.used == 1
    finally:
        client.close()
    assert client._reaction_token is None


def test_author_batch_handles_reordered_missing_and_error_results(monkeypatch):
    from test_limited_export import Response
    client = LiveJournalClient('owner', '', budget=RequestBudget(1))
    client._reaction_token = 'read-token'
    monkeypatch.setattr(client, '_wait', lambda _: None)
    responses = [{'id': 3, 'result': {'result': [{'id': 12, 'username': 'third', 'type': 2}]}},
                 {'id': 1, 'error': {'message': 'unsafe server text'}}]
    monkeypatch.setattr(client.session, 'request', lambda *a, **kw: Response(content=json.dumps(responses).encode()))
    try:
        results = client.reaction_authors([{'journal': 1, 'item': n} for n in [263, 519, 775]])
        assert 'error' in results[0] and 'error' in results[1]
        assert results[2]['users'][0]['username'] == 'third'
        assert 'unsafe server text' not in str(results)
    finally:
        client.close()
