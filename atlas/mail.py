"""Mail mode: Atlas reads the user's Gmail (READ ONLY) and writes down what matters.

    Gmail --(Claude Code + Gmail connector, read tools only)--> raw Gmail JSON
          --(Python: new? relevant? clean text)--> inbox (source 'gmail', id = Gmail message id)
          --(the usual pipeline)--> loops / notes / questions --> mirror to the Google Sheet

* Read only. The Claude Code call may use exactly two tools, search_threads and get_thread;
  every Gmail tool that sends, deletes, labels or drafts is explicitly forbidden. Nothing in
  the mailbox changes (not even "read" marks).
* Text is never retyped by a model. Python reads the Gmail tool results themselves from the
  CLI's event stream (stream-json), so the words stored are exactly the words in the email.
* One email = one inbox row: (source 'gmail', Gmail message id) is unique in atlas.db, so a
  second pass, a restart or a crash in the middle never makes a duplicate.
* Advertising, newsletters, codes and similar are not written into Atlas. Which threads were
  judged irrelevant, and why, is kept in ~/Atlas/mail.db (inspectable, never sent anywhere).
* Failure anywhere (network, limits, Gmail) -> nothing is marked as seen, the next pass retries.

Config ~/Atlas/mail.json: {"enabled": true, "interval_minutes": 15, "backfill_days": 7,
"tool_prefix": "mcp__claude_ai_Gmail__", "search_model": "haiku", "triage_model": "sonnet"}
Run:  python3 -m atlas.mail --db ~/Atlas/atlas.db            (loop)
      python3 -m atlas.mail --db ~/Atlas/atlas.db --once     (one pass)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .core import Store
from .pipeline import MAX_ATTEMPTS, process_message

log = logging.getLogger("atlas.mail")
SOURCE = "gmail"
PROGRAMME_DOMAINS = ("@fintyre.it",)
MAX_BODY = 4000
CONTEXT_CHARS = 300

READ_TOOLS = ("search_threads", "get_thread")
WRITE_TOOLS = ("send_message", "reply", "forward", "create_draft", "update_draft", "delete_draft",
               "trash_message", "trash_thread", "untrash_message", "untrash_thread",
               "label_message", "label_thread", "unlabel_message", "unlabel_thread",
               "update_message_labels", "mark_message_spam", "mark_thread_spam",
               "unmark_message_spam", "unmark_thread_spam", "create_label", "delete_label",
               "update_label", "apply_sensitive_message_label", "apply_sensitive_thread_label",
               "download_gmail_attachment")
BUILTIN_BLOCK = "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,Read,Glob,Grep"


class MailError(Exception):
    pass


# Connector tools are "deferred" in Claude Code: without loading the schema first the model
# sometimes guesses the arguments and the tool rejects them.
LOAD = ("First load the tool's schema with the ToolSearch tool, query \"select:{tool}\". "
        "Use exactly the argument names of that schema. ")


def retry(fn, attempts: int = 3):
    """A Claude Code call can fail by chance (a guessed argument, a busy connector): try again."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return fn()
        except MailError as exc:
            last = exc
            log.warning("retrying after: %s", str(exc)[:200])
    raise last  # type: ignore[misc]


# ------------------------------------------------------------------ text of one email
FORWARD = re.compile(r"^\s*(i|fw|fwd|tr|inoltro|wg)\s*:", re.I)
_REPLY_MARKERS = [
    # Gmail: "Il giorno gio 26 nov 2026 alle ore 14:23 Luigi <x@y> ha scritto:" (may wrap)
    re.compile(r"^[^\n]*\d{4}[^\n]*(?:\n[^\n]*){0,2}?\s(?:ha scritto|wrote|пише|пишет|написав|"
               r"написала|a écrit|schrieb)\s*:?\s*(?:-+)?\s*$", re.M | re.I),
    re.compile(r"^-{2,}\s*(?:messaggio originale|original message|messaggio inoltrato)", re.M | re.I),
    # Outlook header block of the previous message
    re.compile(r"^\s*(?:Da|From|Від)\s*:.*\n\s*(?:Inviato|Data|Sent|Date|Надіслано)\s*:", re.M | re.I),
]
_NOISE = [
    re.compile(r"\[cid:[^\]]*\]"),
    re.compile(r"^(?:Nota di riservatezza|Confidentiality notice)\s*\n.*?(?:\n\s*\n|\Z)",
               re.M | re.S | re.I),
]


