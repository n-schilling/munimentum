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
    url = ("https://firma.sharepoint.com/:f:/r/sites/Workspace"
           "/Templates/Folder/Nordwind?web=1")
    adresse, rest = sp.url_teile(url)
    assert adresse == "firma.sharepoint.com:/sites/Workspace"
    assert rest == ["Templates", "Folder", "Nordwind"]


def test_url_teile_forms_ansicht_nimmt_den_id_parameter():
    url = ("https://firma.sharepoint.com/sites/Workspace/Templates/Forms/AllItems.aspx"
           "?id=%2Fsites%2FWorkspace%2FTemplates%2FFolder%2FNordwind&viewid=x")
    adresse, rest = sp.url_teile(url)
    assert adresse == "firma.sharepoint.com:/sites/Workspace"
    assert rest == ["Templates", "Folder", "Nordwind"]


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
            if url.endswith(f"/sites/{adresse}"):
                return {"id": site["id"], "displayName": site["name"]}
        raise requests.HTTPError(response=_Antwort(404))     # as Graph: no such site

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
        "drives": [{"id": "d1", "name": "Dokumente", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/TeamX/Dokumente"},
                   {"id": "d2", "name": "Assets", "driveType": "documentLibrary"},
                   {"id": "d3", "name": "Papierkorb", "driveType": "recycleBin"}]}})
    urls = ["https://firma.sharepoint.com/sites/TeamX",
            "https://firma.sharepoint.com/sites/TeamX/Dokumente"]
    drives, fehl = sp.resolve_drives(g, urls)
    assert fehl == 0
    assert [d["id"] for d in drives] == ["d1", "d2"]      # deduped, no recycle bin
    assert drives[0]["site"] == "Team X"


