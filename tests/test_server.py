"""Stage 2: the HTTP server, end to end over real HTTP (Claude is faked)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from atlas.server import Atlas, make_handler
from atlas.understand import ClaudeParser
from test_stage1 import FakeClient, item

TOKEN = "t" * 32


@pytest.fixture
def srv(tmp_path):
    client = FakeClient()
    app = Atlas(str(tmp_path / "s.db"), TOKEN, lambda: ClaudeParser(client=client))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield app, client, base
    httpd.shutdown()
    httpd.server_close()


def call(base, path, body=None, token=TOKEN, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health_needs_no_token_and_shows_no_data(srv):
    _, _, base = srv
    assert call(base, "/health", token=None) == (200, {"ok": True})


def test_wrong_or_missing_token_is_refused(srv):
    _, client, base = srv
    assert call(base, "/ingest", {"text": "x", "id": "1"}, token=None)[0] == 401
    assert call(base, "/ingest", {"text": "x", "id": "1"}, token="wrong" * 8)[0] == 401
    assert call(base, "/loops", token=None)[0] == 401
    assert client.calls == []


def test_ingest_understands_and_answers_in_ukrainian(srv):
    app, client, base = srv
    client.queue.append([item("new_loop", "на завтра підготувати місце біля рампи 31",
                              title="Підготувати місце біля рампи 31", due="tomorrow",
                              assigned_by="Роберто")])
    status, out = call(base, "/ingest",
                       {"text": "Роберто: на завтра підготувати місце біля рампи 31", "id": "a1"})
    assert status == 200 and out["status"] == "applied" and out["new"] is True
    assert out["reply"].startswith("Справа: «Підготувати місце біля рампи 31» до ")
    assert "(доручив Роберто)" in out["reply"]
    status, loops = call(base, "/loops")
    assert [l["title"] for l in loops["loops"]] == ["Підготувати місце біля рампи 31"]


def test_same_id_twice_is_one_message(srv):
    _, client, base = srv
    client.queue.append([item("idea", "QR на локаціях", title="QR на локаціях")])
    first = call(base, "/ingest", {"text": "Ідея: QR на локаціях", "id": "same"})[1]
    second = call(base, "/ingest", {"text": "Ідея: QR на локаціях"},
                  headers={"Idempotency-Key": "same"})[1]
    assert first["inbox_id"] == second["inbox_id"]
    assert second["new"] is False and second["status"] == "already_applied"
    assert len(client.calls) == 1


def test_bad_requests(srv):
    _, _, base = srv
    assert call(base, "/ingest", {"text": "x"})[0] == 400              # no id
    assert call(base, "/ingest", {"text": "  ", "id": "1"})[0] == 400  # empty text
    assert call(base, "/ingest", raw=b"{not json")[0] == 400
    assert call(base, "/ingest", raw=b"[1]")[0] == 400
    assert call(base, "/ingest", {"text": "x" * 70000, "id": "1"})[0] == 413
    assert call(base, "/nope", {"text": "x", "id": "1"})[0] == 404


def test_claude_down_message_is_kept_and_worker_retries(srv):
    app, client, base = srv
    client.queue += [ConnectionError("down"), [item("idea", "полиця для повернень",
                                                    title="Полиця для повернень")]]
    status, out = call(base, "/ingest", {"text": "Ідея: полиця для повернень", "id": "r1"})
    assert status == 200 and out["status"] == "retry_later"
    assert "оброблю пізніше" in out["reply"]
    assert app.retry_pending() == 0            # back-off: not yet
    app.next_try.clear()
    assert app.retry_pending() == 1
    store = app.store()
    assert store.inbox(out["inbox_id"])["status"] == "done"
    assert store.conn.execute("SELECT title FROM ideas").fetchone()[0] == "Полиця для повернень"
    store.close()


def test_short_token_refuses_to_start(tmp_path):
    with pytest.raises(SystemExit):
        Atlas(str(tmp_path / "x.db"), "short", lambda: None)
