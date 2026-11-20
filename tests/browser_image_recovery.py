"""Browser login -> protected images -> archive; all network traffic stays local."""
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from ljarchive.server import make_server
from ljarchive.storage import Store
from ljarchive.render import build_archive
from ljarchive.recovery import inventory, summary
from test_browser_bridge import protected_browser, sign_in, PNG


def main():
    fixture = protected_browser.__wrapped__()
    bridge, source, calls, token = next(fixture)
    server = thread = None
    try:
        with tempfile.TemporaryDirectory(prefix='journal-image-ui-') as directory:
            root = Path(directory)
            store = Store(root / 'fixture')
            urls = [source + '/private.png', source + '/private.png?second=1', source + '/missing.png', source + '/denied.png']
            with store.db:
                store.set('username', 'fixture')
                store.save_entry({'itemid': 1, 'anum': 1, 'subject': 'Закрытые изображения', 'eventtime': '2024-01-01 00:00:00', 'security': 'private', 'event': ''.join(f'<img src="{u}">' for u in urls)})
                store.set('entry_materials', {'1': {'urls': urls}})
                store.set('media', {u: None for u in urls})
                store.set('media_errors', {u: {'status': 'pending', 'reason': 'web_login', 'http_status': 302, 'requests': 4, 'session_type': 'web'} for u in urls})
            build_archive(store, {'username': 'fixture', 'status': 'partial', 'created_at': '2026-01-01T00:00:00Z', 'entries_saved': 1, 'comments_saved': 0, 'missing_comments': []})
            recovery = summary(inventory(store)); store.close()
            server = make_server(root, 0); server.jobs.sessions._browsers['fixture'] = bridge
            server.jobs.current = {'archive': 'fixture', 'username': 'fixture', 'delay': 1, 'max_requests': 10}
            server.jobs.state = {'phase': 'partial', 'slug': 'fixture', 'username': 'fixture', 'job_id': 'image-ui', 'recovery': recovery, 'archive_url': '/archive/fixture/index.html'}
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            # Only the trusted-image host restriction changes for the local fixture.
            with patch('ljarchive.media.authenticated_image_host', lambda u: u.startswith(source + '/')), patch('ljarchive.on_demand.authenticated_image_host', lambda u: u.startswith(source + '/')), sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1000, 'height': 850})
                errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
                page.goto(base)
                page.locator('#job-more summary').click()
                page.get_by_role('button', name='Изображения: файлы и докачка').click()
                modal = page.locator('.image-recovery')
                expect(modal.locator('.image-failure')).to_have_count(4)
                modal.get_by_role('button', name='Докачать через браузер').click()
                expect(modal.get_by_role('button', name='Продолжить', exact=True)).to_be_enabled(timeout=10000)
                expect(modal).to_contain_text('Проверьте вход')
                sign_in(bridge, source, calls)
                calls.clear()
                modal.get_by_role('button', name='Продолжить', exact=True).click()
                expect(modal.locator('[data-role=status]')).to_contain_text('Сохранено 2 из 4 · остались ссылками 2', timeout=30000)
                expect(modal.locator('.image-failure')).to_have_count(0)
                expect(modal.get_by_role('button', name='Докачать через браузер')).to_be_hidden()
                assert calls.index('/denied.png') < calls.index('/private.png')
                assert calls.count('/denied.png') == calls.count('/missing.png') == 1
                assert len([u for u in calls if u.startswith('/private.png')]) == 2
                for width in (320, 1000):
                    page.set_viewport_size({'width': width, 'height': 850})
                    assert not modal.evaluate('e=>e.scrollWidth>e.clientWidth')
                page.screenshot(path=str(Path(tempfile.gettempdir()) / 'journal-browser-images.png'), full_page=True)
                page.reload()
                expect(page.locator('#job-message')).to_contain_text('Сохранено 2 из 4')
                expect(page.locator('#job-photo-result')).to_contain_text('сохранено 2 · недоступно 2')
                page.goto(base + '/archive/fixture/entries/1.html')
                expect(page.locator('.post .prose img')).to_have_count(2)
                assert page.locator('.post .prose img').evaluate_all('nodes=>nodes.every(e=>e.complete&&e.naturalWidth===1)')
                data = json.loads((root / 'fixture/data/archive.json').read_text())
                assert all((root / 'fixture' / data['media'][url]).read_bytes() == PNG for url in urls[:2])
                assert set(data['media_errors']) == set(urls[2:])
                assert all(e['closed'] and e['attempts'] == 1 for e in data['media_errors'].values())
                assert token not in json.dumps(data)
                assert not errors, errors
                browser.close()
        print('PASS: login window, browser cookies, batch selection, actual images in HTML/JSON, HTTP404, mobile, reload')
    finally:
        if server:
            server.jobs.materials.shutdown()
            if server.jobs.materials.thread: server.jobs.materials.thread.join(10)
            server.shutdown(); server.server_close(); thread.join(5)
        try: next(fixture)
        except StopIteration: pass


if __name__ == '__main__': main()
