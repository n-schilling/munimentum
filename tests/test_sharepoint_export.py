"""sharepoint_export.py – addressing, filters and the aggregated runs.

The Graph side is faked throughout; what matters here is the part that is
SharePoint's own: URL -> site -> libraries, the extension filters, and that
several libraries add up to one result event.
"""


import json

import pytest
import requests

import progress
import sharepoint_export as sp
from drive_mirror import Selection


# ---------------------------------------------------------------------------
# site_address: whatever the browser shows must resolve to the site
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url,erwartet", [
    ("https://firma.sharepoint.com/sites/TeamX", "firma.sharepoint.com:/sites/TeamX"),
    ("https://firma.sharepoint.com/sites/TeamX/Freigegebene%20Dokumente/Forms/AllItems.aspx",
     "firma.sharepoint.com:/sites/TeamX"),
    ("https://firma.sharepoint.com/teams/Projekt/Unterordner/tief",
     "firma.sharepoint.com:/teams/Projekt"),
    ("firma.sharepoint.com/sites/TeamX", "firma.sharepoint.com:/sites/TeamX"),
    ("https://firma.sharepoint.com/", "firma.sharepoint.com"),
    ("https://firma.sharepoint.com", "firma.sharepoint.com"),
])
def test_site_address(url, erwartet):
    assert sp.site_address(url) == erwartet


def test_site_address_ohne_host_ist_none():
    assert sp.site_address("///nur/pfad") is None


def test_url_teile_sharing_link_findet_site_und_pfad():
    """The reported case: a sharing link (/:f:/r/…) landed on the root
    site and found nothing there."""
    url = ("https://firma.sharepoint.com/:f:/r/sites/PS-UK"
           "/Projects/N/Nordwind?d=w46c78&csf=1&web=1&e=y7q8ln")
    adresse, rest = sp.url_teile(url)
    assert adresse == "firma.sharepoint.com:/sites/PS-UK"
    assert rest == ["Projects", "N", "Nordwind"]


def test_url_teile_forms_ansicht_nimmt_den_id_parameter():
    url = ("https://firma.sharepoint.com/sites/PS-UK/Projects/Forms/AllItems.aspx"
           "?id=%2Fsites%2FPS-UK%2FProjects%2FN%2FNordwind&viewid=x")
    adresse, rest = sp.url_teile(url)
    assert adresse == "firma.sharepoint.com:/sites/PS-UK"
    assert rest == ["Projects", "N", "Nordwind"]


def test_url_teile_site_ohne_pfad():
    assert sp.url_teile("https://firma.sharepoint.com/sites/TeamX") == (
        "firma.sharepoint.com:/sites/TeamX", [])


# ---------------------------------------------------------------------------
# Selection: the extension filters (the size cap is covered by the mirror tests)
# ---------------------------------------------------------------------------
def test_selection_include_laesst_nur_genannte_typen_durch():
    wahl = Selection(include_ext=["pdf", ".DOCX"])
    assert wahl.takes("Dateien/a.pdf", 1)
    assert wahl.takes("Dateien/b.docx", 1)
    assert not wahl.takes("Dateien/c.mp4", 1)
    assert not wahl.takes("Dateien/ohne_endung", 1)


def test_selection_exclude_gewinnt_gegen_include():
    wahl = Selection(include_ext=["pdf"], exclude_ext=["pdf"])
    assert not wahl.takes("Dateien/a.pdf", 1)


def test_selection_leer_nimmt_alles():
    assert Selection().takes("Dateien/irgendwas.xyz", 10)
    assert Selection().takes("Dateien/ohne_endung", 10)


def test_selection_endung_nur_aus_dem_dateinamen():
    """A dot in a folder name must not count as an extension."""
    wahl = Selection(exclude_ext=["backup"])
    assert wahl.takes("Dateien/alt.backup/liste.txt", 1)
    assert not wahl.takes("Dateien/alt/liste.backup", 1)


# ---------------------------------------------------------------------------
# resolve_drives: broken lines are reported, good ones survive, ids dedupe
# ---------------------------------------------------------------------------
class _FakeGraph:
    def __init__(self, sites=None, kaputt=None):
        self.sites = sites or {}
        self.kaputt = kaputt or {}

    def get(self, url):
        for adresse, antwort in self.kaputt.items():
            if adresse in url:
                fehler = requests.HTTPError(response=antwort)
                raise fehler
        for adresse, site in self.sites.items():
            if f"/sites/{adresse}" in url:
                return {"id": site["id"], "displayName": site["name"]}
        raise RuntimeError(f"unbekannt: {url}")

    def paged(self, url):
        for site in self.sites.values():
            if f"/sites/{site['id']}/drives" in url:
                yield from site["drives"]
                return
        raise RuntimeError(f"unbekannt: {url}")


