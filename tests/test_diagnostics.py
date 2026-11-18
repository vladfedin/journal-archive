import json

import requests

from ljarchive.diagnostics import build_diagnostics
from ljarchive.storage import Store
from test_render_server import local_server


def test_report_is_allowlisted_and_keeps_per_file_causes(tmp_path):
    store = Store(tmp_path)
    url = 'https://private.example/secret-picture?token=SECRET'
    with store.db:
        store.set('username', 'PRIVATE_OWNER')
        store.save_entry({'itemid': 9, 'event': 'PRIVATE_TEXT'})
        store.set('media', {url: None})
        store.set('media_errors', {url: {'reason': 'http', 'http_status': 403, 'requests': 1, 'attempts': 3,
                                      'authentication': 'sent', 'session_type': 'api', 'closed': True,
                                      'checked_at': '2026-11-17T12:00:00Z', 'message': 'SECRET'}})
        store.set('entry_materials', {'9': {'urls': [url]}})
        store.set('videos', {url: {'status': 'failed', 'entries': [9], 'title': 'PRIVATE_TEXT',
                                  'error': {'reason': 'download', 'message': 'SECRET'}}})
    report = build_diagnostics(tmp_path, {'phase': 'waiting', 'message': 'SECRET', 'events': [
        {'kind': 'wait', 'host': 'private.example', 'reason': 'http', 'http_status': 429, 'retry_at': 123}],
        'wait': {'reason': 'timeout', 'host': 'private.example'}, 'requests_used': 12},
        {'password': 'SECRET', 'username': 'PRIVATE_OWNER', 'archive': '/secret/path', 'images': True,
         'urls': [url], 'max_requests': None})
    assert report['inventory_available'] and report['materials'][0]['http_status'] == 403
    assert report['materials'][0]['entry_ids'] == [9] and report['materials'][0]['attempts'] == 3
    assert report['materials'][0]['requests'] == 1 and report['materials'][0]['closed'] is True
    assert report['materials'][0]['authentication'] == 'sent' and report['materials'][0]['session_type'] == 'api'
    assert report['reasons'] == {'http': 1, 'download': 1}
    assert report['run']['events'][0]['http_status'] == 429
    text = json.dumps(report)
    for value in ('SECRET', 'PRIVATE_TEXT', 'PRIVATE_OWNER', 'private.example', '/secret/path', 'https://'):
        assert value not in text
    store.close()


def test_unavailable_inventory_is_not_reported_as_empty(tmp_path):
    data = build_diagnostics(tmp_path, {}, {})
    assert data['inventory_available'] is False and 'counts' not in data


def test_missing_local_file_and_disabled_option_are_distinct(tmp_path):
    store = Store(tmp_path)
    with store.db:
        store.set('media', {'https://example.org/a': 'media/lost.png', 'https://example.org/b': None})
    report = build_diagnostics(tmp_path, {}, {'images': False})
    assert report['reasons'] == {'local_missing': 1, 'not_requested': 1}
    store.close()


def test_diagnostics_requires_local_session_and_rejects_traversal(local_server):
    server, base = local_server
    assert requests.get(base + '/api/diagnostics/fixture').status_code == 403
    with requests.Session() as session:
        session.get(base)
        assert session.get(base + '/api/diagnostics/fixture').json()['inventory_available'] is False
        assert session.get(base + '/api/diagnostics/%2e%2e%2foutside').status_code == 400
