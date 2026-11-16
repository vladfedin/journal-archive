import json
from pathlib import Path

import pytest
import requests

from ljarchive.client import ExportError, RequestBudget
from ljarchive.data import snapshot
from ljarchive.demo import create_demo
from ljarchive.server import Jobs
from ljarchive.storage import Store, dumps, atomic_write
from test_render_server import local_server


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
    assert json.loads((folder/'export-report.json').read_text(encoding='utf-8'))['format_version']=='0.5.0'


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