class _Antwort:
    def __init__(self, status):
        self.status_code = status


def _events(capsys):
    return [e for e in (progress.lies_event(z) for z in
                        capsys.readouterr().out.splitlines()) if e]


def test_resolve_drives_sammelt_bibliotheken_und_dedupliziert(capsys):
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/TeamX": {
        "id": "s1", "name": "Team X",
        "drives": [{"id": "d1", "name": "Dokumente", "driveType": "documentLibrary"},
                   {"id": "d2", "name": "Assets", "driveType": "documentLibrary"},
                   {"id": "d3", "name": "Papierkorb", "driveType": "recycleBin"}]}})
    urls = ["https://firma.sharepoint.com/sites/TeamX",
            "https://firma.sharepoint.com/sites/TeamX/Unterseite"]
    drives, fehl = sp.resolve_drives(g, urls)
    assert fehl == 0
    assert [d["id"] for d in drives] == ["d1", "d2"]      # dedupliziert, ohne Papierkorb
    assert drives[0]["site"] == "Team X"


def test_resolve_drives_pfad_begrenzt_auf_eine_bibliothek(capsys):
    """A folder URL mirrors exactly that subtree – not the whole site."""
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/PS-UK": {
        "id": "s1", "name": "PS UK",
        "drives": [
            {"id": "d1", "name": "Dokumente", "driveType": "documentLibrary",
             "webUrl": "https://firma.sharepoint.com/sites/PS-UK/Freigegebene%20Dokumente"},
            {"id": "d2", "name": "Projects", "driveType": "documentLibrary",
             "webUrl": "https://firma.sharepoint.com/sites/PS-UK/Projects"}]}})
    drives, fehl = sp.resolve_drives(
        g, ["https://firma.sharepoint.com/:f:/r/sites/PS-UK/Projects/N/Nordwind?web=1"])
    assert fehl == 0 and [d["id"] for d in drives] == ["d2"]
    assert drives[0]["prefixes"] == {"N/Nordwind"}

    # The same site in full on top: the wider scope wins.
    drives, _ = sp.resolve_drives(
        g, ["https://firma.sharepoint.com/:f:/r/sites/PS-UK/Projects/N/Nordwind",
            "https://firma.sharepoint.com/sites/PS-UK"])
    d2 = next(d for d in drives if d["id"] == "d2")
    assert d2["prefixes"] is None and len(drives) == 2


def test_drive_auswahl_nimmt_nur_den_teilbaum():
    basis = Selection(exclude_ext=["mp4"])
    wahl = sp.drive_auswahl(basis, {"prefixes": {"N/Nordwind"}})
    assert wahl.takes("Dateien/N/Nordwind/plan.pdf", 1)
    assert wahl.takes("Dateien/N/Nordwind/tief/mehr.docx", 1)
    assert not wahl.takes("Dateien/N/Anderes/plan.pdf", 1)
    assert not wahl.takes("Dateien/oben.pdf", 1)
    assert not wahl.takes("Dateien/N/Nordwind/film.mp4", 1)   # Filter gelten weiter
    assert wahl.pfad_ok("Dateien/N/Nordwind/film.mp4")        # aber im Pfad-Scope


def test_resolve_drives_403_wird_als_verweigert_gemeldet(capsys):
    g = _FakeGraph(kaputt={"sites/geheim": _Antwort(403)})
    drives, fehl = sp.resolve_drives(
        g, ["https://firma.sharepoint.com/sites/geheim"])
    assert drives == [] and fehl == 1
    assert any(e["k"] == "run.sharepoint.denied" for e in _events(capsys))


def test_resolve_drives_kaputte_url_kostet_die_anderen_nicht(capsys):
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/TeamX": {
        "id": "s1", "name": "Team X",
        "drives": [{"id": "d1", "name": "Dokumente", "driveType": "documentLibrary"}]}})
    drives, fehl = sp.resolve_drives(g, ["///kaputt",
                                         "https://firma.sharepoint.com/sites/TeamX"])
    assert fehl == 1 and [d["id"] for d in drives] == ["d1"]
    assert any(e["k"] == "run.sharepoint.site_failed" for e in _events(capsys))


