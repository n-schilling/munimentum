"""Tests für onedrive_export.py – OneDrive als Spiegel.

Drei Zusagen stehen hier im Mittelpunkt, und zwei davon sind an einem echten
Laufwerk schon einmal gebrochen worden:

  * Ein abgeschnittener Dateiname darf nicht mit einem anderen kollidieren.
    Zwei Dateien, deren Namen sich erst nach 120 Zeichen unterschieden, landeten
    auf demselben Pfad – der zweite Download scheiterte an der Teildatei, die
    der erste schon weggeräumt hatte.
  * Ein abgebrochener Lauf darf den Delta-Zeiger nicht vorrücken, sonst
    verschluckt der nächste Lauf alle Änderungen dazwischen.
  * Gelöscht in OneDrive heißt vermerkt, nicht weggeworfen.
"""

import pytest

import folders
import onedrive_export as od


def _datei(kennung, name, pfad="/drive/root:/Ordner", groesse=10, ctag="c1", **extra):
    return {"id": kennung, "name": name, "size": groesse, "cTag": ctag,
            "file": {"mimeType": "application/pdf"},
            "parentReference": {"path": pfad}, **extra}


# --------------------------------------------------------------------------
# Pfade
# --------------------------------------------------------------------------
@pytest.mark.parametrize("pfad,name,erwartet", [
    ("/drive/root:", "a.pdf", "Dateien/a.pdf"),
    ("/drive/root:/Kunden", "a.pdf", "Dateien/Kunden/a.pdf"),
    ("/drive/root:/A%20B", "a.pdf", "Dateien/A B/a.pdf"),          # prozentkodiert
    ("/drive/root:/A/B/C", "a.pdf", "Dateien/A/B/C/a.pdf"),
])
def test_rel_pfad(pfad, name, erwartet):
    assert od.rel_pfad(_datei("1", name, pfad)) == erwartet


def test_rel_pfad_bricht_nicht_aus_dem_ausgabeordner_aus():
    """Ein Name aus der Cloud ist Fremdeingabe – er darf kein Verzeichnis
    hochsteigen."""
    rel = od.rel_pfad(_datei("1", "../../.ssh/id_rsa", "/drive/root:/a/../b"))
    assert ".." not in rel.split("/")
    assert rel.startswith("Dateien/")


def test_lange_namen_kollidieren_nicht():
    """Regression vom echten Laufwerk: zwei Dateien, deren Namen sich erst
    NACH dem Schnitt unterscheiden, landeten auf demselben Pfad. Der zweite
    Download scheiterte dann an der Teildatei, die der erste weggeräumt hatte.

    Deshalb hier gleiche Endung und gleicher Anfang – der Unterschied liegt
    jenseits der 120 Zeichen. Mit verschiedenen Endungen bestünde der Test
    auch ohne Kürzel und prüfte nichts."""
    gleich = "A" * 130
    a = od.rel_pfad(_datei("id-A", gleich + "_variante_eins.pdf"))
    b = od.rel_pfad(_datei("id-B", gleich + "_variante_zwei.pdf"))
    assert a != b, "zwei verschiedene Dateien auf demselben Pfad"
    assert a.endswith(".pdf") and b.endswith(".pdf"), "Endung verloren"
    assert all(len(t) <= 120 for t in a.split("/"))


def test_kuerzung_erhaelt_die_endung():
    """Ohne die Endung wären "Bericht.pdf" und "Bericht.docx" nach dem Schnitt
    dieselbe Datei – genau das ist am echten Laufwerk passiert."""
    lang = "B" * 200
    assert od.safe(lang + ".pdf", kennung="1").endswith(".pdf")
    assert od.safe(lang + ".docx", kennung="1").endswith(".docx")
    assert od.safe(lang + ".pdf", kennung="1") != od.safe(lang + ".docx", kennung="1")


def test_kurze_namen_bleiben_unangetastet():
    """Das Kürzel darf nur auftauchen, wo wirklich gekürzt wird."""
    assert od.safe("Angebot.pdf", kennung="id") == "Angebot.pdf"


# --------------------------------------------------------------------------
# Planung: was der Lauf täte, ohne Netz
# --------------------------------------------------------------------------
def test_plane_trennt_laden_auslassen_und_geloescht(tmp_path):
    bestand = od.Bestand(tmp_path / od.BESTAND_DATEI)
    bestand.merke("weg", "Dateien/Ordner/alt.pdf", "c1", 10)
    eintraege = [
        _datei("neu", "neu.pdf"),
        _datei("aus", "aus.pdf", "/drive/root:/Fotos"),
        {"id": "weg", "deleted": {"state": "deleted"}},
        {"id": "ordner", "name": "Ordner", "folder": {"childCount": 2},
         "parentReference": {"path": "/drive/root:"}},
    ]
    regeln = folders.lies_regeln("- Dateien/Fotos/**")
    plan = od.plane(eintraege, bestand, tmp_path, regeln, grenze=0)
    assert [a["rel"] for a in plan["laden"]] == ["Dateien/Ordner/neu.pdf"]
    assert plan["ausgelassen"] == 1
    assert plan["geloescht"] == ["Dateien/Ordner/alt.pdf"]
    assert [e["pfad"] for e in plan["baum"]] == ["Dateien/Ordner"]


