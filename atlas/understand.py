"""Stage 1: understand a message with Claude, then check everything Claude said with code.

Claude never writes to the database. It returns *items* (what it thinks the message says,
each with a verbatim quote). `translate()` checks every item and turns the valid ones into
core actions for `Store.apply()`:

  * the quote must really be in the message -> otherwise the item is rejected;
  * names (assigned_by / waiting_on) must appear in the message -> otherwise dropped;
  * a metric value must appear as digits in its quote -> otherwise rejected + question;
  * dates are expressions ("tomorrow", "weekday:sat"), resolved here, never by Claude;
  * "done" only moves a loop to verifying + asks for confirmation; closing needs a later
    confirmation (confirm_done) while the loop is already waiting for it.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from . import dates

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ITEM_TYPES = ["new_loop", "update_loop", "change_state", "confirm_done", "note", "idea",
              "update_idea", "metric", "ask", "answer"]
LOOP_KINDS = ["task", "reminder", "waiting_for", "agreement"]
ENTRY_KINDS = ["event", "note", "decision", "problem"]
STATES = ["active", "waiting", "needs_info", "done_claimed", "dropped"]
IDEA_STATUSES = ["new", "exploring", "planned", "in_work", "done", "dropped"]
DEFAULT_METRICS = ["trucks_unload", "trucks_load", "workers_total", "workers_agenzia",
                   "workers_cooperativa"]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


_STR = {"type": "string"}
_ITEM_PROPS: dict[str, Any] = {
    "type": {"type": "string", "enum": ITEM_TYPES},
    "quote": {"type": "string", "description": "exact words copied from the message"},
    "ref": _nullable(_STR),
    "loop_id": _nullable({"type": "integer"}),
    "idea_id": _nullable({"type": "integer"}),
    "question_id": _nullable({"type": "integer"}),
    "kind": _nullable({"type": "string", "enum": LOOP_KINDS + ENTRY_KINDS}),
    "title": _nullable(_STR),
    "text": _nullable(_STR),
    "state": _nullable({"type": "string", "enum": STATES}),
    "idea_status": _nullable({"type": "string", "enum": IDEA_STATUSES}),
    "due": _nullable(_STR),
    "happened": _nullable(_STR),
    "assigned_by": _nullable(_STR),
    "waiting_on": _nullable(_STR),
    "next_action": _nullable(_STR),
    "importance": _nullable({"type": "string", "enum": ["low", "normal", "high"]}),
    "metric": _nullable(_STR),
    "value": _nullable({"type": "number"}),
    "site": _nullable(_STR),
    "about": _nullable(_STR),
    "missing_field": _nullable(_STR),
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object", "properties": _ITEM_PROPS,
                      "required": list(_ITEM_PROPS), "additionalProperties": False},
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are the parser of Atlas, the work memory of a warehouse shift coordinator in Italy \
(Polo 1 / Polo 2). He dictates short messages from his iPhone, mostly in Ukrainian, sometimes \
Italian or Russian, often with dictation errors. Your job is to say what the message contains; \
code will check your answer and write it to the database. You never invent anything.

Return items. Every item has `quote`: words copied exactly, character for character, from the \
message (not translated, not corrected) that support the item. If you cannot quote it, do not \
emit it. Fields you do not know are null. Write titles and text in the language of the message.

Item types:
- new_loop: something that must get done or is awaited. kind: task (he must do it), reminder, \
waiting_for (someone else must do/answer; set waiting_on), agreement (a promise between people). \
title = short imperative outcome. assigned_by = the person who gave the task, copied exactly as \
written in the message. Give `ref` like "L1" so other items can point at it as "$L1".
- update_loop: new facts about an existing open loop (loop_id from the list below).
- change_state: loop_id + state. "зробив / готово / fatto" about an open loop -> done_claimed. \
Waiting for someone -> waiting. Cancelled / not needed -> dropped. Missing essential info -> needs_info.
- confirm_done: ONLY when the message confirms that a loop which is already in state \
done_claimed or verifying is really finished (e.g. answers "так" to the confirmation question).
- note: something that happened, a decision, a problem. kind: event | note | decision | problem.
- idea: a new idea (title, text). update_idea: idea_id + idea_status or new text.
- metric: a daily number. metric = snake_case English name (known names listed below; reuse \
them), value = the number exactly as said, site = "polo1" | "polo2" | null (null = whole warehouse).
- ask: one short question when something essential is missing or ambiguous (who, by when, \
which loop). about = "$L1" or "loop:12". missing_field = the field name. Do not ask about \
things that are merely optional.
- answer: question_id of an open question that this message answers (plus the item that uses \
the answer, e.g. update_loop).

Dates: never compute dates. Put an expression in `due` / `happened`:
today | tomorrow | day_after_tomorrow | weekday:mon..sun | this_week | next_week | end_of_month \
| in_days:N | in_weeks:N | date:YYYY-MM-DD | date:DD.MM | day:DD, optionally with "@HH:MM".
"до кінця місяця" -> end_of_month, "в суботу" -> weekday:sat, "на наступний тиждень" -> next_week.

Dictation errors are common. Warehouse vocabulary: рампа / ribalta (loading dock, e.g. "рампа 31", "Ribalta 37"), локація / location codes like 3Q.15.004.01, 2T54, 2O 06, 2F08, 2V30, 1B.75, Polo 1 / Polo 2, Agenzia, Cooperativa, CrossChex, етикетки, палети, навантажувач, ретрак, Roberto Poli. When a word looks like a dictation error (e.g. "риба 31" where "рампа 31" is likely), never silently correct it and never record nonsense as a fact: emit an `ask` item ("Ти мав на увазі «рампа 31»?") and, if the rest is clear, the item with the doubtful words kept exactly as dictated. If the whole message is unintelligible, emit only one `ask`.

One message may contain several items (e.g. three tasks from the same manager, or numbers \
for trucks and workers). Do not duplicate an open loop that already exists: update it instead. \
Greetings, filler and thanks produce no items."""