def test_several_urls_into_one_site_ask_for_it_once(capsys):
    """Six URLs into one site cost two requests, not twelve."""
    class Counting(_FakeGraph):
        calls = 0

        def get(self, url):
            Counting.calls += 1
            return super().get(url)

        def paged(self, url):
            Counting.calls += 1
            yield from super().paged(url)
    g = Counting(sites={"firma.sharepoint.com:/sites/Workspace": {
        "id": "s1", "name": "Workspace",
        "drives": [{"id": "d2", "name": "Templates", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/Workspace/Templates"}]}})
    urls = [f"https://firma.sharepoint.com/:f:/r/sites/Workspace/Templates/Folder/Sub{i}" for i in range(6)]
    drives, fehl = sp.resolve_drives(g, urls)
    assert fehl == 0 and Counting.calls == 2
    assert drives[0]["prefixes"] == {f"Folder/Sub{i}" for i in range(6)}


def test_several_urls_into_one_site_say_so_in_one_line(capsys):
    """Seven URLs into one site used to log "Workspace: 1 libraries." seven
    times; now one line per site counts its libraries and addresses."""
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/Workspace": {
        "id": "s1", "name": "Workspace",
        "drives": [{"id": "d1", "name": "Documents", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/Workspace/Shared%20Documents"},
                   {"id": "d2", "name": "Templates", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/Workspace/Templates"}]}})
    urls = [f"https://firma.sharepoint.com/:f:/r/sites/Workspace/Templates/Folder/Sub{i}" for i in range(6)]
    urls.append("https://firma.sharepoint.com/:f:/r/sites/Workspace/Shared%20Documents/A")
    sp.resolve_drives(g, urls)
    lines = [e for e in _events(capsys) if e["k"].startswith("run.sharepoint.libraries")]
    assert lines == [{"k": "run.sharepoint.libraries_from", "level": "info",
                      "v": {"site": "Workspace", "n": 2,
                            "urls": {"k": "run.sharepoint.addresses", "v": {"n": 7}}}}]
    sp.resolve_drives(g, ["https://firma.sharepoint.com/sites/Workspace/Templates"])
    lines = [e for e in _events(capsys) if e["k"].startswith("run.sharepoint.libraries")]
    assert [(e["k"], e["v"]["n"]) for e in lines] == [("run.sharepoint.libraries", 1)]


def test_a_token_that_runs_out_keeps_the_lines_already_resolved(capsys):
    import auth

    class RunsOut(_FakeGraph):
        def get(self, url):
            if "Second" in url:
                raise auth.TokenExpired()
            return super().get(url)
    g = RunsOut(sites={"firma.sharepoint.com:/sites/First": {
        "id": "s1", "name": "First",
        "drives": [{"id": "d1", "name": "Documents", "driveType": "documentLibrary"}]}})
    with pytest.raises(auth.TokenExpired):
        sp.resolve_drives(g, ["https://firma.sharepoint.com/sites/First",
                              "https://firma.sharepoint.com/sites/Second"])
    lines = [(e["k"], e["v"]["site"]) for e in _events(capsys)
             if e["k"].startswith("run.sharepoint.libraries")]
    assert lines == [("run.sharepoint.libraries", "First")]


def test_resolve_drives_pfad_begrenzt_auf_eine_bibliothek(capsys):
    """A folder URL mirrors exactly that subtree – not the whole site."""
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/Workspace": {
        "id": "s1", "name": "Workspace",
        "drives": [
            {"id": "d1", "name": "Dokumente", "driveType": "documentLibrary",
             "webUrl": "https://firma.sharepoint.com/sites/Workspace/Freigegebene%20Dokumente"},
            {"id": "d2", "name": "Templates", "driveType": "documentLibrary",
             "webUrl": "https://firma.sharepoint.com/sites/Workspace/Templates"}]}})
    drives, fehl = sp.resolve_drives(
        g, ["https://firma.sharepoint.com/:f:/r/sites/Workspace/Templates/Folder/Nordwind?web=1"])
    assert fehl == 0 and [d["id"] for d in drives] == ["d2"]
    assert drives[0]["prefixes"] == {"Folder/Nordwind"}

    # The same site in full on top: the wider scope wins.
    drives, _ = sp.resolve_drives(
        g, ["https://firma.sharepoint.com/:f:/r/sites/Workspace/Templates/Folder/Nordwind",
            "https://firma.sharepoint.com/sites/Workspace"])
    d2 = next(d for d in drives if d["id"] == "d2")
    assert d2["prefixes"] is None and len(drives) == 2


def test_drive_auswahl_nimmt_nur_den_teilbaum():
    basis = Selection(exclude_ext=["mp4"])
    wahl = sp.drive_auswahl(basis, {"prefixes": {"Folder/Nordwind"}})
    assert wahl.takes("Dateien/Folder/Nordwind/plan.pdf", 1)
    assert wahl.takes("Dateien/Folder/Nordwind/tief/mehr.docx", 1)
    assert not wahl.takes("Dateien/Folder/Other/plan.pdf", 1)
    assert not wahl.takes("Dateien/oben.pdf", 1)
    assert not wahl.takes("Dateien/Folder/Nordwind/film.mp4", 1)   # filters still apply
    assert wahl.pfad_ok("Dateien/Folder/Nordwind/film.mp4")        # but in path scope


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

    namen = []

    def fake_lauf(graph, out, wahl, arbeiter, still=False, sammler=None,
                  zustand=None, name=None, einheiten=None):
        assert still, "je Bibliothek darf kein eigenes RESULT kommen"
        namen.append(name)
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
    assert namen == ["S / A", "S / B"], "the log calls the mirror by site and library"
    assert G().__dict__ == {}                      # drive_base sits on the client


def test_je_drive_setzt_die_drive_basis(tmp_path):
    class G:
        pass
    g = G()
    drives = [{"id": "d7", "site": "S", "name": "A"}]
    for _ in sp.je_drive(g, drives):
        assert g.drive_base.endswith("/drives/d7")


def _bibliothek(da, offen, ausgeschlossen=0, behalten=0, wartend=0, mb=0, mb_aus=0):
    import completeness
    return completeness.bilanz("sharepoint", "files", da=da, offen=offen,
                               ausgeschlossen=ausgeschlossen, behalten=behalten,
                               wartend=wartend,
                               extra={"bytes": mb * 1048576,
                                      "bytes_ausgeschlossen": mb_aus * 1048576,
                                      "typen": [{"ext": "pdf", "n": da + offen,
                                                 "bytes": mb * 1048576}]})


def test_preview_schreibt_eine_bilanz_ueber_alle_bibliotheken(tmp_path, monkeypatch, capsys):
    """One report at the root, one row per "site/library" with something
    open, the bytes and types merged for the size preview."""
    import completeness
    drives = [{"id": "d1", "site": "S", "name": "A"},
              {"id": "d2", "site": "S", "name": "B"}]
    berichte = iter([_bibliothek(4, 6, ausgeschlossen=2, mb=3, mb_aus=1),
                     _bibliothek(5, 0, behalten=1, mb=1)])
    monkeypatch.setattr(sp.drive_mirror, "nur_pruefen",
                        lambda graph, out, wahl, still=False, zustand=None,
                        quelle="onedrive": next(berichte))

    class G:
        pass

    bericht = sp.nur_pruefen(G(), tmp_path, drives)
    assert (bericht["da"], bericht["offen"], bericht["ausgeschlossen"], bericht["behalten"]) == (9, 6, 2, 1)
    assert bericht["bytes"] == 4 * 1048576 and bericht["bytes_ausgeschlossen"] == 1048576
    assert bericht["typen"] == [{"ext": "pdf", "n": 15, "bytes": 4 * 1048576}]
    gespeichert = completeness.lesen(sp.state_db.StateDb(tmp_path), "sharepoint")
    assert [(z["pfad"], z["offen"]) for z in gespeichert["zeilen"]] == [("S/A", 6)]
    events = _events(capsys)
    vorschau = [e for e in events if e["k"] == "run.sharepoint.preview"]
    assert len(vorschau) == 2 and vorschau[0]["v"] == {"site": "S", "name": "A", "n": 10, "mb": 3, "skipped": 2}


def test_preview_nennt_eine_bibliothek_die_nicht_antwortet(tmp_path, monkeypatch, capsys):
    drives = [{"id": "d1", "site": "S", "name": "A"},
              {"id": "d2", "site": "S", "name": "B"}]

    def wackelig(graph, out, wahl, still=False, zustand=None, quelle="onedrive"):
        if str(out).endswith("B"):
            raise RuntimeError("HTTP 503")
        return _bibliothek(3, 0)
    monkeypatch.setattr(sp.drive_mirror, "nur_pruefen", wackelig)

    class G:
        pass

    bericht = sp.nur_pruefen(G(), tmp_path, drives)
    assert bericht["stand"] == "teilweise" and bericht["da"] == 3
    assert bericht["fehler"] == [{"pfad": "S/B", "grund": "run.sharepoint.library_failed"}]
    assert any(e["k"] == "run.sharepoint.library_failed" for e in _events(capsys))


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
    sp.seiten_lauf(g, tmp_path, sites)          # one page now lies here
    g.seiten.append({"id": "p2", "name": "Neu.aspx", "title": "Neu",
                     "eTag": "e2"})
    capsys.readouterr()
    b = sp.seiten_pruefen(g, tmp_path, sites)
    assert (b["quelle"], b["einheit"]) == ("sharepoint_pages", "pages")
    assert (b["da"], b["offen"]) == (1, 1)
    assert [(z["pfad"], z["offen"]) for z in b["zeilen"]] == [("Team X", 1)]
    import completeness
    assert completeness.lesen(sp.state_db.StateDb(tmp_path), "sharepoint_pages")["da"] == 1
    e = progress.lies_ergebnis(
        [z for z in capsys.readouterr().out.splitlines()
         if progress.lies_ergebnis(z)][0])
    assert e["extra"] == {"present": 1, "open": 1, "kept": 0}


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


# ---------------------------------------------------------------------------
# SHAREPOINT_RULES: path rules over "<site>/<library>/Dateien/…"
# ---------------------------------------------------------------------------
def test_regeln_greifen_ueber_site_und_bibliothek(monkeypatch):
    monkeypatch.setenv("SHAREPOINT_RULES", "- Nordwind/Dokumente/Dateien/Archiv/**")
    basis = sp.auswahl()
    wahl = sp.drive_auswahl(basis, {"id": "d1", "site": "Nordwind", "name": "Dokumente"})
    assert wahl.takes("Dateien/Aktuell/plan.pdf", 1)
    assert not wahl.takes("Dateien/Archiv/alt.pdf", 1)
    assert not wahl.pfad_ok("Dateien/Archiv/alt.pdf")
    # The same folder name in another library is another path.
    andere = sp.drive_auswahl(basis, {"id": "d2", "site": "Nordwind", "name": "Assets"})
    assert andere.takes("Dateien/Archiv/alt.pdf", 1)
    # On top of the scope and the type filters, not instead of them.
    monkeypatch.setenv("SHAREPOINT_TYPES_EXCLUDE", "mp4")
    eng = sp.drive_auswahl(sp.auswahl(), {"id": "d1", "site": "Nordwind",
                                          "name": "Dokumente",
                                          "prefixes": {"Aktuell"}})
    assert eng.takes("Dateien/Aktuell/plan.pdf", 1)
    assert not eng.takes("Dateien/Aktuell/film.mp4", 1)
    assert not eng.takes("Dateien/Archiv/alt.pdf", 1)


def test_regeln_ohne_umgebung_kommen_aus_der_datei(monkeypatch):
    monkeypatch.delenv("SHAREPOINT_RULES", raising=False)
    monkeypatch.setattr(sp.settings, "value",
                        lambda key, default=None: "- Nordwind/**"
                        if key == "sharepoint_rules" else default)
    assert sp.aktuelle_regeln() == [(False, "Nordwind/**")]
    monkeypatch.setenv("SHAREPOINT_RULES", "")
    assert sp.aktuelle_regeln() == []


def test_regeln_gehoeren_zum_kennzeichen(monkeypatch):
    drive = {"id": "d1", "site": "Nordwind", "name": "Dokumente"}
    monkeypatch.setenv("SHAREPOINT_RULES", "")
    ohne = sp.drive_auswahl(sp.auswahl(), drive).kennzeichen()
    monkeypatch.setenv("SHAREPOINT_RULES", "- Nordwind/Dokumente/Dateien/Archiv/**")
    mit = sp.drive_auswahl(sp.auswahl(), drive).kennzeichen()
    assert ohne != mit
    assert sp.drive_auswahl(sp.auswahl(), drive).kennzeichen() == mit


def test_vorschau_und_liste_kennen_die_regeln(tmp_path, monkeypatch):
    """--check reflects the rules, and the report rows carry the very
    "<site>/<library>" the rules are written against."""
    import drive_mirror
    monkeypatch.setenv("SHAREPOINT_RULES", "- Nordwind/Dokumente/Dateien/Archiv/**")
    drive = {"id": "d1", "site": "Nordwind", "name": "Dokumente"}
    wahl = sp.drive_auswahl(sp.auswahl(), drive)
    eintraege = [
        {"id": "a", "name": "alt.pdf", "file": {}, "size": 5, "cTag": "c",
         "parentReference": {"path": "/drive/root:/Archiv"}},
        {"id": "b", "name": "plan.pdf", "file": {}, "size": 7, "cTag": "c",
         "parentReference": {"path": "/drive/root:/Aktuell"}}]
    b = drive_mirror.pruefe_vollstaendigkeit(eintraege, tmp_path, wahl)
    assert b["da"] + b["offen"] == 1 and b["ausgeschlossen"] == 1
    assert "ausgelassene_ordner" not in b
    assert sp.drive_praefix(drive) == "Nordwind/Dokumente"
    assert sp.drive_praefix({"site": "A: B?", "name": "Docs"}) == "A_ B_/Docs"
    assert sp.drive_ziel(tmp_path, drive) == tmp_path / "Nordwind" / "Dokumente"


# ---------------------------------------------------------------------------
# Pages: rows are upserted, the table is never rewritten
# ---------------------------------------------------------------------------
def test_seiten_lauf_schreibt_nur_die_geaenderte_zeile(tmp_path, monkeypatch):
    g = _SeitenGraph()
    g.seiten = [{"id": "p1", "name": "Home.aspx", "title": "Home", "eTag": "e1"},
                {"id": "p2", "name": "Team.aspx", "title": "Team", "eTag": "e1"}]
    sites = [{"id": "s1", "pfad": ["Team X"]}]
    assert sp.seiten_lauf(g, tmp_path, sites) == 2
    aufrufe = []
    urspruenglich = sp.state_db.StateDb.seiten_aktualisieren

    def merkend(self, geaendert, geloescht=()):
        aufrufe.append((set(geaendert), list(geloescht)))
        return urspruenglich(self, geaendert, geloescht)

    monkeypatch.setattr(sp.state_db.StateDb, "seiten_aktualisieren", merkend)
    monkeypatch.setattr(sp.state_db.StateDb, "seiten_schreiben",
                        lambda *a, **kw: pytest.fail("the table was rewritten"))
    g.seiten[1] = {**g.seiten[1], "eTag": "e2"}
    assert sp.seiten_lauf(g, tmp_path, sites) == 1
    assert aufrufe == [({"p2"}, [])]
    assert g.detailabrufe == 3, "the changed page is fetched once more, only it"
    bestand = sp.state_db.StateDb(tmp_path).seiten_lesen()
    assert set(bestand) == {"p1", "p2"} and bestand["p2"]["etag"] == "e2"
    # Gone at Microsoft: one delete, no rewrite.
    g.seiten = g.seiten[:1]
    sp.seiten_lauf(g, tmp_path, sites)
    assert aufrufe[-1] == (set(), ["p2"])
    assert set(sp.state_db.StateDb(tmp_path).seiten_lesen()) == {"p1"}


def test_seiten_lauf_nimmt_das_layout_aus_der_liste_wenn_es_da_ist(tmp_path):
    g = _SeitenGraph()
    g.seiten[0]["canvasLayout"] = g.layout
    sp.seiten_lauf(g, tmp_path, [{"id": "s1", "pfad": ["Team X"]}])
    assert g.detailabrufe == 0
    html = next(tmp_path.rglob("*.html")).read_text(encoding="utf-8")
    assert "Inhalt der Startseite" in html


# ---------------------------------------------------------------------------
# Folder cadences per library, the OneDrive way
# ---------------------------------------------------------------------------
class _TaktGraph:
    """A library drive for sp.lauf: delta entries per run, item lookups by
    id (None = 404), every call in order."""

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
        kennung = url.split("/items/")[1].split("?")[0]
        self.log.append(("get", kennung))
        meta = self.items.get(kennung)
        if meta is None:
            raise requests.HTTPError(response=_Antwort(404))
        return meta

    def lade(self, item_id, ziel, geaendert=None):
        self.log.append(("lade", item_id))
        self.geladen.append(item_id)
        return _lade_x(item_id, ziel, geaendert)


def _sp_datei(kennung, name, pfad, ctag="c1"):
    return {"id": kennung, "name": name, "file": {}, "size": 1, "cTag": ctag,
            "parentReference": {"path": pfad}}


NORDWIND = {"id": "d1", "site": "Nordwind", "name": "Dokumente",
            "urls": ["https://firma.sharepoint.com/sites/Nordwind"]}
ARCHIV_KEY = "sharepoint:Nordwind/Dokumente/Dateien/Archiv"
ARCHIV_STEMPEL = "last_sync:sharepoint:Dateien/Archiv"


def _beide(ctag="c1"):
    return [_sp_datei("alt", "alt.pdf", "/drive/root:/Archiv", ctag),
            _sp_datei("neu", "neu.pdf", "/drive/root:/Aktuell", ctag)]


def _wartend(ziel):
    return {k: json.loads(v) for k, v in
            sp.state_db.StateDb(ziel).saetze_lesen("wartend").items()}


def _ergebnis(capsys):
    zeilen = [z for z in capsys.readouterr().out.splitlines()
              if progress.lies_ergebnis(z)]
    return progress.lies_ergebnis(zeilen[-1])


def test_getakteter_ordner_in_faelliger_bibliothek_wartet(tmp_path, monkeypatch,
                                                           capsys):
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({ARCHIV_KEY: "monthly"}))
    d = dict(NORDWIND)
    assert sp.lauf(_TaktGraph(_beide()), tmp_path, [d])["new"] == 2
    ziel = sp.drive_ziel(tmp_path, d)
    db = sp.state_db.StateDb(ziel)
    assert db.kv_lesen("last_sync") and db.kv_lesen(ARCHIV_STEMPEL)
    capsys.readouterr()
    g = _TaktGraph(_beide("c2"))
    summe = sp.lauf(g, tmp_path, [d])
    assert g.geladen == ["neu"] and summe["new"] == 1
    assert _wartend(ziel) == {"alt": {"rel": "Dateien/Archiv/alt.pdf", "ctag": "c2",
                                      "size": 1, "einheit": "Dateien/Archiv"}}
    assert sp.state_db.DbZustand(ziel).delta_lesen() == "delta-1"
    out = capsys.readouterr().out
    events = [e for e in (progress.lies_event(z) for z in out.splitlines()) if e]
    paced = [e for e in events if e["k"] == "run.sharepoint.folders_paced"]
    assert len(paced) == 1 and paced[0]["v"] == {"name": "Nordwind / Dokumente", "n": 1}
    ergebnis = progress.lies_ergebnis(
        [z for z in out.splitlines() if progress.lies_ergebnis(z)][-1])
    assert ergebnis["extra"]["waiting"] == 1


def test_faelliger_ordner_leert_die_warteliste_vor_dem_delta(tmp_path, monkeypatch,
                                                              capsys):
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({ARCHIV_KEY: "monthly"}))
    d = dict(NORDWIND)
    sp.lauf(_TaktGraph(_beide()), tmp_path, [d])
    sp.lauf(_TaktGraph(_beide("c2")), tmp_path, [d])
    ziel = sp.drive_ziel(tmp_path, d)
    db = sp.state_db.StateDb(ziel)
    import time
    alt = db.kv_lesen(ARCHIV_STEMPEL)
    db.kv_schreiben(ARCHIV_STEMPEL, str(time.time() - 31 * 86400))
    capsys.readouterr()
    g = _TaktGraph([], items={"alt": _sp_datei("alt", "alt.pdf", "/drive/root:/Archiv", "c2")})
    sp.lauf(g, tmp_path, [d])
    assert g.log[:2] == [("get", "alt"), ("delta", "delta-1")], "waiting list first"
    assert g.geladen == ["alt"] and _wartend(ziel) == {}
    assert db.bestand_lesen()["alt"]["ctag"] == "c2"
    assert float(db.kv_lesen(ARCHIV_STEMPEL)) > float(alt)
    assert _ergebnis(capsys)["extra"]["waiting"] == 0


def test_nicht_faellige_bibliothek_mit_faelligem_ordner_wird_gelistet(
        tmp_path, monkeypatch, capsys):
    """The library is monthly, one folder always: listed all the same,
    only the folder's file comes, the library's own file waits."""
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({ARCHIV_KEY: "always"}))
    d = dict(NORDWIND, kadenz="monthly")
    sp.lauf(_TaktGraph(_beide()), tmp_path, [d])
    ziel = sp.drive_ziel(tmp_path, d)
    db = sp.state_db.StateDb(ziel)
    stempel = db.kv_lesen("last_sync")
    capsys.readouterr()
    g = _TaktGraph(_beide("c2"))
    sp.lauf(g, tmp_path, [d])
    assert g.geladen == ["alt"]
    assert _wartend(ziel)["neu"]["einheit"] == ""
    assert db.kv_lesen("last_sync") == stempel, "the library unit was not due"
    events = _events(capsys)
    assert not any(e["k"] in ("run.cadence.skip", "run.sharepoint.folders_paced")
                   for e in events)
    # Folder monthly as well, both stamped: skipped before any listing.
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({ARCHIV_KEY: "monthly"}))
    g = _TaktGraph(_beide("c3"))
    sp.lauf(g, tmp_path, [d])
    assert g.log == []
    assert any(e["k"] == "run.cadence.skip" and e["v"]["cadence"]["k"] == "cadence.monthly"
               for e in _events(capsys))


def test_ohne_ordnertakt_bleibt_alles_wie_es_war(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    d = dict(NORDWIND)
    g = _TaktGraph(_beide())
    sp.lauf(g, tmp_path, [d])
    ziel = sp.drive_ziel(tmp_path, d)
    # The downloads run side by side: which one finishes first is chance.
    assert sorted(g.geladen) == ["alt", "neu"] and _wartend(ziel) == {}
    assert sp.state_db.StateDb(ziel).kv_lesen("last_sync"), "a clean run stamps the library"
    assert "waiting" not in _ergebnis(capsys)["extra"]


def test_urls_landen_in_der_state_db_der_bibliothek(tmp_path, monkeypatch, capsys):
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/TeamX": {
        "id": "s1", "name": "Team X",
        "drives": [{"id": "d1", "name": "Dokumente", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/TeamX/Dokumente"}]}})
    urls = ["https://firma.sharepoint.com/sites/TeamX",
            "https://firma.sharepoint.com/sites/TeamX/Dokumente"]
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    drives, _ = sp.resolve_drives(g, urls)
    assert drives[0]["urls"] == urls
    # The --folders sync writes the key …
    monkeypatch.setattr(sp.drive_mirror, "nur_ordner",
                        lambda *a, **kw: {"neu": [], "verschwunden": [],
                                          "umbenannt": [], "ordner": []})
    sp.nur_ordner(_TaktGraph(), tmp_path, drives)
    db = sp.state_db.StateDb(sp.drive_ziel(tmp_path, drives[0]))
    assert json.loads(db.kv_lesen("urls")) == urls
    # … and so does the export run.
    db.kv_schreiben("urls", "[]")
    sp.lauf(_TaktGraph(), tmp_path, drives)
    assert json.loads(db.kv_lesen("urls")) == urls


def test_drive_einheiten_nehmen_nur_die_eigenen_ordner(tmp_path):
    db = sp.state_db.StateDb(tmp_path)
    e = sp.drive_einheiten(
        {ARCHIV_KEY: "monthly", "sharepoint:Nordwind/Assets/Dateien/Alt": "daily",
         "sharepoint": "weekly"},
        {"site": "Nordwind", "name": "Dokumente", "kadenz": "weekly"}, db)
    assert e.ordner == ["Dateien/Archiv"]
    assert e.einheit("Dateien/Archiv/tief/a.pdf") == "Dateien/Archiv"
    assert e.einheit("Dateien/Alt/a.pdf") == ""
    assert e.kadenz("") == "weekly" and e.kadenz("Dateien/Archiv") == "monthly"
    assert e.kv_key("Dateien/Archiv") == ARCHIV_STEMPEL and e.kv_key("") == "last_sync"
    # The library's cadence is what resolve_drives merged, not a category key.
    assert sp.drive_einheiten({"sharepoint": "daily"},
                              {"site": "Nordwind", "name": "Dokumente"}, db).kadenz("") == "always"


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


def test_seiten_pruefen_zaehlt_grabsteine_als_behalten(tmp_path, capsys):
    """A page gone at Microsoft is kept here – a number, not a gap."""
    g = _SeitenGraph()
    g.seiten = []
    sites = [{"id": "s1", "pfad": ["A"], "host": "h"},
             {"id": "s2", "pfad": ["A", "Sub"], "host": "h"}]
    sp.state_db.StateDb(tmp_path).verschwunden_ergaenzen(
        ["A/Sub/page.html"], "2026-08-01T00:00:00+00:00")
    b = sp.seiten_pruefen(g, tmp_path, sites)
    assert b["behalten"] == 1 and b["offen"] == 0 and b["zeilen"] == []


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
                yield None, "delta-2"          # nothing changed
                return
            yield {"id": "in1", "name": "plan.pdf", "file": {}, "size": 1,
                   "cTag": "c1",
                   "parentReference": {"path": "/drive/root:/Folder/Nordwind"}}, None
            yield {"id": "out1", "name": "fremd.pdf", "file": {}, "size": 1,
                   "cTag": "c1",
                   "parentReference": {"path": "/drive/root:/Anderes"}}, None
            yield None, "delta-1"

        def lade(self, item_id, ziel, geaendert=None):
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_bytes(b"x")
            return 1

    g = FakeGraph()
    wahl = sp.drive_auswahl(Selection(), {"prefixes": {"Folder/Nordwind"}})
    zustand = sp.state_db.DbZustand(tmp_path)
    zahlen = drive_mirror.lauf(g, tmp_path, wahl, 1, still=True,
                               zustand=zustand)
    assert zahlen == {"new": 1, "excluded": 0, "errors": 0,
                      "moved": 0, "gone": 0}
    assert (tmp_path / "Dateien/Folder/Nordwind/plan.pdf").is_file()
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
    assert db.delta_lesen() is None               # pointer does not advance
    assert db.walk_status()["fertig"] == "delta-1"

    g.kaputt = False
    capsys.readouterr()
    zahlen = drive_mirror.lauf(g, tmp_path, Selection(), 1, still=True,
                               zustand=zustand)
    assert g.walks == 1                           # no second enumeration
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
    """Cadence lives on the source URL; the site's and the library's own
    both reach one drive – the closer one, the library's, wins."""
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/TeamX": {
        "id": "s1", "name": "Team X",
        "drives": [{"id": "d1", "name": "Dokumente", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/TeamX/Dokumente"}]}})
    urls = ["https://firma.sharepoint.com/sites/TeamX",
            "https://firma.sharepoint.com/sites/TeamX/Dokumente"]
    monkeypatch.setenv("SYNC_CADENCE", json.dumps(
        {f"sharepoint-url:{urls[0]}": "daily",
         f"sharepoint-url:{urls[1]}": "weekly"}))
    drives, fehl = sp.resolve_drives(g, urls)
    assert fehl == 0 and drives[0]["kadenz"] == "weekly"


def test_lauf_ueberspringt_bibliothek_unter_ihrer_kadenz(tmp_path, monkeypatch,
                                                         capsys):
    drives = [{"id": "d1", "site": "S", "name": "A", "kadenz": "weekly"},
              {"id": "d2", "site": "S", "name": "B"}]
    gelaufen = []

    def fake_lauf(graph, out, wahl, arbeiter, still=False, zustand=None,
                  name=None, einheiten=None):
        gelaufen.append(str(out))
        return {"new": 1, "excluded": 0, "errors": 0, "moved": 0, "gone": 0}

    monkeypatch.setattr(sp.drive_mirror, "lauf", fake_lauf)
    # d1 ran just now – not due under the weekly cadence.
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

    def fake_lauf(graph, out, wahl, arbeiter, still=False, zustand=None,
                  name=None, einheiten=None):
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
    assert g.detailabrufe == 1                      # nothing fetched again


def test_full_sync_rendert_jede_seite_erneut(tmp_path, monkeypatch, capsys):
    """"Force full sync": every page's eTag is forgotten, so each one is
    fetched and rendered again, and the inventory carries the fresh tag."""
    g = _SeitenGraph()
    sites = [{"id": "s1", "pfad": ["Team X"]}]
    assert sp.seiten_lauf(g, tmp_path, sites) == 1
    monkeypatch.setenv("FULL_SYNC", "1")
    capsys.readouterr()
    assert sp.seiten_lauf(g, tmp_path, sites) == 1 and g.detailabrufe == 2
    assert any(e["k"] == "run.full_sync" for e in _events(capsys))
    assert sp.state_db.StateDb(tmp_path).seiten_lesen()["p1"]["etag"] == "e1"


def test_full_sync_sagt_es_auch_fuer_bibliotheken(tmp_path, monkeypatch, capsys):
    drives = [{"id": "d1", "site": "S", "name": "A", "kadenz": "always"}]
    monkeypatch.setattr(sp.drive_mirror, "lauf", lambda *a, **kw: {
        "new": 0, "excluded": 0, "errors": 0, "moved": 0, "gone": 0})
    monkeypatch.setenv("FULL_SYNC", "1")

    class G:
        pass

    sp.lauf(G(), tmp_path, drives)
    assert [e["k"] for e in _events(capsys)].count("run.full_sync") == 1


def test_nachholen_findet_die_bibliothek_ueber_ihre_gespeicherten_urls(tmp_path, monkeypatch, capsys):
    """"Fetch again" across the libraries: the files are grouped by the
    library folder they lie in, the library is resolved again through the
    URLs stored with it – no configured list – and the mirror's targeted
    fetch runs with the drive set; a library without stored URLs leaves its
    files open."""
    import state_db
    lib = tmp_path / "Team X" / "Dokumente"
    state_db.StateDb(lib).kv_schreiben("urls", json.dumps(["https://firma.sharepoint.com/sites/TeamX"]))
    ohne = tmp_path / "Team X" / "Assets"
    state_db.StateDb(ohne).kv_schreiben("urls", "[]")
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/TeamX": {
        "id": "s1", "name": "Team X",
        "drives": [{"id": "d1", "name": "Dokumente", "driveType": "documentLibrary"}]}})
    gesehen = []

    def fake_nachholen(graph, wurzel, rels, arbeiter, zustand=None, still=False):
        assert still and graph.drive_base.endswith("/drives/d1")
        gesehen.append((wurzel, list(rels)))
        return {"new": len(rels), "errors": 0, "gone": 0, "unknown": 0}

    monkeypatch.setattr(sp.drive_mirror, "nachholen", fake_nachholen)
    sp.nachholen(g, tmp_path, ["Team X/Dokumente/Dateien/a.pdf", "Team X/Dokumente/Dateien/b.pdf",
                               "Team X/Assets/Dateien/c.pdf", "Nirgends/x.pdf"])
    assert gesehen == [(lib, ["Dateien/a.pdf", "Dateien/b.pdf"])]
    zeilen = capsys.readouterr().out.splitlines()
    events = [e for e in (progress.lies_event(z) for z in zeilen) if e]
    (offen,) = [e for e in events if e["k"] == "run.nachholen.nolibrary"]
    assert offen["v"]["n"] == 1
    (ergebnis,) = [progress.lies_ergebnis(z) for z in zeilen if progress.lies_ergebnis(z)]
    assert ergebnis["new"] == 2 and ergebnis["extra"] == {"gone": 0, "unknown": 2}


def test_bilanz_fuehrt_die_offenen_dateien_unter_ihrer_bibliothek(tmp_path, monkeypatch):
    """The merged report names every open file by id under the library's
    path, so "Fetch now" can hand them straight to the libraries."""
    import completeness
    drives = [{"id": "d1", "site": "S", "name": "A", "kadenz": "always"},
              {"id": "d2", "site": "S", "name": "B", "kadenz": "always"}]

    def fake_pruefen(graph, ziel, auswahl, still=False, zustand=None, quelle="onedrive"):
        name = ziel.name
        return completeness.bilanz(
            quelle, "files", da=1, offen=1,
            zeilen=[completeness.zeile("Dateien", 1, 1)],
            extra={"bytes": 0, "bytes_ausgeschlossen": 0, "typen": [],
                   "offene": [{"id": f"i-{name}", "rel": "Dateien/x.pdf"}],
                   "offene_gekappt": name == "B"})
    monkeypatch.setattr(sp.drive_mirror, "nur_pruefen", fake_pruefen)

    class G:
        pass

    b = sp.nur_pruefen(G(), tmp_path, drives)
    assert b["offene"] == [{"id": "i-A", "rel": "S/A/Dateien/x.pdf"},
                           {"id": "i-B", "rel": "S/B/Dateien/x.pdf"}]
    assert b["offene_gekappt"]


# --------------------------------------------------------------------------
# Verdicts on pages and images: refused or gone, recorded – a passing
# failure leaves the page due
# --------------------------------------------------------------------------
class _Text:
    def __init__(self, status):
        self.status_code, self.text = status, ""


def _http(status):
    return requests.HTTPError(f"{status} Client Error", response=_Text(status))


def _image_page():
    g = _SeitenGraph()
    g.layout = {"horizontalSections": [{"columns": [{"webparts": [
        {"@odata.type": "#microsoft.graph.textWebPart",
         "innerHtml": '<p><img src="/sites/T/SiteAssets/logo.png"></p>'}]}]}]}
    return g


SITES = [{"id": "s1", "pfad": ["Team X"], "host": "firma.sharepoint.com"}]
LOGO = "https://firma.sharepoint.com/sites/T/SiteAssets/logo.png"


def test_a_refused_page_is_recorded_for_its_version(tmp_path, capsys):
    """A 403 on the page's layout: a mark for this eTag, no error, and
    the next run does not ask; the check counts it as refused, not
    open; a new eTag is asked once more, and when it comes the mark goes."""
    g = _SeitenGraph()
    g.get = lambda url: (_ for _ in ()).throw(_http(403))
    assert sp.seiten_lauf(g, tmp_path, SITES) == 0
    db = sp.state_db.StateDb(tmp_path)
    m = db.permanent_lesen()["page:p1"]
    assert m["kind"] == "refused" and m["version"] == "e1" and m["unit"] == "Team X"
    assert m["rel"] == "Team X/Home.html"
    events = [progress.lies_event(z) for z in capsys.readouterr().out.splitlines()]
    keys = [e["k"] for e in events if e]
    assert keys.count("run.item.refused") == 1 and "run.pages.page_failed" not in keys
    g2 = _SeitenGraph()
    assert sp.seiten_lauf(g2, tmp_path, SITES) == 0 and g2.detailabrufe == 0, "the marked version was asked"
    b = sp.seiten_pruefen(_SeitenGraph(), tmp_path, SITES)
    assert (b["da"], b["offen"], b["verweigert"], b["weg"]) == (0, 0, 1, 0)
    g3 = _SeitenGraph()
    g3.seiten[0]["eTag"] = "e2"
    assert sp.seiten_lauf(g3, tmp_path, SITES) == 1 and g3.detailabrufe == 1
    assert db.permanent_lesen() == {} and db.seiten_lesen()["p1"]["etag"] == "e2"


def test_an_image_verdict_is_quiet_and_a_passing_failure_keeps_the_page_due(tmp_path, capsys):
    """A 404 on an image: the link stays, a quiet mark, no request next
    time – the page is recorded. A 502: the page is written but not
    recorded, so the next run renders it once more."""
    g = _image_page()
    g.get_bytes = lambda url, label="": (_ for _ in ()).throw(_http(404))
    assert sp.seiten_lauf(g, tmp_path, SITES) == 1
    db = sp.state_db.StateDb(tmp_path)
    m = db.permanent_lesen()[f"image:{LOGO}"]
    assert m["kind"] == "gone" and m["quiet"] is True and m["name"] == "logo.png"
    assert db.seiten_lesen()["p1"]["etag"] == "e1", "the page with a verdict is recorded"
    events = [progress.lies_event(z) for z in capsys.readouterr().out.splitlines()]
    keys = [e["k"] for e in events if e]
    assert keys.count("run.item.gone") == 1 and "run.pages.images_failed" not in keys
    assert 'src="/sites/T/SiteAssets/logo.png"' in next(tmp_path.rglob("*.html")).read_text(encoding="utf-8")
    g2 = _image_page()
    g2.seiten[0]["eTag"] = "e2"
    asked = []
    g2.get_bytes = lambda url, label="": asked.append(url) or (b"BILD", "image/png")
    assert sp.seiten_lauf(g2, tmp_path, SITES) == 1 and asked == [], "the marked image was asked"
    # a passing failure: written, not recorded, rendered again next run
    g3 = _image_page()
    g3.seiten[0]["eTag"] = "e3"
    g3.get_bytes = lambda url, label="": (_ for _ in ()).throw(RuntimeError("HTTP 502"))
    db.permanent_leeren()
    assert sp.seiten_lauf(g3, tmp_path, SITES) == 1
    assert db.seiten_lesen()["p1"]["etag"] == "e2", "a page with an image still owed was recorded"
    keys = [e["k"] for e in (progress.lies_event(z) for z in capsys.readouterr().out.splitlines()) if e]
    assert "run.pages.images_failed" in keys
    g4 = _image_page()
    g4.seiten[0]["eTag"] = "e3"
    g4.get_bytes = lambda url, label="": (b"BILD", "image/png")
    assert sp.seiten_lauf(g4, tmp_path, SITES) == 1 and g4.detailabrufe >= 1
    assert db.seiten_lesen()["p1"]["etag"] == "e3"
    assert "data:image/png;base64," in next(tmp_path.rglob("*.html")).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Every address its own cadence
# --------------------------------------------------------------------------
def _two_libraries():
    return _FakeGraph(sites={"firma.sharepoint.com:/sites/TeamX": {
        "id": "s1", "name": "TeamX",
        "drives": [{"id": "d1", "name": "Documents", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/TeamX/Documents"},
                   {"id": "d2", "name": "Templates", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/TeamX/Templates"}]}})


BASIS = "https://firma.sharepoint.com/sites/TeamX/"


def test_every_address_keeps_its_own_cadence(monkeypatch, capsys):
    """A folder address behind the whole library's address keeps its
    cadence; two folder addresses keep each their own."""
    urls = [BASIS + "Documents", BASIS + "Documents/Folder/A",
            BASIS + "Templates/Folder/B", BASIS + "Templates/Folder/C"]
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({
        "sharepoint-url:" + urls[0]: "weekly", "sharepoint-url:" + urls[1]: "daily",
        "sharepoint-url:" + urls[2]: "weekly", "sharepoint-url:" + urls[3]: "daily"}))
    drives, fehl = sp.resolve_drives(_two_libraries(), urls)
    docs, tpl = drives
    assert fehl == 0
    assert docs["kadenz"] == "weekly" and docs["einheiten"] == {"Folder/A": "daily"}
    assert docs["prefixes"] is None
    # Only folder addresses: each its own, the library waits for the slowest.
    assert tpl["einheiten"] == {"Folder/B": "weekly", "Folder/C": "daily"}
    assert tpl["kadenz"] == "weekly"


def test_a_site_address_paces_every_library_unless_set_closer(monkeypatch, capsys):
    """A site address gives each library its cadence – an address on the
    whole library wins over it (the page writes one when a library under
    a site gets a cadence of its own)."""
    urls = [BASIS.rstrip("/"), BASIS + "Documents/Folder/A", BASIS + "Templates"]
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({
        "sharepoint-url:" + urls[0]: "weekly", "sharepoint-url:" + urls[1]: "daily",
        "sharepoint-url:" + urls[2]: "monthly"}))
    drives, fehl = sp.resolve_drives(_two_libraries(), urls)
    docs, tpl = drives
    assert fehl == 0 and docs["prefixes"] is None and tpl["prefixes"] is None
    assert docs["kadenz"] == "weekly" and docs["einheiten"] == {"Folder/A": "daily"}
    assert tpl["kadenz"] == "monthly" and tpl["einheiten"] == {}
    assert "site_ganz" not in docs and "ganz" not in docs


def test_a_folder_address_can_take_the_library_s_cadence(monkeypatch, capsys):
    urls = [BASIS + "Templates", BASIS + "Templates/Folder/B", BASIS + "Templates/Folder/C"]
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({
        "sharepoint-url:" + urls[0]: "monthly", "sharepoint-url:" + urls[1]: "inherit",
        "sharepoint-url:" + urls[2]: "daily"}))
    (tpl,), _ = sp.resolve_drives(_two_libraries(), urls)
    assert tpl["lib_id"] == "firma.sharepoint.com/sites/TeamX/Templates"
    assert tpl["kadenz"] == "monthly" and tpl["prefixes"] is None
    assert tpl["einheiten"] == {"Folder/B": "monthly", "Folder/C": "daily"}
    assert tpl["unit_of"] == {urls[0]: None, urls[1]: "Folder/B", urls[2]: "Folder/C"}


def test_inherit_under_a_placeholder_takes_the_slowest_folder(monkeypatch, capsys):
    """No address on the library, no default: the library waits for its
    slowest folder, and a folder that said "inherit" waits with it – what
    the page shows for it (spLibCadence), not "always"."""
    urls = [BASIS + "Templates/Folder/B", BASIS + "Templates/Folder/C"]
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({
        "sharepoint-url:" + urls[0]: "inherit", "sharepoint-url:" + urls[1]: "monthly"}))
    (tpl,), _ = sp.resolve_drives(_two_libraries(), urls)
    assert tpl["kadenz"] == "monthly"
    assert tpl["einheiten"] == {"Folder/B": "monthly", "Folder/C": "monthly"}


def test_an_address_given_twice_counts_once(monkeypatch, capsys):
    """The place counts once, whatever its spelling, and the first line's
    cadence is the one that holds."""
    urls = [BASIS + "Templates/Folder/B", BASIS + "Templates/Folder/B",
            "https://firma.sharepoint.com/:f:/r/sites/TeamX/Templates/Folder/B?web=1"]
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({
        "sharepoint-url:" + urls[0]: "daily", "sharepoint-url:" + urls[2]: "monthly"}))
    (tpl,), _ = sp.resolve_drives(_two_libraries(), urls)
    assert tpl["urls"] == [urls[0]] and tpl["prefixes"] == {"Folder/B"}
    assert tpl["einheiten"] == {"Folder/B": "daily"}


def test_one_site_in_two_spellings_is_fetched_once(monkeypatch, capsys):
    """"TeamX" and "teamx" are one site: asked for once, and named by the
    first spelling in every lib_id."""
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    g = _two_libraries()
    asked, original = [], g.get
    g.get = lambda url: (asked.append(url), original(url))[1]
    drives, fehl = sp.resolve_drives(g, [BASIS + "Documents", "https://firma.sharepoint.com/sites/teamx/Templates"])
    assert fehl == 0 and len(asked) == 1
    assert [d["lib_id"] for d in drives] == ["firma.sharepoint.com/sites/TeamX/Documents",
                                              "firma.sharepoint.com/sites/TeamX/Templates"]


def test_the_host_is_read_as_the_page_reads_it():
    """Lower-cased, without userinfo, the scheme's own port left out – and
    a port that is none is no address (URL.host in the browser)."""
    assert sp.url_teile("https://User@FIRMA.sharepoint.com:443/sites/TeamX/Docs") == (
        "firma.sharepoint.com:/sites/TeamX", ["Docs"])
    assert sp.url_teile("https://firma.sharepoint.com:8443/sites/TeamX") == (
        "firma.sharepoint.com:8443:/sites/TeamX", [])
    assert sp.url_teile("https://firma.sharepoint.com:99999/sites/TeamX") is None
    # The folder parameter is decoded once, as the page decodes it: a
    # folder whose name holds "%20" keeps it.
    assert sp.url_teile("https://firma.sharepoint.com/sites/TeamX/Documents/Forms/AllItems.aspx"
                        "?id=%2Fsites%2FTeamX%2FDocuments%2FQ4%2520Report") == (
        "firma.sharepoint.com:/sites/TeamX", ["Documents", "Q4%20Report"])


def test_site_chrome_names_the_site():
    """What the browser shows for a site – its home page, a document
    viewer, a list – is the site, not a library of that name."""
    for url in ("https://firma.sharepoint.com/sites/TeamX/SitePages/Home.aspx",
                "https://firma.sharepoint.com/sites/TeamX/_layouts/15/Doc.aspx?sourcedoc=%7Bx%7D",
                "https://firma.sharepoint.com/sites/TeamX/Lists/Tasks/AllItems.aspx"):
        assert sp.url_teile(url) == ("firma.sharepoint.com:/sites/TeamX", []), url
    assert sp.url_teile("https://firma.sharepoint.com/sites/TeamX/Documents/Report.aspx") == (
        "firma.sharepoint.com:/sites/TeamX", ["Documents"])
    assert sp.url_teile("https://firma.sharepoint.com/_layouts/15/sharepoint.aspx") == (
        "firma.sharepoint.com", [])
    assert sp.url_teile("https://[x/sites/a") is None
    # A page right below /sites/ names no site at all – no address, no crash.
    assert sp.url_teile("https://firma.sharepoint.com/sites/Home.aspx") is None


def test_site_chrome_counts_only_directly_below_the_site():
    """SitePages, Lists, _layouts … name the site where the browser puts
    them – right after it. Deeper down they are folders (a library may
    well hold a folder "Lists"), SiteAssets is a library of its own, and
    a folder named Forms is a folder unless a view follows it."""
    for url, expected in (
            ("https://firma.sharepoint.com/sites/TeamX/Documents/Lists/Old", ["Documents", "Lists", "Old"]),
            ("https://firma.sharepoint.com/sites/TeamX/Documents/SitePages/x.aspx", ["Documents", "SitePages"]),
            ("https://firma.sharepoint.com/sites/TeamX/SiteAssets/Logos", ["SiteAssets", "Logos"]),
            ("https://firma.sharepoint.com/sites/TeamX/Documents/Forms/Travel", ["Documents", "Forms", "Travel"]),
            ("https://firma.sharepoint.com/sites/TeamX/Documents/Forms/AllItems.aspx", ["Documents"]),
            ("https://firma.sharepoint.com/sites/TeamX/Documents/Forms/AllItems.aspx?id=%2Fsites%2FTeamX%2FDocuments%2FGeneral",
             ["Documents", "General"]),
            ("https://firma.sharepoint.com/Shared%20Documents/Forms/AllItems.aspx", ["Shared Documents"]),
            # A selected file: id= is the file, parent= its folder – the address names the folder.
            ("https://firma.sharepoint.com/sites/TeamX/Documents/Forms/AllItems.aspx"
             "?id=%2Fsites%2FTeamX%2FDocuments%2FGeneral%2Freport.docx&parent=%2Fsites%2FTeamX%2FDocuments%2FGeneral",
             ["Documents", "General"])):
        assert sp.url_teile(url)[1] == expected, url


def test_a_library_is_found_in_any_spelling(monkeypatch, capsys):
    """Typed as documents/Folder/A: the library Documents (its segment from
    the webUrl), the folder as typed – not a path that matches nothing."""
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    (docs,), fehl = sp.resolve_drives(_two_libraries(), [BASIS + "documents/Folder/A"])
    assert fehl == 0 and docs["name"] == "Documents" and docs["prefixes"] == {"Folder/A"}
    assert docs["lib_id"] == "firma.sharepoint.com/sites/TeamX/Documents"


def _with_subsite():
    g = _two_libraries()
    g.sites["firma.sharepoint.com:/sites/TeamX/Sub"] = {
        "id": "s2", "name": "Sub",
        "drives": [{"id": "d3", "name": "Docs", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/TeamX/Sub/Docs"}]}
    return g


def test_a_path_that_is_no_library_may_lead_through_a_subsite(monkeypatch, capsys):
    """…/sites/TeamX/Sub/Docs/Folder: Sub is no library of TeamX but a site
    below it – the run follows the path into it, asking once per name; a
    name that is neither library nor subsite is still skipped, and the
    whole site is never mirrored in its place."""
    urls = [BASIS + "Sub/Docs/Folder", BASIS + "Sub/Docs", BASIS + "Typo/Folder", BASIS + "Typo/Other"]
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({
        "sharepoint-url:" + urls[0]: "daily", "sharepoint-url:" + urls[1]: "weekly"}))
    g = _with_subsite()
    asked, original = [], g.get
    g.get = lambda url: (asked.append(url), original(url))[1]
    drives, fehl = sp.resolve_drives(g, urls)
    (docs,) = drives
    assert fehl == 2
    assert docs["site"] == "Sub" and docs["name"] == "Docs" and docs["prefixes"] is None
    assert docs["lib_id"] == "firma.sharepoint.com/sites/TeamX/Sub/Docs"
    assert docs["kadenz"] == "weekly" and docs["einheiten"] == {"Folder": "daily"}
    assert docs["urls"] == urls[:2]
    assert [u.rsplit("/", 1)[-1] for u in asked] == ["TeamX", "Sub", "Typo"]
    events = _events(capsys)
    assert [e["v"]["path"] for e in events if e["k"] == "run.sharepoint.path_unmatched"] == ["Typo/Folder", "Typo/Other"]
    assert [e["v"]["site"] for e in events if e["k"] == "run.sharepoint.libraries_from"] == ["Sub"]
    # The subsite itself as a plain address: every library of it.
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    (docs,), fehl = sp.resolve_drives(_with_subsite(), [BASIS + "Sub"])
    assert fehl == 0 and docs["site"] == "Sub" and docs["prefixes"] is None


def test_a_subsite_probe_that_fails_reports_its_status(monkeypatch, capsys):
    """Only a 404 means "no subsite of that name". A refusal is said as
    one, an outage with its status – and neither is remembered as "no
    site" for the rest of the run."""
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    g = _with_subsite()
    g.kaputt = {"TeamX/Secret": _Antwort(403), "TeamX/Down": _Antwort(500)}
    asked, original = [], g.get
    g.get = lambda url: (asked.append(url), original(url))[1]
    urls = [BASIS + "Secret/Docs", BASIS + "Secret/Other", BASIS + "Down/Docs", BASIS + "Sub/Docs"]
    drives, fehl = sp.resolve_drives(g, urls)
    assert [d["name"] for d in drives] == ["Docs"] and fehl == 3
    events = _events(capsys)
    assert [e["v"]["url"] for e in events if e["k"] == "run.sharepoint.denied"] == urls[:2]
    (down,) = [e for e in events if e["k"] == "run.sharepoint.site_failed"]
    assert down["v"]["url"] == urls[2] and down["v"]["error"] == "HTTP 500"
    assert not [e for e in events if e["k"] == "run.sharepoint.path_unmatched"]
    assert [u.rsplit("/", 1)[-1] for u in asked] == ["TeamX", "Secret", "Secret", "Down", "Sub"]


def test_inherit_on_a_whole_library_address_means_no_cadence_of_its_own(monkeypatch, capsys):
    """A library in a subsite looks like a folder address until a run
    names the subsite, so "inherit" can be stored on it: the library then
    takes the site's address, else its slowest folder – the page reads it
    the same way (spLibCadence)."""
    url = BASIS + "Sub/Docs"
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"sharepoint-url:" + url: "inherit"}))
    (docs,), _ = sp.resolve_drives(_with_subsite(), [url])
    assert docs["prefixes"] is None and docs["kadenz"] == "always"
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({
        "sharepoint-url:" + url: "inherit", "sharepoint-url:" + BASIS + "Sub": "weekly"}))
    (docs,), _ = sp.resolve_drives(_with_subsite(), [url, BASIS + "Sub"])
    assert docs["kadenz"] == "weekly"


def test_a_subsite_s_library_view_and_pages_read_as_the_top_site_s_do(monkeypatch, capsys):
    """The browser's address at the root of a subsite library ends in
    Forms/AllItems.aspx like any other – view chrome, wherever the library
    stands; the subsite's own pages or machinery name the subsite, as the
    top site's name the site."""
    assert sp.url_teile(BASIS + "Sub/Docs/Forms/AllItems.aspx")[1] == ["Sub", "Docs"]
    assert sp.url_teile(BASIS + "Sub/SitePages/Home.aspx")[1] == ["Sub", "SitePages"]
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    (docs,), fehl = sp.resolve_drives(_with_subsite(), [BASIS + "Sub/Docs/Forms/AllItems.aspx"])
    assert fehl == 0 and docs["name"] == "Docs" and docs["prefixes"] is None
    for url in (BASIS + "Sub/SitePages/Home.aspx", BASIS + "Sub/_layouts/15/viewlsts.aspx"):
        (docs,), fehl = sp.resolve_drives(_with_subsite(), [url])
        assert fehl == 0 and docs["site"] == "Sub" and docs["prefixes"] is None, url


def test_a_library_typed_by_its_display_name_is_found(monkeypatch, capsys):
    """"Documents" is what SharePoint shows for the URL segment "Shared
    Documents": typed that way, the line reaches the library – not a
    subsite probe that ends in an error every run."""
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/TeamX": {
        "id": "s1", "name": "TeamX",
        "drives": [{"id": "d1", "name": "Documents", "driveType": "documentLibrary",
                    "webUrl": "https://firma.sharepoint.com/sites/TeamX/Shared%20Documents"}]}})
    asked, original = [], g.get
    g.get = lambda url: (asked.append(url), original(url))[1]
    (docs,), fehl = sp.resolve_drives(g, [BASIS + "Documents/Reports"])
    assert fehl == 0 and docs["prefixes"] == {"Reports"}
    assert docs["lib_id"] == "firma.sharepoint.com/sites/TeamX/Shared Documents"
    assert len(asked) == 1, "the display name was probed as a subsite"


