"""Stage a verified desktop release for the next cold start."""
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.parse import urlsplit

import requests

from .activity import record_activity
from . import __version__
from .client import ExportError
from .launcher import version_tuple
from .paths import app_data
from .storage import atomic_write, exclusive_lock

REPOSITORY = 'vladfedin/journal-archive'
LATEST = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
VERSION = re.compile(r'[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}')
MAX_SIZE = 512 * 1024 * 1024
PACKAGES = {
    ('win32', 'x64'): ('windows-x64.exe', 'LiveJournalArchive.exe'),
    ('linux', 'x64'): ('linux-x64.tar.gz', 'LiveJournalArchive'),
    ('darwin', 'x64'): ('macos-x64.zip', 'LiveJournalArchive.app/Contents/MacOS/LiveJournalArchive'),
    ('darwin', 'arm64'): ('macos-arm64.zip', 'LiveJournalArchive.app/Contents/MacOS/LiveJournalArchive'),
}


def native_package():
    arch = {'x86_64':'x64', 'amd64':'x64', 'aarch64':'arm64'}.get(platform.machine().lower(), platform.machine().lower())
    return PACKAGES.get((sys.platform, arch)) if getattr(sys, 'frozen', False) else None


def candidate(release, suffix):
    tag = release['tag_name']
    version = tag.removeprefix('v')
    if release['draft'] or release['prerelease'] or tag != 'v' + version or not VERSION.fullmatch(version):
        raise ExportError('GitHub вернул неподходящий релиз.')
    if version_tuple(version) <= version_tuple(__version__):
        return None
    name = f'JournalArchive-{version}-{suffix}'
    for asset in release['assets']:
        if asset.get('name') != name or asset.get('state') != 'uploaded':
            continue
        digest, size = asset.get('digest'), asset.get('size')
        if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or type(size) != int or not 0 < size <= MAX_SIZE:
            raise ExportError('Не удалось проверить сведения о пакете.')
        return dict(version=version, size=size, digest=digest[7:],
                    url=f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}')
    raise ExportError(f'Для версии {version} пакет вашей системы ещё не опубликован.')


def fetch_package(session, package, path, progress, event=None):
    url = package['url']
    digest = hashlib.sha256()
    count = 0
    for _ in range(5):
        address = url.split('?', 1)[0]
        if event:
            event('request', method='GET', request_url=address)
        with session.get(url, allow_redirects=False, stream=True, timeout=(10, 45)) as response:
            if event:
                event('redirect' if response.is_redirect else 'response', http_status=response.status_code)
            if response.is_redirect:
                url = response.headers['Location']
                target = urlsplit(url)
                if target.scheme != 'https' or target.netloc not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
                    raise ExportError('Неизвестный адрес файла обновления.')
                continue
            response.raise_for_status()
            with path.open('xb') as out:
                for block in response.iter_content(1024 * 1024):
                    count += len(block)
                    if count > package['size']:
                        raise ExportError('Размер пакета отличается от опубликованного.')
                    digest.update(block)
                    out.write(block)
                    progress(count)
            break
    if count != package['size'] or digest.hexdigest() != package['digest']:
        raise ExportError('Проверка SHA-256 не прошла. Повторите загрузку.')


