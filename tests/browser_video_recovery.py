"""One video task survives reload and updates only its references; real local MP4."""
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from ljarchive.client import AuthenticationRequired
from ljarchive.render import build_archive
from ljarchive.server import make_server
from ljarchive.storage import Store
from test_video import video_source
from ljarchive.video import canonical_video


def main():
    fixture = video_source.__wrapped__(); url, calls, payload = next(fixture)
    local_urls = patch('ljarchive.video.canonical_video', lambda value, base='': url if value == url else canonical_video(value, base))
    local_urls.start()
    server = thread = None
    gate = threading.Event()
    try:
        with tempfile.TemporaryDirectory(prefix='journal-video-ui-') as directory:
            root=Path(directory);store=Store(root/'fixture')
            with store.db:
                store.set('username','fixture')
                for eid in (1,2):
                    store.save_entry({'itemid':eid,'anum':1,'subject':f'Video {eid}', 'eventtime':f'2024-01-0{eid} 12:00:00','event':f'<video src="{url}"></video>' if eid==1 else 'Untouched'})
            build_archive(store, {'username':'fixture','status':'partial','entries_saved':2,'comments_saved':0});store.close()
            old=(root/'fixture/entries/2.html').stat().st_mtime_ns
            server=make_server(root,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            server.jobs.thread=threading.Thread(target=lambda:gate.wait(20));server.jobs.thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            with patch('ljarchive.video.public_url'), patch('ljarchive.video.VideoArchive.wait',lambda *a:None),sync_playwright() as p:
                browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1000,'height':850})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base);page.goto(base+'/archive/fixture/materials-video.html')
                expect(page.locator('.gallery-card')).to_contain_text('Не запрашивалось')
                expect(page.locator('.material-source')).to_have_text(url)
                expect(page.locator('.material-source')).to_have_attribute('href',url)
                page.get_by_role('button',name='Загрузить',exact=True).click()
                expect(page.locator('.material-status')).to_contain_text('В очереди')
                page.reload()
                expect(page.locator('.material-status')).to_contain_text('В очереди')
                assert not calls and len(server.jobs.materials.states)==1
                gate.set();server.jobs.thread.join()
                video=page.locator('.gallery-preview video')
                expect(video).to_be_visible(timeout=15000)
                expect(page.locator('.material-controls')).to_have_count(0,timeout=15000)
                assert page.locator('input[type=password]').count()==0
                expect(page.locator('.material-result')).to_contain_text('Сохранено')
                video.evaluate('e=>e.load()')
                page.wait_for_function('()=>document.querySelector(".gallery-preview video").readyState>=1')
                assert video.evaluate('e=>e.duration>0')
                assert (root/'fixture/entries/2.html').stat().st_mtime_ns==old
                state=next(iter(server.jobs.materials.states.values()))
                assert state['phase']=='complete' and (root/'fixture'/state['asset']['path']).read_bytes()==payload
                page.reload()
                expect(page.locator('.gallery-preview video')).to_have_count(1)
                expect(page.get_by_role('button',name='Загрузить',exact=True)).to_have_count(0)
                page.goto(base+'/archive/fixture/entries/1.html')
                expect(page.locator('.post video')).to_have_count(1)
                for width in (320,1000):
                    page.set_viewport_size({'width':width,'height':850})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert not errors,errors
                browser.close()
        print('PASS: real video bytes/playback, gallery progress/reload, no password, targeted HTML/JSON, mobile')
    finally:
        local_urls.stop()
        gate.set()
        if server:
            server.jobs.materials.shutdown()
            if server.jobs.materials.thread:server.jobs.materials.thread.join(10)
            server.shutdown();server.server_close();thread.join(5)
        try:next(fixture)
        except StopIteration:pass


if __name__=='__main__':main()
