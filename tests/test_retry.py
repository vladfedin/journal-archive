import threading
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest
from ljarchive.client import LiveJournalClient, RequestBudget, ExportError, Cancelled
from ljarchive.retry import retry_after, backoff, restored_deadline
from test_limited_export import Response


def test_retry_after_seconds_and_http_date_are_not_shortened():
    assert retry_after('7200') == 7200
    assert backoff(0, '7200', limited=True) == 7200
    assert retry_after(format_datetime(datetime.fromtimestamp(10000, timezone.utc)), now=1000) == 9000
    assert retry_after('bad') == 0
    assert backoff(1, limited=True) > backoff(0, limited=True)
    assert backoff(5) == backoff(5, limited=True) == 8


def test_upgrade_keeps_server_waits_and_discards_obsolete_local_pauses(monkeypatch):
    monkeypatch.setattr('time.time', lambda: 1000)
    saved = {'request_not_before': 5000, 'request_wait': {'reason': 'incomplete'}}
    assert restored_deadline(saved) == 0
    saved['request_wait'] = {'reason': 'http', 'server_delay': False}
    assert restored_deadline(saved) == 1008
    saved['request_wait']['server_delay'] = True
    assert restored_deadline(saved) == 5000
    saved['request_wait'] = {}
    assert restored_deadline(saved) == 5000


def test_429_retry_is_paced_counted_and_persisted(monkeypatch):
    client = LiveJournalClient('fixture','x',budget=RequestBudget(3))
    waits, deadlines, updates = [], [], []
    client.save_cooldown = deadlines.append
    client.progress = lambda **v: updates.append(v)
    monkeypatch.setattr(client, '_wait', waits.append)
    responses = iter([Response(status=429, headers={'Retry-After':'7200'}), Response(content=b'ok')])
    monkeypatch.setattr(client.session,'request',lambda *a, **k:next(responses))
    assert client._request('GET','/test') == b'ok'
    assert client.budget.used == 2
    assert max(waits) > 7190 and deadlines and updates[0]['phase']=='waiting'
    client.close()


def test_403_stops_without_automatic_retries(monkeypatch):
    client = LiveJournalClient('fixture','x',budget=RequestBudget(20))
    monkeypatch.setattr(client,'_wait',lambda _:None)
    monkeypatch.setattr(client.session,'request',lambda *a, **k:Response(status=403))
    with pytest.raises(ExportError): client._request('GET','/test')
    assert client.budget.used==1
    client.close()


def test_cancel_interrupts_long_cooldown():
    client=LiveJournalClient('fixture','x')
    client.cancel.set()
    with pytest.raises(Cancelled): client._wait(7200)
    client.close()


def test_xmlrpc_rate_limit_retries_with_new_challenge(monkeypatch):
    import xmlrpc.client
    client=LiveJournalClient('fixture','test',budget=RequestBudget(10))
    monkeypatch.setattr(client,'_wait',lambda _:None)
    seen=[]
    def request(method,url,**kwargs):
        params,name=xmlrpc.client.loads(kwargs['data'])
        seen.append((name,params))
        if name.endswith('getchallenge'):
            return Response({'challenge':f'challenge-{len(seen)}'})
        if len(seen)==2:
            return Response(content=xmlrpc.client.dumps(xmlrpc.client.Fault(402,'limit')).encode())
        return Response({'events':[]})
    monkeypatch.setattr(client.session,'request',request)
    assert client.call('getevents',selecttype='lastn',howmany=10)=={'events':[]}
    assert len(seen)==client.budget.used==4
    assert seen[1][1][0]['auth_challenge'] != seen[3][1][0]['auth_challenge']
    client.close()


def test_media_retry_after_uses_same_budget(tmp_path,monkeypatch):
    from ljarchive.media import Media
    from test_media_errors import ImageResponse
    budget=RequestBudget(2)
    media=Media(tmp_path,True,threading.Event(),budget=budget)
    waits=[]
    monkeypatch.setattr(media,'wait',lambda seconds:waits.append(seconds) or True)
    monkeypatch.setattr('ljarchive.media.public_url',lambda _:None)
    replies=iter([ImageResponse(status=429,headers={'Retry-After':'3600'}),
                  Response(content=b'fixture-image',headers={'Content-Type':'image/png'})])
    monkeypatch.setattr(media.session,'get',lambda *a,**k:next(replies))
    url = 'https://example.org/image.png'
    assert media.get(url) is None
    media.retry_failed()
    assert media.mapping[url].startswith('media/')
    assert budget.used==2 and max(waits)>3590
    media.close()


def test_host_deadlines_survive_resume_without_delaying_text(tmp_path, monkeypatch):
    import time
    from ljarchive.retry import restore_cooldowns
    from ljarchive.storage import Store
    store = Store(tmp_path)
    client = LiveJournalClient('fixture', 'test')
    other = LiveJournalClient('fixture', 'test')
    try:
        stamp = time.time() + 300
        with store.db:  # Existing archive with the previous single deadline format.
            store.set('request_not_before', stamp)
            store.set('request_wait', {'host':'images.example', 'reason':'http', 'server_delay':True})
        restore_cooldowns(client, store)
        client._defer(120, url='https://video.example', category='videos', reason='http', server_delay=True)
        restore_cooldowns(other, store)
        waits = []
        monkeypatch.setattr(other, '_wait', waits.append)
        monkeypatch.setattr(other.session, 'request', lambda *a, **kw: Response(content=b'ok'))
        assert other._request('GET', '/text') == b'ok' and max(waits) < 3
        assert other.budget.deadline_for('https://images.example/b') == stamp
        assert other.budget.deadline_for('https://video.example/c') > stamp - 181
    finally:
        store.close()
        client.close()
        other.close()