def test_plane_ueberspringt_was_unveraendert_daliegt(tmp_path):
    ziel = tmp_path / "Dateien/Ordner/a.pdf"
    ziel.parent.mkdir(parents=True)
    ziel.write_bytes(b"x" * 10)
    bestand = od.Bestand(tmp_path / od.BESTAND_DATEI)
    bestand.merke("1", "Dateien/Ordner/a.pdf", "c1", 10)
    plan = od.plane([_datei("1", "a.pdf")], bestand, tmp_path, [], grenze=0)
    assert plan["laden"] == []
    # Anderer cTag = neuer Inhalt, also doch laden.
    plan = od.plane([_datei("1", "a.pdf", ctag="c2")], bestand, tmp_path, [], grenze=0)
    assert len(plan["laden"]) == 1


def test_halbe_datei_gilt_nicht_als_fertig(tmp_path):
    """Größe daneben: ein abgebrochener Download darf nicht durchgehen."""
    ziel = tmp_path / "Dateien/Ordner/a.pdf"
    ziel.parent.mkdir(parents=True)
    ziel.write_bytes(b"x" * 3)                      # erwartet werden 10
    bestand = od.Bestand(tmp_path / od.BESTAND_DATEI)
    bestand.merke("1", "Dateien/Ordner/a.pdf", "c1", 10)
    plan = od.plane([_datei("1", "a.pdf")], bestand, tmp_path, [], grenze=0)
    assert len(plan["laden"]) == 1


def test_verschieben_statt_neu_laden(tmp_path):
    alt = tmp_path / "Dateien/Alt/a.pdf"
    alt.parent.mkdir(parents=True)
    alt.write_bytes(b"x" * 10)
    bestand = od.Bestand(tmp_path / od.BESTAND_DATEI)
    bestand.merke("1", "Dateien/Alt/a.pdf", "c1", 10)
    plan = od.plane([_datei("1", "a.pdf", "/drive/root:/Neu")], bestand, tmp_path,
                    [], grenze=0)
    assert plan["verschoben"] == [("Dateien/Alt/a.pdf", "Dateien/Neu/a.pdf")]
    assert plan["laden"] == [], "verschieben, nicht noch einmal herunterladen"
    assert od.verschiebe(tmp_path, plan["verschoben"]) == 1
    assert (tmp_path / "Dateien/Neu/a.pdf").exists() and not alt.exists()


def test_groessengrenze(tmp_path):
    bestand = od.Bestand(tmp_path / od.BESTAND_DATEI)
    gross = [_datei("1", "gross.pdf", groesse=5 * 1024 * 1024)]
    assert od.plane(gross, bestand, tmp_path, [], grenze=1024 * 1024)["laden"] == []
    assert len(od.plane(gross, bestand, tmp_path, [], grenze=0)["laden"]) == 1


def test_onenote_pakete_zaehlen_als_ordner(tmp_path):
    """Ein Notizbuch ist kein Inhalt; seine .one-Dateien kommen einzeln vor."""
    bestand = od.Bestand(tmp_path / od.BESTAND_DATEI)
    paket = {"id": "p", "name": "Notizbuch", "package": {"type": "oneNote"},
             "folder": {"childCount": 3}, "parentReference": {"path": "/drive/root:"}}
    plan = od.plane([paket], bestand, tmp_path, [], grenze=0)
    assert plan["laden"] == [] and [e["pfad"] for e in plan["baum"]] == ["Dateien/Notizbuch"]


# --------------------------------------------------------------------------
# Bestand und Grabsteine
# --------------------------------------------------------------------------
def test_bestand_ueberlebt_das_schreiben(tmp_path):
    b = od.Bestand(tmp_path / od.BESTAND_DATEI)
    b.merke("1", "Dateien/a.pdf", "c1", 10)
    b.schreibe()
    assert od.Bestand(tmp_path / od.BESTAND_DATEI).eintraege["1"]["rel"] == "Dateien/a.pdf"
    assert not (tmp_path / (od.BESTAND_DATEI + ".tmp")).exists()


def test_grabstein_wird_gesetzt_und_die_datei_bleibt(tmp_path):
    weg = od.schreibe_verschwunden(tmp_path / od.GONE_FILE, {}, ["Dateien/a.pdf"], "2026-01-01")
    assert weg == {"Dateien/a.pdf": "2026-01-01"}
    # Ein zweiter Lauf überschreibt den Zeitpunkt nicht.
    weg = od.schreibe_verschwunden(tmp_path / od.GONE_FILE, weg, ["Dateien/a.pdf"], "2026-06-06")
    assert weg["Dateien/a.pdf"] == "2026-01-01"
    assert od.lies_verschwunden(tmp_path / od.GONE_FILE) == weg


