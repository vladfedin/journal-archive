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
                page.locator('#source-type').select_option('community')
                expect(page.locator('#journal')).to_be_visible()
                expect(page.locator('#journal')).to_have_attribute('required', '')
                expect(page.locator('#account')).to_be_hidden()
                expect(page.locator('#messages')).not_to_be_checked()
                page.locator('#source-type').select_option('own')
                expect(page.locator('#journal')).to_be_hidden()
                expect(page.locator('#account')).to_be_visible()
                page.locator('#username').fill('example_journal')
                page.locator('#password').fill('browser-test-secret')
                page.locator('#submit-export').click()
                expect(page.locator('#job-stage')).to_have_text('Архив готов', timeout=15000)
                expect(page.locator('#job-action')).to_have_text('Скрыть')
                expect(page.locator('#password')).to_have_value('')
                assert calls[0][1]['mode'] == 'full' and calls[0][1]['max_requests'] is None
                page.locator('#job-action').click()
                page.wait_for_timeout(5200)
                expect(page.locator('#job')).to_be_hidden()
                expect(page.locator('#archives tr')).to_have_count(1)
                page.locator('#archives summary').click()
                page.wait_for_timeout(5200)  # Polling must leave an open action menu intact.
                expect(page.locator('#archives details')).to_have_attribute('open', '')
                page.locator('#archives [data-update]').focus()
                fresh = server.jobs.archives()
                fresh['archives'][0]['entries_saved'] = 7
                fresh['archives'][0]['parameters'].update(images=True, delay=7)
                page.route('**/api/archives', lambda route: route.fulfill(json=fresh))
                page.evaluate('library()')
                expect(page.locator('#archives tr td').nth(1)).to_have_text('7')
                expect(page.locator('#archives details')).to_have_attribute('open', '')
                expect(page.locator('#archives [data-update]')).to_be_focused()
                page.locator('#archives').get_by_role('button', name='Параметры', exact=True).click()
                expect(page.locator('#settings-content tr').filter(has_text='Изображения')).to_contain_text('Да')
                expect(page.locator('#settings-content tr').filter(has_text='Пауза')).to_contain_text('7 сек.')
                page.locator('#settings-close').click()
                page.locator('#archives summary').click()
                page.get_by_role('button', name='Обновить', exact=True).click()
                expect(page.locator('#password')).to_be_focused()
                expect(page.locator('#images')).to_be_checked()  # Uses the new row, not a stale closure.
                expect(page.locator('[data-scope=period]')).to_have_attribute('aria-pressed', 'true')
                page.locator('[data-scope=urls]').click()
                expect(page.locator('#entry-urls')).to_be_visible()
                expect(page.locator('[data-scope=latest]')).to_be_hidden()
                page.keyboard.press('Escape')
                fresh['archives'][0]['resumable'] = True
                fresh['archives'][0]['status'] = 'partial'
                fresh['archives'][0]['parameters_known'].remove('videos')
                recovery = {'available': True, 'discovery_complete': True, 'attempts': 3,
                            'rows': [{'key': 'images', 'label': 'Изображения и юзерпики', 'remaining': 12,
                                      'unavailable': 0, 'before': 12, 'resolved': 0, 'new': 0,
                                      'reasons': [{'message': 'ещё не запрашивались', 'final': False, 'count': 12}]},
                                     {'key': 'discussions', 'label': 'Обсуждения', 'remaining': 0, 'unavailable': 2,
                                      'before': 2, 'resolved': 0, 'new': 0,
                                      'reasons': [{'message': 'ЖЖ отдал не все комментарии', 'final': True, 'count': 2}]}],
                            'attempts': [{'finished_at': '2026-11-17T12:00:00Z', 'status': 'partial',
                                          'operation': 'resume', 'recovery': {'rows': []}}]}
                page.route('**/api/recovery/*', lambda route: route.fulfill(json=recovery))
                page.evaluate('library()')
                page.locator('#archives summary').click()
                page.locator('#archives [data-resume]').click()
                expect(page.locator('#dialog-title')).to_have_text('Докачать пропуски')
                expect(page.locator('#resume-plan .is-open [data-recovery-kind=images] .gap-count')).to_have_text('12')
                expect(page.locator('#resume-plan .is-closed [data-recovery-kind=discussions]')).to_contain_text('ЖЖ отдал не все комментарии')
                expect(page.locator('#submit-export')).to_be_enabled()
                expect(page.locator('#password')).to_be_focused()
                expect(page.locator('.recovery-history summary')).to_have_text('Предыдущие запуски')
                page.locator('#resume-settings summary').click()
                expect(page.locator('#resume-parameters tr').filter(has_text='Видео')).to_contain_text('Не сохранено')
                expect(page.locator('#resume-parameters tr').filter(has_text='Изображения')).to_contain_text('Да')
                page.keyboard.press('Escape')
                page.unroute('**/api/recovery/*')
                page.unroute('**/api/archives')
                page.evaluate('library()')
                # Verification remains visible even with an existing timeline.
                state = {**server.jobs.snapshot(), 'job_id': 'preparing-fixture', 'phase': 'verify',
                         'message': 'Проверяем файлы · 12 / 30', 'completed': 12, 'total': 30}
                page.route('**/api/status', lambda route: route.fulfill(json=state))
                page.evaluate('s => showStatus(s)', state)
                expect(page.locator('#job-message')).to_have_text('Проверяем файлы · 12 / 30')
                expect(page.locator('#progress')).to_be_visible()
                expect(page.locator('#progress')).to_have_attribute('value', '12')
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#job-settings').click()
                expect(page.locator('#settings-dialog')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#settings-close').click()
                # A resume shows closed and remaining gaps without collapsing open reasons on polling.
                state.update(operation='resume', phase='waiting', retry_at=4102444800, recovery=recovery,
                             started_at=100, working_seconds=125, waiting_seconds=61, last_success_at=4102444600, server_time=4102444700, worker_alive=True,
                             wait={'reason':'timeout','host':'images.example','attempt':2,'max_attempts':6},
                             message='Загрузка изображения · попытка 2 из 6')
                page.evaluate('s => showStatus(s)', state)
                expect(page.locator('#job-stage')).to_contain_text('Пауза до')
                expect(page.locator('#job-state')).to_have_attribute('data-tone', 'wait')
                expect(page.locator('#job-message')).to_contain_text('Нет связи с сервером · images.example · попытка 2 из 6')
                expect(page.locator('#job-message')).to_contain_text('повтор через 2 мин')
                expect(page.locator('#job-meta')).to_contain_text('идёт 3 мин, из них пауз 1 мин')
                expect(page.locator('#job-kind')).to_have_text('докачка пропусков')
                assert page.locator('#job [aria-live]:not([aria-live=off]), #job [role=status]').evaluate_all('n => n.map(x => x.id)') == ['job-stage']
                # A run with recent 302 responses is not a silent server, even if no file was saved.
                state.update(phase='materials', last_success_at=4102440000, last_response_at=4102444699,
                             request={'at':4102444698,'host':'ic.pics.livejournal.com'},
                             last_activity={'kind':'redirect','http_status':302,'at':4102444699},
                             message='Загрузка изображения · попытка 2 из 3')
                page.evaluate('s => showStatus(s)', state)
                expect(page.locator('#job-message')).to_contain_text('ЖЖ перенаправляет запрос · 302')
                expect(page.locator('#job-message')).not_to_contain_text('не отвечает')
                state.update(last_response_at=4102444500, request={'at':4102444600,'host':'images.example'},
                             last_activity={'kind':'request','at':4102444600})
                page.evaluate('s => showStatus(s)', state)
                expect(page.locator('#job-message')).to_contain_text('Ожидаем ответ от images.example')
                page.locator('#job-diagnostics').click()
                expect(page.locator('#diagnostics-download')).to_be_visible()
                page.locator('#diagnostics-dialog summary').click()
                expect(page.locator('#diagnostics-content')).to_contain_text('schema_version')
                with page.expect_download() as downloaded_report:
                    page.locator('#diagnostics-download').click()
                diagnostic = Path(downloaded_report.value.path()).read_text(encoding='utf-8')
                assert 'browser-test-secret' not in diagnostic and 'https://' not in diagnostic
                assert json.loads(diagnostic)['schema_version'] == 1
                page.locator('#diagnostics-close').click()
                expect(page.locator('#job-recovery .is-open h3')).to_have_text('Докачиваем')
                expect(page.locator('#job-recovery [data-recovery-kind=images] .gap-count')).to_have_text('12')
                expect(page.locator('#job-recovery .is-closed h3')).to_have_text('Недоступно в ЖЖ')
                expect(page.locator('#job-action')).to_have_text('Остановить')
                recovery['rows'][0].update(resolved=10, remaining=2)
                recovery['checked_at'] = '2026-11-17T12:01:00Z'
                state.update(phase='partial', message='Два файла недоступны.')
                page.evaluate('s => showStatus(s)', state)
                expect(page.locator('#job-stage')).to_have_text('Загрузка не завершена')
                expect(page.locator('#job-recovery .gaps-done')).to_have_text('Докачано: 10 изображений.')
                expect(page.locator('#job-recovery .is-open h3')).to_have_text('Осталось докачать')
                expect(page.locator('#job-recovery [data-recovery-kind=images] .gap-count')).to_have_text('2')
                expect(page.locator('#job-action')).to_have_text('Докачать пропуски')
                expect(page.locator('#job-action')).to_have_class('btn primary')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'ljarchive-recovery-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1100})
                page.unroute('**/api/status')
                page.screenshot(path=str(root / 'library.png'), full_page=True)
                page.goto(base + '/archive/example_journal/index.html')
                expect(page.get_by_role('link', name='Все архивы', exact=True)).to_be_visible()
                page.get_by_role('link', name='Все архивы', exact=True).click()
                expect(page).to_have_url(base + '/')
                expect(page.locator('#archives tr')).to_have_count(1)
                page.goto(base + '/archive/example_journal/index.html')
                expect(page.locator('.entry-card')).to_have_count(6)
                expect(page.locator('.full-post .feed-body .prose')).to_have_count(6)
                page.locator('a.year-mark').first.click()
                expect(page.locator('.matrix.months tbody tr')).to_have_count(12)
                page.locator('#calendar-year').select_option('2009')
                expect(page.locator('#calendar-count')).to_contain_text('1 запись')
                page.locator('[data-date="2009-02-03"]').click()
                expect(page.locator('#post-list')).to_contain_text('Книги, к которым возвращаешься')
                page.locator('#period-feed').click()
                expect(page.locator('.entry-card')).to_have_count(1)
                expect(page.locator('.entry-card')).to_contain_text('Книги, к которым возвращаешься')
                expect(page.locator('#date-filter')).to_contain_text('3 февраля 2009')
                page.reload()
                expect(page.locator('.entry-card')).to_have_count(1)
                page.locator('#date-filter').click()
                expect(page.locator('.entry-card')).to_have_count(6)
                page.goto((root / 'example_journal/calendar.html').as_uri() + '?year=2012')
                expect(page.locator('#calendar-year')).to_have_value('2012')
                # Leap years show February 29; publication days retain their weekday.
                page.locator('[data-view=calendar]').click()
                expect(page.locator('.mini-calendar').nth(1).locator('[data-date]')).to_have_count(29)
                day = page.locator('[data-date="2012-10-12"]')
                assert day.evaluate('e => [...e.parentElement.children].indexOf(e) % 7') == 4  # Friday; weeks start Monday.
                for width in (320, 390, 1440):
                    page.set_viewport_size({'width': width, 'height': 1100})
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.screenshot(path=str(root / 'calendar.png'), full_page=True)
                page.locator('.mini-calendar [data-month="9"]').click()
                page.locator('#period-feed').click()
                expect(page.locator('.full-post .feed-body .prose')).to_have_count(1)
                expect(page.locator('.entry-card')).to_contain_text('Тот самый осенний день')
                page.locator('#reset-filters').click()
                expect(page.locator('.entry-card')).to_have_count(6)
                page.goto(base + '/archive/example_journal/index.html')
                page.locator('.archive-sections').get_by_role('link', name='Статистика', exact=True).click()
                expect(page.locator('h1')).to_have_text('Статистика')
                expect(page.locator('#stats-owner')).to_be_checked()
                expect(page.locator('#stats-people .stats-participants')).to_contain_text('example_journal')
                page.locator('#stats-owner').uncheck()
                expect(page.locator('#stats-people .stats-participants')).not_to_contain_text('example_journal')
                page.locator('#stats-owner').check()
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
                expect(page.locator('#stats-owner')).to_be_checked()
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
                page.goto(base + '/archive/live_fixture/index.html')
                expect(page.locator('.entry-card')).to_have_count(1)
                page.locator('[data-view=full]').click()
                expect(page.locator('.feed-body')).to_contain_text('Уже сохранено')
                expect(page.locator('#archive-timeline')).to_have_count(0)
                page.locator('.archive-sections').get_by_role('link', name='Календарь', exact=True).click()
                page.locator('[data-date="2001-01-01"]').click()
                expect(page.locator('#period-feed')).to_have_attribute('href','index.html?view=full&date=2001-01-01')
                expect(page.locator('#post-list')).to_contain_text('Во время загрузки')
                page.goto(base + '/archive/live_fixture/timeline.html')
                expect(page.locator('.timeline-bar')).to_have_count(1)
                expect(page.locator('.timeline-bar')).to_have_class('timeline-bar pending active')
                page.locator('.timeline-bar').click()
                expect(page.locator('.post h1')).to_have_text('Во время загрузки')
                expect(page.locator('.prose').first).to_contain_text('Уже сохранено')
                server.jobs.statistics_cache.interval = 0
                page.goto(base + '/archive/live_fixture/statistics.html?year=2001')
                expect(page.locator('#stats-live')).to_contain_text('Снимок', timeout=10000)
                expect(page.locator('#stats-year')).to_have_value('2001')
                page.locator('#stats-owner').uncheck()
                page.locator('#stats-metric').select_option('share')
                page.locator('#stats-search').fill('alice')
                with live.db:
                    live.db.execute("INSERT INTO users(id,name) VALUES (7,'alice')")
                    live.save_entry({'itemid':2,'subject':'Новый год','eventtime':'2022-01-01 10:00:00'})
                    live.save_comment({'id':10,'jitemid':1,'date':'2001-01-02','posterid':7,'body':'Первый ответ'})
                    live.save_comment({'id':11,'jitemid':2,'date':'2022-01-02','posterid':7,'body':'Второй ответ'})
                expect(page.locator('#stats-year option[value="2022"]')).to_have_count(1, timeout=10000)
                expect(page.locator('#stats-search')).to_be_focused()
                expect(page.locator('#stats-search')).to_have_value('alice')
                expect(page.locator('#stats-year')).to_have_value('2001')
                expect(page.locator('#stats-metric')).to_have_value('share')
                expect(page.locator('#stats-owner')).not_to_be_checked()
                expect(page.locator('#stats-people .stats-participants')).to_contain_text('alice')
                expect(page.locator('#stats-summary')).to_contain_text('Комментариев: 2')
                expect(page.locator('#stats-graph')).not_to_be_visible()
                expect(page.locator('.footer')).not_to_contain_text('Дата неизвестна')
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'ljarchive-live-statistics.png'), full_page=True)
                page.locator('#stats-people a.heat').first.click()
                expect(page.locator('#discussion-count')).to_contain_text('комментариев: 1')
                with live.db:
                    live.save_comment({'id':12,'jitemid':1,'date':'2001-01-03','posterid':7,'body':'Третий ответ'})
                expect(page.locator('#discussion-count')).to_contain_text('комментариев: 2', timeout=10000)
                server.jobs.statistics_cache.interval = 30
                server.jobs.state = old_state
                live.close()
                page.goto(base + '/archive/example_journal/timeline.html')
                expect(page.locator('.timeline-bar')).to_have_count(6)
                expect(page.locator('.timeline-year')).to_be_visible()
                page.locator('.timeline-bar').first.hover()
                expect(page.locator('.timeline-tip')).to_be_visible()
                expect(page.locator('.timeline-tip')).not_to_contain_text('Комментарии:')
                below = page.locator('.timeline-viewport').bounding_box()
                page.mouse.move(5, 5)
                expect(page.locator('.timeline-tip')).to_be_hidden()
                assert page.locator('.timeline-viewport').bounding_box() == below, 'the card must not move the page'
                page.locator('.timeline-bar').first.click()
                expect(page.locator('.post h1')).to_be_visible()
                page.goto((root / 'example_journal/timeline.html').as_uri())
                expect(page.locator('.timeline-bar')).to_have_count(6)
                page.evaluate("""() => {
                    window.largeTimeline = new ArchiveTimeline(document.querySelector('#archive-timeline'));
                    const entries = Array.from({length: 10000}, (_,i) => ({id:i+1,date:`${2000+Math.floor(i/500)}-${String(1+Math.floor(i%500/42)).padStart(2,'0')}-01`,
                      title:'Post '+i,url:'entries/3.html',state:i%7===0?'partial':'ready',comments:i%800,
                      expected_comments:i%800,materials:{saved:2,requested:2},top:[['reader',12]]}));
                    window.denseTimelineData={entries,gaps:[],known:10000,expected:10000,ready:8571,partial:1429};
                    largeTimeline.set(denseTimelineData);
                }""")
                assert page.locator('.timeline-bar').count() < 150
                assert page.evaluate('largeTimeline.overviewBins.reduce((sum,b)=>sum+b.count,0)') == 10000
                assert page.evaluate('largeTimeline.overviewBins.length <= innerWidth')
                assert page.evaluate('largeTimeline.bands.every(b=>b.count===500)')
                assert page.evaluate('largeTimeline.overviewBins.some(b=>b.count>1&&b.state==="partial")')
                rect = page.locator('.timeline-overview').bounding_box()
                page.mouse.click(rect['x'] + rect['width'] * .775, rect['y'] + 10)
                assert page.evaluate('Math.floor(largeTimeline.viewport.scrollLeft/largeTimeline.slot)') == 7500
                page.mouse.move(rect['x'] + rect['width'] * .752, rect['y'] + 48)
                page.mouse.down()
                page.mouse.move(rect['x'] + rect['width'] * .9, rect['y'] + 48, steps=5)
                page.mouse.up()
                assert page.evaluate('Math.floor(largeTimeline.viewport.scrollLeft/largeTimeline.slot)') > 8500
                page.locator('.timeline-year').select_option('2010')
                assert page.evaluate('Math.floor(largeTimeline.viewport.scrollLeft/largeTimeline.slot)') == 5000
                page.locator('.timeline-bar').first.focus()
                page.keyboard.press('ArrowRight')
                assert page.locator('.timeline-bar:focus').count() == 1
                expect(page.locator('.timeline-tip')).to_contain_text('Post 4999')
                expect(page.locator('.timeline-tip')).to_contain_text('199 комментариев · 2 изображения')
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'ljarchive-timeline-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(page.locator('.timeline-bar').first).to_have_css('width', '44px')
                assert page.locator('.timeline-bar').count() < 20
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'ljarchive-timeline-mobile.png'), full_page=True)
                # Equal-year mapping, empty years and refresh anchoring also work with 300 posts/year.
                page.evaluate("""() => {
                    const entries=denseTimelineData.entries.slice(0,6000).map((e,i)=>({...e,date:`${2000+Math.floor(i/300)}-01-01`}));
                    largeTimeline.set({...denseTimelineData,entries,known:6000,expected:6000});
                    largeTimeline.jump(3000,false);
                    const anchor=largeTimeline.items[Math.floor(largeTimeline.viewport.scrollLeft/largeTimeline.slot)].id;
                    entries.unshift({...entries[0],id:20001,date:'1998-01-01'});
                    largeTimeline.set({...denseTimelineData,entries,known:6001,expected:6001});
                    window.preservedAnchor=anchor===largeTimeline.items[Math.floor(largeTimeline.viewport.scrollLeft/largeTimeline.slot)].id;
                }""")
                assert page.evaluate('preservedAnchor')
                assert page.evaluate('largeTimeline.bands.find(b=>b.label==="1999").count') == 0
                assert page.evaluate('largeTimeline.bands.find(b=>b.label==="2010").count') == 300
                page.locator('.timeline-year').select_option('2019')
                page.locator('.timeline-bar').last.focus()
                page.keyboard.press('End')
                expect(page.locator('.timeline-bar:focus')).to_have_attribute('data-timeline-index', '6000')
                page.evaluate("""() => {largeTimeline.set({entries:[],gaps:[{date:'2010-03-01',count:500}],known:0,expected:500,ready:0,partial:0});largeTimeline.jump(0,false);} """)
                assert page.locator('.timeline-bar').count() > 0
                assert page.locator('a.timeline-bar').count() == 0
                assert page.evaluate('largeTimeline.overviewBins.reduce((n,b)=>n+b.count,0)') == 500
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
    from browser_materials import main as check_materials
    check_materials()
    from browser_library import main as check_library
    check_library()
