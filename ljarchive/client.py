"""Read-only LJ protocol client. Credentials never enter persistent storage."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import xmlrpc.client
from dataclasses import dataclass
from typing import Any

import requests
from .retry import backoff
from defusedxml import ElementTree, xmlrpc as safe_xmlrpc

safe_xmlrpc.monkey_patch()
BASE_URL = "https://www.livejournal.com"
LIKES_URL = "https://likes.services.livejournal.com"


class ExportError(Exception):
    """An actionable, credential-free error safe to show in the UI."""


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


class APIRateLimited(ExportError):
    pass


class IncompleteData(ExportError):
    """A successful response omitted requested data; retry only the pending objects."""


class WebLoginRequired(ExportError):
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
        self.save_cooldown = lambda deadline: None

    def event(self, kind, category, url="", **values):
        from urllib.parse import urlsplit
        self.progress(activity={"kind": kind, "category": category,
                                "host": urlsplit(url).hostname or "", "at": time.time(), **values})

    def describe_wait(self, deadline, reason, category, url="", **values):
        from urllib.parse import urlsplit
        self.wait_info = {"reason": reason, "category": category,
                          "host": urlsplit(url).hostname or "", "retry_at": deadline, **values}

    def announce_wait(self, progress=None):
        if self.not_before <= time.time():
            return
        info = self.wait_info or {"reason": "restored", "retry_at": self.not_before}
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
                                   wait=info, retry_at=self.not_before)

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
        self.userpics = None
        self._reaction_token = None
        self.progress = progress or (lambda **kwargs: None)
        self.max_attempts = max_attempts
        self.not_before = 0.0
        self.save_cooldown = lambda deadline: None

    def check(self):
        if self.cancel.is_set():
            raise Cancelled("Выгрузка остановлена. Уже полученные данные сохранены; можно продолжить после повторного входа.")

    def _wait(self, seconds: float):
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            if self.cancel.wait(min(30, deadline - time.monotonic())):
                self.check()

    def _defer(self, seconds, **details):
        self.not_before = max(self.not_before, time.time() + seconds)
        self.budget.not_before = self.not_before
        self.budget.describe_wait(self.not_before, **details)
        self.save_cooldown(self.not_before)
        self.budget.announce_wait(self.progress)

    def _request(self, method: str, path: str, *, base_url=BASE_URL, category="livejournal", accepted_statuses=(200,), **kwargs) -> bytes:
        if base_url not in (BASE_URL, LIKES_URL, f"https://{self.user.replace('_', '-')}.livejournal.com"):
            raise ExportError("Неподдерживаемый адрес сервиса ЖЖ.")
        for attempt in range(self.max_attempts):
            self.check()
            if self.budget.limit is not None and self.budget.used >= self.budget.limit:
                self.budget.consume(category)  # Raise before waiting for a request that cannot be sent.
            self.budget.not_before = max(self.not_before, self.budget.not_before)
            self.budget.announce_wait(self.progress)
            self._wait(self.budget.not_before - time.time())
            self._wait(self.delay - (time.monotonic() - self._last_request))
            if attempt:
                self.progress(phase="retrying", message=f"Повтор запроса к ЖЖ · попытка {attempt + 1} из {self.max_attempts}")
            self.budget.consume(category)
            self.budget.event("request", category, base_url, attempt=attempt + 1, max_attempts=self.max_attempts)
            self._last_request = time.monotonic()
            try:
                with self.session.request(method, base_url + path, timeout=(15, 60),
                                          allow_redirects=False, stream=True, **kwargs) as response:
                    if response.status_code >= 400:
                        self.budget.event('failure', category, base_url, reason='http', http_status=response.status_code)
                    if response.status_code in (429, 500, 502, 503, 504):
                        self._defer(backoff(attempt, response.headers.get("Retry-After"), limited=response.status_code == 429),
                                    reason="http", category=category, url=base_url, http_status=response.status_code,
                                    attempt=attempt + 1, max_attempts=self.max_attempts,
                                    server_delay=bool(response.headers.get("Retry-After")))
                        if attempt == self.max_attempts - 1:
                            raise ExportError("ЖЖ временно ограничивает запросы или недоступен. Данные сохранены, продолжите позже.")
                        continue
                    if response.status_code in (401, 403):
                        raise ExportError("ЖЖ отказал в доступе (HTTP 401/403). Проверьте вход в браузере; при ограничении запросов продолжите позже.")
                    if 300 <= response.status_code < 400 and response.status_code not in accepted_statuses:
                        self.budget.event('failure', category, base_url, reason='redirects', http_status=response.status_code)
                        raise ExportError("ЖЖ перенаправил запрос. Возможно, требуется вход или подтверждение в браузере.")
                    if response.status_code not in accepted_statuses:
                        raise HTTPError(response.status_code)
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        self.check()
                        size += len(chunk)
                        if size > 64 * 1024 * 1024:
                            raise ExportError("Ответ ЖЖ превышает 64 МБ. Выгрузка остановлена без потери прогресса.")
                        chunks.append(chunk)
                    self.budget.event("success", category, base_url)
                    if attempt:
                        self.progress(phase="loading", message="Ответ ЖЖ получен. Продолжаем загрузку.")
                    return b"".join(chunks)
            except requests.exceptions.SSLError as exc:
                self.budget.event('failure', category, base_url, reason='tls')
                raise ExportError("Не удалось проверить TLS-сертификат ЖЖ. Проверьте часы и сертификаты системы; проверка HTTPS не отключалась.") from exc
            except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                self._defer(backoff(attempt), reason="timeout" if isinstance(exc, requests.Timeout) else "connection",
                            category=category, url=base_url, attempt=attempt + 1, max_attempts=self.max_attempts)
                if attempt == self.max_attempts - 1:
                    raise ExportError("Не удалось связаться с ЖЖ. Проверьте соединение и продолжите выгрузку.") from exc
        raise ExportError("Не удалось получить ответ ЖЖ.")

    def _rpc(self, method: str, args: dict | None = None) -> dict:
        body = xmlrpc.client.dumps(() if args is None else (args,),
                                   methodname="LJ.XMLRPC." + method, allow_none=False).encode("utf-8")
        data = self._request("POST", "/interface/xmlrpc", data=body, headers={"Content-Type": "text/xml"})
        try:
            result = normalized(xmlrpc.client.loads(data)[0][0])
            if not isinstance(result, dict):
                raise ValueError("not a struct")
            return result
        except xmlrpc.client.Fault as exc:
            if exc.faultCode == 402:
                raise APIRateLimited("ЖЖ сообщил об ограничении частоты запросов XML-RPC (402).") from exc
            # Never echo a remote fault string: it can contain request credentials.
            raise APIError(method, exc.faultCode) from exc
        except Exception as exc:
            raise ExportError(f"ЖЖ вернул некорректный ответ на {method}. Прогресс сохранён.") from exc

    def call(self, method: str, **params) -> dict:
        capability = {'getdaycounts': 'calendar', 'getevents': 'posts', 'getcomments': 'comments', 'syncitems': 'sync'}.get(method)
        if self.source['type'] == 'community':
            if method in ('getfriends', 'getfriendof', 'getinbox', 'getusertags'):
                raise ExportError('Личные данные аккаунта не входят в архив сообщества.')
            if capability:
                if self.source['access']['status'] != 'verified':
                    raise ExportError('Права управления сообществом ещё не подтверждены.')
                key = 'journal' if method == 'getcomments' else 'usejournal'
                if any(params.get(k, self.user) != self.user for k in ('journal', 'usejournal')):
                    raise ExportError('Запрос относится к другому журналу.')
                if 'journalid' in params and params['journalid'] != self.journal_id:
                    raise ExportError('ID запроса относится к другому журналу.')
                params[key] = self.user
        for attempt in range(self.max_attempts):
            try:
                if attempt:
                    self._wait(max(self.not_before, self.budget.not_before) - time.time())
                    self.progress(phase="retrying", message=f"Повтор запроса XML-RPC · попытка {attempt + 1} из {self.max_attempts}")
                challenge = self._rpc("getchallenge").get("challenge")
                if not isinstance(challenge, str) or not challenge:
                    raise ExportError("ЖЖ не выдал challenge для входа.")
                response = hashlib.md5((challenge + self._password_hash).encode("utf-8")).hexdigest()
                result = self._rpc(method, {**params, "username": self.account, "ver": 1, "auth_method": "challenge",
                                          "auth_challenge": challenge, "auth_response": response})
                if method == 'getevents' and self.source['type'] == 'community':
                    self._validate_entries(result)
                if capability:
                    self.source['capabilities'][capability] = 'available'
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

    def _validate_entries(self, result):
        from urllib.parse import urlsplit
        entries = result.get('events')
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ExportError('ЖЖ не вернул список записей сообщества.')
        for entry in entries:
            if entry.get('journalid') and entry['journalid'] != self.journal_id:
                raise ExportError('ЖЖ вернул запись из другого журнала. Данные не смешаны.')
            if entry.get('url') and not entry.get('repost'):
                host = urlsplit(entry['url']).hostname
                if host not in (self.user + '.livejournal.com', self.user.replace('_', '-') + '.livejournal.com'):
                    raise ExportError('Адрес полученной записи относится к другому журналу.')

    def login(self):
        self.progress(phase='login', message='Вход в ЖЖ…')
        community = self.source['type'] == 'community'
        self.source['access']['status'] = 'unverified'
        result = self.call("login", clientversion="Python-LJLocalArchive/0.4", **({} if community else {'getpickws': 1, 'getpickwurls': 1}))
        if result.get('username') and username(result['username']) != self.account:
            raise ExportError('ЖЖ вернул другой аккаунт при входе.')
        self.profile = {} if community else {key: result[key] for key in (
            "username", "userid", "fullname", "friendgroups", "usejournals", "caps",
            "is_validated", "is_identity", "identity_type", "identity_value", "identity_display") if key in result}
        self.userpics = None if community else {key: result.get(key) for key in ("pickws", "pickwurls", "defaultpicurl")}
        uid = result.get("userid")
        if not community and isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
            self.journal_id = uid
        cookie = self.call("sessiongenerate", expiration="short", ipfixed=1).get("ljsession")
        if not isinstance(cookie, str) or not cookie or any(c in cookie for c in "\r\n;"):
            raise ExportError("ЖЖ не выдал сессию для выгрузки комментариев.")
        self._cookie = cookie
        if community:
            from .sources import managed_communities
            page = self._request('GET', '/community/manage.bml', headers={'Cookie': 'ljsession=' + self._cookie})
            if self.user not in managed_communities(page):
                self.source['access']['status'] = 'unconfirmed'
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

    def ensure_web_session(self):
        from .web_session import establish
        return establish(self)

    def personal_page(self, path):
        if self.source['type'] == 'community':
            raise ExportError('Личные настройки аккаунта не входят в архив сообщества.')
        return self._request("GET", path, category="personal", headers={"Cookie": "ljsession=" + self._cookie})

    def personal_feed(self, name):
        if self.source['type'] == 'community':
            raise ExportError('Личный профиль аккаунта не входит в архив сообщества.')
        if name not in ("foaf", "userpics"):
            raise ExportError("Неизвестный раздел профиля.")
        return self._request("GET", "/data/" + name, category="personal",
                             base_url=f"https://{self.user.replace('_', '-')}.livejournal.com",
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

    def discover_journal_id(self, display_id):
        """Resolve an old archive without a stored user ID, using one public page."""
        raw = self._request("GET", f"/{int(display_id)}.html", category="reactions",
                            base_url=f"https://{self.user.replace('_', '-')}.livejournal.com")
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
            raise ExportError("Не удалось определить ID журнала для реакций. Повторите выгрузку со входом.") from exc
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
        if self.source['type'] == 'community' and self.source['access']['status'] != 'verified':
            raise ExportError('Права управления сообществом ещё не подтверждены.')
        raw = self._request("GET", "/export_comments.bml", params={"get": kind, "startid": start,
                            "numitems": 1000 if kind == "comment_meta" else 500, "props": 1,
                            **({'authas': self.user} if self.source['type'] == 'community' else {})},
                            headers={"Cookie": "ljsession=" + self._cookie})
        page = parse_comments(raw)
        self.source['capabilities']['comments'] = 'available'
        return page, raw

    def close(self):
        self._password_hash = ""
        self._web_password = ""
        self._cookie = None
        self._reaction_token = None
        self.session.cookies.clear()
        self.session.close()