def clean_body(body: str, subject: str = "") -> str:
    """The new part of an email: quoted history cut (except for forwards, where the forwarded
    message IS the content), legal disclaimers and inline-image markers removed."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    if not FORWARD.match(subject or ""):
        cut = len(text)
        for rx in _REPLY_MARKERS:
            m = rx.search(text)
            if m and m.start() < cut and text[:m.start()].strip():
                cut = m.start()
        text = text[:cut]
    text = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith(">"))
    for rx in _NOISE:
        text = rx.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY].rstrip() + "\n[…лист довший, решту не включено]"
    return text


def _names(msg: dict[str, Any]) -> dict[str, str]:
    return {a.get("emailAddress", "").lower(): a.get("displayName", "").strip("'\" ")
            for a in msg.get("addressDisplayNames") or []}


def _who(addr: str, names: dict[str, str]) -> str:
    name = names.get((addr or "").lower())
    return f"{name} <{addr}>" if name and name.lower() != (addr or "").lower() else (addr or "")


def outgoing(msg: dict[str, Any]) -> bool:
    return "SENT" in (msg.get("labelIds") or [])


def msg_time(msg: dict[str, Any]) -> datetime:
    if msg.get("internalDate"):
        return datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
    return datetime.fromisoformat(str(msg["date"]).replace("Z", "+00:00"))


def format_email(msg: dict[str, Any], previous: dict[str, Any] | None = None) -> str:
    """What Atlas stores and what the parser reads. Header lines are in Ukrainian, the email
    text stays exactly as written."""
    names = _names(msg)
    local = msg_time(msg).astimezone(_tz())
    lines = ["[ЛИСТ] " + ("надіслав я" if outgoing(msg) else "вхідний"),
             f"Від: {_who(msg.get('sender', ''), names)}"]
    if msg.get("toRecipients"):
        lines.append("Кому: " + ", ".join(_who(a, names) for a in msg["toRecipients"]))
    if msg.get("ccRecipients"):
        lines.append("Копія: " + ", ".join(_who(a, names) for a in msg["ccRecipients"]))
    lines.append(f"Дата: {local:%Y-%m-%d %H:%M}")
    lines.append(f"Тема: {msg.get('subject') or '(без теми)'}")
    files = [a.get("filename") for a in msg.get("attachments") or []
             if a.get("filename") and not re.match(r"image\d+\.(png|jpe?g|gif)$", a["filename"], re.I)]
    if files:
        lines.append("Вкладення: " + ", ".join(files))
    if previous is not None:
        prev = clean_body(previous.get("plaintextBody") or previous.get("snippet") or "",
                          previous.get("subject") or "")
        prev = " ".join(prev.split())[:CONTEXT_CHARS]
        who = _names(previous).get(previous.get("sender", "").lower()) or previous.get("sender", "")
        if prev:
            lines.append(f"Попередній лист у цій розмові (від {who}, "
                         f"{msg_time(previous).astimezone(_tz()):%d.%m}): «{prev}»")
    lines.append("---")
    lines.append(clean_body(msg.get("plaintextBody") or msg.get("snippet") or "",
                            msg.get("subject") or ""))
    return "\n".join(lines)


def _tz():
    from .dates import TZ
    return TZ


# ------------------------------------------------------------------ Gmail through Claude Code
def tool_results(stream: str, tool_name: str) -> list[Any]:
    """Results of every call of `tool_name` in a `claude -p --output-format stream-json`
    transcript, parsed from the tool's own output (never from the model's words)."""
    return [r for _inp, r in tool_calls(stream, tool_name)]


def tool_calls(stream: str, tool_name: str) -> list[tuple[dict[str, Any], Any]]:
    """(arguments, parsed result) of every call of `tool_name`, in order."""
    uses: dict[str, dict[str, Any]] = {}
    out: list[tuple[dict[str, Any], Any]] = []
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        # Claude Code replaces a big result in the transcript with a "<persisted-output>"
        # stub; the event's own tool_use_result keeps the whole output.
        whole = ev.get("tool_use_result")
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == tool_name:
                uses[block.get("id")] = block.get("input") or {}
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                if block.get("is_error"):
                    raise MailError(f"{tool_name} failed: {str(block.get('content'))[:300]}")
                inp = uses[block.get("tool_use_id")]
                parsed = _from_whole(whole)
                if parsed is not None:
                    out.append((inp, parsed))
                    continue
                raw = block.get("content")
                if isinstance(raw, list):
                    raw = "".join(p.get("text", "") for p in raw if isinstance(p, dict))
                if isinstance(raw, str) and raw.lstrip().startswith("<persisted-output>"):
                    raise MailError(f"{tool_name} output was cut by Claude Code (too large)")
                try:
                    out.append((inp, json.loads(raw)))
                except (TypeError, ValueError) as exc:
                    raise MailError(f"{tool_name} output is not Gmail JSON: {str(raw)[:300]}") from exc
    return out


def _from_whole(whole: Any) -> Any:
    """The full tool output from an event's tool_use_result: {structuredContent, content}."""
    if not isinstance(whole, dict):
        return None
    sc = whole.get("structuredContent")
    if isinstance(sc, dict) and sc:
        return sc
    raw = whole.get("content")
    if isinstance(raw, list):
        raw = "".join(p.get("text", "") for p in raw if isinstance(p, dict))
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return None


