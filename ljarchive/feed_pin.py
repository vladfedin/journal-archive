"""The upper-entry icon in LiveJournal's Expressive journal layout."""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit


class _FeedPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.site = {}
        self.inline_script = False
        self.card_id = None
        self.card_depth = 0
        self.header_depth = 0
        self.header = False
        self.finished_header = False
        self.icons = False
        self.pinned = False
        self.link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get('class') or '').split())
        if tag == 'script':
            self.inline_script = 'src' not in attrs
        if self.finished_header:
            return
        if tag == 'div':
            if self.card_id is None and 'post-asset' in classes:
                self.card_id = attrs.get('id') or ''
                self.card_depth = 1
            elif self.card_depth:
                self.card_depth += 1
                if self.header_depth:
                    self.header_depth += 1
                elif 'asset-header' in classes:
                    self.header_depth = 1
                elif classes.intersection({'asset-body', 'asset-content', 'post-asset'}):
                    self.finished_header = True
        elif tag == 'h2' and self.header_depth and 'asset-name' in classes:
            self.header = True
        elif self.header:
            if tag == 'span' and 'lj-entry-securityicon' in classes:
                self.icons = True
            elif self.icons and tag == 'img':
                src = urlsplit(attrs.get('src') or '')
                remote_icon = src.hostname == 'l-stat.livejournal.net' and src.path == '/img/icon_sticky.png'
                local_icon = (not src.netloc and src.scheme in ('', 'file')
                              and src.path.rsplit('/', 1)[-1] == 'icon_sticky.png'
                              and attrs.get('alt') == attrs.get('title') == 'Верхняя запись')
                if remote_icon or local_icon:
                    self.pinned = True
            elif tag == 'a' and 'subj-link' in classes:
                self.link = attrs.get('href')

    def handle_endtag(self, tag):
        if tag == 'script':
            self.inline_script = False
        elif tag == 'div' and self.card_depth and not self.finished_header:
            self.card_depth -= 1
            if self.header_depth:
                self.header_depth -= 1
                if not self.header_depth:
                    self.finished_header = True
            if not self.card_depth:
                self.finished_header = True
        elif tag == 'span':
            self.icons = False
        elif tag == 'h2' and self.header:
            self.header = False
            self.finished_header = True

    def handle_data(self, text):
        if self.inline_script:
            for m in re.finditer(r'^\s*Site\.(page|journal|remote)\s*=\s*(?=\{)', text, re.M):
                self.site[m[1]], _ = json.JSONDecoder().raw_decode(text, m.end())


def page_pin(html, journal, journal_id, account, *, requested_home=False):
    """Return the full display ID, zero on a verified unpin, or None if unknown."""
    page = _FeedPage()
    try:
        page.feed(html.decode('utf-8', errors='replace'))
        page.close()
        site = page.site
        if not isinstance(site.get('remote'), dict) or site['remote'].get('username') != account:
            if requested_home:
                from .client import AuthenticationRequired
                raise AuthenticationRequired('ЖЖ не подтвердил веб-вход для просмотра журнала.')
            return None
        if (site['journal']['username'] != journal or site['journal']['id'] != journal_id
                or site['page']['styleLayout'] != 'Expressive'):
            return None
        first = site['page']['entries'][0]
        did = first['itemid']
        if (type(did) is not int or not 256 <= did < 2**63 or first['journalid'] != journal_id
                or not page.finished_header or page.card_id != f'asset-{journal}-{did}'
                or not page.link or page.link != first['url']
                or page.link != f"https://{journal.replace('_', '-')}.livejournal.com/{did}.html"):
            return None
        if page.pinned:
            return did
        # Offline HTML can be a later page of the same journal. Only our own
        # request for its front page is evidence that a previous pin disappeared.
        return 0 if requested_home else None
    except (ValueError, KeyError, IndexError, TypeError, RecursionError, AssertionError):
        return None
