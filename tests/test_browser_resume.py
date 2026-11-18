import json
import secrets
import threading
import time
from types import SimpleNamespace

import pytest

from ljarchive.client import ExportError
from ljarchive.server import Jobs
from ljarchive.storage import Store, atomic_write, dumps


def saved_job(root):
    store = Store(root / 'fixture')
    with store.db:
        store.set('username', 'fixture')
        store.set('media_errors', {'https://ic.pics.livejournal.com/fixture/1/2/2.png': {'reason': 'web_login'}})
    store.close()
    atomic_write(root / '.jobs/fixture.json', dumps({'parameters': {'username': 'fixture', 'archive': 'fixture', 'images': True, 'mode': 'full'}, 'state': {'phase': 'partial'}}))


def wait_for(jobs, phase):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if jobs.snapshot().get('phase') == phase: return
        time.sleep(.01)
    pytest.fail(str(jobs.snapshot()))


def test_cancel_login_and_stale_confirmation_cannot_resume_another_job(tmp_path, monkeypatch):
    saved_job(tmp_path)
    jobs = Jobs(tmp_path)
    calls = []
    jobs.sessions.browser = SimpleNamespace(ready=lambda account: False, open_login=lambda account: calls.append('open'), confirm=lambda account: calls.append('confirm'))
    monkeypatch.setattr('ljarchive.server.sync', lambda *a, **k: calls.append('sync') or {'status': 'complete', 'entries_saved': 0, 'comments_saved': 0})
    password = secrets.token_hex(16)
    jobs.start({'resume': 'fixture', 'password': password})
    wait_for(jobs, 'browser_login')
    old = jobs.snapshot()['job_id']
    # A restart while awaiting the user is resumable, not permanently busy.
    assert Jobs(tmp_path).snapshot()['phase'] == 'cancelled'
    jobs.cancel.set(); jobs.thread.join(3)
    assert jobs.snapshot()['phase'] == 'cancelled' and calls == ['open']
    with pytest.raises(ExportError): jobs.confirm_browser(old)
    jobs.start({'resume': 'fixture', 'password': password})
    wait_for(jobs, 'browser_login')
    try:
        with pytest.raises(ExportError): jobs.confirm_browser(old)
        assert calls == ['open', 'open']
        jobs.confirm_browser(jobs.snapshot()['job_id']); jobs.thread.join(3)
        assert jobs.snapshot()['phase'] == 'complete' and calls == ['open', 'open', 'confirm', 'sync']
        assert password not in (tmp_path / '.jobs/fixture.json').read_text()
    finally:
        jobs.cancel.set(); jobs.thread.join(3)


def test_resource_denial_does_not_demand_login_on_main_resume(tmp_path):
    saved_job(tmp_path)
    store = Store(tmp_path / 'fixture')
    with store.db:
        store.set('media_errors', {'https://ic.pics.livejournal.com/fixture/1/2/2.png': {'reason': 'browser_http', 'http_status': 403, 'session_type': 'browser', 'attempts': 1}})
    store.close()
    jobs = Jobs(tmp_path)
    jobs.wait_for_browser = lambda *a, **k: pytest.fail('A resource denial is not a lost browser session')
    jobs.prepare_browser({'archive': 'fixture', 'images': True, '_resuming': True})


def test_old_material_error_does_not_override_main_login(tmp_path):
    jobs = Jobs(tmp_path)
    jobs.current = {'archive': 'fixture'}
    jobs.state = {'phase': 'browser_login', 'job_id': 'new'}
    jobs.thread = threading.current_thread()
    jobs.materials.states['old'] = {'id': 'old', 'phase': 'error', 'message': 'Old denial', '_main_job': 'old', '_identity': ('fixture', 0, 'browser_images', 'missing')}
    assert 'material_task' not in jobs.snapshot()
    jobs.thread = None
    jobs.state.update(phase='partial', message='Request limit')
    assert 'material_task' not in jobs.snapshot()
    jobs.materials.states['old']['_main_job'] = 'new'
    assert jobs.snapshot()['material_task']['message'] == 'Old denial'


@pytest.mark.parametrize('recovered', [True, False])
@pytest.mark.parametrize('fresh_context', [True, False])
def test_main_browser_session_loss_prompts_once_then_retries_same_file(tmp_path, recovered, fresh_context):
    from ljarchive.browser_bridge import BrowserFailure, BrowserLoginRequired
    from ljarchive.client import LiveJournalClient, RequestBudget
    from ljarchive.media import Media
    calls, prompts = [], []
    class Browser:
        def ready(self, account): return True
        def image(self, account, url, referer, budget, cancel, category):
            budget.consume(category); calls.append(url)
            if len(calls) == 1 or not recovered:
                raise BrowserLoginRequired() if fresh_context else BrowserFailure('browser_login')
            return b'file', '.png', []
    client = LiveJournalClient('fixture', '')
    client.browser = Browser()
    client.request_browser = lambda **k: prompts.append(k)
    media = Media(tmp_path, True, threading.Event(), RequestBudget(10), client)
    url = 'https://ic.pics.livejournal.com/fixture/1/2/2.png'
    try:
        path = media.get(url)
        assert bool(path) == recovered and prompts == [{'force': True}] and calls == [url, url]
        assert media.stopped != recovered
        if not recovered:
            assert media.errors[url]['status'] == 'pending'
            media.get('https://ic.pics.livejournal.com/fixture/1/3/3.png')
            assert len(calls) == 2
    finally: media.close(); client.close()
