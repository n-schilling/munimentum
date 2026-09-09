"""Tests for onedrive_export.py – OneDrive as a mirror.

Three promises take centre stage here, and two of them have already been
broken once on a real drive:

  * A truncated file name must not collide with another. Two files whose
    names only differed after 120 characters landed on the same path – the
    second download then failed on the partial file the first one had
    already cleaned up.
  * An aborted run must not advance the delta pointer, otherwise the next
    run swallows every change in between.
  * Deleted in OneDrive means recorded, not thrown away.
"""

import pytest

import folders
import onedrive_export as od
import progress
import state_db


def _datei(kennung, name, pfad="/drive/root:/Ordner", groesse=10, ctag="c1", **extra):
    return {"id": kennung, "name": name, "size": groesse, "cTag": ctag,
            "file": {"mimeType": "application/pdf"},
            "parentReference": {"path": pfad}, **extra}


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
@pytest.mark.parametrize("pfad,name,erwartet", [
    ("/drive/root:", "a.pdf", "Dateien/a.pdf"),
    ("/drive/root:/Kunden", "a.pdf", "Dateien/Kunden/a.pdf"),
    ("/drive/root:/A%20B", "a.pdf", "Dateien/A B/a.pdf"),          # percent-encoded
    ("/drive/root:/A/B/C", "a.pdf", "Dateien/A/B/C/a.pdf"),
])
def test_rel_pfad(pfad, name, erwartet):
    assert od.rel_pfad(_datei("1", name, pfad)) == erwartet


def test_rel_pfad_bricht_nicht_aus_dem_ausgabeordner_aus():
    """A name from the cloud is foreign input – it must not climb up a
    directory."""
    rel = od.rel_pfad(_datei("1", "../../.ssh/id_rsa", "/drive/root:/a/../b"))
    assert ".." not in rel.split("/")
    assert rel.startswith("Dateien/")


def test_lange_namen_kollidieren_nicht():
    """Regression from the real drive: two files whose names only differ
    AFTER the cut landed on the same path. The second download then failed
    on the partial file the first one had cleaned up.

    Hence the same extension and the same start here – the difference lies
    beyond the 120 characters. With different extensions the test would
    pass even without the hash suffix and check nothing."""
    gleich = "A" * 130
    a = od.rel_pfad(_datei("id-A", gleich + "_variante_eins.pdf"))
    b = od.rel_pfad(_datei("id-B", gleich + "_variante_zwei.pdf"))
    assert a != b, "zwei verschiedene Dateien auf demselben Pfad"
    assert a.endswith(".pdf") and b.endswith(".pdf"), "Endung verloren"
    assert all(len(t) <= 120 for t in a.split("/"))


def test_kuerzung_erhaelt_die_endung():
    """Without the extension, "Bericht.pdf" and "Bericht.docx" would be the
    same file after the cut – exactly what happened on the real drive."""
    lang = "B" * 200
    assert od.safe(lang + ".pdf", kennung="1").endswith(".pdf")
    assert od.safe(lang + ".docx", kennung="1").endswith(".docx")
    assert od.safe(lang + ".pdf", kennung="1") != od.safe(lang + ".docx", kennung="1")


def test_kurze_namen_bleiben_unangetastet():
    """The hash suffix may only appear where something is actually cut."""
    assert od.safe("Angebot.pdf", kennung="id") == "Angebot.pdf"


# --------------------------------------------------------------------------
# Planning: what the run would do, without a network
# --------------------------------------------------------------------------
def test_plane_trennt_laden_auslassen_und_geloescht(tmp_path):
    bestand = od.Bestand()
    bestand.merke("weg", "Dateien/Ordner/alt.pdf", "c1", 10)
    eintraege = [
        _datei("neu", "neu.pdf"),
        _datei("aus", "aus.pdf", "/drive/root:/Fotos"),
        {"id": "weg", "deleted": {"state": "deleted"}},
        {"id": "ordner", "name": "Ordner", "folder": {"childCount": 2},
         "parentReference": {"path": "/drive/root:"}},
    ]
    regeln = folders.lies_regeln("- Dateien/Fotos/**")
    plan = od.plane(eintraege, bestand, tmp_path, od.Selection(rules=regeln))
    assert [a["rel"] for a in plan["laden"]] == ["Dateien/Ordner/neu.pdf"]
    assert plan["ausgelassen"] == 1
    assert plan["geloescht"] == ["Dateien/Ordner/alt.pdf"]
    assert [e["pfad"] for e in plan["baum"]] == ["Dateien/Ordner"]


