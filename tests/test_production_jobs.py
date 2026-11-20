import json
from pathlib import Path

import pytest
import requests

from ljarchive.render import ARCHIVE_FORMAT_VERSION
from ljarchive.client import ExportError, RequestBudget
from ljarchive.data import snapshot
from ljarchive.demo import create_demo
from ljarchive.server import Jobs
from ljarchive.storage import Store, dumps, atomic_write
from test_render_server import local_server


def settled(jobs):
    """Library rows once the background gap count for every card is ready."""
    jobs.gaps_started.clear()  # as if the recount pause has passed
    for _ in range(20):
        rows = jobs.archives()['archives']
        threads = list(jobs.gaps_pending.values())
        if not threads and not any(row.get('gaps_pending') for row in rows):
            return rows
        for thread in threads:
            thread.join(10)
    raise AssertionError('gap counts did not settle')


def result(status='complete'):
    return {'status':status,'entries_saved':5,'comments_saved':12,'requests_used':73,'request_limit':None,'issues':[]}


def test_unlimited_budget_tracks_every_attempt_without_implicit_fifty_limit():
    budget=RequestBudget(None)
    for _ in range(251): budget.consume('livejournal')
    assert budget.used==251 and not budget.exhausted
    assert budget.snapshot()['requests_remaining'] is None
    assert json.loads(json.dumps(budget.snapshot()))['request_limit'] is None


def test_full_http_default_has_no_request_cap(local_server,monkeypatch):
    server,base=local_server
    calls=[]
    monkeypatch.setattr('ljarchive.server.sync',lambda user,password,output,**kw: calls.append(kw) or result())
    with requests.Session() as session:
        session.get(base)
        response=session.post(base+'/api/export',headers={'Origin':base},json={'username':'fixture','password':'secret-value'})
        assert response.status_code==202,response.text
        server.jobs.thread.join(5)
        assert calls[0]['mode']=='full' and calls[0]['max_requests'] is None
        state=session.get(base+'/api/status').json()
        assert state['phase']=='complete' and state['request_limit'] is None
        assert state['parameters']['images'] is False
        assert state['parameters']['delay'] == 3
        assert 'password' not in state['parameters']
        assert 'secret-value' not in json.dumps(state)
    assert 'secret-value' not in (server.jobs.tasks/'fixture.json').read_text(encoding='utf-8')


def test_resume_restores_period_and_budget_after_restart_without_storing_password(tmp_path,monkeypatch):
    calls=[]
    def sync(user,password,output,**kw):
        calls.append((user,password,kw))
        return result('partial' if len(calls)==1 else 'complete')
    monkeypatch.setattr('ljarchive.server.sync',sync)
    jobs=Jobs(tmp_path)
    jobs.start({'username':'fixture','password':'first-secret','mode':'period','date_from':'2005-01-01',
                'date_to':'2026-11-15','images':True,'delay':7,'max_requests':120})
    jobs.thread.join(5)
    # A process may disappear between checkpoints; the restart must make this resumable.
    path=tmp_path/'.jobs/fixture.json'
    record=json.loads(path.read_text(encoding='utf-8'))
    record['state']['phase']='comments'
    atomic_write(path,dumps(record))
    restarted=Jobs(tmp_path)
    assert restarted.snapshot()['phase']=='cancelled'
    assert restarted.snapshot()['parameters']['images'] is True
    assert restarted.snapshot()['parameters']['delay'] == 7
    assert restarted.archives()['archives'][0]['resumable']
    restarted.start({'resume':'fixture','password':'new-secret','mode':'full','max_requests':1})
    restarted.thread.join(5)
    assert len(calls)==2 and calls[1][1]=='new-secret'
    params=calls[1][2]
    assert params['mode']=='period' and params['start']=='2005-01-01' and params['end']=='2026-11-15'
    assert params['images'] and params['delay']==7 and params['max_requests']==120
    assert not restarted.archives()['archives'][0]['resumable']
    source=path.read_text(encoding='utf-8')
    assert 'first-secret' not in source and 'new-secret' not in source and 'password' not in source


def test_resume_rejects_foreign_path_and_completed_tasks(tmp_path,monkeypatch):
    jobs=Jobs(tmp_path)
    with pytest.raises(ExportError): jobs.start({'resume':'../secret','password':'x'})
    monkeypatch.setattr('ljarchive.server.sync',lambda *args,**kwargs:result())
    jobs.start({'username':'fixture','password':'x'})
    jobs.thread.join(5)
    with pytest.raises(ExportError,match='завершена'): jobs.start({'resume':'fixture','password':'x'})


