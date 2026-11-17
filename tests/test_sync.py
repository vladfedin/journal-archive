import json
import threading
from pathlib import Path

import pytest

from ljarchive.client import Cancelled, ExportError, RequestBudget
from ljarchive.sync import Scope, SyncExporter, validate_scope
from ljarchive.storage import Store
from ljarchive.integrity import verify_archive


class Journal:
    user = "fixture"
    journal_id = 123
    userpics = {}

    def __init__(self, *, limit=100, fail_page=None, revision=0, removed=False, no_body=False, truncated=False):
        self.budget = RequestBudget(limit)
        self.cancel = threading.Event()
        self.calls = []
        self.fail_page, self.revision, self.removed = fail_page, revision, removed
        self.no_body, self.truncated = no_body, truncated
        self.not_before = 0

    def login(self):
        self.budget.consume("test")

    def check(self):
        pass

    def _wait(self, seconds):
        self.check()

    def close(self):
        pass

    def entry(self, eid):
        return {"itemid": eid, "anum": 7, "ditemid": eid * 256 + 7,
                "eventtime": {1: "2005-01-02 10:00:00", 2: "2026-11-01 12:00:00", 3: "2026-11-01 13:00:00"}[eid],
                "event": f"Entry {eid} revision {self.revision}", "subject": f"Post {eid}", "reply_count": 2}

    def call(self, method, **params):
        self.budget.consume("test")
        self.calls.append((method, params))
        if method == "getdaycounts":
            return {"daycounts": [{"date": "2005-01-02", "count": 1}, {"date": "2026-11-01", "count": 2 if self.truncated else 1}]}
        if method == "getevents":
            if params["selecttype"] == "one":
                return {"events": [] if self.removed else [self.entry(params["itemid"])]}
            eid = 1 if params['year'] == 2005 else 2
            return {"events": [self.entry(eid)]}
        if method == "syncitems":
            return {"syncitems": [{"item": "L-3", "time": "2026-11-01 13:00:00", "action": "create"}]} if params['lastsync'].startswith('1970') else {"syncitems": []}
        if method == "getcomments":
            eid, page = params['itemid'], params['page']
            if page == self.fail_page:
                raise ExportError('Fixture interruption')
            cid = eid * 10 + page
            node = {"userpic": "", "dtalkid": cid * 256 + 7, "parentdtalkid": 0 if page == 1 else (eid * 10 + 1) * 256 + 7,
                    "posterid": 44, "postername": "reader", "state": "S" if page == 1 else "A",
                    "body": f"Comment {cid} revision {self.revision}", "is_loaded": 1}
            if self.no_body:
                node.pop('body')
                node['is_loaded'] = 0
            return {"pages": 2, "page": page, "comments": [node]}
        raise AssertionError(method)

    def reaction_counts(self, entities):
        self.budget.consume("reactions")
        self.calls.append(("reactions", entities))
        return [{**e, "thread": e.get("thread", 0), "count": 0, "groups": [0]} for e in entities]


def run(folder, client, scope=Scope()):
    return SyncExporter(folder, client, scope).run()


def test_resume_reports_local_verification_before_login_and_can_cancel(tmp_path):
    run(tmp_path, Journal())
    events = []
    client = Journal()
    def progress(**state):
        events.append(state)
        if state.get('phase') == 'verify' and state.get('total'):
            raise Cancelled('Stopped during local verification')
    report = SyncExporter(tmp_path, client, Scope(), progress=progress).run()
    assert report['status'] == 'cancelled'
    assert not client.calls and client.budget.used == 0
    assert events[0]['phase'] == 'verify'
    assert any(e.get('phase') == 'verify' and e.get('total', 0) > 0 for e in events)
    assert not any(e.get('phase') == 'login' for e in events)
    assert report['entries_saved'] == 2 and report['comments_saved'] == 4


def test_integrity_progress_finishes_and_large_files_allow_cancellation(tmp_path):
    from ljarchive.integrity import file_hash, write_manifest
    store = Store(tmp_path)
    path = tmp_path / 'large.mp4'
    path.write_bytes(b'fixture-' * (1024 * 1024))
    write_manifest(tmp_path)
    events = []
    report = verify_archive(store, progress=lambda **state: events.append(state))
    assert report['ok'] and report['files_checked'] == 1
    assert events[-1]['completed'] == events[-1]['total'] == 1
    chunks = []
    def check():
        chunks.append(1)
        if len(chunks) == 2:
            raise Cancelled('Stop')
    with pytest.raises(Cancelled):
        file_hash(path, check=check)
    assert len(chunks) == 2
    store.close()


def test_twenty_one_years_and_period_merge_without_losing_old_records(tmp_path):
    first = Journal()
    report = run(tmp_path, first, Scope('period', '2005-01-01', '2005-12-31'))
    assert report['status'] == 'complete', report
    assert report['entries_saved'] == 1 and report['comments_saved'] == 2
    assert not any(p.get('year') == 2026 for m, p in first.calls if m == 'getevents')
    report = run(tmp_path, Journal(revision=1), Scope('period', '2026-11-01', '2026-11-01'))
    assert report['entries_saved'] == 2 and report['comments_saved'] == 4
    data = json.loads((tmp_path/'data/archive.json').read_text(encoding="utf-8"))
    assert data['entries'][0]['event'].endswith('revision 0')
    assert data['entries'][1]['event'].endswith('revision 1')
    report = run(tmp_path, Journal(revision=2))
    assert report['status'] == 'complete'
    assert len(json.loads((tmp_path/'data/archive.json').read_text(encoding="utf-8"))['versions']) >= 6


