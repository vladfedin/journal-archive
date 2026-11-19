"""Documents shared by library search and the portable archive."""
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from .comment_state import comment_view
from .entries import author, is_repost
from .personal import preference_rows, profile_fields
from .storage import atomic_write, dumps

JOURNAL_PARTS = tuple(f'journal-{i}' for i in range(8))
PARTS = (*JOURNAL_PARTS, 'personal', 'materials')
WORDS = re.compile(r'[^\W_]+')


def fold(text):
    return text.lower().replace('ё', 'е')
ACCESS = {'private': 'Личная', 'usemask': 'Для друзей / группы'}


class VisibleText(HTMLParser):
    blocks = {'p', 'div', 'br', 'li', 'td', 'tr', 'blockquote', 'section', 'lj-cut', 'hr'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden = True
        elif tag in self.blocks:
            self.parts.append(' ')
        elif tag == 'lj' and not self.hidden:
            self.parts.append(' ' + (dict(attrs).get('user') or '') + ' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = False
        elif tag in self.blocks:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def visible(value):
    if not isinstance(value, str):
        return ''
    if '<' in value or '&' in value:
        parser = VisibleText()
        try:
            parser.feed(value)
            parser.close()
            value = ''.join(parser.parts)
        except (ValueError, AssertionError):
            pass
    return ' '.join(value.split())


def reaction_names(item):
    authors = ((item.get('reactions') or {}).get('authors') or {}).get('users') or []
    names = [a['username'] for a in authors if isinstance(a, dict) and a.get('username')]
    return 'Отреагировали: ' + ', '.join(names) if names else ''


def doc(kind, href, title, text, *, who='', date='', access='', context='', tags=''):
    return dict(kind=kind, href=href, title=title, text=text, author=who or '', date=date or '',
                access=access, context=context, tags=tags)


def journal_docs(user, entries, comments):
    from .render import CURRENT
    parents = {}
    for entry in entries:
        eid = int(entry['itemid'])
        title = visible(entry.get('subject')) or 'Без заголовка'
        access = ACCESS.get(entry.get('security'), '')
        props = entry.get('props') or {}
        tags = props.get('taglist') or ''
        if isinstance(tags, list):
            tags = ', '.join(tags)
        parents[eid] = title, access, is_repost(entry)
        body = [visible(entry.get('event'))]
        body.extend(visible(props[key]) for key, _ in CURRENT if props.get(key))
        body.append(reaction_names(entry))
        yield doc('post', f'entries/{eid}.html', title, ' · '.join(filter(None, body)),
                  who=author(entry, user), date=entry.get('eventtime', ''), access=access, tags=tags)
    for c in comments:
        eid = int(c.get('jitemid') or 0)
        title, access, repost = parents.get(eid, (f'Запись №{eid}', '', False))
        if repost:
            continue
        label = comment_view(c)['state_label']
        yield doc('comment', f'entries/{eid}.html#comment-{int(c["id"])}', visible(c.get('subject')),
                  ' · '.join(filter(None, (visible(c.get('body')), reaction_names(c)))), who=c.get('user') or 'Аноним', date=c.get('date') or '',
                  context=title, access=' · '.join(filter(None, (access, label))))


def personal_docs(user, data):
    sections = data.get('sections', {})
    profile = sections.get('profile', {}).get('data') or {}
    fields = profile_fields(sections)
    if fields or profile:
        yield doc('profile', 'account.html', profile.get('fullname') or user,
                  ' '.join(f"{r['label']} {r['value']}" for r in fields), who=user)
    friends = sections.get('friends', {}).get('data') or {}
    for key, label in [('friends','Друзья и подписки'), ('friendofs','Добавили в друзья')]:
        for person in friends.get(key, []):
            yield doc('profile', 'account.html#'+key, person.get('username') or '', person.get('fullname') or '', context=label)
    for key, rows in preference_rows(sections).items():
        for row in rows:
            yield doc('profile', 'account.html#'+key, row['label'], str(row['value']), who=user)
    for key, items in [('Группы друзей', friends.get('friendgroups', profile.get('friendgroups', []))),
                       ('Метки', (sections.get('tags', {}).get('data') or {}).get('tags', []))]:
        names = [str(i['name']) for i in items if isinstance(i, dict) and i.get('name')]
        if names:
            yield doc('profile', 'account.html', key, ', '.join(names), who=user)
    messages = sorted(data.get('messages', []), key=lambda m: (m.get('when', 0), m.get('qid', 0)), reverse=True)
    for n, m in enumerate(messages):
        page = n // 100 + 1
        path = 'messages.html' if page == 1 else f'messages-{page}.html'
        yield doc('message', f'{path}#message-{int(m["qid"])}', visible(m.get('subject')),
                  visible(m.get('body')), who=m.get('from') or (user if m.get('type') != 18 else ''),
                  context=' → '.join(filter(None,(m.get('from'),m.get('to')))),
                  date=datetime.fromtimestamp(m['when'], timezone.utc).isoformat(), access='Переписка', tags=m.get('to') or '')


def material_docs(rows, entries, comments, user):
    parents = {int(e['itemid']): (ACCESS.get(e.get('security'), ''), author(e, user)) for e in entries}
    states = {int(c['id']): comment_view(c)['state_label'] for c in comments if c.get('state') != 'A'}
    for row in rows:
        url = row['url']
        filename = unquote(urlsplit(url).path.split('/')[-1]) if row.get('source_href') else ''
        for ref in row['references'] or [{}]:
            access, owner = parents.get(ref.get('entry_id'), ('', ''))
            title = row.get('title') if row.get('title') != ref.get('title') else ''
            caption = ref.get('caption') or title or filename or 'Материал'
            who = (ref.get('author') or 'Аноним') if ref.get('comment_id') else (owner or user)
            yield doc('material', ref.get('href') or 'account.html', caption,
                      ' '.join(filter(None,(filename, url, row.get('path')))), who=who,
                      date=ref.get('date', ''), context=ref.get('title', ''), tags=', '.join(ref.get('tags', [])),
                      access=' · '.join(filter(None, (access, states.get(ref.get('comment_id'))))))


def write_part(folder, part, docs):
    docs = list(docs)
    postings = defaultdict(list)
    for i, d in enumerate(docs):
        text = ' '.join(d[k] or '' for k in ('title','text','author','tags'))
        for word in sorted(set(WORDS.findall(fold(text)))):
            postings[word].append(i)
    payload = 'window.searchPart(' + dumps({'docs': docs, 'postings': postings}).replace('<', '\\u003c') + ');'
    path = folder / 'assets' / f'fulltext-{part}.js'
    if not path.exists() or path.read_text(encoding='utf8') != payload:
        atomic_write(path, payload)


def write_journal(store, entries, comments, changed):
    shard_count = len(JOURNAL_PARTS)
    dirty = set(range(shard_count)) if changed is None else {eid % shard_count for eid in changed}
    for i, part in enumerate(JOURNAL_PARTS):
        if i in dirty or not (store.folder/'assets'/f'fulltext-{part}.js').exists():
            posts = [e for e in entries if int(e['itemid']) % shard_count == i]
            replies = [c for c in comments if int(c.get('jitemid') or 0) % shard_count == i]
            write_part(store.folder, part, journal_docs(store.get('username') or store.folder.name, posts, replies))


def write_page(folder, report):
    from .render import ENV, ROOT
    shutil.copyfile(ROOT / 'static/fulltext.js', folder / 'assets/fulltext.js')
    atomic_write(folder/'search.html', ENV.get_template('search.html').render(user=report['username'], report=report, parts=PARTS))
