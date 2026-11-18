import json
import threading
from types import SimpleNamespace

import pytest

from ljarchive.client import APIError, RequestBudget, RequestLimitReached
from ljarchive.personal import Personal, Preferences, account_data, render_personal
from ljarchive.storage import Store
from ljarchive.data import snapshot
from ljarchive.activitystreams import to_activitystreams, source_snapshot
from ljarchive.integrity import verify_archive


class PersonalClient:
    user = 'fixture'
    journal_id = 123
    profile = {'fullname': 'Владелец', 'userid': 123, 'friendgroups': [{'id': 1, 'name': 'Близкие'}]}
    def __init__(self, count=250, tie=False):
        self.budget = RequestBudget(None)
        self.cancel = threading.Event()
        self.calls = []
        self.items = [{'qid': i, 'when': 123456 if tie else 123000+i//3, 'type': 18,
                       'body': f'<p>Текст {i}</p>', 'subject': 'Личное', 'from': 'reader', 'state': 'N'} for i in range(1,count+1)]
    def check(self):
        pass
    def call(self, method, **params):
        self.budget.consume('personal')
        self.calls.append((method, params))
        if method == 'getfriends':
            return {'friends': [{'username': 'reader', 'groupmask': 2, 'fgcolor': '#ffffff'}],
                    'friendofs': [{'username': 'reader'}], 'friendgroups': [{'id': 1, 'name': 'Близкие'}]}
        if method == 'getusertags':
            return {'tags': [{'name': 'личное', 'security_level': 'private'}]}
        if method == 'getinbox':
            assert params['extended'] == 1 and 0 <= params['skip'] <= 100
            items = sorted((i for i in self.items if i['type'] in params['gettype'] and i['when'] <= params['before']), key=lambda i: (i['when'],i['qid']), reverse=True)
            return {'items': items[params['skip']:params['skip']+params['itemshow']]}
        raise AssertionError(method)
    def personal_feed(self, name):
        self.calls.append(('GET', '/data/' + name))
        if name == 'foaf':
            return b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:foaf="http://xmlns.com/foaf/0.1/"><foaf:Person><foaf:nick>fixture</foaf:nick><foaf:name>Owner</foaf:name><foaf:interest rdf:resource="https://example.org/interest"/></foaf:Person></rdf:RDF>'
        return b'<feed xmlns="http://www.w3.org/2005/Atom"><id>urn:lj:livejournal.com:atom1:fixture:userpics</id><entry><id>pic-7</id><content src="https://l-userpic.livejournal.com/7/123"/><summary>Old picture</summary><category term="old"/></entry></feed>'
    def personal_page(self, path):
        self.calls.append(('GET', path))
        if path.startswith('/allpics'):
            return b'<img src="https://l-userpic.livejournal.com/7/123" alt="old"><img src="https://l-userpic.livejournal.com/8/456">'
        return b'<input type="hidden" name="lj_form_auth" value="SECRET"><input type="checkbox" name="LJ__Setting__Privacy_comments" checked value="friends"><select name="LJ__Setting__TimeZone_timezone"><option value="UTC" selected>UTC</option></select>'


@pytest.fixture
def owner(tmp_path):
    store = Store(tmp_path)
    with store.db:
        store.set('username', 'fixture'); store.set('started_at', 'cycle-1'); store.set('userpic_catalog', {})
    obj = SimpleNamespace(store=store, client=PersonalClient(), account=True, messages=True, images=False, update=lambda *a, **k: None)
    yield obj
    if getattr(obj, '_media', None): obj._media.close()
    store.close()


def test_inbox_cursor_preserves_every_message_across_timestamp_boundaries(owner):
    collector = Personal(owner)
    collector.run(False, True)
    data = account_data(owner.store)
    assert len(data['messages']) == 250
    assert {m['qid'] for m in data['messages']} == set(range(1,251))
    assert data['sections']['messages_incoming']['status'] == 'source_exhausted'
    assert all(m['state'] == 'N' for m in data['messages'])
    calls = len(owner.client.calls)
    collector.run(False, True)
    assert len(owner.client.calls) == calls
    assert {m for m, p in owner.client.calls} == {'getinbox'}