# ---------------------------------------------------------------------------
# The aggregated runs
# ---------------------------------------------------------------------------
def test_lauf_summiert_ueber_bibliotheken(tmp_path, monkeypatch, capsys):
    drives = [{"id": "d1", "site": "S", "name": "A"},
              {"id": "d2", "site": "S", "name": "B"}]

    def fake_lauf(graph, out, wahl, arbeiter, still=False, sammler=None,
                  zustand=None):
        assert still, "je Bibliothek darf kein eigenes RESULT kommen"
        return {"new": 2, "excluded": 1, "errors": 0, "moved": 0, "gone": 1}

    monkeypatch.setattr(sp.drive_mirror, "lauf", fake_lauf)

    class G:
        pass

    sp.lauf(G(), tmp_path, drives, fehl=1)
    out = capsys.readouterr().out
    letzte = [z for z in out.splitlines() if progress.lies_ergebnis(z)]
    assert len(letzte) == 1
    e = progress.lies_ergebnis(letzte[0])
    assert e == {"new": 4, "excluded": 2, "errors": 1,
                 "extra": {"moved": 0, "gone": 2}}
    assert G().__dict__ == {}                      # drive_base steht am Client


def test_je_drive_setzt_die_drive_basis(tmp_path):
    class G:
        pass
    g = G()
    drives = [{"id": "d7", "site": "S", "name": "A"}]
    for _ in sp.je_drive(g, drives):
        assert g.drive_base.endswith("/drives/d7")


def test_preview_schreibt_gesamtbericht_mit_bytes(tmp_path, monkeypatch, capsys):
    drives = [{"id": "d1", "site": "S", "name": "A"},
              {"id": "d2", "site": "S", "name": "B"}]

    berichte = iter([
        {"erwartet": 10, "vorhanden": 4, "geloescht": 0, "fehlt": 6,
         "ausgelassen": 2, "bytes": 3 * 1048576, "bytes_ausgelassen": 1048576},
        {"erwartet": 5, "vorhanden": 5, "geloescht": 1, "fehlt": 0,
         "ausgelassen": 0, "bytes": 1048576, "bytes_ausgelassen": 0}])
    monkeypatch.setattr(sp.drive_mirror, "nur_pruefen",
                        lambda graph, out, wahl, still=False, sammler=None,
                        zustand=None: next(berichte))

    class G:
        pass

    bericht = sp.nur_pruefen(G(), tmp_path, drives)
    assert bericht["erwartet"] == 15 and bericht["fehlt"] == 6
    assert bericht["bytes"] == 4 * 1048576
    gespeichert = sp.state_db.StateDb(tmp_path).bericht_lesen()
    assert {z["ordner"] for z in gespeichert["ordner"]} == {"S/A", "S/B"}
    events = _events(capsys)
    vorschau = [e for e in events if e["k"] == "run.sharepoint.preview"]
    assert len(vorschau) == 2 and vorschau[0]["v"]["mb"] == 3



# ---------------------------------------------------------------------------
# Site pages: rendering, incremental run, tombstones
# ---------------------------------------------------------------------------
def test_render_page_haelt_text_und_benennt_platzhalter():
    layout = {"horizontalSections": [{"columns": [{"webparts": [
        {"@odata.type": "#microsoft.graph.textWebPart",
         "innerHtml": "<p>Hallo <b>Welt</b></p>"},
        {"@odata.type": "#microsoft.graph.standardWebPart",
         "data": {"title": "Quick Links"}}]}]}],
        "verticalSection": {"webparts": [
            {"@odata.type": "#microsoft.graph.textWebPart",
             "innerHtml": "<p>Seitenleiste</p>"}]}}
    html = sp.render_page({"title": "Start <x>", "lastModifiedDateTime":
                           "2026-08-01T00:00:00Z"}, layout)
    assert "Hallo <b>Welt</b>" in html and "Seitenleiste" in html
    assert "[Quick Links]" in html
    assert "<title>Start &lt;x></title>" in html


class _SeitenGraph:
    def __init__(self):
        self.seiten = [{"id": "p1", "name": "Home.aspx", "title": "Home",
                        "eTag": "e1"}]
        self.layout = {"horizontalSections": [{"columns": [{"webparts": [
            {"@odata.type": "#microsoft.graph.textWebPart",
             "innerHtml": "<p>Inhalt der Startseite</p>"}]}]}]}
        self.detailabrufe = 0

    def paged(self, url, params=None):
        assert "/pages/microsoft.graph.sitePage" in url
        yield from self.seiten

    def get(self, url):
        self.detailabrufe += 1
        s = dict(self.seiten[0])
        s["canvasLayout"] = self.layout
        return s


def test_seiten_lauf_schreibt_und_ueberspringt(tmp_path, capsys):
    g = _SeitenGraph()
    sites = [{"id": "s1", "pfad": ["Team X"]}]
    assert sp.seiten_lauf(g, tmp_path, sites) == 1
    dateien = list(tmp_path.rglob("*.html"))
    assert len(dateien) == 1 and dateien[0].parent.name == "Team X"
    assert "Inhalt der Startseite" in dateien[0].read_text(encoding="utf-8")

    # Second run, same eTag: no detail fetch, nothing new.
    capsys.readouterr()
    assert sp.seiten_lauf(g, tmp_path, sites) == 0
    assert g.detailabrufe == 1
    e = progress.lies_ergebnis(
        [z for z in capsys.readouterr().out.splitlines()
         if progress.lies_ergebnis(z)][0])
    assert e["new"] == 0 and e["unchanged"] == 1


