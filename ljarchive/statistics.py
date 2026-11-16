"""Deterministic, offline statistics derived from the portable source snapshot."""
from collections import defaultdict
from datetime import datetime, timezone
from html import unescape
import re


def date_of(record, field='date'):
    value = record.get(field)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
        except ValueError:
            pass
    if field == 'date' and record.get('datepostunix'):
        try:
            return datetime.fromtimestamp(int(record['datepostunix']), timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def title_of(entry):
    return unescape(re.sub(r'<[^>]*>', '', str(entry.get('subject') or 'Без заголовка'))).strip()


def statistics(data):
    entries = {int(e['itemid']): e for e in data.get('entries', [])}
    comments = {int(c['id']): c for c in data.get('comments', [])}
    journal = data.get('journal', {})
    names = {int(u['id']): u.get('username') for u in data.get('users', [])}
    owner_name = (journal.get('username') or '').lower().replace('-', '_')
    owner_id = journal.get('id')
    people, yearly, edges = {}, {}, {}
    events, by_entry = defaultdict(list), defaultdict(list)
    unknown_dates = anonymous = deleted = dangling = 0

    def author(c):
        uid = int(c.get('posterid') or 0)
        name = c.get('user') or names.get(uid)
        if not uid and not name:
            return None
        canonical = (name or '').lower().replace('-', '_')
        owner = bool((owner_id and uid == int(owner_id)) or (canonical and canonical == owner_name))
        key = 'owner' if owner else f'user:{uid}' if uid else 'name:' + canonical
        if key not in people:
            people[key] = {'id': key, 'name': name or f'Пользователь #{uid}', 'owner': owner,
                           'comments': 0, 'posts': set(), 'years': {}, 'first': None, 'last': None}
        return key

    authors = {cid: author(c) for cid, c in comments.items()}
    for cid, c in comments.items():
        eid = int(c.get('jitemid') or 0)
        day, person = date_of(c), authors[cid]
        year = str(day.year) if day else None
        link = f'entries/{eid}.html#comment-{cid}'
        deleted += c.get('state') in ('D', 'X')
        unknown_dates += day is None
        anonymous += person is None
        if year:
            totals = yearly.setdefault(year, {'comments': 0, 'identified': 0, 'posts': set()})
            totals['comments'] += 1
            totals['identified'] += person is not None
            totals['posts'].add(eid)
        if person:
            p = people[person]
            p['comments'] += 1
            p['posts'].add(eid)
            if day:
                stamp = day.isoformat()
                p['first'] = min(p['first'] or stamp, stamp)
                p['last'] = max(p['last'] or stamp, stamp)
                bucket = p['years'].setdefault(year, {'comments': 0, 'posts': set(), 'url': link})
                bucket['comments'] += 1
                bucket['posts'].add(eid)
                events[person].append((day, cid, eid))
        if day:
            by_entry[eid].append((day, cid, person))
        parent = comments.get(int(c.get('parentid') or 0))
        if c.get('parentid') and (parent is None or int(parent.get('jitemid') or 0) != eid):
            dangling += 1
            continue
        target = authors[parent['id']] if parent else None
        if person and target and person != target:
            edge = edges.setdefault((person, target), {'source': person, 'target': target,
                                                       'count': 0, 'years': {}, 'examples': []})
            edge['count'] += 1
            bucket = edge['years'].setdefault(year or 'unknown', {'count': 0, 'url': link})
            bucket['count'] += 1
            if len(edge['examples']) < 5:
                edge['examples'].append(link)

    for p in people.values():
        p['posts'] = len(p['posts'])
        for bucket in p['years'].values():
            bucket['posts'] = len(bucket['posts'])
    for totals in yearly.values():
        totals['posts'] = len(totals['posts'])
    revivals = []
    for eid, rows in by_entry.items():
        if eid not in entries:
            continue
        rows.sort()
        published = date_of(entries[eid], 'eventtime')
        previous_day, previous_id = published, None
        best = None
        for day, cid, person in rows:
            if previous_day and (day - previous_day).days >= 365:
                finding = {'entry': eid, 'title': title_of(entries[eid]), 'days': (day - previous_day).days,
                           'date': day.isoformat(), 'person': person,
                           'url': f'entries/{eid}.html#comment-{cid}',
                           'previous_url': f'entries/{eid}.html' + (f'#comment-{previous_id}' if previous_id else ''),
                           'first_comment': previous_id is None}
                if best is None or finding['days'] > best['days']:
                    best = finding
            previous_day, previous_id = day, cid
        if best:
            revivals.append(best)
    returns = []
    for person, rows in events.items():
        rows.sort()
        pairs = [(b[0] - a[0], a, b) for a, b in zip(rows, rows[1:])]
        if not pairs:
            continue
        gap, a, b = max(pairs)
        if gap.days >= 365:
            returns.append({'person': person, 'days': gap.days, 'date': b[0].isoformat(),
                            'url': f'entries/{b[2]}.html#comment-{b[1]}',
                            'previous_url': f'entries/{a[2]}.html#comment-{a[1]}'})
    coverage = data.get('coverage', {})
    discussions = coverage.get('discussions', {})
    confirmed = sum(bool(discussions.get(str(eid), {}).get('done')) for eid in entries)
    # A period refresh may be complete while other discussions remain unknown.
    complete = confirmed == len(entries) if discussions else bool(coverage.get('comments_done'))
    post_years = [d.year for e in entries.values() if (d := date_of(e, 'eventtime'))]
    years = sorted(set(map(int, yearly)) | set(post_years))
    years = list(range(years[0], years[-1] + 1)) if years and years[-1] - years[0] <= 300 else years
    return {'schema_version': 1, 'journal': journal, 'years': list(map(str, years)), 'yearly': yearly,
            'totals': {'posts': len(entries), 'comments': len(comments), 'people': len(people),
                       'deleted': deleted, 'unknown_dates': unknown_dates, 'anonymous': anonymous,
                       'missing_parents': dangling},
            'coverage': {'discussions_complete': complete, 'confirmed_posts': confirmed,
                         'posts_complete': bool(coverage.get('posts_done')), 'scope': data.get('scope', {})},
            'people': sorted(people.values(), key=lambda p: (-p['comments'], p['id'])),
            'edges': sorted(edges.values(), key=lambda e: (-e['count'], e['source'], e['target'])),
            'revivals': sorted(revivals, key=lambda r: (-r['days'], r['entry']))[:30],
            'returns': sorted(returns, key=lambda r: (-r['days'], r['person']))[:30]}


def write_statistics(store, report):
    from .render import ENV
    from .storage import atomic_write, dumps
    data = statistics({'journal': {'username': store.get('username'), 'id': store.get('journal_id')},
                       'entries': store.entries(), 'comments': store.comments(),
                       'scope': {'entry_limit': store.get('entry_limit'),
                                 'last_sync': store.get('sync_job', {}).get('scope')},
                       'coverage': {'posts_done': store.get('posts_done'),
                                    'comments_done': store.get('bodies_done'),
                                    'discussions': store.get('entry_comments', {})}})
    atomic_write(store.folder / 'data' / 'statistics.json', dumps(data) + '\n')
    atomic_write(store.folder / 'assets' / 'statistics-data.js',
                 'window.ARCHIVE_STATS=' + dumps(data).replace('<', '\\u003c') + ';')
    atomic_write(store.folder / 'statistics.html', ENV.get_template('statistics.html').render(
        user=report['username'], report=report, stats=data))
