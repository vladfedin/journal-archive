"""Polite, finite retries, including the server's full Retry-After deadline."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class SourceCoolingDown(BaseException):
    """Leave a nested browser/player read before its wait; do not count a failed attempt."""


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


def restore_cooldowns(client, store):
    """Older checkpoints have one wait; preserve its host, or its global scope if unknown."""
    import time
    budget = client.budget
    now = time.time()
    budget.cooldowns = {host: info for host, info in store.get('request_cooldowns', {}).items()
                        if info.get('retry_at', 0) > now}
    deadline = restored_deadline(store)
    if deadline > now:
        info = {'reason': 'restored', **store.get('request_wait', {}), 'retry_at': deadline}
        host = info.get('host') or ''
        if budget.cooldowns.get(host, {}).get('retry_at', 0) < deadline:
            budget.cooldowns[host] = info

    def save(deadline):
        with store.db:
            store.set('request_cooldowns', budget.cooldowns)
            store.set('request_not_before', deadline)
            store.set('request_wait', budget.wait_info)
    client.save_cooldown = budget.save_cooldown = save
