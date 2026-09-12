"""onenote_export.py – notebooks walked to the page, resources offline.

The Graph side is faked throughout; what matters here is the export's own
part: section groups walked recursively, pages fetched only when they
moved, images embedded or filed by size, attachments linked, and the marker
for pages that left the notebook.
"""

import json
import re

import pytest

import onenote_export as on
import progress
import state_db


def _events(capsys):
    return [e for e in (progress.lies_event(z) for z in
                        capsys.readouterr().out.splitlines()) if e]


def _stand(ziel):
    """The page bookkeeping – one record per page in the notebook's state.db."""
    return {k: json.loads(v) for k, v in
            state_db.StateDb(ziel).saetze_lesen("pages").items()}


IMG = "https://graph.microsoft.com/v1.0/users('u')/onenote/resources/img-1/$value"
PDF = "https://graph.microsoft.com/v1.0/users('u')/onenote/resources/file-1/$value"

SEITE_HTML = (
    '<html lang="de-DE"><head><title>Besprechung</title>'
    '<meta name="created" content="2026-07-01T10:00:00.0000000" /></head>'
    '<body data-absolute-enabled="true" style="font-family:Calibri">'
    '<div style="position:absolute;left:48px;top:115px;width:624px">'
    "<p>Notizen zur <b>Besprechung</b><script>boese()</script></p>"
    f'<img src="{IMG}" data-src-type="image/png" data-fullres-src="{IMG}" '
    'data-fullres-src-type="image/png" width="10" />'
    f'<object data="{PDF}" data-attachment="Protokoll.pdf" type="application/pdf" />'
    "</div></body></html>")


class _Graph:
    def __init__(self, antworten, bytes_map=None):
        self.antworten = antworten
        self.bytes_map = bytes_map or {}
        self.aufrufe = []
        self.geladen = []

    def _finde(self, url, tabelle):
        for muster, antwort in tabelle.items():
            if muster in url:
                return antwort
        raise AssertionError(f"unerwartete URL: {url}")

    def get(self, url):
        self.aufrufe.append(url)
        antwort = self._finde(url, self.antworten)
        if isinstance(antwort, Exception):
            raise antwort
        return antwort

    def paged(self, url):
        return iter(self.get(url).get("value", []))

    def get_bytes(self, url, label=""):
        self.geladen.append(url)
        antwort = self._finde(url, self.bytes_map)
        if isinstance(antwort, Exception):
            raise antwort
        return antwort


NB = {"id": "nb1", "titel": "Projekte", "geteilt": False}


def _page(pid, titel, lm="2026-07-01T10:00:00Z"):
    return {"id": pid, "title": titel, "createdDateTime": "2026-06-01T10:00:00Z",
            "lastModifiedDateTime": lm, "level": 0, "order": 0}


def _graph(pages_a, pages_b=None, seite=SEITE_HTML, bild=b"PNG"):
    """The plain shape: the API refuses the expanded tree and the notebook-
    wide page listing, so the export walks group by group and lists per
    section – the paths the cheap calls fall back to."""
    import requests
    return _Graph({
        "/onenote/notebooks/nb1?$expand=": requests.HTTPError("400"),
        "/onenote/pages?$filter=": requests.HTTPError("400"),
        "/onenote/notebooks/nb1/sections": {"value": [
            {"id": "s1", "displayName": "Allgemein"}]},
        "/onenote/notebooks/nb1/sectionGroups": {"value": [
            {"id": "g1", "displayName": "2026"}]},
        "/onenote/sectionGroups/g1/sections": {"value": [
            {"id": "s2", "displayName": "Q3"}]},
        "/onenote/sectionGroups/g1/sectionGroups": {"value": []},
        "/onenote/sections/s1/pages": {"value": pages_a},
        "/onenote/sections/s2/pages": {"value": pages_b or []},
    }, bytes_map={
        "/pages/": (seite.encode("utf-8"), "text/html"),
        "resources/img-1": (bild, "image/png"),
        "resources/file-1": (b"PDF", "application/pdf"),
    })


def test_sections_laufen_durch_die_gruppen():
    g = _graph([])
    gefunden = on.sections(g, NB)
    assert [(s["titel"], pfad) for s, pfad in gefunden] == \
        [("Allgemein", []), ("Q3", ["2026"])]


