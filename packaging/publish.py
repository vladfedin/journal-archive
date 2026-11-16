"""Publish verified native packages independently; never replace a conflicting asset."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

PLATFORMS = ('windows-x64.exe', 'macos-arm64.zip', 'macos-x64.zip', 'linux-x64.tar.gz')


def gh(*args):
    return subprocess.run(['gh', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def assets_for(root, tag, platforms):
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', tag):
        raise ValueError('Invalid version tag')
    assets = []
    for platform in platforms:
        if platform not in PLATFORMS:
            raise ValueError('Unknown platform')
        file = root / 'release' / f'JournalArchive-{tag[1:]}-{platform}'
        checksum = file.with_name(file.name + '.sha256')
        if not file.is_file() or not checksum.is_file():
            raise ValueError(f'Missing release asset: {file.name}')
        expected, name = checksum.read_text(encoding='utf-8').strip().split(maxsplit=1)
        if name != file.name or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Checksum mismatch: {file.name}')
        assets.extend([file, checksum])
    return assets


def release_info(repo, tag):
    result = gh('api', f'repos/{repo}/releases/tags/{tag}')
    return json.loads(result.stdout) if result.returncode == 0 else None


def matching_asset(repo, existing, path):
    if existing['size'] != path.stat().st_size:
        return False
    expected = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    if existing.get('digest'):
        return existing['digest'] == expected
    # Older GitHub assets may not expose a digest. Verify their bytes before reuse.
    with tempfile.TemporaryFile() as downloaded:
        result = subprocess.run(['gh', 'api', f"repos/{repo}/releases/assets/{existing['id']}",
                                 '-H', 'Accept: application/octet-stream'], stdout=downloaded, check=False)
        if result.returncode:
            raise RuntimeError('Cannot verify existing release asset')
        downloaded.seek(0)
        digest = hashlib.file_digest(downloaded, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(downloaded.read()).hexdigest()
    return expected == 'sha256:' + digest


def publish(root, repo, tag, platforms):
    assets = assets_for(root, tag, platforms)
    notes = root / 'docs/releases' / (tag[1:] + '.md')
    if not notes.is_file():
        raise ValueError('Release notes are missing')
    release = release_info(repo, tag)
    if release is None:
        result = gh('release', 'create', tag, '--verify-tag', '--latest', '--title', f'Архив ЖЖ {tag[1:]}',
                    '--notes-file', str(notes), '--repo', repo)
        release = release_info(repo, tag)
        # Another platform can create the same release concurrently.
        if release is None:
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('Refusing to change an existing draft or prerelease')
    for attempt in range(4):
        existing = {a['name']: a for a in release['assets']}
        missing = []
        for path in assets:
            if path.name not in existing:
                missing.append(str(path))
            elif not matching_asset(repo, existing[path.name], path):
                raise ValueError(f'Existing asset differs; refusing to replace: {path.name}')
        if not missing:
            print('Published and verified:', ', '.join(platforms), flush=True)
            return
        if attempt == 3:
            break
        result = gh('release', 'upload', tag, *missing, '--repo', repo)
        release = release_info(repo, tag)
        if release is None:
            raise RuntimeError('Cannot verify release after upload')
        # Re-read after a race or partial upload; only missing identical assets are retried.
    raise RuntimeError('Release upload did not finish: ' + result.stderr.decode('utf-8', errors='replace'))


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', choices=PLATFORMS, action='append')
    parser.add_argument('--tag', default=os.environ.get('GITHUB_REF_NAME'))
    args = parser.parse_args()
    version = runpy.run_path(str(root / 'ljarchive/__init__.py'))['__version__']
    if args.tag != 'v' + version:
        raise SystemExit('Tag and package version do not match')
    publish(root, os.environ['GITHUB_REPOSITORY'], args.tag, args.platform or PLATFORMS)


if __name__ == '__main__':
    main()
