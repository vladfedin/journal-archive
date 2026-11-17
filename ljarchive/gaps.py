"""Finite retrieval: one collection pass and a few repeats, counted across runs.

A gap is anything LJ did not return: an image, a discussion, a list of reaction
authors. Each answered attempt is counted in the record that owns the gap. After
ATTEMPTS answered attempts the gap is closed: the archive treats the item as absent,
keeps the last reason and earlier saved text, and does not request it again unless
the user explicitly refreshes that entry. Cancellation and request limits are not
answers and do not count.
"""
from datetime import datetime, timezone

RETRIES = 2
ATTEMPTS = 1 + RETRIES
FIELDS = ('attempts', 'closed', 'closed_at')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def fail(record, previous=None):
    """Count one answered, unsuccessful attempt; close the gap after the last one."""
    attempts = int((previous if previous is not None else record).get('attempts', 0) or 0) + 1
    record['attempts'] = attempts
    record['closed'] = attempts >= ATTEMPTS
    if record['closed']:
        record['closed_at'] = (previous or {}).get('closed_at') or record.get('closed_at') or now()
    else:
        record.pop('closed_at', None)
    return record


def carry(record, previous):
    """Keep the attempt count when a record is rebuilt without a new answer."""
    for key in FIELDS:
        if previous and key in previous:
            record[key] = previous[key]
    return record


def closed(record):
    return bool(record) and bool(record.get('closed'))


def reopen(record):
    for key in FIELDS:
        record.pop(key, None)
    return record
