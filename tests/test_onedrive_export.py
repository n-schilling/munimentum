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

import json
import time

import pytest

import folders
import onedrive_export as od
import progress
import state_db


@pytest.fixture(autouse=True)
def _ohne_kadenz(monkeypatch):
    """The mirror gates itself by SYNC_CADENCE["onedrive"] since 9.0 – the
    developer's own app_config.json must not decide whether a test's second
    run happens. Tests that want the gate set the environment themselves."""
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    monkeypatch.delenv("SYNC_NOW", raising=False)


def _events(capsys):
    return [e for e in (progress.lies_event(z) for z in
                        capsys.readouterr().out.splitlines()) if e]


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


# --------------------------------------------------------------------------
# Cadence: the mirror gates itself, SYNC_NOW steps over the gate
# --------------------------------------------------------------------------
def test_lauf_ueberspringt_onedrive_unter_kadenz(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"onedrive": "weekly"}))
    g = FakeGraph([_datei("1", "a.pdf")])
    assert od.lauf(g, tmp_path)["new"] == 1
    assert state_db.StateDb(tmp_path).kv_lesen("last_sync"), \
        "a clean run stamps the sync"
    capsys.readouterr()
    assert od.lauf(g, tmp_path) is None
    assert g.geladen == ["1"], "the skipped run must not touch the drive"
    events = _events(capsys)
    skip = [e for e in events if e["k"] == "run.cadence.skip"]
    assert len(skip) == 1
    assert skip[0]["v"]["name"] == progress.atom("settings.onedrive.title")
    assert skip[0]["v"]["cadence"]["k"] == "cadence.weekly"
    capsys.readouterr()
    # "Sync now": the gate steps aside.
    monkeypatch.setenv("SYNC_NOW", "1")
    assert od.lauf(g, tmp_path)["new"] == 0
    assert not any(e["k"] == "run.cadence.skip" for e in _events(capsys))


def test_abgebrochener_lauf_stempelt_keinen_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"onedrive": "weekly"}))
    g = FakeGraph([_datei("1", "a.pdf"), _datei("2", "b.pdf")], fehlerhaft={"2"})
    assert od.lauf(g, tmp_path)["errors"] == 1
    assert not state_db.StateDb(tmp_path).kv_lesen("last_sync")
    # Still due, then: the next run comes back for the missing file.
    g.fehlerhaft = set()
    assert od.lauf(g, tmp_path)["new"] == 1


def test_kadenz_gilt_nicht_fuer_pruefen_und_ordner(tmp_path, monkeypatch):
    """--check and --folders are outside the cadence – a stamped sync
    must not silence them."""
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"onedrive": "monthly"}))
    state_db.StateDb(tmp_path).kv_schreiben("last_sync", str(time.time()))
    g = FakeGraph([_datei("1", "a.pdf")])
    assert od.nur_pruefen(g, tmp_path)["erwartet"] == 1
    assert od.nur_ordner(g, tmp_path) is not None


# --------------------------------------------------------------------------
# A changed selection: the delta cannot bring what it never fetched
# --------------------------------------------------------------------------
class _DeltaGraph:
    """Two files, one in Archiv; the delta after the first walk is empty."""

    def __init__(self):
        self.aufrufe = []
        self.geladen = []

    def delta(self, weiter=None):
        self.aufrufe.append(weiter)
        if weiter is None:
            yield _datei("alt", "alt.pdf", "/drive/root:/Archiv"), None
            yield _datei("neu", "neu.pdf"), None
        yield None, "delta-1"

    def lade(self, item_id, ziel, geaendert=None):
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(b"x" * 10)
        self.geladen.append(item_id)
        return 10


def test_erster_lauf_merkt_sich_die_auswahl_ohne_neustart(tmp_path, capsys):
    g = _DeltaGraph()
    wahl = od.Selection(rules=folders.lies_regeln("- Dateien/Archiv/**"))
    assert od.drive_mirror.lauf(g, tmp_path, wahl, 1)["new"] == 1
    assert g.aufrufe == [None] and g.geladen == ["neu"]
    assert state_db.StateDb(tmp_path).kv_lesen("auswahl") == wahl.kennzeichen()
    assert not any(e["k"] == "run.rules_changed" for e in _events(capsys))


