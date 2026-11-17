"""Local integrity checks independent of remote availability."""
import hashlib
import json
import time
from pathlib import Path

from .storage import atomic_write


def file_hash(path, check=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if check:
                check()
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(folder):
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.suffix in (".html", ".css", ".js", ".json", ".jpg", ".png", ".gif", ".webp", ".avif", ".svg", ".ico", ".mp4", ".webm", ".ogv", ".mov", ".m4v"):
            relative = path.relative_to(folder)
            if relative.parts[0] not in ("raw", "quarantine", ".video-work") and str(relative) != "integrity.json":
                files[relative.as_posix()] = file_hash(path)
    atomic_write(folder / "integrity.json", json.dumps({"algorithm": "sha256", "files": files}, indent=2) + "\n")


def verify_archive(store, progress=None, check=None):
    issues = []
    progress = progress or (lambda **values: None)
    check = check or (lambda: None)
    progress(phase="verify", message="Проверяем базу архива…", completed=0, total=0)
    check()
    for row in store.db.execute("PRAGMA quick_check"):
        if row[0] != "ok":
            issues.append({"kind": "database", "message": "SQLite сообщает о повреждении базы."})
    for kind, table in (("post", "entries"), ("comment", "comments"), ("account", "account_sections"), ("message", "messages")):
        for row in store.db.execute(f"SELECT r.id,r.payload,h.digest FROM {table} r LEFT JOIN record_hashes h ON h.object_id=r.id AND h.kind=?", (kind,)):
            check()
            digest = hashlib.sha256(row[1].encode()).hexdigest()
            if digest != row[2]:
                issues.append({"kind": kind, "id": row[0], "message": "Контрольная сумма записи не совпадает."})
        for row in store.db.execute(f"SELECT h.object_id FROM record_hashes h LEFT JOIN {table} r ON r.id=h.object_id WHERE h.kind=? AND r.id IS NULL", (kind,)):
            issues.append({"kind": kind, "id": row[0], "message": "Запись отсутствует в базе, но есть в списке контрольных сумм."})
    manifest_path = store.folder / "integrity.json"
    files_checked = 0
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("algorithm") != "sha256" or not isinstance(manifest.get("files"), dict):
                raise ValueError()
            total = len(manifest["files"])
            updated = 0
            def tick(force=False):
                nonlocal updated
                check()
                moment = time.monotonic()
                if force or moment - updated >= .25:
                    progress(phase="verify", message=f"Проверяем файлы · {files_checked} / {total}",
                             completed=files_checked, total=total)
                    updated = moment
            tick(True)
            for name, expected in manifest["files"].items():
                tick()
                path = (store.folder / name).resolve()
                if not path.is_relative_to(store.folder.resolve()):
                    raise ValueError()
                if not path.is_file() or file_hash(path, check=tick) != expected:
                    issues.append({"kind": "file", "path": name, "message": "Файл отсутствует или изменился."})
                files_checked += 1
            tick(True)
        except (ValueError, OSError, TypeError):
            issues.append({"kind": "manifest", "message": "Не удалось проверить список контрольных сумм."})
    return {"ok": not issues, "manifest_present": manifest_path.exists(), "files_checked": files_checked, "issues": issues}
