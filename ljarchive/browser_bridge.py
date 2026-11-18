"""Retrieve LJ images in a separate, interactive Chrome/Edge session.

The browser manages site data in a dedicated local profile per LJ account.
"""
from __future__ import annotations

import base64
from collections import deque
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect
from websockets.protocol import State
from websockets.exceptions import WebSocketException

from .client import Cancelled, ExportError, RequestLimitReached

MAX_IMAGE = 20 * 1024 * 1024


def safe_url(url):
    """Request evidence must not preserve SSO tokens in redirect queries."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.hostname or '', parts.path, '', ''))
    except ValueError:
        return ''


class BrowserLoginRequired(ExportError):
    def __init__(self):
        super().__init__('Проверьте вход в открытом окне ЖЖ и нажмите «Продолжить».')


class BrowserFailure(ExportError):
    def __init__(self, reason, trace=(), http_status=None):
        self.reason, self.trace, self.http_status = reason, list(trace), http_status
        message = {
            'browser_missing': 'Для загрузки через браузер установите Chrome или Edge.',
            'browser_start': 'Не удалось запустить Chrome или Edge.',
            'browser_profile': 'Не удалось открыть профиль браузера. Проверьте доступ к папке и закройте другие экземпляры Архива ЖЖ.',
            'browser_closed': 'Окно браузера закрыто. Откройте его ещё раз.',
            'browser_timeout': 'Браузер не завершил запрос за 45 секунд.',
            'browser_network': 'Браузер сообщил об ошибке соединения.',
            'browser_dns': 'Не удалось определить адрес сервера изображения.',
            'browser_login': 'Браузер получил страницу входа вместо изображения.',
            'browser_type': 'Браузер получил ответ другого типа вместо изображения.',
            'browser_redirect': 'Браузер вернулся к уже запрошенному адресу.',
            'browser_large': 'Изображение превышает 20 МБ.',
            'browser_empty': 'Браузер получил пустой файл.',
            'browser_http': f'Браузер получил HTTP {http_status}.',
            'browser_address': 'Перенаправление на неподдерживаемый адрес остановлено.',
        }.get(reason, 'Ошибка получения файла через браузер.')
        super().__init__(message)


def browser_executable():
    candidates = []
    if os.name == 'nt':
        for root in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            if os.environ.get(root):
                for suffix in ('Microsoft/Edge/Application/msedge.exe', 'Google/Chrome/Application/chrome.exe'):
                    candidates.append(Path(os.environ[root]) / suffix)
    else:
        candidates.extend(Path(p) for p in (
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        ))
        for name in ('google-chrome', 'google-chrome-stable', 'microsoft-edge', 'chromium', 'chromium-browser'):
            path = shutil.which(name)
            if path:
                candidates.append(Path(path))
    return next((p for p in candidates if p.is_file()), None)


def debugging_endpoint(path):
    """Chromium may still hold its port file exclusively while starting on Windows."""
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
        if (len(lines) >= 2 and 1 <= int(lines[0]) <= 65535
                and re.fullmatch(r'/devtools/browser/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', lines[1])):
            return f'ws://127.0.0.1:{int(lines[0])}{lines[1]}'
    except (OSError, UnicodeError, ValueError):
        pass
    return None


class BrowserBridge:
    def __init__(self, account, profile_dir, *, executable=None, headless=False, validate_url=None,
                 login_url='https://www.livejournal.com/login.bml'):
        # Overrides are for local integration tests, never accepted from HTTP.
        self.executable, self.headless, self.validate_url = executable, headless, validate_url
        self.login_url = login_url
        self.account, self.profile_dir = account, Path(profile_dir)
        self.process = self.socket = self.profile_lock = None
        self.target, self.confirmed = None, False
        self.serial = 0
        self.events = deque()
        self.last_request = 0
        self.lock = threading.RLock()

    def _send(self, method, params=None, session=None):
        self.serial += 1
        packet = {'id': self.serial, 'method': method, 'params': params or {}}
        if session:
            packet['sessionId'] = session
        self.socket.send(json.dumps(packet))
        return self.serial

    def _call(self, method, params=None, session=None, timeout=5):
        try:
            wanted = self._send(method, params, session)
            deadline = time.monotonic() + timeout
            while True:
                packet = json.loads(self.socket.recv(timeout=max(.01, deadline - time.monotonic())))
                if packet.get('id') == wanted:
                    if 'error' in packet:
                        raise BrowserFailure('browser_closed')
                    return packet.get('result', {})
                if 'method' in packet:
                    self.events.append(packet)
                if time.monotonic() >= deadline:
                    raise BrowserFailure('browser_closed')
        except BrowserFailure:
            raise
        except Exception as exc:
            raise BrowserFailure('browser_closed') from exc

    def _launch(self):
        if self.process and self.process.poll() is None and self.socket and self.socket.state == State.OPEN:
            return
        self.close()
        executable = self.executable or browser_executable()
        if not executable:
            raise BrowserFailure('browser_missing')
        from .storage import exclusive_lock
        try:
            self.profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.profile_lock = exclusive_lock(self.profile_dir / 'archive-browser.lock')
        except OSError as exc:
            raise BrowserFailure('browser_profile') from exc
        args = [str(executable), '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                '--user-data-dir=' + str(self.profile_dir), '--no-first-run', '--no-default-browser-check',
                '--disable-background-networking', '--disable-sync', '--no-startup-window']
        if self.headless:
            args.insert(1, '--headless=new')
        try:
            port_file = self.profile_dir / 'DevToolsActivePort'
            # A previous process may have left this file behind.
            port_file.unlink(missing_ok=True)
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
            # A cold Windows browser can take longer than subsequent launches.
            started = time.monotonic()
            deadline = started + 30
            endpoint_seen = False
            while time.monotonic() < deadline and self.process.poll() is None:
                endpoint = debugging_endpoint(port_file)
                if endpoint:
                    endpoint_seen = True
                    try:
                        self.socket = connect(endpoint, proxy=None, max_size=32 * 1024 * 1024,
                                              open_timeout=max(.01, min(2, deadline - time.monotonic())))
                        return
                    except (OSError, WebSocketException, TimeoutError):
                        pass  # The file can precede the listening debugging endpoint.
                time.sleep(.1)
        except Exception as exc:
            self.close()
            raise BrowserFailure('browser_start') from exc
        elapsed, returncode = time.monotonic() - started, self.process.poll()
        self.close()
        raise BrowserFailure('browser_start') from RuntimeError(
            f'Browser startup: {elapsed:.1f}s; exit={returncode}; endpoint_seen={endpoint_seen}')

    def ready(self, account):
        return bool(account == self.account and self.confirmed and self.process and self.process.poll() is None)

    def open_login(self, account):
        if account != self.account:
            raise BrowserFailure('browser_profile')
        with self.lock:
            self._launch()
            self.confirmed = False
            if self.target:
                try:
                    self._call('Target.activateTarget', {'targetId': self.target})
                    return
                except BrowserFailure:
                    self.target = None
            self.target = self._call('Target.createTarget', {
                'url': self.login_url, 'newWindow': True,
            })['targetId']
            for tab in self._call('Target.getTargets').get('targetInfos', []):
                if tab.get('type') == 'page' and tab.get('url') == 'about:blank' and tab['targetId'] != self.target:
                    self._call('Target.closeTarget', {'targetId': tab['targetId']})

    def confirm(self, account):
        with self.lock:
            if account != self.account or not self.process or self.process.poll() is not None or not self.target:
                self.open_login(account)
                raise BrowserLoginRequired()
            self.confirmed = True

    def image(self, account, url, referer, budget, cancel, category='images'):
        from .media import TYPES
        return self._document(account, url, referer, budget, cancel, category, TYPES)

    def document(self, account, url, referer, budget, cancel, category='videos'):
        """Read an authenticated page without executing its scripts or subrequests."""
        body, _, trace = self._document(account, url, referer, budget, cancel, category,
                                       {'text/html': '', 'application/json': '', 'text/plain': ''}, allow_empty=True)
        return body, trace

    def _document(self, account, url, referer, budget, cancel, category, types, *, allow_empty=False):
        with self.lock:
            if not self.ready(account):
                self.open_login(account)
                raise BrowserLoginRequired()
            from .media import public_url
            validate = self.validate_url or public_url
            try:
                validate(url)
            except socket.gaierror as exc:
                raise BrowserFailure('browser_dns') from exc
            except (ValueError, OSError) as exc:
                raise BrowserFailure('browser_address') from exc
            target = self._call('Target.createTarget', {
                'url': 'about:blank', 'background': True,
            })['targetId']
            trace, status, session = [], None, None
            try:
                session = self._call('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
                # The login window remains interactive. A download target only reads
                # an image: unexpected HTML must not start scripts, workers or sockets
                # outside Fetch interception and the request budget.
                self._call('Emulation.setScriptExecutionDisabled', {'value': True}, session)
                self._call('Network.enable', {'maxTotalBufferSize': 24 * 1024 * 1024,
                                              'maxResourceBufferSize': 22 * 1024 * 1024}, session)
                self._call('Network.setCacheDisabled', {'cacheDisabled': True}, session)
                self._call('Network.setBypassServiceWorker', {'bypass': True}, session)
                if allow_empty:
                    # Chrome's tab icon can start after document completion, as the
                    # interception session closes. Document reads never need this icon.
                    self._call('Network.setBlockedURLs', {'urls': ['*/favicon.ico']}, session)
                self._call('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]}, session)
                self._send('Page.navigate', {'url': url, 'referrer': referer or ''}, session)
                deadline, response_id, mime, visits = time.monotonic() + 45, None, '', {}
                while time.monotonic() < deadline:
                    if cancel.is_set():
                        raise Cancelled('Загрузка остановлена.')
                    try:
                        event = self.events.popleft() if self.events else json.loads(self.socket.recv(timeout=.2))
                    except TimeoutError:
                        continue
                    if event.get('sessionId') != session:
                        continue
                    method, p = event.get('method'), event.get('params', {})
                    if method == 'Fetch.requestPaused':
                        address, request_id = p['request']['url'], p['requestId']
                        # A direct image load needs only its document and redirects.
                        # Favicon, scripts and login-page subresources are not downloads.
                        if p.get('resourceType') != 'Document':
                            self._send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Aborted'}, session)
                            continue
                        try:
                            validate(address)
                        except socket.gaierror as exc:
                            raise BrowserFailure('browser_dns', trace, status) from exc
                        except (ValueError, OSError) as exc:
                            raise BrowserFailure('browser_address', trace, status) from exc
                        visits[address] = visits.get(address, 0) + 1
                        if visits[address] > 2 or len(trace) >= 8:
                            raise BrowserFailure('browser_redirect', trace, status)
                        wait = max(0, self.last_request + budget.delay - time.monotonic(), budget.not_before - time.time())
                        if wait:
                            message = 'ЖЖ запросил паузу перед следующим запросом' if budget.not_before > time.time() else 'Пауза между запросами'
                            budget.progress(phase='waiting', message=message, retry_at=time.time() + wait)
                            deadline += wait
                            if cancel.wait(wait):
                                raise Cancelled('Загрузка остановлена.')
                        if time.monotonic() >= deadline:
                            break
                        budget.consume(category)
                        trace.append({'url': safe_url(address), 'transport': 'browser', 'http_status': None,
                                      'at': int(time.time())})
                        budget.progress(phase='running', message='Браузер: ' + safe_url(address))
                        budget.event('request', category, address, transport='browser', request_url=safe_url(address))
                        self._send('Fetch.continueRequest', {'requestId': request_id}, session)
                        self.last_request = time.monotonic()
                    elif method == 'Network.requestWillBeSent' and p.get('redirectResponse'):
                        redirect = p['redirectResponse']
                        status = int(redirect['status'])
                        for item in reversed(trace):
                            if item['url'] == safe_url(redirect['url']):
                                item['http_status'] = status
                                break
                    elif method == 'Network.responseReceived' and p.get('type') == 'Document':
                        response_id, response = p['requestId'], p['response']
                        status, mime = int(response['status']), response.get('mimeType', '').lower()
                        if trace:
                            trace[-1].update(http_status=status, content_type=mime)
                        budget.event('response', category, url, http_status=status, transport='browser')
                        if status in (429, 503):
                            from .retry import backoff
                            retry_after = next((v for k, v in response.get('headers', {}).items() if k.lower() == 'retry-after'), None)
                            if retry_after is not None or status == 429:
                                budget.not_before = max(budget.not_before, time.time() + backoff(0, retry_after, limited=status == 429))
                                budget.describe_wait(budget.not_before, 'http', category, url, http_status=status,
                                                     server_delay=bool(retry_after))
                                budget.save_cooldown(budget.not_before)
                        if status >= 400:
                            raise BrowserFailure('browser_http', trace, status)
                        login = mime == 'text/html' and urlsplit(response['url']).path in ('/login.bml', '/login', '/login/onclick.bml')
                        if mime not in types or login:
                            raise BrowserFailure('browser_login' if login else 'browser_type', trace, status)
                        length = next((v for k, v in response.get('headers', {}).items() if k.lower() == 'content-length'), '')
                        if str(length).isdigit() and int(length) > MAX_IMAGE:
                            raise BrowserFailure('browser_large', trace, status)
                    elif method == 'Network.dataReceived' and p.get('requestId') == response_id:
                        if trace:
                            trace[-1]['bytes'] = trace[-1].get('bytes', 0) + p.get('dataLength', 0)
                            if trace[-1]['bytes'] > MAX_IMAGE:
                                raise BrowserFailure('browser_large', trace, status)
                    elif method == 'Network.loadingFinished' and p.get('requestId') == response_id:
                        result = self._call('Network.getResponseBody', {'requestId': response_id}, session)
                        body = base64.b64decode(result['body']) if result.get('base64Encoded') else result['body'].encode()
                        if not body and not allow_empty:
                            raise BrowserFailure('browser_empty', trace, status)
                        if len(body) > MAX_IMAGE:
                            raise BrowserFailure('browser_large', trace, status)
                        return body, types[mime], trace
                    elif method == 'Network.loadingFailed' and p.get('type') == 'Document':
                        if trace:
                            # Only Chromium's error code, never an arbitrary server string.
                            code = p.get('errorText', '')
                            trace[-1]['network_error'] = code if code.startswith('net::ERR_') and len(code) < 100 else 'network_error'
                        raise BrowserFailure('browser_network', trace, status)
                raise BrowserFailure('browser_timeout', trace, status)
            except (Cancelled, RequestLimitReached) as exc:
                exc.trace, exc.http_status = trace, status
                raise
            except BrowserFailure:
                raise
            except Exception as exc:
                raise BrowserFailure('browser_closed', trace, status) from exc
            finally:
                try:
                    self._call('Target.closeTarget', {'targetId': target})
                except BrowserFailure:
                    pass
                self.events.clear()

    def close(self):
        with self.lock:
            if self.socket:
                try:
                    self._call('Browser.close', timeout=2)
                except BrowserFailure:
                    pass
                self.socket.close()
            if self.process:
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=3)
            if self.profile_lock:
                self.profile_lock.close()
            self.socket = self.process = self.profile_lock = None
            self.target, self.confirmed = None, False
            self.events.clear()