def test_notebook_lauf_schreibt_seiten_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("ONENOTE_IMAGE_MAX_MB", "1")
    g = _graph([_page("p1", "Besprechung")], [_page("p2", "Planung")])
    neu, unveraendert, fehler = on.notebook_lauf(g, tmp_path, NB, on.bild_max())
    assert (neu, unveraendert, fehler) == (2, 0, 0)
    ziel = on.notebook_ziel(tmp_path, NB)
    seiten = sorted(p.relative_to(ziel).as_posix() for p in ziel.rglob("*.html"))
    assert seiten[0].startswith("2026/Q3/") and seiten[0].endswith(".html")
    assert seiten[1].startswith("Allgemein/")
    html = (ziel / seiten[1]).read_text(encoding="utf-8")
    assert "Projekte › Allgemein" in html and "erstellt 2026-06-01" in html
    assert 'src="data:image/png;base64,' in html, "kleines Bild gehört eingebettet"
    assert "data-fullres-src" not in html and "graph.microsoft.com" not in html
    assert "boese()" not in html
    assert 'class="mn-anhang" href="' in html and "Protokoll.pdf" in html
    ordner = ziel / (seiten[1][:-5] + on.DATEI_SUFFIX)
    assert any(f.name.endswith("Protokoll.pdf") and f.read_bytes() == b"PDF"
               for f in ordner.iterdir())


def test_grosse_bilder_landen_als_datei(tmp_path, monkeypatch):
    monkeypatch.setenv("ONENOTE_IMAGE_MAX_MB", "1")
    g = _graph([_page("p1", "Fotos")], bild=b"x" * (2 * 1024 * 1024))
    on.notebook_lauf(g, tmp_path, NB, on.bild_max())
    ziel = on.notebook_ziel(tmp_path, NB)
    html = next(ziel.rglob("*.html")).read_text(encoding="utf-8")
    assert "base64" not in html
    assert f'src="{on.DATEI_SUFFIX}' not in html
    assert on.DATEI_SUFFIX + "/" in html and ".png" in html


def test_unbewegte_seiten_werden_nicht_erneut_geholt(tmp_path):
    g = _graph([_page("p1", "Besprechung")])
    on.notebook_lauf(g, tmp_path, NB, 0)
    g2 = _graph([_page("p1", "Besprechung")])
    neu, unveraendert, fehler = on.notebook_lauf(g2, tmp_path, NB, 0)
    assert (neu, unveraendert) == (0, 1)
    assert g2.geladen == [], "Seite trotz gleichem Änderungsdatum geholt"

    g3 = _graph([_page("p1", "Besprechung", lm="2026-07-09T10:00:00Z")])
    neu, unveraendert, fehler = on.notebook_lauf(g3, tmp_path, NB, 0)
    assert neu == 1 and any("/pages/p1/content" in u for u in g3.geladen)


def test_verschwundene_seite_bleibt_mit_marker(tmp_path):
    on.notebook_lauf(_graph([_page("p1", "A"), _page("p2", "B")]), tmp_path, NB, 0)
    on.notebook_lauf(_graph([_page("p1", "A")]), tmp_path, NB, 0)
    ziel = on.notebook_ziel(tmp_path, NB)
    stand = _stand(ziel)
    assert stand["p2"]["deleted"] and not stand["p1"]["deleted"]
    html = (ziel / stand["p2"]["rel"]).read_text(encoding="utf-8")
    assert "Nicht mehr im Notizbuch seit" in html
    seit = stand["p2"]["deleted"]
    # A third run must not move the timestamp.
    on.notebook_lauf(_graph([_page("p1", "A")]), tmp_path, NB, 0)
    stand = _stand(ziel)
    assert stand["p2"]["deleted"] == seit
    assert html.count("mn-weg") == (ziel / stand["p2"]["rel"]).read_text(
        encoding="utf-8").count("mn-weg")


def test_listenfehler_verhindern_grabsteine(tmp_path, capsys):
    on.notebook_lauf(_graph([_page("p1", "A")], [_page("p2", "B")]), tmp_path, NB, 0)
    g = _graph([_page("p1", "A")])
    g.antworten["/onenote/sections/s2/pages"] = RuntimeError("429")
    neu, unveraendert, fehler = on.notebook_lauf(g, tmp_path, NB, 0)
    assert fehler == 1
    stand = _stand(on.notebook_ziel(tmp_path, NB))
    assert not stand["p2"]["deleted"]
    assert any(e["k"] == "run.onenote.section_failed" for e in _events(capsys))


def test_umbenannter_abschnitt_raeumt_die_alte_kopie_weg(tmp_path):
    on.notebook_lauf(_graph([_page("p1", "A")]), tmp_path, NB, 0)
    ziel = on.notebook_ziel(tmp_path, NB)
    alt = _stand(ziel)["p1"]["rel"]
    g = _graph([_page("p1", "A")])
    g.antworten["/onenote/notebooks/nb1/sections"] = {"value": [
        {"id": "s1", "displayName": "Umbenannt"}]}
    on.notebook_lauf(g, tmp_path, NB, 0)
    neu = _stand(ziel)["p1"]["rel"]
    assert neu != alt and neu.startswith("Umbenannt/")
    assert not (ziel / alt).exists() and (ziel / neu).exists()


