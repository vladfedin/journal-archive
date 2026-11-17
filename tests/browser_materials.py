"""Explicit media downloads in the browser; all external responses are fixtures."""
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from ljarchive.server import make_server
from test_on_demand import fixture, URL
from test_media_errors import ImageResponse


def main():
    with tempfile.TemporaryDirectory(prefix='ljarchive-materials-') as directory:
        root=Path(directory);fixture(root/'fixture')
        server=make_server(root,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII=')
        calls=[]
        gate=threading.Event()
        try:
            def image_get(session,url,**kwargs):
                assert url==URL
                calls.append(url)
                return ImageResponse(content=png,headers={'Content-Type':'image/png'})
            def video_download(manager,url,eid):
                path='media/'+hashlib.sha256(url.encode()).hexdigest()+'.mp4'
                (manager.store.folder/path).write_bytes(b'fixture-video')
                manager.records[url]={'url':url,'status':'saved','path':path,'entries':[eid]};manager.save()
            def post(client,*args,**kwargs):
                return b'<iframe src="https://l.lj-toys.com/?moduleid=235&auth_token=SECRET"></iframe>'
            def wrapper(*args):return io.BytesIO(b'<iframe src="https://music.yandex.ru/iframe/album/1"></iframe>')
            with patch('ljarchive.media.public_url'),patch('ljarchive.media.Media.wait',return_value=True),patch('requests.Session.get',image_get),patch('ljarchive.video.VideoArchive.download',video_download),patch('ljarchive.client.LiveJournalClient._request',post),patch('ljarchive.video.VideoArchive.open',wrapper),sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={'width':1000,'height':900})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                widgets=[]
                page.route('https://music.yandex.ru/**',lambda route:(widgets.append(route.request.url),route.fulfill(content_type='text/html',body='<p>Music widget</p>')))
                page.goto(base)
                page.goto(base+'/archive/fixture/index.html')
                slot=page.locator('[data-material-kind=image]')
                expect(slot.get_by_role('button',name='Загрузить',exact=True)).to_be_visible()
                # A click during export waits; reading remains responsive.
                server.jobs.thread=threading.Thread(target=lambda:gate.wait(15));server.jobs.thread.start()
                slot.get_by_role('button',name='Загрузить',exact=True).click()
                expect(slot.locator('.material-status')).to_contain_text('В очереди')
                assert calls==[]
                gate.set();server.jobs.thread.join()
                expect(slot.locator('img')).to_be_visible(timeout=15000)
                expect(slot.locator('.material-controls')).to_have_count(0,timeout=15000)
                assert calls==[URL] and slot.locator('img').evaluate('e=>e.complete&&e.naturalWidth>0')
                page.goto(base+'/archive/fixture/entries/1.html')
                expect(page.locator('.post .prose img')).to_have_count(1)
                video=page.locator('#comment-10 .material-slot')
                video.get_by_role('button',name='Загрузить',exact=True).click()
                expect(page.locator('#comment-10 video')).to_be_visible(timeout=15000)
                expect(video.locator('.material-controls')).to_have_count(0,timeout=15000)
                page.locator('.lj-cut').evaluate_all("nodes=>nodes.forEach(n=>n.open=true)")
                page.get_by_role('button',name='Загрузить вставку').click()
                expect(page.get_by_role('button',name='Онлайн-плеер')).to_be_visible(timeout=15000)
                expect(page.locator('[data-material-kind=embed] .material-controls')).to_have_count(0,timeout=15000)
                expect(page.locator('iframe')).to_have_count(0)
                assert not widgets
                page.get_by_role('button',name='Онлайн-плеер').click()
                expect(page.locator('iframe')).to_have_count(1)
                expect(page.frame_locator('iframe').get_by_text('Music widget')).to_be_visible()
                assert widgets == ['https://music.yandex.ru/iframe/album/1']
                page.wait_for_function('document.querySelector("iframe").contentWindow !== null')
                page.reload()
                page.locator('.lj-cut').evaluate_all("nodes=>nodes.forEach(n=>n.open=true)")
                expect(page.get_by_role('button',name='Онлайн-плеер')).to_be_visible()
                expect(page.locator('#comment-10 video')).to_have_count(1)
                expect(page.locator('[data-material-kind=image]')).to_have_count(0)
                for width in (320,390,1000):
                    page.set_viewport_size({'width':width,'height':900})
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                page.screenshot(path=str(Path(tempfile.gettempdir())/'ljarchive-materials.png'),full_page=True)
                page.goto((root/'fixture/entries/1.html').as_uri())
                expect(page.locator('.post .prose img')).to_have_count(1)
                expect(page.locator('#comment-10 video')).to_have_count(1)
                expect(page.get_by_role('button',name='Загрузить',exact=True)).to_have_count(0)
                data=json.loads((root/'fixture/data/archive.json').read_text())
                assert data['media'][URL] and data['videos'] and data['external_embeds']
                assert 'SECRET' not in json.dumps(data)
                assert calls==[URL] and not errors,errors
                browser.close()
            print('PASS: click/queue, immediate image/video, local JSON/HTML, music opt-in, reload/file://, mobile')
        finally:
            gate.set();server.jobs.materials.shutdown()
            if server.jobs.materials.thread:server.jobs.materials.thread.join(10)
            server.shutdown();server.server_close();thread.join(5)


if __name__=='__main__':main()
