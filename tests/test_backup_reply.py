from atlas.backup import backup
from atlas.core import Store
from atlas.reply import describe


def test_backup_rotates_and_is_readable(tmp_path):
    db = tmp_path / "a.db"
    s = Store(db)
    i, _ = s.ingest("t", "1", "x")
    s.apply(i, [{"type": "create_idea", "title": "QR"}])
    s.close()
    import time
    paths = []
    for _ in range(3):
        paths.append(backup(str(db), str(tmp_path / "bk"), keep=2))
        time.sleep(1.1)
    left = sorted((tmp_path / "bk").glob("atlas-*.db"))
    assert left == sorted(paths[1:])
    copy = Store(left[-1])
    assert copy.conn.execute("SELECT title FROM ideas").fetchone()[0] == "QR"
    copy.close()


def test_reply_lists_what_was_written(tmp_path):
    s = Store(tmp_path / "a.db")
    text = describe({"status": "applied", "created": {}, "rejected": [{}], "actions": [
        {"type": "record_metric", "date": "2026-11-26", "site": "polo2", "name": "workers_total",
         "value": 103.0},
        {"type": "create_idea", "title": "QR"},
        {"type": "create_loop", "title": "Години", "due_at": "2026-11-30T14:00"},
        {"type": "ask_question", "text": "Коли?"},
    ]}, s)
    assert text.splitlines() == ["26.11: 103 людей (Polo 2)", "Ідея: «QR»",
                                 "Справа: «Години» до 30.11 о 14:00", "Питання: Коли?",
                                 "Не записав 1 пункт(и): не знайшов їх у тексті."]
    s.close()
