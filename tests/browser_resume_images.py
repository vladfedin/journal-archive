"""Main resume button -> browser login -> saved files; local HTTP/Chromium only."""
import json
from pathlib import Path
import secrets
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from ljarchive.client import LiveJournalClient, RequestBudget
from ljarchive.media import Media
from ljarchive.server import make_server
from ljarchive.storage import Store, atomic_write, dumps
from ljarchive.render import build_archive
from test_browser_bridge import protected_browser, sign_in, PNG


def main():
    fixture = protected_browser.__wrapped__()
    bridge, source, calls, token = next(fixture)
    server = thread = None
    password = secrets.token_hex(20)
    try:
        with tempfile.TemporaryDirectory(prefix='journal-resume-ui-') as directory:
            root = Path(directory)
            urls = [source + '/denied.png', source + '/private.png']
            store = Store(root / 'fixture')
            report = {'username': 'fixture', 'status': 'partial', 'created_at': '2026-01-01T00:00:00Z', 'entries_saved': 1, 'comments_saved': 0, 'missing_comments': [], 'issues': ['Не все изображения получены.']}
            entry = {'itemid': 1, 'anum': 1, 'subject': 'Закрытые изображения', 'eventtime': '2024-01-01 00:00:00', 'security': 'private', 'event': ''.join(f'<img src="{u}">' for u in urls)}
            with store.db:
                entry['reactions'] = {'total': 0, 'groups': [], 'authors': {'status': 'complete', 'users': []}}
                store.set('username', 'fixture'); store.save_entry(entry)
                store.set('entry_materials', {'1': {'urls': urls}})
                store.set('media', {u: None for u in urls})
                store.set('media_errors', {u: {'status': 'pending', 'reason': 'web_login', 'session_type': 'web'} for u in urls})
            build_archive(store, report); store.close()
            params = {'username': 'fixture', 'archive': 'fixture', 'images': True, 'delay': 1, 'max_requests': 10, 'mode': 'full', 'entry_limit': None}
            atomic_write(root / '.jobs/fixture.json', dumps({'parameters': params, 'state': {'phase': 'partial', 'slug': 'fixture', 'username': 'fixture', 'job_id': 'before', 'archive_url': '/archive/fixture/index.html'}}))
            server = make_server(root, 0); server.jobs.sessions._browsers['fixture'] = bridge
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            resumed = []
            # Only LJ inventory/API is replaced. The worker, resume form, browser
            # session, HTTP image requests, persistence and archive rendering are real.
            def sync(user, secret, output, **kw):
                assert secret == password and kw['resume'] and kw['mode'] == 'full'
                assert kw['images'] and kw['max_requests'] == 10 and kw['delay'] == 1
                resumed.append(kw['resume'])
                budget = RequestBudget(kw['max_requests'], progress=kw['progress'])
                client = LiveJournalClient(user, secret, delay=kw['delay'], cancel=kw['cancel'], budget=budget)
                kw['session_ready'](client)
                store = Store(output / kw['archive'])
                media = Media(store.folder, True, kw['cancel'], budget, client)
                try:
                    media.load_state(store)
                    for url in urls: media.get(url, referer=source + '/')
                    def checkpoint():
                        with store.db: media.save_state(store)
                    checkpoint(); media.retry_failed(checkpoint=checkpoint)
                    report.update(status='complete', issues=[], requests_used=budget.used)
                    build_archive(store, report)
                    return report
                finally:
                    media.close(); client.close(); store.close()
            with patch('ljarchive.server.sync', sync), patch('ljarchive.media.authenticated_image_host', lambda u: u.startswith(source + '/')), sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1000, 'height': 850})
                errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(base)
                page.locator('#job-action').click()
                page.locator('#password').fill(password)
                page.locator('#submit-export').click()
                expect(page.locator('#contact-body')).to_be_focused()
                page.keyboard.press('Escape')
                expect(page.locator('#job-stage')).to_have_text('Вход в браузере', timeout=10000)
                expect(page.locator('#job-action')).to_have_text('Продолжить')
                expect(page.locator('#job-browser-cancel')).to_be_visible()
                assert not resumed and not any(u.endswith('.png') for u in calls)
                state = page.request.get(base + '/api/status').json()
                job_id = state['job_id']
                assert page.request.post(base + '/api/browser-confirm', data={'job_id': 'stale'}, headers={'Origin': base}).status == 400
                assert page.request.post(base + '/api/browser-confirm', data={'job_id': job_id}, headers={'Origin': 'https://foreign.invalid'}).status == 403
                assert not resumed
                for width in (320, 1000):
                    page.set_viewport_size({'width': width, 'height': 850})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                sign_in(bridge, source, calls); calls.clear()
                page.locator('#job-action').click()
                expect(page.locator('#job-action')).to_have_text('Скрыть', timeout=20000)
                state = page.request.get(base + '/api/status').json()
                assert state['phase'] == 'complete' and state['job_id'] == job_id
                expect(page.locator('#job-meta')).to_contain_text('сохранено 1 · недоступно 1')
                assert resumed == [True] and state['requests_used'] == 2
                assert calls.count('/denied.png') == 1 and calls.count('/private.png') == 1
                data = json.loads((root / 'fixture/data/archive.json').read_text())
                assert (root / 'fixture' / data['media'][urls[1]]).read_bytes() == PNG
                assert data['media_errors'][urls[0]]['closed']
                assert data['entries'][0]['event'] == entry['event'] and data['entries'][0]['security'] == 'private'
                for secret in (password, token):
                    assert secret not in json.dumps(data) and secret not in (root / '.jobs/fixture.json').read_text()
                page.goto(base + '/archive/fixture/entries/1.html')
                expect(page.locator('.post .prose img')).to_have_count(1)
                assert page.locator('.post .prose img').evaluate('e=>e.complete&&e.naturalWidth===1')
                assert not errors, errors
                browser.close()
        print('PASS: main resume, real browser login, denied then saved image, finite retries, same job, limits, HTML/JSON, cancellation control, mobile, stale/foreign confirmation')
    finally:
        if server:
            server.jobs.cancel.set()
            if server.jobs.thread: server.jobs.thread.join(10)
            server.shutdown(); server.server_close(); thread.join(5)
        try: next(fixture)
        except StopIteration: pass


if __name__ == '__main__': main()