def test_same_second_server_limit_reports_gap_and_still_reads_outgoing(owner):
    owner.client = PersonalClient(tie=True)
    Personal(owner).run(False, True)
    data = account_data(owner.store)
    assert len(data['messages']) == 200
    assert data['sections']['messages_incoming']['status'] == 'partial'
    assert data['sections']['messages_outgoing']['status'] == 'source_exhausted'
    assert len(owner.client.calls) == 4


def test_inbox_resume_uses_committed_cursor_and_has_no_duplicates(owner):
    owner.client.budget = RequestBudget(1)
    with pytest.raises(RequestLimitReached):
        Personal(owner).run(False, True)
    assert len(account_data(owner.store)['messages']) == 100
    owner.client = PersonalClient()
    Personal(owner).run(False, True)
    assert len(account_data(owner.store)['messages']) == 250
    assert owner.client.calls[0][1]['before'] < 124000


def test_profile_friends_preferences_catalog_and_messages_round_trip(owner, monkeypatch):
    monkeypatch.setattr('ljarchive.media.Media.get', lambda self, url, **kwargs: self.mapping.setdefault(url, 'media/test.png'))
    with owner.store.db:
        owner.store.set('personal_requested', {'account': True, 'messages': True})
    Personal(owner).run(True, True)
    source = snapshot(owner.store)
    standard = to_activitystreams(source)
    assert source_snapshot(standard) == source
    notes = [i for i in standard['items'] if i.get('ja:kind') == 'message']
    assert len(notes) == 250 and all(i['ja:security'] == 'private' and i['to'] for i in notes)
    assert len([i for i in standard['items'] if i['type'] == 'Follow']) == 2
    assert 'document' in source['account']['sections']['foaf']['data']
    serialized = json.dumps(source, ensure_ascii=False)
    assert 'SECRET' not in serialized and 'lj_form_auth' not in serialized
    sections = source['account']['sections']
    assert sections['friends']['data']['friends'][0]['groupmask'] == 2
    assert len(sections['userpics']['data']['pictures']) == 1
    assert sections['settings_privacy']['data']['controls'][0]['checked'] is True
    report = {'username': 'fixture', 'status': 'complete', 'issues': [], 'created_at': '2026-11-16'}
    render_personal(owner.store, report)
    assert report['personal']['complete']
    assert (owner.store.folder / 'messages-3.html').exists()
    assert 'Текст 250' in (owner.store.folder / 'messages.html').read_text(encoding='utf-8')
    assert verify_archive(owner.store)['ok']
    with owner.store.db:
        owner.store.db.execute("UPDATE messages SET payload='{}' WHERE id=1")
    assert not verify_archive(owner.store)['ok']


def test_settings_exclude_credentials_but_preserve_selected_values():
    parser = Preferences()
    parser.feed('''<input type="password" name="LJ__Setting__Password_value" value="SECRET">
      <input type="hidden" name="LJ__Setting__Token_value" value="SECRET">
      <select name="LJ__Setting__Language_language"><option>Russian</option><option>English</option></select>
      <input type="radio" name="LJ__Setting__Privacy_level" value="all" checked>
      <input type="radio" name="LJ__Setting__Privacy_level" value="friends">''')
    assert 'SECRET' not in json.dumps(parser.controls)
    assert parser.controls[0]['options'][0] == {'value': 'Russian', 'selected': True, 'label': 'Russian'}
    assert parser.controls[1]['checked'] and not parser.controls[2]['checked']