def test_seiten_rel_entschaerft_titel():
    section = {"id": "s1", "titel": "A/B: C"}
    rel = on.seiten_rel(section, ["Grp?"], {"id": "p9", "title": "Was? 100% #1"})
    assert "?" not in rel and "%" not in rel
    assert rel.endswith(".html") and rel.startswith("Grp_/A_B_ C/Was_ 100_ _1__")


NB_LISTE = {"/onenote/notebooks": {"value": [
    {"id": "n1", "displayName": "Projekte"},
    {"id": "n2", "displayName": "Privat", "isDefault": True},
    {"id": "n3", "displayName": "projekte"}]}}


def test_notizbuch_eintraege_taggen_nur_den_namensvetter():
    """The first notebook keeps its plain name, only the namesake gets the
    tag – and a known notebook keeps its folder on the next sync even when
    the namesake now sorts first."""
    buecher = on.list_notebooks(_Graph(NB_LISTE))
    assert [b["id"] for b in buecher] == ["n2", "n1", "n3"], "the default notebook leads"
    eintraege = on.notizbuch_eintraege(buecher)
    pfade = {e["id"]: e["pfad"] for e in eintraege}
    assert pfade == {"n2": "Privat", "n1": "Projekte", "n3": f"projekte__{on.export_util.kuerzel('n3')}"}
    assert eintraege[0]["standard"] is True and eintraege[0]["elemente"] == 0
    # Next sync: n1 is gone, n3 stays – and keeps its tagged folder.
    vorher = {"ordner": eintraege}
    spaeter = on.notizbuch_eintraege([b for b in buecher if b["id"] != "n1"], vorher)
    assert {e["id"]: e["pfad"] for e in spaeter} == {"n2": "Privat", "n3": pfade["n3"]}


def test_waehle_notizbuecher_holt_die_liste_einmal_und_folgt_den_regeln(tmp_path, monkeypatch, capsys):
    import folders
    monkeypatch.delenv("ONENOTE_ONLY", raising=False)
    monkeypatch.setenv("ONENOTE_RULES", "")
    monkeypatch.setenv("SYNC_CADENCE", '{"onenote:n1": "weekly"}')
    g = _Graph(NB_LISTE)
    gewaehlt = on.waehle_notizbuecher(g, tmp_path)
    assert [b["ordner"] for b in gewaehlt] == ["Privat", "Projekte", f"projekte__{on.export_util.kuerzel('n3')}"]
    assert {b["id"]: b["kadenz"] for b in gewaehlt}["n1"] == "weekly"
    assert folders.lade(tmp_path, folders.NOTIZBUECHER)["ordner"], "the list is stored"
    assert any(e["k"] == "run.notebooks.loading" for e in _events(capsys))
    # Second call: no listing – the stored list decides, the rules narrow it.
    monkeypatch.setenv("ONENOTE_RULES", "- **\n+ Projekte")
    g2 = _Graph({})
    assert [b["titel"] for b in on.waehle_notizbuecher(g2, tmp_path)] == ["Projekte"]
    assert g2.aufrufe == []
    # "Sync now" narrows to one notebook – within the rules.
    monkeypatch.setenv("ONENOTE_ONLY", "n2")
    assert on.waehle_notizbuecher(g2, tmp_path) == []
    monkeypatch.setenv("ONENOTE_RULES", "")
    assert [b["id"] for b in on.waehle_notizbuecher(g2, tmp_path)] == ["n2"]


def test_gleiche_notizbuecher_ab_meldet_die_aenderungen(tmp_path, monkeypatch, capsys):
    import folders
    monkeypatch.setenv("ONENOTE_RULES", "")
    on.gleiche_notizbuecher_ab(_Graph(NB_LISTE), tmp_path)
    kleiner = {"/onenote/notebooks": {"value": [
        {"id": "n2", "displayName": "Privat", "isDefault": True},
        {"id": "n4", "displayName": "Neu"}]}}
    on.gleiche_notizbuecher_ab(_Graph(kleiner), tmp_path)
    daten = folders.lade(tmp_path, folders.NOTIZBUECHER)
    assert daten["neu"] == ["Neu"] and sorted(daten["verschwunden"]) == \
        ["Projekte", f"projekte__{on.export_util.kuerzel('n3')}"]
    ereignisse = _events(capsys)
    assert any(e["k"] == "run.sync.changed" and e["v"]["gone"] == 2 for e in ereignisse)
    assert any(e["k"] == "run.sync.result" and e["v"]["chosen"] == 2 for e in ereignisse)


