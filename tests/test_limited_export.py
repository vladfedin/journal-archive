import json
import threading
import xmlrpc.client

import pytest

from ljarchive.client import ExportError, LiveJournalClient, RequestBudget, RequestLimitReached
from ljarchive.exporter import Exporter, archive_slug, validate_entry_limit
from ljarchive.media import Media


class Response:
    status_code = 200
    headers = {}

    def __init__(self, value=None, status=200, headers=None, content=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = content if content is not None else xmlrpc.client.dumps((value or {},), methodresponse=True).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, *_):
        yield self.content

    def raise_for_status(self):
        assert self.status_code == 200


class Wire:
    """Simulated LJ transport; all auth, serialization and budgeting code is real."""
    def __init__(self, pages=1, placeholder=False):
        self.calls = []
        self.pages = pages
        self.placeholder = placeholder

    def request(self, method, url, **kwargs):
        if method == "GET" and url == "https://owner.livejournal.com/":
            self.calls.append(("feed_pin", {}))
            return Response(status=404)
        if method == "GET" and url == "https://www.livejournal.com/":
            self.calls.append(("reaction_token", {}))
            return Response(content=b'<script>Site={"auth_token":"fixture-read-token"}</script>')
        if url == "https://www.livejournal.com/__api/":
            batch = json.loads(kwargs["data"])
            assert len(batch) <= 10
            assert all(request["method"] == "likes.get_user_list" for request in batch)
            assert all(request["params"]["auth_token"] == "fixture-read-token" for request in batch)
            self.calls.append(("reaction_authors", batch))
            return Response(content=json.dumps([{"jsonrpc": "2.0", "id": request["id"],
                "result": {"result": [{"id": 1, "username": "reader_one", "type": 0},
                                      {"id": 2, "username": "reader_two", "type": 2},
                                      {"id": 3, "username": "reader_three", "type": 2}]}}
                for request in reversed(batch)]).encode())
        if url == "https://likes.services.livejournal.com/get":
            params = json.loads(kwargs["data"])
            assert params["mode"] == 0
            assert "Cookie" not in kwargs.get("headers", {})
            self.calls.append(("reaction_counts", params))
            return Response(content=json.dumps([{**item, "thread": item.get("thread", 0),
                "count": 3, "groups": [1, 0, 2]} for item in params["items"]]).encode())
        args, method_name = xmlrpc.client.loads(kwargs["data"])
        name = method_name.rsplit(".", 1)[-1]
        params = args[0] if args else {}
        self.calls.append((name, params))
        if name == "getchallenge":
            result = {"challenge": "fixture-challenge"}
        elif name == "login":
            result = {"fullname": "Fixture", "userid": 123}
        elif name == "sessiongenerate":
            result = {"ljsession": "fixture-session"}
        elif name == "getevents":
            assert params["selecttype"] == "lastn"
            result = {"events": [{"itemid": eid, "anum": 7, "subject": f"Post {eid}",
                "eventtime": f"2026-11-{eid:02d} 12:00:00", "event": f"Text {eid}",
                "security": "private", "reply_count": 2 * self.pages,
                "props": {"hasscreened": 1}} for eid in range(12, 12 - params["howmany"], -1)]}
        elif name == "getcomments":
            assert params["expand_strategy"] == "expand_all" and params["format"] == "thread"
            eid, page = params["itemid"], params["page"]
            cid = eid * 100 + page * 10
            child = {"dtalkid": (cid + 1) * 256 + 7, "parentdtalkid": cid * 256 + 7,
                     "userpic": "", "posterid": 1, "postername": "owner", "state": "A", "is_loaded": 1, "body": "Reply"}
            if self.placeholder and "dtalkid" not in params:
                child.pop("body")
                child["is_loaded"] = 0
            result = {"pages": self.pages, "page": page, "comments": [
                {"dtalkid": cid * 256 + 7, "parentdtalkid": 0, "posterid": 42, "postername": "friend",
                 "userpic": "", "body": "Screened text", "is_loaded": 1, "state": "S", "children": [child]}]}
        else:
            raise AssertionError(f"Unexpected method {name}")
        return Response(result)