def test_plane_ueberspringt_was_unveraendert_daliegt(tmp_path):
    ziel = tmp_path / "Dateien/Ordner/a.pdf"
    ziel.parent.mkdir(parents=True)
    ziel.write_bytes(b"x" * 10)
    bestand = od.Bestand()
    bestand.merke("1", "Dateien/Ordner/a.pdf", "c1", 10)
    plan = od.plane([_datei("1", "a.pdf")], bestand, tmp_path, od.Selection())
    assert plan["laden"] == []
    # Different cTag = new content, so download after all.
    plan = od.plane([_datei("1", "a.pdf", ctag="c2")], bestand, tmp_path, od.Selection())
    assert len(plan["laden"]) == 1


def test_halbe_datei_gilt_nicht_als_fertig(tmp_path):
    """Wrong size: an aborted download must not pass as complete."""
    ziel = tmp_path / "Dateien/Ordner/a.pdf"
    ziel.parent.mkdir(parents=True)
    ziel.write_bytes(b"x" * 3)                      # 10 are expected
    bestand = od.Bestand()
    bestand.merke("1", "Dateien/Ordner/a.pdf", "c1", 10)
    plan = od.plane([_datei("1", "a.pdf")], bestand, tmp_path, od.Selection())
    assert len(plan["laden"]) == 1


def test_verschieben_statt_neu_laden(tmp_path):
    alt = tmp_path / "Dateien/Alt/a.pdf"
    alt.parent.mkdir(parents=True)
    alt.write_bytes(b"x" * 10)
    bestand = od.Bestand()
    bestand.merke("1", "Dateien/Alt/a.pdf", "c1", 10)
    plan = od.plane([_datei("1", "a.pdf", "/drive/root:/Neu")], bestand, tmp_path,
                    od.Selection())
    assert plan["verschoben"] == [("Dateien/Alt/a.pdf", "Dateien/Neu/a.pdf")]
    assert plan["laden"] == [], "verschieben, nicht noch einmal herunterladen"
    assert od.verschiebe(tmp_path, plan["verschoben"]) == 1
    assert (tmp_path / "Dateien/Neu/a.pdf").exists() and not alt.exists()


def test_umbenannt_und_geaendert_wird_verschoben_und_geladen(tmp_path):
    """Both at once: move along first, then download – otherwise the new
    content would land next to an orphaned old file."""
    alt = tmp_path / "Dateien/Alt/a.pdf"
    alt.parent.mkdir(parents=True)
    alt.write_bytes(b"x" * 10)
    bestand = od.Bestand()
    bestand.merke("1", "Dateien/Alt/a.pdf", "c1", 10)
    plan = od.plane([_datei("1", "b.pdf", "/drive/root:/Neu", groesse=99, ctag="c2")],
                    bestand, tmp_path, od.Selection())
    assert plan["verschoben"] == [("Dateien/Alt/a.pdf", "Dateien/Neu/b.pdf")]
    assert [a["rel"] for a in plan["laden"]] == ["Dateien/Neu/b.pdf"]


def test_groessengrenze(tmp_path):
    bestand = od.Bestand()
    gross = [_datei("1", "gross.pdf", groesse=5 * 1024 * 1024)]
    assert od.plane(gross, bestand, tmp_path, od.Selection(max_bytes=1024 * 1024))["laden"] == []
    assert len(od.plane(gross, bestand, tmp_path, od.Selection())["laden"]) == 1


def test_die_wurzel_steht_im_baum(tmp_path):
    """Otherwise every file at the drive root counts as "local only" – a
    false alarm in the export plan that occurs exactly once and confuses
    permanently."""
    bestand = od.Bestand()
    wurzel = {"id": "root!", "name": "root", "root": {}, "folder": {"childCount": 4},
              "parentReference": {"driveId": "d"}}
    plan = od.plane([wurzel], bestand, tmp_path, od.Selection())
    assert [e["pfad"] for e in plan["baum"]] == [od.DATEI_DIR]
    assert plan["laden"] == []


def test_onenote_pakete_zaehlen_als_ordner(tmp_path):
    """A notebook is not content; its .one files appear individually."""
    bestand = od.Bestand()
    paket = {"id": "p", "name": "Notizbuch", "package": {"type": "oneNote"},
             "folder": {"childCount": 3}, "parentReference": {"path": "/drive/root:"}}
    plan = od.plane([paket], bestand, tmp_path, od.Selection())
    assert plan["laden"] == [] and [e["pfad"] for e in plan["baum"]] == ["Dateien/Notizbuch"]


# --------------------------------------------------------------------------
# Inventory and tombstones
# --------------------------------------------------------------------------
def test_bestand_ueberlebt_das_schreiben(tmp_path):
    db = state_db.StateDb(tmp_path)
    b = state_db.DbBestand(db)
    b.merke("1", "Dateien/a.pdf", "c1", 10)
    b.schreibe()
    assert state_db.DbBestand(db).eintraege["1"]["rel"] == "Dateien/a.pdf"


