import json
import threading
from pathlib import Path

import pytest

from ljarchive.client import Cancelled, CommentPage, ExportError, LiveJournalClient, parse_comments
from ljarchive.exporter import Exporter
from ljarchive.render import threaded
from ljarchive.storage import Store


class FixtureClient:
    user = "my_journal"
    journal_id = 123

    def __init__(self, fail_at=None, missing_screened=False, truncated=False):
        self.cancel = threading.Event()
        self.fail_at = fail_at
        self.missing_screened = missing_screened
        self.truncated = truncated
        self.calls = []
        self.closed = False

    def check(self):
        if self.cancel.is_set():
            raise Cancelled("Остановлено")

    def login(self):
        self.calls.append("login")

    def reaction_counts(self, entities):
        return [{**item, "thread": item.get("thread", 0), "count": 0, "groups": [0]} for item in entities]

    def close(self):
        self.closed = True

    def call(self, method, **args):
        self.check()
        self.calls.append((method, args))
        if method == "getdaycounts":
            return {"daycounts": [{"date": "2012-10-12", "count": 2}, {"date": "2012-10-13", "count": 1}]}
        if method == "syncitems":
            return {"syncitems": []}
        if method == "getevents":
            day = args["day"]
            if self.fail_at == day:
                raise ExportError("Сетевая ошибка")
            ids = [1, 2] if day == 12 else [3]
            if self.truncated and day == 12:
                ids = [1]
            return {"events": [{"itemid": i, "anum": 7, "subject": f"Запись {i}",
                "eventtime": f"2012-10-{day} 10:00:00", "event": "Привет <b>мир</b>!",
                "security": "private", "props": {"taglist": "жизнь"}} for i in ids]}
        raise AssertionError(method)

    def comments(self, kind, start):
        self.check()
        self.calls.append((kind, start))
        if kind == "comment_meta":
            if start == 1:
                page = CommentPage([{"id": 1, "posterid": 42}, {"id": 2, "posterid": 42, "state": "S"}], {42: "old_friend"}, 1001, 1000)
            else:
                page = CommentPage([{"id": 1000, "state": "D"}, {"id": 1001, "posterid": 42}], {}, 1001, 1002)
        elif start == 1:
            bodies = [{"id": 1, "jitemid": 1, "body": "Корень", "parentid": 0}]
            if not self.missing_screened:
                bodies.append({"id": 2, "jitemid": 1, "body": "Скрытая тайна", "parentid": 1})
            page = CommentPage(bodies, {}, None, 501)
        elif start in (501, 1000):
            page = CommentPage([], {}, None, None)
        else:
            page = CommentPage([{"id": 1001, "jitemid": 3, "body": "После разрыва ID", "parentid": 0}], {}, None, None)
        return page, b'<?xml version="1.0"?><livejournal><comments/></livejournal>'


def run(tmp_path, client):
    return Exporter(tmp_path / "my_journal", client, images=False).run()


def test_full_export_includes_private_screened_threads_and_gaps(tmp_path):
    client = FixtureClient()
    report = run(tmp_path, client)
    assert report["status"] == "complete"
    assert report["entries_saved"] == 3
    assert report["comments_saved"] == 3
    assert report["screened_comments"] == 1
    assert report["deleted_comments"] == 1
    assert report["missing_comments"] == []
    page = (tmp_path / "my_journal/entries/1.html").read_text(encoding="utf-8")
    assert "Скрытая тайна" in page and 'class="comment depth-1 screened"' in page
    assert "old_friend" in page and "Личная" in page
    assert client.closed
    assert (tmp_path / "my_journal/assets/search-data.js").exists()


def test_missing_screened_is_partial_and_retry_fetches_bodies(tmp_path):
    report = run(tmp_path, FixtureClient(missing_screened=True))
    assert report["status"] == "partial"
    assert report["missing_comments"] == [{"id": 2, "state": "S"}]
    resumed = FixtureClient()
    assert run(tmp_path, resumed)["status"] == "complete"
    assert not any(c[0] == "getevents" for c in resumed.calls if isinstance(c, tuple))
    assert ("comment_body", 1) in resumed.calls


def test_failure_and_resume_skip_committed_days(tmp_path):
    report = run(tmp_path, FixtureClient(fail_at=13))
    assert report["status"] == "partial" and report["entries_saved"] == 2
    resumed = FixtureClient()
    report = run(tmp_path, resumed)
    assert report["status"] == "complete"
    assert [c[1]["day"] for c in resumed.calls if isinstance(c, tuple) and c[0] == "getevents"] == [13]


