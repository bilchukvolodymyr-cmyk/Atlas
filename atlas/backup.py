"""Consistent copy of the live database + rotation.  python3 -m atlas.backup DB DIR --keep 30"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .core import Store


def backup(db: str, dest_dir: str, keep: int = 30) -> Path:
    out_dir = Path(dest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"atlas-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
    store = Store(db)
    try:
        store.backup(dest)
    finally:
        store.close()
    check = sqlite3.connect(dest)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit(f"backup {dest} failed integrity check")
    finally:
        check.close()
    copies = sorted(out_dir.glob("atlas-*.db"))
    for old in copies[:-keep] if keep > 0 else []:
        old.unlink()
    return dest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="atlas-backup")
    p.add_argument("db")
    p.add_argument("dest_dir")
    p.add_argument("--keep", type=int, default=30)
    a = p.parse_args(argv)
    print(backup(a.db, a.dest_dir, a.keep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
