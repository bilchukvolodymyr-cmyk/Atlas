import json
from pathlib import Path

import pytest

from atlas import productivity as P


def report(day, ops, activity=True, backlog=None):
    rows = "".join(f"<tr><td>{o}</td><td>{p}</td><td>{h}</td><td>{'—' if not h else round(p / h, 1)}</td>"
                   f"<td>5</td><td>CON-NU</td></tr>" for o, p, h in ops)
    types = "".join(f"<tr><td>{o}</td><td>{p}</td><td>—</td><td>{p}</td></tr>" for o, p, _h in ops)
    act = ("<h2>Pezzi per tipo di attività, per operatore</h2><table><thead><tr><th>Operatore</th>"
           "<th>Prelievo</th><th>Stoccaggio</th><th>Correzione stock</th><th>Totale</th></tr></thead>"
           + "".join(f"<tr><td>{o}</td><td>{p}</td><td>—</td><td>—</td><td>{p}</td></tr>" for o, p, _h in ops)
           + "</table>") if activity else ""
    bl = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in (backlog or {}).items())
    return (f"<html><body><h1>Produttività Operatori — Bergamo-Seriate</h1><p class=\"periodo\">{day} — {day}</p>"
            "<h2>Operatori</h2><table><thead><tr><th>Operatore</th><th>Pezzi</th><th>Ore</th><th>Pezzi/ora</th>"
            f"<th>Missioni</th><th>Tipo articolo principale</th></tr></thead><tbody>{rows}</tbody></table>"
            "<h2>Per tipo articolo</h2><table><thead><tr><th>Operatore</th><th>CON-NU</th><th>MOT-NU</th>"
            f"<th>Totale</th></tr></thead>{types}</table>{act}"
            "<h2>Stoccaggio — Backlog attuale (non legato al periodo selezionato)</h2><table>"
            f"<thead><tr><th>Tipo articolo</th><th>Pezzi da fare</th></tr></thead>{bl}"
            "<tr><td>Totale</td><td>1</td></tr></table></body></html>")


def test_parse_fields():
    r = P.parse_report(report("2026-11-25", [("a.b", 100.0, 4.0), ("c.d", 300.0, 20.5)],
                              backlog={"CON-NU": 10.0, "TRU-NU": 5.0}))
    assert r["day"] == "2026-11-25"
    assert [o["operator"] for o in r["operators"]] == ["c.d", "a.b"]
    assert r["operators"][0]["by_type"] == {"CON-NU": 300.0}
    assert r["operators"][0]["activity"]["Prelievo"] == 300.0
    assert r["backlog"] == {"CON-NU": 10.0, "TRU-NU": 5.0}
    rows = P.data_rows(r, "1a2b3c4d5e6f7a8b")
    assert rows[0][:7] == ["2026-11-25", "c.d", 300, 20.5, 14.6, 5, 300]
    assert "більше 12" in rows[0][10]
    assert len(rows[0]) == len(P.DATA_HEADER)
    d = P.day_row(r, "1a2b3c4d5e6f7a8b", 2)
    assert d[2:5] == [2, 400, 24.5] and d[10] == 15 and len(d) == len(P.DAYS_HEADER)


def test_rejects_other_html_and_ranges():
    with pytest.raises(P.ReportError):
        P.parse_report("<html><h1>Altro</h1></html>")
    with pytest.raises(P.ReportError):
        P.parse_report(report("2026-11-25", []).replace("2026-11-25 — 2026-11-25", "2026-11-01 — 2026-11-25"))


def write(tmp, name, html):
    (tmp / f"{name}.html").write_text(html, encoding="utf-8")


def test_plan_append_then_replace_latest(tmp_path):
    write(tmp_path, "1a0000000000000a", report("2026-11-24", [("a", 1.0, 1.0), ("b", 2.0, 1.0)]))
    write(tmp_path, "1a0000000000000b", report("2026-11-25", [("a", 5.0, 1.0)]))
    p1 = P.plan(P.load_reports(tmp_path), None)
    assert p1["changed"] == ["2026-11-24", "2026-11-25"]
    assert p1["writes"][0]["range"] == "'Продуктивність (дані)'!A2:K4"
    assert p1["state"]["data_next"] == 5 and p1["state"]["days_next"] == 4
    # same files again: nothing to do (no duplicates)
    st = json.loads(p1["writes"][-1]["values"][0][0])
    assert P.plan(P.load_reports(tmp_path), st)["writes"] == []
    # a corrected report for 24.11 with one row less: rewrite 24 and 25, clear the leftover row
    write(tmp_path, "1a0000000000000c", report("2026-11-24", [("a", 9.0, 1.0)]))
    p2 = P.plan(P.load_reports(tmp_path), st)
    assert p2["rewritten"] == ["2026-11-24", "2026-11-25"]
    assert p2["writes"][0]["range"] == "'Продуктивність (дані)'!A2:K3"
    assert p2["clears"] == ["'Продуктивність (дані)'!A4:K4"]
    # next day appended after the state
    st2 = p2["state"]
    for f in tmp_path.glob("*.html"):
        f.unlink()
    write(tmp_path, "1a0000000000000d", report("2026-11-26", [("z", 1.0, 1.0)]))
    p3 = P.plan(P.load_reports(tmp_path), st2)
    assert p3["writes"][0]["range"] == "'Продуктивність (дані)'!A4:K4"
    assert p3["writes"][1]["range"] == "'Продуктивність — дні'!A4:O4"


def test_plan_needs_missing_later_day(tmp_path):
    write(tmp_path, "1a0000000000000a", report("2026-11-24", [("a", 1.0, 1.0)]))
    write(tmp_path, "1a0000000000000b", report("2026-11-25", [("a", 5.0, 1.0)]))
    st = P.plan(P.load_reports(tmp_path), None)["state"]
    (tmp_path / "1a0000000000000b.html").unlink()
    write(tmp_path, "1a0000000000000c", report("2026-11-24", [("a", 2.0, 1.0)]))
    with pytest.raises(P.ReportError, match="2026-11-25"):
        P.plan(P.load_reports(tmp_path), st)


def test_check_updated_cells():
    p = {"writes": [{"range": "'X'!A2:B2", "values": [[1, ""]]}]}
    assert P.check(p, [{"range": "X!A2:B2", "updatedCells": 1}]) == []
    assert P.check(p, [{"range": "X!A2:B2", "updatedCells": 0}])


def test_plan_skips_days_older_than_state_window(tmp_path):
    days = [f"2026-11-{d:02d}" for d in range(1, P.TAIL + 3)]
    for i, d in enumerate(days):
        write(tmp_path, f"1a{i:014x}", report(d, [("a", 1.0, 1.0)]))
    st = P.plan(P.load_reports(tmp_path), None)["state"]
    assert len(st["days"]) == P.TAIL
    # the whole history again (old days no longer tracked in the state): nothing written twice
    again = P.plan(P.load_reports(tmp_path), st)
    assert again["writes"] == [] and again["skipped"] == days[:2]