def export_fixture(tmp_path, monkeypatch, limit=50, pages=1, placeholder=False):
    client = LiveJournalClient("owner", "fixture-password", budget=RequestBudget(limit))
    monkeypatch.setattr(client, "_wait", lambda _: client.check())
    wire = Wire(pages=pages, placeholder=placeholder)
    monkeypatch.setattr(client.session, "request", wire.request)
    report = Exporter(tmp_path / "owner-latest-10", client, images=False, entry_limit=10).run()
    return report, wire


def test_latest_ten_with_screened_comments_and_reactions_share_budget(tmp_path, monkeypatch):
    report, wire = export_fixture(tmp_path, monkeypatch)
    assert report["status"] == "complete", report
    assert report["entries_saved"] == 10 and report["comments_saved"] == 20
    assert report["screened_comments"] == 10
    assert report["requests_used"] == len(wire.calls) == 32
    assert report["reactions"]["posts_saved"] == 10
    assert report["reactions"]["comments_saved"] == 20
    assert report["reactions"]["authors_complete"]
    assert report["reactions"]["authors_saved"] == 90
    assert report["request_limit"] == 50
    assert not any(name in ("getdaycounts", "syncitems") for name, _ in wire.calls)
    entries = json.loads((tmp_path / "owner-latest-10/data/entries.json").read_text(encoding="utf-8"))
    assert {e["itemid"] for e in entries} == set(range(3, 13))
    comments = json.loads((tmp_path / "owner-latest-10/data/comments.json").read_text(encoding="utf-8"))
    assert {c["jitemid"] for c in comments} == set(range(3, 13))
    assert {c["user"] for c in comments} == {"friend", "owner"}
    assert all(c["reactions"]["count"] == 3 for c in comments)
    assert all(e["reactions"]["groups"] == [1, 0, 2] for e in entries)
    assert all(c["reactions"]["authors"]["status"] == "complete" for c in comments)
    assert "32 / 50" in (tmp_path / "owner-latest-10/report.html").read_text(encoding="utf-8")
    for file in (tmp_path / "owner-latest-10").rglob("*.json"):
        assert "fixture-read-token" not in file.read_text(encoding="utf-8") and "fixture-session" not in file.read_text(encoding="utf-8")
    assert not (tmp_path / "owner").exists()


def test_limit_stops_before_next_request_and_keeps_ten_posts(tmp_path, monkeypatch):
    report, wire = export_fixture(tmp_path, monkeypatch, limit=10)
    assert report["status"] == "partial"
    assert report["requests_used"] == len(wire.calls) == 10
    assert report["request_limit_reached"]
    assert report["entries_saved"] == 10 and report["comments_saved"] == 4
    assert len(report["pending_comment_entry_ids"]) == 8
    folder = tmp_path / "owner-latest-10"
    assert len(list((folder / "entries").glob("*.html"))) == 10
    assert json.loads((folder / "export-report.json").read_text(encoding="utf-8"))["request_limit"] == 10
    resumed, resumed_wire = export_fixture(tmp_path, monkeypatch)
    assert resumed["status"] == "complete"
    assert resumed["requests_used"] == len(resumed_wire.calls) == 26
    assert not any(name == "getevents" for name, _ in resumed_wire.calls)


def test_challenge_counts_even_if_no_budget_left_for_authenticated_call(tmp_path, monkeypatch):
    report, wire = export_fixture(tmp_path, monkeypatch, limit=7)
    assert report["entries_saved"] == 10 and report["comments_saved"] == 0
    assert report["status"] == "partial" and len(wire.calls) == 7
    assert wire.calls[-1][0] == "getchallenge"


def test_comment_pagination_counts_all_pages(tmp_path, monkeypatch):
    report, wire = export_fixture(tmp_path, monkeypatch, pages=2)
    assert report["status"] == "partial", report  # Comment text is complete; reaction authors are pending.
    assert report["comments_saved"] == 40
    assert report["requests_used"] == len(wire.calls) == 50
    assert not report["reactions"]["authors_complete"] and report["request_limit_reached"]
    assert report["reactions"]["author_posts_saved"] == 10
    assert report["reactions"]["author_comments_saved"] == 10


