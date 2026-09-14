"""Tests for export_util.py – the helpers shared by the export scripts.

Much of it is exercised anyway via the aliases in the export test files;
only the contracts no script test covers on its own live here.
"""

from datetime import UTC, datetime

import export_util


def test_hilfe_gewuenscht_kennt_alle_schreibweisen():
    for form in ("-h", "--help", "-help", "help"):
        assert export_util.hilfe_gewuenscht(["ordner", form])
    assert not export_util.hilfe_gewuenscht(["ordner", "--folders"])
    assert not export_util.hilfe_gewuenscht([])


def test_graph_zeit_kuerzt_sieben_stellige_bruchteile():
    dt = export_util.graph_zeit("2025-06-01T09:30:00.1234567Z")
    assert dt == datetime(2025, 6, 1, 9, 30, 0, 123456, tzinfo=UTC)


def test_graph_zeit_wirft_nie():
    assert export_util.graph_zeit(None) is None
    assert export_util.graph_zeit("") is None
    assert export_util.graph_zeit("unsinn") is None
    assert export_util.graph_zeit(42) is None


def test_schreibe_atomar_legt_ordner_an_und_laesst_kein_tmp(tmp_path):
    ziel = tmp_path / "tief" / "datei.txt"
    export_util.schreibe_atomar(ziel, "inhalt")
    assert ziel.read_text(encoding="utf-8") == "inhalt"
    assert not ziel.with_name(ziel.name + ".tmp").exists()


def test_safe_und_kuerzel():
    assert export_util.safe('a\\b/c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert export_util.safe("") == "unbenannt"
    assert len(export_util.kuerzel("x")) == 8
    assert export_util.kuerzel("x") != export_util.kuerzel("y")


def test_kadenz_fuer_nimmt_den_tiefsten_pfad_sonst_die_kategorie():
    kad = {"outlook:mail": "daily", "outlook:mail:E-Mail/Archiv": "monthly",
           "outlook:mail:E-Mail/Archiv/2025/Q1": "always",
           "teams:channels": "weekly", "teams:channels/Nordwind": "monthly"}
    f = export_util.kadenz_fuer
    assert f(kad, "outlook:mail", "E-Mail/Posteingang") == "daily"
    assert f(kad, "outlook:mail", "E-Mail/Archiv") == "monthly"
    assert f(kad, "outlook:mail", "E-Mail/Archiv/2024") == "monthly"       # inherited
    assert f(kad, "outlook:mail", "E-Mail/Archiv/2025/Q1") == "always"     # deeper wins
    assert f(kad, "outlook:mail", "E-Mail/Archiv/2025/Q1/x") == "always"
    assert f(kad, "outlook:mail", "E-Mail/Archivar") == "daily"            # no prefix by name
    assert f(kad, "teams:1on1", "1on1/Alice Beispiel") == "always"         # nothing set
    assert f(kad, "teams", "channels/Nordwind/Releases") == "monthly"
    assert f(kad, "teams", "channels/Vertrieb/Allgemein") == "weekly"
    assert f(kad, "teams", "group/Projekt") == "always"
    assert f({}, "outlook:mail", "E-Mail", vorgabe="weekly") == "weekly"


def test_voll_neu_liest_full_sync(monkeypatch):
    """The "Force full sync" flag – set by the app, blank means off."""
    monkeypatch.delenv("FULL_SYNC", raising=False)
    assert not export_util.voll_neu()
    monkeypatch.setenv("FULL_SYNC", "1")
    assert export_util.voll_neu()
    monkeypatch.setenv("FULL_SYNC", " ")
    assert not export_util.voll_neu()
    # A full sync lets every cadence gate step aside, SYNC_NOW or not.
    monkeypatch.delenv("SYNC_NOW", raising=False)
    assert not export_util.sync_jetzt()
    monkeypatch.setenv("FULL_SYNC", "1")
    assert export_util.sync_jetzt()


def test_abgleich_liest_resync_und_ein_vollsync_zaehlt_als_einer(monkeypatch):
    """"Fetch now" / "Fetch again": the pointers go, the versions stay.
    A full sync forgets the pointers too, so it counts as a resync; every
    cadence gate steps aside for both."""
    for name in ("RESYNC", "FULL_SYNC", "SYNC_NOW"):
        monkeypatch.delenv(name, raising=False)
    assert not export_util.abgleich() and not export_util.sync_jetzt()
    monkeypatch.setenv("RESYNC", "1")
    assert export_util.abgleich() and export_util.sync_jetzt()
    assert not export_util.voll_neu(), "a resync writes nothing current over"
    monkeypatch.setenv("RESYNC", " ")
    assert not export_util.abgleich()
    monkeypatch.setenv("FULL_SYNC", "1")
    assert export_util.abgleich()


def test_nachhol_liste_liest_die_datei_des_abgleichs(tmp_path, monkeypatch):
    """"Fetch again": the list of files travels as a JSON file named in
    FETCH_LIST – None on a regular run, empty when unreadable."""
    monkeypatch.delenv("FETCH_LIST", raising=False)
    assert export_util.nachhol_liste() is None
    liste = tmp_path / "nachholen-outlook.json"
    liste.write_text('{"quelle": "outlook", "dateien": ["E-Mail/a.eml", 7, "E-Mail/b.eml"]}',
                     encoding="utf-8")
    monkeypatch.setenv("FETCH_LIST", str(liste))
    assert export_util.nachhol_liste() == ["E-Mail/a.eml", "E-Mail/b.eml"]
    liste.write_text("kaputt", encoding="utf-8")
    assert export_util.nachhol_liste() == []
    monkeypatch.setenv("FETCH_LIST", str(tmp_path / "fehlt.json"))
    assert export_util.nachhol_liste() == []


def test_abgleich_ordner_liest_die_ordnerliste(monkeypatch):
    monkeypatch.delenv("RESYNC_FOLDERS", raising=False)
    assert export_util.abgleich_ordner() is None
    monkeypatch.setenv("RESYNC_FOLDERS", '["E-Mail/Posteingang", 3, "kalender/Arbeit"]')
    assert export_util.abgleich_ordner() == ["E-Mail/Posteingang", "kalender/Arbeit"]
    monkeypatch.setenv("RESYNC_FOLDERS", "kaputt")
    assert export_util.abgleich_ordner() is None