class Client(Protocol):  # the subset of anthropic.Anthropic we use (lets tests inject a fake)
    beta: Any


class ParseError(Exception):
    """Claude could not give a usable answer."""

    def __init__(self, status: str, message: str, raw: Any = None, request_id: str | None = None):
        super().__init__(message)
        self.status, self.raw, self.request_id = status, raw, request_id


@dataclass
class ParseResult:
    items: list[dict[str, Any]]
    model: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] | None = None


def build_context(store: Any, now: datetime) -> str:
    local = now.astimezone(dates.TZ)
    loops = [{k: l[k] for k in ("id", "kind", "title", "state", "due_at", "assigned_by",
                                "waiting_on") if l[k] is not None}
             for l in store.open_loops()]
    questions = [{"id": q["id"], "text": q["text"],
                  "about": f"{q['object_type']}:{q['object_id']}" if q["object_type"] else None}
                 for q in store.open_questions()]
    ideas = [{"id": i["id"], "title": i["title"], "status": i["status"]}
             for i in store.active_ideas()]
    metrics = sorted(set(DEFAULT_METRICS) | set(store.metric_names()))
    return (f"Now: {local:%Y-%m-%d %H:%M} ({local:%A}), Europe/Rome\n"
            f"Open loops: {json.dumps(loops, ensure_ascii=False)}\n"
            f"Open questions: {json.dumps(questions, ensure_ascii=False)}\n"
            f"Ideas: {json.dumps(ideas, ensure_ascii=False)}\n"
            f"Known metric names: {', '.join(metrics)}")


class ClaudeParser:
    def __init__(self, client: Client | None = None, model: str = MODEL, effort: str = "medium",
                 max_tokens: int = 8000):
        if client is None:
            import anthropic  # optional dependency, only needed for real calls
            client = anthropic.Anthropic()
        self.client, self.model, self.effort, self.max_tokens = client, model, effort, max_tokens

    def parse(self, text: str, context: str, feedback: str | None = None) -> ParseResult:
        content = f"<context>\n{context}\n</context>\n<message>\n{text}\n</message>"
        if feedback:
            content += f"\n<previous_attempt_error>\n{feedback}\n</previous_attempt_error>"
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            )
        except Exception as exc:  # network, rate limit, overload, 5xx: retried later
            raise ParseError("api_error", f"{type(exc).__name__}: {exc}") from exc
        request_id = getattr(resp, "_request_id", None) or getattr(resp, "id", None)
        if resp.stop_reason == "refusal":
            raise ParseError("refusal", "model refused", request_id=request_id)
        if resp.stop_reason == "max_tokens":
            raise ParseError("bad_output", "output cut by max_tokens", request_id=request_id)
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        try:
            data = json.loads(raw)
            items = data["items"]
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                raise TypeError("items is not a list of objects")
        except (ValueError, KeyError, TypeError) as exc:
            raise ParseError("bad_output", f"not valid JSON items: {exc}", raw, request_id) from exc
        usage = getattr(resp, "usage", None)
        usage_d = usage.model_dump() if hasattr(usage, "model_dump") else None
        return ParseResult(items, getattr(resp, "model", self.model), request_id, usage_d)


