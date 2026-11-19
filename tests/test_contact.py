def test_contact_question_matches_each_selected_account(monkeypatch):
    import hashlib
    from ljarchive import contact

    digests = {hashlib.sha256(name).hexdigest() for name in (b'first_fixture', b'second_fixture')}
    monkeypatch.setattr(contact, 'QUESTION_ACCOUNT_DIGESTS', digests)
    assert contact.has_contact_question('first_fixture')
    assert contact.has_contact_question(' Second-Fixture ')
    assert not contact.has_contact_question('second_fixture_2')
    assert not contact.has_contact_question(None)


def test_message_uses_cookie_rpc_and_requires_confirmation(monkeypatch):
    import xmlrpc.client
    from unittest.mock import patch
    import requests
    from ljarchive.contact import send_message
    calls = []
    reply = {'sent_count': 1, 'msgid': [17]}
    def transport(session, method, url, **kwargs):
        calls.append(url)
        assert method == 'POST' and url == 'https://www.livejournal.com/interface/xmlrpc'
        assert kwargs['allow_redirects'] is False and session.headers['X-LJ-Auth'] == 'cookie'
        assert session.headers['Cookie'] == 'ljsession=fixture'
        (args,), name = xmlrpc.client.loads(kwargs['data'])
        assert name == 'LJ.XMLRPC.sendmessage'
        assert args['username'] == 'account' and args['to'] == ['staerum'] and args['auth_method'] == 'cookie'
        assert args['body'].data.decode() == 'Спасибо! 🎉'
        r = requests.Response()
        r.status_code = 200
        r._content = xmlrpc.client.dumps((reply,), methodresponse=True).encode()
        r._content_consumed = True
        return r
    monkeypatch.setattr(requests.Session, 'request', transport)
    assert send_message('account', 'fixture', 'Спасибо! 🎉') == {'status': 'sent', 'message_id': '17'}
    reply['msgid'] = []
    assert send_message('account', 'fixture', 'Спасибо! 🎉')['status'] == 'unknown'
    with patch.object(requests.Session, 'request', side_effect=requests.Timeout) as timeout:
        assert send_message('account', 'fixture', 'Спасибо! 🎉')['status'] == 'unknown'
        assert timeout.call_count == 1
    assert len(calls) == 2


def test_message_stale_job_duplicate_and_sender(tmp_path, monkeypatch):
    from uuid import uuid4
    from ljarchive.server import Jobs
    from ljarchive.client import ExportError
    import pytest
    jobs = Jobs(tmp_path)
    jobs.current = {'username': 'account', 'journal': 'community'}
    jobs.state = {'job_id': 'job'}
    data = {'job_id':'job', 'token':str(uuid4()), 'body':'Привет', 'to':'another', 'username':'another'}
    with pytest.raises(ExportError): jobs.message_author(data)
    jobs.contact_cookie = 'fixture'
    count = []
    def send(account,cookie,body):
        assert (account,cookie,body) == ('account','fixture','Привет')
        assert jobs.message_author(data)['status'] == 'sending'
        count.append(1)
        return {'status':'sent','message_id':'7'}
    monkeypatch.setattr('ljarchive.server.send_message', send)
    with pytest.raises(ExportError): jobs.message_author({**data,'job_id':'old'})
    with pytest.raises(ExportError): jobs.message_author({**data,'body':'   '})
    assert jobs.message_author(data) == jobs.message_author(data) == {'status':'sent','message_id':'7'}
    assert count == [1]