def test_a_short_sharing_link_is_no_address():
    """"/:f:/s/<site>/<token>" carries a token only Graph could resolve –
    refused on both sides, not read as a library named after the site."""
    assert sp.url_teile("https://firma.sharepoint.com/:f:/s/TeamX/EabcXYZ?e=abc") is None
    assert sp.url_teile("https://firma.sharepoint.com/:f:/r/sites/TeamX/Documents/A?web=1") == (
        "firma.sharepoint.com:/sites/TeamX", ["Documents", "A"])


def test_a_library_no_line_reaches_any_more_is_said(tmp_path, monkeypatch, capsys):
    """A line that stopped reaching its library – a typo, a subsite address
    that used to bring the whole site along – leaves the library in the
    archive untouched; the run says so, once, after a clean resolution."""
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    files = [_sp_datei("b", "b.pdf", "/drive/root:/Folder/B")]
    d = {"id": "d2", "site": "TeamX", "name": "Templates", "kadenz": "always", "prefixes": None,
         "einheiten": {}, "urls": [BASIS + "Templates"], "unit_of": {BASIS + "Templates": None}}
    other = {"id": "d1", "site": "TeamX", "name": "Documents", "kadenz": "always", "prefixes": None,
             "einheiten": {}, "urls": [BASIS + "Documents"], "unit_of": {BASIS + "Documents": None}}
    sp.lauf(_TaktGraph(files), tmp_path, [dict(d), dict(other)])
    capsys.readouterr()
    sp.lauf(_TaktGraph(files), tmp_path, [dict(d)])
    assert [e["v"]["name"] for e in _events(capsys) if e["k"] == "run.sharepoint.unnamed"] == ["TeamX/Documents"]
    # Not after a line that failed to resolve: that library may be its own.
    sp.lauf(_TaktGraph(files), tmp_path, [dict(d)], fehl=1)
    assert not [e for e in _events(capsys) if e["k"] == "run.sharepoint.unnamed"]