def test_lauf_ueberspringt_notizbuch_unter_kadenz(tmp_path, monkeypatch, capsys):
    import time
    nb = dict(NB, kadenz="weekly", ordner="Projekte")
    state_db.StateDb(on.notebook_ziel(tmp_path, nb)).kv_schreiben("last_sync", str(time.time()))
    gelaufen = []
    monkeypatch.setattr(on, "notebook_lauf", lambda *a, **kw: gelaufen.append(1) or (1, 0, 0))
    on.lauf(_Graph({}), tmp_path, [nb])
    assert gelaufen == []
    assert any(e["k"] == "run.cadence.skip" for e in _events(capsys))
    # Without a last sync, or with "always", it runs – and stamps the sync.
    frisch = dict(NB, kadenz="always", ordner="Privat")
    on.lauf(_Graph({}), tmp_path, [frisch])
    assert gelaufen == [1]
    assert state_db.StateDb(on.notebook_ziel(tmp_path, frisch)).kv_lesen("last_sync")


def test_seitendatei_traegt_das_aenderungsdatum(tmp_path):
    import os
    on.notebook_lauf(_graph([_page("p1", "A", lm="2026-07-01T10:00:00Z")]), tmp_path, NB, 0)
    datei = next(on.notebook_ziel(tmp_path, NB).rglob("*.html"))
    assert abs(os.stat(datei).st_mtime - 1782900000) < 86400 * 400
    import export_util
    assert int(os.stat(datei).st_mtime) == int(
        export_util.graph_zeit("2026-07-01T10:00:00Z").timestamp())


def test_corpus_liest_seiten_und_marker(tmp_path):
    import corpus
    on.notebook_lauf(_graph([_page("p1", "Besprechung"), _page("p2", "Weg")],
                            [_page("p3", "Planung")]), tmp_path, NB, 0)
    on.notebook_lauf(_graph([_page("p1", "Besprechung")], [_page("p3", "Planung")]),
                     tmp_path, NB, 0)
    ziel = on.notebook_ziel(tmp_path, NB)
    # An attached .html inside a page's files folder must not read as a page.
    ordner = next(ziel.rglob("*.html"))
    (ordner.parent / (ordner.name[:-5] + on.DATEI_SUFFIX)).mkdir(exist_ok=True)
    (ordner.parent / (ordner.name[:-5] + on.DATEI_SUFFIX) / "anhang.html").write_text("<p>x</p>")
    saetze = sorted(corpus.load_onenote(tmp_path), key=lambda s: s["rel"])
    assert len(saetze) == 3, "die angehängte .html ist eine Datei, keine Seite"
    stand = _stand(ziel)
    je_rel = {s["rel"]: s for s in saetze}
    s = je_rel[f'{ziel.name}/{stand["p1"]["rel"]}']
    assert s["src"] == "onenote" and s["root"] == "onenote"
    assert s["title"] == "Besprechung", "der Titel kommt aus dem <title> der Seite"
    assert s["ctx"] == f"{ziel.name}/Allgemein"
    assert je_rel[f'{ziel.name}/{stand["p3"]["rel"]}']["ctx"] == f"{ziel.name}/2026/Q3"
    assert "Notizen zur Besprechung" in s["text"]
    assert "Projekte › Allgemein" not in s["text"], "der Kopf ist Navigation, nicht Notiz"
    assert "boese" not in s["text"] and "base64" not in s["text"]
    assert je_rel[f'{ziel.name}/{stand["p2"]["rel"]}']["gone"] and "gone" not in s
    assert corpus.manifest("onenote", tmp_path).keys() == {x["rel"] for x in saetze}


def test_sections_kommen_aus_einem_aufruf_wenn_die_api_mitspielt():
    g = _Graph({"/onenote/notebooks/nb1?$expand=": {
        "id": "nb1", "sections": [{"id": "s1", "displayName": "Allgemein"}],
        "sectionGroups": [{"id": "g1", "displayName": "2026",
                           "sections": [{"id": "s2", "displayName": "Q3"}],
                           "sectionGroups": [{"id": "g2", "displayName": "Alt",
                                              "sections": [{"id": "s3", "displayName": "Q1"}]}]}]}})
    gefunden = on.sections(g, NB)
    assert [(s["titel"], pfad) for s, pfad in gefunden] == \
        [("Allgemein", []), ("Q3", ["2026"]), ("Q1", ["2026", "Alt"])]
    assert len(g.aufrufe) == 1, "one request for the whole tree"