def test_grabstein_wird_gesetzt_und_die_datei_bleibt(tmp_path):
    db = state_db.StateDb(tmp_path)
    db.verschwunden_ergaenzen(["Dateien/a.pdf"], "2026-01-01")
    # A second run does not overwrite the timestamp.
    db.verschwunden_ergaenzen(["Dateien/a.pdf"], "2026-06-06")
    assert db.verschwunden_lesen() == {"Dateien/a.pdf": "2026-01-01"}


def test_geaendert_am_bevorzugt_die_zeit_des_clients():
    e = {"fileSystemInfo": {"lastModifiedDateTime": "2025-08-04T08:10:52Z"},
         "lastModifiedDateTime": "2020-01-01T00:00:00Z"}
    assert od.geaendert_am(e) == od.geaendert_am(
        {"lastModifiedDateTime": "2025-08-04T08:10:52Z"})
    assert od.geaendert_am({}) is None
    assert od.geaendert_am({"lastModifiedDateTime": "unsinn"}) is None


def test_delta_zeiger(tmp_path):
    z = state_db.DbZustand(tmp_path)
    assert z.delta_lesen() is None
    z.delta_schreiben("https://weiter")
    assert z.delta_lesen() == "https://weiter"
    z.delta_schreiben(None)                           # nothing to remember
    assert z.delta_lesen() == "https://weiter"


# --------------------------------------------------------------------------
# The whole run, against a mocked-up Graph
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
    assert od.lauf(g, tmp_path)["new"] == 2
    assert (tmp_path / "Dateien/Ordner/a.pdf").read_bytes() == b"x" * 10
    assert state_db.DbZustand(tmp_path).delta_lesen() == "https://delta/neu"
    assert folders.lade(tmp_path) is None, \
        "ohne Ordner im Delta darf kein leerer Baum entstehen"


def test_abgebrochener_lauf_rueckt_den_zeiger_nicht_vor(tmp_path):
    """Otherwise every change between this run and the next would be lost –
    silently, and only noticeable months later."""
    g = FakeGraph([_datei("1", "a.pdf"), _datei("2", "b.pdf")], fehlerhaft={"2"})
    od.lauf(g, tmp_path)
    assert state_db.DbZustand(tmp_path).delta_lesen() is None


def test_lauf_schreibt_den_bestand_auch_ohne_download(tmp_path):
    """Regression: a deletion without a simultaneous download went
    unrecorded – on the next run the file was still in the inventory."""
    db = state_db.StateDb(tmp_path)
    b = state_db.DbBestand(db)
    b.merke("weg", "Dateien/alt.pdf", "c1", 10)
    b.schreibe()
    od.lauf(FakeGraph([{"id": "weg", "deleted": {"state": "deleted"}}]), tmp_path)
    assert "weg" not in db.bestand_lesen()
    assert "Dateien/alt.pdf" in db.verschwunden_lesen()


def test_delta_lauf_kuerzt_den_ordnerbaum_nicht(tmp_path):
    """Regression: Graph delivers only CHANGED folders in the delta.
    Replacing the tree with that leaves one folder instead of forty on the
    second run – and thirty-nine false "no longer present" reports."""
    def ordner(kennung, name):
        return {"id": kennung, "name": name, "folder": {"childCount": 1},
                "parentReference": {"path": "/drive/root:"}}

    od.lauf(FakeGraph([ordner("a", "A"), ordner("b", "B"), ordner("c", "C")]), tmp_path)
    assert len(folders.lade(tmp_path)["ordner"]) == 3

    # Second run: only B has changed, C is deleted.
    od.lauf(FakeGraph([ordner("b", "B neu"),
                       {"id": "c", "deleted": {"state": "deleted"}}]), tmp_path)
    baum = {e["id"]: e["pfad"] for e in folders.lade(tmp_path)["ordner"]}
    assert set(baum) == {"a", "b"}, "A ist aus dem Baum gefallen"
    assert baum["b"] == "Dateien/B neu"


# --------------------------------------------------------------------------
# Completeness: what the drive has against what lies here
# --------------------------------------------------------------------------
def test_check_findet_die_fehlende_datei(tmp_path):
    da = tmp_path / "Dateien/Ordner/da.pdf"
    da.parent.mkdir(parents=True)
    da.write_bytes(b"x" * 10)
    b = od.pruefe_vollstaendigkeit(
        [_datei("1", "da.pdf"), _datei("2", "weg.pdf")], tmp_path, od.Selection())
    assert (b["erwartet"], b["vorhanden"], b["fehlt"]) == (2, 1, 1)
    assert [z["ordner"] for z in b["ordner"] if z["fehlt"]] == ["Dateien/Ordner"]


