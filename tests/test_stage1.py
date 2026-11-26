"""Stage 1 tests: Claude is replaced by a fake client, so these run offline and free."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from atlas import dates
from atlas.core import Store
from atlas.pipeline import process_message, process_pending
from atlas.understand import OUTPUT_SCHEMA, ClaudeParser, _ITEM_PROPS, in_message, normalize

ROME = dates.TZ
# Thursday 26 November 2026, 10:00 in Rome = 09:00 UTC
NOW_UTC = "2026-11-26T09:00:00+00:00"


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=ROME)


def item(type_: str, quote: str, **kw) -> dict:
    d = {k: None for k in _ITEM_PROPS}
    d.update(type=type_, quote=quote, **kw)
    return d


class FakeClient:
    """Returns queued responses; records every request."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, SimpleNamespace):
            return r
        return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", id="msg_1",
                               usage=None,
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=json.dumps({"items": r}))])


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "a.db", clock=lambda: NOW_UTC)
    yield s
    s.close()


def say(store, client, text, ext="m1", received=NOW_UTC):
    inbox_id, _ = store.ingest("test", ext, text, received_at=received)
    return process_message(store, ClaudeParser(client=client), inbox_id)


# ------------------------------------------------------------------ dates (code, not Claude)
def test_dates_from_thursday_26_november():
    now = at("2026-11-26T10:00")
    assert dates.resolve("tomorrow", now).iso() == "2026-11-27"
    assert dates.resolve("weekday:sat", now).iso() == "2026-11-28"
    assert dates.resolve("next_week", now).iso() == "2026-12-05"
    assert dates.resolve("end_of_month", now).iso() == "2026-11-30"
    assert dates.resolve("date:01.12", now).iso() == "2026-12-01"
    assert dates.resolve("day:3", now).iso() == "2026-12-03"
    assert dates.resolve("in_weeks:2", now).iso() == "2026-12-10"
    assert dates.resolve("tomorrow@14:30", now).iso() == "2026-11-27T14:30"


def test_ambiguous_and_bad_dates():
    now = at("2026-11-26T10:00")
    with pytest.raises(dates.AmbiguousDate):
        dates.resolve("weekday:thu", now)        # "у четвер" said on Thursday
    with pytest.raises(dates.AmbiguousDate):
        dates.resolve("date:2026-11-20", now)    # in the past
    with pytest.raises(dates.DateError):
        dates.resolve("someday", now)
    with pytest.raises(dates.DateError):
        dates.resolve("date:31.02", now)


def test_late_evening_utc_is_already_tomorrow_in_rome():
    now = dates.local_now("2026-11-26T23:30:00+00:00")   # 00:30 on 27th in Rome
    assert dates.resolve("tomorrow", now).iso() == "2026-11-28"


def test_reminder_policy():
    morning = at("2026-11-26T10:00")
    evening = at("2026-11-26T20:00")
    tomorrow = dates.resolve("tomorrow", morning)
    assert dates.remind_at(tomorrow, morning) == "2026-11-26T18:00+01:00"
    assert dates.remind_at(tomorrow, evening) == "2026-11-27T07:00+01:00"
    week = dates.resolve("next_week", morning)
    assert dates.remind_at(week, morning) == "2026-11-30T07:00+01:00"
    month = dates.resolve("end_of_month", morning)
    assert dates.remind_at(month, morning) == "2026-11-27T07:00+01:00"
    assert dates.remind_at(dates.resolve("today", evening), evening) is None   # overdue digest


# ------------------------------------------------------------------ request shape
def test_request_uses_structured_output_fallbacks_and_context(store):
    client = FakeClient([])
    say(store, client, "Привіт")
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert "thinking" not in call            # always on for this model; effort controls depth
    assert "2026-11-26 10:00 (Thursday)" in call["messages"][0]["content"]


def test_schema_is_strict_everywhere():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for k in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
                assert k not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(OUTPUT_SCHEMA)


# ------------------------------------------------------------------ real-life flows
ROBERTO = ("Роберто Полі дав завдання: на завтра підготувати місце біля рампи 31, "
           "в суботу звільнити зону 2O 06, а на наступний тиждень перевірити етикетки 3Q.")


def test_three_tasks_from_roberto_with_different_deadlines(store):
    client = FakeClient([
        item("new_loop", "на завтра підготувати місце біля рампи 31", ref="L1", kind="task",
             title="Підготувати місце біля рампи 31", due="tomorrow", assigned_by="Роберто Полі"),
        item("new_loop", "в суботу звільнити зону 2O 06", ref="L2", kind="task",
             title="Звільнити зону 2O 06", due="weekday:sat", assigned_by="Роберто Полі"),
        item("new_loop", "на наступний тиждень перевірити етикетки 3Q", ref="L3", kind="task",
             title="Перевірити етикетки 3Q", due="next_week", assigned_by="Роберто Полі"),
    ])
    out = say(store, client, ROBERTO)
    assert out["status"] == "applied" and out["rejected"] == []
    loops = {l["title"]: l for l in store.open_loops()}
    assert loops["Підготувати місце біля рампи 31"]["due_at"] == "2026-11-27"
    assert loops["Підготувати місце біля рампи 31"]["remind_at"] == "2026-11-26T18:00+01:00"
    assert loops["Звільнити зону 2O 06"]["due_at"] == "2026-11-28"
    assert loops["Перевірити етикетки 3Q"]["due_at"] == "2026-12-05"
    assert {l["assigned_by"] for l in loops.values()} == {"Роберто Полі"}
    assert all(l["created_from"] == 1 for l in loops.values())


