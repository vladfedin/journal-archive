from types import SimpleNamespace
import json

import pytest

from ljarchive.client import IncompleteData, LiveJournalClient, RequestLimitReached
from ljarchive.fulltext import personal_docs
from ljarchive.personal import Personal, account_data
from ljarchive.social import social_lists, subscription_page
from ljarchive.storage import Store


def tab(name, *people, total=None):
    return {'list_name': name, 'count': len(people) if total is None else total,
            'list': [{'lc_name': p, 'display_name': p} for p in people]}


def test_archive_distinguishes_direction_and_role_without_inferring_subscription():
    def users(*items):
        return [{'username': name, 'type': kind} for name, kind in items]
    sections = {
        'friends': {'data': {'friends': users(('mutual', 'personal'), ('read', 'community'), ('rss', 'syndicated'), (1042, 'personal')),
                             'friendofs': users(('mutual', 'personal'), ('joined', 'community'))}},
        'profile': {'data': {'usejournals': ['can_post']}},
        'subscriptions': {'data': {'users': users(('news', 'personal')), 'total': 1}},
    }
    rows = {r['id']: [u['username'] for u in r['users']] if r['users'] is not None else None for r in social_lists(sections)}
    assert rows == {'friends': ['mutual', '1042'], 'friendofs': ['mutual'], 'feeds': ['rss'],
                    'watched-communities': ['read'], 'member-communities': ['joined'],
                    'subscriptions': ['news'], 'subscribers': None, 'managed_communities': None}
    docs = list(personal_docs('fixture', {'sections': sections}))
    assert any(d['href'] == 'account.html#subscriptions' and d['title'] == 'news' for d in docs)


def test_subscriptions_keep_preview_on_interruption_and_replace_on_complete(tmp_path, monkeypatch):
    store = Store(tmp_path)
    client = LiveJournalClient('fixture', 'unused')
    responses = []
    def request(method, path, **kwargs):
        responses.append((method, path))
        if method == 'GET':
            data = {'subs': [tab('subscribers', 'fresh', total=2), tab('subscribersof')]}
            return ('<script>Site.journal = {"username":"fixture"}; Site.page = ' + json.dumps(data) +
                    '; Site.entry = {"auth_token":"fixture-only"};</script>').encode()
        payload, = json.loads(kwargs['data'])
        assert payload['params']['get_list'] == 'subscribers'
        if len(responses) == 2:
            raise RequestLimitReached('limit')
        return json.dumps([{'id': 1, 'result': {**tab('subscribers', 'fresh', 'more'), 'status': 'ok'}}]).encode()
    monkeypatch.setattr(client, '_request', request)
    collector = Personal(SimpleNamespace(store=store, client=client, update=lambda *args: None))
    try:
        collector.save('subscriptions', status='captured', data={'users': [{'username': 'old'}], 'total': 1})
        collector.cycle = 'refresh'
        with pytest.raises(RequestLimitReached):
            collector.capture('subscriptions', lambda: collector.subscriptions('subscriptions'), 'profile')
        assert collector.section('subscribers')['status'] == 'captured'
        partial = collector.section('subscriptions')
        assert partial['status'] == 'partial'
        assert {p['username'] for p in partial['data']['users']} == {'old', 'fresh'}
        collector.capture('subscriptions', lambda: collector.subscriptions('subscriptions'), 'profile')
        collector.capture('subscribers', lambda: collector.subscriptions('subscribers'), 'profile')
        assert [p['username'] for p in collector.section('subscriptions')['data']['users']] == ['fresh', 'more']
        assert collector.section('subscribers')['data'] == {'users': [], 'total': 0}
        assert responses == [('GET', '/profile/subslist/'), ('POST', '/__api/'), ('POST', '/__api/')]
        assert 'fixture-only' not in json.dumps(account_data(store))
        with pytest.raises(IncompleteData):
            subscription_page(b'Site.journal = {"username":"other"}; Site.page = {"subs":[]}', 'fixture')
    finally:
        store.close()
        client.session.close()


def test_known_empty_direction_and_unusual_profile_urls():
    from ljarchive.social import social_profile_url
    sections = {'friends': {'data': {'friends': []}, 'retained_fields': [], 'error': 'partial'}}
    groups = {group['id']: group for group in social_lists(sections)}
    assert groups['friends']['users'] == [] and not groups['friends']['retained']
    assert groups['friendofs']['users'] is None
    supplied = 'https://www.livejournal.com/profile/?userid=100&t=I'
    assert social_profile_url({'profile_url': supplied}, 'Display name') == supplied
    assert social_profile_url({'profile_url': 'https://example.org/profile/'}, '_reader') == 'https://users.livejournal.com/_reader/profile/'

def test_managed_list_requires_positive_empty_evidence(monkeypatch):
    client = LiveJournalClient('fixture', 'unused')
    client._cookie = 'fixture-session'
    frame = '<title>Manage Communities</title><script>Site.remote = {"username":"fixture"}</script>'
    try:
        monkeypatch.setattr(client, '_request', lambda *a, **kw: (frame + '<p>Try again later.</p>').encode())
        with pytest.raises(IncompleteData):
            client.managed_communities(confirm_empty=True)
        monkeypatch.setattr(client, '_request', lambda *a, **kw: (frame + '<p>You are not managing any communities.</p>').encode())
        assert client.managed_communities(confirm_empty=True) == []
    finally:
        client.session.close()