def test_seiten_lauf_setzt_grabsteine(tmp_path, capsys):
    g = _SeitenGraph()
    sites = [{"id": "s1", "pfad": ["Team X"]}]
    sp.seiten_lauf(g, tmp_path, sites)
    g.seiten = []                                   # page gone at Microsoft
    sp.seiten_lauf(g, tmp_path, sites)
    weg = sp.state_db.StateDb(tmp_path).verschwunden_lesen()
    assert len(weg) == 1 and next(iter(weg)).startswith("Team X/")
    # The file itself stays – the same promise as everywhere.
    assert list(tmp_path.rglob("*.html"))


def test_resolve_page_sites_steigt_ab_und_dedupliziert(capsys):
    class G:
        def get(self, url):
            assert "/sites/firma.sharepoint.com:/sites/TeamX" in url
            return {"id": "s1", "displayName": "Team X"}

        def paged(self, url, params=None):
            if "/sites/s1/sites" in url:
                yield {"id": "s2", "displayName": "Unter"}
            elif "/sites/s2/sites" in url:
                return
    sites, fehl = sp.resolve_page_sites(
        G(), ["https://firma.sharepoint.com/sites/TeamX",
              "https://firma.sharepoint.com/sites/TeamX"])
    assert fehl == 0
    assert [s["pfad"] for s in sites] == [["Team X"], ["Team X", "Unter"]]


# ---------------------------------------------------------------------------
# Images in pages: embedded as data URIs, failures keep the link
# ---------------------------------------------------------------------------
class _BildGraph:
    def __init__(self, inhalt=b"PNGDATEN", fehler=False):
        self.inhalt = inhalt
        self.fehler = fehler
        self.urls = []
        self.inhaltsabrufe = 0

    def get(self, url):
        if self.fehler:
            raise RuntimeError("403")
        assert "$select=size" in url
        return {"size": len(self.inhalt)}

    def get_bytes(self, url, label=""):
        self.urls.append(url)
        self.inhaltsabrufe += 1
        if self.fehler:
            raise RuntimeError("403")
        return self.inhalt, "image/png"


def test_bilder_einbetten_ersetzt_relative_und_absolute_quellen():
    g = _BildGraph()
    html = ('<p><img class="x" src="/sites/TeamX/SiteAssets/logo.png"></p>'
            '<img src="https://firma.sharepoint.com/bild.jpg">'
            '<img src="data:image/gif;base64,AA==">')
    z = {"bilder": 0, "fehl": 0}
    aus = sp.bilder_einbetten(g, html, "firma.sharepoint.com", z)
    assert z == {"bilder": 2, "fehl": 0}
    assert aus.count("data:image/png;base64,") == 2
    assert "data:image/gif;base64,AA==" in aus          # already embedded
    # The shares detour carries the full URL, base64url-encoded.
    assert all("/shares/u!" in u for u in g.urls)


def test_bilder_einbetten_laesst_bei_fehler_den_link_stehen():
    g = _BildGraph(fehler=True)
    z = {"bilder": 0, "fehl": 0}
    aus = sp.bilder_einbetten(g, '<img src="/a/b.png">', "h", z)
    assert 'src="/a/b.png"' in aus and z["fehl"] == 1


def test_bilder_einbetten_ueberspringt_zu_grosse_ohne_download():
    g = _BildGraph(inhalt=b"x" * 9)
    z = {"bilder": 0, "fehl": 0}
    aus = sp.bilder_einbetten(g, '<img src="/a/b.png">', "h", z, grenze=8)
    assert 'src="/a/b.png"' in aus and z == {"bilder": 0, "fehl": 0}
    # The size probe must have answered this – no wasted content download.
    assert g.inhaltsabrufe == 0
    # 0 means no limit: fetched directly, no probe.
    aus = sp.bilder_einbetten(g, '<img src="/a/b.png">', "h", z, grenze=0)
    assert "data:image/png" in aus and g.inhaltsabrufe == 1


def test_bilder_einbetten_laedt_jede_quelle_nur_einmal():
    """A shared banner on every page must cost one download per run."""
    g = _BildGraph()
    z = {"bilder": 0, "fehl": 0}
    cache = {}
    for _ in range(3):
        aus = sp.bilder_einbetten(g, '<img src="/a/logo.png">', "h", z,
                                  cache=cache)
        assert "data:image/png" in aus
    assert g.inhaltsabrufe == 1 and z["bilder"] == 3