def test_invented_quote_is_rejected_and_invented_name_dropped(store):
    client = FakeClient([
        item("new_loop", "замовити 50 палет", title="Замовити палети"),          # not said
        item("new_loop", "в суботу звільнити зону 2O 06", title="Звільнити зону 2O 06",
             assigned_by="Marco Rossi"),                                         # not said
    ])
    out = say(store, client, ROBERTO)
    assert [r["why"] for r in out["rejected"]] == ["quote is not in the message"]
    assert out["dropped_fields"][0]["field"] == "assigned_by"
    [loop] = store.open_loops()
    assert loop["title"] == "Звільнити зону 2O 06" and loop["assigned_by"] is None


def test_quote_check_ignores_case_and_punctuation_only():
    msg = normalize("Сьогодні 10 машин на розвантаження, 18 — на завантаження!")
    assert in_message("сьогодні 10 машин на розвантаження 18", msg)
    assert in_message("«18 на завантаження»", msg)
    assert not in_message("12 машин", msg)
    assert not in_message("1", msg)            # "1" must not match inside "10"
    assert not in_message("", msg)


def test_daily_metrics_and_number_check(store):
    text = "Сьогодні 10 машин на розвантаження, 18 на завантаження, на Polo 2 було 103 людини."
    client = FakeClient([
        item("metric", "10 машин на розвантаження", metric="trucks_unload", value=10),
        item("metric", "18 на завантаження", metric="trucks_load", value=18),
        item("metric", "на Polo 2 було 103 людини", metric="workers_total", value=103, site="polo2"),
        item("metric", "18 на завантаження", metric="trucks_extra", value=81),   # invented number
    ])
    out = say(store, client, text)
    assert len(out["rejected"]) == 1
    rows = {(r["name"], r["site"]): r["value"]
            for r in store.conn.execute("SELECT * FROM metrics WHERE date='2026-11-26'")}
    assert rows == {("trucks_unload", "all"): 10, ("trucks_load", "all"): 18,
                    ("workers_total", "polo2"): 103}
    assert any(q["missing_field"] == "trucks_extra" for q in store.open_questions())


def test_ambiguous_date_creates_loop_without_due_and_asks(store):
    client = FakeClient([item("new_loop", "у четвер перевірити CrossChex", ref="L1",
                              title="Перевірити CrossChex", due="weekday:thu")])
    say(store, client, "у четвер перевірити CrossChex")
    [loop] = store.open_loops()
    assert loop["due_at"] is None
    [q] = store.open_questions()
    assert (q["object_type"], q["object_id"], q["missing_field"]) == ("loop", loop["id"], "due_at")


def test_done_needs_confirmation_in_a_later_message(store):
    say(store, FakeClient([item("new_loop", "відправити години за листопад", ref="L1",
                                title="Відправити години за листопад", due="end_of_month")]),
        "До кінця місяця відправити години за листопад", ext="m1")
    loop_id = store.open_loops()[0]["id"]

    # "done" + an over-eager confirm in the same message: confirm must be refused
    out = say(store, FakeClient([
        item("change_state", "відправив години", loop_id=loop_id, state="done_claimed"),
        item("confirm_done", "відправив години", loop_id=loop_id),
    ]), "Відправив години в бухгалтерію", ext="m2")
    assert out["rejected"][0]["why"] == "loop is not waiting for confirmation"
    assert store.loop(loop_id)["state"] == "verifying"
    [q] = store.open_questions()
    assert q["missing_field"] == "confirm_close"

    say(store, FakeClient([item("confirm_done", "так", loop_id=loop_id)]), "Так", ext="m3")
    assert store.loop(loop_id)["state"] == "closed"
    assert store.open_questions() == []
    history = store.history("loop", loop_id)
    evidence = [h["payload"]["evidence"] for h in history if h["type"] == "evidence_added"]
    assert evidence == ["відправив години"]
    assert [h for h in history if h["type"] == "verified"][0]["source_text"] == "Так"


def test_confirm_on_active_loop_is_refused(store):
    say(store, FakeClient([item("new_loop", "звільнити зону", title="Звільнити зону")]),
        "звільнити зону", ext="m1")
    out = say(store, FakeClient([item("confirm_done", "так", loop_id=1)]), "так", ext="m2")
    assert out["rejected"] and store.loop(1)["state"] == "active"


