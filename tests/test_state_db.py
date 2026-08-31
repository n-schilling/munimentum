"""state_db.py – one SQLite per export folder for the SharePoint exports."""

import sqlite3

import state_db


def test_leseoperationen_ohne_datei_liefern_leere_werte(tmp_path):
    db = state_db.StateDb(tmp_path)
    assert db.bestand_lesen() == {}
    assert db.seiten_lesen() == {}
    assert db.verschwunden_lesen() == {}
    assert db.delta_lesen() is None
    assert db.baum_lesen() is None
    assert db.bericht_lesen() is None
    # Reading must not create the file – empty folders stay empty.
    assert not (tmp_path / state_db.DB_NAME).exists()


def test_bestand_und_delta_wandern_in_einer_transaktion(tmp_path):
    db = state_db.StateDb(tmp_path)
    eintraege = {"a": {"rel": "Dateien/x.pdf", "ctag": "c1", "size": 5}}
    db.bestand_schreiben(eintraege, delta_link="link-1")
    assert db.bestand_lesen() == eintraege
    assert db.delta_lesen() == "link-1"
    # Without a link the pointer stays as it is.
    db.bestand_schreiben({}, delta_link=None)
    assert db.bestand_lesen() == {} and db.delta_lesen() == "link-1"
    db.delta_loeschen()
    assert db.delta_lesen() is None


def test_grabsteine_behalten_den_ersten_zeitpunkt(tmp_path):
    db = state_db.StateDb(tmp_path)
    db.verschwunden_ergaenzen(["a.html"], "2026-01-01T00:00:00+00:00")
    db.verschwunden_ergaenzen(["a.html", "b.html"], "2026-02-02T00:00:00+00:00")
    weg = db.verschwunden_lesen()
    assert weg["a.html"].startswith("2026-01-01")
    assert weg["b.html"].startswith("2026-02-02")


def test_baum_diff_entspricht_dem_dateibasierten_vertrag(tmp_path):
    db = state_db.StateDb(tmp_path)
    erster = db.baum_schreiben([{"id": "1", "pfad": "Dateien", "name": "D",
                                 "elemente": 0}])
    assert erster["neu"] == []                     # first sync: nothing "new"
    zweiter = db.baum_schreiben(
        [{"id": "1", "pfad": "Dateien", "name": "D", "elemente": 0},
         {"id": "2", "pfad": "Dateien/Neu", "name": "Neu", "elemente": 3}],
        vorher=db.baum_lesen())
    assert zweiter["neu"] == ["Dateien/Neu"]
    assert db.baum_lesen()["ordner"][1]["pfad"] == "Dateien/Neu"


def test_dbbestand_traegt_die_bestandsschnittstelle(tmp_path):
    db = state_db.StateDb(tmp_path)
    bestand = state_db.DbBestand(db)
    bestand.merke("a", "Dateien/x.pdf", "c1", 3)
    (tmp_path / "Dateien").mkdir()
    (tmp_path / "Dateien" / "x.pdf").write_bytes(b"xxx")
    assert bestand.aktuell("a", "c1", 3, tmp_path)
    assert not bestand.aktuell("a", "c2", 3, tmp_path)
    bestand.schreibe(delta_link="link-9")
    frisch = state_db.DbBestand(db)
    assert frisch.eintraege["a"]["rel"] == "Dateien/x.pdf"
    assert db.delta_lesen() == "link-9"


def test_kaputte_datei_bricht_leser_kontrolliert(tmp_path):
    (tmp_path / state_db.DB_NAME).write_bytes(b"kein sqlite")
    db = state_db.StateDb(tmp_path)
    try:
        db.bestand_lesen()
    except sqlite3.DatabaseError:
        pass                                   # klar gemeldet, kein Halbwissen


def test_spiegel_lauf_hinterlaesst_genau_eine_zustandsdatei(tmp_path):
    """The full mirror run against the DB backend: one state.db, no loose
    dateien.tsv/delta.txt/folders.json anywhere."""
    import drive_mirror

    class FakeGraph:
        def delta(self, weiter=None):
            yield {"id": "f1", "name": "a.pdf", "file": {}, "size": 1,
                   "cTag": "c1",
                   "parentReference": {"path": "/drive/root:"}}, None
            yield None, "delta-42"

        def lade(self, item_id, ziel, geaendert=None):
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_bytes(b"x")
            return 1

    zustand = state_db.DbZustand(tmp_path)
    zahlen = drive_mirror.lauf(FakeGraph(), tmp_path, drive_mirror.Selection(),
                               1, still=True, zustand=zustand)
    assert zahlen["new"] == 1
    db = state_db.StateDb(tmp_path)
    assert db.delta_lesen() == "delta-42"
    assert list(db.bestand_lesen().values())[0]["rel"] == "Dateien/a.pdf"
    dateien = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert dateien == {"a.pdf", state_db.DB_NAME}