def final_result(stream: str) -> dict[str, Any]:
    for line in reversed(stream.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                if ev.get("is_error"):
                    raise MailError(f"claude CLI error: {str(ev.get('result'))[:300]}")
                return ev
    raise MailError("claude CLI gave no result")


class ClaudeCodeGmail:
    """Gmail via the Claude Code CLI on the Mac (claude.ai Gmail connector, Claude subscription:
    no Google Cloud project, no password, no payment)."""

    TRIAGE_SCHEMA = {"type": "object", "required": ["threads"], "properties": {"threads": {
        "type": "array", "items": {"type": "object", "required": ["threadId", "relevant", "reason"],
                                   "properties": {"threadId": {"type": "string"},
                                                  "relevant": {"type": "boolean"},
                                                  "reason": {"type": "string"}}}}}}

    def __init__(self, prefix: str = "mcp__claude_ai_Gmail__", search_model: str = "haiku",
                 triage_model: str = "sonnet", executable: str | None = None,
                 timeout: float = 600.0, runner: Any = None):
        import shutil
        import subprocess
        self.prefix, self.search_model, self.triage_model = prefix, search_model, triage_model
        self.timeout = timeout
        self.executable = executable or shutil.which("claude") or "claude"
        self.runner = runner or subprocess.run

    def _cmd(self, model: str, allowed: list[str], schema: dict | None = None) -> list[str]:
        cmd = [self.executable, "-p", "--output-format", "stream-json", "--verbose",
               "--model", model, "--allowedTools", ",".join(allowed),
               "--disallowedTools", BUILTIN_BLOCK + "," + ",".join(self.prefix + t for t in WRITE_TOOLS),
               "--setting-sources", "", "--no-session-persistence"]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        return cmd

    def _run(self, cmd: list[str], prompt: str) -> str:
        env = dict(os.environ, MAX_MCP_OUTPUT_TOKENS=os.environ.get("MAX_MCP_OUTPUT_TOKENS", "120000"))
        try:
            proc = self.runner(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               timeout=self.timeout, env=env)
        except Exception as exc:
            raise MailError(f"claude CLI: {type(exc).__name__}: {exc}") from exc
        if proc.returncode != 0:
            raise MailError(f"claude CLI exit {proc.returncode}: {(proc.stderr or proc.stdout or '')[-300:]}")
        return proc.stdout

    def search(self, query: str) -> list[dict[str, Any]]:
        """Threads matching `query`, newest message of each (THREAD_VIEW_SUMMARY), all pages."""
        tool = self.prefix + "search_threads"
        prompt = (f"{LOAD.format(tool=tool)}Call the tool {tool} with exactly these arguments: "
                  + json.dumps({"query": query, "pageSize": 20, "view": "THREAD_VIEW_SUMMARY"})
                  + ". If the answer has a nextPageToken, call it again with the same arguments plus "
                  "pageToken set to that token; repeat until there is no nextPageToken (at most 60 "
                  "calls). Call no other tool. Then reply with the single word DONE.")
        out = self._run(self._cmd(self.search_model, [tool]), prompt)
        final_result(out)
        pages = tool_results(out, tool)
        if not pages:
            raise MailError("search_threads was not called")
        threads: dict[str, dict[str, Any]] = {}
        for page in pages:
            for t in page.get("threads") or []:
                threads[t["id"]] = t
        return list(threads.values())

    def triage_and_fetch(self, threads: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[str, dict]]:
        """Claude decides which threads matter for Atlas and reads those (get_thread).
        Returns (verdicts by thread id, full threads by id). Full text comes from the tool."""
        tool = self.prefix + "get_thread"
        brief = []
        for t in threads:
            m = (t.get("messages") or [{}])[-1]
            brief.append({"threadId": t["id"], "messages": t.get("messageCount"),
                          "from": m.get("sender"), "names": [a.get("displayName") for a in
                                                             m.get("addressDisplayNames") or []],
                          "to": m.get("toRecipients"), "subject": m.get("subject"),
                          "snippet": (m.get("snippet") or "")[:300], "labels": m.get("labelIds"),
                          "sent_by_me": outgoing(m)})
        prompt = LOAD.format(tool=tool) + TRIAGE_PROMPT.format(tool=tool) + "\n\n" + json.dumps(brief, ensure_ascii=False)
        out = self._run(self._cmd(self.triage_model, [tool], self.TRIAGE_SCHEMA), prompt)
        res = final_result(out)
        verdicts = {v["threadId"]: v for v in (res.get("structured_output") or {}).get("threads") or []
                    if isinstance(v, dict) and v.get("threadId")}
        full = {t["id"]: t for t in tool_results(out, tool) if isinstance(t, dict) and t.get("id")}
        return verdicts, full


    def fetch(self, thread_ids: list[str]) -> dict[str, dict]:
        """Full threads by id (no triage)."""
        tool = self.prefix + "get_thread"
        prompt = (f"{LOAD.format(tool=tool)}Call the tool {tool} once for EACH thread id in this list, with "
                  "{\"threadId\": <id>, \"messageFormat\": \"PLAIN_TEXT\"}. Call no other tool and "
                  "change nothing. Then reply DONE.\n" + json.dumps(thread_ids))
        out = self._run(self._cmd(self.search_model, [tool]), prompt)
        final_result(out)
        return {t["id"]: t for t in tool_results(out, tool) if isinstance(t, dict) and t.get("id")}


TRIAGE_PROMPT = """\
You help Atlas, the work memory of Volodymyr Bilchuk, a warehouse shift coordinator at Fintyre \
(Seriate, Bergamo; Polo 1 / Polo 2; staff from agencies and a cooperative). Below are new email \
threads from his Gmail (newest message of each). Decide for EACH thread whether it is worth \
recording in his memory.

relevant = true: written by a real person (or a work system) about his work: tasks or requests \
to him, shift programmes, loading plans, inventory, damages, problems, staff, agencies/HR, \
deadlines, meetings, replies he is waiting for, his own replies/promises ("Fatto", "provvedo"); \
an out-of-office reply from a colleague (useful: who is away until when); personal matters that \
need an action or have a deadline (bills to pay, appointments, documents, deliveries with a problem).
relevant = false: advertising, newsletters, promotions, Black Friday, shops, airlines offers, \
social networks, login/verification codes, security notices, plain receipts and invoices already \
paid with nothing to do, automatic messages with no information in the text.

Then call the tool {tool} with {{"threadId": "<id>", "messageFormat": "PLAIN_TEXT"}} once for \
EVERY thread you marked relevant (and for no other thread). Call no other tool. Never send, \
reply, delete, label or change anything. Finally return `threads`: for every thread in the list, \
its threadId, relevant, and reason (a few words in Ukrainian)."""


# ------------------------------------------------------------------ ledger + poller
class Ledger:
    """~/Atlas/mail.db: what was already looked at. Losing it only costs a re-check: the
    inbox itself refuses a second copy of the same email."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path).expanduser()
        c = self._c()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS threads (thread_id TEXT PRIMARY KEY, last_msg_id TEXT,
                relevant INTEGER, reason TEXT, subject TEXT, sender TEXT, checked_at TEXT);
            CREATE TABLE IF NOT EXISTS messages (msg_id TEXT PRIMARY KEY, thread_id TEXT,
                sent_at TEXT, inbox_id INTEGER, stored_at TEXT);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);""")
        c.commit()
        c.close()

    def _c(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def get(self, key: str) -> str | None:
        c = self._c()
        try:
            r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return r[0] if r else None
        finally:
            c.close()

    def put(self, key: str, value: str) -> None:
        c = self._c()
        with c:
            c.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?,?)", (key, value))
        c.close()

    def thread_last(self) -> dict[str, str]:
        c = self._c()
        try:
            return {r[0]: r[1] for r in c.execute("SELECT thread_id, last_msg_id FROM threads")}
        finally:
            c.close()

    def stored(self) -> set[str]:
        c = self._c()
        try:
            return {r[0] for r in c.execute("SELECT msg_id FROM messages")}
        finally:
            c.close()

    def mark_thread(self, t: dict[str, Any], relevant: bool, reason: str) -> None:
        m = (t.get("messages") or [{}])[-1]
        c = self._c()
        with c:
            c.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?,?)",
                      (t["id"], m.get("id"), int(relevant), reason, m.get("subject"),
                       m.get("sender"), _now_iso()))
        c.close()

    def mark_message(self, msg: dict[str, Any], inbox_id: int | None) -> None:
        c = self._c()
        with c:
            c.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?)",
                      (msg["id"], msg.get("threadId"), msg_time(msg).isoformat(), inbox_id, _now_iso()))
        c.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MailPoller:
    def __init__(self, db: str | os.PathLike, gmail: Any, parser: Any,
                 ledger: str | os.PathLike | None = None, backfill_days: int = 7,
                 overlap_hours: int = 24, clock: Callable[[], float] = time.time):
        self.db = str(Path(db).expanduser())
        self.gmail, self.parser, self.clock = gmail, parser, clock
        self.ledger = Ledger(ledger or Path(self.db).with_name("mail.db"))
        self.backfill_days, self.overlap_hours = backfill_days, overlap_hours

    def poll(self) -> dict[str, Any]:
        started = self.clock()
        floor = self.ledger.get("floor")
        if floor is None:      # first run ever: look back backfill_days, never further
            floor = str(int(started - self.backfill_days * 86400))
            self.ledger.put("floor", floor)
        cursor = int(self.ledger.get("cursor") or floor)
        since = max(int(floor), cursor - self.overlap_hours * 3600)
        threads = retry(lambda: self.gmail.search(f"after:{since} -in:spam -in:trash"))
        known = self.ledger.thread_last()
        fresh = [t for t in threads
                 if (t.get("messages") or [{}])[-1].get("id") != known.get(t["id"])]
        report = {"threads": len(threads), "new_threads": len(fresh), "relevant": 0,
                  "stored": 0, "results": []}
        if fresh:
            verdicts, full = retry(lambda: self.gmail.triage_and_fetch(fresh), attempts=2)
            stored = self.ledger.stored()
            todo: list[tuple[datetime, dict[str, Any], dict[str, Any] | None]] = []
            done_threads = []
            for t in fresh:
                v = verdicts.get(t["id"])
                if v is None:
                    continue                                  # not judged: retried next pass
                if not v.get("relevant"):
                    self.ledger.mark_thread(t, False, v.get("reason", ""))
                    continue
                if t["id"] not in full:
                    log.warning("thread %s relevant but not fetched, retry later", t["id"])
                    continue
                report["relevant"] += 1
                msgs = sorted(full[t["id"]].get("messages") or [], key=msg_time)
                prev = None
                for m in msgs:
                    m.setdefault("threadId", t["id"])
                    if m["id"] not in stored and msg_time(m).timestamp() >= int(floor):
                        todo.append((msg_time(m), m, prev))
                    prev = m
                done_threads.append((t, v))
            todo.sort(key=lambda x: x[0])
            store = Store(self.db)
            try:
                for _t, m, prev in todo:
                    vol = self._volumes(store, m)
                    if vol is not None:                       # an unloading programme: numbers only
                        self.ledger.mark_message(m, vol.get("inbox_id"))
                        report["stored"] += 1
                        report["results"].append({"inbox_id": vol.get("inbox_id"),
                                                  "status": "programme"})
                        continue
                    text = format_email(m, prev)
                    inbox_id, _created = store.ingest(SOURCE, m["id"], text,
                                                      received_at=msg_time(m).strftime("%Y-%m-%dT%H:%M:%SZ"))
                    self.ledger.mark_message(m, inbox_id)     # durable in atlas.db from here on
                    report["stored"] += 1
                    report["results"].append(self._process(store, inbox_id))
            finally:
                store.close()
            for t, v in done_threads:
                self.ledger.mark_thread(t, True, v.get("reason", ""))
        report["results"] += self.retry_pending()
        self.ledger.put("cursor", str(int(started)))
        if report["stored"] or report["new_threads"]:
            log.info("mail: %s", {k: v for k, v in report.items() if k != "results"})
        return report

    def _volumes(self, store: Store, m: dict[str, Any]) -> dict[str, Any] | None:
        """Unloading programme from Fintyre: counted by code (atlas.volumes). When the e-mail
        is only the programme (a greeting + the table), it is not sent to the parser at all."""
        from . import volumes
        if outgoing(m) or not str(m.get("sender", "")).lower().endswith(PROGRAMME_DOMAINS):
            return None
        body = m.get("plaintextBody") or ""
        parsed = volumes.parse_programme(body)
        if parsed is None:
            return None
        who = _names(m).get(m.get("sender", "").lower()) or m.get("sender", "")
        res = volumes.record(store, m["id"], body, who,
                             received_at=msg_time(m).strftime("%Y-%m-%dT%H:%M:%SZ"))
        intro = volumes.intro_text(body)
        if len(intro) > 250:            # more than "ciao, programma di domani": parse the words too
            return None
        return res

    def _process(self, store: Store, inbox_id: int) -> dict[str, Any]:
        try:
            return process_message(store, self.parser, inbox_id)
        except Exception as exc:
            log.exception("inbox %s", inbox_id)
            store.mark_failed(inbox_id, f"{type(exc).__name__}: {exc}")
            return {"inbox_id": inbox_id, "status": "retry_later", "error": str(exc)}

    def retry_pending(self) -> list[dict[str, Any]]:
        store = Store(self.db)
        try:
            return [self._process(store, m["id"]) for m in store.pending_inbox(MAX_ATTEMPTS)
                    if m["source"] == SOURCE]
        finally:
            store.close()

    def run_forever(self, interval: float, turni: Any = None, turni_every: float = 3 * 3600) -> None:
        log.info("reading Gmail every %.0f min (read only)", interval / 60)
        last_turni = 0.0
        while True:
            try:
                self.poll()
            except Exception as exc:
                log.warning("mail pass failed, will retry: %s", exc)
            if turni is not None and time.time() - last_turni >= turni_every:
                try:
                    turni.sync()
                    last_turni = time.time()
                except Exception as exc:
                    log.warning("shift sheet read failed, will retry: %s", exc)
                    last_turni = time.time() - turni_every + 900
            time.sleep(interval)


