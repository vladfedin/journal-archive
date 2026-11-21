"""Image downloads; LJ credentials are restricted to its HTTPS image hosts."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from .storage import atomic_write
from .client import RequestBudget, RequestLimitReached, WebLoginRequired
from .web_session import AGENT, AUTH_COOKIES, cookie_header, ready as web_ready
from .retry import backoff
from .gaps import ATTEMPTS, carry, closed, fail, reopen

TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif"}
IMAGE_HOSTS = frozenset(('ic.pics.livejournal.com', 'pics.livejournal.com', 'l-userpic.livejournal.com', 'userpic.livejournal.com'))
BROWSER_AGENT = AGENT


def authenticated_image_host(url):
    try:
        parts = urlsplit(url)
        return parts.scheme == 'https' and parts.hostname in IMAGE_HOSTS and not parts.username and not parts.password and parts.port in (None, 443)
    except ValueError:
        return False


def image_url(url):
    """Old posts often embed HTTP URLs; use TLS on known LJ image hosts."""
    try:
        parts = urlsplit(url)
        if (parts.scheme == 'http' and parts.hostname in IMAGE_HOSTS and not parts.username
                and not parts.password and parts.port in (None, 80)):
            return urlunsplit(('https', parts.hostname, parts.path, parts.query, parts.fragment))
    except ValueError:
        pass  # The download records the invalid address as a per-file error.
    return url


def web_auth_host(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname == 'www.livejournal.com'
                and not parts.username and not parts.password and parts.port in (None, 443))
    except ValueError:
        return False

REASONS = {
    "timeout": "Сервер не ответил вовремя", "connection": "Ошибка соединения",
    "dns": "Не удалось определить адрес сервера",
    "transfer": "Загрузка прервалась", "invalid_url": "Недопустимый адрес",
    "unsupported_type": "Ответ не является поддерживаемым изображением",
    "too_large": "Изображение больше 20 МБ", "empty": "Пустой ответ",
    "redirects": "Слишком много перенаправлений", "storage": "Ошибка сохранения файла",
    "tls": "Не удалось проверить сертификат сервера",
    "request_limit": "Достигнут лимит запросов", "cancelled": "Загрузка остановлена",
    "paused": "Не запрошено: загрузка изображений приостановлена после повторных ошибок",
    "host_paused": "Не запрошено: этот сервер изображений приостановлен после повторных ошибок",
    "unknown": "Причина не сохранена; требуется повторная загрузка",
    'web_login': 'ЖЖ не подтвердил вход для изображений. Повторите загрузку с паролем.',
}


def settle_browser_answer(url, error):
    """A final answer for the requested image, rather than an SSO intermediate."""
    if error.get('reason') != 'browser_http' or not error.get('attempts'):
        return error
    from .browser_bridge import safe_url
    trace = error.get('trace') or []
    code = error.get('http_status')
    if (error.get('session_type') == 'browser'
            and code in (403, 404, 410) and trace and trace[-1].get('http_status') == code
            and trace[-1].get('url') == safe_url(image_url(url))):
        error.update(availability='unavailable', closed=True, closed_at=error.get('closed_at') or error.get('updated_at'))
    return error


def plain_reason(error):
    """Short reason for people: what happened to the file, without protocol codes."""
    reason, code = error.get("reason"), error.get("http_status")
    if reason == 'web_login':
        return f'HTTP {code} при загрузке через HTTP' if code else 'запрос не отправлен после ошибки входа'
    if reason and reason.startswith('browser_'):
        return error_message(error)
    if reason == "http":
        if code == 404:
            return "файл не найден"
        if code == 410:
            return "файл больше недоступен"
        if code in (401, 403):
            return "нужен вход в ЖЖ" if error.get("authentication") == "missing" or error.get('session_type') in ('api', 'none') else "доступ к файлу закрыт"
        return "ошибка сервера"
    return {"timeout": "сервер не отвечал", "connection": "сервер недоступен", "dns": "сервер недоступен",
            "transfer": "загрузка обрывалась", "unsupported_type": "по ссылке не картинка",
            "too_large": "больше 20 МБ", "empty": "пустой файл", "redirects": "ссылка никуда не ведёт",
            "invalid_url": "недопустимый адрес", "storage": "не удалось записать на диск",
            'tls': 'не удалось проверить сертификат', 'request_limit': 'достигнут лимит запросов',
            'cancelled': 'загрузка остановлена', 'host_paused': 'сервер временно недоступен',
            'paused': 'загрузка приостановлена', 'web_login': 'нужен веб-вход в ЖЖ'}.get(reason, "причина не сохранена")


def error_message(error):
    if (error.get('reason') or '').startswith('browser_'):
        from .browser_bridge import BrowserFailure
        return str(BrowserFailure(error['reason'], http_status=error.get('http_status')))
    if error.get('reason') == 'web_login':
        code = error.get('http_status')
        return f'HTTP {code} · вход через HTTP не завершён' if code else 'Запрос файла не отправлен: вход через HTTP не завершён'
    if error.get("reason") == "http":
        code = error.get("http_status")
        if code in (401, 403) and (error.get('authentication') == 'missing' or error.get('session_type') in ('api', 'none')):
            return f"HTTP {code} — требуется вход в ЖЖ"
        detail = {401: "требуется авторизация", 403: "доступ запрещён", 404: "файл не найден",
                  429: "слишком много запросов"}.get(code, "ошибка сервера")
        return f"HTTP {code} — {detail}"
    return REASONS.get(error.get("reason"), REASONS["unknown"])


def public_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("unsupported image URL")
    if parts.port not in (None, 80, 443):
        raise ValueError("nonstandard port")
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("non-public address")


class Media:
    def __init__(self, folder: Path, enabled: bool, cancel: threading.Event, budget=None, client=None):
        self.folder, self.enabled, self.cancel = folder, enabled, cancel
        self.mapping, self.errors = {}, {}
        self.budget = budget or RequestBudget()
        self.client = client
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': BROWSER_AGENT, 'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8', 'Accept-Language': 'ru,en;q=0.8'})
        # Never attach credentials from .netrc; LJ cookies are set per request below.
        self.session.trust_env = False
        self.stopped = False
        self.stop_reason = "paused"
        self.paused_origins = set()
        self.attempted = set()
        self.authentication = {}
        self.session_types = {}
        self.categories = {}
        self.referers = {}
        self.auth_unavailable = False
        self.verified_files = set()
        self.used_urls = {"images": set(), "userpics": set()}
        self.videos, self.video_embeds, self.external_embeds = {}, {}, {}
        self.traces = {}
        self.downloads = {}
        self.browser_only = False
        self.browser_outcomes = {}
        self.browser_counts = Counter()
        self.last_browser_result = None

    def headers(self, url, referer=None):
        headers = {}
        if authenticated_image_host(url) and self.client:
            origin = f"https://{self.client.user.replace('_', '-')}.livejournal.com/"
            headers['Referer'] = referer if referer and referer.startswith(origin) else origin
            if getattr(self.client, '_cookie', None):
                # Keep domain/path restrictions on web cookies issued by LJ itself.
                value = cookie_header(self.client, url)
                if not any(part.strip().startswith('ljsession=') for part in value.split(';')):
                    value += ('; ' if value else '') + 'ljsession=' + self.client._cookie
                headers['Cookie'] = value
        return headers

    def load_state(self, store):
        self.downloads = {**store.get('media_downloads', {}), **self.downloads}
        self.videos, self.video_embeds = store.get('videos', {}), store.get('video_embeds', {})
        self.external_embeds = store.get('external_embeds', {})
        self.mapping = {**store.get("media", {}), **self.mapping}
        self.errors = {**store.get("media_errors", {}), **self.errors}
        self.errors = {url: error for url, error in self.errors.items() if not self.mapping.get(url)}
        for url, error in self.errors.items():
            settle_browser_answer(url, error)
            if error.get('reason') in ('storage', 'tls', 'web_login') or (error.get('reason') == 'redirects' and authenticated_image_host(image_url(url))) or (error.get('http_status') in (401, 403) and
                    (error.get('authentication') == 'missing' or (authenticated_image_host(url) and error.get('session_type') not in ('web', 'browser')))):
                reopen(error)

    def save_state(self, store):
        store.set('media_downloads', {**store.get('media_downloads', {}), **self.downloads})
        mapping = {**store.get("media", {}), **self.mapping}
        errors = {**store.get("media_errors", {}), **self.errors}
        store.set("media", mapping)
        store.set("media_errors", {url: error for url, error in errors.items() if not mapping.get(url)})

    def record_error(self, url, reason, *, status="failed", http_status=None, requests_made=0, checked_at=None):
        previous = self.errors.get(url) or {}
        record = {"status": status, "reason": reason, "http_status": http_status,
                  "requests": requests_made, "checked_at": checked_at,
                  "authentication": self.authentication.get(url, 'not_requested'),
                  'session_type': self.session_types.get(url, 'none'),
                  "category": self.categories.get(url, previous.get("category", "images")),
                  "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if url in self.traces:
            record['trace'] = self.traces[url]
        # A stop, missing login or local failure does not count as remote absence.
        if reason in ('storage', 'tls', 'web_login') or (http_status in (401, 403) and
                (record['authentication'] == 'missing' or (authenticated_image_host(url) and record['session_type'] not in ('web', 'browser')))):
            record['status'] = 'pending'
        self.errors[url] = fail(record, previous) if record['status'] == "failed" else carry(record, previous)

    def retry_failed(self, urls=None, checkpoint=None):
        """At the end of a run, repeat failed downloads until they are saved or closed."""
        for _ in range(ATTEMPTS):
            pending = [url for url, error in self.errors.items() if error.get("status") == "failed"
                       and not closed(error) and (urls is None or url in urls)]
            for index, url in enumerate(pending):
                if self.stopped or self.cancel.is_set():
                    return
                category = self.errors[url].get("category", "images")
                self.budget.progress(phase="materials", message="Повторяем недополученные изображения",
                                     completed=index, total=len(pending))
                self.attempted.discard(url)
                self.mapping.pop(url, None)
                self.get(url, required=category == "userpics", category=category)
                if checkpoint:
                    checkpoint()

    def wait(self, seconds):
        # Short interruptible waits keep cancellation responsive even for long Retry-After.
        while seconds > 0:
            portion = min(30, seconds)
            if self.cancel.wait(portion):
                return False
            seconds -= portion
        return not self.cancel.is_set()

    def defer(self, seconds, **details):
        self.budget.not_before = max(self.budget.not_before, time.time() + seconds)
        self.budget.describe_wait(self.budget.not_before, **details)
        self.budget.save_cooldown(self.budget.not_before)
        self.budget.announce_wait()

    def browser_get(self, url, referer, category, *, retry_login=True):
        from .browser_bridge import BrowserFailure, BrowserLoginRequired, safe_url
        from .client import Cancelled
        browser = self.client.browser
        before = self.budget.used
        def publish(result=None):
            if result:
                previous = self.browser_outcomes.get(url)
                if previous:
                    self.browser_counts[previous] -= 1
                self.browser_outcomes[url] = result
                self.browser_counts[result] += 1
                self.last_browser_result = {'url': safe_url(url), 'outcome': result,
                                            'http_status': self.errors.get(url, {}).get('http_status')}
            self.budget.progress(browser_images_progress={
                'current': safe_url(url), 'last': self.last_browser_result, **self.browser_counts})
        publish()
        self.authentication[url], self.session_types[url] = 'browser_managed', 'browser'
        try:
            body, ext, trace = browser.image(self.client.account, image_url(url), referer,
                                             self.budget, self.cancel, category)
            path = 'media/' + hashlib.sha256(url.encode()).hexdigest() + ext
            atomic_write(self.folder / path, body)
            self.mapping[url] = path
            self.errors.pop(url, None)
            self.traces[url] = trace
            self.downloads[url] = {'path': path, 'bytes': len(body), 'transport': 'browser', 'trace': trace,
                                   'saved_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            publish('saved')
            self.budget.event('success', category, url, transport='browser', bytes=len(body))
            return path
        except (BrowserFailure, BrowserLoginRequired) as exc:
            request_browser = getattr(self.client, 'request_browser', None)
            if isinstance(exc, BrowserLoginRequired):
                if not request_browser:
                    raise  # Explicit downloads use the queue's confirmation flow.
                exc = BrowserFailure('browser_login')
            self.traces[url] = exc.trace
            # A resource can deny access even with a working browser session.
            # Count that answer; only an actual session failure needs another login.
            pending = exc.reason in ('browser_closed', 'browser_start', 'browser_profile', 'browser_login', 'browser_missing')
            self.record_error(url, exc.reason, status='pending' if pending else 'failed', http_status=exc.http_status,
                              requests_made=self.budget.used - before)
            record = settle_browser_answer(url, self.errors[url])
            publish('unavailable' if closed(record) else 'pending')
            self.budget.event('failure', category, url, reason=exc.reason, http_status=exc.http_status,
                              transport='browser', request_url=safe_url(url))
            if pending and request_browser:
                if retry_login:
                    request_browser(force=True)
                    return self.browser_get(url, referer, category, retry_login=False)
                self.stopped, self.stop_reason = True, exc.reason
        except (Cancelled, RequestLimitReached) as exc:
            reason = 'cancelled' if isinstance(exc, Cancelled) else 'request_limit'
            self.traces[url] = getattr(exc, 'trace', [])
            self.record_error(url, reason, status='pending', http_status=getattr(exc, 'http_status', None),
                              requests_made=self.budget.used - before)
            publish('pending')
            self.stopped, self.stop_reason = True, reason
        except OSError:
            self.record_error(url, 'storage', status='pending', requests_made=self.budget.used - before)
            publish('pending')
            self.stopped, self.stop_reason = True, 'storage'
        return None

    def get(self, url: str, *, required=False, category="images", referer=None) -> str | None:
        self.used_urls[category].add(url)
        self.categories.setdefault(url, category)
        if referer:
            self.referers[url] = referer
        referer = self.referers.get(url)
        if self.mapping.get(url) and url not in self.verified_files:
            if not (self.folder / self.mapping[url]).is_file():
                self.mapping[url] = None
            self.verified_files.add(url)
        if url in self.mapping and (self.mapping[url] is not None or url in self.attempted):
            return self.mapping[url]
        digest = hashlib.sha256(url.encode()).hexdigest()
        existing = next((self.folder / "media").glob(digest + ".*"), None)
        if existing and existing.suffix in TYPES.values():
            self.mapping[url] = "media/" + existing.name
            self.errors.pop(url, None)
            return self.mapping[url]
        self.mapping[url] = None
        if not (self.enabled and category == "images") and not required:
            return None
        if closed(self.errors.get(url)):
            # Asked for and not given several times: the archive keeps the original link.
            return None
        if self.stopped or self.cancel.is_set():
            if url not in self.errors:
                self.record_error(url, "cancelled" if self.cancel.is_set() else self.stop_reason, status="pending")
            return None
        self.attempted.add(url)
        requests_made, http_status, checked_at = 0, None, None
        reason = "invalid_url"
        current = url
        web_retry = False
        trusted_source = authenticated_image_host(image_url(url))
        browser = getattr(self.client, 'browser', None)
        if trusted_source and browser and (self.browser_only or browser.ready(self.client.account)):
            return self.browser_get(url, referer, category)
        self.traces[url] = []
        visited = set()
        try:
            current = image_url(url)
            if self.auth_unavailable and trusted_source and self.errors.get(url, {}).get('reason') in ('redirects', 'web_login'):
                raise WebLoginRequired()
            attempt = int(self.errors.get(url, {}).get('attempts', 0))
            # Redirects belong to this attempt. Failed downloads are retried by the
            # final pass, not by a nested six-attempt loop.
            for redirect in range(6):
                reason = "invalid_url"
                parts = urlsplit(current)
                origin = (parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
                if origin in self.paused_origins:
                    self.record_error(url, "host_paused", status="pending", requests_made=requests_made, checked_at=checked_at)
                    return None
                public_url(current)
                headers = self.headers(current, referer)
                # Photo authorization can return through the main LJ domain. It needs
                # the server-issued www cookies, never the API master token.
                if trusted_source and web_auth_host(current) and self.client:
                    if not cookie_header(self.client, current) or not web_ready(self.client):
                        web_retry = True
                        if not self.client.ensure_web_session():
                            raise WebLoginRequired()
                    value = cookie_header(self.client, current)
                    if value: headers['Cookie'] = value
                fingerprint = (current, headers.get('Cookie', ''))
                if fingerprint in visited:
                    if trusted_source:
                        raise WebLoginRequired()
                    reason = 'redirects'
                    raise ValueError('redirect loop')
                visited.add(fingerprint)
                if self.budget.limit is not None and self.budget.used >= self.budget.limit:
                    self.budget.consume(category)
                self.budget.announce_wait()
                if not self.wait(max(self.budget.delay, self.budget.not_before - time.time())):
                    self.record_error(url, "cancelled", status="pending", requests_made=requests_made, checked_at=checked_at)
                    return None
                self.budget.consume(category)
                from .browser_bridge import safe_url
                self.traces[url].append({'url': safe_url(current), 'transport': 'http',
                                         'http_status': None, 'at': int(time.time())})
                self.budget.progress(phase='materials', message='Получаем изображение…')
                self.budget.event("request", category, current, attempt=attempt + 1, max_attempts=ATTEMPTS,
                                  method='GET', request_url=safe_url(current))
                requests_made += 1
                if attempt:
                    self.budget.progress(phase="materials", message=f"Загрузка изображения · попытка {attempt + 1} из {ATTEMPTS}")
                checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                http_status = None
                try:
                    # Recompute on every redirect; never forward a cookie to a different service.
                    self.session.cookies.clear()
                    if authenticated_image_host(current):
                        self.authentication[url] = 'sent' if headers.get('Cookie') else 'missing'
                        self.session_types[url] = 'web' if self.client and web_ready(self.client, current) else 'api' if headers.get('Cookie') else 'none'
                    elif not trusted_source:
                        self.authentication[url], self.session_types[url] = 'not_applicable', 'none'
                    response = self.session.get(current, headers=headers, stream=True, timeout=(10, 20), allow_redirects=False)
                except (requests.Timeout, requests.ConnectionError):
                    raise
                with response:
                    http_status = response.status_code
                    self.traces[url][-1]['http_status'] = http_status
                    self.budget.event('response', category, current, http_status=http_status)
                    if self.client and trusted_source and (authenticated_image_host(current) or web_auth_host(current)):
                        # Keep new photo-domain cookies through the redirect chain;
                        # clear the transport jar before each request to other hosts.
                        for cookie in getattr(response, 'cookies', ()):
                            domain = cookie.domain.lstrip('.')
                            if (cookie.name in AUTH_COOKIES and domain in {'livejournal.com', 'www.livejournal.com', *IMAGE_HOSTS}
                                    and (parts.hostname == domain or parts.hostname.endswith('.' + domain))):
                                from copy import copy
                                self.client.session.cookies.set_cookie(copy(cookie))
                        if getattr(self.client, 'session_ready', None):
                            self.client.session_ready(self.client)
                    if (http_status in (401, 403) and authenticated_image_host(current) and self.client and not web_retry
                            and self.session_types[url] != 'web' and hasattr(self.client, 'ensure_web_session')):
                        web_retry = True
                        if self.client.ensure_web_session():
                            visited.clear()
                            continue
                    if response.status_code in (429, 500, 502, 503, 504):
                        self.defer(backoff(attempt, response.headers.get("Retry-After"), limited=response.status_code == 429),
                                   reason="http", category=category, url=current, http_status=response.status_code,
                                   attempt=attempt + 1, max_attempts=ATTEMPTS, server_delay=bool(response.headers.get("Retry-After")))
                        response.raise_for_status()
                    if 300 <= response.status_code < 400:
                        target = image_url(urljoin(current, response.headers.get("Location", "")))
                        self.budget.event('redirect', category, current, http_status=http_status,
                                          destination='journal_login' if web_auth_host(target) else 'journal_image' if authenticated_image_host(target) else 'external')
                        current = target
                        continue
                    response.raise_for_status()
                    ext = TYPES.get(response.headers.get("Content-Type", "").split(";")[0].lower())
                    if not ext:
                        reason = "unsupported_type"
                        raise ValueError("not a supported raster image")
                    chunks, size = [], 0
                    try:
                        for chunk in response.iter_content(65536):
                            if self.cancel.is_set():
                                self.record_error(url, "cancelled", status="pending", http_status=http_status,
                                                  requests_made=requests_made, checked_at=checked_at)
                                return None
                            size += len(chunk)
                            if size > 20 * 1024 * 1024:
                                reason = "too_large"
                                raise ValueError("image over 20 MB")
                            chunks.append(chunk)
                    except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
                        raise
                    if not size:
                        reason = "empty"
                        raise ValueError("empty image")
                    path = "media/" + digest + ext
                    reason = "storage"
                    atomic_write(self.folder / path, b"".join(chunks))
                    self.mapping[url] = path
                    self.errors.pop(url, None)
                    self.budget.event("success", category, current, http_status=http_status, bytes=size)
                    return path
            if trusted_source and any(web_auth_host(address) for address, _ in visited):
                raise WebLoginRequired()
            reason = "redirects"
            raise ValueError("too many redirects")
        except WebLoginRequired:
            self.record_error(url, 'web_login', status='pending', http_status=http_status,
                              requests_made=requests_made, checked_at=checked_at)
            self.budget.event('failure', category, url, reason='web_login', http_status=http_status)
            request_browser = getattr(self.client, 'request_browser', None)
            if request_browser:
                request_browser()
                return self.browser_get(url, referer, category)
            self.auth_unavailable = True
            return None
        except RequestLimitReached:
            self.stopped = True
            self.stop_reason = "request_limit"
            self.record_error(url, "request_limit", status="pending", http_status=http_status,
                              requests_made=requests_made, checked_at=checked_at)
            return None
        except (requests.RequestException, ValueError, OSError) as exc:
            if isinstance(exc, requests.HTTPError):
                reason = "http"
            elif isinstance(exc, requests.exceptions.SSLError):
                reason = 'tls'
            elif isinstance(exc, requests.Timeout):
                reason = "timeout"
            elif isinstance(exc, requests.ConnectionError):
                reason = "connection"
            elif isinstance(exc, requests.RequestException):
                reason = "transfer"
            elif isinstance(exc, socket.gaierror):
                reason = "dns"
            self.record_error(url, reason, http_status=http_status, requests_made=requests_made, checked_at=checked_at)
            self.budget.event('failure', category, '' if reason == 'invalid_url' else current,
                              reason=reason, http_status=http_status)
            if reason == 'storage':
                self.stopped, self.stop_reason = True, 'storage'
            if closed(self.errors[url]) and (reason in ('timeout', 'connection', 'transfer', 'dns') or http_status in (429, 500, 502, 503, 504)):
                self.paused_origins.add(origin)
            return None

    def close(self):
        self.session.cookies.clear()
        self.session.close()
