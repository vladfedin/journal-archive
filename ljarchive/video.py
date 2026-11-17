"""Embedded video inventory and paced downloads; source text is never rewritten."""
from __future__ import annotations

import hashlib
import html
import io
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests

from .client import Cancelled, ExportError, RequestLimitReached
from .media import public_url
from .retry import backoff

VIDEO_SUFFIXES = {'.mp4', '.webm', '.ogv', '.mov', '.m4v'}


def canonical_video(value, base=''):
    url = urljoin(base, html.unescape(value or ''))
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or p.username or p.password or p.port not in (None, 80, 443):
            return None
        host = (p.hostname or '').lower()
        q = parse_qs(p.query)
        video_id = None
        if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'www.youtube-nocookie.com', 'youtube-nocookie.com'):
            video_id = q.get('v', [None])[0] if p.path == '/watch' else p.path.rstrip('/').split('/')[-1]
        elif host == 'youtu.be':
            video_id = p.path.strip('/')
        elif host in ('l.lj-toys.com', 'www.livejournal.com') and q.get('source') == ['youtube']:
            video_id = q.get('vid', [None])[0]
        if video_id and re.fullmatch(r'[\w-]{11}', video_id):
            return 'https://www.youtube.com/watch?v=' + video_id
        if host in ('vk.com', 'vkvideo.ru', 'www.vk.com', 'www.vkvideo.ru'):
            oid, vid = q.get('oid', [''])[0], q.get('id', [''])[0]
            if re.fullmatch(r'-?\d+', oid) and vid.isdigit():
                return f'https://vkvideo.ru/video{oid}_{vid}'
            if re.fullmatch(r'/video-?\d+_\d+', p.path):
                return 'https://vkvideo.ru' + p.path
        if host in ('vimeo.com', 'player.vimeo.com') and p.path.rstrip('/').split('/')[-1].isdigit():
            return 'https://vimeo.com/' + p.path.rstrip('/').split('/')[-1]
        if Path(p.path).suffix.lower() in VIDEO_SUFFIXES:
            return urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))
    except ValueError:
        pass
    return None


def embed_key(base, module):
    return base.split('#')[0] + '#lj-embed-' + str(module)


def external_embed(value, base=''):
    """Recognize a service widget without claiming that its audio was archived."""
    try:
        p = urlsplit(urljoin(base, html.unescape(value or '')))
        if p.scheme != 'https' or p.hostname != 'music.yandex.ru' or p.username or p.password or p.port not in (None, 443):
            return None
        if re.fullmatch(r'/iframe/(?:album/\d+(?:/track/\d+)?|track/\d+)', p.path):
            return {'provider': 'Яндекс Музыка', 'kind': 'audio_service', 'status': 'online_only',
                    'url': 'https://music.yandex.ru' + p.path.removeprefix('/iframe'),
                    'embed_url': 'https://music.yandex.ru' + p.path}
    except ValueError:
        pass
    return None


