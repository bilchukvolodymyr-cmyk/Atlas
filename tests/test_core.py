import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from atlas import ConflictError, Store, TransitionError, ValidationError
from atlas.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "atlas.db", clock=lambda: "2026-11-26T18:00:00+00:00")
    yield s
    s.close()


def new_loop(store, ext="m1", **extra):
    inbox_id, _ = store.ingest("shortcut", ext, "треба зробити години до кінця місяця")
    action = {"type": "create_loop", "kind": "task", "title": "Години за листопад", "ref": "L", **extra}
    res = store.apply(inbox_id, [action])
    return res["created"]["L"]["id"]


def move(store, ext, loop_id, to, **kw):
    inbox_id, _ = store.ingest("shortcut", ext, kw.pop("text", f"move {to}"))
    return store.apply(inbox_id, [{"type": "transition_loop", "id": loop_id, "to": to, **kw}])


# ---------------------------------------------------------------- idempotency
def test_same_external_id_is_stored_once(store):
    a = store.ingest("shortcut", "uuid-1", "текст")
    b = store.ingest("shortcut", "uuid-1", "текст")
    assert a == (1, True)
    assert b == (1, False)
    assert store.conn.execute("SELECT count(*) FROM inbox").fetchone()[0] == 1


def test_same_id_from_different_sources_are_different_messages(store):
    assert store.ingest("shortcut", "x", "a")[0] != store.ingest("web", "x", "a")[0]


def test_redelivery_ten_times_creates_one_loop(store):
    for _ in range(10):
        inbox_id, _ = store.ingest("shortcut", "uuid-42", "підготувати місце")
        store.apply(inbox_id, [{"type": "create_loop", "kind": "task", "title": "Підготувати місце"}])
    assert store.conn.execute("SELECT count(*) FROM loops").fetchone()[0] == 1
    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_apply_twice_returns_already_applied(store):
    inbox_id, _ = store.ingest("cli", "a", "нотатка")
    first = store.apply(inbox_id, [{"type": "create_entry", "kind": "note", "text": "нотатка"}])
    second = store.apply(inbox_id, [{"type": "create_entry", "kind": "note", "text": "нотатка"}])
    assert first["status"] == "applied"
    assert second == {"status": "already_applied"}
    assert store.conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 1


def test_empty_message_rejected(store):
    with pytest.raises(ValidationError):
        store.ingest("cli", "e", "   ")


# ---------------------------------------------------------------- atomicity
def test_failed_action_writes_nothing_and_marks_inbox_failed(store):
    inbox_id, _ = store.ingest("shortcut", "bad", "дві справи")
    with pytest.raises(ValidationError):
        store.apply(inbox_id, [
            {"type": "create_loop", "kind": "task", "title": "Перша"},
            {"type": "create_loop", "kind": "task"},  # no title -> invalid
        ])
    assert store.conn.execute("SELECT count(*) FROM loops").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    row = store.conn.execute("SELECT status, attempts, error FROM inbox WHERE id=?", (inbox_id,)).fetchone()
    assert row["status"] == "failed" and row["attempts"] == 1 and "title" in row["error"]
    # the message itself is kept and can be retried
    assert store.pending_inbox()[0]["id"] == inbox_id
    store.apply(inbox_id, [{"type": "create_loop", "kind": "task", "title": "Перша"}])
    assert store.pending_inbox() == []


def test_unknown_action_type_rejected(store):
    inbox_id, _ = store.ingest("cli", "u", "x")
    with pytest.raises(ValidationError):
        store.apply(inbox_id, [{"type": "delete_everything"}])


def test_unknown_field_rejected(store):
    inbox_id, _ = store.ingest("cli", "f", "x")
    with pytest.raises(ValidationError):
        store.apply(inbox_id, [{"type": "create_loop", "kind": "task", "title": "t", "invented": 1}])