def test_old_archive_format_migration_preserves_source_and_never_requests_network(tmp_path,monkeypatch):
    folder=tmp_path/'example_journal'
    report=create_demo(folder)
    report['format_version']='0.2.0'
    atomic_write(folder/'export-report.json',dumps(report))
    atomic_write(folder/'index.html','old layout')
    store=Store(folder)
    try: before=snapshot(store)
    finally: store.close()
    monkeypatch.setattr(requests.Session,'request',lambda *a,**kw:pytest.fail('Migration must not use the network'))
    jobs=Jobs(tmp_path)
    jobs.refresh_format('example_journal')
    store=Store(folder)
    try: assert snapshot(store)==before
    finally: store.close()
    assert 'old layout' not in (folder/'index.html').read_text(encoding='utf-8')
    assert (folder/'help.html').is_file() and (folder/'assets/common.js').is_file()
    assert json.loads((folder/'export-report.json').read_text(encoding='utf-8'))['format_version']==ARCHIVE_FORMAT_VERSION


def test_help_works_from_local_application_and_offline_archive(local_server):
    server,base=local_server
    with requests.Session() as session:
        response=session.get(base+'/help')
        assert response.status_code==200 and 'Паузы и лимиты' in response.text
        assert '/static/common.js' in response.text
        assert session.get(base+'/api/archives').status_code==200


def test_legacy_partial_archive_parameters_are_available_for_resume(tmp_path):
    folder=tmp_path/'example_journal-latest-10'
    report=create_demo(folder)
    report.update(status='partial',entry_limit=10,request_limit=50,images_requested=True)
    atomic_write(folder/'export-report.json',dumps(report))
    row=Jobs(tmp_path).archives()['archives'][0]
    assert row['resumable'] and row['parameters']['mode']=='latest'
    assert row['parameters']['entry_limit']==10 and row['parameters']['max_requests']==50
    assert row['parameters']['images']
    assert 'images' in row['parameters_known']
    assert 'delay' not in row['parameters_known']
    assert 'videos' not in row['parameters_known']


def test_archive_card_reports_saved_media_and_what_lj_did_not_give(tmp_path):
    folder=tmp_path/'example_journal'
    report=create_demo(folder)
    # The last report covers only the last run; the card counts what is closed in the whole archive.
    report.update(images_saved=532,videos={'total':11,'saved':4},unavailable_comment_entry_ids=[136,141,148],images_closed=31)
    atomic_write(folder/'export-report.json',dumps(report))
    jobs=Jobs(tmp_path)
    row=jobs.archives()['archives'][0]
    assert (row['images_saved'],row['videos_saved'])==(532,4)
    store=Store(folder)
    with store.db:
        store.set('entry_comments',{'1':{'done':False,'error':'Не все комментарии получены.','attempts':3,'closed':True}})
    store.close()
    row=settled(jobs)[0]
    assert row['unavailable_discussions']==1 and row['unavailable_images']==0 and row['removed_images']==0
    assert isinstance(row['open_gaps'],int)
    report.pop('videos')
    atomic_write(folder/'export-report.json',dumps(report))
    assert jobs.archives()['archives'][0]['videos_saved']==0


def test_status_parameters_are_allowlisted(tmp_path):
    jobs = Jobs(tmp_path)
    assert jobs.snapshot() == {'phase': 'idle'}
    jobs.current = {'username': 'fixture', 'images': True, 'password': 'secret', 'cookies': 'private'}
    assert jobs.snapshot()['parameters'] == {'username': 'fixture', 'images': True}


def test_card_counts_unavailable_discussions_across_the_whole_archive_after_a_period_update(tmp_path):
    from ljarchive.sync import Scope, SyncExporter
    from test_sync import Journal

    class Screened(Journal):
        def entry(self, eid):
            return {**super().entry(eid), 'reply_count': 0, 'props': {'hasscreened': 1}}

        def call(self, method, **params):
            result = super().call(method, **params)
            return {'comments': []} if method == 'getcomments' else result

    folder = tmp_path / 'fixture'
    SyncExporter(folder, Screened(), Scope('full')).run()
    assert settled(Jobs(tmp_path))[0]['unavailable_discussions'] == 2
    report = SyncExporter(folder, Screened(), Scope('period', '2026-11-01', '2026-11-01')).run()
    assert len(report['unavailable_comment_entry_ids']) == 1  # the last run's report covers only its entries
    assert settled(Jobs(tmp_path))[0]['unavailable_discussions'] == 2


