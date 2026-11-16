import copy
import json
from pathlib import Path
import subprocess
import sys

from pyld import jsonld
import pytest

from ljarchive.activitystreams import AS_CONTEXT, CONTEXT, NAMESPACE, source_snapshot, to_activitystreams
from ljarchive.data import snapshot
from ljarchive.demo import create_demo
from ljarchive.storage import Store


@pytest.fixture
def source(tmp_path):
    create_demo(tmp_path)
    store = Store(tmp_path)
    try:
        return snapshot(store)
    finally:
        store.close()


def loader(url, options=None):
    assert url in (AS_CONTEXT, AS_CONTEXT + '.jsonld'), 'JSON-LD must not access the network'
    path = Path(__file__).parents[1] / 'ljarchive/schemas/activitystreams-context.json'
    return {'contextUrl': None, 'documentUrl': AS_CONTEXT, 'document': json.loads(path.read_text(encoding='utf-8'))}


def post(document, number):
    return next(item for item in document['items'] if item['id'].endswith(':post:' + str(number)))


def comment(document, number):
    return next(item for item in document['items'] if item['id'].endswith(':comment:' + str(number)))


def test_exact_round_trip_including_unknown_data_and_jsonld_processing(source):
    source['future'] = {'@context': 'not-a-context', '@id': 'not-an-id', 'type': None,
                        'ordered': [3, None, 1, 3, False, '', {'id': 9007199254740993}],
                        'namespaced:key': {'new': 'Юникод 🐈\n<lj-cut text="дальше">текст</lj-cut>'}}
    source['entries'][0]['props']['unknown'] = {'arbitrary': ['x', None, 'x']}
    source['entries'][0]['allowmask'] = 37
    source['entries'][0]['security'] = 'usemask'
    source['versions'] = [{'kind': 'post', 'record': {'event': 'previous'}, 'saved_at': 'unknown'}]
    before = copy.deepcopy(source)
    doc = to_activitystreams(source)
    assert source == before and source_snapshot(doc) == source
    assert source_snapshot(json.loads(json.dumps(doc))) == source
    options = {'documentLoader': loader, 'processingMode': 'json-ld-1.0'}
    expanded = jsonld.expand(doc, options=options)
    assert expanded[0]['@type'] == [AS_CONTEXT + '#Collection']
    assert NAMESPACE + 'source' in expanded[0]
    compacted = jsonld.compact(expanded, CONTEXT, options=options)
    assert source_snapshot(compacted) == source
    assert post(doc, source['entries'][0]['itemid'])['ja:allowmask'] == 37


def test_relationships_and_selected_pictures_survive_in_standard_projection(source):
    doc = to_activitystreams(source)
    objects = {item['id']: item for item in doc['items']}
    for original in source['comments']:
        item = comment(doc, original['id'])
        parent = comment(doc, original['parentid']) if original['parentid'] else post(doc, original['jitemid'])
        assert item['inReplyTo'] == parent['id']
        assert item['id'] in parent['replies']['items']
        assert item['context'] == post(doc, original['jitemid'])['id']
        if original['user']:
            assert item['attributedTo']['name'] == original['user']
        else:
            assert 'attributedTo' not in item
        pic = source['userpics']['comment'][str(original['id'])]
        assert item['icon']['ja:localPath'] == pic['path']
        assert item['ja:state'] == original['state']
        assert 'to' not in item  # Do not infer audience through screened ancestors.
    assert doc['totalItems'] == len(objects) == len(doc['items'])


def test_reactions_keep_actor_type_and_capture_time_without_fabricated_publication(source):
    entry = source['entries'][0]
    entry['reactions'] = {'count': 5, 'groups': [1, 0, 2, 0, 1], 'authors': {
        'status': 'partial', 'captured_at': '2026-11-15T22:00:00Z', 'users': [
            {'id': 10, 'username': 'reader', 'type': 0},
            {'id': 11, 'username': 'other', 'type': 4},
            {'id': 12, 'username': 'third', 'type': 2},
            {'id': 13, 'username': 'fourth', 'type': 99}]}}
    doc = to_activitystreams(source)
    item = post(doc, entry['itemid'])
    reactions = [r for r in doc['items'] if r.get('object') == item['id']]
    assert [r['type'] for r in reactions] == ['Like', 'Dislike', ['Activity', 'ja:Reaction'], ['Activity', 'ja:Reaction']]
    assert [r['ja:reactionType'] for r in reactions] == [0, 4, 2, 99]
    assert item['ja:reactionCount'] == 5 and item['ja:reactionAuthorsStatus'] == 'partial'
    assert len(item['ja:reactions']['items']) == 4
    assert all('published' not in r and r['ja:capturedAt'].startswith('2026-11-15') for r in reactions)
    assert source_snapshot(doc)['entries'][0]['reactions'] == entry['reactions']


