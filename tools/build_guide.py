"""Build a standalone HTML and printable PDF from docs/guide/README.md.

Install doc-only dependencies: pip install markdown playwright
Capture screenshots first with tools/capture_guide.py.
"""
from pathlib import Path
import base64
import mimetypes
import re

import markdown
from markdown.extensions.toc import slugify_unicode
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / 'docs' / 'guide'
PUBLIC = 'https://staerum.ru/journal-archive/guide/'

STYLE = '''
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:#20262e;background:#f5f6f8;font:17px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1050px;margin:auto;padding:48px 60px 64px;background:white}h1{font-size:40px;line-height:1.15;margin:0 0 18px}h2{font-size:28px;line-height:1.25;margin:64px 0 18px;padding-top:24px;border-top:1px solid #dce1e7;scroll-margin:20px}p{margin:14px 0}li{margin:8px 0}a{color:#1e5fc4;text-underline-offset:3px}img{display:block;max-width:100%;height:auto;border:1px solid #dce1e7;border-radius:7px}figure{margin:24px 0;break-inside:avoid}figcaption{color:#596574;font-size:13px;line-height:1.45;margin-top:8px}code{font-size:.88em;overflow-wrap:anywhere}hr{border:0;border-top:1px solid #dce1e7;margin:36px 0}
.print{position:fixed;right:24px;bottom:24px;background:#245fc0;color:white;border:0;border-radius:8px;padding:12px 20px;font:inherit;cursor:pointer;box-shadow:0 4px 20px #0002}
@media(max-width:700px){body{font-size:16px}main{padding:28px 20px}h1{font-size:32px}h2{font-size:24px;margin-top:42px}.print{position:static;display:block;margin:16px auto}figure{margin-left:-8px;margin-right:-8px}}
@media print{@page{size:A4;margin:15mm 15mm 18mm}body{font:10.5pt/1.5 Arial,sans-serif;background:white}main{padding:0;max-width:none}h1{font-size:28pt}h2{font-size:20pt;break-after:avoid;margin:20px 0 14px;padding:0;border:0}h2:first-of-type{break-before:auto;margin-top:24px}p{margin:10px 0}li{margin:5px 0}figure{margin:12px 0}img{max-height:108mm;width:auto;max-width:100%;margin:auto}figcaption{font-size:8pt}a{color:#174d9d;text-decoration:none}.print{display:none}}
'''


def main():
    body = markdown.markdown((GUIDE / 'README.md').read_text(), extensions=['tables', 'toc'],
                             extension_configs={'toc': {'slugify': slugify_unicode}})
    # Short ASCII PDF destination names avoid oversized escaped Unicode names.
    for number, anchor in enumerate(re.findall(r'id="([^"]+)"', body)):
        body = body.replace('id="' + anchor + '"', f'id="section-{number}"')
        body = body.replace('href="#' + anchor + '"', f'href="#section-{number}"')
    def embed(relative, alt):
        path = GUIDE / relative
        mime = mimetypes.guess_type(path.name)[0]
        uri = 'data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode()
        return f'<figure><img src="{uri}" alt="{alt}"><figcaption>{alt}</figcaption></figure>'
    body = re.sub(r'<p><img alt="([^"]*)" src="([^"]+)"\s*/></p>',
                  lambda m: embed(m[2], m[1]), body)
    body = body.replace('href="guide.pdf"', 'href="' + PUBLIC + 'guide.pdf"')
    body = body.replace('href="../../README.md"', 'href="https://github.com/vladfedin/journal-archive#readme"')
    body = body.replace(f'<a href="{PUBLIC}">Веб-версия</a>',
                        f'<a href="{PUBLIC}" download="guide.html">Скачать HTML</a>')
    document = '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Как сохранить свой ЖЖ · Гайд для Windows</title><style>' + STYLE + '</style><main>' + body + '</main><button class="print" onclick="window.print()">Печать / PDF</button></html>'
    (GUIDE / 'guide.html').write_text(document)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.context.set_offline(True)
        page.goto((GUIDE / 'guide.html').as_uri())
        page.evaluate('document.fonts.ready')
        assert page.locator('img').count() == 16
        assert page.locator('img').evaluate_all('(items) => items.every(i => i.complete && i.naturalWidth > 0)')
        broken = page.locator('a[href^="#"]').evaluate_all('(links) => links.filter(a => !document.getElementById(decodeURIComponent(a.hash.slice(1)))).map(a => a.hash)')
        assert not broken, broken
        page.pdf(path=str(GUIDE / 'guide.pdf'), print_background=True, prefer_css_page_size=True, tagged=True, outline=True,
                 display_header_footer=True, header_template='<span></span>',
                 footer_template='<div style="width:100%;font:9px Arial;color:#65707d;padding:0 15mm;display:flex;justify-content:space-between"><span>Архив ЖЖ · Windows · 0.9.6</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
        for width in (390, 1280):
            page.set_viewport_size({'width': width, 'height': 900})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=f'/tmp/ljarchive-guide-{width}.png')
        browser.close()
    print('Built standalone HTML and PDF; images, navigation and responsive layout checked.')


if __name__ == '__main__':
    main()
