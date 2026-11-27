"""Google Sheet mirror: rows keyed by id, sent once, re-sent only when changed, never duplicated."""
import time

import pytest

from atlas.core import Store
from atlas.folder import FolderInbox
from atlas.mirror import (DECISIONS, JOURNAL, LOOPS, METRICS, MONTHS, AutoMirror, Mirror, MirrorError,
                          build)
from atlas.understand import ClaudeParser
from test_stage1 import FakeClient, item


class FakeSheet:
    """Does what deploy/sheets/Code.gs does: column A is the key, update or append."""

    def __init__(self, fail=0):
        self.tabs, self.calls, self.fail = {}, 0, fail

    def __call__(self, url, payload):
        self.calls += 1
        if self.fail:
            self.fail -= 1
            raise OSError("no network")
        if payload["token"] != "t":
            return {"ok": False, "error": "bad token"}
        up = ap = 0
        for name, part in payload["sheets"].items():
            tab = self.tabs.setdefault(name, {})
            for row in part["rows"]:
                key = str(row[0])
                if key in tab:
                    up += 1
                else:
                    ap += 1
                tab[key] = row
        return {"ok": True, "updated": up, "appended": ap}


@pytest.fixture
def db(tmp_path):
    s = Store(tmp_path / "a.db")
    i, _ = s.ingest("icloud", "m1", "Завтра 40 людей Agenzia, 12 Cooperativa")
    s.apply(i, [{"type": "record_metric", "date": "2026-07-01", "name": "workers_agenzia", "value": 40},
                {"type": "record_metric", "date": "2026-07-01", "name": "workers_cooperativa", "value": 12},
                {"type": "record_metric", "date": "2026-07-01", "site": "polo1", "name": "workers_total", "value": 30},
                {"type": "create_loop", "kind": "task", "title": "Етикетки 3Q", "state": "active",
                 "assigned_by": "Roberto Poli"},
                {"type": "create_entry", "kind": "decision", "text": "Шини пакуємо окремо"}])
    s.close()
    return tmp_path / "a.db"


def test_rows_from_database(db):
    rows = build(db)
    day = rows[METRICS]["2026-07-01"]
    assert day[1] == "ср"
    assert day[2:8] == [52, 30, "", "", 40, 12]     # total (sum), polo1-3, agenzia, coop
    assert day[8:11] == [416, 8.32, 424.32]         # 52*8, *0.02, sum
    assert "розрахунок" in day[19] and "Agenzia + Cooperativa" in day[19]
    [month] = rows[MONTHS].values()
    assert month[0] == "липень 2026" and month[1:3] == [1, 52] and month[7] == 416
    [loop] = rows[LOOPS].values()
    assert loop[1] == "Етикетки 3Q" and loop[3] == "в роботі" and "Roberto Poli" in loop[7]
    [j] = rows[JOURNAL].values()
    assert j[3].startswith("Завтра 40") and "справа #1 «Етикетки 3Q»" in j[5]
    [d] = rows[DECISIONS].values()
    assert d[2] == "Шини пакуємо окремо"


def test_no_total_means_no_hours_invented(tmp_path):
    s = Store(tmp_path / "a.db")
    i, _ = s.ingest("cli", "x", "8 машин")
    s.apply(i, [{"type": "record_metric", "date": "2026-07-02", "name": "trucks_unload", "value": 8}])
    s.close()
    day = build(tmp_path / "a.db")[METRICS]["2026-07-02"]
    assert day[2] == "" and day[8] == "" and day[11] == 8


def test_sent_once_then_only_changes(db, tmp_path):
    sheet = FakeSheet()
    m = Mirror(db, "u", "t", state=tmp_path / "m.db", post=sheet)
    first = m.sync()
    assert first["appended"] == first["sent"] == 5 and first["updated"] == 0
    assert m.sync()["sent"] == 0 and sheet.calls == 1          # nothing changed -> nothing sent
    s = Store(db)
    i, _ = s.ingest("icloud", "m2", "етикетки зроблено")
    s.apply(i, [{"type": "transition_loop", "id": 1, "to": "done_claimed"}])
    s.close()
    again = m.sync()
    # loop row changed; new journal row appended
    assert again["updated"] == 1 and again["appended"] == 1
    assert sheet.tabs[LOOPS]["1"][3] == "кажуть, що зроблено"
    assert "доказ до справи #1" in sheet.tabs[JOURNAL]["2"][5]
    assert len(sheet.tabs[LOOPS]) == 1                          # no duplicate


def test_lost_state_file_resends_without_duplicates(db, tmp_path):
    sheet = FakeSheet()
    Mirror(db, "u", "t", state=tmp_path / "m.db", post=sheet).sync()
    (tmp_path / "m.db").unlink()
    r = Mirror(db, "u", "t", state=tmp_path / "m.db", post=sheet).sync()
    assert r["updated"] == 5 and r["appended"] == 0
    assert sum(len(t) for t in sheet.tabs.values()) == 5


def test_failure_is_retried_next_time(db, tmp_path):
    sheet = FakeSheet(fail=1)
    m = Mirror(db, "u", "t", state=tmp_path / "m.db", post=sheet)
    with pytest.raises(OSError):
        m.sync()
    assert m.sync()["sent"] == 5


def test_bad_token_is_an_error(db, tmp_path):
    m = Mirror(db, "u", "wrong", state=tmp_path / "m.db", post=FakeSheet())
    with pytest.raises(MirrorError):
        m.sync()


def test_database_is_opened_read_only(db, tmp_path):
    before = db.read_bytes()
    Mirror(db, "u", "t", state=tmp_path / "m.db", post=FakeSheet()).sync()
    assert db.read_bytes() == before