def test_webpart_mit_imagesources_wird_zum_img():
    wp = {"@odata.type": "#microsoft.graph.standardWebPart",
          "data": {"serverProcessedContent": {"imageSources": [
              {"key": "imageSource", "value": "/sites/T/SiteAssets/foto.jpg"}]}}}
    aus = sp._webpart_html(wp)
    assert '<img src="/sites/T/SiteAssets/foto.jpg"' in aus


def test_seiten_lauf_bettet_bilder_ein(tmp_path):
    g = _SeitenGraph()
    g.layout = {"horizontalSections": [{"columns": [{"webparts": [
        {"@odata.type": "#microsoft.graph.textWebPart",
         "innerHtml": '<p><img src="/sites/T/SiteAssets/logo.png"></p>'}]}]}]}
    g.get_bytes = lambda url, label="": (b"BILD", "image/png")
    sites = [{"id": "s1", "pfad": ["Team X"], "host": "firma.sharepoint.com"}]
    sp.seiten_lauf(g, tmp_path, sites)
    html = next(tmp_path.rglob("*.html")).read_text(encoding="utf-8")
    assert "data:image/png;base64," in html


def test_seiten_pruefen_zaehlt_je_site(tmp_path, capsys):
    """The pages check: what Microsoft lists per site against what lies
    here – same report shape as the mirror check."""
    g = _SeitenGraph()
    sites = [{"id": "s1", "pfad": ["Team X"], "host": "h"}]
    sp.seiten_lauf(g, tmp_path, sites)          # eine Seite liegt jetzt hier
    g.seiten.append({"id": "p2", "name": "Neu.aspx", "title": "Neu",
                     "eTag": "e2"})
    capsys.readouterr()
    b = sp.seiten_pruefen(g, tmp_path, sites)
    assert b["erwartet"] == 2 and b["vorhanden"] == 1 and b["fehlt"] == 1
    assert b["ordner"][0]["ordner"] == "Team X"
    assert sp.state_db.StateDb(tmp_path).bericht_lesen()["erwartet"] == 2
    e = progress.lies_ergebnis(
        [z for z in capsys.readouterr().out.splitlines()
         if progress.lies_ergebnis(z)][0])
    assert e["extra"] == {"expected": 2, "present": 1, "missing": 1}


def test_seiten_lauf_grabstein_nur_bei_sauberer_site(tmp_path, capsys):
    """A failed page listing (or a removed URL) proves nothing about the
    site's pages – no tombstones, no inventory eviction."""
    g = _SeitenGraph()
    sites = [{"id": "s1", "pfad": ["Team X"], "host": "h"}]
    sp.seiten_lauf(g, tmp_path, sites)

    def kaputt(url, params=None):
        raise RuntimeError("503")

    g.paged = kaputt
    sp.seiten_lauf(g, tmp_path, sites)
    db = sp.state_db.StateDb(tmp_path)
    assert db.verschwunden_lesen() == {}
    assert db.seiten_lesen()

    # URL removed from the configuration: same promise.
    sp.seiten_lauf(g, tmp_path, [])
    assert db.verschwunden_lesen() == {}


def test_praefix_aufnehmen_haelt_die_menge_flach():
    """Nested prefixes would walk (and download) the same files twice."""
    menge = {"A"}
    sp._praefix_aufnehmen(menge, "A/B")
    assert menge == {"A"}
    menge = {"A/B", "C"}
    sp._praefix_aufnehmen(menge, "A")
    assert menge == {"A", "C"}


def test_resolve_page_sites_trennt_gleichnamige_sites():
    """Two sites sharing a display name must not share an output folder."""
    class G:
        def get(self, url):
            kennung = "s1" if "/sites/A" in url else "s2"
            return {"id": kennung, "displayName": "Projekte",
                    "webUrl": "https://firma.sharepoint.com/sites/x"}

        def paged(self, url, params=None):
            return iter(())

    sites, fehl = sp.resolve_page_sites(
        G(), ["https://firma.sharepoint.com/sites/A",
              "https://firma.sharepoint.com/sites/B"])
    assert fehl == 0 and len(sites) == 2
    assert len({tuple(s["pfad"]) for s in sites}) == 2


def test_seiten_lauf_zieht_umbenannte_seite_um(tmp_path):
    """A renamed page must not leave its old file behind as a stale twin."""
    g = _SeitenGraph()
    sites = [{"id": "s1", "pfad": ["Team X"], "host": "h"}]
    sp.seiten_lauf(g, tmp_path, sites)
    g.seiten[0] = {**g.seiten[0], "name": "Neu.aspx", "eTag": "e2"}
    sp.seiten_lauf(g, tmp_path, sites)
    namen = sorted(p.name for p in tmp_path.rglob("*.html"))
    assert len(namen) == 1 and namen[0].startswith("Neu")
    assert sp.state_db.StateDb(tmp_path).verschwunden_lesen() == {}


