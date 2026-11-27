"""Programme of unloadings (mail) and people per day (shift sheet): counted by code."""
import json

from atlas.core import Store
from atlas.turni import TurniSync, count_day, registry, tab_date
from atlas.volumes import parse_programme, record

PROG = """Ciao, programma di domani.

26/11/2026
polo 1

SUPPLIER A
aaa
1359
38
consumer

6:00

gio
26/11/2026
polo 1
CARRIER
SUPPLIER B
bbb
  1.000

consumer
10:00

gio
26/11/2026

gio
26/11/2026
polo 2

SUPPLIER C
ccc
870
15
6:00
"""


def test_programme_counts_arrivals_and_pieces_per_site():
    p = parse_programme(PROG)
    assert p["days"] == {"2026-11-26": {"polo1": {"arrivals": 2, "pieces": 2359},
                                        "polo2": {"arrivals": 1, "pieces": 870}}}
    assert p["navette_out"] is None
    assert parse_programme("Cao, navette out 16 ingressi:\n" + PROG)["navette_out"] == 16


def test_not_a_programme_and_quoted_programme_are_ignored():
    assert parse_programme("Buongiorno, in allegato il file") is None
    assert parse_programme("Fatto\n" + "\n".join("> " + l for l in PROG.split("\n"))) is None


def test_later_programme_for_same_day_replaces_numbers(tmp_path):
    s = Store(str(tmp_path / "a.db"))
    record(s, "m1", PROG, "Luigi")
    record(s, "m1", PROG, "Luigi")                       # same mail again: nothing new
    fewer = PROG.split("gio\n26/11/2026\npolo 1\nCARRIER")[0]
    record(s, "m2", fewer + "\n26/11/2026\npolo 2\nX\nx\n500\n", "Roberto")
    rows = {(r["site"], r["name"]): r["value"] for r in
            s.conn.execute("SELECT site, name, value FROM metrics WHERE date='2026-11-26'")}
    assert rows[("all", "trucks_unload")] == 2 and rows[("polo1", "unload_pieces")] == 1359
    assert s.conn.execute("SELECT COUNT(*) FROM inbox WHERE source='programma'").fetchone()[0] == 2
    s.close()


REG = [["ID", "DIPENDENTE", "TIPO PERSONALE"], ["", "ROSSI  MARIO", "AGENZIA"],
       ["", "BIANCHI LUCA", "COOPERATIVA"], ["", "VERDI, ANNA", "COOPERATIVA"]]
DAY = [["d", "", "persone totale", "4"], [], ["", "", "POLO 1", "3"], ["", "", "POLO 2", "1"],
       ["", "", "POLO 3", "0"], [], [],
       ["", "", "Piano Risorse", "", "", "POLO 1", "", "", "Venerdì"],
       ["", "", "TURNO da 06:00 a 14:00", "", "", "TURNO CENTRALE"],
       ["", "", "ROSSI MARIO", "NUOVO", "", "BIANCHI  LUCA"], ["", "", "NERI PAOLO"],
       ["", "", "2", "", "", "1"],
       ["", "", "Piano Risorse", "", "", "POLO 2"], ["", "", "VERDI ANNA"]]


def test_count_day_uses_register_and_never_guesses():
    c = count_day(DAY, registry(REG))
    assert c["totals"] == {"all": 4, "polo1": 3, "polo2": 1, "polo3": 0}
    assert c["people"]["polo1"] == {"agenzia": 1, "cooperativa": 1, "unknown": 1}
    assert c["people"]["polo2"] == {"agenzia": 0, "cooperativa": 1, "unknown": 0}
    assert tab_date("27.11.2026") == "2026-11-27" and tab_date("1") is None


class Reader:
    def __init__(self):
        self.reads = 0

    def titles(self, sid):
        return ["GUIDA", "26.11.2026", "27.11.2026", "DIPENDENTI"]

    def values(self, sid, ranges):
        self.reads += 1
        return {r: (REG if "DIPENDENTI" in r else DAY) for r in ranges}


def test_turni_sync_is_idempotent(tmp_path):
    db = str(tmp_path / "a.db")
    sync = TurniSync(db, Reader(), "sid")
    assert sync.sync()["recorded"] == 2
    assert sync.sync()["recorded"] == 0                  # unchanged tabs: nothing new
    s = Store(db)
    assert s.conn.execute("SELECT COUNT(*) FROM inbox WHERE source='turni'").fetchone()[0] == 2
    v = {(r[0], r[1], r[2]): r[3] for r in s.conn.execute("SELECT date, site, name, value FROM metrics")}
    assert v[("2026-11-27", "all", "workers_total")] == 4
    assert v[("2026-11-27", "all", "workers_agenzia")] == 1
    assert v[("2026-11-27", "all", "workers_unknown_type")] == 1
    s.close()