def test_gleiche_auswahl_liest_nur_das_delta(tmp_path, capsys):
    g = _DeltaGraph()
    regeln = folders.lies_regeln("- Dateien/Archiv/**")
    od.drive_mirror.lauf(g, tmp_path, od.Selection(rules=regeln), 1)
    capsys.readouterr()
    # A fresh Selection with the same content – not the same object.
    od.drive_mirror.lauf(g, tmp_path, od.Selection(rules=regeln), 1)
    assert g.aufrufe == [None, "delta-1"]
    assert not any(e["k"] == "run.rules_changed" for e in _events(capsys))


def test_geaenderte_auswahl_liest_den_ordner_einmal_voll(tmp_path, capsys):
    """The regression this guards: a rule opened later never fetched the
    files that did not change since – the delta had nothing to say."""
    g = _DeltaGraph()
    od.drive_mirror.lauf(g, tmp_path, od.Selection(
        rules=folders.lies_regeln("- Dateien/Archiv/**")), 1)
    assert not (tmp_path / "Dateien/Archiv/alt.pdf").exists()
    capsys.readouterr()
    weit = od.Selection()
    zahlen = od.drive_mirror.lauf(g, tmp_path, weit, 1, name="Nordwind")
    assert g.aufrufe == [None, None], "full read, not the delta"
    assert zahlen["new"] == 1 and (tmp_path / "Dateien/Archiv/alt.pdf").is_file()
    events = [e for e in _events(capsys) if e["k"] == "run.rules_changed"]
    assert len(events) == 1 and events[0]["v"]["name"] == "Nordwind"
    assert state_db.StateDb(tmp_path).kv_lesen("auswahl") == weit.kennzeichen()
    assert state_db.DbZustand(tmp_path).delta_lesen() == "delta-1"


def test_engere_auswahl_laesst_liegen_was_da_ist(tmp_path):
    """Narrowing excludes from now on – files already here stay on disk."""
    g = _DeltaGraph()
    od.drive_mirror.lauf(g, tmp_path, od.Selection(), 1)
    assert (tmp_path / "Dateien/Archiv/alt.pdf").is_file()
    zahlen = od.drive_mirror.lauf(g, tmp_path, od.Selection(
        rules=folders.lies_regeln("- Dateien/Archiv/**")), 1)
    assert zahlen["new"] == 0 and zahlen["excluded"] == 1
    assert (tmp_path / "Dateien/Archiv/alt.pdf").is_file()


# --------------------------------------------------------------------------
# Folder cadences: one delta stream, downloads gated per folder unit
# --------------------------------------------------------------------------
class _Antwort:
    def __init__(self, status):
        self.status_code = status


class _KadenzGraph:
    """Delta entries per run, item lookups by id (None = 404), and a log of
    every call in order – the waiting list must be handled before the
    delta is asked."""

    drive_base = "https://graph.microsoft.com/v1.0/me/drive"

    def __init__(self, seiten=(), items=None):
        self.seiten = list(seiten)
        self.items = items or {}
        self.log = []
        self.geladen = []

    def delta(self, weiter=None):
        self.log.append(("delta", weiter))
        for e in self.seiten:
            yield e, None
        yield None, f"delta-{len(self.log)}"

    def get(self, url):
        import requests
        kennung = url.split("/items/")[1].split("?")[0]
        self.log.append(("get", kennung))
        meta = self.items.get(kennung)
        if meta is None:
            raise requests.HTTPError(response=_Antwort(404))
        return meta

    def lade(self, item_id, ziel, geaendert=None):
        self.log.append(("lade", item_id))
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(b"x" * 10)
        self.geladen.append(item_id)
        return 10


FOTO = "/drive/root:/Fotos"
FOTOS_KEY = "last_sync:onedrive:Dateien/Fotos"


def _wartend(out):
    return {k: json.loads(v) for k, v in
            state_db.StateDb(out).saetze_lesen("wartend").items()}