def test_notizbuchweite_seitenliste_spart_die_abfrage_je_abschnitt(tmp_path):
    import requests
    g = _Graph({
        "/onenote/notebooks/nb1?$expand=": {
            "id": "nb1", "sections": [{"id": "s1", "displayName": "Allgemein"},
                                      {"id": "s2", "displayName": "Q3"}],
            "sectionGroups": []},
        "/onenote/pages?$filter=": {"value": [
            dict(_page("p1", "Besprechung"), parentSection={"id": "s1"}),
            dict(_page("p2", "Planung"), parentSection={"id": "s2"})]},
        "/onenote/sections/": requests.HTTPError("must not be asked"),
    }, bytes_map={"/pages/": (SEITE_HTML.encode("utf-8"), "text/html"),
                  "resources/img-1": (b"PNG", "image/png"),
                  "resources/file-1": (b"PDF", "application/pdf")})
    neu, unveraendert, fehler = on.notebook_lauf(g, tmp_path, NB, 0)
    assert (neu, unveraendert, fehler) == (2, 0, 0)
    assert not any("/onenote/sections/" in u for u in g.aufrufe)
    assert sum(1 for u in g.aufrufe if "/onenote/" in u) == 2, "tree + page list"


def _zaehlend(g):
    """The fake never reaches graph_client.fetch, so it registers its own
    requests with the pacer – the way a real request would."""
    import time

    import graph_client
    urspruenglich_get, urspruenglich_bytes = g.get, g.get_bytes

    def get(url):
        graph_client.TAKT.zeiten.append(time.time())
        return urspruenglich_get(url)

    def get_bytes(url, label=""):
        graph_client.TAKT.zeiten.append(time.time())
        return urspruenglich_bytes(url, label)
    g.get, g.get_bytes = get, get_bytes
    return g


def test_budget_beendet_den_lauf_sauber_und_der_naechste_macht_weiter(tmp_path, capsys):
    import graph_client
    jetzt = __import__("time").time()
    # The hour is nearly spent before the run starts: room for the listing
    # (tree attempt, four walk calls, page-list attempt, two sections) and
    # one page with its two resources – not for the second page.
    graph_client.takt(on.PRO_MINUTE, on.PRO_STUNDE,
                      [jetzt - 10.0] * (on.PRO_STUNDE - 12))
    g = _zaehlend(_graph([_page("p1", "A"), _page("p2", "B"), _page("p3", "C")]))
    zwei = dict(NB, id="nb1", ordner="Projekte")
    on.lauf(g, tmp_path, [zwei, dict(NB, id="nb2", titel="Zweites", ordner="Zweites")])
    ereignisse = _events(capsys)
    budget = [e for e in ereignisse if e["k"] == "run.onenote.budget"]
    assert budget and budget[0]["v"]["n"] >= 1 and budget[0]["v"]["name"] == "Projekte"
    assert any(e["k"] == "run.onenote.budget_rest" and e["v"]["n"] == 1 for e in ereignisse)
    stand_vorher = _stand(on.notebook_ziel(tmp_path, zwei))
    assert 1 <= len(stand_vorher) < 3, "what arrived is kept, the rest waits"
    assert not state_db.StateDb(on.notebook_ziel(tmp_path, zwei)).kv_lesen("last_sync"), \
        "a cut-short notebook must not count as synced"
    # Next run, fresh hour: the missing pages come, nothing is fetched twice.
    graph_client.takt(on.PRO_MINUTE, on.PRO_STUNDE, [])
    g2 = _zaehlend(_graph([_page("p1", "A"), _page("p2", "B"), _page("p3", "C")]))
    on.lauf(g2, tmp_path, [zwei])
    stand = _stand(on.notebook_ziel(tmp_path, zwei))
    assert len(stand) == 3
    assert len([u for u in g2.geladen if "/pages/" in u]) == 3 - len(stand_vorher), \
        "only the pages that never arrived are fetched"


def test_takt_wird_zwischen_laeufen_gemerkt(tmp_path):
    import time

    import graph_client
    takt = on.takt_einrichten(tmp_path)
    assert takt.zeiten == [] and takt is graph_client.TAKT
    takt.zeiten.extend([time.time() - 5000.0, time.time() - 5.0])
    on.takt_merken(tmp_path)
    graph_client.TAKT = None
    wieder = on.takt_einrichten(tmp_path)
    assert len(wieder.zeiten) == 1, "only the last hour survives"
    assert wieder.verbraucht(3600.0) == 1


