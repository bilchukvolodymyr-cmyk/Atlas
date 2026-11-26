"""Folder mode (iCloud Drive): files in, answers out, nothing lost or duplicated."""
import os
import time

from atlas.core import Store
from atlas.folder import FolderInbox
from atlas.understand import ClaudeParser
from test_stage1 import FakeClient, item


def make(tmp_path, *responses):
    client = FakeClient(*responses)
    box = FolderInbox(tmp_path / "Atlas", str(tmp_path / "a.db"), ClaudeParser(client=client),
                      clock=lambda: time.time() + 10)
    return box, client


def drop(box, name, text):
    (box.inbox / name).write_text(text, encoding="utf-8")


def test_file_is_understood_answered_and_moved(tmp_path):
    box, client = make(tmp_path, [item("idea", "QR на локаціях", title="QR на локаціях")])
    drop(box, "iphone-20261126-1.txt", "Ідея: QR на локаціях")
    [r] = box.poll()
    assert r["status"] == "applied"
    assert (box.outbox / "iphone-20261126-1.txt").read_text("utf-8") == "Ідея: «QR на локаціях»\n"
    assert not (box.inbox / "iphone-20261126-1.txt").exists()
    assert (box.done / "iphone-20261126-1.txt").exists()          # kept, not deleted
    assert box.poll() == []                                       # nothing twice
    assert len(client.calls) == 1


def test_same_file_synced_again_is_not_duplicated(tmp_path):
    box, client = make(tmp_path, [item("idea", "QR", title="QR")])
    drop(box, "m1.txt", "Ідея: QR")
    box.poll()
    drop(box, "m1.txt", "Ідея: QR")                               # iCloud re-delivers
    [r] = box.poll()
    assert r["status"] == "already_applied" and len(client.calls) == 1
    s = Store(box.db)
    assert s.conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1
    s.close()


def test_file_still_being_written_is_left_alone(tmp_path):
    box, client = make(tmp_path)
    box.clock = time.time
    drop(box, "fresh.txt", "щойно")
    assert box.poll() == [] and (box.inbox / "fresh.txt").exists()


def test_claude_unavailable_then_retried(tmp_path):
    box, client = make(tmp_path, ConnectionError("offline"),
                       [item("idea", "полиця", title="Полиця")])
    drop(box, "m1.txt", "Ідея: полиця")
    [r] = box.poll()
    assert r["status"] == "retry_later"
    assert "оброблю пізніше" in (box.outbox / "m1.txt").read_text("utf-8")
    assert (box.done / "m1.txt").exists()                         # text is safe in the database
    box.next_try.clear()
    [r] = box.poll()
    assert r["status"] == "applied"
    assert (box.outbox / "m1.txt").read_text("utf-8") == "Ідея: «Полиця»\n"


def test_icloud_placeholders_and_other_files_ignored(tmp_path):
    box, client = make(tmp_path)
    drop(box, ".m2.txt.icloud", "")
    drop(box, "photo.jpg", "x")
    assert box.poll() == []
    assert client.calls == []
