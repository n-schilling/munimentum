"""Tests für export_util.py – die geteilten Helfer der Exportskripte.

Vieles läuft ohnehin über die Aliasse in den Export-Testdateien mit; hier
stehen nur die Verträge, die kein Skript-Test von selbst abdeckt.
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


def test_alte_verschwunden_datei_wird_fuer_die_migration_gelesen(tmp_path):
    """Der Legacy-Leser wohnt in migrate_state – der einzige Code, der die
    Dateien von vor 6.2 noch anfasst."""
    import migrate_state
    pfad = tmp_path / "verschwunden.tsv"
    pfad.write_text("a.eml\t2025-01-01\nb.eml\t2025-02-02\nkaputt\n",
                    encoding="utf-8")
    assert migrate_state._lies_verschwunden(pfad) == {
        "a.eml": "2025-01-01", "b.eml": "2025-02-02"}
    assert migrate_state._lies_verschwunden(tmp_path / "fehlt.tsv") == {}


def test_safe_und_kuerzel():
    assert export_util.safe('a\\b/c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert export_util.safe("") == "unbenannt"
    assert len(export_util.kuerzel("x")) == 8
    assert export_util.kuerzel("x") != export_util.kuerzel("y")
