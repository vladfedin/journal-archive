"""A serialized, bounded queue for explicit downloads from archived posts."""
import json
import secrets
import sqlite3
import threading
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urljoin

from .client import Cancelled, ExportError, LiveJournalClient, RequestBudget, RequestLimitReached
from .media import Media, error_message
from .render import build_archive, original_url, text_value
from .storage import Store
from .video import VideoArchive, VideoRefs, VideoStop, canonical_video, embed_key


class NeedsLogin(ExportError):
    pass


def inventory(store, eid):
    row = store.db.execute('SELECT payload FROM entries WHERE id=?', (eid,)).fetchone()
    if not row:
        raise ExportError('Запись не найдена в архиве.')
    entry = json.loads(row[0])
    base = original_url(store.get('username'), entry)
    refs, images = VideoRefs(base), set()
    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            value = dict(attrs).get('src')
            if tag == 'img' and value:
                images.add(urljoin(base, value))
    parser = Images()
    for item in [entry, *store.comments(eid)]:
        body = text_value(item.get('event', item.get('body')))
        refs.feed(body); parser.feed(body)
    refs.close(); parser.close()
    resolved = store.get('video_embeds', {})
    for module in refs.modules:
        url = resolved.get(embed_key(base, module))
        if url and canonical_video(url, base): refs.urls.add(url)
    return entry, {'image': images, 'video': refs.urls, 'embed': refs.modules}


def validate(store, data):
    eid, kind, value = data.get('entry_id'), data.get('kind'), data.get('value')
    if type(eid) is not int or eid <= 0 or kind not in ('image', 'video', 'embed') or not isinstance(value, str) or len(value) > 4096:
        raise ExportError('Некорректный материал.')
    entry, allowed = inventory(store, eid)
    if value not in allowed[kind]:
        raise ExportError('Этот материал не найден в сохранённой записи или её комментариях.')
    return entry


def download_one(store, data, cancel, update):
    """Only the selected material; never re-fetch comments or change authored text."""
    entry = validate(store, data)
    eid, kind, value = data['entry_id'], data['kind'], data['value']
    budget = RequestBudget(data.get('max_requests', 50), progress=update)
    client = LiveJournalClient(store.get('username'), data.get('password', ''),
                               delay=data.get('delay', 3), cancel=cancel, budget=budget, progress=update)
    client.session.trust_env = False
    budget.not_before = client.not_before = store.get('request_not_before', 0)
    def cooldown(deadline):
        with store.db: store.set('request_not_before', deadline)
    client.save_cooldown = budget.save_cooldown = cooldown
    manager, media = None, None
    try:
        update(phase='running', message='Загрузка…', **budget.snapshot())
        if kind == 'image':
            media = Media(store.folder, True, cancel, budget); media.load_state(store)
            path = media.get(value)
            with store.db: media.save_state(store)
            if cancel.is_set(): raise Cancelled('Загрузка остановлена.')
            if not path:
                raise ExportError(error_message(media.errors.get(value, {})))
            asset = {'kind': 'image', 'path': path}
        else:
            manager = VideoArchive(store, client, update)
            if kind == 'embed':
                key = embed_key(original_url(client.user, entry), value)
                if key not in manager.embeds and key not in manager.external:
                    if data.get('password'):
                        client.login()
                    try:
                        manager.resolve(entry, [value])
                    except (Cancelled, RequestLimitReached):
                        raise
                    except ExportError as exc:
                        if not data.get('password') and ('401/403' in str(exc) or 'требуется вход' in str(exc)):
                            raise NeedsLogin('Для этой вставки может потребоваться вход в ЖЖ.') from exc
                        raise
                if key in manager.external:
                    asset = {**manager.external[key], 'kind': 'external'}
                elif key in manager.embeds:
                    value = manager.embeds[key]
                    asset = None
                else:
                    if not data.get('password'):
                        raise NeedsLogin('Вставка не получена. Можно повторить со входом в ЖЖ.')
                    raise ExportError('Тип вставки пока не поддерживается. Откройте оригинал.')
            else:
                asset = None
            if asset is None:
                manager.download(value, eid)
                if cancel.is_set(): raise Cancelled('Загрузка остановлена.')
                record = manager.records[value]
                if record['status'] != 'saved':
                    from .video import video_report
                    message = video_report(store)['missing'].get(value, {}).get('error', {}).get('message', 'Видео не получено.')
                    raise ExportError(message)
                asset = {'kind': 'video', 'path': record['path']}
        if cancel.is_set():
            raise Cancelled('Загрузка остановлена.')
        update(phase='saving', message='Сохранение страниц…', asset=asset)
        return asset
    finally:
        if media: media.close()
        if manager: manager.close()
        client.close()
        data.pop('password', None)