def test_truncated_day_is_never_complete(tmp_path):
    report = run(tmp_path, FixtureClient(truncated=True))
    assert report["status"] == "partial"
    assert report["pending_days"] == [{"day": "2012-10-12", "expected": 2}]


def test_duplicate_ids_across_days_are_not_marked_complete(tmp_path):
    class DuplicateClient(FixtureClient):
        def call(self, method, **args):
            result = super().call(method, **args)
            if method == "getevents" and args["day"] == 13:
                result["events"][0]["itemid"] = 1
            return result
    report = run(tmp_path, DuplicateClient())
    assert report["status"] == "partial"
    assert report["entries_saved"] == 2 and report["entries_expected"] == 3
    assert any("не совпадает с календарём" in issue for issue in report["issues"])


def test_restart_completed_snapshot_fetches_every_day(tmp_path):
    run(tmp_path, FixtureClient())
    fresh = FixtureClient()
    assert run(tmp_path, fresh)["status"] == "complete"
    assert [c[1]["day"] for c in fresh.calls if isinstance(c, tuple) and c[0] == "getevents"] == [12, 13]


def test_cancel_preserves_readable_partial_archive(tmp_path):
    client = FixtureClient()
    client.cancel.set()
    report = run(tmp_path, client)
    assert report["status"] == "cancelled"
    assert (tmp_path / "my_journal/index.html").exists()


def test_comment_xml_preserves_escaped_html_and_user_map():
    page = parse_comments(b'''<?xml version="1.0"?><livejournal><maxid>2</maxid><nextid>3</nextid><comments><comment id="2" posterid="7" jitemid="4" parentid="1" state="S"><body>&lt;b&gt;secret&lt;/b&gt;</body><subject>Subject</subject><properties><property name="opt_preformatted">1</property></properties></comment></comments><usermaps><usermap id="7" user="friend"/></usermaps></livejournal>''')
    assert page.comments[0]["body"] == "<b>secret</b>"
    assert page.comments[0]["parentid"] == 1 and page.users[7] == "friend"
    assert page.nextid == 3 and page.maxid == 2


@pytest.mark.parametrize("data", [b"<html>Login</html>", b"<livejournal><error>No access</error></livejournal>", b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><livejournal><comments>&x;</comments></livejournal>', b"<livejournal><comments>"])
def test_bad_xml_and_entities_fail_closed(data):
    with pytest.raises(ExportError):
        parse_comments(data)


def test_deep_or_cyclic_threads_never_drop_comments():
    chain = [{"id": i, "parentid": i - 1} for i in range(1, 2500)]
    result = threaded(chain)
    assert len(result) == 2499 and result[-1]["depth"] == 2498
    cycle = threaded([{"id": 1, "parentid": 2}, {"id": 2, "parentid": 1}, {"id": 3, "parentid": 99}])
    assert {c["id"] for c in cycle} == {1, 2, 3}


def test_store_prevents_two_writers(tmp_path):
    first = Store(tmp_path)
    try:
        with pytest.raises(ExportError):
            Store(tmp_path)
    finally:
        first.close()
    second = Store(tmp_path)
    second.close()


def test_challenge_response_never_sends_password(monkeypatch):
    import hashlib
    import xmlrpc.client
    client = LiveJournalClient("test_user", "very-secret-password")
    requests = []
    def request(method, path, **kwargs):
        raw = kwargs["data"]
        requests.append(raw)
        args, rpc_method = xmlrpc.client.loads(raw)
        if rpc_method.endswith("getchallenge"):
            value = {"challenge": "sample-challenge"}
        else:
            expected = hashlib.md5(("sample-challenge" + hashlib.md5(b"very-secret-password").hexdigest()).encode()).hexdigest()
            assert args[0]["auth_response"] == expected
            value = {"ljsession": "v1:test-session"} if rpc_method.endswith("sessiongenerate") else {"fullname": "Test"}
        return xmlrpc.client.dumps((value,), methodresponse=True).encode()
    monkeypatch.setattr(client, "_request", request)
    client.login()
    assert all(b"very-secret-password" not in r for r in requests)
    assert len(requests) == 4
    client.close()
    assert client._password_hash == "" and client._cookie is None