def test_card_counts_images_the_browser_confirmed_gone_or_closed_apart_from_gaps(tmp_path, local_server):
    folder = tmp_path / 'example_journal'
    create_demo(folder)
    gone, closed, refused = (f'https://pics.example/{name}.png' for name in ('gone', 'closed', 'refused'))
    store = Store(folder)
    with store.db:
        eid = str(store.db.execute('select id from entries order by id limit 1').fetchone()[0])
        store.set('entry_materials', {eid: {'urls': [gone, closed, refused]}})
        store.set('media', {gone: None, closed: None, refused: None})
        # Answers the browser with the user's login received: settled, not gaps to fetch again.
        store.set('media_errors', {gone: {'reason': 'browser_http', 'http_status': 404, 'availability': 'unavailable', 'closed': True},
                                   closed: {'reason': 'browser_http', 'http_status': 403, 'availability': 'unavailable', 'closed': True},
                                   refused: {'reason': 'http', 'http_status': 403, 'attempts': 3, 'closed': True}})
    store.close()
    jobs = Jobs(tmp_path)
    first = jobs.archives()['archives'][0]
    assert first.get('gaps_pending') and 'removed_images' not in first  # the list does not wait for the count
    for thread in list(jobs.gaps_pending.values()):
        thread.join(10)
    row = jobs.archives()['archives'][0]
    assert (row['unavailable_images'], row['removed_images']) == (1, 2)
    # Written to all the time: the card keeps its count and does not ask again until the pause passes.
    store = Store(folder)
    with store.db:
        store.set('entry_materials', {eid: {'urls': [gone, closed]}})
    store.close()
    row = jobs.archives()['archives'][0]
    assert (row['unavailable_images'], row['removed_images']) == (1, 2) and not row.get('gaps_pending') and not jobs.gaps_pending
    row = settled(jobs)[0]
    assert (row['unavailable_images'], row['removed_images']) == (0, 2)
    store = Store(folder)
    with store.db:
        store.set('entry_materials', {eid: {'urls': [gone, closed, refused]}})
    store.close()
    plan = jobs.recovery_plan('example_journal', whole=True)
    assert plan['confirmed'] == {'images_missing': 1, 'images_refused': 1}
    assert 'confirmed' not in jobs.recovery_plan('example_journal')
    server, base = local_server
    with requests.Session() as session:
        session.get(base)
        assert session.get(base + '/api/recovery/example_journal?whole=1').json()['confirmed'] == plan['confirmed']
        assert 'confirmed' not in session.get(base + '/api/recovery/example_journal').json()


def test_a_failing_card_count_is_not_retried_on_every_poll(tmp_path, monkeypatch, capsys):
    create_demo(tmp_path / 'example_journal')
    calls = []
    def broken(folder, **kw):
        calls.append(folder)
        raise KeyError('unexpected record')
    monkeypatch.setattr('ljarchive.recovery.read_inventory', broken)
    jobs = Jobs(tmp_path)
    rows = settled(jobs)
    assert len(calls) == 1 and 'open_gaps' not in rows[0] and not rows[0].get('gaps_pending')
    for _ in range(3):
        assert not jobs.archives()['archives'][0].get('gaps_pending')
    assert len(calls) == 1 and 'KeyError' in capsys.readouterr().err


def test_a_job_lifts_the_recount_pause_for_its_archive(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr('ljarchive.server.sync', lambda *args, **kwargs: result())
    jobs = Jobs(tmp_path)
    jobs.gaps_started.update(fixture=time.monotonic(), other=time.monotonic())
    jobs.start({'username': 'fixture', 'password': 'x'})
    jobs.thread.join(5)
    assert 'fixture' not in jobs.gaps_started and 'other' in jobs.gaps_started


def test_a_finished_material_task_lifts_the_recount_pause_for_its_archive(tmp_path, monkeypatch):
    import time
    from test_on_demand import URL, wait
    from test_targeted_materials import prepare
    prepare(tmp_path)
    monkeypatch.setattr('ljarchive.on_demand.download_one', lambda *a, **k: (_ for _ in ()).throw(ExportError('HTTP 404 · file')))
    jobs = Jobs(tmp_path)
    jobs.gaps_started.update(fixture=time.monotonic(), other=time.monotonic())
    task = jobs.materials.start({'archive': 'fixture', 'entry_id': 1, 'kind': 'image', 'value': URL})
    wait(jobs.materials, task['id'])
    for _ in range(100):
        if not jobs.materials.running:
            break
        time.sleep(0.05)
    assert 'fixture' not in jobs.gaps_started and 'other' in jobs.gaps_started
