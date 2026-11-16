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


class Cancelled(ExportError):
    pass


class RequestLimitReached(ExportError):
    pass


class APIRateLimited(ExportError):
    pass


class IncompleteData(ExportError):
    """A successful response omitted requested data; retry only the pending objects."""


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
        self.save_cooldown = lambda deadline: None

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
                 progress=None, max_attempts=6):
        self.user = username(user)
        self._password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
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

    def _defer(self, seconds, message):
        self.not_before = max(self.not_before, time.time() + seconds)
        self.budget.not_before = self.not_before
        self.save_cooldown(self.not_before)
        self.progress(phase="waiting", message=message, retry_at=self.not_before)

    def _request(self, method: str, path: str, *, base_url=BASE_URL, category="livejournal", **kwargs) -> bytes:
        if base_url not in (BASE_URL, LIKES_URL, f"https://{self.user.replace('_', '-')}.livejournal.com"):
            raise ExportError("Неподдерживаемый адрес сервиса ЖЖ.")
        for attempt in range(self.max_attempts):
            self.check()
            if self.budget.limit is not None and self.budget.used >= self.budget.limit:
                self.budget.consume(category)  # Raise before waiting for a request that cannot be sent.
            self._wait(self.not_before - time.time())
            self._wait(self.delay - (time.monotonic() - self._last_request))
            self.budget.consume(category)
            self._last_request = time.monotonic()
            try:
                with self.session.request(method, base_url + path, timeout=(15, 60),
                                          allow_redirects=False, stream=True, **kwargs) as response:
                    if response.status_code in (429, 500, 502, 503, 504):
                        self._defer(backoff(attempt, response.headers.get("Retry-After"), limited=response.status_code == 429),
                                    f"ЖЖ вернул HTTP {response.status_code}. Ждём перед повтором ({attempt + 1}/{self.max_attempts})…")
                        if attempt == self.max_attempts - 1:
                            raise ExportError("ЖЖ временно ограничивает запросы или недоступен. Данные сохранены, продолжите позже.")
                        continue
                    if response.status_code in (401, 403):
                        raise ExportError("ЖЖ отказал в доступе (HTTP 401/403). Проверьте вход в браузере; при ограничении запросов продолжите позже.")
                    if 300 <= response.status_code < 400:
                        raise ExportError("ЖЖ перенаправил запрос. Возможно, требуется вход или подтверждение в браузере.")
                    if response.status_code != 200:
                        raise ExportError(f"ЖЖ вернул HTTP {response.status_code}. Прогресс сохранён.")
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        self.check()
                        size += len(chunk)
                        if size > 64 * 1024 * 1024:
                            raise ExportError("Ответ ЖЖ превышает 64 МБ. Выгрузка остановлена без потери прогресса.")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except requests.exceptions.SSLError as exc:
                raise ExportError("Не удалось проверить TLS-сертификат ЖЖ. Проверьте часы и сертификаты системы; проверка HTTPS не отключалась.") from exc
            except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                self._defer(backoff(attempt), f"Соединение прервалось. Пауза перед повтором ({attempt + 1}/{self.max_attempts})…")
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
            raise ExportError(f"ЖЖ отклонил запрос {method} (код {exc.faultCode}). Проверьте логин, пароль и доступность API.") from exc
        except Exception as exc:
            raise ExportError(f"ЖЖ вернул некорректный ответ на {method}. Прогресс сохранён.") from exc

    def call(self, method: str, **params) -> dict:
        for attempt in range(self.max_attempts):
            try:
                challenge = self._rpc("getchallenge").get("challenge")
                if not isinstance(challenge, str) or not challenge:
                    raise ExportError("ЖЖ не выдал challenge для входа.")
                response = hashlib.md5((challenge + self._password_hash).encode("utf-8")).hexdigest()
                return self._rpc(method, {"username": self.user, "ver": 1, "auth_method": "challenge",
                                          "auth_challenge": challenge, "auth_response": response, **params})
            except APIRateLimited:
                self._defer(backoff(attempt, limited=True), "ЖЖ просит снизить частоту XML-RPC. Ждём и повторим с новым challenge…")
                if attempt == self.max_attempts - 1:
                    raise

    def login(self):
        result = self.call("login", clientversion="Python-LJLocalArchive/0.4", getpickws=1, getpickwurls=1)
        self.userpics = {key: result.get(key) for key in ("pickws", "pickwurls", "defaultpicurl")}
        uid = result.get("userid")
        if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
            self.journal_id = uid
        cookie = self.call("sessiongenerate", expiration="short", ipfixed=1).get("ljsession")
        if not isinstance(cookie, str) or not cookie or any(c in cookie for c in "\r\n;"):
            raise ExportError("ЖЖ не выдал сессию для выгрузки комментариев.")
        self._cookie = cookie

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
            raw = self._request("GET", f"/{int(entities[0]['item'])}.html", category="reaction_authors",
                                base_url=f"https://{self.user.replace('_', '-')}.livejournal.com",
                                headers=headers)
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
        raw = self._request("GET", "/export_comments.bml", params={"get": kind, "startid": start,
                            "numitems": 1000 if kind == "comment_meta" else 500, "props": 1},
                            headers={"Cookie": "ljsession=" + self._cookie})
        return parse_comments(raw), raw

    def close(self):
        self._password_hash = ""
        self._cookie = None
        self._reaction_token = None
        self.session.cookies.clear()
        self.session.close()
