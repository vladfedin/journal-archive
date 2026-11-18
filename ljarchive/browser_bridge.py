"""Retrieve LJ images in a separate, interactive Chrome/Edge session.

Authentication stays in an off-the-record browser context. No cookies are
exported, and the user's normal browser profile is never opened or copied.
"""
from __future__ import annotations

import base64
from collections import deque
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect
from websockets.protocol import State

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
        super().__init__('Войдите в ЖЖ в открытом окне, затем нажмите «Продолжить».')


class BrowserFailure(ExportError):
    def __init__(self, reason, trace=(), http_status=None):
        self.reason, self.trace, self.http_status = reason, list(trace), http_status
        message = {
            'browser_missing': 'Для загрузки через браузер установите Chrome или Edge.',
            'browser_start': 'Не удалось запустить Chrome или Edge.',
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


class BrowserBridge:
    def __init__(self, *, executable=None, headless=False, validate_url=None,
                 login_url='https://www.livejournal.com/login.bml'):
        # Overrides are for local integration tests, never accepted from HTTP.
        self.executable, self.headless, self.validate_url = executable, headless, validate_url
        self.login_url = login_url
        self.process = self.socket = self.profile = None
        self.contexts = {}
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
        self.profile = tempfile.TemporaryDirectory(prefix='journal-archive-browser-', ignore_cleanup_errors=True)
        args = [str(executable), '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                '--user-data-dir=' + self.profile.name, '--no-first-run', '--no-default-browser-check',
                '--disable-background-networking', '--disable-sync', '--no-startup-window']
        if self.headless:
            args.insert(1, '--headless=new')
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
            port_file = Path(self.profile.name) / 'DevToolsActivePort'
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and self.process.poll() is None:
                if port_file.is_file():
                    lines = port_file.read_text().splitlines()
                    if len(lines) >= 2:
                        self.socket = connect(f'ws://127.0.0.1:{int(lines[0])}{lines[1]}',
                                              proxy=None, max_size=32 * 1024 * 1024, open_timeout=5)
                        return
                time.sleep(.1)
        except Exception as exc:
            self.close()
            raise BrowserFailure('browser_start') from exc
        self.close()
        raise BrowserFailure('browser_start')

    def ready(self, account):
        return bool(self.process and self.process.poll() is None and self.contexts.get(account, {}).get('confirmed'))

    def open_login(self, account):
        with self.lock:
            self._launch()
            old = self.contexts.get(account)
            if old:
                try:
                    self._call('Target.activateTarget', {'targetId': old['target']})
                    return
                except BrowserFailure:
                    self.contexts.pop(account, None)
                    try:
                        self._call('Target.disposeBrowserContext', {'browserContextId': old['context']})
                    except BrowserFailure:
                        pass
            context = self._call('Target.createBrowserContext', {'disposeOnDetach': True})['browserContextId']
            target = self._call('Target.createTarget', {
                'url': self.login_url, 'browserContextId': context,
                'newWindow': True,
            })['targetId']
            self.contexts[account] = {'context': context, 'target': target, 'confirmed': False}
            # The bootstrap window is not the login window and carries no session.
            private_contexts = {item['context'] for item in self.contexts.values()}
            for tab in self._call('Target.getTargets').get('targetInfos', []):
                if (tab.get('type') == 'page' and tab.get('browserContextId') not in private_contexts
                        and tab.get('url') == 'about:blank'):
                    self._call('Target.closeTarget', {'targetId': tab['targetId']})

    def confirm(self, account):
        with self.lock:
            if not self.process or self.process.poll() is not None or account not in self.contexts:
                self.open_login(account)
                raise BrowserLoginRequired()
            self.contexts[account]['confirmed'] = True

    def image(self, account, url, referer, budget, cancel, category='images'):
        with self.lock:
            if not self.ready(account):
                self.open_login(account)
                raise BrowserLoginRequired()
            from .media import public_url, TYPES
            validate = self.validate_url or public_url
            try:
                validate(url)
            except socket.gaierror as exc:
                raise BrowserFailure('browser_dns') from exc
            except (ValueError, OSError) as exc:
                raise BrowserFailure('browser_address') from exc
            target = self._call('Target.createTarget', {
                'url': 'about:blank', 'browserContextId': self.contexts[account]['context'],
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
                        if mime not in TYPES:
                            login = 'login' in urlsplit(response['url']).path
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
                        if not body:
                            raise BrowserFailure('browser_empty', trace, status)
                        if len(body) > MAX_IMAGE:
                            raise BrowserFailure('browser_large', trace, status)
                        return body, TYPES[mime], trace
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
            if self.profile:
                self.profile.cleanup()
            self.socket = self.process = self.profile = None
            self.contexts.clear()
            self.events.clear()