class VideoRefs(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.urls, self.modules, self.resolved = base, set(), set(), {}
        self.wrappers, self.external = {}, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'lj-embed' and str(a.get('id', '')).isdigit():
            self.modules.add(a['id'])
        if tag in ('iframe', 'embed', 'video', 'source', 'object', 'a'):
            value = a.get('src') or a.get('data') or a.get('href')
            if value and tag == 'iframe':
                widget = external_embed(value, self.base)
                if widget: self.external.append(widget)
                try:
                    p = urlsplit(urljoin(self.base, html.unescape(value)))
                    q = parse_qs(p.query)
                    module = q.get('moduleid', [''])[0]
                    if p.scheme == 'https' and p.hostname == 'l.lj-toys.com' and not p.username and not p.password and module.isdigit():
                        self.wrappers[module] = urljoin(self.base, html.unescape(value))
                except ValueError:
                    pass
            url = canonical_video(value, self.base) if value else None
            if url:
                self.urls.add(url)
                q = parse_qs(urlsplit(html.unescape(value)).query)
                module = q.get('moduleid', [''])[0]
                if module.isdigit():
                    self.resolved[module] = url


def video_markup(source, base, media):
    """Translate embeds to inert video elements before sanitization and local rendering."""
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.out, self.capture, self.attrs, self.children = [], None, {}, []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if self.capture:
                if tag == 'source' and not self.attrs.get('src'):
                    self.attrs['src'] = a.get('src', '')
                return
            if tag == 'lj-embed':
                key = embed_key(base, a.get('id', ''))
                url = getattr(media, 'video_embeds', {}).get(key)
                if url:
                    self.out.append('<video src="' + html.escape(url, quote=True) + '"></video>')
                else:
                    self.out.append('<video data-lj-module="' + html.escape(str(a.get('id', '')), quote=True) + '" src="' + html.escape(base, quote=True) + '"></video>')
            elif tag in ('iframe', 'video', 'object'):
                self.capture, self.attrs = tag, a
            elif tag == 'embed':
                self.emit(a.get('src', ''))
            else:
                self.out.append(self.get_starttag_text())

        def emit(self, value):
            url = canonical_video(value, base)
            if url:
                self.out.append('<video src="' + html.escape(url, quote=True) + '"></video>')
            elif value:
                self.out.append('<p><a href="' + html.escape(urljoin(base, value), quote=True) + '">Открыть встроенный материал в оригинале ↗</a></p>')

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if self.capture == tag:
                self.handle_endtag(tag)

        def handle_endtag(self, tag):
            if self.capture:
                if tag == self.capture:
                    self.emit(self.attrs.get('src') or self.attrs.get('data', ''))
                    self.capture = None
            elif tag not in ('lj-embed', 'embed'):
                self.out.append('</' + tag + '>')

        def handle_data(self, value):
            if not self.capture: self.out.append(value)
        def handle_entityref(self, name):
            self.handle_data('&' + name + ';')
        def handle_charref(self, name):
            self.handle_data('&#' + name + ';')
    parser = Parser(); parser.feed(source); parser.close()
    if parser.capture:
        parser.emit(parser.attrs.get('src') or parser.attrs.get('data', ''))
    return ''.join(parser.out)


class VideoStop(BaseException):
    def __init__(self, reason):
        self.reason = reason


class VideoArchive:
    def __init__(self, store, client, progress):
        self.store, self.client, self.progress = store, client, progress
        self.records, self.embeds = store.get('videos', {}), store.get('video_embeds', {})
        self.external = store.get('external_embeds', {})
        self.attempted, self.pages = set(), set()
        self.session = requests.Session(); self.session.trust_env = False
        self.last_http = None

    def save(self):
        with self.store.db:
            self.store.set('videos', self.records)
            self.store.set('video_embeds', self.embeds)
            self.store.set('external_embeds', self.external)

    def resolve(self, entry, modules):
        from .render import original_url
        base = original_url(self.client.user, entry)
        p = urlsplit(base)
        expected = self.client.user.replace('_', '-') + '.livejournal.com'
        if p.hostname != expected or not re.fullmatch(r'/\d+\.html', p.path):
            raise ExportError('Не удалось определить страницу вставки')
        headers = {'Cookie': 'ljsession=' + self.client._cookie} if getattr(self.client, '_cookie', None) else {}
        content = self.client._request('GET', p.path, base_url='https://' + expected, headers=headers)
        page = VideoRefs(base); page.feed(content.decode('utf-8', 'replace')); page.close()
        for module in modules:
            key = embed_key(base, module)
            if module in page.resolved:
                self.embeds[key] = page.resolved[module]
            elif module in page.wrappers:
                with self.open(page.wrappers[module]) as response:
                    body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise ExportError('Слишком большая страница вставки')
                child = VideoRefs(base); child.feed(body.decode('utf-8', 'replace')); child.close()
                if len(child.urls) == 1:
                    self.embeds[key] = next(iter(child.urls))
                elif len(child.external) == 1:
                    self.external[key] = {**child.external[0], 'entries': [int(entry['itemid'])]}
            if key in self.embeds or key in self.external:
                self.records.pop(key, None)
        self.save()

    def check(self):
        if self.client.cancel.is_set():
            raise VideoStop('cancelled')

    def wait(self, seconds):
        if self.client.cancel.wait(max(0, seconds)):
            raise VideoStop('cancelled')

    def open(self, request):
        from yt_dlp.networking import Request, Response
        from yt_dlp.networking.exceptions import HTTPError, TransportError
        if isinstance(request, str): request = Request(request)
        current, method, data = request.url, request.method, request.data
        budget = self.client.budget
        owner = self
        class Stream(io.RawIOBase):
            def __init__(self, response): self.response = response
            def readable(self): return True
            def read(self, size=-1):
                owner.check()
                try: return self.response.raw.read(size, decode_content=True)
                except Exception as exc: raise TransportError('Video transfer interrupted', cause=exc) from exc
            def close(self):
                self.response.close(); super().close()
        for attempt in range(6):
            self.check(); public_url(current)
            if budget.limit is not None and budget.used >= budget.limit:
                budget.consume('videos')
            self.wait(max(budget.delay, budget.not_before - time.time()))
            budget.consume('videos')
            try:
                r = self.session.request(method, current, data=data, headers=dict(request.headers),
                                         stream=True, timeout=(10, 20), allow_redirects=False)
            except requests.RequestException as exc:
                if attempt == 5: raise TransportError('Video connection failed', cause=exc) from exc
                budget.not_before = time.time() + backoff(attempt)
                budget.save_cooldown(budget.not_before)
                continue
            self.last_http = r.status_code
            if r.status_code in (429, 500, 502, 503, 504):
                budget.not_before = time.time() + backoff(attempt, r.headers.get('Retry-After'), limited=r.status_code == 429)
                budget.save_cooldown(budget.not_before)
                if attempt < 5:
                    r.close(); self.progress(phase='waiting', message='Пауза перед повтором загрузки видео', retry_at=budget.not_before)
                    continue
            if 300 <= r.status_code < 400:
                current = urljoin(current, r.headers.get('Location', ''))
                if r.status_code == 303 or (r.status_code in (301, 302) and method == 'POST'):
                    method, data = 'GET', None
                r.close(); continue
            response = Response(Stream(r), current, r.headers, r.status_code, r.reason)
            if r.status_code >= 400: raise HTTPError(response)
            return response
        raise TransportError('Too many video redirects')

    def download(self, url, eid):
        record = self.records.setdefault(url, {'url': url, 'status': 'not_requested', 'entries': []})
        record['entries'] = sorted(set(record.get('entries', [])) | {eid})
        if record.get('status') == 'saved' and (self.store.folder / record.get('path', '')).is_file():
            return
        if url in self.attempted: return
        self.attempted.add(url)
        record.update(status='pending', checked_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
        self.save()
        import yt_dlp
        owner = self
        class Downloader(yt_dlp.YoutubeDL):
            def urlopen(self, req):
                try: return owner.open(req)
                except RequestLimitReached: raise VideoStop('request_limit') from None
        class Logger:
            def debug(self, msg): pass
            def warning(self, msg): pass
            def error(self, msg): pass
        digest = hashlib.sha256(url.encode()).hexdigest()
        work = self.store.folder / '.video-work' / digest
        work.mkdir(parents=True, exist_ok=True)
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        runtime = Path(getattr(sys, '_MEIPASS', Path(__file__).parent.parent / 'build')) / 'video-runtime' / ('deno.exe' if os.name == 'nt' else 'deno')
        js = {'deno': {'path': str(runtime)}} if runtime.is_file() else {'deno': {}, 'node': {}}
        def hook(state):
            self.check()
            if state['status'] == 'downloading':
                self.progress(phase='videos', message=record.get('title') or 'Видео',
                              completed=state.get('downloaded_bytes', 0), total=state.get('total_bytes') or state.get('total_bytes_estimate') or 0)
        options = {'quiet': True, 'no_warnings': True, 'logger': Logger(), 'noplaylist': True,
                   'cachedir': False, 'retries': 2, 'fragment_retries': 2,
                   'retry_sleep_functions': {'http': lambda attempt: self.wait(backoff(attempt)) or 0, 'fragment': lambda attempt: self.wait(backoff(attempt)) or 0}, 'extractor_retries': 0,
                   'socket_timeout': 20, 'concurrent_fragment_downloads': 1, 'fixup': 'never',
                   'ffmpeg_location': ffmpeg, 'js_runtimes': js, 'remote_components': [],
                   'outtmpl': str(work / 'video.%(ext)s'), 'overwrites': False,
                   'progress_hooks': [hook], 'merge_output_format': 'mp4',
                   'format': 'b[height<=?720][ext=mp4][protocol=https]/bv[height<=?720][ext=mp4][protocol=https]+ba[ext=m4a][protocol=https]/b[height<=?720][protocol=https]/b[protocol=http]',
                   'match_filter': lambda info, **kw: 'Прямой эфир не архивируется' if info.get('is_live') or info.get('live_status') == 'is_upcoming' else None}
        try:
            self.last_http = None
            with Downloader(options) as downloader:
                info = downloader.extract_info(url, download=False)
                if not info or info.get('_type') in ('playlist', 'multi_video'):
                    raise ValueError('Not a single video')
                record.update(title=info.get('title'), duration=info.get('duration'), provider=info.get('extractor_key'), video_id=info.get('id'))
                self.save()
                downloader.process_info(info)
            paths = [p for p in work.glob('video.*') if p.suffix in VIDEO_SUFFIXES and '.f' not in p.name]
            if len(paths) != 1 or not paths[0].stat().st_size:
                raise ValueError('Video file was not produced')
            target = self.store.folder / 'media' / (digest + paths[0].suffix)
            target.parent.mkdir(exist_ok=True)
            paths[0].replace(target)
            record.update(status='saved', path=target.relative_to(self.store.folder).as_posix(), bytes=target.stat().st_size,
                          height=info.get('height'), format=info.get('format_id'))
            record.pop('error', None); shutil.rmtree(work)
        except VideoStop as exc:
            record.update(status='pending', error={'reason': exc.reason})
        except Exception as exc:
            reason = 'http' if self.last_http and self.last_http >= 400 else 'download'
            record.update(status='failed', error={'reason': reason, 'http_status': self.last_http if reason == 'http' else None,
                                                  'type': type(exc).__name__, 'detail': re.sub(r'https?://[^\s]+', '[URL]', str(exc))[:500]})
        finally:
            self.save()

    def capture(self, entry, enabled):
        from .render import original_url, text_value
        eid, base = int(entry['itemid']), original_url(self.client.user, entry)
        refs = VideoRefs(base)
        for record in [entry, *self.store.comments(eid)]:
            refs.feed(text_value(record.get('event', record.get('body'))))
        refs.close()
        missing = [module for module in refs.modules if embed_key(base, module) not in self.embeds and embed_key(base, module) not in self.external]
        if enabled and missing and base not in self.pages:
            self.pages.add(base)
            try:
                self.resolve(entry, missing)
            except (ExportError, OSError, VideoStop):
                pass
            except Exception:
                # One failed widget must not abort the remaining journal export.
                pass
        for module in refs.modules:
            key = embed_key(base, module)
            if key in self.embeds: refs.urls.add(self.embeds[key])
            elif key not in self.external:
                record = self.records.setdefault(key, {'url': base, 'entries': [eid], 'status': 'not_requested'})
                if enabled:
                    reason = 'request_limit' if self.client.budget.exhausted else 'cancelled' if self.client.cancel.is_set() else 'embed_unresolved'
                    record.update(status='pending' if reason != 'embed_unresolved' else 'failed', error={'reason': reason})
        self.save()
        for url in sorted(refs.urls):
            if enabled: self.download(url, eid)
            else:
                record = self.records.setdefault(url, {'url': url, 'status': 'not_requested', 'entries': []})
                record['entries'] = sorted(set(record.get('entries', [])) | {eid})
        for module in refs.modules:
            if embed_key(base, module) in self.embeds: self.records.pop(embed_key(base, module), None)
        self.save()

    def close(self):
        self.session.close()


def capture_videos(exporter, entry):
    manager = getattr(exporter, '_videos', None)
    if manager is None:
        manager = exporter._videos = VideoArchive(exporter.store, exporter.client, exporter.progress)
    manager.capture(entry, getattr(exporter, 'videos', False))


def video_report(store, selected=None):
    records = store.get('videos', {})
    rows = {url: r for url, r in records.items() if selected is None or set(r.get('entries', [])) & set(selected)}
    for record in rows.values():
        if record.get('error'):
            error = record['error']
            error['message'] = {'request_limit': 'Достигнут лимит запросов', 'cancelled': 'Загрузка остановлена',
                'embed_unresolved': 'Не удалось получить адрес видео', 'download': 'Не удалось скачать видео'}.get(error.get('reason'),
                'HTTP ' + str(error.get('http_status')) if error.get('reason') == 'http' else 'Не загружено')
            if error.get('detail'): error['message'] += ': ' + error['detail']
    return {'total': len(rows), 'saved': sum(r['status'] == 'saved' for r in rows.values()),
            'missing': {url: r for url, r in rows.items() if r['status'] != 'saved'}}


def missing_by_entry(store):
    from collections import Counter
    counts = Counter()
    for record in store.get('videos', {}).values():
        if record['status'] in ('failed', 'pending'):
            counts.update(record.get('entries', []))
    return counts
