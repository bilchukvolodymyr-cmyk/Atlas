"""Atlas HTTP server (stage 2). Standard library only; runs behind Caddy (HTTPS).

POST /ingest     Authorization: Bearer <ATLAS_TOKEN>
                 JSON {"text": "...", "id": "<unique id from the phone>"}
                 (the id may also come as the Idempotency-Key header)
                 -> the message is stored first, then understood; the answer says what was
                    written. Same id again -> same message, nothing new is created.
GET  /loops      open loops          (auth)
GET  /questions  open questions      (auth)
GET  /health     liveness, no auth, no data

A background worker retries messages that failed (Claude down, network), with growing pauses.
Run:  ATLAS_TOKEN=... ANTHROPIC_API_KEY=... python3 -m atlas.server --db /var/lib/atlas/atlas.db
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .core import AtlasError, Store
from .pipeline import MAX_ATTEMPTS, process_message
from .reply import describe

log = logging.getLogger("atlas.server")
MAX_BODY = 64 * 1024
MAX_ID = 200


class Atlas:
    """Shared state: one processing lock so a message is never sent to Claude twice at once."""

    def __init__(self, db: str, token: str, parser_factory: Callable[[], Any],
                 wait_seconds: float = 25.0):
        if len(token) < 24:
            raise SystemExit("ATLAS_TOKEN must be at least 24 characters")
        self.db, self.token = db, token
        self.parser_factory = parser_factory
        self._parser = None
        self.wait_seconds = wait_seconds
        self.lock = threading.Lock()
        self.next_try: dict[int, float] = {}
        Store(db).close()  # create schema once at start

    @property
    def parser(self):
        if self._parser is None:
            self._parser = self.parser_factory()
        return self._parser

    def store(self) -> Store:
        return Store(self.db)  # one connection per thread / request

    def handle_message(self, text: str, external_id: str) -> dict[str, Any]:
        store = self.store()
        try:
            inbox_id, created = store.ingest("phone", external_id, text)
            # the message is durable now; understanding it is a separate step
            if not self.lock.acquire(timeout=self.wait_seconds):
                return {"inbox_id": inbox_id, "new": created, "status": "pending",
                        "reply": describe({"status": "pending"}, store)}
            try:
                result = process_message(store, self.parser, inbox_id)
            finally:
                self.lock.release()
            if result["status"] == "retry_later":
                self.next_try[inbox_id] = time.monotonic() + 60
            return {"inbox_id": inbox_id, "new": created, "status": result["status"],
                    "reply": describe(result, store)}
        finally:
            store.close()

    def retry_pending(self) -> int:
        """One worker pass: messages that are new or failed, respecting back-off."""
        done = 0
        store = self.store()
        try:
            for msg in store.pending_inbox(max_attempts=MAX_ATTEMPTS):
                if time.monotonic() < self.next_try.get(msg["id"], 0):
                    continue
                with self.lock:
                    try:
                        result = process_message(store, self.parser, msg["id"])
                    except Exception as exc:  # keep the worker alive
                        log.exception("inbox %s", msg["id"])
                        store.mark_failed(msg["id"], f"{type(exc).__name__}: {exc}")
                        result = {"status": "retry_later"}
                if result["status"] == "retry_later":
                    attempts = (store.inbox(msg["id"]) or {}).get("attempts", 1)
                    self.next_try[msg["id"]] = time.monotonic() + 60 * 2 ** attempts
                else:
                    self.next_try.pop(msg["id"], None)
                    done += 1
        finally:
            store.close()
        return done

    def run_worker(self, stop: threading.Event, interval: float = 15.0) -> None:
        while not stop.is_set():
            try:
                self.retry_pending()
            except Exception:
                log.exception("worker pass failed")
            stop.wait(interval)


def make_handler(app: Atlas) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Atlas"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # no message text in logs
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            given = header[7:] if header.startswith("Bearer ") else ""
            return hmac.compare_digest(given.encode(), app.token.encode())

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            store = app.store()
            try:
                if self.path == "/loops":
                    return self._send(200, {"loops": store.open_loops()})
                if self.path == "/questions":
                    return self._send(200, {"questions": store.open_questions()})
            finally:
                store.close()
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/ingest":
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, {"error": "bad Content-Length"})
            if length <= 0 or length > MAX_BODY:
                return self._send(413 if length > MAX_BODY else 400, {"error": "bad body size"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
            except (ValueError, UnicodeDecodeError) as exc:
                return self._send(400, {"error": f"bad JSON: {exc}"})
            text = body.get("text")
            ext = str(body.get("id") or self.headers.get("Idempotency-Key") or "").strip()
            if not isinstance(text, str) or not text.strip():
                return self._send(400, {"error": "text is required"})
            if not ext or len(ext) > MAX_ID:
                return self._send(400, {"error": "id (or Idempotency-Key) is required"})
            try:
                out = app.handle_message(text.strip(), ext)
            except AtlasError as exc:
                return self._send(400, {"error": str(exc)})
            except Exception:
                log.exception("ingest failed")
                return self._send(500, {"error": "internal error"})
            self._send(HTTPStatus.OK, out)

    return Handler


def serve(app: Atlas, host: str, port: int, worker: bool = True) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    stop = threading.Event()
    if worker:
        threading.Thread(target=app.run_worker, args=(stop,), daemon=True).start()
    log.info("Atlas listening on %s:%s", host, port)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atlas-server")
    p.add_argument("--db", default=os.environ.get("ATLAS_DB", "atlas.db"))
    p.add_argument("--host", default=os.environ.get("ATLAS_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("ATLAS_PORT", "8710")))
    p.add_argument("--model", default=os.environ.get("ATLAS_MODEL", "claude-opus-5-5"))
    p.add_argument("--effort", default=os.environ.get("ATLAS_EFFORT", "medium"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = os.environ.get("ATLAS_TOKEN", "")

    def factory():
        from .understand import ClaudeParser
        return ClaudeParser(model=args.model, effort=args.effort)

    serve(Atlas(args.db, token, factory), args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