def test_ueberlastet_beendet_den_lauf_ohne_fehlerleiter(tmp_path, capsys):
    import graph_client
    g = _graph([_page("p1", "A"), _page("p2", "B")])
    g.bytes_map["/pages/"] = graph_client.Ueberlastet("429")
    on.lauf(g, tmp_path, [dict(NB, ordner="Projekte")])
    ereignisse = _events(capsys)
    assert any(e["k"] == "run.onenote.throttled" for e in ereignisse)
    assert not any(e["k"] == "run.onenote.page_failed" for e in ereignisse), \
        "a spent budget is not a page error"
    assert len([u for u in g.geladen if "/pages/" in u]) == 1, "stops at the first refusal"


def test_seitenstand_wird_nach_jeder_seite_gesichert(tmp_path):
    """Cancel terminates the process; what arrived before must be known to
    the next run. So the bookkeeping is written page by page – and the
    stop is a BaseException that no page-level handler swallows."""
    on.takt_einrichten(tmp_path)
    g = _zaehlend(_graph([_page("p1", "A"), _page("p2", "B"), _page("p3", "C")]))
    urspruenglich = g.get_bytes

    def get_bytes(url, label=""):
        if "/pages/p2/" in url:
            raise on.Abgebrochen()
        return urspruenglich(url, label)
    g.get_bytes = get_bytes
    nb = dict(NB, ordner="Projekte")
    with pytest.raises(on.Abgebrochen):
        on.notebook_lauf(g, tmp_path, nb, 4 * 1024 * 1024)
    db = state_db.StateDb(on.notebook_ziel(tmp_path, nb))
    assert json.loads(db.kv_lesen("notebook"))["id"] == "nb1"
    assert list(_stand(db.pfad.parent)) == ["p1"], "the first page is kept"
    # The pacer's moments too: the next process must know this hour's cost.
    assert json.loads(state_db.StateDb(tmp_path).kv_lesen("takt"))
    # Next run: only the pages that never arrived are fetched.
    g2 = _zaehlend(_graph([_page("p1", "A"), _page("p2", "B"), _page("p3", "C")]))
    assert on.notebook_lauf(g2, tmp_path, nb, 4 * 1024 * 1024)[:2] == (2, 1)


def test_seitenstand_kommt_als_eine_zeile_je_seite(tmp_path):
    """One row per page, written when the page is done – the whole table
    is never rewritten; the tombstone run touches only the rows it marks."""
    on.notebook_lauf(_graph([_page("p1", "A"), _page("p2", "B")]), tmp_path, NB, 0)
    db = state_db.StateDb(on.notebook_ziel(tmp_path, NB))
    assert set(db.saetze_lesen("pages")) == {"p1", "p2"}
    aufrufe = []
    urspruenglich = db.saetze_schreiben

    def merkend(bereich, eintraege):
        aufrufe.append((bereich, sorted(eintraege)))
        return urspruenglich(bereich, eintraege)

    db.saetze_schreiben = merkend
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(state_db, "StateDb", lambda ordner: db)
        on.notebook_lauf(_graph([_page("p1", "A", lm="2026-07-09T10:00:00Z")]),
                         tmp_path, NB, 0)
    assert ("pages", ["p1"]) in aufrufe and ("pages", ["p2"]) in aufrufe
    assert all(len(keys) == 1 for bereich, keys in aufrufe if bereich == "pages")
    assert _stand(db.pfad.parent)["p2"]["deleted"]


def test_alter_seitenstand_zieht_in_die_saetze_um(tmp_path):
    """A notebook from 8.x holds its bookkeeping as one kv blob – it moves
    over once, and nothing is fetched again for it."""
    on.notebook_lauf(_graph([_page("p1", "A")]), tmp_path, NB, 0)
    db = state_db.StateDb(on.notebook_ziel(tmp_path, NB))
    # The 8.x shape: one kv blob, no rows.
    zeilen = {k: json.loads(v) for k, v in db.saetze_lesen("pages").items()}
    db.saetze_leeren("pages")
    db.kv_schreiben("pages", json.dumps(zeilen, ensure_ascii=False))
    g = _graph([_page("p1", "A")])
    assert on.notebook_lauf(g, tmp_path, NB, 0)[:2] == (0, 1)
    assert g.geladen == [] and set(db.saetze_lesen("pages")) == {"p1"}


