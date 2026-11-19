import json
import shutil

import pytest

from ljarchive.fulltext import PARTS
from ljarchive.render import build_archive
from ljarchive.search_library import SearchLibrary
from ljarchive.storage import Store


@pytest.mark.parametrize('fts', [True, False])
def test_fulltext_search_updates_and_portable_copy(tmp_path, fts):
    folder = tmp_path/'journal'
    store = Store(folder)
    with store.db:
        store.set('username', 'journal')
        store.save_entry({'itemid': 3, 'subject': 'Утро', 'eventtime': '2022-01-01',
                          'event': '<lj-cut><p>Ёлка</p><p>у моря</p></lj-cut><img src="https://example.org/old.jpg" alt="Зимняя гавань">',
                          'props': {'taglist': 'воспоминания'}, 'security':'private'})
        store.db.execute("INSERT INTO users VALUES(7, 'alice')")
        store.save_comment({'id': 9, 'jitemid': 3, 'posterid': 7, 'state': 'S', 'date': '2023-02-01',
                            'body': 'Моря здесь нет. Под окном ёлка.'})
        store.db.executemany('INSERT INTO messages VALUES(?,?)',
                             [(i,json.dumps({'qid':i, 'when':i, 'body':'Письмо', 'from':'bob', 'to':'journal',
                                             'subject':'Старая ёлка' if i==1 else 'Сохранено', 'type':18}))
                              for i in range(1,102)])
        store.db.execute('INSERT INTO account_sections VALUES(?,?)',('profile',json.dumps({'status':'captured','data':{'fullname':'Летописец'}})))
    report = {'username':'journal','status':'complete','issues':[], 'entries_saved':1,'comments_saved':1}
    build_archive(store,report)
    library = SearchLibrary()
    library.fts = fts

    def hits(q, **filters):
        result = library.search(tmp_path, {'q':q,**filters})
        assert not result['unavailable']
        return result['rows']

    assert [d['kind'] for d in hits('«елка у моря»')] == ['post']
    assert len(hits('моря елка')) == 2
    reply, = hits('елка',author='ALICE',year='2023')
    assert reply['access']=='Личная · Скрыт' and reply['href']=='entries/3.html#comment-9'
    assert hits('воспоминания',kind='post')
    assert hits('Зимняя гавань',kind='material')[0]['href']=='entries/3.html'
    assert hits('"old.jpg"',kind='material')
    assert hits('Летописец',kind='profile')[0]['href']=='account.html'
    letter, = hits('елка',kind='message')
    assert letter['href']=='messages-2.html#message-1'
    assert 'id="message-1"' in (folder/'messages-2.html').read_text()
    assert len(hits('елка', messages='0'))==2
    with store.db:
        store.save_comment({'id':9,'jitemid':3,'date':'2023','body':'Обновлённый ответ'})
    assert hits('обновленный',kind='comment')
    assert not hits('елка',kind='comment')
    build_archive(store,report,entry_ids=[3])
    store.close()
    portable = tmp_path/'portable'/'assets'
    portable.mkdir(parents=True)
    for part in PARTS:
        name = f'fulltext-{part}.js'
        shutil.copy(folder/'assets'/name,portable/name)
    assert len(hits('обновлённый'))==2
    assert hits('обновлённый',archive='portable')[0]['href'].endswith('#comment-9')
    legacy = tmp_path/'legacy'/'data'
    legacy.mkdir(parents=True)
    for name in ('archive.json', 'materials.json'):
        shutil.copy(folder/'data'/name, legacy/name)
    assert hits('елка',kind='message',archive='legacy')[0]['href'] == 'messages-2.html#message-1'
