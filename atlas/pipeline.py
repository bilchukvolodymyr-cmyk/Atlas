"""inbox -> Claude -> code checks -> Store.apply. Safe to run again at any moment.

* A message already applied is never sent to Claude again (status 'done').
* Claude/API failure -> message stays in the inbox as 'failed' and is retried later
  (up to MAX_ATTEMPTS); nothing is lost, nothing is half-written.
* If the checked actions still fail in apply (e.g. a state Claude misread), Claude gets one
  more try with the error; if that fails too, the message is kept as a plain note with an
  open question, so it is never silently dropped.
"""
from __future__ import annotations

import logging
from typing import Any

from . import dates
from .core import AtlasError, Store
from .understand import ClaudeParser, ParseError, build_context, translate

log = logging.getLogger("atlas.pipeline")
MAX_ATTEMPTS = 5


def process_message(store: Store, parser: ClaudeParser, inbox_id: int) -> dict[str, Any]:
    msg = store.inbox(inbox_id)
    if msg is None:
        raise AtlasError(f"inbox {inbox_id} not found")
    if msg["status"] == "done":
        return {"inbox_id": inbox_id, "status": "already_applied"}
    now = dates.local_now(msg["received_at"])
    feedback = None
    for attempt in (1, 2):
        try:
            parsed = parser.parse(msg["raw_text"], build_context(store, now), feedback)
        except ParseError as exc:
            store.log_llm_run(inbox_id, exc.status, model=parser.model, request_id=exc.request_id,
                              output=exc.raw, error=str(exc))
            if exc.status == "api_error":
                store.mark_failed(inbox_id, str(exc))
                return {"inbox_id": inbox_id, "status": "retry_later", "error": str(exc)}
            return _keep_as_note(store, inbox_id, msg["raw_text"], str(exc))

        tr = translate(msg["raw_text"], parsed.items, now, store)
        actions = tr.actions
        if not actions:
            # nothing recognisable: keep the words as a note so they are findable later
            actions = [{"type": "create_entry", "kind": "note", "text": msg["raw_text"]}]
        try:
            result = store.apply(inbox_id, actions, actor="llm")
        except AtlasError as exc:
            store.log_llm_run(inbox_id, "apply_error", model=parsed.model,
                              request_id=parsed.request_id, output=parsed.items,
                              report=tr.report(), error=f"{type(exc).__name__}: {exc}",
                              usage=parsed.usage)
            feedback = f"{type(exc).__name__}: {exc}"
            log.warning("inbox %s attempt %s: %s", inbox_id, attempt, feedback)
            continue
        store.log_llm_run(inbox_id, "ok", model=parsed.model, request_id=parsed.request_id,
                          output=parsed.items, report=tr.report(), usage=parsed.usage)
        return {"inbox_id": inbox_id, "status": result["status"], "created": result.get("created"),
                "actions": actions, "rejected": tr.rejected, "dropped_fields": tr.dropped_fields}
    return _keep_as_note(store, inbox_id, msg["raw_text"], feedback or "unknown error")


def _keep_as_note(store: Store, inbox_id: int, text: str, why: str) -> dict[str, Any]:
    actions = [
        {"type": "create_entry", "kind": "note", "text": text, "ref": "n"},
        {"type": "ask_question", "object_type": "entry", "object_id": "$n",
         "text": "Не зміг розібрати це повідомлення — зберіг як нотатку. Що з ним зробити?",
         "missing_field": "manual_review"},
    ]
    result = store.apply(inbox_id, actions, actor="system")
    return {"inbox_id": inbox_id, "status": "kept_as_note", "why": why,
            "created": result.get("created")}


def process_pending(store: Store, parser: ClaudeParser) -> list[dict[str, Any]]:
    """Process every message that is new or failed earlier (oldest first)."""
    results = []
    for msg in store.pending_inbox(max_attempts=MAX_ATTEMPTS):
        try:
            results.append(process_message(store, parser, msg["id"]))
        except Exception as exc:  # one bad message must not stop the others
            log.exception("inbox %s", msg["id"])
            store.mark_failed(msg["id"], f"{type(exc).__name__}: {exc}")
            results.append({"inbox_id": msg["id"], "status": "error", "error": str(exc)})
    return results
