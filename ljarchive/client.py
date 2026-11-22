"""Read-only LJ protocol client. Credentials never enter persistent storage."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import xmlrpc.client
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests
from .sources import journal_base
from .retry import backoff
from defusedxml import ElementTree, xmlrpc as safe_xmlrpc

safe_xmlrpc.monkey_patch()
BASE_URL = "https://www.livejournal.com"
LIKES_URL = "https://likes.services.livejournal.com"


class ExportError(Exception):
    """An actionable, credential-free error safe to show in the UI."""


class AuthenticationRequired(ExportError):
    """An HTTP access refusal or login redirect, distinct from transport failure."""
    def __init__(self, message, *, status=None):
        self.status = status
        super().__init__(message)


class HTTPError(ExportError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"ЖЖ вернул HTTP {status}. Прогресс сохранён.")


class APIError(ExportError):
    def __init__(self, method, code):
        self.method, self.code = method, code
        super().__init__(f"ЖЖ отклонил запрос {method} (код {code}).")


class Cancelled(ExportError):
    pass


class RequestLimitReached(ExportError):
    pass


class HTTPRateLimited(ExportError):
    """The HTTP transport has already exhausted the attempts for a 429."""


class APIRateLimited(ExportError):
    pass


class IncompleteData(ExportError):
    """A successful response omitted requested data; retry only the pending objects."""


class WebLoginRequired(AuthenticationRequired):
    def __init__(self):
        super().__init__('ЖЖ не подтвердил вход для изображений. Проверьте вход в браузере и повторите загрузку с паролем.')


class RequestBudget:
    """One hard limit for all outbound HTTP attempts in an export, including retries."""
    def __init__(self, limit=50, progress=None):
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000):
            raise ExportError("Предел запросов должен быть целым числом от 1 до 10000.")
        self.limit, self.used = limit, 0
        self.categories = {}
        self.exhausted = False
        self.progress = progress or (lambda **kwargs: None)
        self.lock = threading.Lock()
        self.delay = 2.0
        self.not_before = 0.0
        self.wait_info = {}
        self.cooldowns = {}
        self.before_host_wait = None
        self.save_cooldown = lambda deadline: None

    def event(self, kind, category, url="", **values):
        from urllib.parse import urlsplit
        self.progress(activity={"kind": kind, "category": category,
                                "host": urlsplit(url).hostname or "", "at": time.time(), **values})

    def describe_wait(self, deadline, reason, category, url="", **values):
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname or ""
        self.cooldowns = {h: wait for h, wait in self.cooldowns.items() if wait["retry_at"] > time.time()}
        previous = self.cooldowns.get(host, {})
        self.wait_info = previous if previous.get("retry_at", 0) > deadline else {
            "reason": reason, "category": category, "host": host, "retry_at": deadline, **values}
        self.cooldowns[host] = self.wait_info
        self.not_before = self.wait_info["retry_at"]

    def wait_for(self, url):
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname or ""
        info = self.cooldowns.get(host, {})
        general = self.cooldowns.get("", {})
        if general.get("retry_at", 0) > info.get("retry_at", 0):
            info = general
        if not self.cooldowns and self.not_before and self.wait_info.get("host", host) in (host, ""):
            return {**self.wait_info, "retry_at": self.not_before}
        return info

    def deadline_for(self, url):
        return self.wait_for(url).get("retry_at", 0)

    def announce_wait(self, progress=None, *, url=None, yieldable=True):
        info = self.wait_for(url) if url is not None else self.wait_info
        deadline = info.get("retry_at", 0)
        if deadline <= time.time():
            return
        if yieldable and url is not None and self.before_host_wait:
            self.before_host_wait(url)
        reason = info.get("reason")
        message = {"timeout": "Сервер не ответил вовремя. Повторим запрос.",
                   "connection": "Не удалось соединиться. Повторим запрос.",
                   "transfer": "Передача прервалась. Повторим запрос.",
                   "api_limit": "ЖЖ ограничил частоту запросов (402).",
                   "incomplete": "Получены не все данные. Повторим загрузку.",
                   "restored": "Ожидание перед продолжением загрузки."}.get(reason)
        if reason == "http":
            message = f"Сервер вернул HTTP {info.get('http_status')}. Повторим запрос."
        (progress or self.progress)(phase="waiting", message=message or "Ожидание повтора.",
                                   wait=info, retry_at=deadline)

    def consume(self, category):
        with self.lock:
            if self.limit is not None and self.used >= self.limit:
                self.exhausted = True
                raise RequestLimitReached(f"Достигнут предел {self.limit} HTTP-запросов. Новые запросы не отправлены; полученные данные сохранены.")
            self.used += 1
            self.categories[category] = self.categories.get(category, 0) + 1
            snapshot = self.snapshot()
        self.progress(**snapshot)

    def snapshot(self):
        return {"requests_used": self.used, "request_limit": self.limit,
                "requests_remaining": None if self.limit is None else self.limit - self.used,
                "request_limit_reached": self.exhausted, "request_categories": dict(self.categories)}


def username(value: str) -> str:
    value = value.strip().lower().replace("-", "_")
    if not re.fullmatch(r"[a-z0-9_]{1,15}", value):
        raise ExportError("Укажите имя пользователя ЖЖ (до 15 букв, цифр или подчёркиваний), без адреса сайта.")
    return value


def normalized(value: Any) -> Any:
    if isinstance(value, xmlrpc.client.Binary):
        # LJ returns base64 for some old posts; retain undecodable bytes in JSON.
        import base64
        try:
            return value.data.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": base64.b64encode(value.data).decode("ascii"), "encoding_unknown": True}
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(v) for v in value]
    return value


@dataclass
class CommentPage:
    comments: list[dict]
    users: dict[int, str]
    maxid: int | None
    nextid: int | None


def parse_comments(data: bytes) -> CommentPage:
    try:
        root = ElementTree.fromstring(data)
        if root.tag != "livejournal" or root.find("comments") is None:
            raise ValueError("not an export")
        comments = []
        for node in root.findall("./comments/comment"):
            item = dict(node.attrib)
            for key in ("id", "posterid", "jitemid", "parentid"):
                if key in item:
                    item[key] = int(item[key] or 0)
            if int(item.get("id", 0)) <= 0:
                raise ValueError("invalid ID")
            for key in ("body", "subject", "date"):
                child = node.find(key)
                if child is not None:
                    item[key] = "".join(child.itertext())
            item["props"] = {p.attrib["name"]: p.text or "" for p in node.findall("./properties/property")}
            comments.append(item)
        users = {int(n.attrib["id"]): n.attrib["user"] for n in root.findall(".//usermap")}
        def number(key):
            text = root.findtext(key)
            return int(text) if text is not None else None
        return CommentPage(comments, users, number("maxid"), number("nextid"))
    except Exception as exc:
        raise ExportError("ЖЖ вернул некорректный XML комментариев или страницу входа. Выгрузка не отмечена полной; повторите вход.") from exc


class LiveJournalClient:
    def __init__(self, user: str, password: str, *, delay: float = 2.0,
                 cancel: threading.Event | None = None, budget: RequestBudget | None = None,
                 progress=None, max_attempts=4, journal=None, source_type=None, session_ready=None):
        from .sources import source
        self.source = source(user, journal, source_type)
        self.session_ready = session_ready
        self.account = self.source['login_username']
        self.user = self.source['username']
        self._password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
        self._web_password = password
        self._web_login_attempted = False
        self.delay = max(1.0, float(delay))
        self.cancel = cancel or threading.Event()
        self.budget = budget or RequestBudget()
        self.budget.delay = self.delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "LiveJournalLocalArchive/0.4 (personal backup)"
        self._last_request = 0.0
        self._cookie = None
        self.journal_id = None
        self._calendar = None
        self.userpics = None
        self._reaction_token = None
        self.progress = progress or (lambda **kwargs: None)
        self.deadline = None
        self.max_attempts = max_attempts
        self.not_before = 0.0
        self.save_cooldown = lambda deadline: None

    def check(self):
        if self.deadline and time.monotonic() >= self.deadline:
            raise ExportError("Истекло время поиска сообществ. Попробуйте ещё раз позже.")
        if self.cancel.is_set():
            raise Cancelled("Выгрузка остановлена. Уже полученные данные сохранены; можно продолжить после повторного входа.")

    def _wait(self, seconds: float):
        self.check()
        deadline = time.monotonic() + max(0, seconds)
        if self.deadline and deadline >= self.deadline:
            raise ExportError("Ожидание превышает время поиска сообществ. Попробуйте позже.")
        while time.monotonic() < deadline:
            if self.cancel.wait(min(30, deadline - time.monotonic())):
                self.check()

    def _defer(self, seconds, **details):
        if self.deadline and details.get("attempt") == details.get("max_attempts"):
            return  # This foreground operation has no further retry to schedule.
        self.budget.describe_wait(time.time() + seconds, **details)
        self.save_cooldown(self.budget.not_before)
        self.budget.announce_wait(self.progress)

    def _request(self, method: str, path: str, *, base_url=BASE_URL, category="livejournal", accepted_statuses=(200,), attempts=None, rpc_method=None, **kwargs) -> bytes:
        user_directory = (base_url == 'https://users.livejournal.com'
                          and (path == f'/{self.user}/profile/subslist/' or base_url + path == journal_base(self.user) + '/')
                          and (self.user.startswith('_') or self.user.endswith('_')))
        if not user_directory and base_url not in (BASE_URL, LIKES_URL, journal_base(self.user)):
            raise ExportError("Неподдерживаемый адрес сервиса ЖЖ.")
        attempts = self.max_attempts if attempts is None else attempts
        from .browser_bridge import safe_url
        endpoint = {'method': method, 'request_url': safe_url(base_url + path)}
        if rpc_method:
            endpoint['rpc_method'] = rpc_method
        for attempt in range(attempts):
            self.check()
            if self.budget.limit is not None and self.budget.used >= self.budget.limit:
                self.budget.consume(category)  # Raise before waiting for a request that cannot be sent.
            self.budget.announce_wait(self.progress, url=base_url, yieldable=path != '/login.bml')
            self._wait(self.budget.deadline_for(base_url) - time.time())
            self._wait(self.delay - (time.monotonic() - self._last_request))
            if attempt:
                self.progress(phase="retrying", message=f"Повтор запроса к ЖЖ · попытка {attempt + 1} из {attempts}")
            self.budget.consume(category)
            self.budget.event("request", category, base_url, **endpoint, attempt=attempt + 1, max_attempts=attempts)
            self._last_request = time.monotonic()
            try:
                timeout = (15, 60)
                if self.deadline:
                    left = max(.1, self.deadline - time.monotonic())
                    timeout = (min(8, left), min(12, left))
                with self.session.request(method, base_url + path, timeout=timeout,
                                          allow_redirects=False, stream=True, **kwargs) as response:
                    if response.status_code >= 400:
                        self.budget.event('failure', category, base_url, **endpoint, reason='http', http_status=response.status_code)
                    if response.status_code in (429, 500, 502, 503, 504):
                        self._defer(backoff(attempt, response.headers.get("Retry-After"), limited=response.status_code == 429),
                                    reason="http", category=category, url=base_url, http_status=response.status_code,
                                    attempt=attempt + 1, max_attempts=attempts,
                                    server_delay=bool(response.headers.get("Retry-After")))
                        if attempt == attempts - 1:
                            if response.status_code == 429:
                                raise HTTPRateLimited("HTTP 429 · ЖЖ ограничил частоту запросов. Срок повтора сохранён.")
                            raise ExportError("ЖЖ временно ограничивает запросы или недоступен. Данные сохранены, продолжите позже.")
                        continue
                    if response.status_code in (401, 403):
                        raise AuthenticationRequired("ЖЖ отказал в доступе (HTTP 401/403). Проверьте вход в браузере; при ограничении запросов продолжите позже.", status=response.status_code)
                    if 300 <= response.status_code < 400 and response.status_code not in accepted_statuses:
                        self.budget.event('failure', category, base_url, **endpoint, reason='redirects', http_status=response.status_code)
                        raise AuthenticationRequired("ЖЖ перенаправил запрос. Возможно, требуется вход или подтверждение в браузере.", status=response.status_code)
                    if response.status_code not in accepted_statuses:
                        raise HTTPError(response.status_code)
                    self.budget.event('response', category, base_url, **endpoint, http_status=response.status_code)
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        self.check()
                        size += len(chunk)
                        if size > 64 * 1024 * 1024:
                            raise ExportError("Ответ ЖЖ превышает 64 МБ. Выгрузка остановлена без потери прогресса.")
                        chunks.append(chunk)
                    self.budget.event("success", category, base_url, **endpoint, http_status=response.status_code, bytes=size)
                    if attempt:
                        self.progress(phase="loading", message="Ответ ЖЖ получен. Продолжаем загрузку.")
                    return b"".join(chunks)
            except requests.exceptions.SSLError as exc:
                self.budget.event('failure', category, base_url, **endpoint, reason='tls')
                raise ExportError("Не удалось проверить TLS-сертификат ЖЖ. Проверьте часы и сертификаты системы; проверка HTTPS не отключалась.") from exc
            except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                self.budget.event('failure', category, base_url, **endpoint,
                                  reason='timeout' if isinstance(exc, requests.Timeout) else 'connection')
                self._defer(backoff(attempt), reason="timeout" if isinstance(exc, requests.Timeout) else "connection",
                            category=category, url=base_url, attempt=attempt + 1, max_attempts=attempts)
                if attempt == attempts - 1:
                    raise ExportError("Не удалось связаться с ЖЖ. Проверьте соединение и продолжите выгрузку.") from exc
        raise ExportError("Не удалось получить ответ ЖЖ.")

    def _rpc(self, method: str, args: dict | None = None) -> dict:
        body = xmlrpc.client.dumps(() if args is None else (args,),
                                   methodname="LJ.XMLRPC." + method, allow_none=False).encode("utf-8")
        data = self._request("POST", "/interface/xmlrpc", rpc_method=method, data=body, headers={"Content-Type": "text/xml"})
        try:
            result = normalized(xmlrpc.client.loads(data)[0][0])
            if not isinstance(result, dict):
                raise ValueError("not a struct")
            return result
        except xmlrpc.client.Fault as exc:
            self.budget.event('failure', 'livejournal', BASE_URL, rpc_method=method,
                              rpc_code=exc.faultCode if type(exc.faultCode) is int else None)
            if exc.faultCode == 402:
                raise APIRateLimited("ЖЖ сообщил об ограничении частоты запросов XML-RPC (402).") from exc
            # Never echo a remote fault string: it can contain request credentials.
            raise APIError(method, exc.faultCode) from exc
        except Exception as exc:
            raise ExportError(f"ЖЖ вернул некорректный ответ на {method}. Прогресс сохранён.") from exc

    def call(self, method: str, *, refresh=False, **params) -> dict:
        standard_calendar = method == 'getdaycounts' and not params
        capability = {'getdaycounts': 'calendar', 'getevents': 'posts', 'getcomments': 'comments', 'syncitems': 'sync'}.get(method)
        if self.source['type'] == 'reader' and method not in ('login', 'sessiongenerate', 'getevents', 'getcomments'):
            raise ExportError('Метод недоступен в режиме чтения.')
        if self.source['type'] != 'own':
            if method in ('getfriends', 'getfriendof', 'getinbox', 'getusertags'):
                raise ExportError('Личные данные аккаунта не входят в архив сообщества.')
            if capability:
                if self.source['access']['status'] != 'verified':
                    raise ExportError('Права доступа к журналу ещё не подтверждены.')
                key = 'journal' if method == 'getcomments' else 'usejournal'
                if any(params.get(k, self.user) != self.user for k in ('journal', 'usejournal')):
                    raise ExportError('Запрос относится к другому журналу.')
                if 'journalid' in params and params['journalid'] != self.journal_id:
                    raise ExportError('ID запроса относится к другому журналу.')
                params[key] = self.user
        if standard_calendar and refresh:
            self._calendar = None
        if standard_calendar and self._calendar and time.monotonic() < self._calendar[0]:
            self.check()
            self.budget.event('step', 'livejournal', rpc_method=method, message='Календарь из текущей выгрузки')
            return deepcopy(self._calendar[1])
        for attempt in range(self.max_attempts):
            try:
                if attempt:
                    self.progress(phase="retrying", message=f"Повтор запроса XML-RPC · попытка {attempt + 1} из {self.max_attempts}")
                challenge = self._rpc("getchallenge").get("challenge")
                if not isinstance(challenge, str) or not challenge:
                    raise ExportError("ЖЖ не выдал challenge для входа.")
                response = hashlib.md5((challenge + self._password_hash).encode("utf-8")).hexdigest()
                result = self._rpc(method, {**params, "username": self.account, "ver": 1, "auth_method": "challenge",
                                          "auth_challenge": challenge, "auth_response": response})
                if method == 'getevents' and self.source['type'] != 'own':
                    self._validate_entries(result)
                if capability:
                    self.source['capabilities'][capability] = 'available'
                if standard_calendar and self._valid_calendar(result):
                    self._calendar = (time.monotonic() + 60, deepcopy(result))
                return result
            except APIRateLimited:
                self._defer(backoff(attempt, limited=True), reason="api_limit", category="livejournal",
                            url=BASE_URL, attempt=attempt + 1, max_attempts=self.max_attempts)
                if attempt == self.max_attempts - 1:
                    raise
            except APIError as exc:
                if capability:
                    self.source['capabilities'][capability] = 'denied' if exc.code in (300, 301, 302, 303, 304) else 'error'
                raise

    @staticmethod
    def _valid_calendar(result):
        days = result.get('daycounts')
        if not isinstance(days, list):
            return False
        try:
            return all(date.fromisoformat(day['date']).isoformat() == day['date']
                       and str(day['count']).isascii() and str(day['count']).isdecimal()
                       for day in days)
        except (ValueError, TypeError, KeyError):
            return False

    def _validate_entries(self, result):
        from urllib.parse import urlsplit
        entries = result.get('events')
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ExportError('ЖЖ не вернул список записей журнала.')
        for entry in entries:
            if self.source['type'] == 'reader' and not entry.get('url') and not entry.get('journalid'):
                raise ExportError('ЖЖ не указал журнал полученной записи.')
            if entry.get('journalid') and entry['journalid'] != self.journal_id:
                raise ExportError('ЖЖ вернул запись из другого журнала. Данные не смешаны.')
            if entry.get('url') and not entry.get('repost'):
                parsed = urlsplit(entry['url'])
                directory = (journal_base(self.user).startswith('https://users.livejournal.com/')
                             and parsed.hostname == 'users.livejournal.com'
                             and parsed.path.startswith('/' + self.user + '/'))
                if not directory and parsed.hostname not in (self.user + '.livejournal.com', self.user.replace('_', '-') + '.livejournal.com'):
                    raise ExportError('Адрес полученной записи относится к другому журналу.')

    def login(self):
        self._calendar = None
        self.progress(phase='login', message='Вход в ЖЖ…')
        external = self.source['type'] != 'own'
        self.source['access']['status'] = 'unverified'
        result = self.call("login", clientversion="Python-LJLocalArchive/0.4", **({} if external else {'getpickws': 1, 'getpickwurls': 1}))
        if result.get('username') and username(result['username']) != self.account:
            raise ExportError('ЖЖ вернул другой аккаунт при входе.')
        self.profile = {} if external else {key: result[key] for key in (
            "username", "userid", "fullname", "friendgroups", "usejournals", "caps",
            "is_validated", "is_identity", "identity_type", "identity_value", "identity_display") if key in result}
        self.userpics = None if external else {key: result.get(key) for key in ("pickws", "pickwurls", "defaultpicurl")}
        uid = result.get("userid")
        if not external and isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
            self.journal_id = uid
        self.generate_session()
        if self.source['type'] == 'reader':
            base = journal_base(self.user)
            try:
                raw = self._request('GET', '/', base_url=base, headers={'Cookie': 'ljsession=' + self._cookie})
                try:
                    self.read_journal_identity(raw)
                except ExportError:
                    from .web_session import LoginForm
                    form = LoginForm()
                    form.feed(raw.decode('utf-8', errors='replace'))
                    if not form.token:
                        raise
                    raise AuthenticationRequired('ЖЖ запросил веб-вход для чтения журнала.') from None
            except AuthenticationRequired:
                if not self.ensure_web_session(base + '/'):
                    raise
                self.read_journal_identity(self._request('GET', '/', base_url=base))
        elif self.source['type'] == 'community':
            self.source['access']['status'] = 'unconfirmed'
            if self.user not in self.managed_communities(confirm_empty=True):
                raise ExportError('ЖЖ не подтвердил управление этим сообществом. Проверьте аккаунт и права на странице «Мои сообщества»; право публикации или модерации недостаточно.')
            identity = self.call('login', usejournal=self.user, getcaps=1)
            if identity.get('username') != self.user or type(identity.get('userid')) is not int or identity['userid'] <= 0 or 'caps' not in identity:
                self.source['access']['status'] = 'unconfirmed'
                raise ExportError('ЖЖ не подтвердил ID и права управления сообществом. Выгрузка не начата.')
            self.journal_id = identity['userid']
        self.source['id'] = self.journal_id
        self.source['access']['status'] = 'verified'
        if self.session_ready:
            self.session_ready(self)

    def generate_session(self):
        cookie = self.call("sessiongenerate", expiration="short", ipfixed=1).get("ljsession")
        if not isinstance(cookie, str) or not cookie or any(c in cookie for c in "\r\n;"):
            raise ExportError("ЖЖ не выдал сессию для этого аккаунта.")
        self._cookie = cookie

    def managed_communities(self, *, confirm_empty=False):
        from .sources import managed_communities
        if not self._cookie:
            self.generate_session()
        path = '/community/manage.bml'
        try:
            page = self._request('GET', path, headers={'Cookie': 'ljsession=' + self._cookie})
            # LJ can serve its sign-in form with HTTP 200 to an XML-RPC session.
            from .web_session import LoginForm
            names = managed_communities(page) - {self.account}
            if not names and not re.search(rb'Site\.remote\s*=\s*\{[^}]*"username"\s*:\s*"[^"]+"', page):
                form = LoginForm()
                form.feed(page.decode('utf-8', errors='replace'))
                if form.token:
                    self.budget.event('step', 'livejournal', BASE_URL, message='ЖЖ вернул форму входа вместо списка сообществ.')
                    raise AuthenticationRequired('ЖЖ вернул форму входа.')
        except AuthenticationRequired:
            try:
                if not self.ensure_web_session(BASE_URL + path):
                    raise WebLoginRequired()
            except WebLoginRequired:
                raise AuthenticationRequired('ЖЖ не подтвердил вход для просмотра сообществ.') from None
            page = self._request('GET', path)
            names = managed_communities(page) - {self.account}
        if confirm_empty and not names:
            text = page.decode('utf-8', errors='replace')
            remote = re.search(r'\bSite\.remote\s*=\s*(\{)', text)
            try:
                identity = json.JSONDecoder().raw_decode(text[remote.start(1):])[0] if remote else {}
            except ValueError:
                identity = {}
            from .fulltext import visible
            empty_message = re.search(
                r"you (?:do not|don't|currently do not) (?:manage|maintain) any communit(?:y|ies)"
                r"|you are not managing any communities|вы не управляете сообществами"
                r"|you are not (?:a |the )?(?:maintainer|owner) of any communit(?:y|ies)"
                r"|вы не (?:управляете ни одним сообществом|являетесь смотрителем ни одного сообщества)"
                r"|у вас нет (?:управляемых сообществ|сообществ, которыми вы управляете)",
                visible(text), re.I)
            if identity.get('username') != self.account or not empty_message:
                raise IncompleteData('ЖЖ не подтвердил список управляемых сообществ.')
        return sorted(names)

    def ensure_web_session(self, url="https://ic.pics.livejournal.com/"):
        from .web_session import establish
        return establish(self, url)

    def personal_page(self, path):
        if self.source['type'] != 'own':
            raise ExportError('Личные настройки аккаунта не входят в архив сообщества.')
        return self._request("GET", path, category="personal", headers={"Cookie": "ljsession=" + self._cookie})

    def subscription_preview(self):
        from .social import subscription_page
        if self.source['type'] != 'own':
            raise ExportError('Подписки доступны в профиле своего журнала.')
        from urllib.parse import urlsplit
        from .social import social_profile_url
        profile = urlsplit(social_profile_url({}, self.user))
        raw = self._request('GET', profile.path + 'subslist/', base_url='https://' + profile.netloc, category='personal',
                            headers={'Cookie': 'ljsession=' + self._cookie} if self._cookie else {})
        return subscription_page(raw, self.user)

    def full_subscription_list(self, name, token):
        from .social import SUBSCRIPTIONS, subscription_data
        if self.source['type'] != 'own' or name not in SUBSCRIPTIONS.values() or not token:
            raise IncompleteData('ЖЖ не предоставил полный список подписок.')
        batch = [{'jsonrpc': '2.0', 'id': 1, 'method': 'profile.get_friends',
                  'params': {'user': self.user, 'get_list': name, 'mode_full': True, 'auth_token': token}}]
        raw = self._request('POST', '/__api/', category='personal', data=json.dumps(batch),
                            headers={'Content-Type': 'text/plain;charset=UTF-8',
                                     **({'Cookie': 'ljsession=' + self._cookie} if self._cookie else {})})
        try:
            reply, = json.loads(raw)
            if reply.get('id') != 1 or reply.get('error') or reply['result'].get('status') != 'ok':
                raise ValueError()
            return subscription_data(reply['result'], name)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise IncompleteData('ЖЖ не вернул полный список подписок.') from exc

    def personal_feed(self, name):
        if self.source['type'] != 'own':
            raise ExportError('Личный профиль аккаунта не входит в архив сообщества.')
        if name not in ("foaf", "userpics"):
            raise ExportError("Неизвестный раздел профиля.")
        return self._request("GET", "/data/" + name, category="personal",
                             base_url=journal_base(self.user),
                             headers={"Cookie": "ljsession=" + self._cookie})

    def reaction_counts(self, entities):
        # The read-only service used by LJ.LikeReaction.callGetLikesRaw.
        # No password, session cookie or write/vote method is sent to this service.
        raw = self._request("POST", "/get", base_url=LIKES_URL, category="reactions",
                            data=json.dumps({"mode": 0, "items": entities}),
                            headers={"Content-Type": "text/plain;charset=UTF-8"})
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ExportError("ЖЖ вернул некорректный ответ с реакциями.") from exc
        if not isinstance(result, list):
            raise ExportError("ЖЖ не вернул список реакций.")
        return result

    def journal_feed_pin(self):
        from .feed_pin import page_pin
        from .web_session import ready

        base = journal_base(self.user)
        def read(path, headers):
            raw = self._request('GET', path, base_url=base, headers=headers, attempts=1)
            return page_pin(raw, self.user, self.journal_id, self.account, requested_home=True)

        home = base + '/'
        signed = ready(self, home)
        for path in ('/', '/?style=mine'):
            if self.budget.limit is not None and self.budget.used >= self.budget.limit:
                break
            try:
                did = read(path, {} if signed else {'Cookie': 'ljsession=' + self._cookie})
            except AuthenticationRequired:
                if signed or not self.ensure_web_session(home):
                    return None
                signed = True
                did = read(path, {})
            if did is not None:
                return did
        return None

    def web_deleted_threads(self, ditemid, dtalkid):
        from .comment_page import deleted_threads
        from .web_session import cookie_header
        base = journal_base(self.user)
        headers = {"Accept": "text/html"}
        if self._cookie and not cookie_header(self, base + "/"):
            headers["Cookie"] = "ljsession=" + self._cookie
        raw = self._request("GET", f"/{int(ditemid)}.html?thread={int(dtalkid)}",
                            base_url=base, category="livejournal", headers=headers)
        return deleted_threads(raw, self.user, ditemid)

    def discover_journal_id(self, display_id):
        """Resolve an old archive without a stored user ID, using one public page."""
        raw = self._request("GET", f"/{int(display_id)}.html", category="reactions",
                            base_url=journal_base(self.user))
        return self.read_journal_identity(raw)

    def read_journal_identity(self, raw):
        source = raw.decode("utf-8", errors="replace")
        match = re.search(r"\bSite\.journal\s*=\s*(\{)", source)
        try:
            if not match:
                raise ValueError()
            data, _ = json.JSONDecoder().raw_decode(source[match.start(1):])
            uid = data["id"]
            if username(data["username"]) != self.user or type(uid) is not int or uid <= 0:
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise ExportError("ЖЖ не подтвердил имя и ID запрошенного журнала.") from exc
        community = data.get('is_community')
        if community in (True, False, 1, 0, '1', '0'):
            self.source['is_community'] = community in (True, 1, '1')
        elif data.get('is_personal') is True:
            self.source['is_community'] = False
        self.journal_id = uid
        return uid

    def reaction_authors(self, entities):
        """Batch only the read-only method used by LJ's reaction details popup."""
        if not entities:
            return []
        headers = {"Content-Type": "text/plain;charset=UTF-8"}
        if self._cookie:
            headers["Cookie"] = "ljsession=" + self._cookie
        if not self._reaction_token:
            # The JSON-RPC token belongs to the site session, not an entry. Journal
            # subdomains can redirect private entries even with a valid API session.
            raw = self._request("GET", "/", category="reaction_authors", headers=headers)
            match = re.search(r'"auth_token"\s*:\s*("(?:[^"\\]|\\.)*")', raw.decode("utf-8", errors="replace"))
            if not match:
                raise ExportError("ЖЖ не выдал токен для чтения авторов реакций. Попробуйте выгрузку со входом.")
            self._reaction_token = json.loads(match[1])
        batch = [{"jsonrpc": "2.0", "method": "likes.get_user_list", "id": index,
                  "params": {**entity, "type": "all", "auth_token": self._reaction_token}}
                 for index, entity in enumerate(entities, 1)]
        raw = self._request("POST", "/__api/", category="reaction_authors", data=json.dumps(batch), headers=headers)
        try:
            responses = json.loads(raw)
            if not isinstance(responses, list):
                raise ValueError()
        except (ValueError, UnicodeError) as exc:
            raise ExportError("ЖЖ вернул некорректный список авторов реакций.") from exc
        by_id = {}
        for response in responses:
            if isinstance(response, dict) and type(response.get("id")) is int:
                by_id.setdefault(response["id"], []).append(response)
        result = []
        for index in range(1, len(entities) + 1):
            matches = by_id.get(index, [])
            response = matches[0] if len(matches) == 1 else {}
            inner = response.get("result")
            users = inner.get("result") if isinstance(inner, dict) else None
            if response.get("error") or not isinstance(users, list) or inner.get("error"):
                result.append({"error": "ЖЖ не вернул авторов этой реакции. Может потребоваться вход владельца."})
            else:
                result.append({"users": users})
        return result

    def comments(self, kind: str, start: int) -> tuple[CommentPage, bytes]:
        if not self._cookie:
            raise ExportError("Сначала войдите в ЖЖ.")
        if self.source['type'] == 'reader':
            raise ExportError('Общий экспорт комментариев доступен только владельцу или управляющему.')
        if self.source['type'] == 'community' and self.source['access']['status'] != 'verified':
            raise ExportError('Права доступа к журналу ещё не подтверждены.')
        raw = self._request("GET", "/export_comments.bml", params={"get": kind, "startid": start,
                            "numitems": 1000 if kind == "comment_meta" else 500, "props": 1,
                            **({'authas': self.user} if self.source['type'] == 'community' else {})},
                            headers={"Cookie": "ljsession=" + self._cookie})
        page = parse_comments(raw)
        self.source['capabilities']['comments'] = 'available'
        return page, raw

    def close(self):
        self._calendar = None
        self._password_hash = ""
        self._web_password = ""
        self._cookie = None
        self._reaction_token = None
        self.session.cookies.clear()
        self.session.close()