def test_url_refreshes_old_discussion_without_touching_newer_post(tmp_path):
    run(tmp_path, Journal())
    client = Journal(revision=1)
    scope = validate_scope('fixture', 'urls', urls='https://fixture.livejournal.com/263.html?thread=2823')
    report = run(tmp_path, client, scope)
    assert report['status'] == 'complete'
    assert not any(m == 'getdaycounts' for m, _ in client.calls)
    assert {p['itemid'] for m, p in client.calls if m == 'getcomments'} == {1}
    data = json.loads((tmp_path/'data/archive.json').read_text(encoding="utf-8"))
    assert data['entries'][1]['event'].endswith('revision 0')
    assert next(c for c in data['comments'] if c['id'] == 11)['body'].endswith('revision 1')


def test_resume_keeps_page_cursor_and_does_not_repeat_posts(tmp_path):
    report = run(tmp_path, Journal(fail_page=2), Scope('urls', urls=('https://fixture.livejournal.com/263.html',)))
    assert report['status'] == 'partial' and report['comments_saved'] == 1
    client = Journal()
    report = run(tmp_path, client, Scope('urls', urls=('https://fixture.livejournal.com/263.html',)))
    assert report['status'] == 'complete' and report['comments_saved'] == 2
    assert not any(m == 'getevents' for m, _ in client.calls)
    assert [p['page'] for m,p in client.calls if m == 'getcomments'] == [2]


def test_budget_resume_completes_reactions_without_restarting_text_download(tmp_path):
    scope = Scope('urls', urls=('https://fixture.livejournal.com/263.html',))
    report = run(tmp_path, Journal(limit=4), scope)
    assert report['status'] == 'partial' and report['requests_used'] == 4
    client = Journal()
    report = run(tmp_path, client, scope)
    assert report['status'] == 'complete'
    assert [m for m,_ in client.calls] == ['reactions']


def test_removed_entry_keeps_saved_text_and_marks_availability(tmp_path):
    scope = Scope('urls', urls=('https://fixture.livejournal.com/263.html',))
    run(tmp_path, Journal(), scope)
    report = run(tmp_path, Journal(removed=True), scope)
    assert report['status'] == 'partial'
    data = json.loads((tmp_path/'data/archive.json').read_text(encoding="utf-8"))
    assert data['entries'][0]['event'] == 'Entry 1 revision 0'
    assert data['entries'][0]['source_status'] == 'not_returned'


def test_old_body_is_not_proof_of_successful_refresh(tmp_path):
    scope = Scope('urls', urls=('https://fixture.livejournal.com/263.html',))
    run(tmp_path, Journal(), scope)
    report = run(tmp_path, Journal(no_body=True), scope)
    assert report['status'] == 'partial'
    data = json.loads((tmp_path/'data/archive.json').read_text(encoding="utf-8"))
    assert data['comments'][0]['body'] and data['comments'][0]['source_status'] == 'body_not_returned'


def test_truncated_day_is_reconciled_with_individual_ids(tmp_path):
    report = run(tmp_path, Journal(truncated=True))
    assert report['status'] == 'complete', report
    assert report['entries_saved'] == 3


def test_full_refresh_repairs_modified_source_and_missing_page(tmp_path):
    run(tmp_path, Journal())
    (tmp_path/'entries/1.html').unlink()
    store = Store(tmp_path)
    with store.db:
        row = json.loads(store.db.execute('SELECT payload FROM entries WHERE id=1').fetchone()[0])
        row['event'] = 'tampered'
        store.db.execute('UPDATE entries SET payload=? WHERE id=1', (json.dumps(row),))
    assert not verify_archive(store)['ok']
    store.close()
    report = run(tmp_path, Journal())
    assert not report['integrity_before']['ok']
    assert (tmp_path/'entries/1.html').is_file()
    store = Store(tmp_path)
    try:
        assert verify_archive(store)['ok']
        assert store.entries()[0]['event'] != 'tampered'
    finally: store.close()


@pytest.mark.parametrize('url', ['https://other.livejournal.com/263.html', 'https://fixture.livejournal.com.evil/263.html',
                               'file:///263.html', 'https://fixture.livejournal.com:9000/263.html', 'https://fixture.livejournal.com/'])
def test_url_scope_rejects_wrong_journal_or_shape(url):
    with pytest.raises(ExportError): validate_scope('fixture', 'urls', urls=[url])


def test_dates_validate_without_twenty_year_limit():
    assert validate_scope('fixture', 'period', '1999-01-01', '2026-11-15').includes('2005-01-02')
    with pytest.raises(ExportError): validate_scope('fixture', 'period', '2026-11-15', '1999-01-01')
    with pytest.raises(ExportError): validate_scope('fixture', 'period', '2026-02-31')
