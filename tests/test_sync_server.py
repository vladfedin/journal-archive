import requests
import pytest

from test_render_server import local_server


def test_scoped_update_http_passes_period_and_existing_archive(local_server, monkeypatch):
    server, base = local_server
    captured = {}
    def sync(user, password, output, **kwargs):
        captured.update(user=user, password=password, **kwargs)
        return {"status":"complete", "entries_saved":1, "comments_saved":2, "requests_used":5, "request_limit":50}
    monkeypatch.setattr('ljarchive.server.sync', sync)
    with requests.Session() as session:
        session.get(base)
        response=session.post(base+'/api/export',headers={'Origin':base},json={
            'username':'fixture','password':'test-password','mode':'period','date_from':'2005-01-01',
            'date_to':'2026-11-15','archive':'fixture-latest-10','entry_limit':None,'max_requests':50})
        assert response.status_code==202,response.text
        server.jobs.thread.join(timeout=5)
        assert captured['mode']=='period' and captured['start']=='2005-01-01'
        assert captured['archive']=='fixture-latest-10'
        state=session.get(base+'/api/status').json()
        assert state['archive_url']=='/archive/fixture-latest-10/index.html'
        assert 'test-password' not in str(state)


@pytest.mark.parametrize('extra',[
    {'mode':'period','date_from':'2026-02-31'},
    {'mode':'urls','urls':'https://other.livejournal.com/263.html'},
    {'mode':'full','archive':'../escape'},
    {'mode':'full','archive':'other'},
    {'mode':'urls','urls':[{}]},
])
def test_invalid_update_scope_fails_before_start(local_server,extra):
    server,base=local_server
    with requests.Session() as session:
        session.get(base)
        response=session.post(base+'/api/export',headers={'Origin':base},json={
            'username':'fixture','password':'test-password','entry_limit':None,**extra})
        assert response.status_code==400,response.text
        assert server.jobs.thread is None
