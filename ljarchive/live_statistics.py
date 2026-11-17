"""Throttled background snapshots; no writes to an archive owned by an exporter."""
from datetime import datetime, timezone
import threading
import time

from .statistics import statistics, statistics_source
from .storage import Store


def revision(folder):
    parts = []
    for name in ('archive.sqlite3', 'archive.sqlite3-wal'):
        try:
            stat = (folder / name).stat()
            parts.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            parts.append(None)
    return tuple(parts)


class LiveStatistics:
    def __init__(self, interval=30):
        self.interval, self.lock, self.records = interval, threading.Lock(), {}

    def get(self, folder, *, active=False):
        key = str(folder.resolve())
        current = revision(folder)
        with self.lock:
            record = self.records.setdefault(key, {'phase': 'empty', 'data': None, 'version': '', 'checked': 0})
            ended = record.get('active') and not active
            record['active'] = active
            due = time.monotonic() - record['checked'] >= self.interval
            changed = current != record.get('revision')
            if record['phase'] != 'updating' and (record['phase'] == 'empty' or ended or due and (changed or record['phase'] == 'error')):
                record.update(phase='updating', checked=time.monotonic())
                def worker():
                    try:
                        store = Store.reader(folder)
                        try:
                            captured = datetime.now(timezone.utc).isoformat()
                            source = statistics_source(store)
                        finally:
                            store.close()
                        # Release the read transaction before the potentially expensive calculation.
                        data = statistics(source)
                        data['generated_at'] = captured
                        with self.lock:
                            record.update(data=data, version=str(time.time_ns()), revision=current, phase='ready')
                    except Exception:
                        with self.lock:
                            record.update(phase='error')
                record['thread'] = threading.Thread(target=worker, daemon=True)
                record['thread'].start()
            # Keep only a few idle archives in memory; an open page can request a fresh snapshot.
            for old in list(self.records):
                if len(self.records) <= 4:
                    break
                if old != key and self.records[old]['phase'] != 'updating':
                    del self.records[old]
            return {name: value for name, value in record.items() if name not in ('thread', 'revision', 'checked')}


def public_statistics(data):
    return {key: value for key, value in data.items() if key != 'discussions'}


def discussion_statistics(data):
    return {**data.get('discussions', {'posts': [], 'comments': []}), 'generated_at': data.get('generated_at'),
            'people': [{'id': p['id'], 'name': p['name']} for p in data['people']]}
