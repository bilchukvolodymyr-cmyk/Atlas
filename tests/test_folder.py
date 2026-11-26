"""Folder mode (iCloud Drive): files in, answers out, nothing lost or duplicated."""
import os
import time

from atlas.core import Store
from atlas.folder import FolderInbox
from atlas.understand import ClaudeParser
from test_stage1 import FakeClient, item


def make(tmp_path, *responses):
    client = FakeClient(*responses)
    box = FolderInbox(tmp_path / "Atlas", str(tmp_path / "a.db"), ClaudeParser(client=client),
                      clock=lambda: time.time() + 10)
    return box, client


def drop(box, name, text):
    (box.inbox / name).write_text(text, encoding="utf-8")


def test_file_is_understood_answered_and_moved(tmp_path):
    box, client = make(tmp_path, [item("idea", "QR на локаціях", title="QR на локаціях")])
    drop(box, "iphone-20261126-1.txt", "Ідея: QR на локаціях")
    [r] = box.poll()
    assert r["status"] == "applied"
    assert (box.outbox / "iphone-20261126-1.txt").read_text("utf-8") == "Ідея: «QR на локаціях»\n"
    assert not (box.inbox / "iphone-20261126-1.txt").exists()
    assert (box.done / "iphone-20261126-1.txt").exists()          # kept, not deleted
    assert box.poll() == []                                       # nothing twice
    assert len(client.calls) == 1


def test_same_file_synced_again_is_not_duplicated(tmp_path):
    box, client = make(tmp_path, [item("idea", "QR", title="QR")])
    drop(box, "m1.txt", "Ідея: QR")
    box.poll()
    drop(box, "m1.txt", "Ідея: QR")                               # iCloud re-delivers
    [r] = box.poll()
    assert r["status"] == "already_applied" and len(client.calls) == 1
    s = Store(box.db)
    assert s.conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1
    s.close()


def test_file_still_being_written_is_left_alone(tmp_path):
    box, client = make(tmp_path)
    box.clock = time.time
    drop(box, "fresh.txt", "щойно")
    assert box.poll() == [] and (box.inbox / "fresh.txt").exists()


def test_claude_unavailable_then_retried(tmp_path):
    box, client = make(tmp_path, ConnectionError("offline"),
                       [item("idea", "полиця", title="Полиця")])
    drop(box, "m1.txt", "Ідея: полиця")
    [r] = box.poll()
    assert r["status"] == "retry_later"
    assert "оброблю пізніше" in (box.outbox / "m1.txt").read_text("utf-8")
    assert (box.done / "m1.txt").exists()                         # text is safe in the database
    box.next_try.clear()
    [r] = box.poll()
    assert r["status"] == "applied"
    assert (box.outbox / "m1.txt").read_text("utf-8") == "Ідея: «Полиця»\n"


def test_icloud_placeholders_and_other_files_ignored(tmp_path):
    box, client = make(tmp_path)
    drop(box, ".m2.txt.icloud", "")
    drop(box, "photo.jpg", "x")
    assert box.poll() == []
    assert client.calls == []


def test_custom_inbox_and_outbox_folders(tmp_path):
    client = FakeClient([item("idea", "QR", title="QR")])
    box = FolderInbox(tmp_path / "x", str(tmp_path / "a.db"), ClaudeParser(client=client),
                      clock=lambda: time.time() + 10,
                      inbox=tmp_path / "Shortcuts" / "Atlas", outbox=tmp_path / "Shortcuts" / "Atlas-replies")
    (tmp_path / "Shortcuts" / "Atlas" / "20261126-221500-123.txt").write_text("Ідея: QR", "utf-8")
    [r] = box.poll()
    assert r["status"] == "applied"
    assert (tmp_path / "Shortcuts" / "Atlas-replies" / "20261126-221500-123.txt").exists()
    assert (tmp_path / "Shortcuts" / "Atlas" / "processed" / "20261126-221500-123.txt").exists()


class FakeWhisper:
    def __init__(self, *results):
        self.results = list(results)

    def transcribe(self, path):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_audio_is_transcribed_then_understood(tmp_path):
    box, client = make(tmp_path, [item("new_loop", "завтра звільнити рампу 31",
                                       title="Звільнити рампу 31", due="tomorrow")])
    box.transcriber = FakeWhisper("Завтра звільнити рампу 31.")
    (box.inbox / "20261126-223000-001.m4a").write_bytes(b"audio")
    assert box.poll() == []                     # first sight: size not yet known to be stable
    [r] = box.poll()
    assert r["status"] == "applied"
    reply = (box.outbox / "20261126-223000-001.txt").read_text("utf-8")
    assert reply.startswith("Почув: «Завтра звільнити рампу 31.»\n")
    assert "Справа: «Звільнити рампу 31»" in reply
    assert (box.done / "20261126-223000-001.m4a").exists()       # audio kept


