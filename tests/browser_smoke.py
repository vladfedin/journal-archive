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
                expect(page.locator('.entry-card')).to_have_count(6)
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
                assert len(json.loads(Path(downloaded.value.path()).read_text(encoding='utf-8'))['entries']) == 6
                response = page.request.get(base + '/download/example_journal.zip')
                with zipfile.ZipFile(io.BytesIO(response.body())) as zipped:
                    assert 'example_journal/help.html' in zipped.namelist()
                    assert not any('sqlite' in name or '.jobs' in name for name in zipped.namelist())
                for width in (390, 320):
                    page.set_viewport_size({'width': width, 'height': 844})
                    for url in ('/', '/help', '/archive/example_journal/index.html', '/archive/example_journal/entries/3.html', '/archive/example_journal/report.html'):
                        page.goto(base + url)
                        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), (width, url)
                    page.goto(base)
                    page.locator('.heading [data-new]').click()
                    expect(page.locator('[data-scope=latest]')).to_be_visible()
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                    page.screenshot(path=str(root / f'modal-{width}.png'), full_page=True)
                    page.keyboard.press('Escape')
                page.goto(base + '/archive/example_journal/entries/3.html')
                page.locator('#comment-4 [aria-label="Пиши ещё!: 1"]').click()
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                page.locator('#theme-toggle').click()
                assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
                page.reload()
                assert page.evaluate('document.documentElement.dataset.theme') == 'dark'
                expect(page.locator('#theme-toggle')).to_have_attribute('aria-label', 'Включить светлую тему')
                page.screenshot(path=str(root / 'entry-mobile-dark.png'), full_page=True)
                assert not errors, errors
                browser.close()
                screenshots = os.environ.get('LJARCHIVE_SCREENSHOTS')
                if screenshots:
                    import shutil
                    target = Path(screenshots)
                    target.mkdir(parents=True, exist_ok=True)
                    for file in root.glob('*.png'):
                        shutil.copy2(file, target / file.name)
                print('PASS: create/update, defaults, JSON/ZIP, offline help/search, reactions, threads, 320/390px, dark theme, no JS errors')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == '__main__':
    main()
