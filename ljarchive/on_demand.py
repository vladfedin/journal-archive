"""A serialized, bounded queue for explicit downloads from archived posts."""
import json
import secrets
import sqlite3
import threading
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urljoin

from .client import AuthenticationRequired, Cancelled, ExportError, LiveJournalClient, RequestBudget, RequestLimitReached
from .media import Media, authenticated_image_host, image_url, error_message, settle_browser_answer
from .gaps import ATTEMPTS, closed, reopen
from .retry import restored_deadline
from .render import build_archive, original_url, text_value
from .storage import Store
from .video import VideoArchive, VideoRefs, VideoStop, canonical_video, embed_key
from .browser_bridge import BrowserLoginRequired


class NeedsLogin(ExportError):
    pass


def inventory(store, eid):
    row = store.db.execute('SELECT payload FROM entries WHERE id=?', (eid,)).fetchone()
    comments = store.comments(eid)
    if not row and not comments:
        raise ExportError('Запись не найдена в архиве.')
    entry = json.loads(row[0]) if row else {'itemid': eid, 'event': '', 'unavailable': True}
    base = original_url(store.get('username'), entry)
    refs, images = VideoRefs(base), set()
    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            value = dict(attrs).get('src')
            if tag == 'img' and value:
                images.add(urljoin(base, value))
    parser = Images()
    for item in [entry, *comments]:
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
    if (data.get('transport', 'http') not in ('http', 'browser') or type(data.get('confirmed', False)) is not bool
            or type(data.get('open_browser', False)) is not bool):
        raise ExportError('Некорректный способ загрузки.')
    if kind == 'browser_images' and type(eid) is int and eid == 0 and value == 'missing':
        return None
    if kind == 'profile' and type(eid) is int and eid == 0 and value == 'profile':
        from .sources import stored_source
        if stored_source(store)['type'] != 'own':
            raise ExportError('Личный профиль доступен только в архиве своего журнала.')
        return None
    if type(eid) is not int or eid <= 0 or kind not in ('image', 'video', 'embed') or not isinstance(value, str) or len(value) > 4096:
        raise ExportError('Некорректный материал.')
    entry, allowed = inventory(store, eid)
    if value not in allowed[kind]:
        raise ExportError('Этот материал не найден в сохранённой записи или её комментариях.')
    from .video import lj_video_id
    if data.get('transport') == 'browser' and not (kind == 'embed' or
            (kind == 'video' and lj_video_id(value)) or (kind == 'image' and authenticated_image_host(image_url(value)))):
        raise ExportError('Вход через браузер доступен для материалов ЖЖ.')
    return entry


def browser_login(client, data, sessions):
    if not sessions:
        raise ExportError('Откройте архив через локальное приложение.')
    if data.get('open_browser'):
        client.browser.open_login(client.account)
        raise BrowserLoginRequired()
    if data.get('confirmed'):
        client.browser.confirm(client.account)
    if not client.browser.ready(client.account):
        client.browser.open_login(client.account)
        raise BrowserLoginRequired()


def image_failures(store):
    """Local, per-file evidence with backlinks. Not included in shared diagnostics."""
    mapping, errors = store.get('media', {}), store.get('media_errors', {})
    refs = {}
    for eid, record in store.get('entry_materials', {}).items():
        for url in record.get('urls', []):
            refs.setdefault(url, set()).add(int(eid))
    titles = {row[0]: {'id': row[0], 'title': row[1] or 'Без заголовка', 'date': row[2] or ''}
              for row in store.db.execute("SELECT id,json_extract(payload,'$.subject'),json_extract(payload,'$.eventtime') FROM entries")}
    result = []
    for url in sorted(set(errors) | set(mapping) | set(refs)):
        path = mapping.get(url)
        if path and (store.folder / path).is_file():
            continue
        error = settle_browser_answer(url, errors.get(url, {}))
        if error.get('availability') == 'unavailable':
            continue
        result.append({'url': url, 'browser': authenticated_image_host(image_url(url)),
                       'error': error, 'message': error_message(error),
                       'entries': [titles[eid] for eid in sorted(refs.get(url, ())) if eid in titles]})
    return result