def test_ressourcen_werden_beim_erneuten_abruf_nicht_neu_geladen(tmp_path):
    """The hour's 400 requests: a page that moved costs its content, not
    its images and attachments again – embedded or filed alike."""
    on.notebook_lauf(_graph([_page("p1", "Besprechung")]), tmp_path, NB, 0)
    ziel = on.notebook_ziel(tmp_path, NB)
    saetze = {k: json.loads(v) for k, v in
              state_db.StateDb(ziel).saetze_lesen("ressourcen").items()}
    assert set(saetze) == {"img-1", "file-1"}
    assert saetze["img-1"]["inline"] and saetze["img-1"]["rel"].endswith(".html")
    assert (ziel / saetze["file-1"]["rel"]).read_bytes() == b"PDF"
    assert saetze["file-1"]["size"] == 3 and saetze["file-1"]["seen"]
    g2 = _graph([_page("p1", "Besprechung", lm="2026-07-09T10:00:00Z")])
    neu, _, fehler = on.notebook_lauf(g2, tmp_path, NB, 0)
    assert (neu, fehler) == (1, 0)
    assert [u for u in g2.geladen if "/pages/" in u], "the content itself comes"
    assert not any("/resources/" in u for u in g2.geladen), "no resource request"
    html = next(ziel.rglob("*.html")).read_text(encoding="utf-8")
    assert 'src="data:image/png;base64,UE5H"' in html    # b"PNG" again
    assert "Protokoll.pdf" in html and 'class="mn-anhang" href="' in html


def test_abgelegte_ressourcen_ziehen_mit_der_seite_um(tmp_path, monkeypatch):
    """A renamed section moves the page: the filed image is copied along
    from disk, not fetched, and the old folder goes as before."""
    monkeypatch.setenv("ONENOTE_IMAGE_MAX_MB", "1")
    gross = b"x" * (2 * 1024 * 1024)
    on.notebook_lauf(_graph([_page("p1", "A")], bild=gross), tmp_path, NB, on.bild_max())
    ziel = on.notebook_ziel(tmp_path, NB)
    alt = _stand(ziel)["p1"]["rel"]
    g = _graph([_page("p1", "A", lm="2026-07-09T10:00:00Z")], bild=gross)
    g.antworten["/onenote/notebooks/nb1/sections"] = {"value": [
        {"id": "s1", "displayName": "Umbenannt"}]}
    on.notebook_lauf(g, tmp_path, NB, on.bild_max())
    assert not any("/resources/" in u for u in g.geladen)
    neu = _stand(ziel)["p1"]["rel"]
    assert neu.startswith("Umbenannt/") and not (ziel / alt).exists()
    assert not (ziel / (alt[:-5] + on.DATEI_SUFFIX)).exists()
    ordner = ziel / (neu[:-5] + on.DATEI_SUFFIX)
    assert any(f.read_bytes() == gross for f in ordner.iterdir())
    saetze = {k: json.loads(v) for k, v in
              state_db.StateDb(ziel).saetze_lesen("ressourcen").items()}
    assert saetze["img-1"]["rel"].startswith(neu[:-5] + on.DATEI_SUFFIX + "/")


def test_geaenderte_grenze_entscheidet_neu_ohne_abruf(tmp_path, monkeypatch):
    """Embedded last time, filed now: the bytes come from the old page."""
    on.notebook_lauf(_graph([_page("p1", "A")], bild=b"x" * 3000), tmp_path, NB, 0)
    monkeypatch.setenv("ONENOTE_IMAGE_MAX_MB", "0")
    g = _graph([_page("p1", "A", lm="2026-07-09T10:00:00Z")], bild=b"x" * 3000)
    on.notebook_lauf(g, tmp_path, NB, 1000)
    assert not any("/resources/" in u for u in g.geladen)
    ziel = on.notebook_ziel(tmp_path, NB)
    html = next(ziel.rglob("*.html")).read_text(encoding="utf-8")
    assert "base64" not in html.split("<img")[1].split(">")[0]
    ordner = next(p for p in ziel.rglob("*" + on.DATEI_SUFFIX) if p.is_dir())
    assert any(f.read_bytes() == b"x" * 3000 for f in ordner.iterdir())


def test_fehlende_datei_wird_wieder_geholt(tmp_path, monkeypatch):
    """A record whose file is gone (somebody tidied the folder) is no
    reason to leave a hole: the resource is fetched again."""
    monkeypatch.setenv("ONENOTE_IMAGE_MAX_MB", "1")
    gross = b"x" * (2 * 1024 * 1024)
    on.notebook_lauf(_graph([_page("p1", "A")], bild=gross), tmp_path, NB, on.bild_max())
    ziel = on.notebook_ziel(tmp_path, NB)
    ordner = next(p for p in ziel.rglob("*" + on.DATEI_SUFFIX) if p.is_dir())
    next(f for f in ordner.iterdir() if f.suffix == ".png").unlink()
    g = _graph([_page("p1", "A", lm="2026-07-09T10:00:00Z")], bild=gross)
    on.notebook_lauf(g, tmp_path, NB, on.bild_max())
    assert [u for u in g.geladen if "/resources/" in u] == [IMG]
    ordner = next(p for p in ziel.rglob("*" + on.DATEI_SUFFIX) if p.is_dir())
    assert any(f.read_bytes() == gross for f in ordner.iterdir())


