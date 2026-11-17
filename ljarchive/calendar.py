"""Publication-day counts; journal dates stay in the journal's own calendar."""
from collections import Counter
from datetime import date

from .storage import atomic_write, dumps


def calendar_data(store):
    days, undated = Counter(), 0
    for (value,) in store.db.execute("SELECT json_extract(payload,'$.eventtime') FROM entries"):
        try:
            day = str(value or '')[:10]
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError:
            undated += 1
        else:
            days[day] += 1
    years = Counter()
    for day, count in days.items():
        years[day[:4]] += count
    return {'days': dict(sorted(days.items())), 'years': dict(sorted(years.items())),
            'total': sum(days.values()) + undated, 'undated': undated}


def write_calendar(store, report):
    from .render import ENV
    data = calendar_data(store)
    atomic_write(store.folder / 'assets/calendar-data.js', 'window.CALENDAR=' + dumps(data) + ';')
    atomic_write(store.folder / 'calendar.html', ENV.get_template('calendar.html').render(
        user=report['username'], report=report))
