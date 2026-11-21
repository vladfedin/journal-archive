"""Remove a single local archive. The staged directory also records an interrupted deletion."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

from .client import ExportError
from .storage import exclusive_lock


def deletion_target(folder, task):
    staged = task.with_suffix('.deleting')
    for target in (staged, folder, task):
        try:
            stat = target.lstat()
        except FileNotFoundError:
            continue
        identity = (str(target), stat.st_dev, stat.st_ino, stat.st_ctime_ns)
        token = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return {'path': str(target), 'token': token}
    raise ExportError('Архив не найден.')


def archive_folder(folder):
    report = folder / 'export-report.json'
    if report.is_file() and not report.is_symlink() and (folder / 'index.html').is_file():
        try:
            if json.loads(report.read_text(encoding='utf-8')).get('username'):
                return True
        except (ValueError, AttributeError):
            pass
    db = folder / 'archive.sqlite3'
    if db.is_file() and not db.is_symlink():
        try:
            con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
            try:
                return con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                                   "AND name IN ('entries','settings')").fetchone()[0] == 2
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return False


def has_nested_mount(folder):
    # Linux bind mounts can have the same device number as the archive.
    mounts = Path('/proc/self/mountinfo')
    if mounts.is_file():
        with mounts.open('rb') as stream:
            for line in stream:
                raw = line.split()[4]
                name = re.sub(rb'\\([0-7]{3})', lambda match: bytes([int(match[1], 8)]), raw)
                if Path(os.fsdecode(name)).is_relative_to(folder):
                    return True
        return False
    if os.name == 'nt':
        # shutil.rmtree removes junctions themselves on supported Windows versions.
        return False
    return any(os.path.ismount(root) for root, _, _ in os.walk(folder))


def remove_files(folder: Path, task: Path):
    staged = task.with_suffix('.deleting')
    paths = (folder, task.parent, task, staged, folder / 'archive.lock')
    if any(p.is_symlink() or p.resolve() != p for p in paths):
        raise ExportError('Путь архива ведёт в другую папку. Удаление отменено.')
    if staged.exists() and folder.exists():
        raise ExportError('В папке снова появился архив с тем же именем. Проверьте путь перед удалением.')
    if staged.exists() and not staged.is_dir():
        raise ExportError('Путь для удаления архива занят другим файлом.')
    tree = staged if staged.is_dir() else folder
    if tree.is_dir() and (os.path.ismount(tree) or has_nested_mount(tree)):
        raise ExportError('В папке архива подключён диск. Отключите его перед удалением.')
    if folder.exists():
        if not folder.is_dir() or not (archive_folder(folder) or (
                task.is_file() and all(p.name == 'archive.lock' for p in folder.iterdir()))):
            raise ExportError('В выбранной папке нет базы или отчёта архива.')
        try:
            lock = exclusive_lock(folder / 'archive.lock')
        except OSError as exc:
            raise ExportError('Архив открыт другим процессом. Завершите работу с архивом.') from exc
        try:
            if os.name == 'nt':
                # Windows prevents renaming a directory with our open lock file.
                lock.close()
            folder.rename(staged)
        finally:
            lock.close()
    elif not staged.is_dir() and not task.is_file():
        raise ExportError('Архив не найден.')
    task.with_suffix('.json.tmp').unlink(missing_ok=True)
    task.unlink(missing_ok=True)
    if staged.is_dir():
        shutil.rmtree(staged)
