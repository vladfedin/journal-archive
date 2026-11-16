"""Version-aware desktop startup and graceful handover of the local HTTP server."""
from __future__ import annotations

import errno
import html
import json
import os
import re
import socket
import time
from contextlib import contextmanager
from pathlib import Path

import requests

from . import __version__
from .client import ExportError
from .storage import atomic_write, dumps


VERSION = re.compile(r'([0-9]+)\.([0-9]+)(?:\.([0-9]+))?')


def version_tuple(value):
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ExportError('Не удалось определить версию работающего приложения. Завершите его кнопкой внизу страницы и повторите запуск.')
    return tuple(int(part or 0) for part in match.groups())


@contextmanager
def startup_lock(path, timeout):
    """Serialize desktop starts, including separate copies of a one-file executable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    deadline = time.monotonic() + timeout
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
        else:
            import fcntl
        while True:
            try:
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise ExportError('Другой запуск ещё ожидает завершения старой версии. Повторите запуск позже.') from None
                time.sleep(.2)
        yield
    finally:
        # Closing the descriptor releases the OS lock, including after a crash.
        stream.close()


def get_json(session, url):
    response = session.get(url, timeout=(1, 5), allow_redirects=False)
    if response.status_code != 200:
        raise ExportError('Работающее приложение не ответило. Завершите его кнопкой внизу страницы и повторите запуск.')
    value = response.json()
    if not isinstance(value, dict):
        raise ExportError('Работающее приложение вернуло некорректный ответ.')
    return value


def inspect_server(session, port, instance=None):
    base = f'http://127.0.0.1:{port}'
    try:
        response = session.get(base + '/', timeout=(1, 5), allow_redirects=False)
    except (requests.ConnectionError, requests.Timeout):
        return None
    if response.status_code != 200 or not response.headers.get('Server', '').startswith('LJLocalArchive/'):
        return None
    # The initial GET obtains the same local session cookie as a browser.
    response.encoding = 'utf-8'
    version = response.headers.get('X-LJArchive-Version')
    metadata = session.get(base + '/api/app', timeout=(1, 5), allow_redirects=False)
    info = metadata.json() if metadata.status_code == 200 else {}
    if not isinstance(info, dict):
        raise ExportError('Не удалось проверить работающий экземпляр приложения.')
    if info:
        if info.get('application') != 'journal-archive' or not isinstance(info.get('instance_id'), str):
            raise ExportError('Не удалось проверить работающий экземпляр приложения.')
        version = info.get('version')
    if instance is not None and info.get('instance_id') != instance:
        return None  # A stale alternate-port registry must not select another process.
    if not version:
        # 0.3–0.6.1 had no version endpoint, and their Server header was stale.
        footer = re.search(r'<footer\b[^>]*>(.*?)</footer>', response.text, re.S | re.I)
        text = html.unescape(re.sub(r'<[^>]*>', ' ', footer[1])) if footer else ''
        match = re.search(r'Архив ЖЖ\s*·\s*([0-9]+\.[0-9]+\.[0-9]+)\b', text)
        version = match[1] if match else None
    version_tuple(version)
    return {**info, 'base': base, 'port': port, 'version': version}


def stop_previous(session, peer, timeout):
    """Use the app's own cancellation/checkpoint path; never kill a process."""
    base = peer['base']
    data = {'expected_instance': peer['instance_id']} if peer.get('instance_id') else {}
    if not peer.get('stopping'):
        response = session.post(base + '/api/quit', headers={'Origin': base}, json=data,
                                timeout=(1, 5), allow_redirects=False)
        if response.status_code != 200:
            raise ExportError('Не удалось завершить старую версию. Нажмите «Завершить приложение» в её окне и повторите запуск.')
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection(('127.0.0.1', peer['port']), timeout=3):
                pass
        except ConnectionRefusedError:
            return  # serve() closed the listener after the export worker finished.
        except OSError:
            pass  # A timeout does not prove that the previous process has stopped.
        if time.monotonic() >= deadline:
            raise ExportError('Старая версия ещё сохраняет данные. Дождитесь её завершения и запустите новый файл ещё раз. Архивы не удалены.')
        time.sleep(.2)


def prepare_server(output, port=8787, *, timeout=180):
    """Return (new bound server or None, URL). Reuse only this or a newer version."""
    from .server import make_server
    output = Path(output).resolve()
    registry = output.parent / 'desktop-instance.json'
    with startup_lock(output.parent / 'desktop-start.lock', timeout):
        candidates = []
        try:
            saved = json.loads(registry.read_text(encoding='utf-8'))
            saved_port, instance = saved['port'], saved['instance_id']
            previous_output = saved.get('output')
            if isinstance(previous_output, str) and Path(previous_output).is_absolute():
                output = Path(previous_output).resolve()
            if type(saved_port) is int and 1 <= saved_port <= 65535 and isinstance(instance, str):
                candidates.append((saved_port, instance))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if port:
            candidates.append((port, None))
        with requests.Session() as session:
            session.trust_env = False  # Never route loopback handover through a proxy/.netrc.
            peer = None
            for candidate, expected in candidates:
                peer = inspect_server(session, candidate, expected)
                if peer:
                    break
            if peer:
                if version_tuple(peer['version']) >= version_tuple(__version__) and not peer.get('stopping'):
                    return None, peer['base'] + '/'
                previous_output = peer.get('output')
                if previous_output is None:
                    previous_output = get_json(session, peer['base'] + '/api/archives').get('output')
                if not isinstance(previous_output, str) or not Path(previous_output).is_absolute():
                    raise ExportError('Не удалось определить папку старых архивов. Завершите старую версию и повторите запуск.')
                output = Path(previous_output).resolve()
                stop_previous(session, peer, timeout)
                port = peer['port']
            if not peer and port:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=3):
                        port = 0
                except OSError:
                    pass
            try:
                server = make_server(output, port)
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE and getattr(exc, 'winerror', None) != 10048:
                    raise
                if peer:
                    raise ExportError('Порт приложения снова занят. Повторите запуск, чтобы проверить работающую версию.') from None
                # Unrelated service: leave it alone and remember our alternate port.
                server = make_server(output, 0)
            try:
                atomic_write(registry, dumps({'port': server.server_port, 'instance_id': server.instance_id, 'output': str(server.jobs.output)}))
            except OSError:
                server.server_close()
                raise
            return server, f'http://127.0.0.1:{server.server_port}/'
