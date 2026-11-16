"""Recover successful packages from trusted builds of the exact tagged commit."""
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import zipfile

from publish import PLATFORMS, publish

RUNNERS = {'windows-x64.exe': 'windows-2022', 'macos-arm64.zip': 'macos-15',
           'macos-x64.zip': 'macos-15-intel', 'linux-x64.tar.gz': 'ubuntu-22.04'}


def api(path):
    result = subprocess.run(['gh', 'api', path], check=True, stdout=subprocess.PIPE)
    return json.loads(result.stdout)


def recover(root, repo, tag):
    reference = api(f'repos/{repo}/git/ref/tags/{tag}')['object']
    for _ in range(5):
        if reference['type'] == 'commit':
            break
        reference = api(f"repos/{repo}/git/tags/{reference['sha']}")['object']
    if reference['type'] != 'commit':
        raise ValueError('Cannot resolve the release tag')
    commit = reference['sha']
    workflow = api(f'repos/{repo}/actions/workflows/build.yml')
    runs = api(f'repos/{repo}/actions/workflows/build.yml/runs?head_sha={commit}&per_page=100')['workflow_runs']
    candidates = {}
    for run in runs:
        if run['head_sha'] != commit or run['workflow_id'] != workflow['id'] or run['event'] not in ('push', 'workflow_dispatch'):
            continue
        if run['head_repository']['full_name'] != repo:
            continue
        jobs = api(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")['jobs']
        passed = {job['name'] for job in jobs if job['conclusion'] == 'success'}
        artifacts = api(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100")['artifacts']
        for platform, runner in RUNNERS.items():
            if platform in candidates or f'package ({runner})' not in passed:
                continue
            artifact = next((a for a in artifacts if a['name'] == f'LiveJournalArchive-{runner}' and not a['expired']), None)
            if artifact:
                candidates[platform] = artifact
    if not candidates:
        print('No verified packages available for', tag)
        return
    # Windows first; other successful packages are added immediately afterwards.
    for platform in PLATFORMS:
        if platform not in candidates:
            print('Still pending:', platform)
            continue
        artifact = candidates[platform]
        result = subprocess.run(['gh', 'api', f"repos/{repo}/actions/artifacts/{artifact['id']}/zip"],
                                check=True, stdout=subprocess.PIPE)
        folder = root / 'release'
        folder.mkdir(exist_ok=True)
        name = f'JournalArchive-{tag[1:]}-{platform}'
        with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
            # Read only the two expected basenames; never extract arbitrary paths.
            for member in (name, name + '.sha256'):
                if archive.namelist().count(member) != 1:
                    raise ValueError('Missing or duplicate artifact member: ' + member)
                (folder / member).write_bytes(archive.read(member))
        publish(root, repo, tag, [platform])


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    version = runpy.run_path(str(root / 'ljarchive/__init__.py'))['__version__']
    recover(root, os.environ['GITHUB_REPOSITORY'], 'v' + version)
