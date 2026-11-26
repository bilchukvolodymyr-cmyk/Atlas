"""Live evaluation of the Claude parser on real-style phrases (costs money: real API calls).

    ANTHROPIC_API_KEY=... python3 evals/run_eval.py [--effort medium] [--only id1,id2]

Every phrase runs in a fresh temporary database at the fixed moment
Thursday 2026-11-26 10:00 Europe/Rome, so expected dates are stable.
Grading is done by code (no LLM judge): loops / dates / names / metrics / states.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atlas.core import Store  # noqa: E402
from atlas.pipeline import process_message  # noqa: E402
from atlas.understand import ClaudeParser  # noqa: E402

NOW_UTC = "2026-11-26T09:00:00+00:00"


def check(store: Store, expect: dict, setup_inbox: int | None = None) -> list[str]:
    errs: list[str] = []
    q = lambda sql, *a: [dict(r) for r in store.conn.execute(sql, a)]
    loops = q("SELECT * FROM loops ORDER BY id")
    new_loops = [l for l in loops if l["created_from"] != setup_inbox]
    ideas = q("SELECT * FROM ideas ORDER BY id")
    metrics = q("SELECT * FROM metrics")
    entries = q("SELECT * FROM entries")
    open_q = store.open_questions()

    for want in expect.get("loops", []):
        def ok(l: dict) -> bool:
            if "title_has" in want and want["title_has"].lower() not in l["title"].lower():
                return False
            if "due_at" in want and l["due_at"] != want["due_at"]:
                return False
            for f in ("assigned_by", "waiting_on"):
                if f in want and l[f] != want[f]:
                    return False
                if f + "_has" in want and want[f + "_has"].lower() not in (l[f] or "").lower():
                    return False
            if "state" in want and l["state"] != want["state"]:
                return False
            return True
        if not any(ok(l) for l in new_loops):
            errs.append(f"no loop matching {want}; got "
                        f"{[(l['title'], l['due_at'], l['assigned_by'], l['state']) for l in new_loops]}")
    if "loops_max" in expect and len(new_loops) > expect["loops_max"]:
        errs.append(f"{len(new_loops)} loops created, expected <= {expect['loops_max']}")
    if "loops_min" in expect and len(new_loops) < expect["loops_min"]:
        errs.append(f"{len(new_loops)} loops created, expected >= {expect['loops_min']}")
    if "loops_total" in expect and len(loops) != expect["loops_total"]:
        errs.append(f"{len(loops)} loops in total, expected {expect['loops_total']} (duplicate?)")
    if "ideas" in expect and len([i for i in ideas if i["created_from"] != setup_inbox]) != expect["ideas"]:
        errs.append(f"{len(ideas)} ideas, expected {expect['ideas']}")
    if "ideas_total" in expect and len(ideas) != expect["ideas_total"]:
        errs.append(f"{len(ideas)} ideas in total, expected {expect['ideas_total']}")
    if "loops_or_ideas_min" in expect and len(new_loops) + len(ideas) < expect["loops_or_ideas_min"]:
        errs.append("nothing created")
    if expect.get("no_invented_due") and any(l["due_at"] for l in new_loops):
        errs.append("a deadline was invented")
    if "metrics" in expect:
        got = {m["name"]: m["value"] for m in metrics}
        if got != {k: float(v) for k, v in expect["metrics"].items()}:
            errs.append(f"metrics {got} != {expect['metrics']}")
    if "metrics_site" in expect:
        got = {m["site"]: m["value"] for m in metrics}
        if got != {k: float(v) for k, v in expect["metrics_site"].items()}:
            errs.append(f"metrics by site {got} != {expect['metrics_site']}")
    if "metrics_ok_or_question" in expect:
        name = expect["metrics_ok_or_question"]
        if not any(m["name"] == name and m["value"] == 10 for m in metrics) and not open_q:
            errs.append("metric neither recorded correctly nor asked")
    if "entries_min" in expect and len(entries) < expect["entries_min"]:
        errs.append(f"{len(entries)} diary entries, expected >= {expect['entries_min']}")
    if "questions_min" in expect and len(open_q) < expect["questions_min"]:
        errs.append(f"{len(open_q)} open questions, expected >= {expect['questions_min']}")
    if "open_questions_max" in expect and len(open_q) > expect["open_questions_max"]:
        errs.append(f"{len(open_q)} open questions left")
    for lid, state in expect.get("states", {}).items():
        l = store.loop(int(lid))
        if l["state"] != state:
            errs.append(f"loop {lid} is {l['state']}, expected {state}")
    if "closed" in expect:
        n = sum(1 for l in loops if l["state"] == "closed")
        if n != expect["closed"]:
            errs.append(f"{n} loops closed without confirmation")
    for lid, due in expect.get("loop_due", {}).items():
        if store.loop(int(lid))["due_at"] != due:
            errs.append(f"loop {lid} due {store.loop(int(lid))['due_at']}, expected {due}")
    if "due_any" in expect and not any((l["due_at"] or "").startswith(expect["due_any"])
                                       for l in new_loops):
        errs.append(f"no loop due {expect['due_any']}")
    for iid, statuses in expect.get("idea_status", {}).items():
        st = q("SELECT status FROM ideas WHERE id=?", int(iid))[0]["status"]
        if st not in statuses:
            errs.append(f"idea {iid} status {st}, expected one of {statuses}")
    return errs


def run_case(case: dict, parser: ClaudeParser) -> tuple[list[str], dict]:
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "eval.db", clock=lambda: NOW_UTC)
        try:
            sid = None
            if case.get("setup"):
                sid, _ = store.ingest("eval", "setup", "[setup]", received_at=NOW_UTC)
                store.apply(sid, case["setup"], actor="system")
            mid, _ = store.ingest("eval", case["id"], case["text"], received_at=NOW_UTC)
            out = process_message(store, parser, mid)
            runs = store.llm_runs(mid)
            errs = check(store, case["expect"], sid)
            if out["status"] not in ("applied",):
                errs.insert(0, f"pipeline status {out['status']}: {out.get('why') or out.get('error')}")
            return errs, {"out": out, "runs": runs}
        finally:
            store.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--log", default=str(ROOT / "evals" / "last_run.jsonl"))
    args = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set: the live eval needs real Claude calls.", file=sys.stderr)
        return 2
    cases = [json.loads(l) for l in (ROOT / "evals" / "phrases.jsonl").read_text("utf-8").splitlines()
             if l.strip()]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    parser = ClaudeParser(model=args.model, effort=args.effort)
    passed, tokens_in, tokens_out = 0, 0, 0
    with open(args.log, "w", encoding="utf-8") as log:
        for case in cases:
            errs, detail = run_case(case, parser)
            for r in detail["runs"]:
                u = json.loads(r["usage"] or "{}")
                tokens_in += (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                    + (u.get("cache_creation_input_tokens") or 0)
                tokens_out += u.get("output_tokens") or 0
            passed += not errs
            print(f"{'PASS' if not errs else 'FAIL'}  {case['id']}")
            for e in errs:
                print(f"      - {e}")
            log.write(json.dumps({"id": case["id"], "errors": errs, **detail},
                                 ensure_ascii=False, default=str) + "\n")
    print(f"\n{passed}/{len(cases)} passed; tokens in {tokens_in}, out {tokens_out}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