# ---------------------------------------------------------------- state machine
def test_full_verified_close_path(store):
    lid = new_loop(store)
    move(store, "m2", lid, "done_claimed", text="зробив години")
    move(store, "m3", lid, "verifying")
    inbox_id, _ = store.ingest("shortcut", "m4", "бухгалтерія підтвердила")
    store.apply(inbox_id, [{"type": "verify_loop", "id": lid, "evidence": "бухгалтерія підтвердила"}])
    assert store.loop(lid)["state"] == "closed"
    types = [e["type"] for e in store.history("loop", lid)]
    assert types == ["created", "evidence_added", "state_changed", "state_changed",
                     "verified", "state_changed"]


def test_done_claimed_stores_user_message_as_evidence(store):
    lid = new_loop(store)
    move(store, "m2", lid, "done_claimed", text="все, години відправив")
    ev = [e for e in store.history("loop", lid) if e["type"] == "evidence_added"][0]
    assert ev["payload"]["evidence"] == "все, години відправив"


@pytest.mark.parametrize("to", ["closed"])
def test_cannot_close_directly(store, to):
    lid = new_loop(store)
    with pytest.raises(TransitionError):
        move(store, "m2", lid, to)
    assert store.loop(lid)["state"] == "active"


def test_cannot_skip_verification(store):
    lid = new_loop(store)
    move(store, "m2", lid, "done_claimed")
    inbox_id, _ = store.ingest("shortcut", "m3", "закривай")
    with pytest.raises(TransitionError):
        store.apply(inbox_id, [{"type": "verify_loop", "id": lid, "evidence": "x"}])
    assert store.loop(lid)["state"] == "done_claimed"


def test_forbidden_transition(store):
    lid = new_loop(store)
    with pytest.raises(TransitionError):
        move(store, "m2", lid, "verifying")  # active -> verifying not allowed


def test_new_loop_cannot_start_closed(store):
    inbox_id, _ = store.ingest("cli", "c", "x")
    with pytest.raises(TransitionError):
        store.apply(inbox_id, [{"type": "create_loop", "kind": "task", "title": "t", "state": "closed"}])


def test_reopen_closed_loop(store):
    lid = new_loop(store)
    move(store, "m2", lid, "done_claimed")
    move(store, "m3", lid, "verifying")
    inbox_id, _ = store.ingest("cli", "m4", "ok")
    store.apply(inbox_id, [{"type": "verify_loop", "id": lid, "evidence": "ok"}])
    move(store, "m5", lid, "active", reason="виявилось не все")
    assert store.loop(lid)["state"] == "active"


def test_waiting_for_someone(store):
    lid = new_loop(store, waiting_on="Roberto Poli", state="waiting")
    assert store.loop(lid)["state"] == "waiting"
    move(store, "m2", lid, "active", text="Роберто відповів")
    assert store.loop(lid)["state"] == "active"


# ---------------------------------------------------------------- optimistic lock
def test_stale_version_conflict(store):
    lid = new_loop(store)
    inbox_id, _ = store.ingest("cli", "u1", "x")
    store.apply(inbox_id, [{"type": "update_loop", "id": lid, "fields": {"next_action": "a"}}])
    inbox_id, _ = store.ingest("cli", "u2", "y")
    with pytest.raises(ConflictError):
        store.apply(inbox_id, [{"type": "update_loop", "id": lid, "expected_version": 1,
                                "fields": {"next_action": "b"}}])
    assert store.loop(lid)["next_action"] == "a"


# ---------------------------------------------------------------- refs, entries, ideas, metrics
def test_one_message_many_objects_with_refs(store):
    text = ("Сьогодні 10 машин на розвантаження, 18 на завантаження, працювали 103 людини. "
            "Роберто Полі сказав підготувати і звільнити місце на суботу.")
    inbox_id, _ = store.ingest("shortcut", "day-26", text)
    res = store.apply(inbox_id, [
        {"type": "create_entry", "kind": "event", "text": text, "happened_at": "2026-11-26"},
        {"type": "record_metric", "date": "2026-11-26", "name": "trucks_unload", "value": 10},
        {"type": "record_metric", "date": "2026-11-26", "name": "trucks_load", "value": 18},
        {"type": "record_metric", "date": "2026-11-26", "name": "workers_total", "value": 103},
        {"type": "create_loop", "ref": "place", "kind": "task", "title": "Підготувати і звільнити місце",
         "assigned_by": "Roberto Poli", "due_at": "2026-11-28", "state": "needs_info"},
        {"type": "ask_question", "object_type": "loop", "object_id": "$place",
         "text": "Яке місце підготувати і для чого?", "missing_field": "done_criteria"},
    ])
    assert res["status"] == "applied" and res["events"] == 6
    loop = store.loop(res["created"]["place"]["id"])
    assert loop["assigned_by"] == "Roberto Poli" and loop["state"] == "needs_info"
    q = store.conn.execute("SELECT * FROM questions").fetchone()
    assert q["object_id"] == loop["id"] and q["status"] == "open"


