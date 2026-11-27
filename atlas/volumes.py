"""Daily work volumes from Fintyre e-mails: the unloading programme ("Programma 26/11/2026",
"volumi in&out del 25/11/26", "volumi di lunedì").

Their text is a table pasted as lines: every arrival starts with its date and site,

    26/11/2026          <- date of the unloading
    polo 1              <- site
    ZHONGCE CINA        <- supplier (and sometimes a carrier line before it)
    wsl                 <- brand code
    973                 <- pieces (the first plain number of the record)
    7  / 4S / 0 / 0 / 973 / 6:00 ...

Counting is done by code, never by a model: number of arrivals per site and the pieces.
"navette out 16" (outgoing shuttles) is taken with a regular expression.
A later programme for the same date replaces the earlier one (metrics are per date+site+name).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any

DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")
SITE = re.compile(r"^\s*polo\s*([123])\s*$", re.I)
PIECES = re.compile(r"^\s*(\d{1,3}(?:\.\d{3})+|\d{1,5})\s*$")
WEEKDAY = re.compile(r"^\s*(lun|mar|mer|gio|ven|sab|dom)\s*$", re.I)
NAVETTE = re.compile(r"navette\s+out\s*:?\s*(\d{1,3})", re.I)
TIME = re.compile(r"^\s*(\d{1,2}[:.]\d{2}|mattina|pomeriggio|sera)\s*$", re.I)


def _body_lines(text: str) -> list[str]:
    # replies quote the programme with "> "; take the original only once
    return [l for l in (text or "").replace("\r", "").split("\n") if not l.lstrip().startswith(">")]


def intro_text(text: str) -> str:
    """Words before the first record (greeting, remarks such as cancellations)."""
    lines = _body_lines(text)
    out = []
    for l in lines:
        if DATE.match(l):
            break
        out.append(l.strip())
    return " ".join(x for x in out if x)


def parse_programme(text: str) -> dict[str, Any] | None:
    """{"days": {"2026-11-26": {"polo1": {"arrivals": 7, "pieces": 7582}, ...}},
        "navette_out": 16 | None}  or None when the text is not an unloading programme."""
    lines = _body_lines(text)
    records: list[tuple[str, str, list[str]]] = []
    i = 0
    while i < len(lines):
        m = DATE.match(lines[i])
        if m and i + 1 < len(lines):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            s = SITE.match(lines[j]) if j < len(lines) else None
            if s:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                try:
                    day = date(y, mo, d).isoformat()
                except ValueError:
                    i += 1
                    continue
                body: list[str] = []
                k = j + 1
                while k < len(lines) and not DATE.match(lines[k]) and not WEEKDAY.match(lines[k]):
                    if lines[k].strip():
                        body.append(lines[k].strip())
                    k += 1
                records.append((day, f"polo{s.group(1)}", body))
                i = k
                continue
        i += 1
    real = [r for r in records if r[2]]          # a date+site with nothing after it is an empty row
    if len(real) < 2:
        return None
    days: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"arrivals": 0, "pieces": 0}))
    for day, site, body in real:
        cell = days[day][site]
        cell["arrivals"] += 1
        for line in body:
            if TIME.match(line):
                break
            p = PIECES.match(line)
            if p:
                cell["pieces"] += int(p.group(1).replace(".", ""))
                break
    nav = NAVETTE.search("\n".join(lines))
    return {"days": {d: dict(v) for d, v in days.items()},
            "navette_out": int(nav.group(1)) if nav else None}


def metric_actions(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for day, sites in parsed["days"].items():
        tot_a = sum(c["arrivals"] for c in sites.values())
        tot_p = sum(c["pieces"] for c in sites.values())
        for site in ("polo1", "polo2", "polo3"):
            c = sites.get(site, {"arrivals": 0, "pieces": 0})
            out.append({"type": "record_metric", "date": day, "site": site, "name": "trucks_unload",
                        "value": c["arrivals"], "unit": "машин"})
            out.append({"type": "record_metric", "date": day, "site": site, "name": "unload_pieces",
                        "value": c["pieces"], "unit": "шт"})
        out.append({"type": "record_metric", "date": day, "site": "all", "name": "trucks_unload",
                    "value": tot_a, "unit": "машин"})
        out.append({"type": "record_metric", "date": day, "site": "all", "name": "unload_pieces",
                    "value": tot_p, "unit": "шт"})
    if parsed.get("navette_out") is not None and parsed["days"]:
        first = min(parsed["days"])
        out.append({"type": "record_metric", "date": first, "site": "all", "name": "navette_out",
                    "value": parsed["navette_out"], "unit": "навет"})
    return out


def summary(parsed: dict[str, Any], who: str) -> str:
    parts = []
    for day in sorted(parsed["days"]):
        sites = parsed["days"][day]
        tot = sum(c["arrivals"] for c in sites.values())
        detail = ", ".join(f"Polo {s[-1]}: {c['arrivals']} ({c['pieces']} шт.)"
                           for s, c in sorted(sites.items()))
        parts.append(f"{day[8:10]}.{day[5:7]}: {tot} розвантажень — {detail}")
    nav = f"; navette out {parsed['navette_out']}" if parsed.get("navette_out") is not None else ""
    return f"Програма розвантажень від {who}. " + "; ".join(parts) + nav


def record(store: Any, external_id: str, text: str, who: str, received_at: str | None = None
           ) -> dict[str, Any] | None:
    """Store the programme's numbers once per e-mail (source 'programma')."""
    parsed = parse_programme(text)
    if parsed is None:
        return None
    inbox_id, _ = store.ingest("programma", external_id, summary(parsed, who), received_at=received_at)
    return store.apply(inbox_id, metric_actions(parsed), actor="system") | {"inbox_id": inbox_id,
                                                                             "parsed": parsed}


def backfill_mail(db: str, gmail: Any, since: str, chunk: int = 8) -> dict[str, int]:
    """Read old Fintyre programmes (read only) and store their numbers. Safe to repeat."""
    from .core import Store
    from .mail import _names, msg_time, outgoing
    threads = gmail.search(f"from:fintyre.it after:{since} (programma OR volumi OR scarichi OR navette)")
    ids = [t["id"] for t in threads]
    found = stored = 0
    for i in range(0, len(ids), chunk):
        full = gmail.fetch(ids[i:i + chunk])
        store = Store(db)
        try:
            for t in full.values():
                for m in t.get("messages") or []:
                    if outgoing(m):
                        continue
                    body = m.get("plaintextBody") or ""
                    if parse_programme(body) is None:
                        continue
                    found += 1
                    who = _names(m).get(m.get("sender", "").lower()) or m.get("sender", "")
                    r = record(store, m["id"], body, who,
                               received_at=msg_time(m).strftime("%Y-%m-%dT%H:%M:%SZ"))
                    if r and r.get("status") == "applied":
                        stored += 1
        finally:
            store.close()
    return {"threads": len(ids), "programmes": found, "stored": stored}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import logging
    import os
    from pathlib import Path
    from .mail import ClaudeCodeGmail, load_config
    p = argparse.ArgumentParser(prog="atlas-volumes")
    p.add_argument("--db", default=os.environ.get("ATLAS_DB", "~/Atlas/atlas.db"))
    p.add_argument("--since", default="2025/12/01", help="Gmail date YYYY/MM/DD")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config() or {}
    gmail = ClaudeCodeGmail(prefix=cfg.get("tool_prefix", "mcp__claude_ai_Gmail__"))
    print(json.dumps(backfill_mail(str(Path(args.db).expanduser()), gmail, args.since)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
