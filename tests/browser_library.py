"""Gallery, profile-only download and calendar spacing, using local fixtures."""
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

from ljarchive.demo import create_demo
from ljarchive.server import make_server


def main():
    with tempfile.TemporaryDirectory(prefix='journal-library-browser-') as directory:
        root = Path(directory); folder = root / 'fixture'
        create_demo(folder)
        server = make_server(root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}/archive/fixture/'
        calls = []
        def collect(store, data, cancel, update, sessions=None):
            assert data['password'] == 'browser-fixture-password'
            calls.append('profile')
            update(phase='personal', message='Друзья и подписки')
            with store.db:
                store.set('personal_requested', {'account': True, 'messages': False})
                for key, value in {'profile': {'fullname': 'Владелец журнала'},
                    'foaf': {'fields': [{'name': 'bio', 'value': '<p>Об авторе<br>Вторая строка</p>' * 15},
                        *[{'name': 'interest', 'value': f'Тема {i}'} for i in range(20)]]},
                    'friends': {'friends': [{'username': 'reader', 'fullname': 'Читатель'}] * 700, 'friendofs': []},
                    'settings_display': {'controls': [{'name': 'LJ__Setting__TimeZone_timezone', 'type': 'select', 'options': [{'label': 'UTC', 'selected': True}]}]}}.items():
                    store.db.execute('INSERT OR REPLACE INTO account_sections VALUES (?,?)',
                        (key, json.dumps({'status': 'captured', 'data': value})))
            return {'kind': 'profile', 'complete': True}
        try:
            with patch('ljarchive.personal.download_profile', collect), sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1280, 'height': 950})
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                entry = (folder / 'entries/6.html').read_bytes()
                page.goto(base.split('/archive/')[0])
                page.goto(base + 'account.html')
                if not page.locator('#profile-action').is_visible():
                    page.locator('#profile-details summary').click()
                expect(page.locator('#profile-action')).to_be_visible()
                page.locator('#profile-load').click()
                page.locator('#profile-password').fill('browser-fixture-password')
                page.locator('#profile-form button').click()
                expect(page.locator('h1')).to_have_text('Владелец журнала')
                expect(page.locator('.friend-list a').first).to_be_hidden()
                page.locator('#friends summary').press('Enter')
                expect(page.locator('.friend-list a').first).to_be_visible()
                page.locator('#friends summary').press('Enter')
                expect(page.get_by_text('Тема 19', exact=True)).to_be_hidden()
                page.get_by_text('Ещё 8', exact=True).click()
                expect(page.get_by_text('Тема 19', exact=True)).to_be_visible()
                page.get_by_text('Читать полностью', exact=True).click()
                expect(page.locator('.profile-prose br').first).to_be_attached()
                page.goto(base + 'account.html#settings_display')
                expect(page.locator('.profile-settings')).to_be_visible()
                expect(page.locator('.profile-settings')).to_contain_text('Часовой пояс')
                assert calls == ['profile'] and (folder / 'entries/6.html').read_bytes() == entry
                assert 'browser-fixture-password' not in (folder / 'data/archive.json').read_text()
                page.screenshot(path='/tmp/journal-profile-desktop.png', full_page=True)
                page.goto(base + 'materials-image.html')
                expect(page.locator('.gallery-card').first).to_be_visible()
                tile = page.locator('.gallery-card').first
                expect(tile.locator('.gallery-caption')).to_be_hidden()
                page.clock.install()
                from datetime import datetime, timedelta, timezone
                page.clock.pause_at(datetime.now(timezone.utc) + timedelta(seconds=2))
                tile.hover()
                page.clock.run_for(349)
                assert not tile.locator('.gallery-caption').is_visible()
                page.clock.run_for(1)
                expect(tile.locator('.gallery-caption')).to_be_visible()
                assert tile.locator('.photo-panel').bounding_box()['width'] > tile.bounding_box()['width']
                page.keyboard.press('Escape')
                expect(tile.locator('.gallery-caption')).to_be_hidden()
                tile.locator('.gallery-preview a').press('Enter')
                expect(page.locator('.slide-view')).to_be_visible()
                page.locator('.slide-source').click()
                page.clock.resume()
                expect(page).to_have_url(base + 'entries/6.html')
                page.goto(base + 'materials.html')
                expect(page.locator('.gallery-card')).to_have_count(1)
                page.get_by_role('navigation', name='Теги записей').get_by_role('link', name='жизнь').click()
                expect(page.locator('.active-topic')).to_have_text('жизнь ×')
                expect(page.locator('.gallery-card')).to_have_count(1)
                page.get_by_role('link', name='Сбросить фильтр жизнь').click()
                page.get_by_role('link', name='Юзерпики', exact=False).first.click()
                expect(page.locator('.gallery-card')).to_have_count(2)
                page.locator('.gallery-preview img').first.click()
                viewer = page.locator('.slide-view')
                expect(viewer.locator('.slide-count')).to_have_text('1 / 2')
                first_src = viewer.locator('img').get_attribute('src')
                viewer.get_by_role('button', name='Следующий кадр').click()
                expect(viewer.locator('.slide-count')).to_have_text('2 / 2')
                assert viewer.locator('img').get_attribute('src') != first_src
                viewer.get_by_role('button', name='Автопоказ').click()
                page.clock.fast_forward(4100)
                expect(viewer.locator('.slide-count')).to_have_text('1 / 2')
                viewer.press('Escape')
                expect(viewer).to_be_hidden()
                page.goto(base + 'materials.html')
                page.screenshot(path='/tmp/journal-gallery-desktop.png', full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                for name in ('materials.html', 'account.html'):
                    page.goto(base + name)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), name
                page.screenshot(path='/tmp/journal-profile-mobile.png', full_page=True)
                page.goto((folder / 'account.html').as_uri())
                expect(page.locator('#profile-action')).to_be_hidden()
                page.goto((folder / 'materials-image.html').as_uri())
                assert page.locator('.gallery-preview img').first.evaluate('(img)=>img.complete && img.naturalWidth>0')
                phone = browser.new_context(has_touch=True, is_mobile=True, viewport={'width': 390, 'height': 844})
                mobile = phone.new_page()
                mobile.goto((folder / 'materials-image.html').as_uri())
                mobile.locator('.gallery-preview img').first.tap()
                expect(mobile.locator('.slide-view')).to_be_visible()
                mobile.get_by_role('button', name='Закрыть слайдшоу').tap()
                expect(mobile.locator('.gallery-caption').first).to_be_hidden()
                phone.close()
                page.set_viewport_size({'width': 1280, 'height': 950})
                page.goto(base + 'timeline.html')
                page.evaluate('''() => {
                  window.testTime=new ArchiveTimeline(document.querySelector('#archive-timeline'));
                  const entries=Array.from({length:60},(_,i)=>({id:i+1,date:`2025-${i<30?'10':'12'}-${String(i%30+1).padStart(2,'0')}`,title:'Post',state:'ready'}));
                  testTime.set({entries,ready:60,expected:60,partial:0});
                }''')
                assert page.evaluate('testTime.overviewBins.slice(0,Math.floor(testTime.overviewBins.length*.74)).every(b=>b.count===0)')
                assert page.evaluate('testTime.overviewBins.reduce((n,b)=>n+b.count,0)') == 60
                page.screenshot(path='/tmp/journal-timeline-time.png', full_page=True)
                page.locator('.timeline-scale [data-scale=posts]').click()
                assert page.evaluate('testTime.overviewBins.slice(0,Math.floor(testTime.overviewBins.length*.5)).some(b=>b.count>0)')
                page.locator('.timeline-scale [data-scale=time]').click()
                assert page.evaluate('testTime.indexAt(.1)') == 0
                create_demo(root / 'neighbor')
                page.goto(base.split('/archive/')[0])
                card = page.locator('[data-archive="fixture"].volume')
                menu = card.locator('details.more summary')
                menu.click()
                card.get_by_role('button', name='Удалить архив…').click()
                expect(page.locator('#delete-path')).to_have_text(str(folder))
                expect(page.locator('#delete-cancel')).to_be_focused()
                page.keyboard.press('Escape')
                expect(page.locator('#delete-dialog')).to_be_hidden()
                assert (folder / 'archive.sqlite3').is_file()
                page.set_viewport_size({'width': 390, 'height': 844})
                menu.click()
                card.get_by_role('button', name='Удалить архив…').click()
                page.locator('#delete-confirm').click()
                expect(card).to_have_count(0)
                expect(page.locator('[data-archive="neighbor"].volume')).to_be_visible()
                assert not folder.exists()
                assert not errors, errors
                browser.close()
            print('PASS: profile-only queue, gallery backlinks, mobile/file://, empty months on timeline')
        finally:
            server.shutdown(); server.server_close(); thread.join(5)


if __name__ == '__main__':
    main()
