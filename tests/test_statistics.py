from copy import deepcopy

from ljarchive.statistics import statistics


def fixture():
    return {'journal': {'username': 'owner', 'id': 1},
            'users': [{'id': 2, 'username': 'alice'}, {'id': 3, 'username': 'bob'}],
            'entries': [{'itemid': 1, 'subject': 'Old <b>post</b>', 'eventtime': '2000-01-01 12:00:00'},
                        {'itemid': 2, 'eventtime': '2010-01-01 12:00:00'}],
            'comments': [
                {'id': 1, 'jitemid': 1, 'posterid': 2, 'date': '2000-01-02T10:00:00Z', 'state': 'D'},
                {'id': 2, 'jitemid': 1, 'posterid': 3, 'parentid': 1, 'date': '2000-01-03', 'state': 'S'},
                {'id': 3, 'jitemid': 1, 'posterid': 2, 'parentid': 2, 'date': '2010-01-04'},
                {'id': 4, 'jitemid': 1, 'posterid': 2, 'parentid': 3, 'date': '2010-01-05'},
                {'id': 5, 'jitemid': 2, 'posterid': 1, 'user': 'owner', 'parentid': 2, 'date': '2010-01-06'},
                {'id': 6, 'jitemid': 2, 'posterid': 0, 'parentid': 5, 'date': '2010-01-07'},
                {'id': 7, 'jitemid': 2, 'posterid': 2, 'parentid': 99, 'date': 'bad'},
            ], 'coverage': {'posts_done': True, 'comments_done': True,
                            'discussions': {'1': {'done': True}, '2': {'done': False}}}}


def test_distinct_posts_years_reply_edges_deletion_and_coverage():
    source = fixture(); before = deepcopy(source)
    stats = statistics(source)
    assert source == before
    assert stats['years'] == list(map(str, range(2000, 2011)))
    people = {p['name']: p for p in stats['people']}
    assert people['alice']['comments'] == 4 and people['alice']['posts'] == 2
    assert people['alice']['years']['2010']['comments'] == 2
    assert people['alice']['years']['2010']['posts'] == 1
    assert people['owner']['owner']
    assert stats['yearly']['2010']['comments'] == 4
    assert stats['yearly']['2010']['identified'] == 3
    assert stats['totals']['unknown_dates'] == 1 and stats['totals']['anonymous'] == 1
    assert stats['totals']['deleted'] == 1 and stats['totals']['missing_parents'] == 2
    assert not stats['coverage']['discussions_complete']
    # No self-edges, anonymous people, cross-post parents, or invented parent authors.
    assert {(e['source'], e['target'], e['count']) for e in stats['edges']} == {
        ('user:2', 'user:3', 1), ('user:3', 'user:2', 1)}
    assert stats['revivals'][0]['url'] == 'entries/1.html#comment-3'
    assert stats['revivals'][0]['previous_url'] == 'entries/1.html#comment-2'
    assert stats['revivals'][0]['days'] > 3650
    assert stats['returns'][0]['person'] == 'user:2'
    assert stats['returns'][0]['previous_url'] == 'entries/1.html#comment-1'


def test_unknown_dates_not_assigned_post_year_and_unix_supported():
    source = fixture()
    source['comments'] = [{'id': 1, 'jitemid': 1, 'posterid': 2},
                          {'id': 2, 'jitemid': 1, 'posterid': 2, 'datepostunix': 1262304000}]
    stats = statistics(source)
    assert '2000' not in stats['yearly']
    assert stats['yearly']['2010']['comments'] == 1
    assert stats['revivals'][0]['first_comment']
    assert stats['totals']['unknown_dates'] == 1


def test_empty_archive_and_same_identity_after_rename():
    assert statistics({})['people'] == []
    source = fixture()
    source['comments'][2]['user'] = 'renamed'
    stats = statistics(source)
    assert len(stats['people']) == 3
    assert next(p for p in stats['people'] if p['id'] == 'user:2')['comments'] == 4
