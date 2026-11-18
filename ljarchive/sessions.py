"""Short-lived, in-memory LJ sessions shared with explicit material downloads."""
from copy import deepcopy
import threading
import time


class Sessions:
    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()
        self._browsers = {}

    def browser_for(self, account):
        from .browser_bridge import BrowserBridge
        from .client import username
        from .paths import browser_profiles
        account = username(account)
        with self._lock:
            if account not in self._browsers:
                self._browsers[account] = BrowserBridge(account, browser_profiles() / ('lj-' + account))
            return self._browsers[account]

    @staticmethod
    def key(client):
        return client.account, client.user, client.source['type']

    def remember(self, client):
        client.browser = self.browser_for(client.account)
        if not client._cookie or client.source['access']['status'] != 'verified':
            return
        with self._lock:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if v['expires'] > now}
            if len(self._items) >= 32:
                self._items.pop(next(iter(self._items)))
            self._items[self.key(client)] = {
                'expires': now + 3600, 'cookie': client._cookie,
                'source': deepcopy(client.source), 'cookies': client.session.cookies.copy(),
            }

    def restore(self, client):
        client.browser = self.browser_for(client.account)
        with self._lock:
            item = self._items.get(self.key(client))
            if not item or item['expires'] <= time.monotonic():
                self._items.pop(self.key(client), None)
                return False
            client._cookie = item['cookie']
            client.source = deepcopy(item['source'])
            client.journal_id = item['source'].get('id')
            client.session.cookies.update(item['cookies'])
            return True

    def forget(self, client):
        with self._lock:
            self._items.pop(self.key(client), None)

    def clear(self):
        with self._lock:
            self._items.clear()
            browsers = list(self._browsers.values())
            self._browsers.clear()
        for browser in browsers:
            browser.close()