def download_browser_images(store, data, cancel, update, sessions):
    from .sources import stored_source
    identity = stored_source(store)
    budget = RequestBudget(data.get('max_requests'), progress=update)
    client = LiveJournalClient(identity['login_username'], '', journal=identity['username'], source_type=identity['type'],
                               delay=data.get('delay', 3), cancel=cancel, budget=budget, progress=update)
    media = Media(store.folder, True, cancel, budget, client=client)
    def cooldown(deadline):
        with store.db:
            store.set('request_not_before', deadline)
            store.set('request_wait', budget.wait_info)
    budget.save_cooldown = cooldown
    try:
        sessions.restore(client)
        media.load_state(store); media.browser_only = True
        pending = [item for item in image_failures(store) if item['browser'] and not closed(media.errors.get(item['url']))]
        saved, seen, consecutive = set(), set(), 0
        # Persist final answers from older archives even when no request is needed.
        with store.db: media.save_state(store)
        if pending:
            browser_login(client, data, sessions)
        budget.not_before = restored_deadline(store)
        budget.wait_info = store.get('request_wait', {})
        update(phase='running', batch_total=len(pending), total=len(pending), completed=0, saved=0, failed=0)
        for attempt in range(ATTEMPTS):
            retry = [item for item in pending if item['url'] not in saved and not closed(media.errors.get(item['url']))]
            for item in retry:
                if cancel.is_set():
                    raise Cancelled('Загрузка остановлена.')
                value = item['url']
                referer = f'https://{client.user.replace("_", "-")}.livejournal.com/'
                if item['entries']:
                    row = store.db.execute('SELECT payload FROM entries WHERE id=?', (item['entries'][0]['id'],)).fetchone()
                    if row: referer = original_url(client.user, json.loads(row[0]))
                media.attempted.discard(value)
                update(phase='running', message=f'Изображения · попытка {attempt + 1} из {ATTEMPTS}',
                       batch_total=len(pending), completed=len(seen), total=len(pending), saved=len(saved))
                path = media.get(value, required=True, referer=referer)
                with store.db: media.save_state(store)
                seen.add(value)
                if path: saved.add(value)
                update(phase='running', message=f'Сохранено {len(saved)} из {len(pending)} · попытка {attempt + 1} из {ATTEMPTS}',
                       completed=len(seen), total=len(pending), saved=len(saved), failed=len(seen) - len(saved))
                if media.stopped:
                    if cancel.is_set(): raise Cancelled('Загрузка остановлена.')
                    raise ExportError(error_message(media.errors.get(value, {})))
                reason = media.errors.get(value, {}).get('reason')
                consecutive = consecutive + 1 if reason in ('browser_network', 'browser_timeout', 'browser_dns') else 0
                if not path and (reason in ('browser_closed', 'browser_login', 'browser_start', 'browser_profile', 'browser_missing') or
                                 media.errors.get(value, {}).get('http_status') == 429 or consecutive >= 3):
                    # Session loss, rate limits and a broken connection affect the
                    # queue; a file-specific 401/403/404 does not.
                    update(error=media.errors[value])
                    raise ExportError(error_message(media.errors[value]))
        return {'kind': 'images', 'saved': len(saved), 'failed': len(pending) - len(saved), 'total': len(pending)}
    finally:
        media.close(); client.close()