def test_auto_mirror_waits_for_config(db, tmp_path):
    cfg = tmp_path / "sheets.json"
    auto = AutoMirror(db, cfg)
    assert auto.sync()["sent"] == 0
    cfg.write_text('{"url": "u", "token": "t"}')
    auto.mirror = None
    sheet = FakeSheet()
    from atlas import mirror as mod
    orig = mod.http_post
    mod.http_post = sheet
    try:
        auto.mirror = mod.Mirror(db, "u", "t", state=tmp_path / "m.db", post=sheet)
        assert auto.sync()["sent"] == 5
    finally:
        mod.http_post = orig


def test_folder_pass_mirrors_and_survives_mirror_failure(tmp_path):
    client = FakeClient([item("idea", "QR на локаціях", title="QR на локаціях")])
    box = FolderInbox(tmp_path / "Atlas", str(tmp_path / "a.db"), ClaudeParser(client=client),
                      clock=lambda: time.time() + 10, downloader=lambda p: None)
    (box.inbox / "m1.txt").write_text("Ідея: QR на локаціях", encoding="utf-8")
    sheet = FakeSheet(fail=1)
    box.mirror = Mirror(box.db, "u", "t", state=tmp_path / "m.db", post=sheet)
    [r] = box.poll()                                  # mirror fails, message still applied
    assert r["status"] == "applied"
    box.poll()                                        # next pass sends it
    assert "1" in sheet.tabs[JOURNAL]


# ---------------------------------------------------------------- Claude Code + Sheets connector
import json as _json
from types import SimpleNamespace

from atlas.mirror import ClaudeCodeSheets


class FakeClaude:
    """Plays Claude Code calling the Sheets tool: applies each range to an in-memory grid."""

    def __init__(self, lie=False):
        self.grid, self.cmds, self.lie = {}, [], lie

    def __call__(self, cmd, input, **kw):
        self.cmds.append(cmd)
        calls = _json.loads(input[input.index("["):])
        results = []
        for c in calls:
            tab, rng = c["range"].rsplit("!", 1)
            tab = tab.strip("'")
            first = int(rng.split(":")[0][1:])
            for i, row in enumerate(c["values"]):
                self.grid.setdefault(tab, {})[first + i] = row
            cells = sum(len(v) for v in c["values"])
            results.append({"range": c["range"], "updatedCells": 0 if self.lie else cells})
        out = {"is_error": False, "structured_output": {"results": results}}
        return SimpleNamespace(returncode=0, stdout=_json.dumps(out), stderr="")


def test_claude_code_writer_fixed_rows_no_duplicates(db, tmp_path):
    fake = FakeClaude()
    w = ClaudeCodeSheets("SID", runner=fake, executable="claude")
    m = Mirror(db, "", "", state=tmp_path / "m.db", post=w)
    assert m.sync()["sent"] == 5
    assert fake.grid[METRICS][1] == HEADERS_M and fake.grid[METRICS][2][0] == "2026-07-01"
    assert fake.grid[JOURNAL][2][0] == 1
    assert "--allowedTools" in fake.cmds[0] and "SID" not in fake.cmds[0]
    assert m.sync()["sent"] == 0 and len(fake.cmds) == 1
    s = Store(db)
    i, _ = s.ingest("icloud", "m2", "етикетки зроблено")
    s.apply(i, [{"type": "transition_loop", "id": 1, "to": "done_claimed"}])
    s.close()
    m.sync()
    assert sorted(fake.grid[JOURNAL]) == [1, 2, 3]          # header, row 1, new row 2
    assert sorted(fake.grid[LOOPS]) == [1, 2]                # same loop row overwritten
    assert fake.grid[LOOPS][2][3] == "кажуть, що зроблено"


def test_claude_code_writer_unconfirmed_write_is_retried(db, tmp_path):
    fake = FakeClaude(lie=True)
    m = Mirror(db, "", "", state=tmp_path / "m.db", post=ClaudeCodeSheets("SID", runner=fake))
    with pytest.raises(MirrorError):
        m.sync()
    fake.lie = False
    assert m.sync()["sent"] == 5


def test_ranges_are_grouped_and_quoted():
    w = ClaudeCodeSheets("SID", runner=None)
    calls = w.calls({"sheets": {METRICS: {"header": ["a", "b"], "write_header": True,
                                          "rows": [["x", 1], ["y", 2], ["z", 3]], "at": [5, 2, 3]}}})
    assert [c["range"] for c in calls] == ["'Показники по днях'!A1:B1",
                                           "'Показники по днях'!A2:B3", "'Показники по днях'!A5:B5"]


from atlas.mirror import HEADERS
HEADERS_M = HEADERS[METRICS]


def test_days_and_months_stay_in_date_order(db, tmp_path):
    fake = FakeClaude()
    m = Mirror(db, "", "", state=tmp_path / "m.db", post=ClaudeCodeSheets("SID", runner=fake))
    m.sync()
    s = Store(db)                                  # an earlier day and month loaded later
    i, _ = s.ingest("cli", "old", "5 людей")
    s.apply(i, [{"type": "record_metric", "date": "2026-06-30", "name": "workers_total", "value": 5}])
    s.close()
    m.sync()
    assert [fake.grid[METRICS][n][0] for n in (2, 3)] == ["2026-06-30", "2026-07-01"]
    assert [fake.grid[MONTHS][n][0] for n in (2, 3)] == ["червень 2026", "липень 2026"]
    before = len(fake.cmds)
    assert m.sync()["sent"] == 0 and len(fake.cmds) == before     # stable afterwards
