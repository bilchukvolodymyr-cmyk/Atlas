"""Command-line interface for Atlas stage 0 (no LLM).

Every command is stored as an inbox message first. Pass --id <uuid> to make a retry
idempotent: the same --id never creates a second record.

Examples:
  atlas add "Сьогодні 10 машин на розвантаження"
  atlas loop new "Години за листопад" --due 2026-11-30 --assigned-by "Roberto Poli"
  atlas loop move 1 done_claimed --evidence "відправив у бухгалтерію"
  atlas loop move 1 verifying
  atlas loop verify 1 --evidence "бухгалтерія підтвердила"
  atlas list
  atlas show 1
  atlas metric 2026-11-26 trucks_unload 10
  atlas rebuild --check
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from .core import AtlasError, Store

DEFAULT_DB = os.environ.get("ATLAS_DB", "atlas.db")


def _store(args: argparse.Namespace) -> Store:
    return Store(args.db)


def _submit(store: Store, args: argparse.Namespace, text: str, actions: list[dict]) -> dict:
    inbox_id, created = store.ingest("cli", args.id or str(uuid.uuid4()), text)
    result = store.apply(inbox_id, actions, actor="user")
    result["inbox_id"] = inbox_id
    result["new_message"] = created
    return result


def cmd_add(store: Store, args: argparse.Namespace) -> dict:
    return _submit(store, args, args.text,
                   [{"type": "create_entry", "kind": args.kind, "text": args.text,
                     "happened_at": args.when}])


def cmd_loop_new(store: Store, args: argparse.Namespace) -> dict:
    action = {"type": "create_loop", "kind": args.kind, "title": args.title, "ref": "L"}
    for field in ("due_at", "assigned_by", "waiting_on", "importance", "done_criteria",
                  "next_action", "recurrence"):
        value = getattr(args, field)
        if value:
            action[field] = value
    if args.waiting_on:
        action["state"] = "waiting"
    return _submit(store, args, f"[cli] loop new: {args.title}", [action])


def cmd_loop_move(store: Store, args: argparse.Namespace) -> dict:
    action = {"type": "transition_loop", "id": args.loop_id, "to": args.to}
    if args.evidence:
        action["evidence"] = args.evidence
    text = args.evidence or f"[cli] loop {args.loop_id} -> {args.to}"
    return _submit(store, args, text, [action])


def cmd_loop_verify(store: Store, args: argparse.Namespace) -> dict:
    return _submit(store, args, args.evidence,
                   [{"type": "verify_loop", "id": args.loop_id, "evidence": args.evidence}])


def cmd_metric(store: Store, args: argparse.Namespace) -> dict:
    return _submit(store, args, f"[cli] metric {args.date} {args.site} {args.name}={args.value}",
                   [{"type": "record_metric", "date": args.date, "site": args.site,
                     "name": args.name, "value": args.value, "unit": args.unit}])


def cmd_list(store: Store, args: argparse.Namespace) -> list:
    return store.open_loops()


def cmd_show(store: Store, args: argparse.Namespace) -> dict:
    return {"loop": store.loop(args.loop_id), "history": store.history("loop", args.loop_id)}


def cmd_rebuild(store: Store, args: argparse.Namespace) -> dict:
    before = store.snapshot()
    counts = store.rebuild()
    same = store.snapshot() == before
    return {"counts": counts, "identical": same, "integrity_ok": store.integrity_ok()}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atlas", description="Atlas core (stage 0)")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite file (env ATLAS_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_id(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--id", help="idempotency key; same key = same message")
        return sp

    a = with_id(sub.add_parser("add", help="diary entry"))
    a.add_argument("text")
    a.add_argument("--kind", default="note", choices=["event", "note", "decision", "problem"])
    a.add_argument("--when", help="when it happened (ISO date/time)")
    a.set_defaults(func=cmd_add)

    loop = sub.add_parser("loop", help="open loops").add_subparsers(dest="loop_cmd", required=True)
    n = with_id(loop.add_parser("new"))
    n.add_argument("title")
    n.add_argument("--kind", default="task", choices=["task", "reminder", "waiting_for", "agreement"])
    n.add_argument("--due", dest="due_at")
    n.add_argument("--assigned-by", dest="assigned_by")
    n.add_argument("--waiting-on", dest="waiting_on")
    n.add_argument("--importance", choices=["low", "normal", "high"])
    n.add_argument("--done-criteria", dest="done_criteria", choices=["self", "artifact", "deferred"])
    n.add_argument("--next", dest="next_action")
    n.add_argument("--recurrence")
    n.set_defaults(func=cmd_loop_new)

    m = with_id(loop.add_parser("move"))
    m.add_argument("loop_id", type=int)
    m.add_argument("to")
    m.add_argument("--evidence")
    m.set_defaults(func=cmd_loop_move)

    v = with_id(loop.add_parser("verify"))
    v.add_argument("loop_id", type=int)
    v.add_argument("--evidence", required=True)
    v.set_defaults(func=cmd_loop_verify)

    mt = with_id(sub.add_parser("metric", help="daily number"))
    mt.add_argument("date")
    mt.add_argument("name")
    mt.add_argument("value", type=float)
    mt.add_argument("--site", default="all")
    mt.add_argument("--unit")
    mt.set_defaults(func=cmd_metric)

    sub.add_parser("list", help="open loops").set_defaults(func=cmd_list)
    s = sub.add_parser("show", help="loop with history")
    s.add_argument("loop_id", type=int)
    s.set_defaults(func=cmd_show)
    r = sub.add_parser("rebuild", help="rebuild state from the journal and compare")
    r.add_argument("--check", action="store_true", help="(default behaviour) report if identical")
    r.set_defaults(func=cmd_rebuild)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _store(args)
    try:
        out = args.func(store, args)
    except AtlasError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    finally:
        store.close()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