def test_unsupported_optional_method_does_not_stop_remaining_sections(owner, monkeypatch):
    original = owner.client.call
    def call(method, **params):
        if method == 'getfriends': raise APIError(method, 100)
        return original(method, **params)
    owner.client.call = call
    monkeypatch.setattr('ljarchive.media.Media.get', lambda *a, **k: None)
    Personal(owner).run(True, True)
    data = account_data(owner.store)
    assert data['sections']['friends']['status'] == 'unavailable'
    assert len(data['messages']) == 250


def test_missing_message_body_retries_only_its_timestamp_and_keeps_old_text(owner):
    client = owner.client = PersonalClient(count=2)
    Personal(owner).run(False, True)
    with owner.store.db:
        owner.store.set('started_at', 'cycle-2')
    client.items[0].pop('body')
    Personal(owner).run(False, True)
    data = account_data(owner.store)
    assert data['messages'][0]['body'] == '<p>Текст 1</p>'
    assert data['messages'][0]['source_status'] == 'body_not_returned'
    assert data['sections']['messages_incoming']['status'] == 'partial'
    repairs = [p for m, p in client.calls if m == 'getinbox' and 'lastsync' in p]
    assert repairs and repairs[0]['lastsync'] == client.items[0]['when'] == repairs[0]['before']
    client.items[0]['body'] = 'Recovered'
    Personal(owner).run(False, True)
    assert account_data(owner.store)['sections']['messages_incoming']['status'] == 'source_exhausted'


def test_malformed_or_foreign_profile_is_not_reported_as_saved(owner):
    owner.client.personal_feed = lambda name: b'<html>not a feed</html>'
    Personal(owner).run(True, False)
    sections = account_data(owner.store)['sections']
    assert sections['foaf']['status'] == sections['userpics']['status'] == 'partial'
    assert sections['friends']['status'] == 'captured'


def test_partial_friends_response_keeps_missing_lists_with_provenance(owner):
    collector = Personal(owner)
    collector.save('friends', status='captured', data={'friends': [{'username': 'old'}], 'friendofs': [{'username': 'known'}]})
    owner.personal_cycle = 'refresh-friends'
    owner.client.call = lambda *a, **k: {'friends': [{'username': 'fresh'}]}
    collector = Personal(owner)
    collector.capture('friends', collector.friends, 'LJ.XMLRPC.getfriends')
    section = collector.section('friends')
    assert section['status'] == 'partial'
    assert section['data']['friends'] == [{'username': 'fresh'}]
    assert section['data']['friendofs'] == [{'username': 'known'}]
    assert section['retained_fields'] == ['friendofs']
    assert section['latest_response'] == {'friends': [{'username': 'fresh'}]}
    owner.client.call = lambda *a, **k: {'friends': [], 'friendofs': []}
    collector.capture('friends', collector.friends, 'LJ.XMLRPC.getfriends')
    section = collector.section('friends')
    assert section['status'] == 'captured' and section['data']['friendofs'] == []
    assert 'retained_fields' not in section and 'latest_response' not in section


def test_partial_userpic_feed_keeps_old_and_new_valid_pictures(owner):
    collector = Personal(owner)
    collector.save('userpics', status='captured', data={'pictures': [{'url': 'https://l-userpic.livejournal.com/7/123'}]})
    owner.personal_cycle = 'refresh-pictures'
    owner.client.personal_feed = lambda name: b'<feed xmlns="http://www.w3.org/2005/Atom"><id>urn:lj:livejournal.com:atom1:fixture:userpics</id><entry><id>bad</id></entry><entry><content src="https://l-userpic.livejournal.com/8/123"/></entry></feed>'
    collector = Personal(owner)
    collector.capture('userpics', collector.userpics, 'data/userpics')
    section = collector.section('userpics')
    assert section['status'] == 'partial'
    assert {p['url'] for p in section['data']['pictures']} == {'https://l-userpic.livejournal.com/7/123', 'https://l-userpic.livejournal.com/8/123'}
    assert len(section['latest_response']['pictures']) == 1