def test_unloaded_children_are_expanded_with_same_budget(tmp_path, monkeypatch):
    report, wire = export_fixture(tmp_path, monkeypatch, placeholder=True)
    assert report["status"] == "partial", report  # Expansion consumed the budget before reactions.
    assert report["comments_saved"] == 20
    assert report["requests_used"] == 50
    assert len([p for name, p in wire.calls if name == "getcomments" and "dtalkid" in p]) == 10


def test_reactions_cannot_exceed_budget_after_complete_text_download(tmp_path, monkeypatch):
    report, wire = export_fixture(tmp_path, monkeypatch, limit=26)
    assert report["status"] == "partial"  # The overall task includes reactions; saved text remains available.
    assert report["requests_used"] == len(wire.calls) == 26
    assert report["request_limit_reached"]
    assert not report["reactions"]["complete"]
    assert report["reactions"]["issues"]
    assert not any(name == "reaction_counts" for name, _ in wire.calls)
    assert "Реакции не загружены" in (tmp_path / "owner-latest-10/entries/12.html").read_text(encoding="utf-8")


def test_retry_cannot_bypass_request_limit(monkeypatch):
    client = LiveJournalClient("owner", "test", budget=RequestBudget(1))
    attempts = []
    monkeypatch.setattr(client, "_wait", lambda _: None)
    def request(*args, **kwargs):
        attempts.append(1)
        return Response(status=503)
    monkeypatch.setattr(client.session, "request", request)
    with pytest.raises(RequestLimitReached):
        client._request("POST", "/interface/xmlrpc")
    assert len(attempts) == client.budget.used == 1
    client.close()


def test_media_redirects_share_budget_and_stop_before_next_hop(tmp_path, monkeypatch):
    budget = RequestBudget(3)
    budget.consume("livejournal")
    budget.consume("livejournal")
    cancel = threading.Event()
    monkeypatch.setattr(cancel, "wait", lambda _: False)
    media = Media(tmp_path, True, cancel, budget=budget)
    monkeypatch.setattr("ljarchive.media.public_url", lambda _: None)
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return Response(status=302, headers={"Location": "https://images.example/second.jpg"})
    monkeypatch.setattr(media.session, "get", get)
    assert media.get("https://images.example/first.jpg") is None
    assert budget.used == 3 and budget.exhausted and len(calls) == 1
    assert media.get("https://images.example/third.jpg") is None
    assert len(calls) == 1
    media.close()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "50", 10001])
def test_budget_requires_positive_integer(value):
    with pytest.raises(ExportError):
        RequestBudget(value)


def test_sample_and_full_archives_have_different_names():
    assert archive_slug("journal", 10) == "journal-latest-10"
    assert archive_slug("journal", None) == "journal"
    with pytest.raises(ExportError):
        validate_entry_limit(51)


def test_resume_reaction_only_trial_does_not_repeat_saved_comments(tmp_path, monkeypatch):
    first, _ = export_fixture(tmp_path, monkeypatch, limit=26)
    assert first["status"] == "partial" and first["comments_saved"] == 20
    resumed, wire = export_fixture(tmp_path, monkeypatch)
    assert resumed["status"] == "complete"
    assert not any(name in ("getevents", "getcomments") for name, _ in wire.calls)


