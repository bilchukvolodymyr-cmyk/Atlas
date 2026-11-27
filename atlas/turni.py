"""People per day from the shifts spreadsheet ("TURNI 2026", one tab per day "27.11.2026" +
the register "DIPENDENTI"). READ ONLY: the spreadsheet is never written.

* Totals come from the tab itself (C/D cells "persone totale", "POLO 1", "POLO 2", "POLO 3").
* Agenzia / Cooperativa: every name on the tab is looked up in DIPENDENTI (TIPO PERSONALE).
  A name that is not in the register is counted as "тип невідомий", never guessed.
* Hours are not read here: the mirror computes them (people × 8, +2 %) and labels them as
  a planning calculation, not worked time.
* One tab = one inbox row (source 'turni', id = date + content hash): an unchanged tab is
  never recorded twice; an edited tab records the new numbers (metrics are overwritten).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from .mail import BUILTIN_BLOCK, MailError, final_result, tool_calls

log = logging.getLogger("atlas.turni")
TAB = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})\s*$")
SECTION = re.compile(r"\bPOLO\s*([123])\b", re.I)
NOT_NAME = re.compile(r"TURNO|PIANO|RISORSE|POLO|NUOVO|PERSONE|TOTALE|CENTRALE|NOTTURNO|"
                      r"LUNEDI|MARTEDI|MERCOLEDI|GIOVEDI|VENERDI|SABATO|DOMENICA", re.I)
SHIFT_COLUMNS = (2, 5, 8, 11)
VERSION = "2"   # part of the content hash: a counting change re-reads every tab
WRITE_TOOLS = ("update_values", "append_values", "batch_clear_values", "update_formulas",
               "update_spreadsheet", "insert_dimension", "copy_sheet_to_another_spreadsheet")


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().upper()
    return " ".join(sorted(re.sub(r"[^A-Z0-9]+", " ", s).split()))


def looks_like_name(cell: str) -> bool:
    c = str(cell).strip()
    if len(c) < 4 or NOT_NAME.search(c) or re.search(r"\d", c):
        return False
    words = re.findall(r"[A-Za-zÀ-ÿ]+", c)
    return len(words) >= 2 and c.upper() == c


def tab_date(title: str) -> str | None:
    m = TAB.match(title)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def registry(values: list[list[Any]]) -> dict[str, str]:
    """DIPENDENTI rows -> {normalized name: 'agenzia' | 'cooperativa'}."""
    out: dict[str, str] = {}
    if not values:
        return out
    head = [str(h).strip().upper() for h in values[0]]
    try:
        i_name, i_type = head.index("DIPENDENTE"), head.index("TIPO PERSONALE")
    except ValueError:
        i_name, i_type = 1, 2
    for row in values[1:]:
        if len(row) > max(i_name, i_type) and str(row[i_name]).strip():
            t = str(row[i_type]).strip().upper()
            if t.startswith("AGENZ"):
                out[norm(row[i_name])] = "agenzia"
            elif t.startswith("COOP"):
                out[norm(row[i_name])] = "cooperativa"
    return out


def _num(v: Any) -> int | None:
    try:
        return int(float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def count_day(values: list[list[Any]], reg: dict[str, str]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    for row in values[:8]:
        if len(row) >= 4:
            label, n = str(row[2]).strip().upper(), _num(row[3])
            if n is None:
                continue
            if label.startswith("PERSONE TOTALE"):
                totals["all"] = n
            elif SECTION.fullmatch(label.strip()):
                totals[f"polo{SECTION.fullmatch(label.strip()).group(1)}"] = n
    seen: dict[str, dict[str, str]] = {}            # site -> {norm name: type}
    site = None
    for row in values[6:]:
        cells = [str(c) for c in row]
        if any("PIANO RISORSE" in c.upper() for c in cells):
            for c in cells:
                m = SECTION.search(c)
                if m:
                    site = f"polo{m.group(1)}"
            continue
        if site is None:
            continue
        for col in SHIFT_COLUMNS:          # C, F, I, L; column P lists ASSENTI / RIPOSO / Lista nera
            c = cells[col] if col < len(cells) else ""
            if looks_like_name(c):
                key = norm(c)
                seen.setdefault(site, {})[key] = reg.get(key, "unknown")
    people: dict[str, dict[str, int]] = {}
    for s, names in seen.items():
        cnt = {"agenzia": 0, "cooperativa": 0, "unknown": 0}
        for t in names.values():
            cnt[t] += 1
        people[s] = cnt
    return {"totals": totals, "people": people}


def metric_actions(day: str, c: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    def rec(site: str, name: str, v: int, unit: str = "людей"):
        out.append({"type": "record_metric", "date": day, "site": site, "name": name,
                    "value": v, "unit": unit})
    for site, n in c["totals"].items():
        rec(site, "workers_total", n)
    agg = {"agenzia": 0, "cooperativa": 0, "unknown": 0}
    for site, cnt in c["people"].items():
        rec(site, "workers_agenzia", cnt["agenzia"])
        rec(site, "workers_cooperativa", cnt["cooperativa"])
        for k in agg:
            agg[k] += cnt[k]
    if c["people"]:
        rec("all", "workers_agenzia", agg["agenzia"])
        rec("all", "workers_cooperativa", agg["cooperativa"])
        rec("all", "workers_unknown_type", agg["unknown"])
        if "all" not in c["totals"]:
            rec("all", "workers_total", sum(agg.values()))
    return out


def summary(day: str, c: dict[str, Any]) -> str:
    t = c["totals"]
    sites = ", ".join(f"Polo {k[-1]}: {v}" for k, v in sorted(t.items()) if k != "all")
    agg = {"agenzia": 0, "cooperativa": 0, "unknown": 0}
    for cnt in c["people"].values():
        for k in agg:
            agg[k] += cnt[k]
    unk = f", тип невідомий {agg['unknown']}" if agg["unknown"] else ""
    return (f"Таблиця змін, {day[8:10]}.{day[5:7]}.{day[:4]}: всього {t.get('all', sum(agg.values()))} "
            f"людей ({sites}); Agenzia {agg['agenzia']}, Cooperativa {agg['cooperativa']}{unk}")


class ClaudeCodeSheetsReader:
    """Reads ranges through Claude Code's Google Sheets connector (read tools only). Values are
    taken from the tool results, not from the model's answer."""

    def __init__(self, prefix: str = "mcp__claude_ai_Google_Sheets__", model: str = "haiku",
                 executable: str | None = None, timeout: float = 900.0, runner: Any = None):
        import shutil
        import subprocess
        self.prefix, self.model, self.timeout = prefix, model, timeout
        self.executable = executable or shutil.which("claude") or "claude"
        self.runner = runner or subprocess.run

    def _run(self, tool: str, prompt: str) -> str:
        import os
        cmd = [self.executable, "-p", "--output-format", "stream-json", "--verbose",
               "--model", self.model, "--allowedTools", self.prefix + tool,
               "--disallowedTools", BUILTIN_BLOCK + "," + ",".join(self.prefix + t for t in WRITE_TOOLS),
               "--setting-sources", "", "--no-session-persistence"]
        env = dict(os.environ, MAX_MCP_OUTPUT_TOKENS=os.environ.get("MAX_MCP_OUTPUT_TOKENS", "120000"))
        try:
            proc = self.runner(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               timeout=self.timeout, env=env)
        except Exception as exc:
            raise MailError(f"claude CLI: {type(exc).__name__}: {exc}") from exc
        if proc.returncode != 0:
            raise MailError(f"claude CLI exit {proc.returncode}: {(proc.stderr or proc.stdout or '')[-300:]}")
        final_result(proc.stdout)
        return proc.stdout

    def titles(self, spreadsheet_id: str) -> list[str]:
        tool = "get_spreadsheet"
        out = self._run(tool, f"Call the tool {self.prefix}{tool} once with exactly "
                        + json.dumps({"spreadsheetId": spreadsheet_id,
                                      "fields": ["sheets.properties.title"]})
                        + ". Call no other tool. Then reply DONE.")
        calls = tool_calls(out, self.prefix + tool)
        if not calls:
            raise MailError("get_spreadsheet was not called")
        res = calls[-1][1]
        res = res.get("content", res)
        return [s["properties"]["title"] for s in res.get("sheets", [])]

    def values(self, spreadsheet_id: str, ranges: list[str]) -> dict[str, list[list[Any]]]:
        tool = "get_values"
        prompt = (f"Call the tool {self.prefix}{tool} once for EACH range in this list, with "
                  f"spreadsheetId \"{spreadsheet_id}\" and the range copied exactly. Call no other "
                  "tool and change nothing. Then reply DONE.\n" + json.dumps(ranges, ensure_ascii=False))
        out = self._run(tool, prompt)
        got: dict[str, list[list[Any]]] = {}
        for inp, res in tool_calls(out, self.prefix + tool):
            if isinstance(res, dict):
                got[inp.get("range", "")] = res.get("values") or []
        missing = [r for r in ranges if r not in got]
        if missing:
            raise MailError(f"ranges not read: {missing[:5]}")
        return got


