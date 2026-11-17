"""Update published release descriptions from changed versioned Markdown files."""
import json
import os
from pathlib import Path
import re
import subprocess


def gh(*args):
    return subprocess.run(['gh', *args], capture_output=True, check=False)


def changed_tags(root, before, after):
    if not all(re.fullmatch(r'[0-9a-f]{40}', value or '') for value in (before, after)):
        raise ValueError('Expected commit IDs')
    paths = subprocess.check_output(['git', 'diff', '--name-only', '--diff-filter=AM', '-z',
                                     before, after, '--', 'docs/releases/'], cwd=root).decode().split('\0')
    return sorted({'v' + match[1] for path in paths
                   if (match := re.fullmatch(r'docs/releases/(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)\.md', path))})


def update(root, repo, tag):
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?', tag):
        raise ValueError('Invalid version tag')
    notes = root / 'docs/releases' / (tag[1:] + '.md')
    body = notes.read_text(encoding='utf-8')
    result = gh('api', f'repos/{repo}/releases/tags/{tag}')
    if result.returncode:
        if b'HTTP 404' in result.stderr:
            print('Not published yet:', tag)
            return
        raise RuntimeError('Could not read release: ' + result.stderr.decode('utf-8', errors='replace'))
    release = json.loads(result.stdout)
    if release.get('draft'):
        raise ValueError('Use the publishing workflow for draft releases')
    if (release.get('body') or '').strip() == body.strip():
        print('Description already matches:', tag)
        return
    result = gh('release', 'edit', tag, '--notes-file', str(notes), '--repo', repo)
    if result.returncode:
        raise RuntimeError('Could not update release: ' + result.stderr.decode('utf-8', errors='replace'))
    print('Updated description:', tag)


def main():
    root = Path(__file__).resolve().parents[1]
    tags = ([os.environ['NOTES_TAG']] if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
            else changed_tags(root, os.environ.get('BEFORE_COMMIT'), os.environ.get('GITHUB_SHA')))
    for tag in tags:
        update(root, os.environ['GITHUB_REPOSITORY'], tag)


if __name__ == '__main__':
    main()
