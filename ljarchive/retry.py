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
    return max(retry_after(header), min(3600, (60 if limited else 15) * 2 ** attempt))
