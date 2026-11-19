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
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
import json

import requests

from .client import AuthenticationRequired, Cancelled, ExportError, HTTPError, RequestLimitReached
from .media import public_url
from .retry import backoff
from .gaps import ATTEMPTS, closed, fail, reopen

VIDEO_SUFFIXES = {'.mp4', '.webm', '.ogv', '.mov', '.m4v'}


def lj_video_url(record):
    if isinstance(record, str) and re.fullmatch(r'(?:[0-9]+|record::[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})', record):
        return 'https://vc.videos.livejournal.com/index/player?' + urlencode({'record_id': record})
    return None


def lj_video_id(url):
    p = urlsplit(url)
    if p.scheme == 'https' and p.hostname == 'vc.videos.livejournal.com' and p.path == '/index/player' and not p.username and not p.password and p.port in (None, 443):
        value = parse_qs(p.query).get('record_id', [''])[0]
        return value if lj_video_url(value) else None
    return None


def rambler_frame(parts):
    if (parts.scheme == 'https' and parts.netloc == 'vp.rambler.ru'
            and re.fullmatch(r'/player/[\d.]+/player\.html', parts.path)):
        fields = parse_qs(parts.fragment)
        record = fields.get('ramblerPlayerId', fields.get('id', ['']))[0]
        if lj_video_url(record):
            return record, fields.get('authToken', [''])[0]
    return None


def canonical_video(value, base=''):
    url = urljoin(base, html.unescape(value or ''))
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or p.username or p.password or p.port not in (None, 80, 443):
            return None
        host = (p.hostname or '').lower()
        q = parse_qs(p.query)
        if host == 'vp.rambler.ru' and (frame := rambler_frame(p)):
            return lj_video_url(frame[0])
        if host == 'vc.videos.livejournal.com' and p.path == '/index/player':
            return lj_video_url(q.get('record_id', [''])[0])
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


class LJPlayer(HTMLParser):
    def __init__(self, base, identities, *, comments=True):
        super().__init__()
        self.base, self.identities = base, identities
        self.comments = comments
        self.script = False
        self.token = None

    def handle_starttag(self, tag, attrs):
        self.script = tag == 'script'
        a = dict(attrs)
        if a.get('data-rambler-player-id') in self.identities and a.get('data-auth-token'):
            self.token = a['data-auth-token']
        if tag == 'iframe':
            try:
                frame = rambler_frame(urlsplit(urljoin(self.base, a.get('src') or '')))
            except ValueError:
                return
            if frame and frame[0] in self.identities and frame[1]:
                self.token = frame[1]

    def handle_endtag(self, tag):
        if tag == 'script':
            self.script = False

    def handle_data(self, data):
        if not self.comments or not self.script or 'rambler' not in data:
            return
        # LJ serializes the HTML of each discussion comment in an article field.
        for match in re.finditer(r'"article"\s*:\s*(?=")', data):
            try:
                article, _ = json.JSONDecoder().raw_decode(data, match.end())
            except ValueError:
                continue
            if 'rambler' in article:
                child = LJPlayer(self.base, self.identities, comments=False)
                child.feed(article)
                child.close()
                if child.token:
                    self.token = child.token
                    return