def download_one(store, data, cancel, update, sessions=None):
    """Only the selected material; never re-fetch comments or change authored text."""
    entry = validate(store, data)
    eid, kind, value = data['entry_id'], data['kind'], data['value']
    budget = RequestBudget(data.get('max_requests', 50), progress=update)
    from .sources import stored_source
    identity = stored_source(store)
    client = LiveJournalClient(identity['login_username'], data.get('password', ''),
                               journal=identity['username'], source_type=identity['type'],
                               delay=data.get('delay', 3), cancel=cancel, budget=budget, progress=update,
                               session_ready=sessions.remember if sessions else None)
    client.session.trust_env = False
    if sessions:
        sessions.restore(client)
    budget.not_before = client.not_before = restored_deadline(store)
    budget.wait_info = store.get('request_wait', {})
    def cooldown(deadline):
        with store.db:
            store.set('request_not_before', deadline)
            store.set('request_wait', budget.wait_info)
    client.save_cooldown = budget.save_cooldown = cooldown
    manager, media = None, None
    try:
        update(phase='running', message='Загрузка…', **budget.snapshot())
        if kind == 'image':
            trusted = authenticated_image_host(image_url(value))
            if data.get('transport') == 'browser':
                browser_login(client, data, sessions)
            if trusted and data.get('password'):
                client.session.cookies.clear()
                client.login()
            media = Media(store.folder, True, cancel, budget, client=client); media.load_state(store)
            media.browser_only = data.get('transport') == 'browser'
            if value in media.errors:
                reopen(media.errors[value])
            path = media.get(value, referer=original_url(client.user, entry))
            with store.db: media.save_state(store)
            if cancel.is_set(): raise Cancelled('Загрузка остановлена.')
            if not path:
                error = media.errors.get(value, {})
                update(error=error)
                if error.get('availability') == 'unavailable' and closed(error):
                    return {'kind': 'unavailable', 'http_status': error['http_status']}
                if (trusted and (error.get('http_status') in (401, 403) or error.get('reason') == 'web_login')
                        and error.get('session_type') != 'browser' and not data.get('password')):
                    if sessions:
                        sessions.forget(client)
                    raise NeedsLogin('Для закрытого изображения войдите в ЖЖ.')
                raise ExportError(error_message(error))
            asset = {'kind': 'image', 'path': path}
        else:
            if data.get('transport') == 'browser':
                browser_login(client, data, sessions)
            manager = VideoArchive(store, client, update)
            if kind == 'embed':
                key = embed_key(original_url(client.user, entry), value)
                if key in manager.records:
                    reopen(manager.records[key])
                if key not in manager.embeds and key not in manager.external:
                    if data.get('password') and not client._cookie:
                        client.login()
                    try:
                        manager.resolve(entry, [value])
                    except (Cancelled, RequestLimitReached):
                        raise
                    except AuthenticationRequired:
                        if sessions:
                            client.browser.open_login(client.account)
                            raise BrowserLoginRequired() from None
                        raise
                if key in manager.external:
                    asset = {**manager.external[key], 'kind': 'external'}
                elif key in manager.embeds:
                    value = manager.embeds[key]
                    asset = None
                else:
                    from .gaps import fail
                    record = manager.records.setdefault(key, {'url': original_url(client.user, entry), 'entries': [eid]})
                    record.update(status='failed', stage='source', trace=manager.trace[-100:],
                                  error={'reason': 'embed_unresolved', 'detail': 'Вставка не найдена на текущей странице записи.'})
                    fail(record); manager.save()
                    update(error={**record['error'], 'stage': 'source', 'trace': record['trace']})
                    raise ExportError('Вставка не найдена на текущей странице записи.')
            else:
                asset = None
            if asset is None:
                from .video import lj_video_id
                if lj_video_id(value) and data.get('password') and not client._cookie:
                    client.login()
                if value in manager.records:
                    reopen(manager.records[value])
                manager.download(value, eid)
                if cancel.is_set(): raise Cancelled('Загрузка остановлена.')
                record = manager.records[value]
                if record['status'] != 'saved':
                    update(error={**record.get('error', {}), 'trace': record.get('trace', []),
                                  'requests': record.get('requests', 0), 'stage': record.get('stage'),
                                  'updated_at': record.get('checked_at')})
                    if record.get('error', {}).get('reason') == 'auth_required' and sessions:
                        client.browser.open_login(client.account)
                        raise BrowserLoginRequired()
                    from .video import video_report
                    message = video_report(store)['missing'].get(value, {}).get('error', {}).get('message', 'Видео не получено.')
                    raise ExportError(message)
                asset = {'kind': 'video', 'path': record['path'], 'bytes': record.get('bytes'), 'title': record.get('title'),
                         'trace': record.get('trace', []), 'requests': record.get('requests', 0)}
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
                if state['_identity'] == identity and state['phase'] not in ('complete', 'error', 'needs_auth', 'needs_browser', 'cancelled'):
                    return self.status(state['id'])
            if len(self.queue) >= 100:
                raise ExportError('Очередь заполнена. Дождитесь загрузки материалов.')
            for key in list(self.states):
                if len(self.states) < 200: break
                if self.states[key]['phase'] in ('complete', 'error', 'needs_auth', 'needs_browser', 'cancelled'): del self.states[key]
            task_id = secrets.token_hex(12)
            self.states[task_id] = {'id': task_id, '_identity': identity, '_main_job': self.jobs.state.get('job_id'),
                                    'phase': 'queued', 'message': 'В очереди', 'requests_used': 0}
            # Credentials live only in this in-memory request; never in status or task files.
            request = {k: data[k] for k in ('archive', 'entry_id', 'kind', 'value', 'password', 'transport', 'confirmed', 'open_browser') if k in data}
            if self.jobs.current and self.jobs.current.get('archive') == slug:
                request['delay'] = self.jobs.current.get('delay') or 3
                request['max_requests'] = self.jobs.current.get('max_requests')
            else:
                try:
                    parameters = json.loads(self.jobs.task_path(slug).read_text(encoding='utf-8'))['parameters']
                    request['delay'] = parameters.get('delay') or 3
                    request['max_requests'] = parameters.get('max_requests')
                except (OSError, ValueError, KeyError):
                    pass
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
            if state['phase'] in ('complete', 'error', 'needs_auth', 'needs_browser', 'cancelled'): return state
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
                available = None
                for candidate, pending in self.queue:
                    reason = self.jobs.archive_busy(pending['archive'])
                    if reason:
                        self.states[candidate]['message'] = 'В очереди · ' + reason
                    elif available is None:
                        available = (candidate, pending)
                busy = available is None
                if not busy:
                    task_id, request = available
                    self.queue.remove(available)
                    if self.states[task_id]['phase'] == 'cancelled': request.clear(); continue
                    self.states[task_id]['_main_job'] = self.jobs.state.get('job_id')
                    self.current, self.running, self.cancel = task_id, True, threading.Event()
            if busy:
                threading.Event().wait(0.5)
                continue
            def update(**values):
                if values.get('phase') in ('videos', 'login', 'materials', 'personal', 'retrying', 'loading'): values['phase'] = 'running'
                with self.jobs.lock:
                    current_state = self.states[task_id]
                    if ((values.get('stage') and values['stage'] != current_state.get('stage')) or
                            (values.get('phase') and values['phase'] != current_state.get('phase'))):
                        for key in ('completed', 'total', 'progress_unit'):
                            current_state.pop(key, None)
                    event = values.pop('activity', None)
                    if event:
                        self.states[task_id]['last_activity'] = event
                    if event and event.get('kind') == 'request' and self.states[task_id].get('phase') == 'waiting':
                        values.update(phase='running', message='Получаем данные…')
                    if values.get('phase') and values['phase'] != 'waiting':
                        self.states[task_id].pop('retry_at', None)
                        self.states[task_id].pop('wait', None)
                    self.states[task_id].update(values)
            store, asset = None, None
            def refresh(*, failed=False, interruption=None):
                if request['kind'] == 'profile':
                    from .personal import refresh_profile
                    report = refresh_profile(store, interruption=interruption)
                    from .recovery import inventory as gaps, summary
                    self.jobs.profile_result(request['archive'], report, summary(gaps(store)))
                    return
                path = store.folder / 'export-report.json'
                report = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
                    'username': store.get('username'), 'status': 'partial', 'created_at': store.get('started_at', ''),
                    'entries_saved': store.counts()['entries'], 'comments_saved': store.counts()['comments'], 'missing_comments': []}
                from .recovery import inventory as gaps, summary, images_only_partial, requested_gaps_settled
                current = summary(gaps(store))
                complete = images_only_partial(report) and requested_gaps_settled(report, current)
                targets = None
                if request['kind'] in ('image', 'video', 'embed'):
                    from .gallery import inventory as material_inventory
                    value = request['value']
                    if request['kind'] == 'embed':
                        entry, _ = inventory(store, request['entry_id'])
                        value = store.get('video_embeds', {}).get(embed_key(original_url(store.get('username'), entry), value))
                    targets = {request['entry_id']}
                    for item in material_inventory(store):
                        if item['url'] == value:
                            targets.update(ref['entry_id'] for ref in item['references'])
                if failed and targets is not None:
                    targets = set()  # Only evidence/JSON/gallery changed, no new file to insert into posts.
                if not failed:
                    update(phase='saving', stage='index', message='Обновляем связанные записи…')
                def rendering(**values):
                    if not failed: update(**{**values, 'phase': 'saving'})
                build_archive(store, report, images=False, complete_after_media=complete, progress=rendering,
                              entry_ids=targets, changed_media=[asset['path']] if asset and asset.get('path') else [])
                with self.jobs.lock:
                    if self.jobs.current and self.jobs.current.get('archive') == request['archive']:
                        values = {'recovery': current}
                        if complete and report['status'] == 'complete' and self.jobs.state.get('phase') == 'partial':
                            values['phase'] = 'complete'
                        self.jobs.progress(**values)
            try:
                store = Store(self.jobs.output / request['archive'])
                if request['kind'] == 'profile':
                    from .personal import download_profile
                    asset = download_profile(store, request, self.cancel, update, sessions=self.jobs.sessions)
                elif request['kind'] == 'browser_images':
                    asset = download_browser_images(store, request, self.cancel, update, self.jobs.sessions)
                else:
                    asset = download_one(store, request, self.cancel, update, sessions=self.jobs.sessions)
                refresh()
                message = 'Сохранено'
                if asset['kind'] == 'images':
                    message = f"Сохранено {asset['saved']} из {asset['total']} · остались ссылками {asset['failed']}"
                elif asset['kind'] == 'unavailable':
                    message = f"Недоступно · HTTP {asset['http_status']}. Ссылка сохранена."
                elif asset['kind'] == 'external':
                    message = 'Онлайн-плеер'
                counts = {'completed': asset['total'], 'total': asset['total']} if asset['kind'] == 'images' else {}
                update(phase='complete', message=message, asset=asset, **counts)
                with self.jobs.lock:
                    if request['kind'] == 'browser_images' and self.jobs.current and self.jobs.current.get('archive') == request['archive']:
                        self.jobs.progress(message=message, browser_images_progress=self.states[task_id].get('browser_images_progress', {}))
            except BrowserLoginRequired as exc:
                update(phase='needs_browser', message=str(exc))
            except NeedsLogin as exc:
                update(phase='needs_auth', message=str(exc))
            except (Cancelled, VideoStop):
                if store and request['kind'] in ('profile', 'browser_images'):
                    try: refresh()
                    except Exception: pass  # Captured sections remain committed to SQLite.
                update(phase='cancelled', message='Загрузка остановлена')
            except ExportError as exc:
                update(phase='error', message=str(exc))
                if store:
                    from .client import HTTPRateLimited, APIRateLimited
                    try: refresh(failed=True, interruption=exc if isinstance(exc, (HTTPRateLimited, APIRateLimited, RequestLimitReached)) else None)
                    except Exception: pass  # Error metadata is already committed to SQLite.
            except Exception:
                update(phase='error', message='Не удалось сохранить материал. Можно повторить загрузку.')
            finally:
                if store: store.close()
                request.clear()
                with self.jobs.lock: self.current, self.running = None, False
