"""Mirror of the Atlas database into a Google Sheet ("Atlas — пам'ять"), so that Claude on the
iPhone (voice mode + Google Drive) can answer questions like "скільки людей було в липні".

* SQLite stays the source of truth. This module only READS atlas.db (read-only connection).
* Every row has a stable key (inbox id, loop id, date, entry id). The sheet side
  (deploy/sheets/Code.gs) updates the row with that key or appends it: sending the same row
  twice never makes a duplicate.
* What was already sent is remembered as a hash per row in a separate small file
  (mirror.db). If that file is lost, everything is simply sent again — still no duplicates.
* No network or a Google error -> nothing is marked as sent, the next pass retries.

Configuration: ~/Atlas/sheets.json  {"url": "<Apps Script web app /exec URL>", "token": "..."}
(or env ATLAS_SHEETS_URL / ATLAS_SHEETS_TOKEN). Without it the mirror does nothing.
Run once by hand:  python3 -m atlas.mirror --db ~/Atlas/atlas.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("atlas.mirror")
TZ = ZoneInfo("Europe/Rome")
CHUNK = 200
HOURS_PER_PERSON = 8
OVERTIME_RATE = 0.02          # default when no percent is known for the period (user profile)

JOURNAL = "Журнал"
LOOPS = "Цикли"
METRICS = "Показники по днях"
DECISIONS = "Рішення та правила"
MONTHS = "Підсумки по місяцях"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
MONTH_NAMES = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень",
               "серпень", "вересень", "жовтень", "листопад", "грудень"]

HEADERS: dict[str, list[str]] = {
    JOURNAL: ["ID", "Дата і час", "Джерело", "Текст (що почув Atlas)", "Стан обробки",
              "Що записано"],
    LOOPS: ["ID", "Справа", "Тип", "Стан", "Наступна дія", "Строк", "Нагадати",
            "Учасники", "Чекаю відповіді від", "Бажаний результат", "Як перевірити завершення",
            "Важливість", "Створено з запису (ID журналу)", "Оновлено", "Змін в історії"],
    METRICS: ["Дата", "День", "Всього людей", "Polo 1 (людей)", "Polo 2 (людей)", "Polo 3 (людей)",
              "Agenzia (людей)", "Cooperativa (людей)",
              "Базові години — РОЗРАХУНОК (люди×8)",
              "Понаднормові — РОЗРАХУНОК (базові×відсоток)",
              "Загальні години — РОЗРАХУНОК",
              "Розвантажень за програмою (машин)", "Розвантажень Polo 1", "Розвантажень Polo 2",
              "Шин за програмою (шт.)", "Navette out", "Машин на завантаження",
              "Зміна до попереднього робочого дня", "Інші показники", "Примітка",
              "Відсоток понаднормових — застосовано"],
    MONTHS: ["Місяць", "Днів з даними про людей", "Середньо людей на день", "Найбільше людей",
             "Найменше людей", "Середньо Agenzia", "Середньо Cooperativa",
             "Базові години за місяць — РОЗРАХУНОК", "Загальні години за місяць — РОЗРАХУНОК",
             "Днів з програмою розвантажень", "Розвантажень за місяць (машин)",
             "Середньо розвантажень на день", "Шин за програмою за місяць (шт.)",
             "Зміна до попереднього місяця", "Примітка"],
    DECISIONS: ["ID", "Дата", "Текст", "Коротко", "Створено з запису (ID журналу)"],
}

KIND = {"task": "справа", "reminder": "нагадування", "waiting_for": "чекаю відповідь",
        "agreement": "домовленість"}
STATE = {"needs_info": "бракує даних", "active": "в роботі", "waiting": "чекаю",
         "done_claimed": "кажуть, що зроблено", "verifying": "перевіряю",
         "closed": "закрито", "dropped": "скасовано"}
DONE = {"self": "достатньо мого слова", "artifact": "потрібен доказ (фото/файл)",
        "deferred": "перевірити пізніше"}
IMPORTANCE = {"low": "низька", "normal": "звичайна", "high": "висока"}
INBOX_STATUS = {"new": "нове", "processing": "обробляється", "done": "оброблено",
                "failed": "помилка, повторю"}
SOURCE = {"icloud": "iPhone", "gmail": "Пошта (Gmail)", "programma": "Пошта: програма розвантажень",
          "turni": "Таблиця змін", "shortcut": "iPhone", "web": "веб", "cli": "Mac (вручну)"}
OBJ = {"loop": "справа", "entry": "запис", "idea": "ідея", "metric": "показник",
       "question": "питання"}
ENTRY_KIND = {"event": "подія", "note": "нотатка", "decision": "рішення", "problem": "проблема"}


def local(iso: str | None) -> str:
    """UTC ISO -> 'YYYY-MM-DD HH:MM' in Italy. A plain date stays as it is."""
    if not iso:
        return ""
    if len(iso) <= 10:
        return iso
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def _num(v: float | None) -> Any:
    if v is None:
        return ""
    return int(v) if float(v).is_integer() else round(float(v), 2)


# ---------------------------------------------------------------- rows from the database
def _event_text(e: sqlite3.Row) -> str:
    p = json.loads(e["payload"])
    after = p.get("after") or {}
    what = OBJ.get(e["object_type"], e["object_type"])
    if e["object_type"] == "loop":
        label = f"«{after.get('title', '')}»"
        if e["type"] != "created" and after.get("state"):
            label += f" → {STATE.get(after['state'], after['state'])}"
    elif e["object_type"] == "metric":
        site = after.get("site")
        label = f"{after.get('name')}{'' if site in (None, 'all') else ' ' + site} = {_num(after.get('value'))}"
    elif e["object_type"] == "entry":
        what = ENTRY_KIND.get(after.get("kind"), what)
        label = f"«{(after.get('summary') or after.get('text') or '')[:80]}»"
    elif e["object_type"] in ("idea", "question"):
        label = f"«{(after.get('title') or after.get('text') or '')[:80]}»"
    else:
        label = ""
    if e["type"] == "evidence_added":
        return f"доказ до справи #{e['object_id']}: «{str(p.get('evidence', ''))[:80]}»"
    verb = {"created": "", "updated": "змінено ", "state_changed": "", "verified": "перевірено ",
            "answered": "відповідь на ", "asked": ""}.get(e["type"], f"{e['type']} ")
    return f"{verb}{what} #{e['object_id']} {label}".strip()


def journal_rows(c: sqlite3.Connection) -> dict[str, list[Any]]:
    done: dict[int, list[str]] = {}
    for e in c.execute("SELECT * FROM events ORDER BY inbox_id, seq"):
        done.setdefault(e["inbox_id"], []).append(_event_text(e))
    rows = {}
    for m in c.execute("SELECT * FROM inbox ORDER BY id"):
        rows[str(m["id"])] = [m["id"], local(m["received_at"]), SOURCE.get(m["source"], m["source"]),
                              m["raw_text"], INBOX_STATUS.get(m["status"], m["status"]),
                              "; ".join(done.get(m["id"], []))]
    return rows


def loop_rows(c: sqlite3.Connection) -> dict[str, list[Any]]:
    counts = {r[0]: r[1] for r in c.execute(
        "SELECT object_id, COUNT(*) FROM events WHERE object_type='loop' GROUP BY object_id")}
    rows = {}
    for l in c.execute("SELECT * FROM loops ORDER BY id"):
        people = ", ".join(x for x in (
            f"доручив {l['assigned_by']}" if l["assigned_by"] else "",
            f"чекаю на {l['waiting_on']}" if l["waiting_on"] else "") if x)
        rows[str(l["id"])] = [
            l["id"], l["title"], KIND.get(l["kind"], l["kind"]), STATE.get(l["state"], l["state"]),
            l["next_action"] or "", local(l["due_at"]), local(l["remind_at"]), people,
            l["waiting_on"] or "", l["desired_outcome"] or "",
            DONE.get(l["done_criteria"], l["done_criteria"] or ""),
            IMPORTANCE.get(l["importance"], l["importance"] or ""), l["created_from"],
            local(l["updated_at"]), counts.get(l["id"], 0)]
    return rows


def _day_values(c: sqlite3.Connection) -> dict[str, dict[tuple[str, str], float]]:
    by_day: dict[str, dict[tuple[str, str], float]] = {}
    for m in c.execute("SELECT date, site, name, value FROM metrics ORDER BY date, name, site"):
        by_day.setdefault(m["date"], {})[(m["name"], m["site"])] = m["value"]
    return by_day


def _people(vals: dict[tuple[str, str], float]) -> tuple[float | None, str]:
    get = vals.get
    total = get(("workers_total", "all"))
    if total is not None:
        return total, ""
    ag, co = get(("workers_agenzia", "all")), get(("workers_cooperativa", "all"))
    if ag is not None and co is not None:
        return ag + co, "всього = Agenzia + Cooperativa"
    sites = [get(("workers_total", f"polo{i}")) for i in (1, 2, 3)]
    if any(v is not None for v in sites):
        return sum(v or 0 for v in sites), "всього = сума по Polo"
    return None, ""


def _rates(c: sqlite3.Connection) -> list[tuple[str, float]]:
    """(from_date, percent) of the overtime percent, as recorded (shift sheet or voice)."""
    return [(r["date"], float(r["value"])) for r in c.execute(
        "SELECT date, value FROM metrics WHERE name='overtime_rate' AND site='all' ORDER BY date")]


def rate_for(day: str, rates: list[tuple[str, float]]) -> tuple[float, str]:
    """The percent in force on that day: the latest one recorded on or before it, else 2 %."""
    found = None
    for since, pct in rates:
        if since <= day:
            found = (since, pct)
    if found is None:
        return OVERTIME_RATE, f"відсоток {_num(OVERTIME_RATE * 100)}% — за замовчуванням"
    since, pct = found
    return pct / 100, f"відсоток {_num(pct)}% — заданий з {since[8:10]}.{since[5:7]}.{since[:4]}"


def _delta(label: str, now: float | None, before: float | None) -> str:
    if now is None or before is None:
        return ""
    d = now - before
    sign = "+" if d > 0 else ("−" if d < 0 else "±")
    return f"{label} {sign}{_num(abs(d))} ({_num(before)}→{_num(now)})"


SHOWN = {("workers_agenzia", "all"), ("workers_cooperativa", "all"), ("workers_total", "all"),
         ("workers_total", "polo1"), ("workers_total", "polo2"), ("workers_total", "polo3"),
         ("trucks_unload", "all"), ("trucks_unload", "polo1"), ("trucks_unload", "polo2"),
         ("trucks_unload", "polo3"), ("unload_pieces", "all"), ("unload_pieces", "polo1"),
         ("unload_pieces", "polo2"), ("unload_pieces", "polo3"), ("navette_out", "all"),
         ("trucks_load", "all"), ("workers_agenzia", "polo1"), ("workers_agenzia", "polo2"),
         ("workers_agenzia", "polo3"), ("workers_cooperativa", "polo1"),
         ("workers_cooperativa", "polo2"), ("workers_cooperativa", "polo3")}


def metric_rows(c: sqlite3.Connection) -> dict[str, list[Any]]:
    rows = {}
    prev: dict[tuple[str, str], float] | None = None
    prev_people: float | None = None
    rates = _rates(c)
    for day, vals in sorted(_day_values(c).items()):
        get = vals.get
        total, how = _people(vals)
        note = [how] if how else []
        base = total * HOURS_PER_PERSON if total is not None else None
        rate, rate_how = rate_for(day, rates)
        over = base * rate if base is not None else None
        if base is not None:
            note.append("години — розрахунок для планування, не фактичний табель; " + rate_how)
        unk = get(("workers_unknown_type", "all"))
        if unk:
            note.append(f"{_num(unk)} людей немає в DIPENDENTI — тип не визначено")
        changes = []
        if prev is not None:
            changes = [x for x in (
                _delta("люди", total, prev_people),
                _delta("розвантаження", get(("trucks_unload", "all")), prev.get(("trucks_unload", "all"))),
                _delta("шини", get(("unload_pieces", "all")), prev.get(("unload_pieces", "all"))),
            ) if x]
        other = "; ".join(f"{n}{'' if s == 'all' else ' ' + s} = {_num(v)}"
                          for (n, s), v in sorted(vals.items())
                          if (n, s) not in SHOWN and n not in ("workers_unknown_type", "overtime_rate"))
        wd = WEEKDAYS[datetime.fromisoformat(day).weekday()]
        rows[day] = [day, wd, _num(total), _num(get(("workers_total", "polo1"))),
                     _num(get(("workers_total", "polo2"))), _num(get(("workers_total", "polo3"))),
                     _num(get(("workers_agenzia", "all"))), _num(get(("workers_cooperativa", "all"))),
                     _num(base), _num(over), _num(base + over if base is not None else None),
                     _num(get(("trucks_unload", "all"))), _num(get(("trucks_unload", "polo1"))),
                     _num(get(("trucks_unload", "polo2"))), _num(get(("unload_pieces", "all"))),
                     _num(get(("navette_out", "all"))), _num(get(("trucks_load", "all"))),
                     "; ".join(changes), other, "; ".join(note),
                     f"{_num(round(rate * 100, 2))}%" if base is not None else ""]
        prev = vals
        if total is not None:
            prev_people = total
    return rows


def month_rows(c: sqlite3.Connection) -> dict[str, list[Any]]:
    months: dict[str, dict[str, list[float]]] = {}
    rates = _rates(c)
    for day, vals in _day_values(c).items():
        m = months.setdefault(day[:7], {"people": [], "ag": [], "co": [], "unl": [], "pcs": [],
                                        "hours": [], "pct": []})
        total, _ = _people(vals)
        if total is not None:
            m["people"].append(total)
            r, _how = rate_for(day, rates)
            m["hours"].append(total * HOURS_PER_PERSON * (1 + r))
            m["pct"].append(round(r * 100, 2))
        for key, name in (("ag", "workers_agenzia"), ("co", "workers_cooperativa"),
                          ("unl", "trucks_unload"), ("pcs", "unload_pieces")):
            if (name, "all") in vals:
                m[key].append(vals[(name, "all")])
    rows, prev_avg = {}, None
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    for key in sorted(months):
        m = months[key]
        a = avg(m["people"])
        base = sum(m["people"]) * HOURS_PER_PERSON if m["people"] else None
        change = ""
        if a is not None and prev_avg is not None:
            change = _delta("середньо людей", round(a, 1), round(prev_avg, 1))
        label = f"{MONTH_NAMES[int(key[5:7]) - 1]} {key[:4]}"
        rows[key] = [label, len(m["people"]), _num(round(a, 1) if a is not None else None),
                     _num(max(m["people"]) if m["people"] else None),
                     _num(min(m["people"]) if m["people"] else None),
                     _num(round(avg(m["ag"]), 1) if m["ag"] else None),
                     _num(round(avg(m["co"]), 1) if m["co"] else None),
                     _num(base), _num(round(sum(m["hours"]), 2) if m["hours"] else None),
                     len(m["unl"]), _num(sum(m["unl"]) if m["unl"] else None),
                     _num(round(avg(m["unl"]), 1) if m["unl"] else None),
                     _num(sum(m["pcs"]) if m["pcs"] else None), change,
                     ("години — розрахунок для планування, не фактичний табель; відсоток понаднормових: "
                      + ", ".join(f"{_num(p)}%" for p in sorted(set(m["pct"])))) if base else ""]
        if a is not None:
            prev_avg = a
    return rows


def decision_rows(c: sqlite3.Connection) -> dict[str, list[Any]]:
    rows = {}
    for e in c.execute("SELECT e.*, i.received_at FROM entries e JOIN inbox i ON i.id=e.inbox_id "
                       "WHERE e.kind='decision' ORDER BY e.id"):
        rows[str(e["id"])] = [e["id"], local(e["happened_at"] or e["received_at"]), e["text"],
                              e["summary"] or "", e["inbox_id"]]
    return rows


def build(db: str | os.PathLike) -> dict[str, dict[str, list[Any]]]:
    c = sqlite3.connect(f"file:{Path(db).expanduser()}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        return {JOURNAL: journal_rows(c), LOOPS: loop_rows(c), METRICS: metric_rows(c),
                MONTHS: month_rows(c), DECISIONS: decision_rows(c)}
    finally:
        c.close()


# ---------------------------------------------------------------- sending
def _hash(row: list[Any]) -> str:
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, default=str).encode()).hexdigest()


def http_post(url: str, payload: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
    """POST JSON to the Apps Script web app. Google answers with a redirect to the result,
    which urllib follows as GET (that is what Apps Script expects)."""
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise MirrorError(f"not JSON from Google: {body[:200]!r}") from exc


class MirrorError(Exception):
    pass


def _col(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


class ClaudeCodeSheets:
    """Writes rows into the sheet through the Claude Code CLI on the Mac, which is logged in with
    the user's Claude subscription and has the Google Sheets connector. No Apps Script, no
    Google Cloud project, no payment. Python decides every cell and every range; Claude only
    passes them to the Sheets tool, and the reported cell counts are checked by Python."""

    SCHEMA = {"type": "object", "required": ["results"], "properties": {"results": {
        "type": "array", "items": {"type": "object", "required": ["range", "updatedCells"],
                                   "properties": {"range": {"type": "string"},
                                                  "updatedCells": {"type": "integer"}}}}}}

    def __init__(self, spreadsheet_id: str, model: str = "sonnet",
                 tool: str = "mcp__claude_ai_Google_Sheets__update_values",
                 executable: str | None = None, timeout: float = 300.0, runner: Any = None):
        import shutil
        import subprocess
        self.spreadsheet_id, self.model, self.tool, self.timeout = spreadsheet_id, model, tool, timeout
        self.executable = executable or shutil.which("claude") or "claude"
        self.runner = runner or subprocess.run

    def calls(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for name, part in payload["sheets"].items():
            width = len(part["header"])
            q = "'" + name.replace("'", "''") + "'"
            if part.get("write_header"):
                out.append({"range": f"{q}!A1:{_col(width)}1", "values": [part["header"]]})
            rows = sorted(zip(part["at"], part["rows"]))
            i = 0
            while i < len(rows):
                j = i
                while j + 1 < len(rows) and rows[j + 1][0] == rows[j][0] + 1:
                    j += 1
                out.append({"range": f"{q}!A{rows[i][0]}:{_col(width)}{rows[j][0]}",
                            "values": [r for _n, r in rows[i:j + 1]]})
                i = j + 1
        return out

    def __call__(self, _url: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls = self.calls(payload)
        prompt = (
            f"Call the tool {self.tool} once for EACH item of the JSON list below, with "
            f"spreadsheetId \"{self.spreadsheet_id}\" and the item's range and values copied "
            "exactly: same text, same numbers, same empty strings, same order. Do not change, "
            "translate, fix or skip anything, and call no other tool. Then return results: for "
            "each item its range and the updatedCells number the tool reported.\n\n"
            + json.dumps(calls, ensure_ascii=False))
        cmd = [self.executable, "-p", "--output-format", "json",
               "--json-schema", json.dumps(self.SCHEMA), "--model", self.model,
               # --tools "" would also hide the claude.ai connectors, so block built-ins instead
               "--allowedTools", self.tool,
               "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task",
               "--setting-sources", "", "--no-session-persistence"]
        try:
            proc = self.runner(cmd, input=prompt, capture_output=True, text=True,
                               encoding="utf-8", timeout=self.timeout)
        except Exception as exc:
            raise MirrorError(f"claude CLI: {type(exc).__name__}: {exc}") from exc
        if proc.returncode != 0:
            raise MirrorError(f"claude CLI exit {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
        try:
            env = json.loads(proc.stdout)
        except ValueError as exc:
            raise MirrorError(f"claude CLI output is not JSON: {proc.stdout[:300]}") from exc
        if env.get("is_error"):
            raise MirrorError(f"claude CLI error: {str(env.get('result'))[:300]}")
        results = (env.get("structured_output") or {}).get("results") or []
        got = {r.get("range", "").replace("'", ""): r.get("updatedCells") for r in results}
        for c in calls:
            cells = sum(len(v) for v in c["values"])
            reported = got.get(c["range"].replace("'", ""))
            # the Sheets API does not count trailing empty cells, so allow fewer, never zero/none
            if not reported or reported > cells:
                raise MirrorError(f"sheet write not confirmed for {c['range']}: {reported} of {cells}")
        return {"ok": True, "updated": 0, "appended": 0, "calls": len(calls)}


class Mirror:
    def __init__(self, db: str | os.PathLike, url: str, token: str,
                 state: str | os.PathLike | None = None,
                 post: Callable[[str, dict[str, Any]], dict[str, Any]] = http_post,
                 chunk: int = CHUNK):
        self.db = Path(db).expanduser()
        self.url, self.token, self.post, self.chunk = url, token, post, chunk
        self.state_path = Path(state).expanduser() if state else self.db.with_name("mirror.db")
        s = sqlite3.connect(self.state_path, timeout=30)
        s.execute("CREATE TABLE IF NOT EXISTS sent (sheet TEXT NOT NULL, key TEXT NOT NULL, "
                  "hash TEXT NOT NULL, PRIMARY KEY (sheet, key))")
        s.execute("CREATE TABLE IF NOT EXISTS rows (sheet TEXT NOT NULL, key TEXT NOT NULL, "
                  "rownum INTEGER NOT NULL, PRIMARY KEY (sheet, key))")
        s.commit()
        s.close()

    def _read(self) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], int]]:
        s = sqlite3.connect(self.state_path, timeout=30)
        try:
            sent = {(r[0], r[1]): r[2] for r in s.execute("SELECT sheet, key, hash FROM sent")}
            rows = {(r[0], r[1]): r[2] for r in s.execute("SELECT sheet, key, rownum FROM rows")}
            return sent, rows
        finally:
            s.close()

    def _mark(self, items: list[tuple[str, str, str, int]]) -> None:
        s = sqlite3.connect(self.state_path, timeout=30)
        try:
            with s:
                s.executemany("INSERT OR REPLACE INTO sent(sheet, key, hash) VALUES (?,?,?)",
                              [(a, b, c) for a, b, c, _n in items])
                s.executemany("INSERT OR REPLACE INTO rows(sheet, key, rownum) VALUES (?,?,?)",
                              [(a, b, n) for a, b, _c, n in items if n])
        finally:
            s.close()

    def sync(self) -> dict[str, int]:
        """Send rows that are new or changed since the last successful send. Each key keeps
        its row number in the sheet for ever (row 1 = header), so a resend overwrites it."""
        sent, rownums = self._read()
        next_row: dict[str, int] = {}
        for (sheet, _k), n in rownums.items():
            next_row[sheet] = max(next_row.get(sheet, 1), n)
        pending: list[tuple[str, str, list[Any], str, int]] = []
        for sheet, rows in build(self.db).items():
            hh = _hash(HEADERS[sheet])
            if sent.get((sheet, "__header__")) != hh:
                pending.append((sheet, "__header__", HEADERS[sheet], hh, 0))
            for key, row in rows.items():
                h = _hash(row)
                if sent.get((sheet, key)) != h:
                    n = rownums.get((sheet, key))
                    if n is None:
                        n = next_row[sheet] = next_row.get(sheet, 1) + 1
                    pending.append((sheet, key, row, h, n))
        counts = {"sent": 0, "updated": 0, "appended": 0}
        data = [p for p in pending if p[1] != "__header__"]
        headers = [p for p in pending if p[1] == "__header__"]
        for i in range(0, max(len(data), 1 if headers else 0), self.chunk):
            part = data[i:i + self.chunk] + (headers if i == 0 else [])
            sheets: dict[str, dict[str, Any]] = {}
            for sheet, key, row, _h, n in part:
                d = sheets.setdefault(sheet, {"header": HEADERS[sheet], "rows": [], "at": []})
                if key == "__header__":
                    d["write_header"] = True
                else:
                    d["rows"].append(row)
                    d["at"].append(n)
            answer = self.post(self.url, {"token": self.token, "sheets": sheets})
            if not answer.get("ok"):
                raise MirrorError(f"Google sheet refused: {answer.get('error', answer)}")
            self._mark([(sheet, key, h, n) for sheet, key, _row, h, n in part])
            counts["sent"] += sum(1 for p in part if p[1] != "__header__")
            counts["updated"] += int(answer.get("updated", 0))
            counts["appended"] += int(answer.get("appended", 0))
        if counts["sent"]:
            log.info("mirror: %s", counts)
        return counts


def load_config(path: str | os.PathLike = "~/Atlas/sheets.json") -> dict[str, Any] | None:
    cfg: dict[str, Any] = {}
    p = Path(path).expanduser()
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
    if os.environ.get("ATLAS_SHEETS_URL"):
        cfg["url"] = os.environ["ATLAS_SHEETS_URL"]
    if os.environ.get("ATLAS_SHEETS_TOKEN"):
        cfg["token"] = os.environ["ATLAS_SHEETS_TOKEN"]
    if cfg.get("spreadsheet_id") or (cfg.get("url") and cfg.get("token")):
        return cfg
    return None


def from_config(db: str | os.PathLike, path: str | os.PathLike = "~/Atlas/sheets.json") -> Mirror | None:
    """sheets.json either {"spreadsheet_id": ...} (written through Claude Code's Google Sheets
    connector) or {"url": ..., "token": ...} (Apps Script web app, deploy/sheets/Code.gs)."""
    cfg = load_config(path)
    if cfg is None:
        return None
    if cfg.get("spreadsheet_id"):
        kw = {k: cfg[k] for k in ("model", "tool") if cfg.get(k)}
        return Mirror(db, "", "", post=ClaudeCodeSheets(cfg["spreadsheet_id"], **kw),
                      chunk=int(cfg.get("chunk", 60)))
    return Mirror(db, cfg["url"], cfg["token"])


class AutoMirror:
    """Picks up ~/Atlas/sheets.json when it appears, without restarting the service.
    After a failure it waits before trying again (a Claude call is not free of limits)."""

    def __init__(self, db: str | os.PathLike, path: str | os.PathLike = "~/Atlas/sheets.json",
                 clock: Callable[[], float] | None = None, retry_after: float = 300.0):
        import time
        self.db, self.path, self.mirror = db, path, None
        self.clock = clock or time.time
        self.retry_after, self.not_before = retry_after, 0.0

    def sync(self) -> dict[str, int]:
        if self.clock() < self.not_before:
            return {"sent": 0, "updated": 0, "appended": 0}
        if self.mirror is None:
            self.mirror = from_config(self.db, self.path)
            if self.mirror is None:
                return {"sent": 0, "updated": 0, "appended": 0}
        try:
            return self.mirror.sync()
        except Exception:
            self.not_before = self.clock() + self.retry_after
            raise


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atlas-mirror")
    p.add_argument("--db", default=os.environ.get("ATLAS_DB", "~/Atlas/atlas.db"))
    p.add_argument("--config", default="~/Atlas/sheets.json")
    p.add_argument("--all", action="store_true", help="forget what was sent and send everything")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    m = from_config(args.db, args.config)
    if m is None:
        print("no Google sheet configured (~/Atlas/sheets.json)")
        return 1
    if args.all:
        s = sqlite3.connect(m.state_path)
        with s:
            s.execute("DELETE FROM sent")
        s.close()
    print(json.dumps(m.sync(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
