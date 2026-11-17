import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location('update_release_notes', Path(__file__).parents[1] / 'packaging/update_release_notes.py')
notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notes)


def source(tmp_path):
    path = tmp_path / 'docs/releases/0.7.7.md'
    path.parent.mkdir(parents=True)
    path.write_text('Updated public description\n', encoding='utf-8')
    return path


def test_changes_only_description_of_requested_release(tmp_path, monkeypatch):
    path = source(tmp_path)
    calls = []
    def gh(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({'body': 'Old description', 'draft': False}).encode(), stderr=b'')
    monkeypatch.setattr(notes, 'gh', gh)
    notes.update(tmp_path, 'owner/project', 'v0.7.7')
    assert calls == [('api', 'repos/owner/project/releases/tags/v0.7.7'),
                     ('release', 'edit', 'v0.7.7', '--notes-file', str(path), '--repo', 'owner/project')]


def test_matching_description_is_not_written_again(tmp_path, monkeypatch):
    path = source(tmp_path)
    calls = []
    def gh(*args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({'body': path.read_text(), 'draft': False}).encode(), stderr=b'')
    monkeypatch.setattr(notes, 'gh', gh)
    notes.update(tmp_path, 'owner/project', 'v0.7.7')
    assert len(calls) == 1


def test_unpublished_release_is_not_created_but_authentication_failure_is_reported(tmp_path, monkeypatch):
    source(tmp_path)
    monkeypatch.setattr(notes, 'gh', lambda *a: subprocess.CompletedProcess(a, 1, stdout=b'', stderr=b'HTTP 404'))
    notes.update(tmp_path, 'owner/project', 'v0.7.7')
    monkeypatch.setattr(notes, 'gh', lambda *a: subprocess.CompletedProcess(a, 1, stdout=b'', stderr=b'HTTP 401'))
    with pytest.raises(RuntimeError, match='Could not read release'):
        notes.update(tmp_path, 'owner/project', 'v0.7.7')


def test_invalid_tag_cannot_select_foreign_file(tmp_path):
    with pytest.raises(ValueError, match='Invalid version tag'):
        notes.update(tmp_path, 'owner/project', '../../file')


def test_changed_tags_selects_version_notes_only(monkeypatch, tmp_path):
    monkeypatch.setattr(notes.subprocess, 'check_output', lambda *a, **k: b'docs/releases/0.7.7.md\0README.md\0docs/releases/README.md\0')
    assert notes.changed_tags(tmp_path, 'a' * 40, 'b' * 40) == ['v0.7.7']
    with pytest.raises(ValueError, match='Expected commit IDs'):
        notes.changed_tags(tmp_path, '--all', 'b' * 40)
