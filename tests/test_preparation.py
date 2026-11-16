import threading

import pytest
import requests

from ljarchive.demo import create_demo
from ljarchive.storage import atomic_write, dumps
from test_render_server import local_server


def test_open_old_archive_returns_progress_and_does_not_duplicate_rebuild(local_server, monkeypatch):
    server, base = local_server
    folder = server.jobs.output / 'fixture'
    report = create_demo(folder)
    report['format_version'] = '0.1.0'
    atomic_write(folder / 'export-report.json', dumps(report))
    entered, release = threading.Event(), threading.Event()
    from ljarchive.render import build_archive
    calls = []
    def slow_build(store, report, **kwargs):
        calls.append(1)
        kwargs['progress'](message='Страницы', completed=2, total=6)
        entered.set()
        assert release.wait(10)
        return build_archive(store, report, **kwargs)
    monkeypatch.setattr('ljarchive.render.build_archive', slow_build)
    try:
        with requests.Session() as session:
            session.get(base)
            response = session.get(base + '/archive/fixture/index.html', timeout=3)
            assert response.status_code == 200 and 'data-preparing="fixture"' in response.text
            assert entered.wait(2)
            state = session.get(base + '/api/preparation/fixture', timeout=2).json()
            assert state['completed'] == 2 and state['total'] == 6
            assert state['phase'] == 'preparing'
            assert session.get(base + '/archive/fixture/statistics.html', timeout=2).status_code == 200
            assert len(calls) == 1
            release.set()
            for thread in server.jobs.preparation_threads:
                thread.join(10)
            assert session.get(base + '/api/preparation/fixture').json()['phase'] == 'complete'
            assert 'data-preparing=' not in session.get(base + '/archive/fixture/index.html').text
            assert len(calls) == 1
    finally:
        release.set()
        for thread in server.jobs.preparation_threads:
            thread.join(10)


def test_preparation_failure_keeps_readable_error_and_allows_retry(local_server, monkeypatch):
    server, _ = local_server
    folder = server.jobs.output / 'fixture'
    report = create_demo(folder)
    report['format_version'] = '0.1.0'
    atomic_write(folder / 'export-report.json', dumps(report))
    def fail(*a, **kw):
        raise OSError('disk problem')
    monkeypatch.setattr('ljarchive.render.build_archive', fail)
    server.jobs.prepare_format('fixture')
    server.jobs.preparation_threads[0].join(5)
    assert server.jobs.preparation('fixture')['phase'] == 'error'
    monkeypatch.undo()
    server.jobs.prepare_format('fixture')
    server.jobs.preparation_threads[-1].join(10)
    assert server.jobs.preparation('fixture')['phase'] == 'complete'
