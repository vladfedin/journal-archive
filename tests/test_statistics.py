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


def test_discussion_index_matches_every_person_year_and_retains_unknown_dates():
    source = fixture()
    source['comments'][2]['body'] = '<b>Test &amp; text</b>'
    stats = statistics(source)
    index = stats['discussions']
    assert len(index['comments']) == stats['totals']['comments']
    assert index['posts'][0]['title'] == 'Old post'
    assert next(c for c in index['comments'] if c['id'] == 3)['excerpt'] == 'Test & text'
    assert next(c for c in index['comments'] if c['id'] == 1)['state_label'] == 'Удалён'
    assert next(c for c in index['comments'] if c['id'] == 2)['state_label'] == 'Скрыт'
    for person in stats['people']:
        rows = [c for c in index['comments'] if c['person'] == person['id']]
        assert len(rows) == person['comments']
        assert len({c['entry'] for c in rows}) == person['posts']
        for year, bucket in person['years'].items():
            selected = [c for c in rows if c['year'] == year]
            assert len(selected) == bucket['comments']
            assert len({c['entry'] for c in selected}) == bucket['posts']


def test_real_getcomments_utc_dates_populate_years_edges_and_findings():
    source = fixture()
    for c in source['comments']:
        if c['date'] != 'bad':
            c['date'] = c['date'][:10] + ' 04:15:04 UTC'
    stats = statistics(source)
    assert stats['totals']['unknown_dates'] == 1
    assert stats['yearly']['2000']['comments'] == 2
    assert stats['yearly']['2010']['comments'] == 4
    assert next(p for p in stats['people'] if p['id'] == 'user:2')['years']['2010']['comments'] == 2
    assert stats['revivals'][0]['days'] > 3650
    assert stats['returns'][0]['person'] == 'user:2'
    assert all('unknown' not in e['years'] for e in stats['edges'])


def test_supported_dates_preserve_local_date_and_unknown_dates_remain_unknown():
    from ljarchive.statistics import date_of
    from ljarchive.activitystreams import published
    from datetime import date
    for stamp in ('2005-04-18 04:15:04 UTC', '2005-04-18T04:15:04Z', '2005-04-18 04:15:04 GMT',
                  'Mon, 18 Apr 2005 04:15:04 GMT', '2005-04-18 04:15:04+00:00'):
        assert date_of({'date': stamp}) == date(2005,4,18)
        assert published(stamp) == '2005-04-18T04:15:04+00:00'
    assert date_of({'date':'2005-04-18 00:01:00+03:00'}) == date(2005,4,18)
    assert published('2005-04-18 04:15:04') is None
    for bad in ('2005-02-30 04:15:04 UTC', 'unknown', '2005-04-18 garbage', None):
        assert date_of({'date': bad}) is None
        assert published(bad) is None