class MaterialQueue:
    def __init__(self, jobs):
        self.jobs = jobs
        self.states, self.queue = {}, deque()
        self.thread = None
        self.running = False
        self.cancel = threading.Event()
        self.current = None

    def start(self, data):
        with self.jobs.lock:
            if self.jobs.stopping:
                raise ExportError('Приложение завершается.')
            slug = data.get('archive')
            self.jobs.task_path(slug)
            if 'password' in data and (not isinstance(data['password'], str) or len(data['password']) > 1024):
                raise ExportError('Некорректный пароль.')
            try:
                reader = Store.reader(self.jobs.output / slug)
                try: validate(reader, data)
                finally: reader.close()
            except (OSError, sqlite3.Error) as exc:
                raise ExportError('Архив не найден.') from exc
            identity = (slug, data['entry_id'], data['kind'], data['value'])
            for state in self.states.values():
                if state['_identity'] == identity and state['phase'] not in ('complete', 'error', 'needs_auth', 'cancelled'):
                    return self.status(state['id'])
            if len(self.queue) >= 100:
                raise ExportError('Очередь заполнена. Дождитесь загрузки материалов.')
            for key in list(self.states):
                if len(self.states) < 200: break
                if self.states[key]['phase'] in ('complete', 'error', 'needs_auth', 'cancelled'): del self.states[key]
            task_id = secrets.token_hex(12)
            self.states[task_id] = {'id': task_id, '_identity': identity, 'phase': 'queued', 'message': 'В очереди', 'requests_used': 0}
            # Credentials live only in this in-memory request; never in status or task files.
            request = {k: data[k] for k in ('archive', 'entry_id', 'kind', 'value', 'password') if k in data}
            if self.jobs.current and self.jobs.current.get('archive') == slug:
                request['delay'] = self.jobs.current.get('delay') or 3
                request['max_requests'] = self.jobs.current.get('max_requests') or 50
            self.queue.append((task_id, request))
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, daemon=True)
                self.thread.start()
            return self.status(task_id)

    def status(self, task_id):
        with self.jobs.lock:
            if not isinstance(task_id, str): raise ExportError('Некорректная задача.')
            state = self.states.get(task_id)
            if state is None: raise ExportError('Задача не найдена. Нажмите «Загрузить» ещё раз.')
            return {k: v for k, v in state.items() if not k.startswith('_')}

    def stop(self, task_id):
        with self.jobs.lock:
            state = self.status(task_id)
            if state['phase'] in ('complete', 'error', 'needs_auth', 'cancelled'): return state
            if self.current == task_id: self.cancel.set()
            else:
                remaining = deque()
                for key, request in self.queue:
                    if key == task_id: request.clear()
                    else: remaining.append((key, request))
                self.queue = remaining
                self.states[task_id].update(phase='cancelled', message='Загрузка отменена')
            return self.status(task_id)

    def shutdown(self):
        self.cancel.set()
        with self.jobs.lock:
            while self.queue:
                task_id, request = self.queue.popleft(); request.clear()
                self.states[task_id].update(phase='cancelled', message='Загрузка отменена')

    def run(self):
        while True:
            with self.jobs.lock:
                if self.jobs.stopping or not self.queue:
                    self.thread = None
                    return
                busy = ((self.jobs.thread and self.jobs.thread.is_alive()) or self.jobs.relocation['phase'] == 'copying'
                        or any(t.is_alive() for t in self.jobs.preparation_threads))
                if busy:
                    for task_id, _ in self.queue:
                        self.states[task_id]['message'] = 'В очереди · архив занят'
                else:
                    task_id, request = self.queue.popleft()
                    if self.states[task_id]['phase'] == 'cancelled': request.clear(); continue
                    self.current, self.running, self.cancel = task_id, True, threading.Event()
            if busy:
                threading.Event().wait(0.5)
                continue
            def update(**values):
                if values.get('phase') in ('videos', 'login'): values['phase'] = 'running'
                with self.jobs.lock: self.states[task_id].update(values)
            store = None
            def refresh():
                path = store.folder / 'export-report.json'
                report = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
                    'username': store.get('username'), 'status': 'partial', 'created_at': store.get('started_at', ''),
                    'entries_saved': store.counts()['entries'], 'comments_saved': store.counts()['comments'], 'missing_comments': []}
                build_archive(store, report, images=False)
            try:
                store = Store(self.jobs.output / request['archive'])
                asset = download_one(store, request, self.cancel, update)
                refresh()
                update(phase='complete', message='Сохранено' if asset['kind'] != 'external' else 'Онлайн-плеер', asset=asset)
            except NeedsLogin as exc:
                update(phase='needs_auth', message=str(exc))
            except (Cancelled, VideoStop):
                update(phase='cancelled', message='Загрузка остановлена')
            except ExportError as exc:
                update(phase='error', message=str(exc))
                if store:
                    try: refresh()
                    except Exception: pass  # Error metadata is already committed to SQLite.
            except Exception:
                update(phase='error', message='Не удалось сохранить материал. Можно повторить загрузку.')
            finally:
                if store: store.close()
                request.clear()
                with self.jobs.lock: self.current, self.running = None, False