def test_sync_now_on_a_folder_address_forces_that_unit_alone(tmp_path, monkeypatch, capsys):
    """The button on a folder address: every line still rides along, so the
    library keeps its scope and its delta pointer (nothing is re-read in
    full, a deletion keeps its tombstone), its own stamp does not move –
    the rest of it comes on its cadence – and no other library runs."""
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    files = [_sp_datei("b", "b.pdf", "/drive/root:/Folder/B"), _sp_datei("o", "o.pdf", "/drive/root:/Other")]
    lines = [BASIS + "Templates", BASIS + "Templates/Folder/B"]
    d = {"id": "d2", "site": "TeamX", "name": "Templates", "kadenz": "weekly", "prefixes": None,
         "einheiten": {"Folder/B": "daily"}, "urls": lines, "unit_of": {lines[0]: None, lines[1]: "Folder/B"}}
    other = {"id": "d1", "site": "TeamX", "name": "Documents", "kadenz": "always", "prefixes": None,
             "einheiten": {}, "urls": [BASIS + "Documents"], "unit_of": {BASIS + "Documents": None}}
    sp.lauf(_TaktGraph(files), tmp_path, [dict(d)])
    db = sp.state_db.StateDb(sp.drive_ziel(tmp_path, d))
    stamp = db.kv_lesen("last_sync")
    capsys.readouterr()
    monkeypatch.setenv("SHAREPOINT_UNIT", lines[1])
    changed = [_sp_datei("b", "b.pdf", "/drive/root:/Folder/B", "c2"),
               _sp_datei("o", "o.pdf", "/drive/root:/Other", "c2")]
    g = _TaktGraph(changed)
    sp.lauf(g, tmp_path, [dict(d), dict(other)])
    events = _events(capsys)
    assert g.geladen == ["b"], "the unit alone is due"
    assert not [e for e in events if e["k"] in ("run.rules_changed", "run.drive.full")]
    assert g.log[0][0] == "delta" and g.log[0][1] is not None, "the delta pointer was dropped"
    assert db.kv_lesen("last_sync") == stamp, "the library's own stamp moved"
    assert json.loads(db.kv_lesen("urls")) == lines
    assert not (sp.drive_ziel(tmp_path, other) / sp.state_db.DB_NAME).exists(), "another library ran"
    # The unit's own stamp moved: on its cadence it is not due again.
    g = _TaktGraph(changed)
    sp.lauf(g, tmp_path, [dict(d)])
    assert g.geladen == []


