"""Isolated browser integration: python tests/browser_smoke.py (requires Chromium).

Uses a temporary archive and a local HTTP server, never contacts LiveJournal.
"""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
import zipfile

from playwright.sync_api import expect, sync_playwright
from ljarchive.demo import create_demo
from ljarchive.server import make_server


def main():
    with tempfile.TemporaryDirectory(prefix='ljarchive-browser-') as directory:
        root = Path(directory)
        moved_root = root.with_name(root.name + "-moved")
        server = make_server(root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        calls = []

        def fake_sync(user, password, output, **kwargs):
            calls.append((user, kwargs))
            assert password == 'browser-test-secret'
            report = create_demo(output / kwargs['archive'])
            report['status'] = 'complete'
            return report

        try:
            with patch('ljarchive.server.sync', fake_sync), sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1100}, color_scheme='light')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base)
                expect(page.locator('#empty')).to_be_visible()
                page.locator('.heading [data-new]').click()
                expect(page.locator('#username')).to_be_focused()
                expect(page.locator('[data-scope=full]')).to_have_attribute('aria-pressed', 'true')
                expect(page.locator('#request-limit')).to_have_value('')
                expect(page.locator('#images')).not_to_be_checked()
                page.locator('[data-scope=latest]').click()
                expect(page.locator('#request-limit')).to_have_value('50')
                page.locator('[data-scope=period]').click()
                expect(page.locator('#date-from')).to_be_visible()
                expect(page.locator('#request-limit')).to_have_value('')
                page.locator('[data-scope=full]').click()
                page.locator('#username').fill('example_journal')
                page.locator('#password').fill('browser-test-secret')
                page.locator('#submit-export').click()
                expect(page.locator('#job-stage')).to_have_text('Готов', timeout=15000)
                expect(page.locator('#password')).to_have_value('')
                assert calls[0][1]['mode'] == 'full' and calls[0][1]['max_requests'] is None
                page.locator('#job-action').click()
                page.wait_for_timeout(5200)
                expect(page.locator('#job')).to_be_hidden()
                expect(page.locator('#archives tr')).to_have_count(1)
                page.locator('#archives summary').click()
                page.wait_for_timeout(5200)  # Polling must leave an open action menu intact.
                expect(page.locator('#archives details')).to_have_attribute('open', '')
                page.get_by_role('button', name='Обновить', exact=True).click()
                expect(page.locator('#password')).to_be_focused()
                expect(page.locator('[data-scope=period]')).to_have_attribute('aria-pressed', 'true')
                page.locator('[data-scope=urls]').click()
                expect(page.locator('#entry-urls')).to_be_visible()
                expect(page.locator('[data-scope=latest]')).to_be_hidden()
                page.keyboard.press('Escape')
                page.screenshot(path=str(root / 'library.png'), full_page=True)
                page.goto(base + '/archive/example_journal/index.html')
                expect(page.get_by_role('link', name='Все архивы', exact=True)).to_be_visible()
                page.get_by_role('link', name='Все архивы', exact=True).click()
                expect(page).to_have_url(base + '/')
                expect(page.locator('#archives tr')).to_have_count(1)
                page.goto(base + '/archive/example_journal/index.html')
                expect(page.locator('.entry-card')).to_have_count(6)
                page.get_by_role('link', name='Статистика ↗').click()
                expect(page.locator('h1')).to_have_text('Статистика')
                expect(page.locator('#stats-people .stats-participants tr')).not_to_have_count(0)
                assert page.locator('#stats-people a.heat').count() > 0
                expect(page.locator('.stats-totals tr')).to_have_count(2)
                expect(page.locator('.stats-totals tr').first).to_contain_text('12')
                page.locator('.stats-totals a[href=\"discussions.html?kind=posts&year=2008\"]').click()
                expect(page.locator('#discussion-count')).to_contain_text('Записей: 1')
                page.locator('#discussion-back').click()
                page.locator('#stats-year').select_option('')
                for width in (1440, 320):
                    page.set_viewport_size({'width': width, 'height': 1100})
                    scroll = page.locator('.stats-scroll').first
                    scroll.evaluate('e => e.scrollLeft = 300')
                    cells = page.locator('#stats-people .stats-participants tr').first.evaluate('e => [...e.children].slice(0,2).map(c => {let b=c.getBoundingClientRect();return {left:b.left,right:b.right}})')
                    assert abs(cells[0]['right'] - cells[1]['left']) < 1, cells
                    scroll.evaluate('e => e.scrollLeft = 0')
                page.set_viewport_size({'width': 1440, 'height': 1100})
                page.locator('#stats-owner').check()
                expect(page.locator('#stats-graph')).to_be_visible()
                assert page.locator('#stats-graph .graph-edge').count() > 0
                page.locator('#stats-metric').select_option('posts')
                page.locator('#stats-year').select_option('2012')
                expect(page.locator('#stats-people .stats-participants')).to_contain_text('old_friend')
                page.locator('#stats-search').fill('old_friend')
                expect(page.locator('#stats-people .stats-participants tr')).to_have_count(1)
                page.locator('#stats-metric').select_option('share')
                expect(page.locator('#stats-people .stats-participants')).to_contain_text('%')
                scroll = page.locator('.stats-scroll').first
                assert scroll.evaluate('e => e.scrollWidth <= e.clientWidth + 1')
                cell = page.locator('#stats-people a.heat').first
                assert 'discussions.html?' in cell.get_attribute('href')
                cell.click()
                expect(page.locator('#discussion-title')).to_contain_text('old_friend')
                expect(page.locator('.discussion-card')).not_to_have_count(0)
                expect(page.locator('.discussion-comments li')).not_to_have_count(0)
                expect(page.locator('.discussion-comments li').first).to_contain_text('2012')
                page.locator('#discussion-back').click()
                expect(page.locator('#stats-search')).to_have_value('old_friend')
                expect(page.locator('#stats-year')).to_have_value('2012')
                page.locator('#stats-search').fill('')
                page.locator('#stats-year').select_option('')
                page.screenshot(path=str(root / 'statistics.png'), full_page=True)
                page.locator('[data-stats-tab=findings]').click()
                expect(page.locator('#stats-returns')).to_contain_text('old_friend')
                page.screenshot(path=str(root / 'findings.png'), full_page=True)
                for width in (320, 390):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                    page.locator('[data-stats-tab=explore]').click()
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.set_viewport_size({'width': 1440, 'height': 1100})
                stats_json = page.request.get(base + '/archive/example_journal/data/statistics.json').json()
                assert stats_json['totals']['comments'] == 12
                assert stats_json['totals']['unknown_dates'] == 0
                assert sum(y['comments'] for y in stats_json['yearly'].values()) == 12
                page.goto((root / 'example_journal/statistics.html').as_uri())
                expect(page.locator('#stats-people .stats-participants')).to_contain_text('old_friend')
                page.locator('#stats-people a.heat').first.click()
                expect(page.locator('.discussion-card')).not_to_have_count(0)
                assert page.url.startswith('file:')
                page.locator('#discussion-back').click()
                page.locator('#stats-owner').check()
                expect(page.locator('#stats-graph')).to_be_visible()
                page.goto(base + '/archive/example_journal/index.html')
                page.locator('[data-view=full]').click()
                expect(page.locator('.full-post .feed-body .prose')).to_have_count(6)
                expect(page.locator('.full-post .userpic')).to_have_count(6)
                cut = page.locator('.full-post .lj-cut')
                expect(cut).not_to_have_attribute('open', '')
                expect(cut.locator('img')).to_be_hidden()
                cut.locator('summary').click()
                expect(cut.locator('img')).to_be_visible()
                assert cut.locator('img').evaluate('(img) => img.complete && img.naturalWidth > 0')
                page.screenshot(path=str(root / 'full-journal.png'), full_page=True)
                page.reload()
                expect(page.locator('[data-view=full]')).to_have_attribute('aria-pressed', 'true')
                page.locator('[data-view=compact]').click()
                expect(page.locator('.entry-card .reaction')).to_have_count(6)
                page.locator('#year').select_option('2012')
                expect(page.locator('.entry-card')).to_have_count(1)
                page.locator('#reset-filters').click()
                page.locator('#tag').select_option('книги')
                expect(page.locator('.entry-card')).to_have_count(1)
                page.locator('#reset-filters').click()
                page.locator('#search').fill('полуночи')
                expect(page.locator('.entry-card')).to_have_count(1)
                expect(page.locator('.entry-card')).to_contain_text('Совпадение в комментарии')
                page.locator('.entry-card h2 a').click()
                page.wait_for_url('**/entries/3.html#comment-4')
                expect(page.locator('.post > .reactions [aria-label="Нравится: 5"]')).to_be_visible()
                reaction = page.locator('#comment-4 [aria-label="Пиши ещё!: 1"]')
                reaction.click()
                expect(page.locator('#comment-4 .reaction-detail[open] .reaction-people')).to_contain_text('sea_notes')
                page.keyboard.press('Escape')
                expect(page.locator('#comment-4 .reaction-detail[open]')).to_have_count(0)
                reaction.click()
                page.locator('h1').click()
                expect(page.locator('#comment-4 .reaction-detail[open]')).to_have_count(0)
                page.locator('#comment-4 .thread-toggle').click()
                expect(page.locator('#comment-5')).to_be_hidden()
                expect(page.locator('#comment-6')).to_be_hidden()
                expect(page.locator('#comment-7')).to_be_visible()
                page.locator('#comment-4 .thread-toggle').click()
                expect(page.locator('#comment-5')).to_be_visible()
                expect(page.locator('#comment-4 .state.screened')).to_contain_text('Скрыт')
                page.locator('#expand-comments').click()
                expect(page.locator('#comment-5')).to_be_hidden()
                page.locator('#expand-comments').click()
                expect(page.locator('#comment-5')).to_be_visible()
                page.screenshot(path=str(root / 'entry.png'), full_page=True)
                page.goto((root / 'example_journal/index.html').as_uri())
                expect(page.get_by_role('link', name='Все архивы', exact=True)).to_have_count(0)
                expect(page.locator('.brand')).to_have_attribute('href', 'index.html')
                expect(page.locator('.brand')).to_have_text('example_journal')
                page.locator('[data-view=full]').click()
                expect(page.locator('.full-post .feed-body .prose')).to_have_count(6)
                expect(page.locator('.full-post .userpic')).to_have_count(6)
                page.locator('[data-view=compact]').click()
                page.locator('#search').fill('полуночи')
                expect(page.locator('.entry-card')).to_have_count(1)
                page.locator('.entry-card h2 a').click()
                expect(page.locator('#comment-4')).to_be_visible()
                page.goto((root / 'example_journal/help.html').as_uri())
                expect(page.locator('h1')).to_have_text('Помощь')
                page.goto(base)
                page.locator('#archives summary').click()
                with page.expect_download() as downloaded:
                    page.get_by_text('Скачать JSON', exact=True).click()
                assert downloaded.value.suggested_filename == 'example_journal.json'
                from ljarchive.activitystreams import source_snapshot
                standard = json.loads(Path(downloaded.value.path()).read_text(encoding='utf-8'))
                assert standard['type'] == 'Collection'
                assert len(source_snapshot(standard)['entries']) == 6
                with page.expect_download() as downloaded:
                    page.get_by_text('Исходный JSON', exact=True).click()
                raw = json.loads(Path(downloaded.value.path()).read_text(encoding='utf-8'))
                assert source_snapshot(standard) == raw
                response = page.request.get(base + '/download/example_journal.zip')
                with zipfile.ZipFile(io.BytesIO(response.body())) as zipped:
                    assert 'example_journal/help.html' in zipped.namelist()
                    assert source_snapshot(json.loads(zipped.read('example_journal/data/activitystreams.json'))) == raw
                    assert not any('sqlite' in name or '.jobs' in name for name in zipped.namelist())
                from ljarchive.storage import Store
                live = Store(root / 'live_fixture')
                old_state = server.jobs.state
                with live.db:
                    live.set('username', 'live_fixture')
                    live.save_entry({'itemid':1,'subject':'Во время загрузки','event':'Уже сохранено','eventtime':'2001-01-01 10:00:00','reply_count':2})
                server.jobs.state = {'phase':'comments','slug':'live_fixture','entry_id':1}
                page.goto(base + '/archive/live_fixture/timeline.html')
                expect(page.locator('.timeline-bar')).to_have_count(1)
                expect(page.locator('.timeline-bar')).to_have_class('timeline-bar pending active')
                page.locator('.timeline-bar').click()
                expect(page.locator('.post h1')).to_have_text('Во время загрузки')
                expect(page.locator('.prose').first).to_contain_text('Уже сохранено')
                server.jobs.state = old_state
                live.close()
                page.goto(base + '/archive/example_journal/timeline.html')
                expect(page.locator('.timeline-bar')).to_have_count(6)
                expect(page.locator('.timeline-year')).to_be_visible()
                page.locator('.timeline-bar').first.hover()
                expect(page.locator('.timeline-tip')).to_contain_text('Комментарии:')
                page.locator('.timeline-bar').first.click()
                expect(page.locator('.post h1')).to_be_visible()
                page.goto((root / 'example_journal/timeline.html').as_uri())
                expect(page.locator('.timeline-bar')).to_have_count(6)
                page.evaluate("""() => {
                    window.largeTimeline = new ArchiveTimeline(document.querySelector('#archive-timeline'));
                    const entries = Array.from({length: 8000}, (_,i) => ({id:i+1,date:`${2000+Math.floor(i/400)}-01-01`,
                      title:'Post '+i,url:'entries/3.html',state:i%7===0?'partial':'ready',comments:i%800,
                      expected_comments:i%800,materials:{saved:2,requested:2},top:[['reader',12]]}));
                    largeTimeline.set({entries,gaps:[],known:8000,expected:8000,ready:6857,partial:1143});
                }""")
                assert page.locator('.timeline-bar').count() < 150
                page.locator('.timeline-year').select_option('2010')
                page.locator('.timeline-bar').first.focus()
                page.keyboard.press('ArrowRight')
                assert page.locator('.timeline-bar:focus').count() == 1
                expect(page.locator('.timeline-tip')).to_contain_text('reader: 12')
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'ljarchive-timeline-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'ljarchive-timeline-mobile.png'), full_page=True)
                page.goto((root / 'example_journal/account.html').as_uri())
                expect(page.locator('h1')).to_be_visible()
                page.goto((root / 'example_journal/messages.html').as_uri())
                expect(page.locator('h1')).to_have_text('Переписка')
                for width in (390, 320):
                    page.set_viewport_size({'width': width, 'height': 844})
                    for url in ('/', '/help', '/archive/example_journal/index.html', '/archive/example_journal/entries/3.html', '/archive/example_journal/report.html', '/archive/example_journal/timeline.html', '/archive/example_journal/account.html', '/archive/example_journal/messages.html'):
                        page.goto(base + url)
                        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), (width, url)
                    page.goto(base + '/archive/example_journal/index.html?view=full')
                    expect(page.locator('.full-post .feed-body .prose')).to_have_count(6)
                    page.locator('.lj-cut summary').click()
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                    page.screenshot(path=str(root / f'full-journal-{width}.png'), full_page=True)
                    page.goto(base)
                    page.locator('.heading [data-new]').click()
                    expect(page.locator('[data-scope=latest]')).to_be_visible()
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                    page.screenshot(path=str(root / f'modal-{width}.png'), full_page=True)
                    page.keyboard.press('Escape')
                page.goto(base + '/archive/example_journal/entries/3.html')
                expect(page.get_by_role('link', name='ЖЖ ↗', exact=True)).to_have_attribute('target', '_blank')
                page.locator('#comment-4 [aria-label="Пиши ещё!: 1"]').click()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.locator('#theme-toggle').click()
                assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
                page.reload()
                assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
                expect(page.locator('#theme-toggle')).to_have_attribute('aria-label', 'Включить светлую тему')
                page.screenshot(path=str(root / 'entry-mobile-dark.png'), full_page=True)
                page.goto(base)
                page.locator('#move-storage').click()
                expect(page.locator('#storage-dialog')).to_be_visible()
                page.locator('#storage-path').fill(str(moved_root))
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.locator('#storage-submit').click()
                expect(page.locator('#storage-dialog')).not_to_be_visible(timeout=20000)
                expect(page.locator('#output-path')).to_have_text(str(moved_root))
                assert not root.exists()
                root = moved_root
                page.goto(base + '/archive/example_journal/statistics.html')
                expect(page.locator('h1')).to_have_text('Статистика')
                assert not errors, errors
                browser.close()
                screenshots = os.environ.get('LJARCHIVE_SCREENSHOTS')
                if screenshots:
                    import shutil
                    target = Path(screenshots)
                    target.mkdir(parents=True, exist_ok=True)
                    for file in root.glob('*.png'):
                        shutil.copy2(file, target / file.name)
                print('PASS: create/update, full/compact feed, LJ-cut, userpics, JSON/ZIP, file://, reactions, threads, 320/390px, dark theme')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            import shutil
            shutil.rmtree(moved_root, ignore_errors=True)


if __name__ == '__main__':
    main()
