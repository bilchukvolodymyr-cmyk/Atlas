"""Mail mode: Gmail read only, exact text from tool results, one email = one record."""
import json
from types import SimpleNamespace

import pytest

from atlas.core import Store
from atlas.mail import (ClaudeCodeGmail, MailError, MailPoller, WRITE_TOOLS, clean_body,
                        format_email, tool_results)
from atlas.understand import ClaudeParser
from test_stage1 import FakeClient, item

P = "mcp__claude_ai_Gmail__"
ME = "me@example.com"


def msg(mid, thread, sender, body, subject="Test", ts=1795777401000, labels=("INBOX",), to=(ME,)):
    return {"id": mid, "threadId": thread, "sender": sender, "toRecipients": list(to),
            "subject": subject, "snippet": body[:100], "plaintextBody": body,
            "internalDate": str(ts), "labelIds": list(labels),
            "addressDisplayNames": [{"displayName": "Luigi Apicella", "emailAddress": "luigi@work.it"},
                                    {"displayName": "Volodymyr Bilchuk", "emailAddress": ME}]}


def stream(calls, structured=None):
    """A fake `claude -p --output-format stream-json` transcript."""
    lines = [{"type": "system", "subtype": "init"}]
    for i, (name, result) in enumerate(calls):
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": name, "input": {}}]}})
        lines.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": json.dumps(result)}]}})
    lines.append({"type": "result", "is_error": False, "result": "DONE",
                  "structured_output": structured})
    return "\n".join(json.dumps(l) for l in lines)


class Runner:
    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []

    def __call__(self, cmd, input, **kw):
        self.calls.append((cmd, input))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def summary(*threads):
    return {"threads": [{"id": t, "messageCount": 1, "messages": [m]} for t, m in threads]}


def test_quoted_reply_and_disclaimer_are_cut():
    body = ("Fatto\n\nпн, 23 лист. 2026 р. о 17:56 Luigi Apicella <luigi@work.it>\nпише:\n\n"
            "> Ciao, le ubichi devono partire.\n>\n> Nota di riservatezza\n")
    assert clean_body(body, "Re: da ubicare") == "Fatto"
    it = ("Buongiorno Luigi,\nin allegato il file.\n\nIl giorno gio 26 nov 2026 alle ore 14:23 "
          "Luigi <luigi@work.it> ha scritto:\nCiao, programma di domani")
    assert clean_body(it, "Re: programma") == "Buongiorno Luigi,\nin allegato il file."
    disc = ("Verificate il cartello.\n[cid:image001.jpg@01]\nFintyre S.p.A.\n\nNota di riservatezza\n"
            "Se il presente messaggio non è destinato a Lei...\n\nConfidentiality notice\nIf this...")
    assert clean_body(disc) == "Verificate il cartello.\n\nFintyre S.p.A."


def test_forward_keeps_the_forwarded_message():
    body = ("Ciao, il carrello P3.36 non deve essere usato.\n\nDa: Luigi\nInviato: venerdì 12:01\n"
            "Oggetto: Danni\n\nCARRELLI P3.36 vetro rotto")
    out = clean_body(body, "I: Danni da uso improprio")
    assert "vetro rotto" in out and "Inviato" in out


def test_format_email_header_and_direction():
    m = msg("m2", "t1", ME, "Fatto", subject="Re: da ubicare", labels=("SENT",), to=("luigi@work.it",))
    prev = msg("m1", "t1", "luigi@work.it", "Ciao Bilciuck, le ubichi anche sul polo 1 devono partire.")
    text = format_email(m, prev)
    assert text.startswith("[ЛИСТ] надіслав я\nВід: Volodymyr Bilchuk <me@example.com>")
    assert "Кому: Luigi Apicella <luigi@work.it>" in text
    assert "Попередній лист у цій розмові (від Luigi Apicella" in text
    assert text.endswith("---\nFatto")


def test_tool_results_reads_the_tool_output_not_the_model_text():
    out = stream([("ToolSearch", {"x": 1}), (P + "search_threads", {"threads": [{"id": "a"}]})])
    assert tool_results(out, P + "search_threads") == [{"threads": [{"id": "a"}]}]
    bad = out.replace('"tool_use_id": "t1"', '"tool_use_id": "t1", "is_error": true')
    with pytest.raises(MailError):
        tool_results(bad, P + "search_threads")


def test_only_read_tools_allowed_and_every_write_tool_forbidden():
    r = Runner(stream([(P + "search_threads", summary())]))
    ClaudeCodeGmail(runner=r).search("after:1")
    cmd = r.calls[0][0]
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert allowed == [P + "search_threads"]
    assert all(P + t in denied for t in WRITE_TOOLS) and "Bash" in denied
    assert P + "send_message" in denied and P + "trash_thread" in denied


def make_poller(tmp_path, runner, *parsed):
    gmail = ClaudeCodeGmail(runner=runner)
    parser = ClaudeParser(client=FakeClient(*parsed))
    return MailPoller(tmp_path / "a.db", gmail, parser, clock=lambda: 1795780000), parser