def test_repeat_sample_keeps_previous_snapshot_until_selection_is_confirmed(tmp_path, monkeypatch):
    from ljarchive.integrity import verify_archive
    from ljarchive.storage import Store

    folder = tmp_path / 'owner-latest-10'
    assert export_fixture(tmp_path, monkeypatch)[0]['status'] == 'complete'
    store = Store(folder)
    try:
        with store.db:
            store.observe_feed_pin(4 * 256 + 7)
            store.set('videos', {'old': {'status': 'not_requested', 'entries': [3]},
                                 'shared': {'status': 'not_requested', 'entries': [3, 4]}})
        old_counts = store.counts()
    finally:
        store.close()

    # Login succeeded, but the request for the new list exceeded the budget.
    report, _ = export_fixture(tmp_path, monkeypatch, limit=4)
    assert report['status'] == 'partial'
    store = Store(folder)
    try:
        assert store.counts() == old_counts
        assert store.sticky_entry_id() == 4
        assert verify_archive(store)['ok']
    finally:
        store.close()

    original_request = Wire.request
    def shifted(self, method, url, **kwargs):
        response = original_request(self, method, url, **kwargs)
        if method == 'POST' and url.endswith('/interface/xmlrpc') and self.calls[-1][0] == 'getevents':
            events = xmlrpc.client.loads(response.content)[0][0]['events']
            events[-1].update(itemid=13, eventtime='2026-11-13 12:00:00')
            return Response({'events': events})
        return response
    monkeypatch.setattr(Wire, 'request', shifted)
    assert export_fixture(tmp_path, monkeypatch)[0]['status'] == 'complete'
    store = Store(folder)
    try:
        assert {e['itemid'] for e in store.entries()} == set(range(4, 14))
        assert {c['jitemid'] for c in store.comments()} == set(range(4, 14))
        assert len(store.comments()) == 20 and store.sticky_entry_id() == 4
        assert store.get('videos') == {'shared': {'status': 'not_requested', 'entries': [4]}}
        assert not (folder / 'entries/3.html').exists()
        assert not (folder / 'assets/posts/3.js').exists()
        assert verify_archive(store)['ok']
    finally:
        store.close()


def test_repeat_string_ids_retain_discussions_before_first_comment_request(tmp_path, monkeypatch):
    from ljarchive.storage import Store

    assert export_fixture(tmp_path, monkeypatch)[0]['status'] == 'complete'
    original = Wire.request
    def string_ids(self, method, url, **kwargs):
        response = original(self, method, url, **kwargs)
        if method == 'POST' and url.endswith('/interface/xmlrpc') and self.calls[-1][0] == 'getevents':
            events = xmlrpc.client.loads(response.content)[0][0]['events']
            return Response({'events': [{**event, 'itemid': str(event['itemid'])} for event in events]})
        return response
    monkeypatch.setattr(Wire, 'request', string_ids)
    assert export_fixture(tmp_path, monkeypatch, limit=6)[0]['status'] == 'partial'
    store = Store(tmp_path / 'owner-latest-10')
    try:
        assert store.counts() == {'entries': 10, 'comments': 20, 'comment_meta': 20}
        assert len(store.reactions('comment')) == 20
    finally:
        store.close()

def test_trial_refresh_keeps_missing_reply_and_reports_failed_reaction_refresh(tmp_path, monkeypatch):
    from ljarchive.storage import Store

    assert export_fixture(tmp_path, monkeypatch)[0]['status'] == 'complete'
    original = Wire.request
    def fewer_replies(self, method, url, **kwargs):
        if url == 'https://likes.services.livejournal.com/get':
            return Response(status=404)
        response = original(self, method, url, **kwargs)
        if method == 'POST' and url.endswith('/interface/xmlrpc'):
            name, params = self.calls[-1]
            if name in ('getevents', 'getcomments'):
                result = xmlrpc.client.loads(response.content)[0][0]
                if name == 'getevents':
                    for entry in result['events']:
                        if entry['itemid'] == 3:
                            entry['reply_count'] = 1
                elif params['itemid'] == 3:
                    result['comments'][0]['children'] = []
                return Response(result)
        return response
    monkeypatch.setattr(Wire, 'request', fewer_replies)
    report, _ = export_fixture(tmp_path, monkeypatch)
    assert report['status'] == 'partial' and any('404' in reason for reason in report['issues'])
    store = Store(tmp_path / 'owner-latest-10')
    try:
        reply = next(c for c in store.comments(3) if c['id'] == 311)
        assert reply['source_status'] == 'not_returned' and reply['body'] == 'Reply'
        assert not store.get('finished') and len(store.reactions('comment')) == 20
    finally:
        store.close()
