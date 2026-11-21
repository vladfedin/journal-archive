"""Bounded request history shared by interactive operations."""
import time


def record_activity(state, values):
    event = values.get('activity')
    if not event and values.get('phase') == 'waiting':
        event = {'kind': 'wait', **values.get('wait', {})}
        previous = (state.get('events') or [{}])[-1]
        if all(previous.get(k) == v for k, v in event.items()):
            return
    if event:
        state['events'] = (state.get('events', []) + [{'at': time.time(), **event}])[-30:]
