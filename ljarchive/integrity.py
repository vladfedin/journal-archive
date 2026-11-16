"""Local integrity checks independent of remote availability."""
import hashlib
import json
from pathlib import Path

from .storage import atomic_write


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
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


def verify_archive(store):
    issues = []
    for row in store.db.execute("PRAGMA quick_check"):
        if row[0] != "ok":
            issues.append({"kind": "database", "message": "SQLite сообщает о повреждении базы."})
    for kind, table in (("post", "entries"), ("comment", "comments"), ("account", "account_sections"), ("message", "messages")):
        for row in store.db.execute(f"SELECT r.id,r.payload,h.digest FROM {table} r LEFT JOIN record_hashes h ON h.object_id=r.id AND h.kind=?", (kind,)):
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
            for name, expected in manifest["files"].items():
                path = (store.folder / name).resolve()
                if not path.is_relative_to(store.folder.resolve()):
                    raise ValueError()
                files_checked += 1
                if not path.is_file() or file_hash(path) != expected:
                    issues.append({"kind": "file", "path": name, "message": "Файл отсутствует или изменился."})
        except (ValueError, OSError, TypeError):
            issues.append({"kind": "manifest", "message": "Не удалось проверить список контрольных сумм."})
    return {"ok": not issues, "manifest_present": manifest_path.exists(), "files_checked": files_checked, "issues": issues}