def test_a_library_without_web_url_is_named_by_its_name(monkeypatch, capsys):
    """Graph sends no webUrl: the library is still named, not the bare
    site – the page's tree and the default cadence hang on that name."""
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    g = _FakeGraph(sites={"firma.sharepoint.com:/sites/TeamX": {
        "id": "s1", "name": "TeamX",
        "drives": [{"id": "d1", "name": "Documents", "driveType": "documentLibrary"}]}})
    (docs,), fehl = sp.resolve_drives(g, [BASIS.rstrip("/")])
    assert fehl == 0 and docs["lib_id"] == "firma.sharepoint.com/sites/TeamX/Documents"


def test_the_synthetic_archive_names_its_libraries_as_a_run_does(tmp_path, monkeypatch):
    """testdata writes the kv lib_id a run writes – the page maps the
    library on disk to its place in the tree by it, so another spelling
    there would leave the tree's folds empty."""
    from testdata import people, sources
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    url = sources.sharepoint_library_url("Projects")
    sources._write_library(tmp_path, "Projects", [], [url])
    db = sp.state_db.StateDb(tmp_path / sources.SHAREPOINT_SITE_DIR / "Projects")
    g = _FakeGraph(sites={sp.site_address(people.SHAREPOINT_SITE): {
        "id": "s1", "name": "Nordwind",
        "drives": [{"id": "d1", "name": "Projects", "driveType": "documentLibrary", "webUrl": url}]}})
    (lib,), fehl = sp.resolve_drives(g, [url])
    assert fehl == 0
    assert db.kv_lesen("lib_id") == lib["lib_id"] == "firma.sharepoint.com/sites/nordwind/Projects"
    assert json.loads(db.kv_lesen("urls")) == lib["urls"] == [url]