def load_config(path: str | os.PathLike = "~/Atlas/mail.json") -> dict[str, Any] | None:
    p = Path(path).expanduser()
    if not p.exists():
        return None
    cfg = json.loads(p.read_text(encoding="utf-8"))
    return cfg if cfg.get("enabled", True) else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atlas-mail")
    p.add_argument("--db", default=os.environ.get("ATLAS_DB", "~/Atlas/atlas.db"))
    p.add_argument("--config", default="~/Atlas/mail.json")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(args.config)
    if cfg is None:
        print("mail reading is off (no ~/Atlas/mail.json)")
        if args.once:
            return 1
        while load_config(args.config) is None:   # wait quietly instead of crash-looping
            time.sleep(300)
        cfg = load_config(args.config) or {}
    from .understand import ClaudeCodeParser
    gmail = ClaudeCodeGmail(prefix=cfg.get("tool_prefix", "mcp__claude_ai_Gmail__"),
                            search_model=cfg.get("search_model", "haiku"),
                            triage_model=cfg.get("triage_model", "sonnet"))
    parser = ClaudeCodeParser(model=os.environ.get("ATLAS_CC_MODEL", cfg.get("parse_model", "opus")))
    poller = MailPoller(args.db, gmail, parser, backfill_days=int(cfg.get("backfill_days", 7)))
    if args.once:
        r = poller.poll()
        print(json.dumps({k: v for k, v in r.items() if k != "results"}, ensure_ascii=False))
        for x in r["results"]:
            print(x.get("inbox_id"), x.get("status"))
        return 0
    turni = None
    if cfg.get("turni_spreadsheet_id"):
        from .turni import ClaudeCodeSheetsReader, TurniSync
        turni = TurniSync(str(Path(args.db).expanduser()), ClaudeCodeSheetsReader(
            prefix=cfg.get("sheets_tool_prefix", "mcp__claude_ai_Google_Sheets__")),
            cfg["turni_spreadsheet_id"])
    poller.run_forever(float(cfg.get("interval_minutes", 15)) * 60, turni=turni)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
