"""migrate_state.py – the loose state files move into state.db once.

The order under test is the safety net: the database is written first, the
legacy files are renamed to *.bak only afterwards – so a crash in between
leaves the loose files as the source of truth and the migration simply runs
again.
"""

import json

import migrate_state
import state_db


def _baum(pfad):
    return {"ordner": [{"id": "1", "pfad": pfad, "name": pfad.split("/")[-1],
                        "elemente": 1}],
            "abgeglichen": "2026-01-01T00:00:00+00:00",
            "neu": [], "verschwunden": [], "umbenannt": []}


def _outlook_altbestand(ordner):
    ordner.mkdir(parents=True, exist_ok=True)
    (ordner / "exported.tsv").write_text(
        "m1\ta.eml\nm2\tb.eml\nm1\tneu.eml\n", encoding="utf-8")
    (ordner / "verschwunden.tsv").write_text(
        "E-Mail/weg.eml\t2026-01-01\n", encoding="utf-8")
    (ordner / "folders.json").write_text(
        json.dumps(_baum("E-Mail/A")), encoding="utf-8")
    (ordner / "calendars.json").write_text(
        json.dumps(_baum("kalender/K")), encoding="utf-8")
    (ordner / "vollstaendigkeit.json").write_text(
        json.dumps({"erwartet": 3, "ordner": []}), encoding="utf-8")


def test_noetig_erkennt_nur_alte_bestaende(tmp_path):
    outlook, teams, onedrive = (tmp_path / "o", tmp_path / "t", tmp_path / "d")
    assert migrate_state.noetig(outlook, teams, onedrive) == []
    _outlook_altbestand(outlook)
    teams.mkdir()
    (teams / "export_state.json").write_text("{}", encoding="utf-8")
    # OneDrive already on state.db: not due.
    state_db.StateDb(onedrive).kv_schreiben("delta", "x")
    faellig = migrate_state.noetig(outlook, teams, onedrive)
    assert [name for name, _ in faellig] == ["outlook", "teams"]


def test_outlook_migration_uebernimmt_alles_und_raeumt_weg(tmp_path):
    _outlook_altbestand(tmp_path)
    import folders
    n = migrate_state._outlook(tmp_path)
    db = state_db.StateDb(tmp_path)
    # Doppelte Mail-ID: die letzte Zeile gilt, wie beim angehängten TSV.
    log = state_db.DbDoneLog(db)
    assert log.done == {"m1": "neu.eml", "m2": "b.eml"}
    log.close()
    assert db.verschwunden_lesen() == {"E-Mail/weg.eml": "2026-01-01"}
    assert folders.lade(tmp_path)["ordner"][0]["pfad"] == "E-Mail/A"
    assert folders.lade(tmp_path, folders.KALENDER)["ordner"][0]["pfad"] == \
        "kalender/K"
    assert db.bericht_lesen()["erwartet"] == 3
    assert n >= 3
    # Weggeräumt heißt umbenannt, nicht gelöscht.
    assert not (tmp_path / "exported.tsv").exists()
    assert (tmp_path / "exported.tsv.bak").exists()
    assert (tmp_path / "verschwunden.tsv.bak").exists()


def test_teams_migration(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    state = {"version": 1, "conversations": {"k": {"done": True,
                                                   "rel": "1on1/a.html"}}}
    (tmp_path / "export_state.json").write_text(json.dumps(state),
                                                encoding="utf-8")
    migrate_state._teams(tmp_path)
    import teams_export as te
    assert te.load_state(tmp_path) == state
    assert (tmp_path / "export_state.json.bak").exists()


def test_onedrive_migration_mit_walk(tmp_path):
    (tmp_path / "dateien.tsv").write_text(
        "id1\tDateien/a.pdf\tc1\t10\n", encoding="utf-8")
    (tmp_path / "delta.txt").write_text("delta-7", encoding="utf-8")
    (tmp_path / "verschwunden.tsv").write_text(
        "Dateien/weg.pdf\t2026-02-02\n", encoding="utf-8")
    (tmp_path / "folders.json").write_text(json.dumps(_baum("Dateien/A")),
                                           encoding="utf-8")
    (tmp_path / "walk.jsonl").write_text('{"id": "w1"}\n{"kaputt', encoding="utf-8")
    (tmp_path / "walk_cursor.txt").write_text("seite-3", encoding="utf-8")
    migrate_state._onedrive(tmp_path)
    db = state_db.StateDb(tmp_path)
    assert db.bestand_lesen()["id1"]["rel"] == "Dateien/a.pdf"
    assert db.delta_lesen() == "delta-7"
    assert db.verschwunden_lesen() == {"Dateien/weg.pdf": "2026-02-02"}
    status = db.walk_status()
    assert status == {"cursor": "seite-3", "fertig": None, "n": 1}
    assert [e["id"] for e in db.walk_eintraege()] == ["w1"]
    assert not (tmp_path / "dateien.tsv").exists()
    assert (tmp_path / "delta.txt.bak").exists()


def test_wiederholung_nach_abbruch_ist_harmlos(tmp_path):
    """Crash before the rename: the loose files are still there, and running
    the migration again just overwrites the half-written database."""
    _outlook_altbestand(tmp_path)
    migrate_state._outlook(tmp_path)
    # Simulierter Abbruch VOR dem Wegräumen: die Altdateien liegen wieder da.
    for name in ("exported.tsv", "verschwunden.tsv"):
        (tmp_path / (name + ".bak")).replace(tmp_path / name)
    migrate_state._outlook(tmp_path)
    db = state_db.StateDb(tmp_path)
    log = state_db.DbDoneLog(db)
    assert log.done == {"m1": "neu.eml", "m2": "b.eml"}
    log.close()
    assert db.verschwunden_lesen() == {"E-Mail/weg.eml": "2026-01-01"}


def test_lauf_meldet_je_bestand(tmp_path):
    _outlook_altbestand(tmp_path / "o")
    gemeldet = []
    ergebnis = migrate_state.lauf([("outlook", tmp_path / "o")],
                                  melde=lambda name, n: gemeldet.append(name))
    assert gemeldet == ["outlook"] and ergebnis["outlook"] >= 3