def test_check_erkennt_die_halb_uebertragene_datei(tmp_path):
    """Present means same size. Otherwise an abort would count as success."""
    halb = tmp_path / "Dateien/Ordner/a.pdf"
    halb.parent.mkdir(parents=True)
    halb.write_bytes(b"x" * 3)                      # 10 are expected
    b = od.pruefe_vollstaendigkeit([_datei("1", "a.pdf")], tmp_path, od.Selection())
    assert b["fehlt"] == 1


def test_check_rechnet_ausgelassenes_nicht_als_luecke(tmp_path):
    """Otherwise the first check would report hundreds of false alarms for
    folders one excluded oneself – a report that shows nonsense the first
    time is never opened again."""
    regeln = folders.lies_regeln("- Dateien/Fotos/**")
    b = od.pruefe_vollstaendigkeit(
        [_datei("1", "a.jpg", "/drive/root:/Fotos"),
         _datei("2", "gross.zip", groesse=99_000_000)],
        tmp_path, od.Selection(rules=regeln, max_bytes=1_000_000))
    assert b["fehlt"] == 0 and b["erwartet"] == 0
    assert b["ausgelassen"] == 2
    assert any(z["ausgelassen"] for z in b["ordner"])


def test_check_erklaert_geloeschtes_statt_es_zu_vermissen(tmp_path):
    state_db.StateDb(tmp_path).verschwunden_ergaenzen(
        ["Dateien/Ordner/alt.pdf"], "2026-01-01")
    b = od.pruefe_vollstaendigkeit([], tmp_path, od.Selection())
    assert b["geloescht"] == 1 and b["fehlt"] == 0


def test_check_schreibt_den_bericht_in_die_db(tmp_path):
    state_db.StateDb(tmp_path).bericht_schreiben({"erwartet": 1})
    assert state_db.StateDb(tmp_path).bericht_lesen() == {"erwartet": 1}


def test_null_heisst_ohne_grenze(monkeypatch):
    """Regression: settings.number raises to at least 1. Right for
    "parallel downloads", wrong here – the disabled limit turned into one
    of a single megabyte, and the mirror silently left every larger file
    behind. It only showed in the report: "208 not counted", although
    nobody had excluded anything."""
    monkeypatch.setenv("ONEDRIVE_MAX_MB", "0")
    assert od.max_bytes() == 0
    wahl = od.Selection(max_bytes=od.max_bytes())
    assert wahl.takes("Dateien/a.bin", 500 * 1024 * 1024), "0 muss ALLES durchlassen"
    monkeypatch.setenv("ONEDRIVE_MAX_MB", "50")
    assert od.max_bytes() == 50 * 1024 * 1024
    assert not od.Selection(max_bytes=od.max_bytes()).takes(
        "Dateien/a.bin", 60 * 1024 * 1024)


def test_ohne_grenze_wird_nichts_ausgelassen(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEDRIVE_MAX_MB", "0")
    gross = [_datei(str(i), f"f{i}.bin", groesse=99_000_000) for i in range(3)]
    wahl = od.Selection(max_bytes=od.max_bytes())
    plan = od.plane(gross, od.Bestand(), tmp_path, wahl)
    assert plan["ausgelassen"] == 0 and len(plan["laden"]) == 3
    b = od.pruefe_vollstaendigkeit(gross, tmp_path, wahl)
    assert b["ausgelassen"] == 0, "Bericht meldet Ausgelassenes ohne jede Regel"


def test_lauf_erneuert_abgelaufenen_delta_zeiger(tmp_path, capsys):
    """410 Gone on a stale delta link: re-enumerate once instead of dying
    identically on every run."""
    import requests as _requests

    class Antwort:
        status_code = 410

    class G:
        def __init__(self):
            self.aufrufe = []

        def delta(self, weiter=None):
            self.aufrufe.append(weiter)
            if weiter:
                raise _requests.HTTPError(response=Antwort())
            yield None, "neuer-link"

    state_db.DbZustand(tmp_path).delta_schreiben("alter-link")
    g = G()
    ergebnis = od.drive_mirror.lauf(g, tmp_path, od.Selection(), 1)
    assert ergebnis["new"] == 0 and g.aufrufe == ["alter-link", None]
    assert state_db.DbZustand(tmp_path).delta_lesen() == "neuer-link"
    events = [progress.lies_event(z)
              for z in capsys.readouterr().out.splitlines()]
    assert any(e and e["k"] == "run.drive.resync" for e in events)