def test_a_line_that_matches_no_library_is_skipped(monkeypatch, capsys):
    """It names nothing – the tree shows it as a folder address, and
    mirroring the whole site instead would be the accident the tree rules
    out. Skipped, counted, said."""
    urls = [BASIS + "Documents", BASIS + "Typo/Folder"]
    monkeypatch.setenv("SYNC_CADENCE", json.dumps({"sharepoint-url:" + urls[0]: "weekly"}))
    drives, fehl = sp.resolve_drives(_two_libraries(), urls)
    assert [d["name"] for d in drives] == ["Documents"] and fehl == 1
    assert drives[0]["kadenz"] == "weekly"
    (ev,) = [e for e in _events(capsys) if e["k"] == "run.sharepoint.path_unmatched"]
    assert ev["level"] == "err" and ev["v"]["path"] == "Typo/Folder"


def test_a_folder_address_finds_its_folder_in_any_case(tmp_path):
    """Typed as folder/b, mirrored as Folder/B: still that unit, with its
    cadence and its stamp."""
    db = sp.state_db.StateDb(tmp_path)
    e = sp.drive_einheiten({}, {"site": "TeamX", "name": "Templates", "kadenz": "weekly",
                                "einheiten": {"folder/b": "daily"}}, db)
    assert e.einheit("Dateien/Folder/B/x.pdf") == "Dateien/folder/b"
    assert e.kadenz("Dateien/folder/b") == "daily"
    # A folder value typed in the other case is the same unit: the address
    # wins, and the unit is listed once.
    e = sp.drive_einheiten({"sharepoint:TeamX/Templates/Dateien/Folder/B": "monthly"},
                           {"site": "TeamX", "name": "Templates", "kadenz": "weekly",
                            "einheiten": {"folder/b": "daily"}}, db)
    assert e.ordner == ["Dateien/folder/b"] and e.kadenz("Dateien/folder/b") == "daily"
    # A key written with a trailing slash (by hand, by a script) is no crash.
    e = sp.drive_einheiten({"sharepoint:TeamX/Templates/Dateien/X/": "weekly",
                            "sharepoint:TeamX/Templates/Dateien/x": "daily"},
                           {"site": "TeamX", "name": "Templates", "kadenz": "monthly"}, db)
    assert e.ordner == ["Dateien/x"] and e.einheit("Dateien/X/a.pdf") == "Dateien/x"
    assert e.kadenz("Dateien/x") == "daily"
    e = sp.drive_einheiten({"sharepoint:TeamX/Templates/Dateien/X/": "daily"},
                           {"site": "TeamX", "name": "Templates", "kadenz": "monthly"}, db)
    assert e.ordner == ["Dateien/X"] and e.kadenz("Dateien/X") == "daily", "the slash hid the cadence"
    # Two spellings that normalise to one folder: the more frequent stands,
    # whichever came last.
    for order in ({"sharepoint:TeamX/Templates/Dateien/X/": "daily", "sharepoint:TeamX/Templates/Dateien/X": "monthly"},
                  {"sharepoint:TeamX/Templates/Dateien/X": "monthly", "sharepoint:TeamX/Templates/Dateien/X/": "daily"}):
        e = sp.drive_einheiten(order, {"site": "TeamX", "name": "Templates", "kadenz": "weekly"}, db)
        assert e.kadenz("Dateien/X") == "daily"
    # A folder value written at the address's path with a trailing slash
    # is the same path: the address is the unit there, not outvoted by the
    # more frequent value.
    e = sp.drive_einheiten({"sharepoint:TeamX/Templates/Dateien/Folder/B/": "daily"},
                           {"site": "TeamX", "name": "Templates", "kadenz": "weekly",
                            "einheiten": {"Folder/B": "monthly"}}, db)
    assert e.ordner == ["Dateien/Folder/B"] and e.kadenz("Dateien/Folder/B") == "monthly"
    # A doubled slash is no folder of its own: the unit is found, with its cadence.
    e = sp.drive_einheiten({"sharepoint:TeamX/Templates/Dateien//X": "daily"},
                           {"site": "TeamX", "name": "Templates", "kadenz": "weekly"}, db)
    assert e.ordner == ["Dateien/X"] and e.einheit("Dateien/X/a.pdf") == "Dateien/X"
    assert e.kadenz("Dateien/X") == "daily"


