"""Optional raster-image download, isolated from the authenticated LJ session."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .storage import atomic_write
from .client import RequestBudget, RequestLimitReached
from .retry import backoff

TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif"}

REASONS = {
    "timeout": "Сервер не ответил вовремя", "connection": "Ошибка соединения",
    "dns": "Не удалось определить адрес сервера",
    "transfer": "Загрузка прервалась", "invalid_url": "Недопустимый адрес",
    "unsupported_type": "Ответ не является поддерживаемым изображением",
    "too_large": "Изображение больше 20 МБ", "empty": "Пустой ответ",
    "redirects": "Слишком много перенаправлений", "storage": "Ошибка сохранения файла",
    "request_limit": "Достигнут лимит запросов", "cancelled": "Загрузка остановлена",
    "paused": "Не запрошено: загрузка изображений приостановлена после повторных ошибок",
    "unknown": "Причина не сохранена; требуется повторная загрузка",
}


def error_message(error):
    if error.get("reason") == "http":
        code = error.get("http_status")
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
    def __init__(self, folder: Path, enabled: bool, cancel: threading.Event, budget=None):
        self.folder, self.enabled, self.cancel = folder, enabled, cancel
        self.mapping, self.errors = {}, {}
        self.budget = budget or RequestBudget()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "LiveJournalLocalArchive/0.4"
        # Never attach credentials from .netrc or the owner's LJ cookie to embeds.
        self.session.trust_env = False
        self.stopped = False
        self.stop_reason = "paused"
        self.attempted = set()
        self.verified_files = set()
        self.used_urls = {"images": set(), "userpics": set()}
        self.videos, self.video_embeds = {}, {}

    def load_state(self, store):
        self.videos, self.video_embeds = store.get('videos', {}), store.get('video_embeds', {})
        self.mapping = {**store.get("media", {}), **self.mapping}
        self.errors = {**store.get("media_errors", {}), **self.errors}
        self.errors = {url: error for url, error in self.errors.items() if not self.mapping.get(url)}

    def save_state(self, store):
        mapping = {**store.get("media", {}), **self.mapping}
        errors = {**store.get("media_errors", {}), **self.errors}
        store.set("media", mapping)
        store.set("media_errors", {url: error for url, error in errors.items() if not mapping.get(url)})

    def record_error(self, url, reason, *, status="failed", http_status=None, requests_made=0, checked_at=None):
        self.errors[url] = {"status": status, "reason": reason, "http_status": http_status,
                            "requests": requests_made, "checked_at": checked_at,
                            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def wait(self, seconds):
        # Short interruptible waits keep cancellation responsive even for long Retry-After.
        while seconds > 0:
            portion = min(30, seconds)
            if self.cancel.wait(portion):
                return False
            seconds -= portion
        return not self.cancel.is_set()

    def defer(self, seconds):
        self.budget.not_before = max(self.budget.not_before, time.time() + seconds)
        self.budget.save_cooldown(self.budget.not_before)
        self.budget.progress(phase="waiting", message="Сервер изображений просит подождать перед повтором…", retry_at=self.budget.not_before)

    def get(self, url: str, *, required=False, category="images") -> str | None:
        self.used_urls[category].add(url)
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
        if self.stopped or self.cancel.is_set():
            if url not in self.errors:
                self.record_error(url, "cancelled" if self.cancel.is_set() else self.stop_reason, status="pending")
            return None
        self.attempted.add(url)
        requests_made, http_status, checked_at = 0, None, None
        reason = "invalid_url"
        try:
            current = url
            for attempt in range(6):
                reason = "invalid_url"
                public_url(current)
                if self.budget.limit is not None and self.budget.used >= self.budget.limit:
                    self.budget.consume(category)
                if not self.wait(max(self.budget.delay, self.budget.not_before - time.time())):
                    self.record_error(url, "cancelled", status="pending", requests_made=requests_made, checked_at=checked_at)
                    return None
                self.budget.consume(category)
                requests_made += 1
                checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                http_status = None
                try:
                    response = self.session.get(current, stream=True, timeout=(10, 20), allow_redirects=False)
                except (requests.Timeout, requests.ConnectionError) as exc:
                    self.defer(backoff(attempt))
                    if attempt == 5:
                        self.stopped = True
                        raise exc
                    continue
                with response:
                    http_status = response.status_code
                    if response.status_code in (429, 500, 502, 503, 504):
                        self.defer(backoff(attempt, response.headers.get("Retry-After"), limited=response.status_code == 429))
                        if attempt == 5:
                            self.stopped = True
                            response.raise_for_status()
                        continue
                    if 300 <= response.status_code < 400:
                        current = urljoin(current, response.headers.get("Location", ""))
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
                        self.defer(backoff(attempt))
                        if attempt == 5:
                            self.stopped = True
                            raise
                        continue
                    if not size:
                        reason = "empty"
                        raise ValueError("empty image")
                    path = "media/" + digest + ext
                    reason = "storage"
                    atomic_write(self.folder / path, b"".join(chunks))
                    self.mapping[url] = path
                    self.errors.pop(url, None)
                    return path
            reason = "redirects"
            raise ValueError("too many redirects")
        except RequestLimitReached:
            self.stopped = True
            self.stop_reason = "request_limit"
            self.record_error(url, "request_limit", status="pending", http_status=http_status,
                              requests_made=requests_made, checked_at=checked_at)
            return None
        except (requests.RequestException, ValueError, OSError) as exc:
            if isinstance(exc, requests.HTTPError):
                reason = "http"
            elif isinstance(exc, requests.Timeout):
                reason = "timeout"
            elif isinstance(exc, requests.ConnectionError):
                reason = "connection"
            elif isinstance(exc, requests.RequestException):
                reason = "transfer"
            elif isinstance(exc, socket.gaierror):
                reason = "dns"
            self.record_error(url, reason, http_status=http_status, requests_made=requests_made, checked_at=checked_at)
            return None

    def close(self):
        self.session.close()
