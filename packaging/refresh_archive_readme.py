"""Refresh a bundled README only when old, new and executable hashes are known."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import tempfile

from publish import gh, matching_asset


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def repack(source, target, readme, executable_hash):
    seen = set()
    binary = hashlib.sha256()
    class HashingReader:
        def __init__(self, stream):
            self.stream = stream
        def read(self, size=-1):
            block = self.stream.read(size)
            binary.update(block)
            return block
    with tarfile.open(source) as old, target.open('wb') as output, gzip.GzipFile(fileobj=output, filename='', mode='wb', mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w') as new:
        for member in old:
            if not member.isfile() or member.name not in ('README.md', 'LiveJournalArchive') or member.name in seen:
                raise ValueError('Unexpected archive member')
            seen.add(member.name)
            if member.name == 'README.md':
                member.size = len(readme)
                new.addfile(member, io.BytesIO(readme))
            else:
                new.addfile(member, HashingReader(old.extractfile(member)))
    if seen != {'README.md', 'LiveJournalArchive'} or binary.hexdigest() != executable_hash:
        raise ValueError('Executable differs from the verified package')
    return digest(target)


def release_info(repo, tag):
    result = gh('api', f'repos/{repo}/releases/tags/{tag}')
    if result.returncode:
        raise RuntimeError('Could not read the published release')
    return json.loads(result.stdout)


def refresh(root, repo, tag, old_hash, new_hash, executable_hash):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag) or not all(re.fullmatch(r'[0-9a-f]{64}', value) for value in (old_hash, new_hash, executable_hash)):
        raise ValueError('Expected a version tag and three SHA-256 hashes')
    release = release_info(repo, tag)
    if release.get('draft') or release.get('immutable'):
        raise ValueError('Release cannot be updated')
    name = f'JournalArchive-{tag[1:]}-linux-x64.tar.gz'
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for asset in (name, name + '.sha256'):
            result = gh('release', 'download', tag, '--pattern', asset, '--dir', str(folder), '--repo', repo)
            if result.returncode:
                raise RuntimeError('Could not download the published package')
        current = digest(folder / name)
        checksum = (folder / (name + '.sha256')).read_text(encoding='utf-8').split()
        if current not in (old_hash, new_hash) or len(checksum) != 2 or checksum[1] != name or checksum[0] not in (old_hash, new_hash):
            raise ValueError('Published package differs from the reviewed hashes')
        target = folder / 'updated' / name
        target.parent.mkdir()
        if repack(folder / name, target, (root / 'README.md').read_bytes(), executable_hash) != new_hash:
            raise ValueError('Updated package differs from the reviewed hash')
        sidecar = target.with_name(name + '.sha256')
        sidecar.write_text(new_hash + '  ' + name + '\n', encoding='utf-8')
        pending = ([str(target)] if current != new_hash else []) + ([str(sidecar)] if checksum[0] != new_hash else [])
        if pending:
            result = gh('release', 'upload', tag, *pending, '--clobber', '--repo', repo)
            if result.returncode:
                raise RuntimeError('Upload incomplete; rerun with the same reviewed hashes')
        assets = {a['name']: a for a in release_info(repo, tag)['assets']}
        if not all(path.name in assets and matching_asset(repo, assets[path.name], path) for path in (target, sidecar)):
            raise RuntimeError('Published files did not match the updated package')
        print('Verified updated README and unchanged executable:', tag, new_hash)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('tag', 'old-hash', 'new-hash', 'executable-hash'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    refresh(Path(__file__).resolve().parents[1], os.environ['GITHUB_REPOSITORY'],
            args.tag, args.old_hash, args.new_hash, args.executable_hash)