def test_audio_transcription_failure_retries_then_sets_aside(tmp_path):
    from atlas.transcribe import TranscribeError
    box, client = make(tmp_path)
    now = [time.time() + 10]
    box.clock = lambda: now[0]
    box.transcriber = FakeWhisper(*[TranscribeError("boom")] * 4)
    (box.inbox / "a1.m4a").write_bytes(b"audio")
    for _ in range(4):                                            # 1 sighting + 3 quick failures
        assert box.poll() == []
    assert (box.inbox / "a1.m4a").exists()                        # 3 fast failures: still waiting
    now[0] += 6 * 60
    box.poll()
    assert (box.done / "a1.m4a").exists()
    assert "Не вдалося розпізнати" in (box.outbox / "a1.txt").read_text("utf-8")
    assert client.calls == []


def test_audio_still_downloading_waits_long(tmp_path):
    from atlas.transcribe import NotReady
    box, client = make(tmp_path, [item("idea", "рампа 31", title="Рампа 31")])
    now = [time.time() + 10]
    box.clock = lambda: now[0]
    box.transcriber = FakeWhisper(*[NotReady("wht?")] * 5, "Ідея: рампа 31")
    (box.inbox / "a2.m4a").write_bytes(b"audio")
    box.poll()
    for _ in range(5):
        now[0] += 5 * 60                                          # 25 min of "not ready"
        assert box.poll() == [] and (box.inbox / "a2.m4a").exists()
    [r] = box.poll()
    assert r["status"] == "applied"
    assert (box.outbox / "a2.txt").read_text("utf-8").startswith("Почув: «Ідея: рампа 31»")


def test_growing_audio_file_is_not_taken(tmp_path):
    box, client = make(tmp_path)
    box.transcriber = FakeWhisper()                               # must not be called
    f = box.inbox / "a3.m4a"
    f.write_bytes(b"a")
    box.poll()
    f.write_bytes(b"ab")                                          # still arriving
    assert box.poll() == [] and f.exists()


def test_silent_audio_is_not_recorded(tmp_path):
    box, client = make(tmp_path)
    box.transcriber = FakeWhisper("")
    (box.inbox / "a4.m4a").write_bytes(b"audio")
    box.poll()
    assert box.poll() == []
    assert "запис порожній" in (box.outbox / "a4.txt").read_text("utf-8")
    assert (box.done / "a4.m4a").exists() and client.calls == []


def test_whisper_silence_and_stock_phrases(tmp_path):
    import array, wave
    from atlas.transcribe import WhisperCpp, is_hallucination
    assert is_hallucination("Дякую за перегляд!") and is_hallucination(" ... ")
    assert not is_hallucination("Дякую, рампа 31 вільна")

    def make_wav(name, seconds, amp):
        path = tmp_path / name
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(array.array("h", [amp, -amp] * int(8000 * seconds)).tobytes())
        return path
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return type("P", (), {"returncode": 0, "stdout": "Рампа 31", "stderr": ""})()
    w = WhisperCpp(binary="whisper-cli", model="/m.bin", runner=runner)
    assert w.transcribe(make_wav("quiet.wav", 2.1, 461)) == "" and calls == []
    assert w.transcribe(make_wav("short.wav", 0.3, 9000)) == "" and calls == []
    assert w.transcribe(make_wav("voice.wav", 3, 9000)) == "Рампа 31" and len(calls) == 1


def test_afconvert_cannot_open_means_not_ready(tmp_path):
    import pytest
    from atlas.transcribe import NotReady, WhisperCpp

    def runner(cmd, **kw):
        return type("P", (), {"returncode": 1, "stdout": "",
                              "stderr": "Error: Couldn't open input file ('wht?')"})()
    (tmp_path / "x.m4a").write_bytes(b"audio")
    with pytest.raises(NotReady):
        WhisperCpp(binary="w", model="/m.bin", runner=runner).transcribe(tmp_path / "x.m4a")


def test_whisper_command_line(tmp_path):
    from atlas.transcribe import WhisperCpp
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return type("P", (), {"returncode": 0, "stdout": "  Завтра  рампа 31 \n", "stderr": ""})()
    w = WhisperCpp(binary="whisper-cli", model="/m/ggml.bin", runner=runner)
    (tmp_path / "x.m4a").write_bytes(b"audio")
    assert w.transcribe(tmp_path / "x.m4a") == "Завтра рампа 31"
    assert calls[0][0] == "afconvert"
    assert calls[1][:5] == ["whisper-cli", "-m", "/m/ggml.bin", "-l", "uk"]