def test_geaendert_am_bevorzugt_die_zeit_des_clients():
    e = {"fileSystemInfo": {"lastModifiedDateTime": "2025-08-04T08:10:52Z"},
         "lastModifiedDateTime": "2020-01-01T00:00:00Z"}
    assert od.geaendert_am(e) == od.geaendert_am(
        {"lastModifiedDateTime": "2025-08-04T08:10:52Z"})
    assert od.geaendert_am({}) is None
    assert od.geaendert_am({"lastModifiedDateTime": "unsinn"}) is None


def test_delta_zeiger(tmp_path):
    assert od.lies_delta(tmp_path) is None
    od.schreibe_delta(tmp_path, "https://weiter")
    assert od.lies_delta(tmp_path) == "https://weiter"
    od.schreibe_delta(tmp_path, None)                 # nichts zu merken
    assert od.lies_delta(tmp_path) == "https://weiter"


# --------------------------------------------------------------------------
# Der ganze Lauf, gegen ein nachgestelltes Graph
# --------------------------------------------------------------------------
class FakeGraph:
    def __init__(self, seiten, fehlerhaft=()):
        self.seiten = seiten
        self.fehlerhaft = set(fehlerhaft)
        self.geladen = []

    def delta(self, weiter=None):
        for eintrag in self.seiten:
            yield eintrag, None
        yield None, "https://delta/neu"

    def lade(self, item_id, ziel, geaendert=None):
        if item_id in self.fehlerhaft:
            raise RuntimeError("Netz weg")
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(b"x" * 10)
        self.geladen.append(item_id)
        return 10


def test_lauf_spiegelt_und_merkt_sich_den_zeiger(tmp_path):
    g = FakeGraph([_datei("1", "a.pdf"), _datei("2", "b.pdf")])
    assert od.lauf(g, tmp_path) == 2
    assert (tmp_path / "Dateien/Ordner/a.pdf").read_bytes() == b"x" * 10
    assert od.lies_delta(tmp_path) == "https://delta/neu"
    assert not (tmp_path / "folders.json").exists(), \
        "ohne Ordner im Delta darf kein leerer Baum entstehen"


def test_abgebrochener_lauf_rueckt_den_zeiger_nicht_vor(tmp_path):
    """Sonst gingen alle Änderungen zwischen diesem und dem nächsten Lauf
    verloren – still, und erst Monate später bemerkbar."""
    g = FakeGraph([_datei("1", "a.pdf"), _datei("2", "b.pdf")], fehlerhaft={"2"})
    od.lauf(g, tmp_path)
    assert od.lies_delta(tmp_path) is None


def test_lauf_schreibt_den_bestand_auch_ohne_download(tmp_path):
    """Regression: eine Löschung ohne gleichzeitigen Download blieb ungemerkt –
    beim nächsten Lauf stand die Datei noch im Bestand."""
    b = od.Bestand(tmp_path / od.BESTAND_DATEI)
    b.merke("weg", "Dateien/alt.pdf", "c1", 10)
    b.schreibe()
    od.lauf(FakeGraph([{"id": "weg", "deleted": {"state": "deleted"}}]), tmp_path)
    assert "weg" not in od.Bestand(tmp_path / od.BESTAND_DATEI).eintraege
    assert "Dateien/alt.pdf" in od.lies_verschwunden(tmp_path / od.GONE_FILE)


def test_delta_lauf_kuerzt_den_ordnerbaum_nicht(tmp_path):
    """Regression: Graph liefert im Delta nur GEÄNDERTE Ordner. Wer den Baum
    damit ersetzt, hat beim zweiten Lauf statt vierzig Ordnern noch einen –
    und neununddreißig falsche Meldungen "nicht mehr vorhanden"."""
    def ordner(kennung, name):
        return {"id": kennung, "name": name, "folder": {"childCount": 1},
                "parentReference": {"path": "/drive/root:"}}

    od.lauf(FakeGraph([ordner("a", "A"), ordner("b", "B"), ordner("c", "C")]), tmp_path)
    assert len(folders.lade(tmp_path)["ordner"]) == 3

    # Zweiter Lauf: nur B hat sich geändert, C ist gelöscht.
    od.lauf(FakeGraph([ordner("b", "B neu"),
                       {"id": "c", "deleted": {"state": "deleted"}}]), tmp_path)
    baum = {e["id"]: e["pfad"] for e in folders.lade(tmp_path)["ordner"]}
    assert set(baum) == {"a", "b"}, "A ist aus dem Baum gefallen"
    assert baum["b"] == "Dateien/B neu"