class VideoRefs(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.urls, self.modules, self.resolved = base, set(), set(), {}
        self.wrappers, self.external = {}, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'div' and (url := lj_video_url(a.get('data-rambler-player-id'))):
            self.urls.add(url)
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
            elif tag == 'div' and (url := lj_video_url(a.get('data-rambler-player-id'))):
                self.out.append('<video src="' + html.escape(url, quote=True) + '"></video><div>')
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
        self.trace, self.stage = [], "source"

    def save(self):
        with self.store.db:
            self.store.set('videos', self.records)
            self.store.set('video_embeds', self.embeds)
            self.store.set('external_embeds', self.external)

    def step(self, stage, message, **values):
        self.stage = stage
        self.progress(phase='running', stage=stage, message=message, completed=0, total=0, **values)

    def journal_page(self, base, *, params=None):
        """Reuse the existing web/browser session; a missing player is not a login failure."""
        from .web_session import cookie_header, AGENT
        from .browser_bridge import safe_url, BrowserFailure, BrowserLoginRequired
        def checked(body):
            if b'action="/login.bml"' in body or b"action='/login.bml'" in body:
                raise AuthenticationRequired('ЖЖ вернул форму входа.', status=200)
            return body
        browser = getattr(self.client, 'browser', None)
        if browser and browser.ready(self.client.account):
            url = base + ('?' + urlencode(params) if params else '')
            try:
                body, trace = browser.document(self.client.account, url, '', self.client.budget, self.client.cancel)
                self.trace.extend({**item, 'stage': self.stage} for item in trace)
                return checked(body)
            except ExportError as exc:
                self.trace.extend({**item, 'stage': self.stage} for item in getattr(exc, 'trace', []))
                if isinstance(exc, BrowserLoginRequired) or (isinstance(exc, BrowserFailure) and exc.reason in ('browser_login', 'browser_closed', 'browser_start')):
                    raise AuthenticationRequired('Сеанс браузера завершён. Войдите в ЖЖ.') from None
                raise
        p = urlsplit(base)
        headers = {'User-Agent': AGENT}
        cookie = cookie_header(self.client, base)
        if not cookie and self.client._cookie:
            cookie = 'ljsession=' + self.client._cookie
        if cookie: headers['Cookie'] = cookie
        trace = {'url': safe_url(base), 'transport': 'http', 'http_status': None, 'stage': self.stage}
        self.trace.append(trace)
        try:
            body = self.client._request('GET', p.path, base_url='https://' + p.hostname,
                                        category='videos', headers=headers, **({'params': params} if params else {}))
            trace['http_status'] = 200
            return checked(body)
        except (HTTPError, AuthenticationRequired) as exc:
            trace['http_status'] = exc.status
            raise

    def resolve(self, entry, modules):
        from .render import original_url
        base = original_url(self.client.user, entry)
        p = urlsplit(base)
        expected = self.client.user.replace('_', '-') + '.livejournal.com'
        if p.hostname != expected or not re.fullmatch(r'/\d+\.html', p.path):
            raise ExportError('Не удалось определить страницу вставки')
        self.step('source', 'Определяем источник вставки…')
        content = self.journal_page(base)
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
            budget.announce_wait()
            self.wait(max(budget.delay, budget.not_before - time.time()))
            budget.consume('videos')
            from .browser_bridge import safe_url
            trace = {'url': safe_url(current), 'transport': 'http', 'http_status': None, 'stage': self.stage}
            self.trace.append(trace)
            budget.event('request', 'videos', current, request_url=safe_url(current), attempt=attempt + 1, max_attempts=6)
            try:
                r = self.session.request(method, current, data=data, headers=dict(request.headers),
                                         stream=True, timeout=(10, 20), allow_redirects=False)
            except requests.RequestException as exc:
                raise TransportError('Video connection failed', cause=exc) from exc
            self.last_http = r.status_code
            trace["http_status"] = r.status_code
            budget.event("response", "videos", current, http_status=r.status_code, request_url=safe_url(current))
            if r.status_code in (429, 500, 502, 503, 504):
                budget.not_before = time.time() + backoff(attempt, r.headers.get('Retry-After'), limited=r.status_code == 429)
                budget.describe_wait(budget.not_before, 'http', 'videos', current, http_status=r.status_code,
                                     attempt=attempt + 1, max_attempts=6, server_delay=bool(r.headers.get('Retry-After')))
                budget.save_cooldown(budget.not_before)
            if 300 <= r.status_code < 400:
                current = urljoin(current, r.headers.get('Location', ''))
                if r.status_code == 303 or (r.status_code in (301, 302) and method == 'POST'):
                    method, data = 'GET', None
                r.close(); continue
            response = Response(Stream(r), current, r.headers, r.status_code, r.reason)
            if r.status_code >= 400: raise HTTPError(response)
            budget.event('success', 'videos', current)
            return response
        raise TransportError('Too many video redirects')

    def download(self, url, eid):
        record = self.records.setdefault(url, {'url': url, 'status': 'not_requested', 'entries': []})
        record['entries'] = sorted(set(record.get('entries', [])) | {eid})
        if record.get('status') == 'saved' and (self.store.folder / record.get('path', '')).is_file():
            return
        if url in self.attempted or closed(record): return
        self.attempted.add(url)
        requests_before = self.client.budget.categories.get('videos', 0)
        self.trace = []
        self.step('source', 'Определяем источник видео…')
        record.update(status='pending', checked_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
        self.save()
        from .browser_bridge import BrowserLoginRequired
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
                self.stage = 'download'
                self.progress(phase='running', stage='download', progress_unit='bytes', message='Скачиваем видео',
                              completed=state.get('downloaded_bytes', 0), total=state.get('total_bytes') or state.get('total_bytes_estimate') or 0)
        options = {'quiet': True, 'no_warnings': True, 'logger': Logger(), 'noplaylist': True,
                   'cachedir': False, 'retries': 0, 'fragment_retries': 0,
                   'retry_sleep_functions': {'http': lambda attempt: self.wait(backoff(attempt)) or 0, 'fragment': lambda attempt: self.wait(backoff(attempt)) or 0}, 'extractor_retries': 0,
                   'socket_timeout': 20, 'concurrent_fragment_downloads': 1, 'fixup': 'never',
                   'ffmpeg_location': ffmpeg, 'js_runtimes': js, 'remote_components': [],
                   'outtmpl': str(work / 'video.%(ext)s'), 'overwrites': False,
                   'progress_hooks': [hook], 'merge_output_format': 'mp4',
                   'format': 'b[height<=?720][ext=mp4][protocol=https]/bv[height<=?720][ext=mp4][protocol=https]+ba[ext=m4a][protocol=https]/b[height<=?720][protocol=https]/b[protocol=http]',
                   'match_filter': lambda info, **kw: 'Прямой эфир не архивируется' if info.get('is_live') or info.get('live_status') == 'is_upcoming' else None}
        try:
            self.last_http = None
            native = lj_video_id(url)
            source, metadata = self.lj_source(url, eid) if native else (url, {})
            if native:
                options.update(format='b[height<=?720]/b', hls_prefer_native=True, fixup='force')
            with Downloader(options) as downloader:
                info = downloader.extract_info(source, download=False)
                if not info or info.get('_type') in ('playlist', 'multi_video'):
                    raise ValueError('Not a single video')
                info.update(metadata)
                record.update(title=info.get('title'), duration=info.get('duration'), provider=info.get('extractor_key'), video_id=info.get('id'))
                self.save()
                self.step('download', 'Скачиваем видео…', progress_unit='bytes')
                downloader.process_info(info)
                self.step('file', 'Сохраняем видеофайл…')
            paths = [p for p in work.glob('video.*') if p.suffix in VIDEO_SUFFIXES and '.f' not in p.name]
            if len(paths) != 1 or not paths[0].stat().st_size:
                raise ValueError('Video file was not produced')
            target = self.store.folder / 'media' / (digest + paths[0].suffix)
            target.parent.mkdir(exist_ok=True)
            paths[0].replace(target)
            record.update(status='saved', path=target.relative_to(self.store.folder).as_posix(), bytes=target.stat().st_size,
                          height=info.get('height'), format=info.get('format_id'))
            record.pop('error', None); shutil.rmtree(work)
            reopen(record)
        except VideoStop as exc:
            record.update(status='pending', error={'reason': exc.reason})
        except (Cancelled, RequestLimitReached) as exc:
            record.update(status='pending', error={'reason': 'cancelled' if isinstance(exc, Cancelled) else 'request_limit'})
        except (AuthenticationRequired, BrowserLoginRequired) as exc:
            record.update(status='pending', error={'reason': 'auth_required', 'detail': str(exc)})
        except ExportError as exc:
            from .browser_bridge import BrowserFailure
            if isinstance(exc, BrowserFailure):
                reason = 'auth_required' if exc.reason in ('browser_login', 'browser_closed', 'browser_start') else exc.reason
                record.update(status='pending' if reason == 'auth_required' else 'failed',
                              error={'reason': reason, 'http_status': exc.http_status, 'detail': str(exc)})
                if reason != 'auth_required': fail(record)
            else:
                record.update(status='failed', error={'reason': 'http' if isinstance(exc, HTTPError) else 'download',
                    'http_status': exc.status if isinstance(exc, HTTPError) else None, 'detail': str(exc)})
                fail(record)
        except OSError:
            record.update(status='pending', error={'reason': 'storage'})
        except Exception as exc:
            reason = 'http' if self.last_http and self.last_http >= 400 else 'download'
            record.update(status='failed', error={'reason': reason, 'http_status': self.last_http if reason == 'http' else None,
                                                  'type': type(exc).__name__, 'detail': re.sub(r'https?://[^\s]+', '[URL]', str(exc))[:500]})
            fail(record)
        finally:
            record['stage'] = self.stage
            record['trace'] = self.trace[-100:]
            record['requests'] = self.client.budget.categories.get('videos', 0) - requests_before
            self.save()

    def lj_source(self, url, eid):
        """Resolve old numeric IDs through the provider before matching the current player."""
        from .render import original_url
        record_id = lj_video_id(url)
        row = self.store.db.execute('SELECT payload FROM entries WHERE id=?', (eid,)).fetchone()
        if not row:
            raise ExportError('Не найдена запись для проверки доступа к видео ЖЖ.')
        entry = json.loads(row[0])
        base = original_url(self.client.user, entry)
        p = urlsplit(base)
        if p.hostname != self.client.user.replace('_', '-') + '.livejournal.com' or not re.fullmatch(r'/\d+\.html', p.path):
            raise ExportError('Не удалось определить запись с видео ЖЖ.')
        params = {'id' if record_id.isdigit() else 'uuid': int(record_id) if record_id.isdigit() else record_id,
                  'referrer': base}
        self.step('source', 'Получаем данные видео ЖЖ…')
        with self.open('https://api.vp.rambler.ru/api/v3/records/getPlayerData?' + urlencode({'params': json.dumps(params)})) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Video metadata too large')
        data = json.loads(raw)
        if not data.get('success'):
            raise ExportError('Видеосервис ЖЖ не вернул этот ролик.')
        playlist = data.get('result', {}).get('playList', {})
        if (playlist.get('type') != 'offline' or (str(playlist.get('id')) != record_id and playlist.get('uuid') != record_id)):
            raise ExportError('Видеосервис вернул другой ролик или прямой эфир.')
        identities = {value for value in (record_id, str(playlist.get('id')), playlist.get('uuid')) if value}
        from .render import text_value
        def contains_player(markup):
            refs = VideoRefs(base)
            refs.feed(text_value(markup))
            refs.close()
            return any(lj_video_id(ref) in identities for ref in refs.urls)

        pages = []
        if contains_player(entry.get('event')):
            pages.append(None)
        for comment in self.store.comments(eid):
            if contains_player(comment.get('body')):
                thread = comment.get('dtalkid') or int(comment['id']) * 256 + int(entry.get('anum') or 0)
                if str(thread).isdigit():
                    pages.append(int(thread))
        pages.append(None)
        self.step('access', 'Проверяем доступ к видео ЖЖ…')
        for thread in dict.fromkeys(pages):
            raw = self.journal_page(base, params={'thread': thread} if thread else None)
            player = LJPlayer(base, identities)
            player.feed(raw.decode('utf-8', 'replace'))
            player.close()
            if player.token:
                break
        if not player.token:
            raise ExportError('Этот видеоплеер не найден в текущей записи.')
        try:
            self.journal_page('https://www.livejournal.com/video/check',
                              params={'recordId': playlist['id'], 'token': player.token})
        except AuthenticationRequired:
            raise ExportError('ЖЖ отказал в доступе к этому видео.') from None
        finally:
            player.token = None
        source = playlist.get('directSource') or playlist.get('source')
        if not isinstance(source, str) or not source.startswith('https://'):
            raise ExportError('Видеосервис не сообщил адрес файла.')
        return source, {'title': playlist.get('title') or 'Видео ЖЖ',
                        'duration': (playlist.get('duration') or 0) / 1000,
                        'extractor_key': 'LiveJournal', 'id': record_id}

    def capture(self, entry, enabled):
        from .render import original_url, text_value
        eid, base = int(entry['itemid']), original_url(self.client.user, entry)
        refs = VideoRefs(base)
        for record in [entry, *self.store.comments(eid)]:
            refs.feed(text_value(record.get('event', record.get('body'))))
        refs.close()
        missing = [module for module in refs.modules if embed_key(base, module) not in self.embeds and embed_key(base, module) not in self.external
                   and not closed(self.records.get(embed_key(base, module)))]
        tried = bool(enabled and missing and base not in self.pages)
        stop_reason, detail = None, None
        if tried:
            self.trace = []
            requests_before = self.client.budget.used
            self.pages.add(base)
            try:
                self.resolve(entry, missing)
            except (Cancelled, RequestLimitReached, VideoStop) as exc:
                stop_reason = 'request_limit' if isinstance(exc, RequestLimitReached) else getattr(exc, 'reason', 'cancelled')
            except ExportError as exc:
                detail = str(exc)
                if '401/403' in str(exc) or 'требуется вход' in str(exc):
                    stop_reason = 'auth_required'
            except OSError:
                stop_reason = 'storage'
            except Exception as exc:
                detail = f'Ошибка обработки вставки ({type(exc).__name__})'
        for module in refs.modules:
            key = embed_key(base, module)
            if key in self.embeds: refs.urls.add(self.embeds[key])
            elif key not in self.external:
                record = self.records.setdefault(key, {'url': base, 'entries': [eid], 'status': 'not_requested'})
                if enabled and not closed(record):
                    if not tried and not self.client.budget.exhausted and not self.client.cancel.is_set():
                        continue
                    reason = stop_reason or ('request_limit' if self.client.budget.exhausted else 'cancelled' if self.client.cancel.is_set() else 'embed_unresolved')
                    record.update(status='pending' if reason != 'embed_unresolved' else 'failed', error={'reason': reason})
                    if tried:
                        record.update(checked_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                      requests=self.client.budget.used - requests_before, trace=self.trace[-8:])
                        if detail:
                            record['error']['detail'] = detail
                    if reason == 'embed_unresolved' and tried and module in missing:
                        fail(record)
        self.save()
        for url in sorted(refs.urls):
            if enabled: self.download(url, eid)
            else:
                record = self.records.setdefault(url, {'url': url, 'status': 'not_requested', 'entries': []})
                record['entries'] = sorted(set(record.get('entries', [])) | {eid})
        for module in refs.modules:
            if embed_key(base, module) in self.embeds: self.records.pop(embed_key(base, module), None)
        self.save()

    def retry_failed(self, selected=None):
        entries = {int(e['itemid']): e for e in self.store.entries()}
        for _ in range(ATTEMPTS):
            pending = {url: record for url, record in self.records.items() if record.get('status') == 'failed'
                       and not closed(record) and (selected is None or set(record.get('entries', [])) & selected)}
            if not pending or self.client.cancel.is_set() or self.client.budget.exhausted:
                return
            self.attempted.difference_update(pending)
            ids = {eid for record in pending.values() for eid in record.get('entries', [])
                   if eid in entries and (selected is None or eid in selected)}
            from .render import original_url
            for eid in sorted(ids):
                self.pages.discard(original_url(self.client.user, entries[eid]))
                self.capture(entries[eid], True)

    def close(self):
        self.session.close()


def capture_videos(exporter, entry):
    manager = getattr(exporter, '_videos', None)
    if manager is None:
        manager = exporter._videos = VideoArchive(exporter.store, exporter.client, exporter.progress)
    manager.capture(entry, getattr(exporter, 'videos', False))


def video_error_message(record):
    error = record.get('error') or {}
    message = {'request_limit': 'Достигнут лимит запросов', 'cancelled': 'Загрузка остановлена',
        'embed_unresolved': 'Адрес вставки не найден', 'download': 'Видео не скачано',
        'storage': 'Не удалось сохранить файл на диск', 'auth_required': 'Нужен вход в браузере'}.get(error.get('reason'),
        'HTTP ' + str(error.get('http_status')) if error.get('reason') == 'http' else 'Видео не скачано')
    if error.get('detail'): message += ': ' + error['detail']
    return message


def video_report(store, selected=None):
    records = store.get('videos', {})
    rows = {url: r for url, r in records.items() if selected is None or set(r.get('entries', [])) & set(selected)}
    for record in rows.values():
        if record.get('error'):
            record['error']['message'] = video_error_message(record)
    return {'total': len(rows), 'saved': sum(r['status'] == 'saved' for r in rows.values()),
            'missing': {url: r for url, r in rows.items() if r['status'] != 'saved' and not closed(r)},
            'unavailable': {url: r for url, r in rows.items() if r['status'] != 'saved' and closed(r)}}


def missing_by_entry(store):
    from collections import Counter
    counts = Counter()
    for record in store.get('videos', {}).values():
        if record['status'] in ('failed', 'pending'):
            counts.update(record.get('entries', []))
    return counts