def test_abgelegte_dateien_sind_von_der_seite_aus_verlinkt(tmp_path, monkeypatch):
    """The page links its folder relative to itself – opened from disk or
    through the app's route, every link must resolve."""
    monkeypatch.setenv("ONENOTE_IMAGE_MAX_MB", "1")
    on.notebook_lauf(_graph([_page("p1", "Fotos")], bild=b"x" * (2 * 1024 * 1024)),
                     tmp_path, NB, on.bild_max())
    seite = next(on.notebook_ziel(tmp_path, NB).rglob("*.html"))
    html = seite.read_text(encoding="utf-8")
    ziele = [z for z in re.findall(r'(?:src|href)="([^"]+)"', html)
             if not z.startswith("data:")]
    assert len(ziele) == 2, "one filed image, one attachment"
    for z in ziele:
        assert "/" in z and not z.startswith("/")
        assert (seite.parent / z).is_file(), z


def test_abbruchsignal_wird_zum_sauberen_stopp(monkeypatch):
    import signal
    installiert = {}
    monkeypatch.setattr(signal, "signal", lambda num, fn: installiert.__setitem__(num, fn))
    on.abbruch_einrichten()
    with pytest.raises(on.Abgebrochen):
        installiert[signal.SIGTERM](signal.SIGTERM, None)
    assert not issubclass(on.Abgebrochen, Exception), \
        "a page-level `except Exception` must not turn the stop into a page error"


def test_leeres_kontingent_stoppt_vor_dem_listen(tmp_path, capsys):
    """A listing into a spent hour would be refused request by request –
    each refusal counting against the next hour. Say it once, stop."""
    import time

    import graph_client
    graph_client.takt(on.PRO_MINUTE, on.PRO_STUNDE, [time.time() - 10.0] * on.PRO_STUNDE)
    g = _graph([_page("p1", "A")])
    on.lauf(g, tmp_path, [dict(NB, ordner="Projekte")])
    assert g.aufrufe == [] and g.geladen == [], "no request into a spent hour"
    assert any(e["k"] == "run.onenote.budget_wait" for e in _events(capsys))
    with pytest.raises(on.BudgetLeer):
        on.gleiche_notizbuecher_ab(g, tmp_path)
    assert g.aufrufe == []


def test_full_sync_holt_jede_seite_und_ressource_erneut(tmp_path, monkeypatch, capsys):
    """"Force full sync": the page stamps and the resource records are
    forgotten – content, images and attachments come again."""
    on.notebook_lauf(_graph([_page("p1", "Besprechung")]), tmp_path, NB, 0)
    monkeypatch.setenv("FULL_SYNC", "1")
    g2 = _graph([_page("p1", "Besprechung")])
    neu, unveraendert, fehler = on.notebook_lauf(g2, tmp_path, NB, 0)
    assert (neu, unveraendert, fehler) == (1, 0, 0)
    assert any("/pages/p1/content" in u for u in g2.geladen)
    assert any("/resources/" in u for u in g2.geladen), "resources fetched again"
    ziel = on.notebook_ziel(tmp_path, NB)
    assert _stand(ziel)["p1"]["lm"] == "2026-07-01T10:00:00Z"
    assert set(state_db.StateDb(ziel).saetze_lesen("ressourcen")) == {"img-1", "file-1"}


def test_full_sync_vergisst_die_stempel_vor_dem_ersten_abruf(tmp_path, monkeypatch):
    """The hour's budget may end the run before the first page: the stamps
    are already gone, so the next regular run fetches what is left."""
    on.notebook_lauf(_graph([_page("p1", "Besprechung")]), tmp_path, NB, 0)
    ziel = on.notebook_ziel(tmp_path, NB)
    monkeypatch.setenv("FULL_SYNC", "1")

    def leer():
        raise on.BudgetLeer()
    monkeypatch.setattr(on, "budget_pruefen", leer)
    with pytest.raises(on.BudgetLeer):
        on.notebook_lauf(_graph([_page("p1", "Besprechung")]), tmp_path, NB, 0)
    assert _stand(ziel)["p1"]["lm"] == ""
    assert state_db.StateDb(ziel).saetze_lesen("ressourcen") == {}
