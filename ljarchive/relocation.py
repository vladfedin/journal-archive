"""Verified library relocation with an external, durable redirect for cold starts."""
import hashlib
import json
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path

from .client import ExportError
from .storage import Store, atomic_write, dumps


def redirect_file(folder):
    return folder.with_name('.' + folder.name + '-location.json')


def check_profile_location(folder):
    from .paths import browser_profiles
    profiles = browser_profiles().resolve()
    if profiles.is_relative_to(folder) or folder.is_relative_to(profiles):
        raise ExportError('Выберите папку архивов отдельно от профилей браузера.')


def resolve_output(folder):
    folder = Path(folder).resolve()
    visited = set()
    while redirect_file(folder).exists():
        if folder in visited or len(visited) >= 16:
            raise ExportError('Не удалось определить папку архивов: цепочка переносов повреждена.')
        visited.add(folder)
        try:
            value = json.loads(redirect_file(folder).read_text(encoding='utf-8'))['output']
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError()
            folder = Path(value).resolve()
            if not folder.is_dir() and not redirect_file(folder).exists():
                raise ExportError('Папка архивов после переноса недоступна. Подключите диск или верните папку на прежнее место.')
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise ExportError('Не удалось прочитать сохранённый путь к архивам.') from exc
    check_profile_location(folder)
    return folder


def target_path(source, value):
    if not isinstance(value, str) or not value.strip() or not Path(value).expanduser().is_absolute():
        raise ExportError('Укажите полный путь к новой папке.')
    target = Path(value).expanduser().resolve()
    check_profile_location(target)
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise ExportError('Выберите отдельную папку вне текущей папки архивов.')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ExportError('Папка назначения должна быть пустой или новой.')
    if redirect_file(target).exists():
        raise ExportError('Эта папка уже использовалась для переноса. Выберите новую папку.')
    return target


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.digest()


def relocate(source, target, switch, progress):
    """Publish a verified copy, then redirect and remove the old tree.

    Before the redirect commits, failure leaves the original authoritative. After
    commit, the destination is authoritative even if old-file cleanup fails.
    """
    staging = target.with_name('.' + target.name + '-moving-' + uuid.uuid4().hex)
    published = False
    with ExitStack() as stack:
        for database in source.glob('*/archive.sqlite3'):
            store = Store(database.parent)
            stack.callback(store.close)
            store.db.close()  # Keep the archive lock, with SQLite fully flushed.
        paths = sorted(source.rglob('*'))
        if any(p.is_symlink() for p in paths):
            raise ExportError('В папке архивов есть символические ссылки. Перенос отменён; исходные данные сохранены.')
        files = [p for p in paths if p.is_file() and p.suffix != '.lock']
        total = sum(p.stat().st_size for p in files)
        target.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < total:
            raise ExportError('На выбранном диске недостаточно места.')
        try:
            staging.mkdir(mode=0o700)
            for path in paths:
                if path.is_dir():
                    (staging / path.relative_to(source)).mkdir(exist_ok=True)
            saved = 0
            for index, path in enumerate(files):
                copy = staging / path.relative_to(source)
                shutil.copy2(path, copy)
                if digest(path) != digest(copy):
                    raise ExportError('Проверка копии не прошла. Исходная папка сохранена.')
                saved += copy.stat().st_size
                progress(phase='copying', completed=index + 1, total=len(files), bytes=saved, total_bytes=total)
            # Recheck destination to avoid merging with data created during copying.
            if target.exists():
                target.rmdir()  # Fails safely if it is no longer empty.
            staging.rename(target)
            published = True
            atomic_write(redirect_file(source), dumps({'output': str(target)}))
            switch(target)
        except Exception:
            if not published:
                shutil.rmtree(staging, ignore_errors=True)
            raise
    try:
        shutil.rmtree(source)
    except OSError:
        return 'Перенос завершён. Часть файлов в старой папке занята; старую копию можно удалить позже.'
    return None
