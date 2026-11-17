"""Capture the real UI with fictional data; never log in to LiveJournal.

Run from the repo: PYTHONPATH=. .venv/bin/python tools/capture_guide.py
Requires the browser extra and Chromium. Only the release screenshot uses the network.
"""
from pathlib import Path
import json
import tempfile
import threading

from playwright.sync_api import expect, sync_playwright

from ljarchive import __version__
from ljarchive.demo import create_demo
from ljarchive.render import build_archive
from ljarchive.server import make_server
from ljarchive.storage import Store

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / 'docs' / 'guide' / 'images'


def main():
    IMAGES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ljarchive-guide-') as directory, sync_playwright() as pw:
        root = Path(directory)
        server = make_server(root, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': 1200, 'height': 860}, color_scheme='light', locale='ru-RU')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def shot(name, selector=None):
            page.evaluate('document.fonts.ready')
            target = page.locator(selector) if selector else page
            target.screenshot(path=str(IMAGES / (name + '.png')), animations='disabled')

        # GitHub is captured as served, with an outline around the actual download link.
        page.goto(f'https://github.com/vladfedin/journal-archive/releases/tag/v{__version__}', wait_until='domcontentloaded')
        download = page.get_by_role('link', name=f'JournalArchive-{__version__}-windows-x64.exe', exact=True)
        download.wait_for()
        download.evaluate("e => {e.style.outline='3px solid #2563eb';e.style.outlineOffset='5px';}")
        download.locator('xpath=ancestor::details[1]').screenshot(path=str(IMAGES / '01-download.png'))

        # All subsequent browser requests stay on the disposable local server.
        page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
        # Display a fictional Windows path instead of the developer's temporary directory.
        def archives(route):
            data = server.jobs.archives()
            data['output'] = r'C:\Users\Anna\AppData\Local\LiveJournalArchive\archives'
            route.fulfill(json=data)
        page.route('**/api/archives', archives)
        page.goto(base)
        expect(page.locator('#empty')).to_be_visible()
        shot('03-start')
        page.locator('.heading [data-new]').click()
        page.locator('#username').fill('example_journal')
        page.locator('#password').fill('fictional-password')
        page.locator('#images').check()
        page.locator('#videos').check()
        shot('04-login', '#export-dialog')
        page.keyboard.press('Escape')
        expect(page.locator('#password')).to_have_value('')

        folder = root / 'example_journal'
        report = create_demo(folder)
        store = Store(folder)
        with store.db:
            store.set('entry_comments', {str(i): {'done': i < 4} for i in range(1, 7)})
        store.close()
        server.jobs.state = {'phase': 'comments', 'username': 'example_journal', 'slug': 'example_journal',
                             'entries': 6, 'comments': 12, 'entry_id': 4, 'requests_used': 31,
                             'archive_url': '/archive/example_journal/index.html'}
        page.reload()
        expect(page.locator('#job-timeline .timeline-bar')).to_have_count(6)
        expect(page.locator('#job-stage')).to_have_text('Комментарии')
        shot('05-progress', '#job')

        store = Store(folder)
        with store.db:
            store.set('posts_done', True)
            store.set('bodies_done', True)
            store.set('entry_comments', {str(i): {'done': True} for i in range(1, 7)})
        report.update(status='complete', requests_used=42, images_requested=True)
        build_archive(store, report)
        store.close()
        server.jobs.state.update(phase='complete', requests_used=42)
        page.reload()
        expect(page.locator('#job-stage')).to_have_text('Готов')
        shot('06-ready', '#job')

        page.goto(base + '/archive/example_journal/index.html')
        expect(page.locator('.entry-card')).to_have_count(6)
        shot('06-archive')
        page.goto(base + '/archive/example_journal/entries/3.html')
        expect(page.locator('#comment-4')).to_be_visible()
        shot('06-post')

        page.goto(base + '/archive/example_journal/report.html')
        page.locator('.form-advanced summary').click()
        shot('07-report')

        page.goto(base + '/archive/example_journal/statistics.html')
        expect(page.locator('.stats-totals tr')).to_have_count(2)
        shot('08-statistics')
        page.locator('.stats-participants a.heat').first.click()
        expect(page.locator('.discussion-card')).not_to_have_count(0)
        shot('08-discussions')

        server.jobs.state = {'phase': 'idle'}
        page.goto(base)
        expect(page.locator('#archives tr')).to_have_count(1)
        page.locator('#archives summary').click()
        shot('09-files')
        page.get_by_role('button', name='Обновить', exact=True).click()
        page.locator('[data-scope=urls]').click()
        page.locator('#entry-urls').fill('https://example-journal.livejournal.com/769.html')
        page.locator('#images').check()
        page.locator('#videos').check()
        shot('09-update', '#export-dialog')
        assert not errors, errors
        browser.close()
        server.shutdown()
        server.server_close()
    print(json.dumps({'screenshots': len(list(IMAGES.glob('*.png'))), 'application_version': __version__,
                      'data': 'fictional', 'livejournal_requests': 0}))


if __name__ == '__main__':
    main()