def test_bad_ref_rejected(store):
    inbox_id, _ = store.ingest("cli", "r", "x")
    with pytest.raises(ValidationError):
        store.apply(inbox_id, [{"type": "ask_question", "object_type": "loop", "object_id": "$nope",
                                "text": "?"}])


def test_metric_same_day_updates_not_duplicates(store):
    for ext, value in (("a", 10), ("b", 12)):
        inbox_id, _ = store.ingest("cli", ext, f"trucks {value}")
        store.apply(inbox_id, [{"type": "record_metric", "date": "2026-11-26", "name": "trucks_unload",
                                "value": value}])
    rows = store.conn.execute("SELECT * FROM metrics").fetchall()
    assert len(rows) == 1 and rows[0]["value"] == 12
    hist = store.history("metric", rows[0]["id"])
    assert [h["type"] for h in hist] == ["created", "updated"]
    assert hist[1]["payload"]["before"]["value"] == 10


@pytest.mark.parametrize("value", ["десять", True, None])
def test_metric_value_must_be_number(store, value):
    inbox_id, _ = store.ingest("cli", f"v{value}", "x")
    with pytest.raises(ValidationError):
        store.apply(inbox_id, [{"type": "record_metric", "date": "2026-11-26", "name": "n", "value": value}])


def test_metric_bad_date(store):
    inbox_id, _ = store.ingest("cli", "d", "x")
    with pytest.raises(ValidationError):
        store.apply(inbox_id, [{"type": "record_metric", "date": "26.11.2026", "name": "n", "value": 1}])


def test_idea_update_touches(store):
    inbox_id, _ = store.ingest("cli", "i1", "ідея: генератор етикеток")
    res = store.apply(inbox_id, [{"type": "create_idea", "ref": "I", "title": "Генератор етикеток"}])
    iid = res["created"]["I"]["id"]
    later = Store(store.path, clock=lambda: "2026-12-30T08:00:00+00:00")
    inbox_id, _ = later.ingest("cli", "i2", "беремо в роботу")
    later.apply(inbox_id, [{"type": "update_idea", "id": iid, "fields": {"status": "in_work"}}])
    row = later.conn.execute("SELECT * FROM ideas WHERE id=?", (iid,)).fetchone()
    assert row["status"] == "in_work" and row["last_touched_at"].startswith("2026-12-30")
    later.close()