def test_access_missing_unknown_and_deleted_content_are_not_conflated(source):
    source['entries'][0].update(security='private', event={'base64': '/w==', 'encoding_unknown': True})
    source['entries'][1].update(security='usemask', allowmask=5, source_status='not_returned')
    source['comments'][0].pop('body')
    source['comments'][1]['body'] = ''
    source['comments'][2]['state'] = 'D'
    source['comments'][3]['state'] = 'S'
    source['comments'][4]['parentid'] = 998
    source['comment_metadata'].append({'id': 999, 'posterid': 0, 'state': 'S'})
    doc = to_activitystreams(source)
    private = post(doc, source['entries'][0]['itemid'])
    assert private['ja:security'] == 'private' and private['to'] != [AS_CONTEXT + '#Public']
    assert 'content' not in private and private['ja:contentStatus'] == 'unknown-encoding'
    restricted = post(doc, source['entries'][1]['itemid'])
    assert 'to' not in restricted and restricted['type'] == 'Article'
    assert 'content' not in comment(doc, source['comments'][0]['id'])
    assert comment(doc, source['comments'][1]['id'])['content'] == ''
    assert comment(doc, source['comments'][2]['id'])['type'] == 'Tombstone'
    assert comment(doc, source['comments'][3]['id'])['type'] == 'Note'
    assert comment(doc, source['comments'][4]['id'])['inReplyTo'].endswith(':comment:998')
    assert comment(doc, 999)['ja:metadataOnly'] is True
    assert 'inReplyTo' not in comment(doc, 999) and 'attributedTo' not in comment(doc, 999)
    assert source_snapshot(doc) == source


def test_dates_html_media_and_deterministic_ids(source):
    entry = source['entries'][0]
    entry.update(eventtime='2005-01-02 13:14:15', url='https://example.livejournal.com/263.html',
                 event='<p>Hello</p><lj user="reader"/><lj-cut text="More"><img src="/photo.png"><b>World</b></lj-cut><script>bad()</script>')
    source['media']['https://example.livejournal.com/photo.png'] = 'media/photo.png'
    source['comments'][0]['date'] = 'Wed, 02 Oct 2002 13:00:00 GMT'
    doc = to_activitystreams(source)
    item = post(doc, entry['itemid'])
    assert 'published' not in item and item['ja:localPublished'] == entry['eventtime']
    assert comment(doc, source['comments'][0]['id'])['published'] == '2002-10-02T13:00:00+00:00'
    assert '<lj' not in item['content'] and '<script' not in item['content']
    assert 'https://reader.livejournal.com/' in item['content'] and '<b>World</b>' in item['content']
    assert item['attachment'][0]['ja:localPath'] == 'media/photo.png'
    assert item['attachment'][0]['url']['href'] == 'https://example.livejournal.com/photo.png'
    assert doc == to_activitystreams(source)
    changed = copy.deepcopy(source)
    changed['entries'][0]['event'] = 'edited'
    assert item['id'] == post(to_activitystreams(changed), entry['itemid'])['id']


def test_damaged_source_or_unsupported_profile_is_rejected(source):
    doc = to_activitystreams(source)
    doc['ja:source']['content'] += ' '
    with pytest.raises(ValueError, match='checksum'):
        source_snapshot(doc)
    doc['ja:profileVersion'] = 999
    with pytest.raises(ValueError, match='version'):
        source_snapshot(doc)


def test_cli_export_works_offline_without_renderer_and_matches_source(tmp_path):
    folder = tmp_path / 'journal'
    create_demo(folder)
    output = tmp_path / 'standard.json'
    code = '''import sys
import requests
requests.Session.request = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('No network'))
from ljarchive.__main__ import main
sys.argv = ['ljarchive', 'json', sys.argv[1], '--format', 'activitystreams', '--output', sys.argv[2]]
main()
assert 'ljarchive.render' not in sys.modules and 'jinja2' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', code, str(folder), str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert source_snapshot(json.loads(output.read_text(encoding='utf-8'))) == json.loads((folder/'data/archive.json').read_text(encoding='utf-8'))