def _erster_lauf(tmp_path, monkeypatch, kadenzen=None):
    """Run 1 brings both files and stamps drive and folder."""
    monkeypatch.setenv("SYNC_CADENCE", json.dumps(
        kadenzen or {"onedrive:Dateien/Fotos": "monthly"}))
    g = _KadenzGraph([_datei("f1", "f1.jpg", FOTO), _datei("a1", "a1.pdf")])
    assert od.lauf(g, tmp_path)["new"] == 2
    db = state_db.StateDb(tmp_path)
    assert db.kv_lesen("last_sync") and db.kv_lesen(FOTOS_KEY)
    return db


def test_datei_im_getakteten_ordner_wartet_und_der_zeiger_rueckt_vor(
        tmp_path, monkeypatch, capsys):
    db = _erster_lauf(tmp_path, monkeypatch)
    capsys.readouterr()
    g = _KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2"),
                      _datei("a1", "a1.pdf", ctag="c2")])
    zahlen = od.lauf(g, tmp_path)
    assert g.geladen == ["a1"], "the sibling comes, the paced file waits"
    assert zahlen["new"] == 1 and zahlen["waiting"] == 1
    assert _wartend(tmp_path) == {"f1": {"rel": "Dateien/Fotos/f1.jpg", "ctag": "c2",
                                         "size": 10, "einheit": "Dateien/Fotos"}}
    assert state_db.DbZustand(tmp_path).delta_lesen() == "delta-1"
    assert db.bestand_lesen()["f1"]["ctag"] == "c1", "the inventory keeps the old version"
    events = _events(capsys)
    paced = [e for e in events if e["k"] == "run.onedrive.folders_paced"]
    assert len(paced) == 1 and paced[0]["v"] == {"n": 1}
    assert not any(e["k"] == "run.cadence.skip" for e in events)