class TurniSync:
    def __init__(self, db: str, reader: Any, spreadsheet_id: str, ledger: str | None = None,
                 chunk: int = 20, recheck_last: int = 3):
        self.db, self.reader, self.sid = db, reader, spreadsheet_id
        self.ledger = Path(ledger or Path(db).expanduser().with_name("mail.db"))
        self.chunk, self.recheck_last = chunk, recheck_last
        c = sqlite3.connect(self.ledger, timeout=30)
        c.execute("CREATE TABLE IF NOT EXISTS turni (tab TEXT PRIMARY KEY, day TEXT, hash TEXT, "
                  "inbox_id INTEGER)")
        c.commit()
        c.close()

    def _known(self) -> dict[str, str]:
        c = sqlite3.connect(self.ledger, timeout=30)
        try:
            return {r[0]: r[1] for r in c.execute("SELECT tab, hash FROM turni")}
        finally:
            c.close()

    def _mark(self, tab: str, day: str, h: str, inbox_id: int) -> None:
        c = sqlite3.connect(self.ledger, timeout=30)
        with c:
            c.execute("INSERT OR REPLACE INTO turni VALUES (?,?,?,?)", (tab, day, h, inbox_id))
        c.close()

    def sync(self, limit: int | None = None) -> dict[str, int]:
        from .core import Store
        tabs = [(t, tab_date(t)) for t in self.reader.titles(self.sid)]
        tabs = sorted([(t, d) for t, d in tabs if d], key=lambda x: x[1])
        known = self._known()
        todo = [t for t in tabs if t[0] not in known] + \
               [t for t in tabs[-self.recheck_last:] if t[0] in known]
        if limit:
            todo = todo[:limit]
        if not todo:
            return {"tabs": len(tabs), "read": 0, "recorded": 0}
        reg = registry(self.reader.values(self.sid, ["'DIPENDENTI'!A1:C1000"])["'DIPENDENTI'!A1:C1000"])
        recorded = read = 0
        for i in range(0, len(todo), self.chunk):
            part = todo[i:i + self.chunk]
            ranges = {f"'{t}'!A1:P200": (t, d) for t, d in part}
            vals = self.reader.values(self.sid, list(ranges))
            store = Store(self.db)
            try:
                for rng, (t, d) in ranges.items():
                    v = vals[rng]
                    read += 1
                    h = hashlib.sha256(json.dumps([VERSION, v, sorted(reg.items())], ensure_ascii=False)
                                       .encode()).hexdigest()[:16]
                    if known.get(t) == h:
                        continue
                    c = count_day(v, reg)
                    acts = metric_actions(d, c)
                    if not acts:
                        self._mark(t, d, h, 0)
                        continue
                    inbox_id, _ = store.ingest("turni", f"{d}:{h}", summary(d, c))
                    store.apply(inbox_id, acts, actor="system")
                    self._mark(t, d, h, inbox_id)
                    recorded += 1
            finally:
                store.close()
        log.info("turni: %s tabs, read %s, recorded %s", len(tabs), read, recorded)
        return {"tabs": len(tabs), "read": read, "recorded": recorded}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    from .mail import load_config
    p = argparse.ArgumentParser(prog="atlas-turni")
    p.add_argument("--db", default=os.environ.get("ATLAS_DB", "~/Atlas/atlas.db"))
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config() or {}
    if not cfg.get("turni_spreadsheet_id"):
        print("no turni_spreadsheet_id in ~/Atlas/mail.json")
        return 1
    sync = TurniSync(str(Path(args.db).expanduser()), ClaudeCodeSheetsReader(
        prefix=cfg.get("sheets_tool_prefix", "mcp__claude_ai_Google_Sheets__")),
        cfg["turni_spreadsheet_id"])
    print(json.dumps(sync.sync(limit=args.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
