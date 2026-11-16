"""Validate all four native packages, then publish an existing version tag via gh."""
import hashlib
import os
from pathlib import Path
import runpy
import subprocess

root = Path(__file__).resolve().parents[1]
version = runpy.run_path(str(root / 'ljarchive/__init__.py'))['__version__']
tag = os.environ['GITHUB_REF_NAME']
if tag != 'v' + version:
    raise SystemExit('Tag and package version do not match')
notes = root / 'docs/releases' / (version + '.md')
if not notes.is_file():
    raise SystemExit('Release notes are missing')
assets = []
for platform in ('windows-x64.exe', 'macos-arm64.zip', 'macos-x64.zip', 'linux-x64.tar.gz'):
    file = root / 'release' / f'JournalArchive-{version}-{platform}'
    checksum = file.with_name(file.name + '.sha256')
    if not file.is_file() or not checksum.is_file():
        raise SystemExit(f'Missing release asset: {file.name}')
    expected, name = checksum.read_text(encoding='utf-8').strip().split(maxsplit=1)
    if name != file.name or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
        raise SystemExit(f'Checksum mismatch: {file.name}')
    assets.extend([str(file), str(checksum)])
subprocess.run(['gh', 'release', 'create', tag, *assets, '--verify-tag', '--latest',
                '--title', f'Архив ЖЖ {version}', '--notes-file', str(notes),
                '--repo', os.environ['GITHUB_REPOSITORY']], check=True, cwd=root)
