"""Small, clearly labeled fictional archive for offline UI exploration."""
from pathlib import Path

from .exporter import now
from .render import build_archive
from .reactions import parse_authors
from .storage import Store


def create_demo(folder: Path, progress=None):
    store = Store(folder)
    entries = [
        (6, "2024-06-14 19:42:00", "Оставить немного места для нового", "public", "жизнь, заметки", "<p>Сегодня разобрал старые тетради. Между страницами — билет на поезд, список книг и телефон человека, с которым мы давно не разговаривали.</p><p>Забавно, как много памяти помещается в обычной коробке. Решил, что и этим записям пора найти свой дом.</p>"),
        (5, "2020-04-23 10:15:00", "Маленькие ритуалы большого города", "usemask", "город, повседневное", "<p>Открыть окно. Поставить чайник. Услышать, как внизу кто-то выгуливает собаку и здоровается с дворником.</p><p>Кажется, город состоит прежде всего из таких маленьких повторений. Из них же состоит и ощущение дома.</p>"),
        (4, "2017-08-09 22:30:00", "Дорога к морю", "public", "путешествия, лето", "<p>За последним поворотом вдруг появилось море. Именно так — вдруг, хотя мы ехали к нему весь день.</p><blockquote>Иногда нужно просто выбрать дорогу и посмотреть, куда она приведёт.</blockquote><p>На обратном пути купили персики. Они пахли всем этим летом сразу.</p>"),
        (3, "2012-10-12 16:08:00", "Тот самый осенний день", "private", "жизнь, воспоминания", "<p>Помню, как шёл домой через парк. Под ногами шуршали листья, в кармане лежала ещё тёплая булочка, и совершенно не хотелось торопиться.</p><p>Пишу это, чтобы однажды открыть и вспомнить. Не какое-то большое событие, а обычный день, в котором было хорошо.</p>"),
        (2, "2009-02-03 11:20:00", "Книги, к которым возвращаешься", "public", "книги, списки", "<p>Составляю список книг, которые хочется перечитать. Не потому, что забыл сюжет, а потому, что сам с тех пор изменился.</p><ul><li>Туве Янссон — «Летняя книга»</li><li>Рэй Брэдбери — «Вино из одуванчиков»</li><li>Юрий Коваль — «Недопёсок»</li></ul><p>А у вас есть такая книга?</p>"),
        (1, "2008-09-01 09:05:00", "Пусть здесь будет начало", "public", "жизнь", "<p>Привет, журнал. Пока не знаю, что здесь будет, но пусть будет место для мыслей, фотографий и случайных встреч.</p><p>Возможно, через много лет будет интересно перечитать.</p>"),
    ]
    comments = [
        (1, 1, 0, "Аня", "A", "Добро пожаловать! Хорошее начало."),
        (2, 2, 0, "book_wanderer", "A", "«Летнюю книгу» перечитываю каждую весну. Каждый раз замечаю что-то новое."),
        (3, 2, 2, "example_journal", "A", "Да! Удивительно, как маленькая книга может столько вмещать."),
        (4, 3, 0, "old_friend", "S", "Помню этот день. Мы потом ещё сидели на кухне до полуночи. Пусть этот комментарий останется между нами."),
        (5, 3, 4, "example_journal", "A", "И чай всё время остывал. Спасибо, что напомнил."),
        (6, 3, 5, "old_friend", "S", "Зато разговор был тёплый :)"),
        (7, 3, 0, "Аноним", "A", "Очень знакомое чувство. Спасибо за эту запись."),
        (8, 4, 0, "sea_notes", "A", "У меня тоже море всегда появляется вдруг. Даже когда видишь его каждый день."),
        (9, 4, 8, "example_journal", "A", "Хочется научиться замечать так и всё остальное."),
        (10, 5, 0, "city_walker", "S", "А у нас по утрам во дворе играет скрипач. Уже считаю его частью дома."),
        (11, 6, 0, "old_friend", "A", "Как здорово, что эти записи сохранились. Целая жизнь между строк."),
        (12, 6, 11, "example_journal", "A", "И наши разговоры тоже. Пожалуй, это самое ценное."),
    ]
    try:
        def demo_reactions(groups):
            data = {"count": sum(groups), "groups": groups, "captured_at": now(), "source": "demo"}
            names = ["old_friend", "book_wanderer", "sea_notes", "city_walker", "example_journal", "anna_notes", "reader_one", "reader_two"]
            users = []
            for kind, count in enumerate(groups):
                for _ in range(count):
                    uid = len(users) + 1
                    users.append({"id": uid, "username": names[(uid - 1) % len(names)], "type": kind})
            data["authors"] = {**parse_authors(users, data), "source": "demo"}
            return data
        with store.db:
            for eid, date, title, security, tags, body in entries:
                store.save_entry({"itemid": eid, "anum": 1, "eventtime": date, "subject": title,
                                  "event": body, "security": security, "props": {"taglist": tags}})
                store.save_reactions("post", eid, demo_reactions([eid + 2]))
            for cid, eid, parent, user, state, body in comments:
                store.db.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (cid, user))
                store.db.execute("INSERT OR REPLACE INTO comment_meta VALUES (?,?,?)", (cid, cid, state))
                store.save_comment({"id": cid, "jitemid": eid, "parentid": parent, "posterid": cid,
                                    "body": body, "date": entries[6 - eid][1].replace(" ", "T") + "Z"})
                groups = [2, 1] if cid == 4 else [cid % 3]
                store.save_reactions("comment", cid, demo_reactions(groups))
            store.set("username", "example_journal")
        report = {"username": "example_journal", "created_at": now(), "started_at": now(), "status": "demo",
                  "entries_saved": 6, "entries_expected": 6, "comments_saved": 12, "comments_metadata": 12,
                  "screened_comments": 3, "deleted_comments": 0, "missing_comments": [], "pending_days": [],
                  "orphan_entry_ids": [], "issues": [], "images_requested": False,
                  "scope": "Демонстрационный архив. Все записи и комментарии вымышлены.",
                  "limitations": "Вход в ЖЖ и скачивание реальных данных для этого примера не выполнялись."}
        return build_archive(store, report, progress=progress)
    finally:
        store.close()