def test_seiten_pruefen_zaehlt_grabstein_genau_einmal(tmp_path, capsys):
    """A tombstone under a subsite belongs to the deepest row only."""
    g = _SeitenGraph()
    g.seiten = []
    sites = [{"id": "s1", "pfad": ["A"], "host": "h"},
             {"id": "s2", "pfad": ["A", "Sub"], "host": "h"}]
    sp.state_db.StateDb(tmp_path).verschwunden_ergaenzen(
        ["A/Sub/page.html"], "2026-08-01T00:00:00+00:00")
    b = sp.seiten_pruefen(g, tmp_path, sites)
    assert b["geloescht"] == 1
    je = {z["ordner"]: z["geloescht"] for z in b["ordner"]}
    assert je == {"A": 0, "A/Sub": 1}


def test_gescopte_bibliothek_laeuft_ueber_das_delta(tmp_path):
    """A folder URL rides the drive delta: out-of-scope entries are ignored
    silently (not counted as excluded), deletions come from the feed, and an
    unchanged library costs one request on the next run."""
    import drive_mirror

    class FakeGraph:
        def __init__(self):
            self.aufrufe = []

        def delta(self, weiter=None):
            self.aufrufe.append(weiter)
            if weiter == "delta-1":
                yield None, "delta-2"          # nichts geändert
                return
            yield {"id": "in1", "name": "plan.pdf", "file": {}, "size": 1,
                   "cTag": "c1",
                   "parentReference": {"path": "/drive/root:/N/Nordwind"}}, None
            yield {"id": "out1", "name": "fremd.pdf", "file": {}, "size": 1,
                   "cTag": "c1",
                   "parentReference": {"path": "/drive/root:/Anderes"}}, None
            yield None, "delta-1"

        def lade(self, item_id, ziel, geaendert=None):
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_bytes(b"x")
            return 1

    g = FakeGraph()
    wahl = sp.drive_auswahl(Selection(), {"prefixes": {"N/Nordwind"}})
    zustand = sp.state_db.DbZustand(tmp_path)
    zahlen = drive_mirror.lauf(g, tmp_path, wahl, 1, still=True,
                               zustand=zustand)
    assert zahlen == {"new": 1, "excluded": 0, "errors": 0,
                      "moved": 0, "gone": 0}
    assert (tmp_path / "Dateien/N/Nordwind/plan.pdf").is_file()
    assert not (tmp_path / "Dateien/Anderes").exists()

    zahlen = drive_mirror.lauf(g, tmp_path, wahl, 1, still=True,
                               zustand=zustand)
    assert zahlen["new"] == 0 and g.aufrufe == [None, "delta-1"]


def _drive_datei(kennung, name, ctag="c1"):
    return {"id": kennung, "name": name, "file": {}, "size": 1, "cTag": ctag,
            "parentReference": {"path": "/drive/root:"}}


def _lade_x(item_id, ziel, geaendert=None):
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_bytes(b"x")
    return 1


def test_walk_setzt_nach_abbruch_am_cursor_fort(tmp_path, capsys):
    """A killed first walk does not start over: the next run resumes at the
    stored page link and both halves add up to one complete mirror."""
    import drive_mirror

    class G:
        def __init__(self):
            self.aufrufe = []

        def delta_seiten(self, weiter=None):
            self.aufrufe.append(weiter)
            if weiter is None:
                yield [_drive_datei("a", "a.pdf")], "seite-2", None
                raise requests.HTTPError(response=_Antwort(500))
            assert weiter == "seite-2"
            yield [_drive_datei("b", "b.pdf")], None, "delta-1"

        lade = staticmethod(_lade_x)

    g = G()
    zustand = sp.state_db.DbZustand(tmp_path)
    with pytest.raises(requests.HTTPError):
        drive_mirror.lauf(g, tmp_path, Selection(), 1, still=True,
                          zustand=zustand)
    db = sp.state_db.StateDb(tmp_path)
    assert db.walk_status() == {"cursor": "seite-2", "fertig": None, "n": 1}

    capsys.readouterr()
    zahlen = drive_mirror.lauf(g, tmp_path, Selection(), 1, still=True,
                               zustand=zustand)
    assert g.aufrufe == [None, "seite-2"]
    assert zahlen["new"] == 2 and zahlen["errors"] == 0
    assert (tmp_path / "Dateien/a.pdf").is_file()
    assert (tmp_path / "Dateien/b.pdf").is_file()
    assert db.delta_lesen() == "delta-1"
    assert db.walk_status() == {"cursor": None, "fertig": None, "n": 0}
    assert any(e["k"] == "run.drive.resume" and e["v"]["n"] == 1
               for e in _events(capsys))


