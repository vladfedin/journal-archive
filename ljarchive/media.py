"""Optional raster-image download, isolated from the authenticated LJ session."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .storage import atomic_write
from .client import RequestBudget, RequestLimitReached

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
        self.session.headers["User-Agent"] = "LiveJournalLocalArchive/0.1"
        # Never attach credentials from .netrc or the owner's LJ cookie to embeds.
        self.session.trust_env = False

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
            for _ in range(6):
                public_url(current)
                # Media requests are intentionally paced as well.
                if self.cancel.wait(0.3):
                    return None
                self.budget.consume("images")
                with self.session.get(current, stream=True, timeout=(10, 20), allow_redirects=False) as response:
                    if 300 <= response.status_code < 400:
                        current = urljoin(current, response.headers.get("Location", ""))
                        continue
                    response.raise_for_status()
                    ext = TYPES.get(response.headers.get("Content-Type", "").split(";")[0].lower())
                    if not ext:
                        raise ValueError("not a supported raster image")
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        if self.cancel.is_set():
                            return None
                        size += len(chunk)
                        if size > 20 * 1024 * 1024:
                            raise ValueError("image over 20 MB")
                        chunks.append(chunk)
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
