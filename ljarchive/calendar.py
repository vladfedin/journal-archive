"""Compact publication calendar, with links and observed discussion coverage."""
from collections import Counter
from datetime import date

from .storage import atomic_write, dumps


def publication_day(value):
    """Keep the journal's date, without converting its local midnight to UTC."""
    day = str(value or '')[:10]
    try:
        return day if date.fromisoformat(day).isoformat() == day else None
    except ValueError:
        return None


def calendar_data(store):
    from .progress import timeline
    source = timeline(store)
    days, years, entries, undated = Counter(), Counter(), [], 0
    for item in source['entries']:
        day = publication_day(item['date'])
        if day:
            days[day] += 1
            years[day[:4]] += 1
        else:
            undated += 1
        # Calendar data contains no post/comment HTML or media URLs.
        entries.append({key: item[key] for key in (
            'id', 'date', 'title', 'url', 'comments', 'expected_comments',
            'comments_complete', 'repost', 'state', 'notice', 'error')})
    missing = {g['date']: g['count'] for g in source['gaps'] if publication_day(g['date'])}
    for day in missing:
        years.setdefault(day[:4], 0)
    return {'schema_version': 2, 'days': dict(sorted(days.items())),
            'years': dict(sorted(years.items())), 'entries': entries,
            'missing': missing, 'total': len(entries), 'undated': undated,
            'discovery_complete': bool(store.get('days_loaded', False)),
            'partial': bool(missing or any(e['state'] != 'ready' for e in entries))}


def calendar_script(store):
    return 'window.CALENDAR=' + dumps(calendar_data(store)).replace('<', '\\u003c') + ';'


def write_calendar(store, report):
    from .render import ENV
    atomic_write(store.folder / 'assets/calendar-data.js', calendar_script(store))
    atomic_write(store.folder / 'calendar.html', ENV.get_template('calendar.html').render(
        user=report['username'], report=report))