def test_download_fehler_fuehrt_zu_replan_ohne_neue_aufzaehlung(tmp_path,
                                                                capsys):
    """When only downloads failed, the stored walk is complete: the next run
    plans from it, retries just the missing file and only then advances the
    delta pointer."""
    import drive_mirror

    class G:
        def __init__(self):
            self.walks = 0
            self.kaputt = True

        def delta_seiten(self, weiter=None):
            self.walks += 1
            yield [_drive_datei("a", "a.pdf"), _drive_datei("b", "b.pdf")], \
                None, "delta-1"

        def lade(self, item_id, ziel, geaendert=None):
            if self.kaputt and item_id == "b":
                raise RuntimeError("kaputt")
            return _lade_x(item_id, ziel, geaendert)

    g = G()
    zustand = sp.state_db.DbZustand(tmp_path)
    zahlen = drive_mirror.lauf(g, tmp_path, Selection(), 1, still=True,
                               zustand=zustand)
    assert zahlen["new"] == 1 and zahlen["errors"] == 1
    db = sp.state_db.StateDb(tmp_path)
    assert db.delta_lesen() is None               # Zeiger rückt nicht vor
    assert db.walk_status()["fertig"] == "delta-1"

    g.kaputt = False
    capsys.readouterr()
    zahlen = drive_mirror.lauf(g, tmp_path, Selection(), 1, still=True,
                               zustand=zustand)
    assert g.walks == 1                           # keine zweite Aufzählung
    assert zahlen["new"] == 1 and zahlen["errors"] == 0
    assert (tmp_path / "Dateien/b.pdf").is_file()
    assert db.delta_lesen() == "delta-1"
    assert db.walk_status() == {"cursor": None, "fertig": None, "n": 0}
    assert any(e["k"] == "run.drive.replan" for e in _events(capsys))


def test_veralteter_cursor_faellt_auf_volle_aufzaehlung_zurueck(tmp_path,
                                                                capsys):
    """410 on a stored walk cursor: staging is dropped and the walk restarts
    in full – without doubling the entries it had already stored."""
    import drive_mirror

    db = sp.state_db.StateDb(tmp_path)
    db.walk_ergaenzen([_drive_datei("alt", "alt.pdf")], "cursor-alt")

    class G:
        def __init__(self):
            self.aufrufe = []

        def delta_seiten(self, weiter=None):
            self.aufrufe.append(weiter)
            if weiter == "cursor-alt":
                raise requests.HTTPError(response=_Antwort(410))
            yield [_drive_datei("a", "a.pdf")], None, "delta-2"

        lade = staticmethod(_lade_x)

    g = G()
    zahlen = drive_mirror.lauf(g, tmp_path, Selection(), 1, still=True,
                               zustand=sp.state_db.DbZustand(tmp_path))
    assert g.aufrufe == ["cursor-alt", None]
    assert zahlen["new"] == 1
    assert not (tmp_path / "Dateien/alt.pdf").exists()
    assert db.delta_lesen() == "delta-2"


def test_verschlanke_behaelt_nur_die_gelesenen_felder():
    import drive_mirror

    roh = {"id": "1", "name": "a.pdf", "size": 5, "cTag": "c",
           "file": {"mimeType": "application/pdf",
                    "hashes": {"quickXorHash": "x"}},
           "parentReference": {"path": "/drive/root:/A", "driveId": "d",
                               "id": "p", "siteId": "s"},
           "fileSystemInfo": {"lastModifiedDateTime": "2026-01-01T00:00:00Z",
                              "createdDateTime": "2020-01-01T00:00:00Z"},
           "createdBy": {"user": {"displayName": "Jemand"}},
           "webUrl": "https://firma.sharepoint.com/x", "eTag": "e"}
    s = drive_mirror.verschlanke(roh)
    assert s == {"id": "1", "name": "a.pdf", "size": 5, "cTag": "c",
                 "file": {}, "parentReference": {"path": "/drive/root:/A"},
                 "fileSystemInfo":
                 {"lastModifiedDateTime": "2026-01-01T00:00:00Z"}}
    assert drive_mirror.rel_pfad(s) == "Dateien/A/a.pdf"


def test_plane_dedupliziert_wiederholte_eintraege(tmp_path):
    """Delta may name the same item twice (and a resumed walk repeats a
    page) – the plan must hold one download, not two threads on one file."""
    import drive_mirror

    bestand = drive_mirror.Bestand()
    plan = drive_mirror.plane(
        [_drive_datei("a", "a.pdf", ctag="c1"),
         _drive_datei("a", "a.pdf", ctag="c2")],
        bestand, tmp_path, Selection())
    assert len(plan["laden"]) == 1 and plan["laden"][0]["ctag"] == "c2"


