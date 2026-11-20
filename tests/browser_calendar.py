"""Calendar integration on local synthetic archives; no LiveJournal requests."""
import json
from pathlib import Path
import shutil
import tempfile

from playwright.sync_api import sync_playwright, expect

from ljarchive.calendar import write_calendar
from ljarchive.storage import Store


def main():
    with tempfile.TemporaryDirectory(prefix='journal-calendar-') as directory:
        folder = Path(directory)
        store = Store(folder)
        from datetime import date, timedelta
        with store.db:
            store.set('username', 'fixture')
            store.set('days_loaded', True)
            store.set('bodies_done', True)
            for year in range(2005, 2025):
                for i in range(500):
                    ordinal = i * 280 // 500
                    if ordinal > 140:
                        ordinal += 75
                    stamp = date(year, 1, 1) + timedelta(days=ordinal)
                    store.save_entry({'itemid': (year-2005)*500+i+1,
                                      'eventtime': f'{stamp} '+('99:99:00' if i == 4 else '23:45:00'),
                                      'subject': 'Запись <b>' + str(i+1) + '</b>'})
            store.save_entry({'itemid': 10001, 'eventtime': '', 'subject': 'Без даты'})
            store.set('entry_comments', {'1': {'done': False, 'error': 'incomplete'}})
            for i in range(1250):
                store.save_comment({'id': i+1, 'jitemid': 1, 'body': 'saved', 'state': 'D' if i == 3 else 'A'})
            store.db.execute('INSERT INTO days(day,expected,done) VALUES (?,?,?)', ('2026-02-01', 3, 0))
        report = {'username': 'fixture', 'created_at': '2026-01-01'}
        write_calendar(store, report)
        store.close()
        assets = Path(__file__).resolve().parents[1] / 'ljarchive/static'
        for name in ('archive.css', 'typography.css', 'calendar.css', 'calendar.js', 'archive.js', 'theme.js', 'common.js', 'material-actions.js', 'favicon.ico', 'favicon.svg'):
            shutil.copyfile(assets / name, folder / 'assets' / name)
        (folder / 'entries').mkdir()
        (folder / 'entries/1.html').write_text('<h1>Запись 1</h1>')
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1080}, offline=True)
            page = context.new_page()
            errors, violations, requests = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: violations.append(message.text) if message.type == 'error' else None)
            page.on('request', lambda request: requests.append(request.url))
            url = (folder / 'calendar.html').as_uri()
            page.goto(url+'?year=2024')
            expect(page.locator('#calendar-year')).to_have_value('2024')
            expect(page.locator('#calendar-count')).to_contain_text('500 записей')
            assert page.locator('#post-list .post-item').count() == 25
            assert page.locator('body *').count() < 8000
            for view in ('months', 'weeks', 'calendar'):
                page.locator(f'[data-view={view}]').click()
                assert page.locator('[data-date]').count() == 366
                assert page.locator('[data-date]').evaluate_all('(els)=>els.reduce((n,e)=>n+Number(e.dataset.count),0)') == 500
                expect(page.locator('[data-date="2024-02-29"]')).to_have_count(1)
            page.locator('[data-date="2024-02-29"]').click()
            selected = page.locator('#selection-counts').inner_text()
            for scale in ('posts', 'time'):
                page.locator(f'button[data-scale={scale}]').click()
                expect(page.locator('#selection-counts')).to_have_text(selected)
            page.locator('#calendar-year').select_option('2025')
            assert page.locator('[data-date]').count() == 365
            expect(page.locator('#selection-counts')).to_contain_text('0 записей')
            page.locator('#calendar-year').select_option('2026')
            page.locator('[data-date="2026-02-01"]').click()
            expect(page.locator('.empty-state')).to_have_text('Записи пока не получены')
            expect(page.locator('#selection-note')).to_have_text('Не получено записей: 3')
            expect(page.locator('[data-date="2026-02-01"]')).to_have_attribute('data-missing', '3')
            page.locator('[data-date="2026-02-02"]').click()
            expect(page.locator('.empty-state')).to_have_text('Нет сохранённых записей')
            page.locator('#calendar-undated').click()
            expect(page.locator('#post-list')).to_contain_text('Без даты')
            expect(page.locator('#post-list a')).to_have_attribute('href', 'entries/10001.html')
            page.locator('#calendar-year').select_option('2005')
            page.locator('[data-date="2005-01-01"]').click()
            expect(page.locator('#post-list')).to_contain_text('1\xa0250 комментариев')
            expect(page.locator('#selection-counts')).to_contain_text('сохранено')
            page.locator('#metric').select_option('comments')
            page.locator('#post-list a').first.click()
            expect(page.locator('h1')).to_have_text('Запись 1')
            page.goto(url+'?year=2024')
            expect(page.locator('#metric')).to_have_value('comments')
            page.locator('[data-view=months]').click()
            page.locator('[data-date="2024-01-31"]').focus()
            page.keyboard.press('ArrowDown')
            expect(page.locator('[data-date="2024-02-29"]')).to_be_focused()
            for width in (320, 390, 768, 1440):
                page.set_viewport_size({'width': width, 'height': 1000})
                for view in ('months', 'weeks', 'calendar'):
                    page.locator(f'[data-view={view}]').click()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), (width, view)
            page.locator('#theme-toggle').click()
            expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
            page.set_viewport_size({'width': 1440, 'height': 1080})
            page.locator('[data-view=months]').click()
            page.screenshot(path=str(Path(tempfile.gettempdir())/'journal-calendar-desktop.png'), full_page=True)
            # A malformed preference must not stop the page, and standalone archives need no localStorage.
            context.add_init_script("Object.defineProperty(window,'localStorage',{get(){throw Error('disabled')}})")
            page.reload()
            expect(page.locator('[data-date]')).to_have_count(366)
            # Empty/undated-only archives have no fictitious dates or years.
            (folder/'assets/calendar-data.js').write_text('window.CALENDAR='+json.dumps({'entries': [{'id': 1, 'date': '', 'title': 'Undated'}], 'days': {}, 'missing': {}})+';')
            page.reload()
            expect(page.locator('#calendar-year')).to_be_disabled()
            expect(page.locator('#post-list')).to_contain_text('Undated')
            page.locator('[data-view=weeks]').click()
            page.locator('button[data-scale=posts]').click()
            assert not errors, errors
            assert not violations, violations
            assert all(request.startswith('file:') for request in requests), requests
            browser.close()
    print('PASS: 10,000 posts, date/week/month selections, missing/empty/undated, reply counts, real links, file://, CSP, no storage, 320–1440px')


if __name__ == '__main__':
    main()
