import json

import requests

from ljarchive.calendar import calendar_data
from ljarchive.demo import create_demo
from ljarchive.storage import Store
from test_render_server import local_server


def test_calendar_counts_publication_days_without_timezone_conversion(tmp_path):
    store = Store(tmp_path)
    try:
        with store.db:
            for i, stamp in enumerate(('2000-02-29 09:00:00', '2000-02-29T23:00:00-11:00',
                                        '2024-01-01T00:30:00+12:00', '1900-02-29', '', '2024-13-01'), 1):
                store.save_entry({'itemid': i, 'eventtime': stamp})
            store.save_comment({'id': 1, 'jitemid': 1, 'date': '2026-01-01'})
        data = calendar_data(store)
        assert data['days'] == {'2000-02-29': 2, '2024-01-01': 1}
        assert data['years'] == {'2000': 2, '2024': 1}
        assert data['total'] == 6 and data['undated'] == 3
    finally:
        store.close()


def test_active_archive_keeps_feed_calendar_and_timeline_separate(local_server):
    server, base = local_server
    folder = server.jobs.output / 'fixture'
    create_demo(folder)
    originals = {p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.suffix in ('.html', '.js', '.json')}
    writer = Store(folder)
    try:
        with writer.db:
            writer.save_entry({'itemid': 40, 'anum': 1, 'eventtime': '2024-06-14 23:30:00', 'subject': 'Свежая запись',
                              'event': '<p>Новый текст</p><lj-cut text="Дальше">Под катом<script>bad()</script></lj-cut>'})
        server.jobs.state = {'phase': 'comments', 'slug': 'fixture', 'username': 'example_journal', 'entry_id': 40}
        with requests.Session() as session:
            session.get(base)
            root = base + '/archive/fixture/'
            index = session.get(root + 'index.html')
            assert index.status_code == 200 and 'id="entry-list"' in index.text
            assert 'id="archive-timeline"' not in index.text and 'Свежая запись' in index.text
            data = session.get(root + 'assets/search-data.js')
            assert data.headers['Content-Type'].startswith('text/javascript')
            search = json.loads(data.text.removeprefix('window.ARCHIVE=').removesuffix(';'))
            assert len(search['entries']) == 7
            assert any('полуночи' in d['text'] for d in search['documents'])
            feed = session.get(root + 'assets/posts/40.js').text
            assert 'window.ArchivePost(40,' in feed and 'Новый текст' in feed
            assert 'lj-cut' in feed and 'bad()' not in feed
            assert 'id="calendar-grid"' in session.get(root + 'calendar.html').text
            calendar = session.get(root + 'assets/calendar-data.js').text
            calendar = json.loads(calendar.removeprefix('window.CALENDAR=').removesuffix(';'))
            assert calendar['days']['2024-06-14'] == 2
            assert 'id="archive-timeline"' in session.get(root + 'timeline.html').text
            with writer.db:
                writer.save_entry({'itemid': 40, 'eventtime': '2024-06-14 23:30:00', 'event': 'Изменённый текст'})
            assert 'Изменённый текст' in session.get(root + 'assets/posts/40.js').text
        # Preview requests must not rebuild, overwrite the export, or modify source records.
        assert all((folder / path).read_bytes() == content for path, content in originals.items())
    finally:
        writer.close()


def test_first_export_can_show_a_feed_before_any_html_is_built(local_server):
    server, base = local_server
    folder = server.jobs.output / 'fresh'
    writer = Store(folder)
    try:
        with writer.db:
            writer.set('username', 'fresh')
            writer.save_entry({'itemid': 1, 'subject': 'Первая запись', 'event': 'Полученный текст',
                               'eventtime': '2024-02-29 01:00:00'})
        server.jobs.state = {'phase': 'posts', 'slug': 'fresh', 'username': 'fresh'}
        with requests.Session() as session:
            session.get(base)
            root = base + '/archive/fresh/'
            assert 'id="entry-list"' in session.get(root + 'index.html').text
            assert 'Полученный текст' in session.get(root + 'assets/posts/1.js').text
            assert '2024-02-29' in session.get(root + 'assets/calendar-data.js').text
            assert session.get(root + 'assets/calendar.js').status_code == 200
            assert session.get(root + 'assets/posts/999.js').status_code == 404
        assert not (folder / 'index.html').exists()
    finally:
        writer.close()