def test_lange_aufzaehlung_meldet_zwischenstand(capsys):
    """A first walk over a big drive is minutes of silence otherwise – every
    2000 entries one line proves the run is alive."""
    import drive_mirror

    def delta(weiter=None):
        for i in range(4100):
            yield {"id": str(i)}, None
        yield None, "link"

    eintraege, link = drive_mirror.sammle(
        type("G", (), {"delta": staticmethod(delta)})(), None)
    assert len(eintraege) == 4100 and link == "link"
    takt = [e["v"]["n"] for e in _events(capsys)
            if e["k"] == "run.drive.walking"]
    assert takt == [2000, 4000]


# ---------------------------------------------------------------------------
# Cadence: units below their interval are skipped, with a clear line
# ---------------------------------------------------------------------------
def test_resolve_drives_haengt_die_url_kadenz_an(capsys, monkeypatch):
    """Cadence lives on the source URL; two URLs feeding one drive merge to
    the more frequent one."""
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/TeamX": {
        "id": "s1", "name": "Team X",
        "drives": [{"id": "d1", "name": "Dokumente",
                    "driveType": "documentLibrary"}]}})
    urls = ["https://firma.sharepoint.com/sites/TeamX",
            "https://firma.sharepoint.com/sites/TeamX/Unterseite"]
    monkeypatch.setenv("SYNC_CADENCE", json.dumps(
        {f"sharepoint-url:{urls[0]}": "monthly",
         f"sharepoint-url:{urls[1]}": "weekly"}))
    drives, fehl = sp.resolve_drives(g, urls)
    assert fehl == 0 and drives[0]["kadenz"] == "weekly"


def test_lauf_ueberspringt_bibliothek_unter_ihrer_kadenz(tmp_path, monkeypatch,
                                                         capsys):
    drives = [{"id": "d1", "site": "S", "name": "A", "kadenz": "weekly"},
              {"id": "d2", "site": "S", "name": "B"}]
    gelaufen = []

    def fake_lauf(graph, out, wahl, arbeiter, still=False, zustand=None):
        gelaufen.append(str(out))
        return {"new": 1, "excluded": 0, "errors": 0, "moved": 0, "gone": 0}

    monkeypatch.setattr(sp.drive_mirror, "lauf", fake_lauf)
    # d1 lief gerade eben – unter der Wochen-Kadenz nicht fällig.
    import time
    sp.state_db.StateDb(sp.drive_ziel(tmp_path, drives[0]))._kv_schreiben(
        "last_sync", str(time.time()))

    class G:
        pass

    summe = sp.lauf(G(), tmp_path, drives)
    assert len(gelaufen) == 1 and gelaufen[0].endswith("B")
    events = _events(capsys)
    skip = [e for e in events if e["k"] == "run.cadence.skip"]
    assert len(skip) == 1 and skip[0]["v"]["name"] == "S / A"
    assert skip[0]["v"]["cadence"]["k"] == "cadence.weekly"
    assert summe["new"] == 1


def test_sync_jetzt_ignoriert_kadenz(tmp_path, monkeypatch):
    """SYNC_NOW is the "sync now" button: cadence bypassed (the unit filter
    happens upstream via the URL override)."""
    drives = [{"id": "d1", "site": "S", "name": "A", "kadenz": "monthly"}]
    gelaufen = []

    def fake_lauf(graph, out, wahl, arbeiter, still=False, zustand=None):
        gelaufen.append(str(out))
        return {"new": 1, "excluded": 0, "errors": 0, "moved": 0, "gone": 0}

    monkeypatch.setattr(sp.drive_mirror, "lauf", fake_lauf)
    monkeypatch.setenv("SYNC_NOW", "1")
    import time
    sp.state_db.StateDb(sp.drive_ziel(tmp_path, drives[0]))._kv_schreiben(
        "last_sync", str(time.time()))

    class G:
        pass

    sp.lauf(G(), tmp_path, drives)
    assert len(gelaufen) == 1 and gelaufen[0].endswith("A")


def test_seiten_lauf_ueberspringt_site_unter_kadenz(tmp_path, capsys):
    """A skipped site keeps its pages untouched – no tombstones, no
    re-render, and the skip is one clear log line."""
    g = _SeitenGraph()
    sites = [{"id": "s1", "pfad": ["Team X"], "host": "h"}]
    sp.seiten_lauf(g, tmp_path, sites)
    sites[0]["kadenz"] = "monthly"
    capsys.readouterr()
    sp.seiten_lauf(g, tmp_path, sites)
    db = sp.state_db.StateDb(tmp_path)
    assert db.verschwunden_lesen() == {} and db.seiten_lesen()
    events = _events(capsys)
    assert any(e["k"] == "run.cadence.skip" and e["v"]["name"] == "Team X"
               for e in events)
    assert g.detailabrufe == 1                      # nichts erneut geholt