def test_ergebnis_traegt_die_wartenden(tmp_path, monkeypatch, capsys):
    _erster_lauf(tmp_path, monkeypatch)
    capsys.readouterr()
    od.lauf(_KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2")]), tmp_path)
    zeilen = [z for z in capsys.readouterr().out.splitlines() if progress.lies_ergebnis(z)]
    assert progress.lies_ergebnis(zeilen[-1])["extra"]["waiting"] == 1


def test_faelliger_ordner_holt_die_wartenden_vor_dem_delta(tmp_path, monkeypatch,
                                                            capsys):
    db = _erster_lauf(tmp_path, monkeypatch)
    od.lauf(_KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2")]), tmp_path)
    alt = db.kv_lesen(FOTOS_KEY)
    db.kv_schreiben(FOTOS_KEY, str(time.time() - 31 * 86400))   # a month passed
    capsys.readouterr()
    g = _KadenzGraph([], items={"f1": _datei("f1", "f1.jpg", FOTO, ctag="c2")})
    zahlen = od.lauf(g, tmp_path)
    assert g.log[:2] == [("get", "f1"), ("delta", "delta-1")], "waiting list first"
    assert g.geladen == ["f1"] and zahlen["new"] == 1 and zahlen["waiting"] == 0
    assert _wartend(tmp_path) == {}
    assert db.bestand_lesen()["f1"]["ctag"] == "c2"
    assert float(db.kv_lesen(FOTOS_KEY)) > float(alt), "the unit is stamped"
    assert not any(e["k"] == "run.onedrive.folders_paced" for e in _events(capsys))


def test_wartende_die_schon_aktuell_ist_faellt_ohne_download_weg(tmp_path,
                                                                  monkeypatch):
    db = _erster_lauf(tmp_path, monkeypatch)
    od.lauf(_KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2")]), tmp_path)
    db.kv_schreiben(FOTOS_KEY, str(time.time() - 31 * 86400))
    # The lookup says c1 – what lies here already.
    g = _KadenzGraph([], items={"f1": _datei("f1", "f1.jpg", FOTO, ctag="c1")})
    assert od.lauf(g, tmp_path)["waiting"] == 0
    assert g.geladen == [] and _wartend(tmp_path) == {}


def test_am_quellort_geloeschte_wartende_verlaesst_die_liste(tmp_path, monkeypatch):
    db = _erster_lauf(tmp_path, monkeypatch)
    od.lauf(_KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2")]), tmp_path)
    g = _KadenzGraph([{"id": "f1", "deleted": {"state": "deleted"}}])
    zahlen = od.lauf(g, tmp_path)
    assert g.geladen == [] and zahlen["waiting"] == 0 and zahlen["gone"] == 1
    assert _wartend(tmp_path) == {}
    assert "Dateien/Fotos/f1.jpg" in db.verschwunden_lesen()
    # Gone by the time the unit is due: the lookup's 404 clears it too.
    od.lauf(_KadenzGraph([_datei("f2", "f2.jpg", FOTO)]), tmp_path)
    od.lauf(_KadenzGraph([_datei("f2", "f2.jpg", FOTO, ctag="c2")]), tmp_path)
    assert set(_wartend(tmp_path)) == {"f2"}
    db.kv_schreiben(FOTOS_KEY, str(time.time() - 31 * 86400))
    g = _KadenzGraph([], items={})
    assert od.lauf(g, tmp_path)["waiting"] == 0 and g.geladen == []


def test_verschobene_wartende_wartet_unter_dem_neuen_pfad(tmp_path, monkeypatch):
    _erster_lauf(tmp_path, monkeypatch)
    od.lauf(_KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2")]), tmp_path)
    g = _KadenzGraph([_datei("f1", "f1.jpg", FOTO + "/2026", ctag="c2")])
    zahlen = od.lauf(g, tmp_path)
    assert g.geladen == [] and zahlen["waiting"] == 1
    assert _wartend(tmp_path)["f1"] == {"rel": "Dateien/Fotos/2026/f1.jpg", "ctag": "c2",
                                        "size": 10, "einheit": "Dateien/Fotos"}
    # The local copy of the old version moved along, as for every rename.
    assert (tmp_path / "Dateien/Fotos/2026/f1.jpg").is_file()
    # Moved out of the paced folder into a due unit: downloaded, list cleared.
    g = _KadenzGraph([_datei("f1", "f1.jpg", "/drive/root:/Ordner", ctag="c2")])
    zahlen = od.lauf(g, tmp_path)
    assert g.geladen == ["f1"] and zahlen["waiting"] == 0


def test_sync_now_holt_alles_wartende(tmp_path, monkeypatch):
    db = _erster_lauf(tmp_path, monkeypatch)
    od.lauf(_KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2")]), tmp_path)
    stempel = db.kv_lesen(FOTOS_KEY)
    monkeypatch.setenv("SYNC_NOW", "1")
    g = _KadenzGraph([], items={"f1": _datei("f1", "f1.jpg", FOTO, ctag="c2")})
    zahlen = od.lauf(g, tmp_path)
    assert g.geladen == ["f1"] and zahlen["waiting"] == 0
    assert float(db.kv_lesen(FOTOS_KEY)) > float(stempel)


def test_getaktetes_laufwerk_mit_faelligem_ordner_listet_trotzdem(tmp_path,
                                                                   monkeypatch,
                                                                   capsys):
    """The drive itself is monthly, one folder always: the run lists (the
    folder is due), the folder's file comes, the drive's file waits – and
    no folder is reported as paced."""
    db = _erster_lauf(tmp_path, monkeypatch,
                      {"onedrive": "monthly", "onedrive:Dateien/Fotos": "always"})
    drive_stempel = db.kv_lesen("last_sync")
    capsys.readouterr()
    g = _KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2"),
                      _datei("a1", "a1.pdf", ctag="c2")])
    zahlen = od.lauf(g, tmp_path)
    assert g.geladen == ["f1"] and zahlen["waiting"] == 1
    assert _wartend(tmp_path)["a1"]["einheit"] == ""
    assert db.kv_lesen("last_sync") == drive_stempel, "the drive unit was not due"
    assert not any(e["k"] == "run.onedrive.folders_paced" for e in _events(capsys))
    # Nothing due at all: the whole run is skipped, before any listing.
    monkeypatch.setenv("SYNC_CADENCE", json.dumps(
        {"onedrive": "monthly", "onedrive:Dateien/Fotos": "monthly"}))
    g = _KadenzGraph([_datei("a1", "a1.pdf", ctag="c3")])
    assert od.lauf(g, tmp_path) is None and g.log == []
    assert any(e["k"] == "run.cadence.skip" for e in _events(capsys))


def test_fehlgeschlagener_download_laesst_die_einheit_faellig(tmp_path, monkeypatch):
    """Only a unit whose downloads all succeeded is stamped; a failed
    waiting download stays on the list – the walk store does not know it."""
    db = _erster_lauf(tmp_path, monkeypatch)
    od.lauf(_KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2")]), tmp_path)
    db.kv_schreiben(FOTOS_KEY, str(time.time() - 31 * 86400))
    alt = db.kv_lesen(FOTOS_KEY)
    g = _KadenzGraph([_datei("a1", "a1.pdf", ctag="c2")],
                     items={"f1": _datei("f1", "f1.jpg", FOTO, ctag="c2")})
    heil = g.lade

    def lade(item_id, ziel, geaendert=None):
        if item_id == "f1":
            raise RuntimeError("Netz weg")
        return heil(item_id, ziel, geaendert)

    g.lade = lade
    zahlen = od.lauf(g, tmp_path)
    assert zahlen["errors"] == 1 and zahlen["new"] == 1 and zahlen["waiting"] == 1
    assert set(_wartend(tmp_path)) == {"f1"}
    assert db.kv_lesen(FOTOS_KEY) == alt, "the folder unit stays due"
    assert float(db.kv_lesen("last_sync")) > float(alt), "the drive unit is stamped"


def test_einheiten_ordnen_pfade_der_tiefsten_taste_zu(tmp_path):
    e = od.drive_mirror.Einheiten(
        {"onedrive": "weekly", "onedrive:Dateien/Fotos": "monthly",
         "onedrive:Dateien/Fotos/Urlaub": "daily", "onedrive:": "x"},
        "onedrive", state_db.StateDb(tmp_path))
    assert e.ordner == ["Dateien/Fotos", "Dateien/Fotos/Urlaub"]
    assert e.einheit("Dateien/Fotos/Urlaub/2026/a.jpg") == "Dateien/Fotos/Urlaub"
    assert e.einheit("Dateien/Fotos/a.jpg") == "Dateien/Fotos"
    assert e.einheit("Dateien/Ordner/a.pdf") == "" and e.einheit("Dateien/Fotosalbum/a.jpg") == ""
    assert e.kadenz("") == "weekly" and e.kadenz("Dateien/Fotos/Urlaub") == "daily"
    assert e.kv_key("") == "last_sync"
    assert e.kv_key("Dateien/Fotos") == "last_sync:onedrive:Dateien/Fotos"
    assert e.irgendeine_faellig() and e.zurueckgehalten() == []


def test_kadenzen_gehoeren_nicht_zum_kennzeichen(tmp_path, monkeypatch, capsys):
    _erster_lauf(tmp_path, monkeypatch)
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"onedrive:Dateien/Fotos": "daily"}))
    capsys.readouterr()
    g = _KadenzGraph([])
    od.lauf(g, tmp_path)
    assert g.log == [("delta", "delta-1")], "delta, not a full read"
    assert not any(e["k"] == "run.rules_changed" for e in _events(capsys))


def test_kennzeichen_haengt_an_allem_was_die_auswahl_entscheidet():
    basis = od.Selection().kennzeichen()
    assert od.Selection().kennzeichen() == basis
    assert od.Selection(max_bytes=1).kennzeichen() != basis
    assert od.Selection(include_ext=["pdf"]).kennzeichen() != basis
    assert od.Selection(exclude_ext=["mp4"]).kennzeichen() != basis
    assert od.Selection(rules=[(False, "Dateien/x/**")]).kennzeichen() != basis
    assert od.Selection(scope=[(False, "**")]).kennzeichen() != basis
    assert od.Selection(prefix="S/L").kennzeichen() != basis
    # Order of the type sets must not matter.
    assert od.Selection(include_ext=["pdf", "docx"]).kennzeichen() == \
        od.Selection(include_ext=["docx", "PDF"]).kennzeichen()


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


# --------------------------------------------------------------------------
# "Force full sync": pointer, walk and versions go – every file again
# --------------------------------------------------------------------------
def test_full_sync_holt_alles_neu_und_loescht_nichts(tmp_path, monkeypatch, capsys):
    """The button's promise: the drive is walked and fetched as on the first
    run, a local copy is written over, and a file the drive no longer lists
    stays where it is."""
    g = FakeGraph([_datei("1", "a.pdf"), _datei("2", "b.pdf")])
    assert od.lauf(g, tmp_path)["new"] == 2
    (tmp_path / "Dateien/Ordner/a.pdf").write_bytes(b"a" * 10)   # same size, other bytes
    capsys.readouterr()
    # Without the flag the same listing brings nothing: version and size match.
    g1 = FakeGraph([_datei("1", "a.pdf"), _datei("2", "b.pdf")])
    assert od.lauf(g1, tmp_path)["new"] == 0 and g1.geladen == []
    assert (tmp_path / "Dateien/Ordner/a.pdf").read_bytes() == b"a" * 10
    capsys.readouterr()
    monkeypatch.setenv("FULL_SYNC", "1")
    g2 = FakeGraph([_datei("1", "a.pdf")])          # b.pdf no longer listed
    zahlen = od.lauf(g2, tmp_path)
    assert g2.geladen == ["1"] and zahlen["new"] == 1 and zahlen["gone"] == 0
    assert (tmp_path / "Dateien/Ordner/a.pdf").read_bytes() == b"x" * 10
    assert (tmp_path / "Dateien/Ordner/b.pdf").is_file(), "nothing is deleted"
    assert state_db.StateDb(tmp_path).verschwunden_lesen() == {}
    events = [e["k"] for e in _events(capsys)]
    assert "run.full_sync" in events and "run.drive.full" in events
    assert state_db.DbZustand(tmp_path).delta_lesen() == "https://delta/neu"
    # The next regular run is incremental again.
    monkeypatch.delenv("FULL_SYNC")
    g3 = FakeGraph([_datei("1", "a.pdf")])
    assert od.lauf(g3, tmp_path)["new"] == 0 and g3.geladen == []


def test_full_sync_unterbrochen_holt_der_naechste_lauf_den_rest(tmp_path, monkeypatch):
    """The versions are forgotten on disk before the first download: a run
    cut short leaves the rest due, and the next regular run – replanning
    from the stored walk – fetches exactly that."""
    od.lauf(FakeGraph([_datei("1", "a.pdf"), _datei("2", "b.pdf")]), tmp_path)
    monkeypatch.setenv("FULL_SYNC", "1")
    g2 = FakeGraph([_datei("1", "a.pdf"), _datei("2", "b.pdf")], fehlerhaft={"2"})
    assert od.lauf(g2, tmp_path)["errors"] == 1 and g2.geladen == ["1"]
    monkeypatch.delenv("FULL_SYNC")
    g3 = FakeGraph([_datei("1", "a.pdf"), _datei("2", "b.pdf")])
    assert od.lauf(g3, tmp_path)["new"] == 1 and g3.geladen == ["2"]


def test_full_sync_leert_die_warteliste(tmp_path, monkeypatch):
    """A file waiting for its folder's cadence needs no lookup of its own:
    the full walk names it, the gates step aside, it comes."""
    _erster_lauf(tmp_path, monkeypatch)
    od.lauf(_KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2")]), tmp_path)
    assert set(_wartend(tmp_path)) == {"f1"}
    monkeypatch.setenv("FULL_SYNC", "1")
    g = _KadenzGraph([_datei("f1", "f1.jpg", FOTO, ctag="c2"), _datei("a1", "a1.pdf")])
    zahlen = od.lauf(g, tmp_path)
    assert sorted(g.geladen) == ["a1", "f1"] and zahlen["waiting"] == 0
    assert not any(art == "get" for art, _k in g.log), "no lookup per waiting file"
    assert _wartend(tmp_path) == {}