def test_unknown_loop_id_is_rejected(store):
    out = say(store, FakeClient([item("change_state", "зробив", loop_id=99,
                                      state="done_claimed")]), "зробив")
    assert out["rejected"][0]["why"] == "no such open loop or bad state"
    # nothing understood -> the words are still kept as a note
    assert store.conn.execute("SELECT text FROM entries").fetchone()[0] == "зробив"


# ------------------------------------------------------------------ reliability
def test_redelivery_does_not_call_claude_again(store):
    client = FakeClient([item("idea", "етикетки з QR", title="Етикетки з QR")])
    say(store, client, "Ідея: етикетки з QR", ext="same")
    out = say(store, client, "Ідея: етикетки з QR", ext="same")
    assert out["status"] == "already_applied"
    assert len(client.calls) == 1
    assert store.conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1


def test_api_error_keeps_message_and_retries_later(store):
    client = FakeClient(ConnectionError("network down"),
                        [item("idea", "етикетки з QR", title="Етикетки з QR")])
    out = say(store, client, "Ідея: етикетки з QR")
    assert out["status"] == "retry_later"
    msg = store.inbox(1)
    assert msg["status"] == "failed" and msg["attempts"] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 0

    results = process_pending(store, ClaudeParser(client=client))
    assert results[0]["status"] == "applied"
    assert store.inbox(1)["status"] == "done"
    assert [r["status"] for r in store.llm_runs(1)] == ["api_error", "ok"]


def test_refusal_keeps_message_as_note_with_question(store):
    client = FakeClient(SimpleNamespace(stop_reason="refusal", content=[], id="x", usage=None))
    out = say(store, client, "щось дивне")
    assert out["status"] == "kept_as_note"
    assert store.conn.execute("SELECT text FROM entries").fetchone()[0] == "щось дивне"
    assert store.open_questions()[0]["missing_field"] == "manual_review"


def test_broken_json_keeps_message_as_note(store):
    bad = SimpleNamespace(stop_reason="end_turn", id="x", usage=None, model="m",
                          content=[SimpleNamespace(type="text", text="{not json")])
    out = say(store, FakeClient(bad), "текст")
    assert out["status"] == "kept_as_note"
    assert store.llm_runs(1)[0]["status"] == "bad_output"


def _loop_in_verifying(store):
    say(store, FakeClient([item("new_loop", "звільнити зону", title="Звільнити зону")]),
        "звільнити зону", ext="m1")
    say(store, FakeClient([item("change_state", "звільнив", loop_id=1, state="done_claimed")]),
        "звільнив", ext="m2")
    assert store.loop(1)["state"] == "verifying"


def test_apply_error_gets_one_retry_with_feedback(store):
    _loop_in_verifying(store)
    # verifying -> waiting is not allowed by the state machine: core refuses, Claude retries
    client = FakeClient(
        [item("change_state", "чекаю на Роберто", loop_id=1, state="waiting")],
        [item("note", "чекаю на Роберто", kind="note", text="Чекаю на Роберто")],
    )
    out = say(store, client, "чекаю на Роберто", ext="m3")
    assert out["status"] == "applied"
    assert "TransitionError" in client.calls[1]["messages"][0]["content"]
    assert [r["status"] for r in store.llm_runs(3)] == ["apply_error", "ok"]
    assert store.loop(1)["state"] == "verifying"


def test_two_apply_errors_keep_message_as_note(store):
    _loop_in_verifying(store)
    bad = [item("change_state", "чекаю", loop_id=1, state="waiting")]
    out = say(store, FakeClient(bad, bad), "чекаю", ext="m3")
    assert out["status"] == "kept_as_note"
    assert store.inbox(3)["status"] == "done"
    assert store.loop(1)["state"] == "verifying"


def test_one_bad_message_does_not_block_others(store):
    store.ingest("test", "a", "перше", received_at=NOW_UTC)
    store.ingest("test", "b", "друге: ідея QR", received_at=NOW_UTC)
    client = FakeClient(ConnectionError("x"), [item("idea", "ідея QR", title="QR")])
    results = process_pending(store, ClaudeParser(client=client))
    assert [r["status"] for r in results] == ["retry_later", "applied"]


def test_real_sdk_serialises_the_request(store):
    """With the real anthropic SDK (no network): the body it would send is what we expect."""
    anthropic = pytest.importorskip("anthropic")
    try:  # the SDK's HTTP library: httpx2 in anthropic 2.x, httpx before
        import httpx2 as httpx
    except ImportError:
        httpx = pytest.importorskip("httpx")
    sent = {}

    def handler(request):
        sent["body"] = json.loads(request.content)
        sent["beta"] = request.headers.get("anthropic-beta")
        return httpx.Response(200, json={
            "id": "msg_x", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": json.dumps({"items": [
                item("idea", "QR на етикетках", title="QR на етикетках")]})}]})

    client = anthropic.Anthropic(api_key="test", base_url="http://test",
                                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = say(store, client, "Ідея: QR на етикетках")
    assert out["status"] == "applied"
    body = sent["body"]
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in sent["beta"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "medium"