def test_end_to_end_relevant_mail_becomes_task_and_ad_is_skipped(tmp_path):
    work = msg("w1", "tw", "luigi@work.it",
               "Ciao Bilciuck, per favore verificate che ci sia un cartello da non utilizzare.",
               subject="I: Danni")
    ad = msg("a1", "ta", "promo@shop.it", "Black Friday -50%", subject="Offerte")
    runner = Runner(
        stream([(P + "search_threads", summary(("tw", work), ("ta", ad)))]),
        stream([(P + "get_thread", {"id": "tw", "messages": [work]})],
               {"threads": [{"threadId": "tw", "relevant": True, "reason": "доручення"},
                            {"threadId": "ta", "relevant": False, "reason": "реклама"}]}),
        stream([(P + "search_threads", summary(("tw", work), ("ta", ad)))]),
    )
    poller, parser = make_poller(tmp_path, runner, [item(
        "new_loop", "verificate che ci sia un cartello da non utilizzare",
        title="Перевірити табличку «не використовувати»", kind="task",
        assigned_by="Luigi Apicella")])
    r = poller.poll()
    assert (r["new_threads"], r["relevant"], r["stored"]) == (2, 1, 1)
    s = Store(str(tmp_path / "a.db"))
    [row] = s.conn.execute("SELECT * FROM inbox").fetchall()
    assert row["source"] == "gmail" and row["external_id"] == "w1" and row["status"] == "done"
    assert "verificate che ci sia un cartello" in row["raw_text"]
    [loop] = s.open_loops()
    assert loop["assigned_by"] == "Luigi Apicella"
    s.close()
    # second pass: nothing new -> only the cheap search, no triage, no parser call
    r2 = poller.poll()
    assert r2["new_threads"] == 0 and len(runner.calls) == 3
    assert len(parser.client.calls) == 1
    # the triage call itself was told to read only relevant threads with the read tool
    triage_cmd = runner.calls[1][0]
    assert triage_cmd[triage_cmd.index("--allowedTools") + 1] == P + "get_thread"


def test_lost_ledger_never_duplicates(tmp_path):
    work = msg("w1", "tw", "luigi@work.it", "Ciao Bilciuck, controlla la ribalta 31.")
    def outputs():
        return [stream([(P + "search_threads", summary(("tw", work)))]),
                stream([(P + "get_thread", {"id": "tw", "messages": [work]})],
                       {"threads": [{"threadId": "tw", "relevant": True, "reason": "робота"}]})]
    runner = Runner(*outputs(), *outputs())
    poller, _ = make_poller(tmp_path, runner, [], [])
    poller.poll()
    (tmp_path / "mail.db").unlink()
    poller2 = MailPoller(tmp_path / "a.db", poller.gmail, poller.parser, clock=lambda: 1795780000)
    poller2.poll()
    s = Store(str(tmp_path / "a.db"))
    assert s.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
    s.close()


def test_gmail_failure_marks_nothing_and_retries(tmp_path):
    work = msg("w1", "tw", "luigi@work.it", "Ciao Bilciuck, controlla la ribalta 31.")
    runner = Runner(OSError("no network"),
                    stream([(P + "search_threads", summary(("tw", work)))]),
                    stream([(P + "get_thread", {"id": "tw", "messages": [work]})],
                           {"threads": [{"threadId": "tw", "relevant": True, "reason": "робота"}]}))
    poller, _ = make_poller(tmp_path, runner, [])
    with pytest.raises(MailError):
        poller.poll()
    assert poller.ledger.get("cursor") is None
    assert poller.poll()["stored"] == 1


def test_relevant_thread_not_fetched_is_retried(tmp_path):
    work = msg("w1", "tw", "luigi@work.it", "Ciao Bilciuck, controlla la ribalta 31.")
    search = stream([(P + "search_threads", summary(("tw", work)))])
    verdict = {"threads": [{"threadId": "tw", "relevant": True, "reason": "робота"}]}
    runner = Runner(search, stream([], verdict), search,
                    stream([(P + "get_thread", {"id": "tw", "messages": [work]})], verdict))
    poller, _ = make_poller(tmp_path, runner, [])
    assert poller.poll()["stored"] == 0
    assert poller.poll()["stored"] == 1


def test_old_messages_of_a_thread_before_the_window_are_not_imported(tmp_path):
    old = msg("o1", "tw", "luigi@work.it", "vecchio", ts=(1795780000 - 30 * 86400) * 1000)
    new = msg("n1", "tw", ME, "Fatto", labels=("SENT",), to=("luigi@work.it",))
    runner = Runner(stream([(P + "search_threads", summary(("tw", new)))]),
                    stream([(P + "get_thread", {"id": "tw", "messages": [old, new]})],
                           {"threads": [{"threadId": "tw", "relevant": True, "reason": "відповідь"}]}))
    poller, _ = make_poller(tmp_path, runner, [])
    assert poller.poll()["stored"] == 1
    s = Store(str(tmp_path / "a.db"))
    [row] = s.conn.execute("SELECT raw_text FROM inbox").fetchall()
    assert "«vecchio»" in row[0]          # older email only as context of the reply
    s.close()


def test_folder_service_leaves_gmail_messages_alone(tmp_path):
    import time
    from atlas.folder import FolderInbox
    s = Store(str(tmp_path / "a.db"))
    s.ingest("gmail", "g1", "[ЛИСТ] вхідний\n---\nCiao")
    s.close()
    client = FakeClient()
    box = FolderInbox(tmp_path / "Atlas", str(tmp_path / "a.db"), ClaudeParser(client=client),
                      clock=lambda: time.time() + 10, downloader=lambda p: None)
    assert box.poll() == [] and client.calls == []
    assert list(box.outbox.iterdir()) == []