# ---------------------------------------------------------------- journal
def test_events_are_append_only(store):
    new_loop(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE events SET type='x'")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("DELETE FROM events")


def _busy_db(store):
    lid = new_loop(store)
    move(store, "m2", lid, "done_claimed")
    move(store, "m3", lid, "verifying")
    inbox_id, _ = store.ingest("cli", "m4", "ok")
    store.apply(inbox_id, [{"type": "verify_loop", "id": lid, "evidence": "ok"}])
    test_one_message_many_objects_with_refs(store)
    for ext, v in (("x1", 1), ("x2", 2)):
        inbox_id, _ = store.ingest("cli", ext, "m")
        store.apply(inbox_id, [{"type": "record_metric", "date": "2026-11-27", "name": "n", "value": v}])
    inbox_id, _ = store.ingest("cli", "q", "відповідь")
    store.apply(inbox_id, [{"type": "answer_question", "id": 1}])


def test_rebuild_from_journal_gives_identical_state(store):
    _busy_db(store)
    before = store.snapshot()
    assert sum(len(v) for v in before.values()) > 5
    store.rebuild()
    assert store.snapshot() == before


def test_rebuild_after_state_table_loss(store):
    _busy_db(store)
    before = store.snapshot()
    store.conn.execute("DELETE FROM loops")
    store.conn.execute("DELETE FROM metrics")
    store.rebuild()
    assert store.snapshot() == before


def test_data_survives_restart(tmp_path):
    path = tmp_path / "a.db"
    s = Store(path)
    inbox_id, _ = s.ingest("cli", "p", "x")
    s.apply(inbox_id, [{"type": "create_loop", "kind": "task", "title": "Пам'ятати"}])
    s.close()
    s2 = Store(path)
    assert s2.open_loops()[0]["title"] == "Пам'ятати"
    s2.close()


def test_backup_is_consistent(store, tmp_path):
    _busy_db(store)
    dest = tmp_path / "backup.db"
    store.backup(dest)
    b = Store(dest)
    assert b.snapshot() == store.snapshot() and b.integrity_ok()
    b.close()


# ---------------------------------------------------------------- crash (kill -9 inside transaction)
CHILD = textwrap.dedent("""
    import sys
    from atlas import Store
    s = Store(sys.argv[1])
    inbox_id, _ = s.ingest("shortcut", "crash-1", "справа перед аварією")
    print("READY", flush=True)
    s.apply(inbox_id, [{"type": "create_loop", "kind": "task", "title": "Справа"},
                       {"type": "record_metric", "date": "2026-11-26", "name": "n", "value": 1}])
    print("COMMITTED", flush=True)
""")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="SIGKILL")
def test_kill_minus_9_inside_transaction(tmp_path):
    db = tmp_path / "crash.db"
    env = {**os.environ, "ATLAS_TEST_SLEEP_IN_TX": "5", "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(db)], env=env,
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "READY"
    time.sleep(1.0)  # child is now sleeping inside BEGIN IMMEDIATE ... COMMIT
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    assert "COMMITTED" not in proc.stdout.read()

    s = Store(db)
    assert s.integrity_ok()
    # message survived (it was stored before processing), nothing half-written
    inbox = s.conn.execute("SELECT * FROM inbox").fetchone()
    assert inbox["raw_text"] == "справа перед аварією" and inbox["status"] == "new"
    for table in ("loops", "metrics", "events"):
        assert s.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    # reprocessing after restart works and is complete
    s.apply(inbox["id"], [{"type": "create_loop", "kind": "task", "title": "Справа"},
                          {"type": "record_metric", "date": "2026-11-26", "name": "n", "value": 1}])
    assert s.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    s.close()


# ---------------------------------------------------------------- CLI
def run_cli(db, *argv, capsys):
    code = cli_main(["--db", str(db), *argv])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out else None), out.err


def test_cli_flow(tmp_path, capsys):
    db = tmp_path / "cli.db"
    code, out, _ = run_cli(db, "loop", "new", "Години за листопад", "--due", "2026-11-30",
                           "--assigned-by", "Roberto Poli", "--id", "k1", capsys=capsys)
    assert code == 0 and out["status"] == "applied"
    code, out, _ = run_cli(db, "loop", "new", "Години за листопад", "--id", "k1", capsys=capsys)
    assert out["status"] == "already_applied"          # retry with same --id
    code, out, _ = run_cli(db, "list", capsys=capsys)
    assert len(out) == 1 and out[0]["assigned_by"] == "Roberto Poli"
    code, out, err = run_cli(db, "loop", "move", "1", "closed", capsys=capsys)
    assert code == 2 and "TransitionError" in err
    run_cli(db, "loop", "move", "1", "done_claimed", "--evidence", "відправив", capsys=capsys)
    run_cli(db, "loop", "move", "1", "verifying", capsys=capsys)
    run_cli(db, "loop", "verify", "1", "--evidence", "підтвердили", capsys=capsys)
    code, out, _ = run_cli(db, "list", capsys=capsys)
    assert out == []
    code, out, _ = run_cli(db, "add", "Сьогодні 103 людини", "--kind", "event", capsys=capsys)
    assert out["status"] == "applied"
    code, out, _ = run_cli(db, "metric", "2026-11-26", "workers_total", "103", capsys=capsys)
    assert out["status"] == "applied"
    code, out, _ = run_cli(db, "rebuild", capsys=capsys)
    assert out["identical"] is True and out["integrity_ok"] is True
