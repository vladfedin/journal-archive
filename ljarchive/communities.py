"""Short-lived community picker operations. Credentials never enter the status record."""
import secrets
import threading
import time

from . import client as protocol
from .activity import record_activity


LOOKUP_TIMEOUT = 60


class CommunityLookups:
    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = {}
        self.workers = 0

    def start(self, account, password):
        account = protocol.username(account)
        with self.lock:
            for key, (state, _) in list(self.tasks.items()):
                if state.get('ended_at', float('inf')) < time.time() - 600:
                    del self.tasks[key]
            if self.workers >= 4:
                raise protocol.ExportError('Уже идёт поиск сообществ. Дождитесь ответа.')
            if len(self.tasks) >= 20:
                del self.tasks[next(k for k,(s,_) in self.tasks.items() if s.get('ended_at'))]
            key = secrets.token_hex(16)
            state = {'id': key, 'phase': 'loading', 'message': 'Загружаем сообщества…',
                     'started_at': time.time(), 'events': []}
            cancel = threading.Event()
            self.tasks[key] = state, cancel
            self.workers += 1
            threading.Thread(target=self.run, args=(state, cancel, account, password), daemon=True).start()
            return dict(state)

    def finish(self, state, result):
        if not state.get('ended_at'):
            state.update(result, ended_at=time.time())

    def run(self, state, cancel, account, password):
        def expired():
            with self.lock:
                self.finish(state, {'phase':'error', 'message':'Истекло время поиска сообществ. Повторите запрос позже.'})
                cancel.set()

        watchdog = threading.Timer(LOOKUP_TIMEOUT, expired)
        watchdog.daemon = True
        watchdog.start()

        def update(**values):
            with self.lock:
                if state.get('ended_at'):
                    return
                record_activity(state, values)
                state.update({k: v for k, v in values.items() if k in ('phase','requests_used','message')})
                if values.get('activity', {}).get('kind') == 'request':
                    state.update(phase='loading', message='Загружаем сообщества…')
        client = None
        try:
            client = protocol.LiveJournalClient(account, password, delay=1, max_attempts=2, cancel=cancel,
                progress=update, budget=protocol.RequestBudget(12, progress=update))
            client.deadline = time.monotonic() + LOOKUP_TIMEOUT
            names = client.managed_communities(confirm_empty=True)
            result = {'phase': 'complete', 'communities': names, 'message': f'Найдено сообществ: {len(names)}'}
        except protocol.ExportError as exc:
            result = {'phase': 'error', 'message': str(exc)}
        except Exception as exc:
            update(activity={'kind':'failure','reason':type(exc).__name__})
            result = {'phase': 'error', 'message': 'Не удалось обработать ответ при загрузке сообществ.'}
        finally:
            watchdog.cancel()
            try:
                if client:
                    client.close()
            finally:
                with self.lock:
                    self.workers -= 1
                    self.finish(state, result)

    def status(self, key, *, cancel=False):
        with self.lock:
            if not isinstance(key, str) or key not in self.tasks:
                raise protocol.ExportError('Поиск сообществ завершён или устарел. Повторите запрос.')
            state, stop = self.tasks[key]
            if cancel:
                stop.set()
                self.finish(state, {'phase':'error', 'message':'Поиск сообществ отменён.'})
            return {**state, 'server_time': time.time()}
