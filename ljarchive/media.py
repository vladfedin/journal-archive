"""Optional raster-image download, isolated from the authenticated LJ session."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .storage import atomic_write
from .client import RequestBudget, RequestLimitReached
from .retry import backoff

TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif"}


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
        self.mapping, self.failed = {}, []
        self.budget = budget or RequestBudget()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "LiveJournalLocalArchive/0.3"
        # Never attach credentials from .netrc or the owner's LJ cookie to embeds.
        self.session.trust_env = False
        self.blocked_hosts = set()

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

    def get(self, url: str) -> str | None:
        if url in self.mapping:
            return self.mapping[url]
        digest = hashlib.sha256(url.encode()).hexdigest()
        existing = next((self.folder / "media").glob(digest + ".*"), None)
        if existing and existing.suffix in TYPES.values():
            self.mapping[url] = "media/" + existing.name
            return self.mapping[url]
        self.mapping[url] = None
        if not self.enabled or self.cancel.is_set():
            return None
        try:
            current = url
            for attempt in range(6):
                public_url(current)
                if urlsplit(current).hostname in self.blocked_hosts:
                    raise ValueError("host denied access")
                if self.budget.limit is not None and self.budget.used >= self.budget.limit:
                    self.budget.consume("images")
                if not self.wait(max(self.budget.delay, self.budget.not_before - time.time())):
                    return None
                self.budget.consume("images")
                try:
                    response = self.session.get(current, stream=True, timeout=(10, 20), allow_redirects=False)
                except (requests.Timeout, requests.ConnectionError) as exc:
                    self.defer(backoff(attempt))
                    if attempt == 5:
                        raise exc
                    continue
                with response:
                    if response.status_code in (429, 500, 502, 503, 504):
                        self.defer(backoff(attempt, response.headers.get("Retry-After"), limited=response.status_code == 429))
                        if attempt == 5:
                            self.enabled = False
                        continue
                    if response.status_code in (401, 403):
                        self.blocked_hosts.add(urlsplit(current).hostname)
                    if 300 <= response.status_code < 400:
                        current = urljoin(current, response.headers.get("Location", ""))
                        continue
                    response.raise_for_status()
                    ext = TYPES.get(response.headers.get("Content-Type", "").split(";")[0].lower())
                    if not ext:
                        raise ValueError("not a supported raster image")
                    chunks, size = [], 0
                    try:
                        for chunk in response.iter_content(65536):
                            if self.cancel.is_set():
                                return None
                            size += len(chunk)
                            if size > 20 * 1024 * 1024:
                                raise ValueError("image over 20 MB")
                            chunks.append(chunk)
                    except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
                        self.defer(backoff(attempt))
                        if attempt == 5:
                            self.enabled = False
                        continue
                    if not size:
                        raise ValueError("empty image")
                    path = "media/" + digest + ext
                    atomic_write(self.folder / path, b"".join(chunks))
                    self.mapping[url] = path
                    return path
            raise ValueError("too many redirects")
        except RequestLimitReached:
            self.enabled = False
            return None
        except (requests.RequestException, ValueError, OSError):
            self.failed.append(url)
            return None

    def close(self):
        self.session.close()
