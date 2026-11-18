"""Release publication tests use only local fixtures and a simulated GitHub CLI."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1] / 'packaging'))
import publish
import recover_release


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root/'release').mkdir()
        (self.root/'docs/releases').mkdir(parents=True)
        (self.root/'docs/releases/0.5.0.md').write_text('Release notes', encoding='utf-8')
        self.release = None
        self.calls = []
        self.race = False
        self.partial = False
        self.partial_done = False
        self.tag = 'v0.5.0'
        self.repo = 'owner/journal'
        self.add_package('windows-x64.exe')

    def add_package(self, platform):
        file = self.root/'release'/f'JournalArchive-0.5.0-{platform}'
        file.write_bytes(('native package ' + platform).encode())
        file.with_name(file.name+'.sha256').write_text(hashlib.sha256(file.read_bytes()).hexdigest()+'  '+file.name+'\n', encoding='utf-8')

    def asset(self, path):
        return {'id': len(self.release['assets']) + 1, 'name': path.name, 'size': path.stat().st_size,
                'digest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()}

    def fake_gh(self, *args):
        self.calls.append(args)
        code, output = 0, b''
        if args[0] == 'api':
            if self.release is None:
                code = 1
            else:
                output = json.dumps(self.release).encode()
        elif args[:2] == ('release', 'create'):
            self.release = {'draft': False, 'prerelease': False, 'assets': []}
            if self.race:
                code = 1  # A competing platform created the release first.
        elif args[:2] == ('release', 'upload'):
            for value in args[3:args.index('--repo')]:
                path = Path(value)
                self.release['assets'].append(self.asset(path))
                if self.partial and not self.partial_done:
                    self.partial_done = True
                    code = 1
                    break
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, stdout=output, stderr=b'simulated race' if code else b'')

    def run_publish(self, platforms):
        with patch.object(publish, 'gh', self.fake_gh):
            publish.publish(self.root, self.repo, self.tag, platforms)

    def test_windows_can_be_published_before_other_platforms(self):
        self.run_publish(['windows-x64.exe'])
        self.assertEqual(len(self.release['assets']), 2)
        self.assertTrue(all('windows' in a['name'] for a in self.release['assets']))
        self.assertIn('--verify-tag', next(c for c in self.calls if c[:2] == ('release', 'create')))

    def test_add_platform_and_repeat_without_replacing_existing_assets(self):
        self.run_publish(['windows-x64.exe'])
        windows = list(self.release['assets'])
        self.add_package('macos-arm64.zip')
        self.run_publish(['macos-arm64.zip'])
        uploads = len([c for c in self.calls if c[:2] == ('release', 'upload')])
        self.run_publish(['windows-x64.exe', 'macos-arm64.zip'])
        self.assertEqual(len(self.release['assets']), 4)
        self.assertEqual(self.release['assets'][:2], windows)
        self.assertEqual(len([c for c in self.calls if c[:2] == ('release', 'upload')]), uploads)
        self.assertFalse(any('--clobber' in c for c in self.calls))

    def test_creation_race_and_partial_upload_are_recovered(self):
        self.race = True
        self.partial = True
        self.run_publish(['windows-x64.exe'])
        self.assertEqual(len(self.release['assets']), 2)
        self.assertEqual(len({a['name'] for a in self.release['assets']}), 2)

    def test_corrupt_checksum_stops_before_release_creation(self):
        (self.root/'release/JournalArchive-0.5.0-windows-x64.exe').write_bytes(b'damaged')
        with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
            self.run_publish(['windows-x64.exe'])
        self.assertEqual(self.calls, [])

    def test_conflicting_published_binary_is_never_overwritten(self):
        self.run_publish(['windows-x64.exe'])
        self.release['assets'][0]['digest'] = 'sha256:' + '0'*64
        calls = len(self.calls)
        with self.assertRaisesRegex(ValueError, 'refusing to replace'):
            self.run_publish(['windows-x64.exe'])
        self.assertFalse(any(c[:2] == ('release', 'upload') for c in self.calls[calls:]))

    def test_recovery_accepts_only_passed_jobs_from_exact_tag_commit(self):
        commit = 'a'*40
        runs = [
            {'id': 1, 'head_sha': 'b'*40, 'workflow_id': 10, 'event': 'push', 'head_repository': {'full_name': self.repo}},
            {'id': 2, 'head_sha': commit, 'workflow_id': 10, 'event': 'pull_request', 'head_repository': {'full_name': self.repo}},
            {'id': 3, 'head_sha': commit, 'workflow_id': 10, 'event': 'push', 'head_repository': {'full_name': self.repo}}]
        seen = []
        def api(path):
            seen.append(path)
            if '/git/ref/' in path: return {'object': {'type': 'tag', 'sha': 'tag-object'}}
            if '/git/tags/' in path: return {'object': {'type': 'commit', 'sha': commit}}
            if path.endswith('workflows/build.yml'): return {'id': 10}
            if 'head_sha=' in path: return {'workflow_runs': runs}
            if '/runs/3/jobs' in path:
                return {'jobs': [{'name': 'package (windows-2022)', 'conclusion': 'success'},
                                 {'name': 'package (macos-15)', 'conclusion': 'cancelled'}]}
            if '/runs/3/artifacts' in path:
                return {'artifacts': [{'id': 100, 'name': 'LiveJournalArchive-windows-2022', 'expired': False},
                                     {'id': 101, 'name': 'LiveJournalArchive-macos-15', 'expired': False}]}
            raise AssertionError(path)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            for file in (self.root/'release').iterdir(): z.write(file, file.name)
            z.writestr('../outside.txt', 'must not be extracted')
        completed = subprocess.CompletedProcess([], 0, stdout=archive.getvalue())
        with patch.object(recover_release, 'api', api), patch.object(recover_release.subprocess, 'run', return_value=completed), patch.object(recover_release, 'publish') as published:
            recover_release.recover(self.root, self.repo, self.tag)
        published.assert_called_once_with(self.root, self.repo, self.tag, ['windows-x64.exe'])
        self.assertFalse((self.root/'outside.txt').exists())
        self.assertFalse(any('/runs/1/' in path or '/runs/2/' in path for path in seen))

    def test_recovery_rejects_trigger_tag_mismatch_before_any_api_call(self):
        with patch.dict(recover_release.os.environ, {'GITHUB_REF': 'refs/tags/v9.9.9', 'GITHUB_REF_NAME': 'v9.9.9', 'GITHUB_REPOSITORY': self.repo}), patch.object(recover_release.runpy, 'run_path', return_value={'__version__': '0.8.7'}), patch.object(recover_release, 'recover') as recovered:
            with self.assertRaisesRegex(ValueError, 'tag does not match'):
                recover_release.main()
            recovered.assert_not_called()

    def test_recovery_uses_current_version_for_manual_branch_run(self):
        with patch.dict(recover_release.os.environ, {'GITHUB_REF': 'refs/heads/main', 'GITHUB_REF_NAME': 'main', 'GITHUB_REPOSITORY': self.repo}), patch.object(recover_release.runpy, 'run_path', return_value={'__version__': '0.8.7'}), patch.object(recover_release, 'recover') as recovered:
            recover_release.main()
            self.assertEqual(recovered.call_args.args[1:], (self.repo, 'v0.8.7'))


if __name__ == '__main__':
    unittest.main()
