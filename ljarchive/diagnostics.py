"""Allowlisted diagnostics. Source text, addresses and credentials stay local."""
from collections import Counter
from datetime import datetime, timezone
import math
import platform
import re
import sqlite3

from . import __version__
from .storage import Store

REASONS = {'http', 'timeout', 'connection', 'transfer', 'dns', 'invalid_url', 'unsupported_type',
           'too_large', 'empty', 'storage', 'redirects', 'host_paused', 'request_limit', 'cancelled',
           'paused', 'unknown', 'download', 'embed_unresolved', 'not_requested', 'local_missing',
           'api_limit', 'incomplete', 'restored', 'tls'}
PHASES = {'idle', 'preparing', 'verify', 'login', 'posts', 'comments', 'reactions', 'videos',
          'materials', 'personal', 'userpics', 'render', 'waiting', 'retrying', 'loading',
          'complete', 'partial', 'cancelled', 'error', 'demo'}
CATEGORIES = {'livejournal', 'likes', 'reactions', 'reaction_authors', 'images', 'userpics', 'videos', 'personal'}


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def date(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).isoformat()
    except (AttributeError, ValueError, TypeError):
        return None


def event(value):
    result = {key: number(value[key]) for key in ('at', 'retry_at', 'attempt', 'max_attempts', 'http_status') if key in value}
    for key, allowed in (('kind', {'request', 'success', 'failure', 'wait'}), ('reason', REASONS), ('category', CATEGORIES)):
        if key in value:
            result[key] = value[key] if value[key] in allowed else 'unknown'
    if type(value.get('server_delay')) is bool:
        result['server_delay'] = value['server_delay']
    return result


def parameters(value):
    result = {key: value[key] for key in ('images', 'videos', 'account', 'messages') if type(value.get(key)) is bool}
    result.update({key: number(value[key]) for key in ('delay', 'max_requests', 'entry_limit') if key in value})
    if value.get('mode') in ('full', 'period', 'urls', 'latest'):
        result['mode'] = value['mode']
    for key in ('date_from', 'date_to'):
        if value.get(key):
            result[key] = date(value[key])
    result['selected_url_count'] = len(value.get('urls') or [])
    return result


def build_diagnostics(folder, state, params):
    result = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'application': {'version': __version__, 'os': platform.system(), 'architecture': platform.machine()},
              'parameters': parameters(params), 'run': {}, 'inventory_available': False, 'materials': []}
    run = result['run']
    version = state.get('app_version')
    run['application_version'] = version if isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+', version) else None
    run['phase'] = state.get('phase') if state.get('phase') in PHASES else 'unknown'
    for key in ('started_at', 'server_time', 'last_progress_at', 'last_success_at', 'last_saved_at', 'working_seconds',
                'waiting_seconds', 'retry_at', 'requests_used', 'request_limit', 'entries', 'comments'):
        if key in state:
            run[key] = number(state[key])
    run['worker_alive'] = state.get('worker_alive') if type(state.get('worker_alive')) is bool else None
    run['requests'] = {key: number(value) for key, value in state.get('request_categories', {}).items() if key in CATEGORIES}
    run['events'] = [event(e) for e in state.get('events', [])[-30:]]
    if state.get('wait'):
        run['wait'] = event(state['wait'])
    try:
        store = Store.reader(folder)
        try:
            result['counts'] = store.counts()
            settings = {key: store.get(key, {}) for key in ('entry_materials', 'media', 'media_errors', 'videos', 'entry_comments')}
            requests = store.get('last_request_stats', {}).get('request_categories', {})
            if not run['requests']:
                run['requests'] = {k: number(v) for k, v in requests.items() if k in CATEGORIES}
        finally:
            store.close()
    except (OSError, sqlite3.Error):
        return result
    result['inventory_available'] = True
    references = {}
    for eid, record in settings['entry_materials'].items():
        if not str(eid).isdigit():
            continue
        for url in record.get('urls', []):
            references.setdefault(url, set()).add(int(eid))
    for index, url in enumerate(sorted(set(settings['media']) | set(settings['media_errors'])), 1):
        saved = settings['media'].get(url)
        if saved:
            target = (folder / saved).resolve()
            if target.is_relative_to(folder.resolve()) and target.is_file():
                continue
        error = settings['media_errors'].get(url, {})
        reason = 'local_missing' if saved else error.get('reason', 'not_requested' if params.get('images') is False else 'unknown')
        result['materials'].append({'id': f'image-{index}', 'kind': 'image',
            'reason': reason if reason in REASONS else 'unknown', 'http_status': number(error.get('http_status')),
            'status': error.get('status') if error.get('status') in ('pending', 'failed', 'unknown') else 'unknown',
            'attempts': number(error.get('requests')), 'checked_at': date(error.get('checked_at')),
            'entry_ids': sorted(references.get(url, []))})
    for index, record in enumerate(settings['videos'].values(), 1):
        saved = record.get('path')
        if record.get('status') == 'saved' and saved:
            target = (folder / saved).resolve()
            if target.is_relative_to(folder.resolve()) and target.is_file():
                continue
        error = record.get('error', {})
        reason = 'local_missing' if saved else 'not_requested' if record.get('status') == 'not_requested' else error.get('reason', 'unknown')
        result['materials'].append({'id': f'video-{index}', 'kind': 'video',
            'reason': reason if reason in REASONS else 'unknown', 'http_status': number(error.get('http_status')),
            'status': record.get('status') if record.get('status') in ('pending', 'failed', 'not_requested', 'saved') else 'unknown',
            'requests': number(record.get('requests')),
            'checked_at': date(record.get('checked_at')),
            'entry_ids': [eid for eid in record.get('entries', []) if type(eid) is int]})
    result['incomplete_discussions'] = [int(eid) for eid, value in settings['entry_comments'].items()
                                         if str(eid).isdigit() and not value.get('done')]
    result['reasons'] = dict(Counter(m['reason'] for m in result['materials']))
    return result