def test_each_folder_address_is_a_unit_of_its_own(tmp_path):
    db = sp.state_db.StateDb(tmp_path)
    d = {"site": "TeamX", "name": "Templates", "kadenz": "weekly",
         "einheiten": {"Folder/B": "weekly", "Folder/C": "daily"}}
    e = sp.drive_einheiten({}, d, db)
    assert e.ordner == ["Dateien/Folder/B", "Dateien/Folder/C"]
    assert e.kadenz("Dateien/Folder/C") == "daily"
    assert e.einheit("Dateien/Folder/C/sub/a.pdf") == "Dateien/Folder/C"
    # A folder value set below an address paces what lies under it.
    e = sp.drive_einheiten({"sharepoint:TeamX/Templates/Dateien/Folder/C/Archiv": "monthly"}, d, db)
    assert e.kadenz("Dateien/Folder/C/Archiv") == "monthly"


def test_a_new_address_comes_on_the_next_run(tmp_path, monkeypatch, capsys):
    """Added to a library that is not due: it has no stamp yet, so it runs at
    once – and the widened scope reads the library once, so its files come."""
    monkeypatch.setenv("SYNC_CADENCE", "{}")
    files = [_sp_datei("b", "b.pdf", "/drive/root:/Folder/B"),
             _sp_datei("d", "d.pdf", "/drive/root:/Folder/D")]
    d = {"id": "d2", "site": "TeamX", "name": "Templates", "kadenz": "weekly",
         "prefixes": {"Folder/B"}, "einheiten": {"Folder/B": "weekly"}, "urls": [BASIS + "Templates/Folder/B"]}
    sp.lauf(_TaktGraph(files), tmp_path, [dict(d)])
    capsys.readouterr()
    # Nothing due a minute later …
    g = _TaktGraph(files)
    sp.lauf(g, tmp_path, [dict(d)])
    assert g.geladen == []
    # … until a folder address joins it.
    widened = dict(d, prefixes={"Folder/B", "Folder/D"},
                   einheiten={"Folder/B": "weekly", "Folder/D": "weekly"})
    g = _TaktGraph(files)
    sp.lauf(g, tmp_path, [widened])
    assert g.geladen == ["d"]
