"""Polite, finite retries, including the server's full Retry-After deadline."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def retry_after(value, *, now=None):
    value = str(value or "").strip()
    if value.isdigit():
        return float(value)
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0.0, deadline.timestamp() - (now if now is not None else datetime.now(timezone.utc).timestamp()))
    except (ValueError, TypeError, OverflowError):
        return 0.0


def backoff(attempt, header="", *, limited=False):
    return max(retry_after(header), min(8, (2 if limited else 1) * 2 ** attempt))


def restored_deadline(store):
    """Keep server deadlines; discard obsolete long local backoffs on upgrade."""
    import time
    deadline = store.get('request_not_before', 0)
    info = store.get('request_wait', {})
    if info.get('server_delay') or info.get('reason') == 'restored' or not info:
        return deadline
    return min(deadline, time.time() + 8) if info.get('reason') != 'incomplete' else 0
