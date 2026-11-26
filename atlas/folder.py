"""Folder mode: messages arrive as files (e.g. iCloud Drive written by an iPhone Shortcut).

    ATLAS_DIR/inbox/<id>.txt      written by the phone; <id> is the unique message id
    ATLAS_DIR/outbox/<id>.txt     Atlas's answer (what it wrote down)
    ATLAS_DIR/inbox/processed/    handled files are moved here, never deleted

Works when the Mac was asleep: files wait in iCloud and are handled on wake.
Run:  python3 -m atlas.folder --dir ~/Library/Mobile\\ Documents/com~apple~CloudDocs/Atlas
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .core import Store
from .pipeline import MAX_ATTEMPTS, process_message
from .reply import describe
from .transcribe import AUDIO_SUFFIXES, NotReady, TranscribeError

log = logging.getLogger("atlas.folder")
SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
SETTLE_SECONDS = 2.0   # a file younger than this may still be being written
GIVE_UP_AUDIO = 30 * 60   # keep retrying an unreadable audio file this long before setting it aside


def _icloud_download(path: Path) -> None:
    """Ask iCloud to fetch the file's content. A background agent opening a not-yet-downloaded
    file only sees an empty/unreadable file, so the download has to be requested explicitly."""
    brctl = shutil.which("brctl")
    if not brctl:
        return
    try:
        subprocess.run([brctl, "download", str(path)], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("brctl download %s: %s", path.name, exc)


class FolderInbox:
    def __init__(self, root: str | os.PathLike, db: str, parser: Any, clock=time.time,
                 inbox: str | os.PathLike | None = None, outbox: str | os.PathLike | None = None,
                 transcriber: Any = None, downloader: Any = None):
        self.root = Path(root).expanduser()
        self.inbox = Path(inbox).expanduser() if inbox else self.root / "inbox"
        self.outbox = Path(outbox).expanduser() if outbox else self.root / "outbox"
        self.done = self.inbox / "processed"
        for d in (self.inbox, self.outbox, self.done):
            d.mkdir(parents=True, exist_ok=True)
        self.db, self.parser, self.clock = db, parser, clock
        self.next_try: dict[int, float] = {}
        self.transcriber = transcriber
        self.downloader = downloader or _icloud_download
        self.audio_failures: dict[str, tuple[int, float]] = {}   # id -> (failures, first failure)
        self.sizes: dict[str, int] = {}   # audio size seen on the previous pass

    def _request_downloads(self) -> None:
        """iCloud keeps not-yet-downloaded files as '.name.txt.icloud'; ask macOS to fetch them."""
        brctl = shutil.which("brctl")
        for ph in self.inbox.glob(".*.icloud"):
            if brctl:
                subprocess.run([brctl, "download", str(ph)], capture_output=True, timeout=30)

    def _ready_files(self) -> list[Path]:
        now = self.clock()
        files = [p for p in self.inbox.iterdir()
                 if p.is_file() and not p.name.startswith(".")
                 and (p.suffix.lower() == ".txt" or p.suffix.lower() in AUDIO_SUFFIXES)
                 and now - p.stat().st_mtime >= SETTLE_SECONDS
                 and self._size_settled(p)]
        return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))

    def _size_settled(self, p: Path) -> bool:
        """Audio from iCloud can show up before it is complete: take it only once its size
        is the same on two passes in a row."""
        if p.suffix.lower() not in AUDIO_SUFFIXES:
            return True
        size = p.stat().st_size
        previous = self.sizes.get(p.name)
        self.sizes[p.name] = size
        return size > 0 and previous == size

    def poll(self) -> list[dict[str, Any]]:
        """One pass: take new files, then retry messages that failed earlier."""
        self._request_downloads()
        results = []
        store = Store(self.db)
        try:
            for path in self._ready_files():
                ext_id = SAFE_ID.sub("_", path.stem)[:200]
                heard = ""
                if path.suffix.lower() in AUDIO_SUFFIXES:
                    text = self._transcribe(path, ext_id)
                    if text is None:          # failed: file stays for a retry, or was set aside
                        continue
                    self.sizes.pop(path.name, None)
                    if not text:
                        self._finish(path, ext_id, "Не почув слів: запис порожній або дуже тихий. "
                                                   "Нічого не записав.")
                        continue
                    heard = f"Почув: «{text}»\n"
                else:
                    text = path.read_text(encoding="utf-8", errors="replace").strip()
                if not text:
                    self._finish(path, ext_id, "Порожнє повідомлення, нічого не записав.")
                    continue
                inbox_id, _ = store.ingest("icloud", ext_id, text)   # durable from here on
                results.append(self._run(store, inbox_id, ext_id, heard))
                shutil.move(str(path), self.done / path.name)          # never deleted
            for msg in store.pending_inbox(max_attempts=MAX_ATTEMPTS):
                if self.clock() >= self.next_try.get(msg["id"], 0):
                    results.append(self._run(store, msg["id"], msg["external_id"]))
        finally:
            store.close()
        return results

    def _transcribe(self, path: Path, ext_id: str) -> str | None:
        if self.transcriber is None:
            self._finish(path, ext_id, "Аудіо отримав, але розпізнавання голосу не налаштоване.")
            return None
        try:
            self.downloader(path)   # a file from the iPhone may be only a placeholder so far
            text = self.transcriber.transcribe(path)
        except TranscribeError as exc:
            n, first = self.audio_failures.get(ext_id, (0, self.clock()))
            n += 1
            self.audio_failures[ext_id] = (n, first)
            waited = self.clock() - first
            log.warning("transcribe %s failed (%s, %.0fs): %s", path.name, n, waited, exc)
            # a file still arriving from iCloud only gives up after a long wait; a real error
            # after 3 tries spread over at least 5 minutes
            limit = GIVE_UP_AUDIO if isinstance(exc, NotReady) else 5 * 60
            if n >= 3 and waited >= limit:
                self.audio_failures.pop(ext_id, None)
                self.sizes.pop(path.name, None)
                self._finish(path, ext_id, "Не вдалося розпізнати аудіо. Файл збережено в processed.")
            return None
        self.audio_failures.pop(ext_id, None)
        return text

    def _run(self, store: Store, inbox_id: int, ext_id: str, heard: str = "") -> dict[str, Any]:
        try:
            result = process_message(store, self.parser, inbox_id)
        except Exception as exc:  # keep going with other messages
            log.exception("inbox %s", inbox_id)
            store.mark_failed(inbox_id, f"{type(exc).__name__}: {exc}")
            result = {"inbox_id": inbox_id, "status": "retry_later", "error": str(exc)}
        if result["status"] == "retry_later":
            attempts = (store.inbox(inbox_id) or {}).get("attempts", 1)
            self.next_try[inbox_id] = self.clock() + 60 * 2 ** attempts
        else:
            self.next_try.pop(inbox_id, None)
        self._write_reply(ext_id, heard + describe(result, store))
        return result

    def _write_reply(self, ext_id: str, text: str) -> None:
        tmp = self.outbox / f".{ext_id}.tmp"
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, self.outbox / f"{ext_id}.txt")   # atomic: phone never sees half a file

    def _finish(self, path: Path, ext_id: str, reply: str) -> None:
        self._write_reply(ext_id, reply)
        shutil.move(str(path), self.done / path.name)

    def run_forever(self, interval: float = 20.0, backup_dir: Path | None = None) -> None:
        log.info("watching %s", self.inbox)
        last_backup = 0.0
        while True:
            try:
                self.poll()
                if backup_dir and self.clock() - last_backup > 24 * 3600:
                    from .backup import backup
                    log.info("backup %s", backup(self.db, str(backup_dir), keep=30))
                    last_backup = self.clock()
            except Exception:
                log.exception("poll failed")
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    icloud = "~/Library/Mobile Documents/com~apple~CloudDocs/Atlas"
    p = argparse.ArgumentParser(prog="atlas-folder")
    p.add_argument("--dir", default=os.environ.get("ATLAS_DIR", icloud))
    p.add_argument("--inbox", help="incoming folder (default DIR/inbox)")
    p.add_argument("--outbox", help="replies folder (default DIR/outbox)")
    p.add_argument("--db", default=os.environ.get("ATLAS_DB", "~/Atlas/atlas.db"))
    p.add_argument("--interval", type=float, default=20.0)
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db = Path(args.db).expanduser()
    db.parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ATLAS_BACKEND", "claude-code") == "api":
        from .understand import ClaudeParser
        parser = ClaudeParser()
    else:
        from .understand import ClaudeCodeParser
        parser = ClaudeCodeParser(model=os.environ.get("ATLAS_CC_MODEL", "opus"))
    transcriber = None
    if os.environ.get("ATLAS_WHISPER_MODEL"):
        from .transcribe import WhisperCpp
        transcriber = WhisperCpp()
    box = FolderInbox(args.dir, str(db), parser, inbox=args.inbox, outbox=args.outbox,
                      transcriber=transcriber)
    if args.once:
        for r in box.poll():
            print(r.get("inbox_id"), r.get("status"))
        return 0
    box.run_forever(args.interval, backup_dir=db.parent / "backups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
