import pytest

from ljarchive.storage import Store
from ljarchive.sync import Scope, SyncExporter
from test_sync import Journal


class FreshJournal(Journal):
    def __init__(self, *, same_day=False):
        super().__init__()
        self.same_day = same_day

    def entry(self, eid):
        item = super().entry(eid)
        if eid == 3 and not self.same_day:
            item['eventtime'] = '2026-11-02 13:00:00'
        return item

    def call(self, method, **params):
        if method == 'getdaycounts':
            response = super().call(method, **params)
            if self.same_day:
                response['daycounts'][-1]['count'] = 2
            else:
                response['daycounts'].append({'date': '2026-11-02', 'count': 1})
            return response
        if method == 'getevents' and params['selecttype'] == 'day' and params['year'] == 2026:
            self.budget.consume('test'); self.calls.append((method, params))
            return {'events': [self.entry(2), self.entry(3)] if self.same_day else [self.entry(3)]}
        return super().call(method, **params)


@pytest.mark.parametrize('same_day', [False, True])
@pytest.mark.parametrize('finished', [False, True])
def test_resume_fetches_new_posts_and_their_comments_without_reloading_old_days(tmp_path, same_day, finished):
    SyncExporter(tmp_path, Journal(), Scope()).run()
    store = Store(tmp_path)
    if not finished:
        state = store.get('sync_job'); state.update(done=False, phase='reactions')
        with store.db: store.set('sync_job', state)
    store.close()
    client = FreshJournal(same_day=same_day)
    updates = []
    report = SyncExporter(tmp_path, client, Scope(), resume=True, progress=lambda **v: updates.append(v)).run()
    assert report['status'] == 'complete' and report['entries_saved'] == 3 and report['comments_saved'] == 6
    posts = [p for m, p in client.calls if m == 'getevents']
    assert len(posts) == 1 and posts[0]['selecttype'] == 'day' and posts[0]['year'] == 2026
    assert all(p['itemid'] == 3 for m, p in client.calls if m == 'getcomments')
    assert sum(m == 'getdaycounts' for m, _ in client.calls) == 1
    assert any(s.get('message') == 'Проверяем свежие записи…' for s in updates)


def test_resume_respects_period_and_unchanged_days(tmp_path):
    scope = Scope('period', '2005-01-01', '2005-12-31')
    SyncExporter(tmp_path, Journal(), scope).run()
    client = FreshJournal()
    report = SyncExporter(tmp_path, client, scope, resume=True).run()
    assert report['entries_saved'] == 1 and report['status'] == 'complete'
    assert not any(m in ('getevents', 'getcomments') for m, _ in client.calls)
