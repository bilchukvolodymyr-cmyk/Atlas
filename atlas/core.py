"""Atlas core: durable, idempotent storage of loops, entries, ideas and metrics.

Rules enforced here (not by the LLM):
  * every incoming message is stored in `inbox` before any processing;
  * all changes caused by one message are applied in ONE transaction, together
    with their journal events and the inbox status -> partial writes are impossible;
  * re-applying the same message is a no-op (inbox status + UNIQUE(inbox_id, seq));
  * loop state changes must follow ALLOWED_TRANSITIONS; `closed` is reachable only
    through verify_loop from `verifying`;
  * the event journal is append-only and is enough to rebuild current state.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "1"

LOOP_STATES = ("needs_info", "active", "waiting", "done_claimed", "verifying", "closed", "dropped")
INITIAL_LOOP_STATES = ("needs_info", "active", "waiting")
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "needs_info": frozenset({"active", "waiting", "dropped"}),
    "active": frozenset({"needs_info", "waiting", "done_claimed", "dropped"}),
    "waiting": frozenset({"active", "done_claimed", "dropped"}),
    "done_claimed": frozenset({"verifying", "active", "dropped"}),
    "verifying": frozenset({"active", "dropped"}),  # -> closed only via verify_loop
    "closed": frozenset({"active"}),                # reopen
    "dropped": frozenset({"active"}),               # reopen
}

LOOP_FIELDS = ("kind", "title", "desired_outcome", "next_action", "waiting_on", "assigned_by",
               "due_at", "remind_at", "importance", "recurrence", "done_criteria")
LOOP_UPDATABLE = tuple(f for f in LOOP_FIELDS if f != "kind")
IDEA_UPDATABLE = ("title", "description", "status", "snoozed_until")

# object_type -> (table, columns restored by rebuild)
STATE_TABLES = {
    "loop": "loops",
    "idea": "ideas",
    "entry": "entries",
    "metric": "metrics",
    "question": "questions",
}


class AtlasError(Exception):
    """Base error."""


class ValidationError(AtlasError):
    """An action is malformed or refers to something that does not exist."""


class TransitionError(AtlasError):
    """A loop state change is not allowed."""


class ConflictError(AtlasError):
    """Optimistic lock failed: the object was changed by someone else."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Store:
    def __init__(self, path: str | os.PathLike, clock: Callable[[], str] = utc_now):
        self.path = str(path)
        self.clock = clock
        # isolation_level=None: we issue BEGIN/COMMIT ourselves.
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                          (SCHEMA_VERSION,))

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ intake
    def ingest(self, source: str, external_id: str, text: str,
               received_at: str | None = None) -> tuple[int, bool]:
        """Store a raw message. Returns (inbox_id, created). Repeated delivery -> created=False."""
        if not source or not external_id:
            raise ValidationError("source and external_id are required")
        if text is None or not str(text).strip():
            raise ValidationError("empty message")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO inbox(source, external_id, received_at, raw_text) VALUES (?,?,?,?)",
            (source, external_id, received_at or self.clock(), text),
        )
        created = cur.rowcount == 1
        row = self.conn.execute("SELECT id FROM inbox WHERE source=? AND external_id=?",
                                (source, external_id)).fetchone()
        return row["id"], created

    def pending_inbox(self, max_attempts: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM inbox WHERE status IN ('new','failed') AND attempts < ? ORDER BY id",
            (max_attempts,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ apply
    def apply(self, inbox_id: int, actions: Iterable[dict[str, Any]],
              actor: str = "user") -> dict[str, Any]:
        """Apply all actions for one inbox message atomically.

        Returns {"status": "applied", "created": {...refs...}, "events": n}
        or {"status": "already_applied"} when the message was processed before.
        On any error nothing is written except inbox.status='failed' + error text.
        """
        actions = list(actions)
        if actor not in ("user", "llm", "system"):
            raise ValidationError(f"bad actor {actor!r}")
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            inbox = c.execute("SELECT * FROM inbox WHERE id=?", (inbox_id,)).fetchone()
            if inbox is None:
                raise ValidationError(f"inbox {inbox_id} not found")
            if inbox["status"] == "done":
                c.execute("ROLLBACK")
                return {"status": "already_applied"}
            ctx = _ApplyContext(self, inbox, actor)
            for i, action in enumerate(actions):
                ctx.run(i, action)
            c.execute("UPDATE inbox SET status='done', attempts=attempts+1, error=NULL WHERE id=?",
                      (inbox_id,))
            delay = os.environ.get("ATLAS_TEST_SLEEP_IN_TX")
            if delay:  # test hook: lets a test kill the process inside the transaction
                time.sleep(float(delay))
            c.execute("COMMIT")
            return {"status": "applied", "created": ctx.refs, "events": ctx.seq}
        except Exception as exc:
            c.execute("ROLLBACK")
            self._mark_failed(inbox_id, exc)
            raise

    def _mark_failed(self, inbox_id: int, exc: Exception) -> None:
        self.conn.execute(
            "UPDATE inbox SET status='failed', attempts=attempts+1, error=? WHERE id=? AND status!='done'",
            (f"{type(exc).__name__}: {exc}", inbox_id))

    # ------------------------------------------------------------------ reads
    def loop(self, loop_id: int) -> dict[str, Any] | None:
        return _row(self.conn.execute("SELECT * FROM loops WHERE id=?", (loop_id,)).fetchone())

    def open_loops(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM loops WHERE state NOT IN ('closed','dropped') "
            "ORDER BY due_at IS NULL, due_at, id").fetchall()
        return [dict(r) for r in rows]

    def history(self, object_type: str, object_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT e.*, i.raw_text AS source_text FROM events e JOIN inbox i ON i.id=e.inbox_id "
            "WHERE e.object_type=? AND e.object_id=? ORDER BY e.id", (object_type, object_id)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ rebuild
    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {t: [dict(r) for r in self.conn.execute(f"SELECT * FROM {t} ORDER BY id")]
                for t in STATE_TABLES.values()}

    def rebuild(self) -> dict[str, int]:
        """Recreate all state tables from the event journal. Returns row counts."""
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            latest: dict[tuple[str, int], dict[str, Any] | None] = {}
            for r in c.execute("SELECT object_type, object_id, payload FROM events ORDER BY id"):
                payload = json.loads(r["payload"])
                if "after" in payload:
                    latest[(r["object_type"], r["object_id"])] = payload["after"]
            for table in STATE_TABLES.values():
                c.execute(f"DELETE FROM {table}")
            counts = {t: 0 for t in STATE_TABLES.values()}
            # insert in id order so ids and ordering are identical
            for (otype, _oid), row in sorted(latest.items(), key=lambda kv: (kv[0][0], kv[0][1])):
                if row is None:
                    continue
                table = STATE_TABLES[otype]
                cols = list(row.keys())
                c.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          [row[k] for k in cols])
                counts[table] += 1
            c.execute("COMMIT")
            return counts
        except Exception:
            c.execute("ROLLBACK")
            raise

    def integrity_ok(self) -> bool:
        return self.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    def backup(self, dest: str | os.PathLike) -> None:
        """Consistent copy of the live database (safe while running)."""
        self.conn.execute("VACUUM INTO ?", (str(dest),))


class _ApplyContext:
    """Executes actions inside an open transaction and writes journal events."""

    def __init__(self, store: Store, inbox: sqlite3.Row, actor: str):
        self.s = store
        self.c = store.conn
        self.inbox = inbox
        self.actor = actor
        self.seq = 0
        self.refs: dict[str, dict[str, Any]] = {}
        self.now = store.clock()

    # -- helpers
    def _event(self, object_type: str, object_id: int, etype: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        self.c.execute(
            "INSERT INTO events(inbox_id, seq, object_type, object_id, type, payload, actor, at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (self.inbox["id"], self.seq, object_type, object_id, etype,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), self.actor, self.now))

    def _get(self, table: str, obj_id: int) -> dict[str, Any]:
        row = self.c.execute(f"SELECT * FROM {table} WHERE id=?", (obj_id,)).fetchone()
        if row is None:
            raise ValidationError(f"{table} id={obj_id} not found")
        return dict(row)

    def _resolve_id(self, value: Any, expected_type: str) -> int:
        """Accept an integer id or "$ref" pointing to an object created earlier in this message."""
        if isinstance(value, str) and value.startswith("$"):
            ref = self.refs.get(value[1:])
            if ref is None or ref["type"] != expected_type:
                raise ValidationError(f"unknown reference {value!r}")
            return ref["id"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"bad {expected_type} id {value!r}")
        return value

    def _remember(self, action: dict[str, Any], otype: str, oid: int) -> None:
        ref = action.get("ref")
        if ref:
            if ref in self.refs:
                raise ValidationError(f"duplicate ref {ref!r}")
            self.refs[ref] = {"type": otype, "id": oid}

    @staticmethod
    def _require(action: dict[str, Any], *keys: str) -> None:
        missing = [k for k in keys if action.get(k) in (None, "")]
        if missing:
            raise ValidationError(f"{action.get('type')}: missing {', '.join(missing)}")

    @staticmethod
    def _only(fields: dict[str, Any], allowed: Iterable[str], what: str) -> None:
        extra = set(fields) - set(allowed)
        if extra:
            raise ValidationError(f"{what}: unknown fields {sorted(extra)}")

    # -- dispatcher
    def run(self, index: int, action: dict[str, Any]) -> None:
        if not isinstance(action, dict) or "type" not in action:
            raise ValidationError(f"action #{index} has no type")
        handler = getattr(self, f"do_{action['type']}", None)
        if handler is None:
            raise ValidationError(f"action #{index}: unknown type {action['type']!r}")
        handler(action)

    # -- loops
    def do_create_loop(self, a: dict[str, Any]) -> None:
        self._require(a, "kind", "title")
        fields = {k: a.get(k) for k in LOOP_FIELDS}
        self._only({k: v for k, v in a.items() if k not in ("type", "ref", "state")},
                   LOOP_FIELDS, "create_loop")
        state = a.get("state", "active")
        if state not in INITIAL_LOOP_STATES:
            raise TransitionError(f"a new loop cannot start in state {state!r}")
        cur = self.c.execute(
            f"INSERT INTO loops({','.join(LOOP_FIELDS)}, state, created_from, updated_at) "
            f"VALUES ({','.join('?' * len(LOOP_FIELDS))}, ?, ?, ?)",
            [fields[k] for k in LOOP_FIELDS] + [state, self.inbox["id"], self.now])
        loop = self._get("loops", cur.lastrowid)
        self._event("loop", loop["id"], "created", {"before": None, "after": loop})
        self._remember(a, "loop", loop["id"])

    def _write_loop(self, before: dict[str, Any], changes: dict[str, Any],
                    expected_version: int | None) -> dict[str, Any]:
        version = before["version"] if expected_version is None else expected_version
        sets = ", ".join(f"{k}=?" for k in changes)
        cur = self.c.execute(
            f"UPDATE loops SET {sets}, version=version+1, updated_at=? WHERE id=? AND version=?",
            [*changes.values(), self.now, before["id"], version])
        if cur.rowcount != 1:
            raise ConflictError(f"loop {before['id']} changed (expected version {version})")
        return self._get("loops", before["id"])

    def do_update_loop(self, a: dict[str, Any]) -> None:
        self._require(a, "id", "fields")
        loop_id = self._resolve_id(a["id"], "loop")
        fields = a["fields"]
        if not isinstance(fields, dict) or not fields:
            raise ValidationError("update_loop: fields must be a non-empty object")
        self._only(fields, LOOP_UPDATABLE, "update_loop")
        before = self._get("loops", loop_id)
        after = self._write_loop(before, fields, a.get("expected_version"))
        self._event("loop", loop_id, "updated", {"before": before, "after": after})

    def do_transition_loop(self, a: dict[str, Any]) -> None:
        self._require(a, "id", "to")
        loop_id = self._resolve_id(a["id"], "loop")
        to = a["to"]
        if to not in LOOP_STATES:
            raise ValidationError(f"unknown state {to!r}")
        before = self._get("loops", loop_id)
        frm = before["state"]
        if to == "closed":
            raise TransitionError("a loop is closed only by verify_loop")
        if to not in ALLOWED_TRANSITIONS[frm]:
            raise TransitionError(f"loop {loop_id}: {frm} -> {to} is not allowed")
        evidence = a.get("evidence")
        if to == "done_claimed" and not evidence:
            # the user's own message is the minimum evidence; it is always stored
            evidence = self.inbox["raw_text"]
        if evidence:
            self._event("loop", loop_id, "evidence_added",
                        {"evidence": evidence, "inbox_id": self.inbox["id"]})
        after = self._write_loop(before, {"state": to}, a.get("expected_version"))
        self._event("loop", loop_id, "state_changed",
                    {"before": before, "after": after, "from": frm, "to": to,
                     "reason": a.get("reason")})

    def do_verify_loop(self, a: dict[str, Any]) -> None:
        self._require(a, "id", "evidence")
        loop_id = self._resolve_id(a["id"], "loop")
        before = self._get("loops", loop_id)
        if before["state"] != "verifying":
            raise TransitionError(f"loop {loop_id}: verify requires state 'verifying', "
                                  f"not {before['state']!r}")
        self._event("loop", loop_id, "verified", {"evidence": a["evidence"],
                                                  "inbox_id": self.inbox["id"]})
        after = self._write_loop(before, {"state": "closed"}, a.get("expected_version"))
        self._event("loop", loop_id, "state_changed",
                    {"before": before, "after": after, "from": "verifying", "to": "closed"})

    # -- entries / ideas
    def do_create_entry(self, a: dict[str, Any]) -> None:
        self._require(a, "kind", "text")
        self._only({k: v for k, v in a.items() if k not in ("type", "ref")},
                   ("kind", "text", "happened_at", "summary"), "create_entry")
        cur = self.c.execute(
            "INSERT INTO entries(inbox_id, happened_at, kind, text, summary) VALUES (?,?,?,?,?)",
            (self.inbox["id"], a.get("happened_at"), a["kind"], a["text"], a.get("summary")))
        entry = self._get("entries", cur.lastrowid)
        self._event("entry", entry["id"], "created", {"before": None, "after": entry})
        self._remember(a, "entry", entry["id"])

    def do_create_idea(self, a: dict[str, Any]) -> None:
        self._require(a, "title")
        self._only({k: v for k, v in a.items() if k not in ("type", "ref")},
                   ("title", "description"), "create_idea")
        cur = self.c.execute(
            "INSERT INTO ideas(title, description, created_from, last_touched_at) VALUES (?,?,?,?)",
            (a["title"], a.get("description"), self.inbox["id"], self.now))
        idea = self._get("ideas", cur.lastrowid)
        self._event("idea", idea["id"], "created", {"before": None, "after": idea})
        self._remember(a, "idea", idea["id"])

    def do_update_idea(self, a: dict[str, Any]) -> None:
        self._require(a, "id", "fields")
        idea_id = self._resolve_id(a["id"], "idea")
        fields = a["fields"]
        if not isinstance(fields, dict) or not fields:
            raise ValidationError("update_idea: fields must be a non-empty object")
        self._only(fields, IDEA_UPDATABLE, "update_idea")
        before = self._get("ideas", idea_id)
        version = a.get("expected_version", before["version"])
        sets = ", ".join(f"{k}=?" for k in fields)
        cur = self.c.execute(
            f"UPDATE ideas SET {sets}, last_touched_at=?, version=version+1 WHERE id=? AND version=?",
            [*fields.values(), self.now, idea_id, version])
        if cur.rowcount != 1:
            raise ConflictError(f"idea {idea_id} changed (expected version {version})")
        after = self._get("ideas", idea_id)
        self._event("idea", idea_id, "updated", {"before": before, "after": after})

    # -- metrics
    def do_record_metric(self, a: dict[str, Any]) -> None:
        self._require(a, "date", "name", "value")
        self._only({k: v for k, v in a.items() if k != "type"},
                   ("date", "site", "name", "value", "unit"), "record_metric")
        try:
            datetime.strptime(a["date"], "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"record_metric: bad date {a['date']!r}") from exc
        if isinstance(a["value"], bool) or not isinstance(a["value"], (int, float)):
            raise ValidationError(f"record_metric: value must be a number, got {a['value']!r}")
        site = a.get("site") or "all"
        before = _row(self.c.execute("SELECT * FROM metrics WHERE date=? AND site=? AND name=?",
                                     (a["date"], site, a["name"])).fetchone())
        if before is None:
            cur = self.c.execute(
                "INSERT INTO metrics(date, site, name, value, unit, inbox_id) VALUES (?,?,?,?,?,?)",
                (a["date"], site, a["name"], a["value"], a.get("unit"), self.inbox["id"]))
            mid, etype = cur.lastrowid, "created"
        else:
            self.c.execute("UPDATE metrics SET value=?, unit=?, inbox_id=? WHERE id=?",
                           (a["value"], a.get("unit", before["unit"]), self.inbox["id"], before["id"]))
            mid, etype = before["id"], "updated"
        after = self._get("metrics", mid)
        self._event("metric", mid, etype, {"before": before, "after": after})

    # -- questions
    def do_ask_question(self, a: dict[str, Any]) -> None:
        self._require(a, "text")
        otype = a.get("object_type")
        oid = None
        if otype is not None:
            if otype not in STATE_TABLES:
                raise ValidationError(f"ask_question: bad object_type {otype!r}")
            oid = self._resolve_id(a.get("object_id"), otype)
            self._get(STATE_TABLES[otype], oid)
        cur = self.c.execute(
            "INSERT INTO questions(object_type, object_id, inbox_id, text, missing_field, asked_at) "
            "VALUES (?,?,?,?,?,?)",
            (otype, oid, self.inbox["id"], a["text"], a.get("missing_field"), self.now))
        q = self._get("questions", cur.lastrowid)
        self._event("question", q["id"], "asked", {"before": None, "after": q})
        self._remember(a, "question", q["id"])

    def do_answer_question(self, a: dict[str, Any]) -> None:
        self._require(a, "id")
        qid = self._resolve_id(a["id"], "question")
        before = self._get("questions", qid)
        if before["status"] != "open":
            raise ValidationError(f"question {qid} is {before['status']}")
        self.c.execute("UPDATE questions SET status='answered' WHERE id=?", (qid,))
        after = self._get("questions", qid)
        self._event("question", qid, "answered",
                    {"before": before, "after": after, "answer_inbox_id": self.inbox["id"]})