def child_env():
    return {**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'}


def receipt(folder):
    try:
        value = (folder / 'package.sha256').read_text(encoding='ascii').strip()
    except (OSError, ValueError):
        return None
    return value if re.fullmatch('[0-9a-f]{64}', value) else None


def stage(session, package, root, executable, progress, event=None):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with exclusive_lock(root / 'install.lock'), tempfile.TemporaryDirectory(dir=root, prefix='.staging-') as tmp:
        target = root / package['version']
        if (target / executable).is_file() and receipt(target) == package['digest']:
            return
        work = Path(tmp)
        archive = work / 'package'
        fetch_package(session, package, archive, progress, event)
        folder = work / 'app'
        folder.mkdir()
        binary = folder / executable
        if executable.endswith('.exe'):
            archive.rename(binary)
        elif executable.startswith('LiveJournalArchive.app/'):
            subprocess.run(['/usr/bin/ditto', '-x', '-k', str(archive), str(folder)], check=True, timeout=180)
        else:
            with tarfile.open(archive, 'r:gz') as tar:
                member = tar.getmember(executable)
                if not member.isfile() or not 0 < member.size <= MAX_SIZE:
                    raise ExportError('В пакете отсутствует исполняемый файл.')
                with tar.extractfile(member) as source, binary.open('xb') as out:
                    shutil.copyfileobj(source, out)
            binary.chmod(0o700)
        subprocess.run([str(binary), '--verify-update-version', package['version']], env=child_env(), check=True, timeout=120)
        (folder / 'package.sha256').write_text(package['digest'], encoding='ascii')
        folder.rename(target)


class DesktopUpdate:
    def __init__(self, package, root=None):
        self.suffix, self.executable = package
        self.root = root or app_data() / 'updates' / self.suffix
        self.lock = threading.RLock()
        self.package = None
        self.checked = 0
        self.state = {'phase': 'idle'}

    def set_automatic(self, enabled):
        if type(enabled) is not bool:
            raise ExportError('Некорректная настройка обновлений.')
        with self.lock:
            flag = self.root / 'automatic'
            try:
                if enabled:
                    self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
                    with exclusive_lock(self.root / 'settings.lock'):
                        atomic_write(flag, '1')
                else:
                    flag.unlink(missing_ok=True)
            except OSError:
                raise ExportError('Не удалось сохранить настройку обновлений.') from None
            if enabled and self.state['phase'] == 'available':
                self.start('install')
            return self.snapshot()

    def installed(self):
        if self.root.is_dir():
            versions = [p for p in self.root.iterdir() if VERSION.fullmatch(p.name) and not p.is_symlink()
                        and version_tuple(p.name) > version_tuple(__version__)
                        and (p / self.executable).is_file() and receipt(p)]
            if versions:
                return max(versions, key=lambda p: version_tuple(p.name))
        return None

    def start_installed(self, output, port, open_browser):
        target = self.installed()
        if not target:
            return False
        args = [str(target / self.executable), 'desktop', '--output', str(output), '--port', str(port)]
        if not open_browser:
            args.append('--no-browser')
        try:
            subprocess.Popen(args, env=child_env())
        except OSError:
            with self.lock:
                self.checked = time.monotonic()
                self.state = {'phase':'error', 'message':'Не удалось запустить обновление. Открыта прежняя версия.'}
            return False
        return True

    def snapshot(self):
        with self.lock:
            if self.state['phase'] == 'idle' or (self.state['phase'] not in ('checking', 'downloading', 'ready')
                                                 and time.monotonic() - self.checked > 86400):
                self.start('check')
            return {**self.state, 'automatic': (self.root / 'automatic').is_file()}

    def start(self, action):
        with self.lock:
            if self.state['phase'] in ('checking', 'downloading', 'ready'):
                return self.state.copy()
            if action == 'install' and (self.state['phase'] != 'available' or not self.package):
                raise ExportError('Сначала проверьте наличие обновления.')
            self.state = {'phase': 'checking'} if action == 'check' else {
                'phase':'downloading', 'version':self.package['version'], 'bytes':0, 'total':self.package['size']}
            threading.Thread(target=self._work, args=(action,), daemon=action == 'check').start()
            return self.state.copy()

    def _work(self, action):
        def event(kind, **values):
            with self.lock:
                record_activity(self.state, {'activity': {'kind':kind, **values}})
        def progress(count):
            with self.lock:
                self.state['bytes'] = count
        try:
            with requests.Session() as session:
                session.trust_env = False
                if action == 'check':
                    installed = self.installed()
                    if installed:
                        state = {'phase':'ready', 'version':installed.name}
                    else:
                        event('request', method='GET', request_url=LATEST)
                        with session.get(LATEST, timeout=(8, 15), allow_redirects=False) as response:
                            event('response', http_status=response.status_code)
                            response.raise_for_status()
                            package = candidate(response.json(), self.suffix)
                        with self.lock:
                            self.package = package
                        state = {'phase':'available', 'version':package['version']} if package else {'phase':'current'}
                else:
                    stage(session, self.package, self.root, self.executable, progress, event)
                    state = {'phase':'ready', 'version':self.package['version']}
        except ExportError as exc:
            state = {'phase':'error', 'message':str(exc)}
        except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError, OSError, subprocess.SubprocessError, tarfile.TarError, EOFError):
            state = {'phase':'error', 'message':'Не удалось проверить релиз на GitHub.' if action == 'check'
                     else 'Не удалось подготовить обновление. Проверьте соединение и свободное место.'}
        with self.lock:
            self.state = {**state, 'events': self.state.get('events', [])}
            self.checked = time.monotonic()
            if state['phase'] == 'available' and (self.root / 'automatic').is_file():
                self.start('install')