# ---------------------------------------------------------------------------- checking
def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[‘’ʼ`ʹ]", "'", s)
    s = s.replace("ё", "е")
    s = re.sub(r"[^\w']+", " ", s)   # punctuation, quotes and spacing do not matter
    return " ".join(s.split())


def in_message(fragment: str | None, message_norm: str) -> bool:
    if not fragment or not fragment.strip():
        return False
    frag = normalize(fragment)
    return bool(frag) and f" {frag} " in f" {message_norm} "   # whole words only


def _num_in(value: float, quote: str) -> bool:
    shown = str(int(value)) if float(value).is_integer() else str(value)
    variants = {shown, shown.replace(".", ",")}
    tokens = re.findall(r"\d+(?:[.,]\d+)?", quote)
    return any(t in variants for t in tokens)


@dataclass
class Translation:
    actions: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    dropped_fields: list[dict[str, Any]] = field(default_factory=list)

    def reject(self, item: dict[str, Any], why: str) -> None:
        self.rejected.append({"item": item, "why": why})

    def report(self) -> dict[str, Any]:
        return {"rejected": self.rejected, "dropped_fields": self.dropped_fields}


def translate(message: str, items: list[dict[str, Any]], now: datetime, store: Any) -> Translation:
    """Check Claude's items against the message and the current state; build core actions."""
    out = Translation()
    msg = normalize(message)
    loops = {l["id"]: l for l in store.open_loops()}
    questions = {q["id"]: q for q in store.open_questions()}
    ideas = {i["id"]: i for i in store.active_ideas(limit=10_000)}
    refs: dict[str, str] = {}  # ref -> object type, for objects created by this message
    q_counter = 0

    def ask(text: str, about: str | None, missing: str | None) -> None:
        nonlocal q_counter
        q_counter += 1
        action: dict[str, Any] = {"type": "ask_question", "text": text, "missing_field": missing,
                                  "ref": f"_q{q_counter}"}
        if about:
            otype, oid = _about(about, refs)
            action["object_type"], action["object_id"] = otype, oid
        out.actions.append(action)

    def resolve_due(item: dict[str, Any], key: str, about: str | None) -> dates.Due | None:
        expr = item.get(key)
        if not expr:
            return None
        try:
            return dates.resolve(expr, now)
        except dates.AmbiguousDate as exc:
            out.dropped_fields.append({"item": item, "field": key, "why": str(exc)})
            ask(f"Уточни дату для «{item.get('title') or item.get('quote')}»: {exc}", about, "due_at")
        except dates.DateError as exc:
            out.dropped_fields.append({"item": item, "field": key, "why": str(exc)})
        return None

    def checked_name(item: dict[str, Any], key: str) -> str | None:
        value = item.get(key)
        if value and not in_message(value, msg):
            out.dropped_fields.append({"item": item, "field": key,
                                       "why": f"{value!r} is not in the message"})
            return None
        return value or None

    for item in items:
        t = item.get("type")
        if t not in ITEM_TYPES:
            out.reject(item, f"unknown type {t!r}")
            continue
        if t != "ask" and not in_message(item.get("quote"), msg):
            out.reject(item, "quote is not in the message")
            continue
        quote = item.get("quote") or ""

        if t == "new_loop":
            if not item.get("title"):
                out.reject(item, "loop without title")
                continue
            ref = item.get("ref") or f"_L{len(refs) + 1}"
            if ref in refs:
                out.reject(item, f"duplicate ref {ref}")
                continue
            refs[ref] = "loop"
            kind = item.get("kind") if item.get("kind") in LOOP_KINDS else "task"
            action = {"type": "create_loop", "ref": ref, "kind": kind, "title": item["title"],
                      "done_criteria": "self"}
            for key in ("assigned_by", "waiting_on"):
                v = checked_name(item, key)
                if v:
                    action[key] = v
            for key in ("next_action", "importance"):
                if item.get(key):
                    action[key] = item[key]
            if action.get("waiting_on"):
                action["state"] = "waiting"
            out.actions.append(action)
            due = resolve_due(item, "due", f"${ref}")
            if due:
                action["due_at"] = due.iso()
                action["remind_at"] = dates.remind_at(due, now)

        elif t == "update_loop":
            loop = loops.get(item.get("loop_id"))
            if loop is None:
                out.reject(item, "no such open loop")
                continue
            fields: dict[str, Any] = {}
            for key in ("assigned_by", "waiting_on"):
                v = checked_name(item, key)
                if v:
                    fields[key] = v
            for key in ("title", "next_action", "importance"):
                if item.get(key):
                    fields[key] = item[key]
            due = resolve_due(item, "due", f"loop:{loop['id']}")
            if due:
                fields["due_at"] = due.iso()
                fields["remind_at"] = dates.remind_at(due, now)
            if fields:
                out.actions.append({"type": "update_loop", "id": loop["id"], "fields": fields})
            elif not any(d["item"] is item for d in out.dropped_fields):
                out.reject(item, "update without changes")

        elif t == "change_state":
            loop = loops.get(item.get("loop_id"))
            to = item.get("state")
            if loop is None or to not in STATES:
                out.reject(item, "no such open loop or bad state")
                continue
            if to == loop["state"]:
                out.reject(item, f"loop already {to}")
                continue
            if to == "done_claimed":
                if loop["state"] in ("done_claimed", "verifying"):
                    out.reject(item, "already waiting for confirmation")
                    continue
                out.actions += [
                    {"type": "transition_loop", "id": loop["id"], "to": "done_claimed",
                     "evidence": quote},
                    {"type": "transition_loop", "id": loop["id"], "to": "verifying"},
                ]
                ask(f"Підтверджуєш, що «{loop['title']}» виконано? «Так» — закрию справу.",
                    f"loop:{loop['id']}", "confirm_close")
            else:
                out.actions.append({"type": "transition_loop", "id": loop["id"], "to": to,
                                    "reason": quote})

        elif t == "confirm_done":
            loop = loops.get(item.get("loop_id"))
            if loop is None or loop["state"] not in ("done_claimed", "verifying"):
                out.reject(item, "loop is not waiting for confirmation")
                continue
            if loop["state"] == "done_claimed":
                out.actions.append({"type": "transition_loop", "id": loop["id"], "to": "verifying"})
            out.actions.append({"type": "verify_loop", "id": loop["id"], "evidence": quote})
            for q in questions.values():
                if (q["object_type"], q["object_id"], q["missing_field"]) == \
                        ("loop", loop["id"], "confirm_close"):
                    out.actions.append({"type": "answer_question", "id": q["id"]})

        elif t == "note":
            kind = item.get("kind") if item.get("kind") in ENTRY_KINDS else "note"
            action = {"type": "create_entry", "kind": kind, "text": item.get("text") or quote}
            happened = resolve_due_past(item, now, out)
            if happened:
                action["happened_at"] = happened
            out.actions.append(action)

        elif t == "idea":
            if not item.get("title"):
                out.reject(item, "idea without title")
                continue
            action = {"type": "create_idea", "title": item["title"]}
            if item.get("text"):
                action["description"] = item["text"]
            if item.get("ref") and item["ref"] not in refs:
                action["ref"] = item["ref"]
                refs[item["ref"]] = "idea"
            out.actions.append(action)

        elif t == "update_idea":
            if item.get("idea_id") not in ideas:
                out.reject(item, "no such idea")
                continue
            fields = {}
            if item.get("idea_status") in IDEA_STATUSES:
                fields["status"] = item["idea_status"]
            if item.get("text"):
                fields["description"] = item["text"]
            if item.get("title"):
                fields["title"] = item["title"]
            if not fields:
                out.reject(item, "update without changes")
                continue
            out.actions.append({"type": "update_idea", "id": item["idea_id"], "fields": fields})

        elif t == "metric":
            name = _metric_name(item.get("metric"))
            value = item.get("value")
            if not name or isinstance(value, bool) or not isinstance(value, (int, float)):
                out.reject(item, "metric without name or value")
                continue
            if not _num_in(value, quote):
                out.reject(item, f"value {value} is not in the quote")
                ask(f"Скільки саме «{name}»? Я не знайшов числа в повідомленні.", None, name)
                continue
            day = resolve_due_past(item, now, out) or now.astimezone(dates.TZ).date().isoformat()
            site = item.get("site") if item.get("site") in ("polo1", "polo2") else "all"
            out.actions.append({"type": "record_metric", "date": day[:10], "site": site,
                                "name": name, "value": value})

        elif t == "ask":
            text = item.get("text")
            about = item.get("about")
            if not text:
                out.reject(item, "question without text")
                continue
            if about and not _about_ok(about, refs, loops, ideas):
                about = None
            ask(text, about, item.get("missing_field"))

        elif t == "answer":
            if item.get("question_id") not in questions:
                out.reject(item, "no such open question")
                continue
            if not any(a["type"] == "answer_question" and a["id"] == item["question_id"]
                       for a in out.actions):
                out.actions.append({"type": "answer_question", "id": item["question_id"]})
    return out


def resolve_due_past(item: dict[str, Any], now: datetime, out: Translation) -> str | None:
    """`happened` may be in the past: today / yesterday / date:..; returns YYYY-MM-DD."""
    expr = (item.get("happened") or "").strip().lower()
    if not expr:
        return None
    today = now.astimezone(dates.TZ).date()
    simple = {"today": today, "yesterday": today - timedelta(days=1),
              "day_before_yesterday": today - timedelta(days=2)}
    if expr in simple:
        return simple[expr].isoformat()
    m = re.fullmatch(r"date:(\d{4}-\d{2}-\d{2})", expr)
    if m:
        try:
            d = datetime.strptime(m[1], "%Y-%m-%d").date()
            if d <= today:
                return d.isoformat()
        except ValueError:
            pass
    m = re.fullmatch(r"date:(\d{1,2})\.(\d{1,2})", expr)
    if m:
        try:
            d = today.replace(month=int(m[2]), day=int(m[1]))
            if d > today:
                d = d.replace(year=d.year - 1)
            return d.isoformat()
        except ValueError:
            pass
    out.dropped_fields.append({"item": item, "field": "happened", "why": f"bad {expr!r}"})
    return None


def _metric_name(name: str | None) -> str | None:
    if not name:
        return None
    n = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return n or None


def _about(about: str, refs: dict[str, str]) -> tuple[str, Any]:
    if about.startswith("$"):
        return refs[about[1:]], about
    otype, _, oid = about.partition(":")
    return otype, int(oid)


def _about_ok(about: str, refs: dict[str, str], loops: dict, ideas: dict) -> bool:
    if about.startswith("$"):
        return about[1:] in refs
    m = re.fullmatch(r"(loop|idea):(\d+)", about)
    if not m:
        return False
    return int(m[2]) in (loops if m[1] == "loop" else ideas)


class ClaudeCodeParser:
    """Same job as ClaudeParser, but through the Claude Code CLI (`claude -p`) logged in with
    the user's Claude subscription: no API key and no per-call payment. Uses the subscription's
    usage limits instead. `runner` is injectable for tests."""

    def __init__(self, model: str = "opus", timeout: float = 180.0, executable: str | None = None,
                 runner: Any = None):
        import shutil
        import subprocess
        self.model, self.timeout = model, timeout
        self.executable = executable or shutil.which("claude") or "claude"
        self.runner = runner or subprocess.run

    def command(self) -> list[str]:
        return [self.executable, "-p",
                "--output-format", "json",
                "--json-schema", json.dumps(OUTPUT_SCHEMA, ensure_ascii=False),
                "--system-prompt", SYSTEM_PROMPT,
                "--model", self.model,
                "--tools", "",                 # no file, shell or web access: text in, JSON out
                "--strict-mcp-config",          # no MCP servers
                "--setting-sources", "",        # ignore local settings, hooks and CLAUDE.md
                "--no-session-persistence"]

    def parse(self, text: str, context: str, feedback: str | None = None) -> ParseResult:
        content = f"<context>\n{context}\n</context>\n<message>\n{text}\n</message>"
        if feedback:
            content += f"\n<previous_attempt_error>\n{feedback}\n</previous_attempt_error>"
        content += "\nReturn only the JSON object with `items`."
        try:
            proc = self.runner(self.command(), input=content, capture_output=True, text=True,
                               encoding="utf-8", timeout=self.timeout)
        except Exception as exc:  # not installed, timeout, killed
            raise ParseError("api_error", f"claude CLI: {type(exc).__name__}: {exc}") from exc
        if proc.returncode != 0:
            raise ParseError("api_error", f"claude CLI exit {proc.returncode}: "
                                          f"{(proc.stderr or proc.stdout or '')[:300]}")
        try:
            envelope = json.loads(proc.stdout)
        except ValueError as exc:
            raise ParseError("bad_output", f"CLI output is not JSON: {exc}", proc.stdout[:2000]) from exc
        if envelope.get("is_error"):
            raise ParseError("api_error", f"claude CLI error: {str(envelope.get('result'))[:300]}")
        data = envelope.get("structured_output")
        if data is None:
            raw = envelope.get("result") or ""
            m = re.search(r"\{.*\}", raw, re.S)
            try:
                data = json.loads(m.group(0) if m else raw)
            except ValueError as exc:
                raise ParseError("bad_output", f"result is not JSON: {exc}", raw[:2000]) from exc
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ParseError("bad_output", "no items list", data)
        return ParseResult(items, f"claude-code:{self.model}", envelope.get("session_id"),
                           envelope.get("usage"))
