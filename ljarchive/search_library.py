"""Local FTS5 indexes built lazily from each saved journal."""
import json
import re
import sqlite3
import threading

from .fulltext import PARTS, WORDS, fold, journal_docs, material_docs, personal_docs
from .storage import Store
from .personal import account_data

PAGE_SIZE = 40


def clauses(query):
    result = []
    for phrase, word in re.findall(r'"([^"]*)"?|([^\s"]+)', fold(query).replace('«', '"').replace('»', '"')):
        tokens = WORDS.findall(phrase or word)
        result.extend([' '.join(tokens)] if phrase and tokens else tokens)
    return result


def read_archive_json(folder, name):
    path = folder/'data'/name
    if not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('Некорректный путь данных.')
    return json.loads(path.read_text(encoding='utf8'))


def load_docs(folder):
    from .gallery import inventory
    if not (folder/'archive.sqlite3').is_file():
        if not (folder/'assets/fulltext-journal-0.js').exists():
            data = read_archive_json(folder, 'archive.json')
            user = data.get('journal', {}).get('username') or folder.name
            entries, comments = data['entries'], data['comments']
            yield from journal_docs(user, entries, comments)
            yield from personal_docs(user, data.get('account', {}))
            if (folder/'data/materials.json').exists():
                yield from material_docs(read_archive_json(folder, 'materials.json')['materials'], entries, comments, user)
            return
        for part in PARTS:
            path = folder/'assets'/f'fulltext-{part}.js'
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
                raise ValueError('Некорректный путь индекса.')
            payload = path.read_text(encoding='utf-8')
            yield from json.loads(payload.removeprefix('window.searchPart(').removesuffix(');'))['docs']
        return
    store = Store.reader(folder)
    try:
        entries, comments = store.entries(), store.comments()
        user = store.get('username') or folder.name
        yield from journal_docs(user, entries, comments)
        yield from personal_docs(user, account_data(store))
        yield from material_docs(inventory(store, entries, comments), entries, comments, user)
    finally:
        store.close()


class SearchLibrary:
    def __init__(self):
        self.indexes = {}
        probe = sqlite3.connect(':memory:')
        try:
            probe.execute('CREATE VIRTUAL TABLE test USING fts5(text)')
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        finally:
            probe.close()
        self.lock = threading.Lock()

    def index(self, folder):
        dbpath = folder / 'archive.sqlite3'
        stamp = []
        if dbpath.exists():
            paths = (dbpath, folder/'archive.sqlite3-wal')
        elif (folder/'assets/fulltext-journal-0.js').exists():
            paths = (folder/'assets'/f'fulltext-{part}.js' for part in PARTS)
        else:
            paths = (folder/'data/archive.json', folder/'data/materials.json')
        for path in paths:
            try:
                st = path.stat()
                stamp.append((st.st_ino, st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                stamp.append(None)
        previous = self.indexes.get(folder)
        if previous and previous[0] == stamp:
            return previous[1]
        index = sqlite3.connect(':memory:', check_same_thread=False)
        try:
            if self.fts:
                index.execute("""CREATE VIRTUAL TABLE words USING fts5(title, body, author, tags,
                                 content='', tokenize='unicode61 remove_diacritics 0')""")
            else:
                index.execute('CREATE TABLE words(title, body, author, tags)')
            index.execute('CREATE TABLE docs(id INTEGER PRIMARY KEY, kind, author, date, payload)')
            for i, d in enumerate(load_docs(folder), 1):
                fields = [fold(d[k] or '') for k in ('title','text','author','tags')]
                author = fields[2]
                if not self.fts:
                    fields = [' ' + ' '.join(WORDS.findall(field)) + ' ' for field in fields]
                index.execute('INSERT INTO words(rowid,title,body,author,tags) VALUES(?,?,?,?,?)', (i,*fields))
                index.execute('INSERT INTO docs VALUES(?,?,?,?,?)', (i,d['kind'],author,d['date'],json.dumps(d, ensure_ascii=False)))
            index.commit()
        except BaseException:
            index.close()
            raise
        self.indexes[folder] = stamp, index
        if previous:
            previous[1].close()
        return index

    def search(self, root, params):
        terms = clauses(params.get('q','')[:512])
        page = max(1, int(params.get('page','1')))
        result = {'rows': [], 'total': 0, 'unavailable': [], 'size': PAGE_SIZE}
        if not terms:
            return result
        if self.fts:
            predicate = ['words MATCH ?']
            args = [' AND '.join('"'+term+'"' for term in terms)]
            score = 'bm25(words,6,1,3,2)'
            weights = []
        else:
            predicate = ["instr(words.title || '|' || words.body || '|' || words.author || '|' || words.tags, ?) > 0" for _ in terms]
            args = [' '+term+' ' for term in terms]
            score = '-(' + '+'.join('(instr(words.title, ?) > 0)' for _ in terms) + ')'
            weights = list(args)
        if params.get('kind'):
            predicate.append('docs.kind = ?')
            args.append(params['kind'])
        if params.get('messages') == '0':
            predicate.append("docs.kind != 'message'")
        if params.get('author'):
            predicate.append('docs.author = ?')
            args.append(fold(params['author'].strip()))
        if params.get('year'):
            predicate.append('substr(docs.date,1,4) = ?')
            args.append(params['year'])
        sql = ' FROM words JOIN docs ON docs.id=words.rowid WHERE ' + ' AND '.join(predicate)
        folders = {p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                   and re.fullmatch(r'[a-z0-9_]{1,15}(?:-latest-[1-9][0-9]?)?', p.name)
                   and not (p/'archive.sqlite3').is_symlink()
                   and ((p/'archive.sqlite3').is_file() or (p/'assets/fulltext-journal-0.js').is_file() or (p/'data/archive.json').is_file())}
        matches = []
        with self.lock:
            for folder in self.indexes.keys() - folders:
                self.indexes.pop(folder)[1].close()
            for folder in sorted(folders):
                if params.get('archive') and folder.name != params['archive']:
                    continue
                try:
                    db = self.index(folder)
                    count = db.execute('SELECT count(*)'+sql,args).fetchone()[0]
                    result['total'] += count
                    rows = db.execute(f'SELECT {score}, docs.date, docs.id' + sql +
                                      ' ORDER BY 1,2 DESC,3 LIMIT ?', [*weights, *args, min(count, page * PAGE_SIZE)])
                    matches.extend((rank, date, folder, ident) for rank, date, ident in rows)
                except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
                    result['unavailable'].append(folder.name)
            matches.sort(key=lambda m: m[1] or '', reverse=True)
            matches.sort(key=lambda m: m[0])
            for _, _, folder, ident in matches[(page-1)*PAGE_SIZE:page*PAGE_SIZE]:
                db = self.indexes[folder][1]
                payload, = db.execute('SELECT payload FROM docs WHERE id=?', (ident,)).fetchone()
                d = json.loads(payload)
                d['archive'] = folder.name
                result['rows'].append(d)
        return result
