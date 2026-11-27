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
OVERTIME_RATE = 0.02

JOURNAL = "Журнал"
LOOPS = "Цикли"
METRICS = "Показники по днях"
DECISIONS = "Рішення та правила"

HEADERS: dict[str, list[str]] = {
    JOURNAL: ["ID", "Дата і час", "Джерело", "Текст (що почув Atlas)", "Стан обробки",
              "Що записано"],
    LOOPS: ["ID", "Справа", "Тип", "Стан", "Наступна дія", "Строк", "Нагадати",
            "Учасники", "Чекаю відповіді від", "Бажаний результат", "Як перевірити завершення",
            "Важливість", "Створено з запису (ID журналу)", "Оновлено", "Змін в історії"],
    METRICS: ["Дата", "Agenzia (людей)", "Cooperativa (людей)", "Всього людей",
              "Polo 1 (людей)", "Polo 2 (людей)",
              "Базові години — РОЗРАХУНОК (люди×8)",
              "Понаднормові — РОЗРАХУНОК (базові×0,02)",
              "Загальні години — РОЗРАХУНОК",
              "Машин на розвантаження", "Машин на завантаження", "Інші показники",
              "Примітка"],
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
SOURCE = {"icloud": "iPhone", "shortcut": "iPhone", "web": "веб", "cli": "Mac (вручну)"}
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
            "answered": "відповідь на "}.get(e["type"], f"{e['type']} ")
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


def metric_rows(c: sqlite3.Connection) -> dict[str, list[Any]]:
    by_day: dict[str, dict[tuple[str, str], float]] = {}
    for m in c.execute("SELECT date, site, name, value FROM metrics ORDER BY date, name, site"):
        by_day.setdefault(m["date"], {})[(m["name"], m["site"])] = m["value"]
    rows = {}
    for day, vals in by_day.items():
        get = vals.get
        agenzia, coop = get(("workers_agenzia", "all")), get(("workers_cooperativa", "all"))
        p1, p2 = get(("workers_total", "polo1")), get(("workers_total", "polo2"))
        total, note = get(("workers_total", "all")), []
        if total is None and agenzia is not None and coop is not None:
            total = agenzia + coop
            note.append("всього = Agenzia + Cooperativa")
        elif total is None and p1 is not None and p2 is not None:
            total = p1 + p2
            note.append("всього = Polo 1 + Polo 2")
        base = total * HOURS_PER_PERSON if total is not None else None
        over = base * OVERTIME_RATE if base is not None else None
        if base is not None:
            note.append("години — розрахунок для планування, не фактичний табель")
        shown = {("workers_agenzia", "all"), ("workers_cooperativa", "all"),
                 ("workers_total", "all"), ("workers_total", "polo1"), ("workers_total", "polo2"),
                 ("trucks_unload", "all"), ("trucks_load", "all")}
        other = "; ".join(f"{n}{'' if s == 'all' else ' ' + s} = {_num(v)}"
                          for (n, s), v in sorted(vals.items()) if (n, s) not in shown)
        rows[day] = [day, _num(agenzia), _num(coop), _num(total), _num(p1), _num(p2),
                     _num(base), _num(over), _num(base + over if base is not None else None),
                     _num(get(("trucks_unload", "all"))), _num(get(("trucks_load", "all"))),
                     other, "; ".join(note)]
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
                DECISIONS: decision_rows(c)}
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


class Mirror:
    def __init__(self, db: str | os.PathLike, url: str, token: str,
                 state: str | os.PathLike | None = None,
                 post: Callable[[str, dict[str, Any]], dict[str, Any]] = http_post):
        self.db = Path(db).expanduser()
        self.url, self.token, self.post = url, token, post
        self.state_path = Path(state).expanduser() if state else self.db.with_name("mirror.db")
        s = sqlite3.connect(self.state_path, timeout=30)
        s.execute("CREATE TABLE IF NOT EXISTS sent (sheet TEXT NOT NULL, key TEXT NOT NULL, "
                  "hash TEXT NOT NULL, PRIMARY KEY (sheet, key))")
        s.commit()
        s.close()

    def _sent(self) -> dict[tuple[str, str], str]:
        s = sqlite3.connect(self.state_path, timeout=30)
        try:
            return {(r[0], r[1]): r[2] for r in s.execute("SELECT sheet, key, hash FROM sent")}
        finally:
            s.close()

    def _mark(self, items: list[tuple[str, str, str]]) -> None:
        s = sqlite3.connect(self.state_path, timeout=30)
        try:
            with s:
                s.executemany("INSERT OR REPLACE INTO sent(sheet, key, hash) VALUES (?,?,?)", items)
        finally:
            s.close()

    def sync(self) -> dict[str, int]:
        """Send rows that are new or changed since the last successful send."""
        sent = self._sent()
        pending: list[tuple[str, str, list[Any], str]] = []
        for sheet, rows in build(self.db).items():
            for key, row in rows.items():
                h = _hash(row)
                if sent.get((sheet, key)) != h:
                    pending.append((sheet, key, row, h))
        counts = {"sent": 0, "updated": 0, "appended": 0}
        for i in range(0, len(pending), CHUNK):
            part = pending[i:i + CHUNK]
            sheets: dict[str, dict[str, Any]] = {}
            for sheet, _key, row, _h in part:
                sheets.setdefault(sheet, {"header": HEADERS[sheet], "rows": []})["rows"].append(row)
            answer = self.post(self.url, {"token": self.token, "sheets": sheets})
            if not answer.get("ok"):
                raise MirrorError(f"Google sheet refused: {answer.get('error', answer)}")
            self._mark([(sheet, key, h) for sheet, key, _row, h in part])
            counts["sent"] += len(part)
            counts["updated"] += int(answer.get("updated", 0))
            counts["appended"] += int(answer.get("appended", 0))
        if counts["sent"]:
            log.info("mirror: %s", counts)
        return counts


def load_config(path: str | os.PathLike = "~/Atlas/sheets.json") -> tuple[str, str] | None:
    url, token = os.environ.get("ATLAS_SHEETS_URL"), os.environ.get("ATLAS_SHEETS_TOKEN")
    p = Path(path).expanduser()
    if (not url or not token) and p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
        url, token = url or cfg.get("url"), token or cfg.get("token")
    return (url, token) if url and token else None


def from_config(db: str | os.PathLike, path: str | os.PathLike = "~/Atlas/sheets.json") -> Mirror | None:
    cfg = load_config(path)
    return Mirror(db, *cfg) if cfg else None


class AutoMirror:
    """Picks up ~/Atlas/sheets.json when it appears, without restarting the service."""

    def __init__(self, db: str | os.PathLike, path: str | os.PathLike = "~/Atlas/sheets.json"):
        self.db, self.path, self.mirror = db, path, None

    def sync(self) -> dict[str, int]:
        if self.mirror is None:
            self.mirror = from_config(self.db, self.path)
            if self.mirror is None:
                return {"sent": 0, "updated": 0, "appended": 0}
        return self.mirror.sync()


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
