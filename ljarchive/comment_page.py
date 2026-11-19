"""Read deletion markers from the same per-entry data used by the LJ web UI."""
import json
import re
from html.parser import HTMLParser


class _CommentPage(HTMLParser):
    def __init__(self, journal, ditemid):
        super().__init__()
        self.journal = journal
        self.ditemid = ditemid
        self.deleted = set()
        self.inline_script = False

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.inline_script = 'src' not in dict(attrs)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.inline_script = False

    def handle_data(self, source):
        if not self.inline_script:
            return
        data = {}
        for match in re.finditer(r'^\s*Site\.(entry|page)\s*=\s*(?=\{)', source, re.M):
            try:
                data[match[1]], _ = json.JSONDecoder().raw_decode(source, match.end())
            except (ValueError, RecursionError):
                return
        entry = data.get('entry', {})
        if entry.get('ditemid') != self.ditemid or entry.get('journal') != self.journal:
            return
        nodes = data.get('page', {}).get('comments')
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict) or node.get('deleted') not in (1, '1'):
                continue
            tid = node.get('dtalkid')
            if (type(tid) is int and tid >= 256 and tid % 256 == self.ditemid % 256
                    and node.get('talkid', tid // 256) == tid // 256):
                self.deleted.add(tid)


def deleted_threads(html, journal, ditemid):
    parser = _CommentPage(journal, ditemid)
    try:
        parser.feed(html.decode('utf-8', errors='replace'))
        parser.close()
    except (ValueError, AssertionError):
        return set()
    return parser.deleted
