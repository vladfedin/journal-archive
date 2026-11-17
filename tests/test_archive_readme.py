import hashlib
import io
import json
from pathlib import Path
import sys
import shutil
import subprocess
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'packaging'))
import refresh_archive_readme as refresh


def fixture(tmp_path, extra=None):
    source = tmp_path / 'original.tar.gz'
    binary = b'executable fixture'
    items = [('LiveJournalArchive', binary), ('README.md', b'Old description')]
    if extra:
        items.append(extra)
    with tarfile.open(source, 'w:gz') as archive:
        for name, body in items:
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(body), 0o755, 123456789
            archive.addfile(member, io.BytesIO(body))
    return source, binary


def test_repack_changes_only_readme_and_is_reproducible(tmp_path):
    source, binary = fixture(tmp_path)
    first, second = tmp_path / 'first.tar.gz', tmp_path / 'second.tar.gz'
    binary_hash = hashlib.sha256(binary).hexdigest()
    first_hash = refresh.repack(source, first, b'Public description', binary_hash)
    assert refresh.repack(first, second, b'Public description', binary_hash) == first_hash
    with tarfile.open(first) as archive:
        assert archive.extractfile('README.md').read() == b'Public description'
        assert archive.extractfile('LiveJournalArchive').read() == binary
        assert archive.getmember('LiveJournalArchive').mode == 0o755
        assert archive.getmember('LiveJournalArchive').mtime == 123456789


def test_changed_executable_is_rejected(tmp_path):
    source, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match='Executable differs'):
        refresh.repack(source, tmp_path / 'out.tar.gz', b'Public description', '0' * 64)


def test_unexpected_or_duplicate_archive_member_is_rejected(tmp_path):
    for extra in (('../outside', b'bad'), ('README.md', b'duplicate')):
        source, binary = fixture(tmp_path, extra)
        with pytest.raises(ValueError, match='Unexpected archive member'):
            refresh.repack(source, tmp_path / 'out.tar.gz', b'Public description', hashlib.sha256(binary).hexdigest())


@pytest.mark.parametrize('changed', [False, True])
def test_resume_partial_upload_repairs_checksum_and_rejects_foreign_package(tmp_path, monkeypatch, changed):
    source, binary = fixture(tmp_path)
    old_hash = refresh.digest(source)
    (tmp_path / 'README.md').write_bytes(b'Public description')
    new = tmp_path / 'new.tar.gz'
    binary_hash = hashlib.sha256(binary).hexdigest()
    new_hash = refresh.repack(source, new, b'Public description', binary_hash)
    if changed:
        new.write_bytes(b'unreviewed package')
    name = 'JournalArchive-0.7.8-linux-x64.tar.gz'
    writes = []
    def gh(*args):
        output = b''
        if args[0] == 'api':
            output = json.dumps({'assets': [{'name': name}, {'name': name + '.sha256'}]}).encode()
        elif args[:2] == ('release', 'download'):
            filename, folder = args[args.index('--pattern') + 1], Path(args[args.index('--dir') + 1])
            if filename.endswith('.sha256'):
                (folder / filename).write_text(old_hash + '  ' + name + '\n')
            else:
                shutil.copy2(new, folder / filename)
        elif args[:2] == ('release', 'upload'):
            writes.append(args)
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b'')
    monkeypatch.setattr(refresh, 'gh', gh)
    monkeypatch.setattr(refresh, 'matching_asset', lambda *a: True)
    if changed:
        with pytest.raises(ValueError, match='differs from the reviewed hashes'):
            refresh.refresh(tmp_path, 'owner/project', 'v0.7.8', old_hash, new_hash, binary_hash)
        assert not writes
    else:
        refresh.refresh(tmp_path, 'owner/project', 'v0.7.8', old_hash, new_hash, binary_hash)
        assert len(writes) == 1
        assert [Path(x).name for x in writes[0][3:writes[0].index('--clobber')]] == [name + '.sha256']
