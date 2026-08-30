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

    def fake_lauf(graph, out, wahl, arbeiter, still=False):
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
                        lambda graph, out, wahl, still=False: next(berichte))

    class G:
        pass

    bericht = sp.nur_pruefen(G(), tmp_path, drives)
    assert bericht["erwartet"] == 15 and bericht["fehlt"] == 6
    assert bericht["bytes"] == 4 * 1048576
    gespeichert = json.loads((tmp_path / sp.BERICHT_DATEI).read_text(encoding="utf-8"))
    assert {z["ordner"] for z in gespeichert["ordner"]} == {"S/A", "S/B"}
    events = _events(capsys)
    vorschau = [e for e in events if e["k"] == "run.sharepoint.preview"]
    assert len(vorschau) == 2 and vorschau[0]["v"]["mb"] == 3
