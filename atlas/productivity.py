"""Worker productivity from the daily LEA report "Produttività Operatori — Bergamo-Seriate"
(e-mail from Yurii David every morning, HTML attachment "Produttivita_Operatori_61_<date>.html").

The attachment holds, for ONE day:
  * "Operatori": login, Pezzi, Ore, Pezzi/ora, Missioni, Tipo articolo principale
  * "Per tipo articolo": pieces per article type (CON-NU, MOT-NU, ...) per login
  * "Pezzi per tipo di attività, per operatore": Prelievo / Stoccaggio / Correzione stock
    (missing in the oldest reports)
  * "Stoccaggio — Backlog attuale": pieces still to put away per type (a snapshot)

Everything is read by code; nothing is guessed. Numbers stay as LEA gives them ("Ore" are the
hours LEA counts for the login, not a confirmed timesheet).

Google Sheet "Atlas — пам'ять" (written by the daily scheduled task, see docs/productivity.md):
  * DATA  "Продуктивність (дані)"  one row per day+login, days in date order (11 columns;
          the per-article-type split is not stored: it would triple the size of every write)
  * DAYS  "Продуктивність — дні"   one row per day (totals, backlog) + cell P1 = state (JSON)
  * VIEW  "Продуктивність працівників"  formulas only (pick a day / a worker)
A later report for the same day (higher Gmail message id) replaces that day: the plan rewrites
from the first changed day to the end, so rows never duplicate and never go missing.

CLI:
  python3 -m atlas.productivity plan --raw DIR --state STATE.json --out PLAN.json
      DIR: files <gmail message id>.html; STATE.json: text of DAYS!P1 ("" when empty)
  python3 -m atlas.productivity check --plan PLAN.json --result RESULT.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

DATA = "Продуктивність (дані)"
DAYS = "Продуктивність — дні"
VIEW = "Продуктивність працівників"
STATE_CELL = "P1"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
TAIL = 21          # days kept in the state cell
CHUNK = 40         # rows per write
MAX_HOURS = 12     # more LEA hours than this in one day is flagged

DATA_HEADER = ["Дата", "Працівник (логін LEA)", "Шт. всього", "Години (за LEA)",
               "Шт. за годину", "Місії", "Підготовка (prelievo), шт.", "Розміщення (stoccaggio), шт.",
               "Коригування залишків, шт.", "Основний тип товару", "Примітка"]
DAYS_HEADER = ["Дата", "День", "Працівників у звіті", "Шт. всього", "Годин усього (за LEA)",
               "Шт. за годину (середньо)", "Місій", "Підготовка (prelievo), шт.",
               "Розміщення (stoccaggio), шт.", "Коригування залишків, шт.",
               "Залишок на розміщення (на момент звіту), шт.", "Залишок за типами",
               "Примітка", "Лист Gmail", "Рядок початку в даних"]


class ReportError(Exception):
    pass


# ---------------------------------------------------------------- parsing
class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[tuple[str, Any]] = []
        self._tag: str | None = None
        self._txt = ""
        self._table: list[list[str]] | None = None
        self._cell: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2") or (tag == "p" and ("class", "periodo") in attrs):
            self._tag, self._txt = tag, ""
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._table.append([])
        elif tag in ("td", "th") and self._table is not None:
            self._cell = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._table:
            self._table[-1].append(" ".join(self._cell.split()))
            self._cell = None
        elif tag == "table" and self._table is not None:
            self.items.append(("table", self._table))
            self._table = None
        elif tag == self._tag:
            self.items.append((self._tag, " ".join(self._txt.split())))
            self._tag = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell += data
        elif self._tag:
            self._txt += data


def _num(s: str) -> float | None:
    s = (s or "").strip()
    if s in ("", "—", "-"):
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _clean(v: float | None) -> Any:
    if v is None:
        return ""
    return int(v) if float(v).is_integer() else round(v, 2)


def parse_report(html: str) -> dict[str, Any]:
    """{"day": "2026-11-25", "operators": [...], "backlog": {type: pieces}, "has_activity": bool}"""
    p = _Tables()
    p.feed(html)
    title = next((t for k, t in p.items if k == "h1"), "")
    if "Produttivit" not in title:
        raise ReportError("not a 'Produttività Operatori' report")
    period = next((t for k, t in p.items if k == "p"), "")
    days = re.findall(r"\d{4}-\d{2}-\d{2}", period)
    if not days or days[0] != days[-1]:
        raise ReportError(f"not a one-day report: {period!r}")
    day = days[0]
    date.fromisoformat(day)
    sections: dict[str, list[list[str]]] = {}
    heading = ""
    for kind, val in p.items:
        if kind == "h2":
            heading = val
        elif kind == "table":
            sections[heading] = val
    ops: dict[str, dict[str, Any]] = {}
    main = sections.get("Operatori") or []
    if main:
        head = main[0]
        need = ["Operatore", "Pezzi", "Ore", "Pezzi/ora", "Missioni", "Tipo articolo principale"]
        if head[:6] != need:
            raise ReportError(f"unexpected columns in Operatori: {head}")
        for r in main[1:]:
            if len(r) < 6 or not r[0] or r[0].lower().startswith("totale"):
                continue
            ops[r[0]] = {"operator": r[0], "pieces": _num(r[1]), "hours": _num(r[2]),
                         "per_hour": _num(r[3]), "missions": _num(r[4]), "main_type": r[5],
                         "by_type": {}, "activity": {}}
    by_type = sections.get("Per tipo articolo") or []
    if by_type:
        head = by_type[0]
        for r in by_type[1:]:
            if r and r[0] in ops:
                for h, v in zip(head[1:], r[1:]):
                    n = _num(v)
                    if h != "Totale" and n:
                        ops[r[0]]["by_type"][h] = n
    act_key = next((k for k in sections if k.startswith("Pezzi per tipo di attivit")), None)
    if act_key:
        head = sections[act_key][0]
        for r in sections[act_key][1:]:
            if r and r[0] in ops:
                for h, v in zip(head[1:], r[1:]):
                    if h != "Totale":
                        ops[r[0]]["activity"][h] = _num(v) or 0
    backlog: dict[str, float] = {}
    bl_key = next((k for k in sections if k.startswith("Stoccaggio")), None)
    if bl_key:
        for r in sections[bl_key][1:]:
            if len(r) >= 2 and r[0] != "Totale" and _num(r[1]) is not None:
                backlog[r[0]] = _num(r[1])
    known = {"Prelievo", "Stoccaggio", "Correzione stock"}
    for o in ops.values():
        extra = set(o["activity"]) - known
        if extra:
            raise ReportError(f"unknown activity columns {sorted(extra)}")
    return {"day": day, "operators": sorted(ops.values(), key=lambda o: -(o["pieces"] or 0)),
            "backlog": backlog, "has_activity": act_key is not None}


# ---------------------------------------------------------------- rows
def _types(d: dict[str, float]) -> str:
    return "; ".join(f"{k} {_clean(v)}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))


def data_rows(rep: dict[str, Any], msg: str) -> list[list[Any]]:
    day = rep["day"]
    out = []
    for o in rep["operators"]:
        a = o["activity"]
        note = []
        if o["hours"] is not None and o["hours"] > MAX_HOURS:
            note.append(f"у звіті більше {MAX_HOURS} год за день — перевірити")
        if not o["hours"]:
            note.append("годин у звіті 0")
        if not rep["has_activity"]:
            note.append("розбивки за діяльністю у цьому звіті немає")
        out.append([day, o["operator"], _clean(o["pieces"]), _clean(o["hours"]),
                    _clean(o["per_hour"]), _clean(o["missions"]),
                    _clean(a.get("Prelievo")) if rep["has_activity"] else "",
                    _clean(a.get("Stoccaggio")) if rep["has_activity"] else "",
                    _clean(a.get("Correzione stock")) if rep["has_activity"] else "",
                    o["main_type"], "; ".join(note)])
    return out


def day_row(rep: dict[str, Any], msg: str, first: int) -> list[Any]:
    ops = rep["operators"]
    s = lambda k: sum(o[k] or 0 for o in ops)
    act = lambda k: sum(o["activity"].get(k) or 0 for o in ops) if rep["has_activity"] else None
    pieces, hours = s("pieces"), s("hours")
    note = [] if ops else ["у звіті немає жодного працівника (вихідний?)"]
    if ops and not rep["has_activity"]:
        note.append("розбивки за діяльністю у цьому звіті немає")
    bl = sum(rep["backlog"].values()) if rep["backlog"] else None
    return [rep["day"], WEEKDAYS[date.fromisoformat(rep["day"]).weekday()], len(ops),
            _clean(pieces), _clean(round(hours, 2)),
            _clean(round(pieces / hours, 1)) if hours else "", _clean(s("missions")),
            _clean(act("Prelievo")), _clean(act("Stoccaggio")), _clean(act("Correzione stock")),
            _clean(bl), _types(rep["backlog"]), "; ".join(note), msg, first if ops else ""]


# ---------------------------------------------------------------- plan
def load_reports(raw: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    """{day: (message id, report)}; of several reports for a day the latest (highest Gmail id)."""
    best: dict[str, tuple[str, dict[str, Any]]] = {}
    for f in sorted(raw.glob("*.html")):
        msg = f.stem
        if not re.fullmatch(r"[0-9a-f]{12,20}", msg):
            raise ReportError(f"file name must be the Gmail message id: {f.name}")
        try:
            rep = parse_report(f.read_text(encoding="utf-8", errors="replace"))
        except ReportError as exc:
            print(f"skip {f.name}: {exc}", file=sys.stderr)
            continue
        cur = best.get(rep["day"])
        if cur is None or int(msg, 16) > int(cur[0], 16):
            best[rep["day"]] = (msg, rep)
    return best


def _q(sheet: str) -> str:
    return "'" + sheet.replace("'", "''") + "'"


def _col(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def plan(reports: dict[str, tuple[str, dict[str, Any]]], state: dict[str, Any] | None
         ) -> dict[str, Any]:
    """Writes that bring the sheet up to date. state = DAYS!P1 of the last run:
    {"days": [[day, msg, first_data_row, rows, days_row], ...] (last TAIL days),
     "data_next": first free data row, "days_next": first free DAYS row}"""
    state = state or {"days": [], "data_next": 2, "days_next": 2}
    known = {d[0]: d for d in state["days"]}
    # Days older than the state window were loaded before; their rows are not tracked any more,
    # so a report for such a day is skipped (reported in "skipped"), never appended twice.
    oldest_known = min(known) if known and len(state["days"]) >= TAIL else None
    skipped = sorted(d for d in reports if oldest_known and d < oldest_known)
    changed = sorted(d for d, (m, _r) in reports.items()
                     if d not in skipped
                     and (d not in known or int(m, 16) > int(known[d][1], 16)))
    if not changed:
        return {"changed": [], "skipped": skipped, "writes": [], "clears": [], "state": state}
    first = changed[0]
    keep = [d for d in state["days"] if d[0] < first]
    redo = sorted(set(changed) | {d for d in known if d >= first})
    missing = [d for d in redo if d not in reports]
    if missing:
        raise ReportError("need the reports of these days too: "
                          + ", ".join(f"{d} ({known[d][1]})" for d in missing))
    data_row = keep[-1][2] + keep[-1][3] if keep else 2
    days_row = keep[-1][4] + 1 if keep else 2
    data_start, days_start = data_row, days_row
    data_vals: list[list[Any]] = []
    days_vals: list[list[Any]] = []
    new_state = [list(d) for d in keep]
    for d in redo:
        msg, rep = reports[d]
        rows = data_rows(rep, msg)
        days_vals.append(day_row(rep, msg, data_row))
        new_state.append([d, msg, data_row, len(rows), days_row])
        data_vals += rows
        data_row += len(rows)
        days_row += 1
    writes = []
    for i in range(0, len(data_vals), CHUNK):
        part = data_vals[i:i + CHUNK]
        a = data_start + i
        writes.append({"range": f"{_q(DATA)}!A{a}:{_col(len(DATA_HEADER))}{a + len(part) - 1}",
                       "values": part})
    writes.append({"range": f"{_q(DAYS)}!A{days_start}:{_col(len(DAYS_HEADER))}{days_row - 1}",
                   "values": days_vals})
    clears = []
    if state["data_next"] > data_row:
        clears.append(f"{_q(DATA)}!A{data_row}:{_col(len(DATA_HEADER))}{state['data_next'] - 1}")
    if state["days_next"] > days_row:
        clears.append(f"{_q(DAYS)}!A{days_row}:{_col(len(DAYS_HEADER))}{state['days_next'] - 1}")
    st = {"days": new_state[-TAIL:], "data_next": data_row, "days_next": days_row}
    writes.append({"range": f"{_q(DAYS)}!{STATE_CELL}",
                   "values": [[json.dumps(st, separators=(",", ":"))]]})
    return {"changed": changed, "skipped": skipped, "rewritten": redo, "writes": writes,
            "clears": clears, "state": st}


def headers() -> list[dict[str, Any]]:
    return [{"range": f"{_q(DATA)}!A1:{_col(len(DATA_HEADER))}1", "values": [DATA_HEADER]},
            {"range": f"{_q(DAYS)}!A1:{_col(len(DAYS_HEADER))}1", "values": [DAYS_HEADER]}]


def check(p: dict[str, Any], result: list[dict[str, Any]]) -> list[str]:
    """result: [{"range": ..., "updatedCells": n}] as the Sheets tool reported, one per write."""
    got = {r["range"].replace("'", ""): r.get("updatedCells") for r in result}
    errors = []
    for w in p["writes"]:
        cells = sum(sum(1 for v in row if v != "") for row in w["values"])
        full = sum(len(row) for row in w["values"])
        n = got.get(w["range"].replace("'", ""))
        if n is None or n < cells or n > full:
            errors.append(f"{w['range']}: updatedCells {n}, expected {cells}..{full}")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas-productivity")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan")
    a.add_argument("--raw", required=True)
    a.add_argument("--state", required=True, help="file with the text of DAYS!P1 (may be empty)")
    a.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--plan", required=True)
    c.add_argument("--result", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "plan":
        text = Path(args.state).read_text(encoding="utf-8").strip()
        state = json.loads(text) if text else None
        p = plan(load_reports(Path(args.raw)), state)
        Path(args.out).write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"changed": p["changed"], "skipped": p.get("skipped", []),
                          "rewritten": p.get("rewritten", []),
                          "writes": [(w["range"], len(w["values"])) for w in p["writes"]],
                          "clears": p["clears"]}, ensure_ascii=False))
        return 0
    errs = check(json.loads(Path(args.plan).read_text(encoding="utf-8")),
                 json.loads(Path(args.result).read_text(encoding="utf-8")))
    print("OK" if not errs else "\n".join(errs))
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())
