import json

import re

from ljarchive.live import preview_index, summaries
from ljarchive.render import build_archive
from ljarchive.storage import Store


def test_pin_moves_between_posts_in_live_and_saved_feeds(tmp_path):
    store = Store(tmp_path)
    report = {'username': 'fixture', 'status': 'complete', 'entries_saved': 3, 'comments_saved': 0}
    oldest, middle, newest = [
        {'itemid': i, 'eventtime': f'200{i}-01-02 12:00:00', 'subject': f'Post {i}',
         'event': 'body', 'props': props}
        for i, props in enumerate([{'sticky': '1'}, {'sticky': '0'}, {}], 1)
    ]

    def check(expected, pinned=None, *, legacy=False):
        with store.db:
            if legacy:
                store.db.execute("DELETE FROM settings WHERE key IN ('sticky_entry_id','feed_pin')")
        build_archive(store, report)
        published = (tmp_path / 'index.html').read_text(encoding='utf-8')
        for html in (published, preview_index(store)):
            cards = re.findall(r'<article class="entry-row entry-card">(.*?)</article>', html, re.S)
            assert [re.search(r'<h2><a href="([^"]+)"', c)[1] for c in cards] == [f'entries/{i}.html' for i in expected]
            assert [eid for eid, c in zip(expected, cards) if '>Закреплено<' in c] == ([pinned] if pinned else [])
        raw = (tmp_path / 'assets/feed-data.js').read_text(encoding='utf-8')
        rows = json.loads(raw.removeprefix('window.ARCHIVE=').removesuffix(';'))['entries']
        assert rows == summaries(store)[1]
        assert rows[0]['date'] == f'200{expected[0]}-01-02 12:00:00'

    try:
        with store.db:
            store.set('username', 'fixture')
            for item in (oldest, middle, newest):
                store.save_entry(item)
        check([1, 3, 2], pinned=1, legacy=True)
        with store.db:
            store.save_entry({**middle, 'props': {'sticky': 1}})
        check([2, 3, 1], pinned=2, legacy=True)
        with store.db:
            store.save_entry({**middle, 'props': {'sticky': 1}})
            store.save_entry({**oldest, 'source_status': 'not_returned'})
        check([2, 3, 1], pinned=2)
        check([3, 2, 1], legacy=True)
        with store.db:
            store.save_entry({**middle, 'props': {'sticky': 1}})
        store.close()
        store = Store(tmp_path)
        with store.db:
            store.save_entry(middle)
        check([3, 2, 1], legacy=True)
        with store.db:
            store.save_entry({**middle, 'props': {'sticky': 1}})
            store.save_entry({**newest, 'itemid': 4, 'eventtime': '2004-01-02 12:00:00', 'props': {'sticky': 1}})
        check([4, 3, 2, 1], pinned=4, legacy=True)
    finally:
        store.close()
