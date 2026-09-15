"""
Search history, saved searches and cases (11.0) through the app: the
routes under /api/suche/* and /api/faelle/*, the `case` filter on
/api/search, the settings that govern them, the export as a run – and
the page's side of it in node: the third door, the tick on a hit, the
mark, the windows.

The store is built with keys the way an index run assigns them
(schluessel.zuweisen), so a case can point at its hits.
"""

import re
import time
import json
import threading

import pytest

import app as app_mod
import corpus
import faelle
import rag_index
import schluessel
import case_export
from tests.hilfen import call
from tests.test_app import (sandbox, _warte, _in_node, GRUNDZUSTAND, TEAMS_HTML)  # noqa: F401

MAIL = ("Message-ID: <m1@example.com>\nFrom: Carla Chef <carla@example.com>\n"
        "To: alice@example.com\nSubject: Rechnung 4711 freigegeben\nDate: Tue, 10 Jun 2025 08:00:00 +0000\n\n"
        "Hallo zusammen, die Rechnung 4711 ist freigegeben.\n")
MAIL2 = ("Message-ID: <m2@example.com>\nFrom: Alice Beispiel <alice@example.com>\n"
         "To: bob@example.com\nSubject: Urlaubsantrag August\nDate: Tue, 1 Jul 2025 12:00:00 +0000\n\n"
         "Hiermit beantrage ich Urlaub.\n")


@pytest.fixture
def welt(sandbox, with_ollama):  # noqa: F811
    """A store with keys, the app on a port, an empty case book."""
    teams = sandbox / "teams_export" / "1on1"
    teams.mkdir(parents=True)
    (teams / "alice__abc.html").write_text(TEAMS_HTML, encoding="utf-8")
    outlook = sandbox / "outlook_export" / "inbox"
    outlook.mkdir(parents=True)
    (outlook / "mail1.eml").write_text(MAIL, encoding="utf-8")
    (outlook / "mail2.eml").write_text(MAIL2, encoding="utf-8")
    recs = corpus.load_records(str(sandbox / "teams_export"), str(sandbox / "outlook_export"))
    chunks = corpus.chunk_records(recs)
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    schluessel.zuweisen(chunks, {"teams": sandbox / "teams_export", "outlook": sandbox / "outlook_export"})
    (sandbox / "rag_store").mkdir()
    rag_index.write_db(sandbox / "rag_store", chunks)
    rag_index.write_info(sandbox / "rag_store", None, 0, len(chunks))
    a = app_mod.App(app_mod.load_config())
    a.cfg["case_export_dir"] = str(sandbox / "exporte")
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"app": a, "port": httpd.server_address[1], "sandbox": sandbox, "chunks": chunks}
    httpd.shutdown()
    httpd.server_close()


def _treffer(welt, q="Rechnung", **extra):
    p = "&".join(f"{k}={v}" for k, v in {"q": q, **extra}.items())
    code, r = call(welt["port"], "GET", "/api/search?" + p)
    assert code == 200, r
    return r


def _fall(welt, name="Nordwind", beschreibung=""):
    code, r = call(welt["port"], "POST", "/api/faelle/anlegen", {"name": name, "beschreibung": beschreibung})
    assert code == 200 and r["ok"], r
    return r["id"]


def _eintrag(h):
    return {"key": h["key"], "src": h["source"], "root": h["root"], "rel": h["path"],
            "titel": h["title"], "datum": h["date"], "wer": h["who"]}


# --------------------------------------------------------------------------
# The search history
# --------------------------------------------------------------------------
def test_suche_landet_in_der_historie_mit_kriterien_ohne_treffer(welt):
    port = welt["port"]
    r = _treffer(welt, "Rechnung", source="outlook", mode="lexical")
    assert r["count"] >= 1
    code, h = call(port, "GET", "/api/suche/historie")
    assert code == 200 and h["retention"] == "90"
    assert len(h["searches"]) == 1
    k = h["searches"][0]["kriterien"]
    assert k["q"] == "Rechnung" and k["source"] == "outlook" and k["mode"] == "text"
    assert h["searches"][0]["treffer"] == r["count"]
    assert "results" not in h["searches"][0] and "hits" not in json.dumps(h["searches"][0])
    # the second page of the same search is not a second search
    _treffer(welt, "Rechnung", source="outlook", mode="lexical", offset="20")
    # the same criteria again move the entry, they do not pile up
    _treffer(welt, "Rechnung", source="outlook", mode="lexical")
    assert len(call(port, "GET", "/api/suche/historie")[1]["searches"]) == 1
    # a different search is a new entry, a browse without criteria is none
    _treffer(welt, "Urlaub")
    _treffer(welt, "")
    assert len(call(port, "GET", "/api/suche/historie")[1]["searches"]) == 2
    code, r = call(port, "POST", "/api/suche/historie-leeren", {})
    assert code == 200 and r["ok"]
    assert call(port, "GET", "/api/suche/historie")[1]["searches"] == []


def test_historie_aus_merkt_nichts_und_leert(welt):
    port = welt["port"]
    _treffer(welt, "Rechnung")
    assert len(call(port, "GET", "/api/suche/historie")[1]["searches"]) == 1
    code, r = call(port, "POST", "/api/config", {"search_history": "off"})
    assert code == 200 and r["config"]["search_history"] == "off"
    h = call(port, "GET", "/api/suche/historie")[1]
    assert h["searches"] == [] and h["retention"] == "off"
    _treffer(welt, "Urlaub")
    assert call(port, "GET", "/api/suche/historie")[1]["searches"] == []
    # an unknown value is ignored, the known ones stick
    call(port, "POST", "/api/config", {"search_history": "sometimes"})
    assert welt["app"].cfg["search_history"] == "off"
    call(port, "POST", "/api/config", {"search_history": "forever"})
    assert welt["app"].cfg["search_history"] == "forever"
    assert app_mod.historie_tage(welt["app"].cfg) is None
    assert app_mod.historie_tage({"search_history": "off"}) == 0
    assert app_mod.historie_tage({"search_history": "365"}) == 365
    assert app_mod.historie_tage({"search_history": "kaputt"}) == 90


def test_historie_wird_beim_start_aufgeraeumt(sandbox, with_ollama):  # noqa: F811
    buch = faelle.Fallbuch(sandbox / faelle.DB_NAME)
    buch.suche_merken(faelle.kriterien({"q": "alt"}), 1)
    import sqlite3
    con = sqlite3.connect(buch.pfad)
    con.execute("UPDATE suchen SET wann = '2020-01-01T00:00:00+00:00'")
    con.commit()
    con.close()
    buch.suche_merken(faelle.kriterien({"q": "neu"}), 1)
    app_mod.App(app_mod.load_config())          # prunes to 90 days
    assert [s["kriterien"]["q"] for s in buch.suchen()] == ["neu"]


# --------------------------------------------------------------------------
# Saved searches
# --------------------------------------------------------------------------
def test_gespeicherte_suche_speichern_umbenennen_laufen_loeschen(welt):
    port = welt["port"]
    code, r = call(port, "POST", "/api/suche/speichern",
                   {"name": "Rechnungen", "kriterien": {"q": "Rechnung", "mode": "lexical", "source": "outlook"}})
    assert code == 200 and r["ok"] and r["search"]["name"] == "Rechnungen"
    sid = r["id"]
    assert r["search"]["kriterien"]["mode"] == "text" and r["search"]["zuletzt"] is None
    code, r = call(port, "POST", "/api/suche/speichern", {"name": "  ", "kriterien": {}})
    assert code == 400 and r["message"]["k"] == "srv.case.noname"
    liste = call(port, "GET", "/api/suche/gespeichert")[1]["searches"]
    assert [g["name"] for g in liste] == ["Rechnungen"]
    # running it records when and how many – on the first page only
    r = _treffer(welt, "Rechnung", source="outlook", saved=str(sid))
    g = call(port, "GET", "/api/suche/gespeichert")[1]["searches"][0]
    assert g["zuletzt"] and g["treffer"] == r["count"]
    code, r = call(port, "POST", "/api/suche/umbenennen", {"id": sid, "name": "Alle Rechnungen"})
    assert code == 200 and r["ok"]
    code, r = call(port, "POST", "/api/suche/umbenennen", {"id": 999, "name": "x"})
    assert code == 404 and r["message"]["k"] == "srv.search.unknown"
    assert call(port, "GET", "/api/suche/gespeichert")[1]["searches"][0]["name"] == "Alle Rechnungen"
    code, r = call(port, "POST", "/api/suche/loeschen", {"id": sid})
    assert code == 200 and r["ok"]
    assert call(port, "GET", "/api/suche/gespeichert")[1]["searches"] == []


def test_gespeicherte_suche_an_fall_haengen_und_loesen(welt):
    port = welt["port"]
    fid = _fall(welt)
    sid = call(port, "POST", "/api/suche/speichern",
               {"name": "Rechnungen", "kriterien": {"q": "Rechnung"}, "fall": fid})[1]["id"]
    g = call(port, "GET", "/api/suche/gespeichert")[1]["searches"][0]
    assert g["fall"] == fid and g["fall_name"] == "Nordwind"
    fall = call(port, "GET", f"/api/faelle/fall?id={fid}")[1]["case"]
    assert [s["id"] for s in fall["suchen_liste"]] == [sid] and fall["suchen"] == 1
    code, r = call(port, "POST", "/api/suche/anhaengen", {"id": sid, "fall": None})
    assert code == 200 and r["ok"]
    assert call(port, "GET", "/api/suche/gespeichert")[1]["searches"][0]["fall"] is None
    code, r = call(port, "POST", "/api/suche/anhaengen", {"id": sid, "fall": 999})
    assert code == 404 and r["message"]["k"] == "srv.case.unknown"
    call(port, "POST", "/api/faelle/schliessen", {"id": fid})
    code, r = call(port, "POST", "/api/suche/anhaengen", {"id": sid, "fall": fid})
    assert code == 409 and r["message"]["k"] == "srv.case.closed"
    code, r = call(port, "POST", "/api/suche/speichern", {"name": "x", "kriterien": {}, "fall": fid})
    assert code == 409


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------
def test_fall_anlegen_aendern_schliessen_oeffnen_loeschen(welt):
    port = welt["port"]
    code, r = call(port, "POST", "/api/faelle/anlegen", {"name": "  "})
    assert code == 400 and r["message"]["k"] == "srv.case.noname"
    fid = _fall(welt, "Nordwind", "Alles zur Rechnung")
    code, r = call(port, "GET", "/api/faelle")
    assert code == 200 and [f["name"] for f in r["cases"]] == ["Nordwind"]
    assert r["cases"][0]["status"] == "offen" and r["cases"][0]["eintraege"] == 0
    assert r["export_dir"] == str(welt["sandbox"] / "exporte")
    code, r = call(port, "POST", "/api/faelle/aendern", {"id": fid, "name": "Nordwind 2026"})
    assert code == 200 and r["case"]["name"] == "Nordwind 2026" and r["case"]["beschreibung"] == "Alles zur Rechnung"
    code, r = call(port, "POST", "/api/faelle/aendern", {"id": fid, "beschreibung": ""})
    assert r["case"]["beschreibung"] == ""
    code, r = call(port, "POST", "/api/faelle/aendern", {"id": fid, "name": ""})
    assert code == 400
    code, r = call(port, "POST", "/api/faelle/schliessen", {"id": fid})
    assert code == 200 and r["case"]["status"] == "zu" and r["case"]["geschlossen"]
    code, r = call(port, "POST", "/api/faelle/aendern", {"id": fid, "name": "x"})
    assert code == 409 and r["message"]["k"] == "srv.case.closed"
    code, r = call(port, "POST", "/api/faelle/schliessen", {"id": fid})
    assert code == 409
    code, r = call(port, "POST", "/api/faelle/oeffnen", {"id": fid})
    assert code == 200 and r["case"]["status"] == "offen" and r["case"]["geschlossen"] is None
    code, r = call(port, "GET", "/api/faelle/fall?id=999")
    assert code == 200 and r["error"]["k"] == "srv.case.unknown"
    code, r = call(port, "POST", "/api/faelle/aendern", {"id": 999, "name": "x"})
    assert code == 404
    code, r = call(port, "POST", "/api/faelle/loeschen", {"id": fid})
    assert code == 200 and r["ok"]
    assert call(port, "GET", "/api/faelle")[1]["cases"] == []
    assert call(port, "POST", "/api/faelle/loeschen", {"id": fid})[0] == 404


def test_treffer_in_den_fall_und_wieder_heraus(welt):
    port = welt["port"]
    fid = _fall(welt)
    hits = _treffer(welt, "Rechnung")["results"]
    assert hits and all(h["key"] for h in hits) and all(h["cases"] == [] for h in hits)
    mail = next(h for h in hits if h["source"] == "outlook")
    code, r = call(port, "POST", "/api/faelle/hinzufuegen",
                   {"id": fid, "eintraege": [_eintrag(mail), {"key": "", "titel": "ohne"}, "kein dict"]})
    assert code == 200 and r == {"ok": True, "added": 1, "already": 0}
    code, r = call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(mail)]})
    assert r == {"ok": True, "added": 0, "already": 1}
    # the hit now wears the mark, the case holds what the hit said
    mail2 = next(h for h in _treffer(welt, "Rechnung")["results"] if h["source"] == "outlook")
    assert mail2["cases"] == [{"id": fid, "name": "Nordwind", "status": "offen"}]
    fall = call(port, "GET", f"/api/faelle/fall?id={fid}")[1]["case"]
    assert fall["eintraege"] == 1 and fall["je_quelle"] == {"outlook": 1}
    e = fall["eintraege_liste"][0]
    assert e["key"] == mail["key"] and e["titel"] == "Rechnung 4711 freigegeben"
    assert e["rel"] == "inbox/mail1.eml" and e["root"] == "outlook" and e["liste"] is None
    code, r = call(port, "POST", "/api/faelle/entfernen", {"id": fid, "key": mail["key"]})
    assert code == 200 and r["case"]["eintraege"] == 0
    call(port, "POST", "/api/faelle/schliessen", {"id": fid})
    code, r = call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(mail)]})
    assert code == 409 and r["message"]["k"] == "srv.case.closed"


def test_der_fallfilter_sucht_nur_im_fall(welt):
    port = welt["port"]
    fid = _fall(welt)
    hits = _treffer(welt, "")["results"]                    # browse: everything
    assert len(hits) >= 3
    mail = next(h for h in hits if h["path"] == "inbox/mail1.eml")
    call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(mail)]})
    im_fall = _treffer(welt, "", case=str(fid))["results"]
    assert [h["path"] for h in im_fall] == ["inbox/mail1.eml"]
    assert [h["uid"] for h in _treffer(welt, "Rechnung", case=str(fid))["results"]] == [mail["uid"]]
    assert _treffer(welt, "Urlaub", case=str(fid))["results"] == []
    r = _treffer(welt, "Rechnung", case="999")
    assert r["results"] == [] and "No case named" in r["error"]
    # the history keeps the case as a criterion
    h = call(port, "GET", "/api/suche/historie")[1]["searches"]
    assert h[0]["kriterien"]["fall"] == fid


def test_ergebnisliste_wird_festgehalten(welt):
    port = welt["port"]
    fid = _fall(welt)
    alle = _treffer(welt, "")["count"]
    code, r = call(port, "POST", "/api/faelle/liste",
                   {"id": fid, "kriterien": {"q": "", "source": "outlook"}})
    assert code == 200 and r["ok"] and r["hits"] == 2 and r["added"] == 2
    lid = r["liste"]
    fall = call(port, "GET", f"/api/faelle/fall?id={fid}")[1]["case"]
    assert fall["eintraege"] == 2 and fall["listen"] == 1
    li = fall["listen_liste"][0]
    assert li["id"] == lid and li["anzahl"] == 2 and li["kriterien"]["source"] == "outlook"
    assert all(e["liste"] == lid for e in fall["eintraege_liste"])
    # a second list with words: hits already there are not new
    code, r2 = call(port, "POST", "/api/faelle/liste", {"id": fid, "kriterien": {"q": "Rechnung", "mode": "text"}})
    assert r2["ok"] and r2["hits"] >= 1 and r2["added"] == r2["hits"] - 1
    assert r2["hits"] <= alle
    # dropping the first list takes its two mails along; what the second
    # list brought stays
    code, r = call(port, "POST", "/api/faelle/liste-loeschen", {"id": fid, "liste": lid})
    assert code == 200 and r["case"]["listen"] == 1 and r["case"]["eintraege"] == r2["added"]
    # a bad search is refused, nothing stored
    code, r = call(port, "POST", "/api/faelle/liste", {"id": fid, "kriterien": {"q": "x", "fall": 999}})
    assert code == 409 and "No case named" in r["message"]
    assert call(port, "GET", f"/api/faelle/fall?id={fid}")[1]["case"]["listen"] == 1


def test_alle_treffer_blaettert_durch_die_seiten(welt, monkeypatch):
    """A list of thousands: the server pages through the engine itself."""
    seiten = []

    class FakeSuche:
        STATE = {"semantic": False}

        @staticmethod
        def search_messages(query, mode, k, offset, **kw):
            seiten.append(offset)
            n = min(k, 250 - offset)
            return {"count": n, "results": [{"key": f"k{offset + i}", "uid": f"u{offset + i}"} for i in range(n)]}

    monkeypatch.setattr(welt["app"].search, "ensure", lambda cfg: FakeSuche)
    handler = app_mod.Handler.__new__(app_mod.Handler)
    handler.app = welt["app"]
    treffer, fehler = handler._alle_treffer(faelle.kriterien({"q": "x"}))
    assert fehler is None and len(treffer) == 250 and seiten == [0, 100, 200]
    treffer, fehler = handler._alle_treffer(faelle.kriterien({"q": "x"}), grenze=150)
    assert len(treffer) == 150


def test_notizen_im_fallbuch(welt):
    port = welt["port"]
    fid = _fall(welt)
    code, r = call(port, "POST", "/api/faelle/notiz", {"id": fid, "text": " Erste Notiz "})
    assert code == 200 and r["ok"] and r["id"]
    nid = r["id"]
    code, r = call(port, "POST", "/api/faelle/notiz", {"id": fid, "text": "  "})
    assert code == 400 and r["message"]["k"] == "srv.case.noname"
    code, r = call(port, "POST", "/api/faelle/notiz-aendern", {"id": fid, "notiz": nid, "text": "Geändert"})
    assert code == 200 and [n["text"] for n in r["case"]["notizen_liste"]] == ["Geändert"]
    code, r = call(port, "POST", "/api/faelle/notiz-loeschen", {"id": fid, "notiz": nid})
    assert code == 200 and r["case"]["notizen_liste"] == [] and r["case"]["notizen"] == 0
    call(port, "POST", "/api/faelle/schliessen", {"id": fid})
    assert call(port, "POST", "/api/faelle/notiz", {"id": fid, "text": "spät"})[0] == 409


def test_neue_treffer_der_angehaengten_suchen(welt):
    port = welt["port"]
    fid = _fall(welt)
    hits = _treffer(welt, "")["results"]
    mail1 = next(h for h in hits if h["path"] == "inbox/mail1.eml")
    call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(mail1)]})
    call(port, "POST", "/api/suche/speichern",
         {"name": "Alle Mails", "kriterien": {"q": "", "source": "outlook"}, "fall": fid})
    call(port, "POST", "/api/suche/speichern",
         {"name": "Kaputt", "kriterien": {"q": "x", "fall": 999}, "fall": fid})
    code, r = call(port, "GET", f"/api/faelle/neu?id={fid}")
    assert code == 200 and r["case"]["name"] == "Nordwind"
    bloecke = {b["name"]: b for b in r["searches"]}
    assert [h["path"] for h in bloecke["Alle Mails"]["new"]] == ["inbox/mail2.eml"]
    assert bloecke["Alle Mails"]["new_count"] == 1
    assert "No case named" in bloecke["Kaputt"]["error"] and bloecke["Kaputt"]["new"] == []
    assert call(port, "GET", "/api/faelle/neu?id=999")[1]["error"]["k"] == "srv.case.unknown"


def test_fall_loeschen_loest_suchen_und_nimmt_die_marke(welt):
    port = welt["port"]
    fid = _fall(welt)
    hits = _treffer(welt, "Rechnung")["results"]
    call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(hits[0])]})
    sid = call(port, "POST", "/api/suche/speichern", {"name": "S", "kriterien": {"q": "R"}, "fall": fid})[1]["id"]
    call(port, "POST", "/api/faelle/loeschen", {"id": fid})
    g = call(port, "GET", "/api/suche/gespeichert")[1]["searches"]
    assert [x["id"] for x in g] == [sid] and g[0]["fall"] is None
    assert all(h["cases"] == [] for h in _treffer(welt, "Rechnung")["results"])


# --------------------------------------------------------------------------
# The export as a run
# --------------------------------------------------------------------------
def test_export_ist_ein_lauf_und_schreibt_den_ordner(welt, monkeypatch):
    a, port = welt["app"], welt["port"]
    fid = _fall(welt, "Nordwind", "Alles")
    hits = _treffer(welt, "")["results"]
    call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(h) for h in hits]})
    call(port, "POST", "/api/faelle/notiz", {"id": fid, "text": "Notiz"})
    code, r = call(port, "POST", "/api/faelle/export", {"id": fid, "zip": True})
    assert code == 200 and r["ok"] and r["message"] is None, r
    ziel = r["path"]
    assert ziel.startswith(str(welt["sandbox"] / "exporte")) and "Nordwind" in ziel
    assert a.jobs.busy
    _warte(a.jobs, 60)
    assert a.jobs.last["ok"], "\n".join(str(z.get("text")) for z in a.jobs.lines)
    ordner = welt["sandbox"] / "exporte"
    assert (ordner / ziel.rsplit("/", 1)[1] / "index.html").exists()
    assert (ordner / ziel.rsplit("/", 1)[1] / "Outlook" / "inbox" / "mail1.eml").exists()
    assert (ordner / ziel.rsplit("/", 1)[1] / "Teams" / "1on1" / "alice__abc.html").exists()
    assert (ordner / (ziel.rsplit("/", 1)[1] + ".zip")).exists()
    log = "\n".join(json.dumps(z, ensure_ascii=False) for z in a.jobs.lines)
    assert "run.case.start" in log and "run.case.done" in log
    fall = call(port, "GET", f"/api/faelle/fall?id={fid}")[1]["case"]
    assert fall["exportiert"].endswith(".zip") and fall["exportiert_wann"]
    # "Show folder": the zip's folder, through the file manager
    geoeffnet = []
    monkeypatch.setattr(app_mod.archive_check, "ordner_oeffnen", lambda p: geoeffnet.append(str(p)) or True)
    code, r = call(port, "POST", "/api/faelle/export-ordner", {"id": fid})
    assert code == 200 and r["ok"] and geoeffnet == [str(ordner)]
    # a closed case can be exported as well
    call(port, "POST", "/api/faelle/schliessen", {"id": fid})
    code, r = call(port, "POST", "/api/faelle/export", {"id": fid})
    assert code == 200 and r["ok"]
    _warte(a.jobs, 60)
    assert a.jobs.last["ok"]


def test_export_verweigert_waehrend_eines_laufs_und_ohne_fall(welt, monkeypatch):
    a, port = welt["app"], welt["port"]
    fid = _fall(welt)
    monkeypatch.setattr(a.jobs, "start", lambda steps, label, **kw: True)
    code, r = call(port, "POST", "/api/faelle/export", {"id": 999})
    assert code == 404 and r["message"]["k"] == "srv.case.unknown"
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.update(steps=steps, label=label) or True)
    code, r = call(port, "POST", "/api/faelle/export", {"id": fid, "zip": False})
    assert code == 200 and r["ok"]
    (schritt,) = gesehen["steps"]
    assert schritt["key"] == "fall_export" and gesehen["label"] == "job.case_export"
    argv = schritt["argv"]
    assert argv[1].endswith("case_export.py") and "--zip" not in argv
    assert argv[argv.index("--fall") + 1] == str(fid)
    assert argv[argv.index("--ziel") + 1] == r["path"]
    assert argv[argv.index("--faelle") + 1] == str(welt["sandbox"] / faelle.DB_NAME)
    assert "--lang" in argv and "--res" in argv
    assert schritt["env"]["MUNIMENTUM_HOME"] == str(welt["sandbox"])
    monkeypatch.setattr(type(a.jobs), "busy", property(lambda self: True))
    code, r = call(port, "POST", "/api/faelle/export", {"id": fid})
    assert code == 409 and r["message"]["k"] == "srv.busy"


def test_export_ordner_ohne_export(welt, monkeypatch):
    port = welt["port"]
    fid = _fall(welt)
    code, r = call(port, "POST", "/api/faelle/export-ordner", {"id": fid})
    assert code == 404 and r["message"]["k"] == "srv.case.noexport"
    (welt["sandbox"] / "exporte").mkdir()
    geoeffnet = []
    monkeypatch.setattr(app_mod.archive_check, "ordner_oeffnen", lambda p: geoeffnet.append(str(p)) or True)
    code, r = call(port, "POST", "/api/faelle/export-ordner", {"id": fid})
    assert code == 200 and geoeffnet == [str(welt["sandbox"] / "exporte")]


def test_export_basis(tmp_path, monkeypatch):
    assert app_mod.fall_export_basis({"case_export_dir": str(tmp_path / "x")}) == tmp_path / "x"
    monkeypatch.setattr(app_mod.Path, "home", classmethod(lambda cls: tmp_path))
    assert app_mod.fall_export_basis({}) == tmp_path / "Munimentum cases"
    (tmp_path / "Documents").mkdir()
    assert app_mod.fall_export_basis({"case_export_dir": ""}) == tmp_path / "Documents" / "Munimentum cases"
    assert case_export.zielordner(tmp_path, "N").parent == tmp_path


def test_einstellungen_fuer_faelle_werden_gespeichert(welt):
    port = welt["port"]
    code, r = call(port, "POST", "/api/config",
                   {"case_export_dir": "  /tmp/x  ", "mcp_cases_write": True, "search_history": "30"})
    cfg = r["config"]
    assert cfg["case_export_dir"] == "/tmp/x" and cfg["mcp_cases_write"] is True and cfg["search_history"] == "30"
    code, r = call(port, "POST", "/api/config", {"case_export_dir": "", "mcp_cases_write": False})
    assert r["config"]["case_export_dir"] == "" and r["config"]["mcp_cases_write"] is False
    # the MCP server reads the switch from the same file
    import settings
    assert settings.flag("MCP_CASES_WRITE", "mcp_cases_write") is False


def test_die_suchbruecke_kennt_das_fallbuch(welt):
    mod = welt["app"].search.ensure(welt["app"].cfg)
    assert mod.STATE["faelle_db"] == str(welt["sandbox"] / faelle.DB_NAME)
    assert mod.STATE["cases_write"] is False


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------
def test_die_dritte_tuer_und_ihre_teile_stehen_im_markup():
    seite = app_mod.seite()
    nav = seite[seite.index("<nav>"):seite.index("</nav>")]
    haupt = nav[:nav.index('class="nav-neben"')]
    assert 'data-tab="faelle"' in haupt, "Cases is a door, not a side room"
    assert 'id="tab-faelle"' in seite and 'id="f-fall"' in seite
    # the two windows sit next to the search button, in the search row
    zeile = seite[seite.index('class="suchzeile"'):seite.index('id="modus-fehlt"')]
    assert 'onclick="historieFenster()"' in zeile and 'onclick="gespeicherteFenster()"' in zeile
    # the cases tab has one primary action
    block = seite[seite.index('<section id="tab-faelle"'):seite.index('<section id="tab-analytics"')]
    assert block.count('class="act') == 1 and 'fallNeuFenster()' in block
    # the case filter counts but does not search, like every filter – and
    # starts hidden: it appears with the first case
    feld = re.search(r'<select id="f-fall"[^>]*>', seite).group(0)
    assert "zeigeFilterstand()" in feld and "doSearch" not in feld and 'class="hide"' in feld
    # every setting row has its (i)
    for key in ("search_history", "case_export_dir", "mcp_cases_write"):
        assert f'data-i18n-title="settings.{key}.i"' in seite and f'id="c-{key}"' in seite


PRUEFUNG_SEITE = GRUNDZUSTAND + """
var anfragen = [];
var FAELLE_ANTWORT = {cases: [
  {id: 1, name: 'Nordwind', status: 'offen', eintraege: 3, je_quelle: {outlook: 2, teams: 1}, listen: 0, notizen: 1, suchen: 0,
   angelegt: '2026-09-01T10:00:00+00:00', geaendert: '2026-09-14T10:00:00+00:00', geschlossen: null},
  {id: 2, name: 'Alt', status: 'zu', eintraege: 1, je_quelle: {outlook: 1}, listen: 0, notizen: 0, suchen: 0,
   angelegt: '2026-01-01T10:00:00+00:00', geaendert: '2026-02-01T10:00:00+00:00', geschlossen: '2026-02-01T10:00:00+00:00'}
 ], export_dir: '/tmp/exporte'};
var TREFFER_ANTWORT = {count: 2, results: [
  {uid: 'outlook:inbox/mail1.eml:0', key: 'mail:<m1@example.com>', source: 'outlook', root: 'outlook',
   path: 'inbox/mail1.eml', title: 'Rechnung 4711', date: '2025-06-10', who: 'Carla Chef', preview: 'Die Rechnung',
   cases: [{id: 1, name: 'Nordwind', status: 'offen'}]},
  {uid: 'teams:1on1/alice__abc.html:0', key: null, source: 'teams', root: 'teams',
   path: '1on1/alice__abc.html', title: 'Projekt Alpha', date: '2025-06-01', who: 'Alice', preview: 'Hallo', cases: []}
]};
global.fetch = function(pfad, opt){
  anfragen.push({pfad: String(pfad), body: opt && opt.body ? JSON.parse(opt.body) : null});
  var antwort = statusGeruest();
  if(String(pfad).indexOf('/api/faelle/fall?') === 0) antwort = {case: Object.assign({eintraege_liste: [
      {key: 'mail:<m1@example.com>', src: 'outlook', root: 'outlook', rel: 'inbox/mail1.eml', titel: 'Rechnung 4711', datum: '2025-06-10', wer: 'Carla Chef', liste: null},
      {key: 'teams:x#1', src: 'teams', root: 'teams', rel: '1on1/alice__abc.html', titel: 'Projekt Alpha', datum: '2025-06-01', wer: 'Alice', liste: null}],
    listen_liste: [], notizen_liste: [{id: 7, wann: '2026-09-14T10:00:00+00:00', text: 'Erste Notiz'}],
    suchen_liste: [{id: 5, name: 'Rechnungen', kriterien: {q: 'Rechnung', mode: 'text', person: '', source: 'outlook', from: '', to: '', folder: '', filetype: '', gone: false, fall: null}, zuletzt: null, treffer: null, fall: 1, fall_name: 'Nordwind'}]},
    FAELLE_ANTWORT.cases[String(pfad).indexOf('id=2') > 0 ? 1 : 0])};
  else if(String(pfad) === '/api/faelle') antwort = FAELLE_ANTWORT;
  else if(String(pfad).indexOf('/api/search?') === 0) antwort = TREFFER_ANTWORT;
  else if(String(pfad) === '/api/suche/historie') antwort = {retention: '90', searches: [
      {id: 1, wann: new Date().toISOString(), treffer: 4, kriterien: {q: 'Rechnung', mode: 'ki', person: 'Alice', source: 'outlook', from: '', to: '', folder: '', filetype: '', gone: false, fall: null}},
      {id: 2, wann: '2026-01-05T10:00:00+00:00', treffer: 0, kriterien: {q: '', mode: 'text', person: '', source: 'all', from: '2025-01-01', to: '', folder: '', filetype: 'pdf', gone: true, fall: 1}}]};
  else if(String(pfad) === '/api/suche/gespeichert') antwort = {searches: [
      {id: 5, name: 'Rechnungen', kriterien: {q: 'Rechnung', mode: 'text', person: '', source: 'outlook', from: '', to: '', folder: '', filetype: '', gone: false, fall: null}, zuletzt: '2026-09-14T10:00:00+00:00', treffer: 4, fall: 1, fall_name: 'Nordwind'},
      {id: 6, name: 'Lose', kriterien: {q: 'x', mode: 'text', person: '', source: 'all', from: '', to: '', folder: '', filetype: '', gone: false, fall: null}, zuletzt: null, treffer: null, fall: null, fall_name: null}]};
  else if(opt && opt.method === 'POST') antwort = {ok: true, id: 9, added: 2, already: 0, hits: 2, case: FAELLE_ANTWORT.cases[0]};
  return Promise.resolve({json: function(){ return Promise.resolve(antwort); }});
};
global.confirm = function(){ return true; };
global.prompt = function(text, wert){ return 'Neu benannt'; };
function warte(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
function letzte(pfad){ return anfragen.filter(function(a){ return a.pfad.indexOf(pfad) === 0; }).pop(); }

(async function(){
  renderStatus(statusGeruest());
  // no case yet: the filter is not offered at all
  FAELLE = []; fuelleFallFilter();
  pruefe(el('f-fall').classList.contains('hide'), 'Fallfilter ohne Faelle sichtbar');
  await ladeFaelle();
  pruefe(FAELLE.length === 2, 'Faelle nicht geladen');
  pruefe(!el('f-fall').classList.contains('hide'), 'Fallfilter trotz Faellen versteckt');
  // the filter lists every case, the closed one marked
  var optionen = el('f-fall').innerHTML;
  pruefe(optionen.indexOf('value="1">Nordwind<') >= 0, 'offener Fall fehlt im Filter: ' + optionen);
  pruefe(optionen.indexOf('value="2">Alt · Geschlossen<') >= 0, 'geschlossener Fall nicht markiert: ' + optionen);

  // --- filters: the case counts, clears, sends
  el('f-fall').value = '1';
  pruefe(filterFelder().indexOf('1') >= 0, 'Fallfilter zaehlt nicht');
  zeigeFilterstand();
  pruefe(el('f-fall').classList.contains('on'), 'Fallfilter leuchtet nicht');
  pruefe(!el('filter-weg').classList.contains('hide'), 'Zuruecksetzen fehlt');
  el('q').value = 'Rechnung';
  doSearch(0, 5);
  var such = letzte('/api/search?');
  pruefe(such && such.pfad.indexOf('case=1') > 0, 'case fehlt in der Anfrage: ' + (such && such.pfad));
  pruefe(such.pfad.indexOf('saved=5') > 0, 'saved fehlt in der Anfrage');
  await warte(5);
  filterLeeren();
  pruefe(el('f-fall').value === '', 'Zuruecksetzen leert den Fallfilter nicht');
  doSearch(20);
  pruefe(letzte('/api/search?').pfad.indexOf('saved=') < 0 || letzte('/api/search?').pfad.indexOf('saved=&') > 0, 'saved auf Seite 2');
  await warte(5);

  // --- hits: tick, mark, head
  var liste = el('results').innerHTML;
  pruefe(liste.indexOf('class="wahl"') >= 0, 'kein Haken am Treffer');
  pruefe(liste.indexOf('im-fall') >= 0 && liste.indexOf('Nordwind') >= 0, 'keine Fallmarke am Treffer');
  pruefe(liste.indexOf('disabled title="') >= 0, 'Treffer ohne Schluessel nicht ausgegraut');
  pruefe(!el('treffer-kopf').classList.contains('hide'), 'Listenkopf fehlt');
  pruefe(el('alle-in-fall').textContent.length > 3 && el('treffer-stand').textContent.indexOf('2') >= 0, 'Listenkopf unvollstaendig: ' + el('treffer-stand').textContent);
  pruefe(el('auswahl-leiste').classList.contains('hide'), 'Auswahlleiste ohne Auswahl sichtbar');
  trefferWahl(0, true);
  pruefe(!el('auswahl-leiste').classList.contains('hide') && el('auswahl-zahl').textContent.indexOf('1') >= 0, 'Auswahl nicht gezaehlt');
  trefferWahl(1, true);                     // no key: cannot be picked
  pruefe(Object.keys(AUSWAHL).length === 1, 'Treffer ohne Schluessel wurde gewaehlt');
  // detail: the mark and the button
  zeigeDetail(TREFFER[0], 1);
  var detail = el('detail-inhalt').innerHTML;
  pruefe(detail.indexOf("fallWahl('einer', 0)") >= 0, 'Kein "In einen Fall" am Detail');
  pruefe(detail.indexOf('im-fall') >= 0, 'Keine Marke am Detail');
  zeigeDetail(TREFFER[1], 2);
  pruefe(el('detail-inhalt').innerHTML.indexOf('fallWahl(') < 0, 'Treffer ohne Schluessel bietet den Fall an');

  // --- the choice window: open cases only, one primary button, the post
  fallWahl('auswahl');
  await warte(5);
  var html = modal.innerHTML;
  pruefe(html.indexOf('value="1"') >= 0 && html.indexOf('value="2"') < 0, 'geschlossener Fall im Auswahlfenster');
  pruefe(html.split('class="act"').length - 1 === 1, 'nicht genau ein primaerer Knopf');
  pruefe(html.indexOf('class="ghost"') < 0, 'ein zweiter Knopf, der nur schliesst');
  pruefe(html.indexOf('1 Treffer') >= 0 || html.indexOf('1 hits') >= 0 || html.indexOf('1 ') >= 0, 'Titel nennt die Zahl nicht');
  fallWahlAusfuehren();
  await warte(10);
  var hinzu = letzte('/api/faelle/hinzufuegen');
  pruefe(hinzu && hinzu.body.id === 1, 'nichts an den Fall geschickt');
  pruefe(hinzu.body.eintraege.length === 1 && hinzu.body.eintraege[0].key === 'mail:<m1@example.com>', 'falscher Eintrag');
  pruefe(hinzu.body.eintraege[0].rel === 'inbox/mail1.eml' && hinzu.body.eintraege[0].titel === 'Rechnung 4711', 'Eintrag ohne Pfad/Titel');
  pruefe(Object.keys(AUSWAHL).length === 0, 'Auswahl nach dem Hinzufuegen nicht geleert');
  pruefe(!el('treffer-meldung').classList.contains('hide'), 'keine Rueckmeldung');
  // the whole list: the criteria go, plus the attached search when asked
  el('q').value = 'Rechnung';
  fallWahl('liste');
  await warte(5);
  pruefe(modal.innerHTML.indexOf('fall-wahl-anhaengen') >= 0, 'kein Anhaengen-Schalter bei der Liste');
  fallWahlAusfuehren();
  await warte(10);
  var li = letzte('/api/faelle/liste');
  pruefe(li && li.body.kriterien.q === 'Rechnung' && li.body.kriterien.mode === 'text', 'Liste ohne Kriterien: ' + JSON.stringify(li && li.body));
  pruefe(TREFFER[0].cases.length === 1, 'Marke doppelt gesetzt');

  // --- stored criteria set the form without searching (the status above
  // greyed the two rear modes: no Ollama – a stored mode needs them on)
  el('m-aehnlich').disabled = false; el('m-ki').disabled = false;
  var vorher = anfragen.length;
  kriterienAnwenden({q: 'Urlaub', mode: 'aehnlich', person: 'Alice', source: 'outlook', from: '2025-01-01', to: '', folder: 'inbox/x', filetype: 'pdf', gone: true, fall: 2});
  pruefe(anfragen.length === vorher, 'kriterienAnwenden hat gesucht');
  pruefe(el('q').value === 'Urlaub' && el('f-person').value === 'Alice' && el('f-source').value === 'outlook', 'Felder nicht gesetzt');
  pruefe(el('f-from').value === '2025-01-01' && el('f-folder').value === 'inbox/x' && el('f-typ').value === 'pdf', 'Felder nicht gesetzt (2)');
  pruefe(el('f-gone').checked === true && el('f-fall').value === '2', 'Felder nicht gesetzt (3)');
  pruefe(SUCHMODUS === 'aehnlich' && el('m-aehnlich').classList.contains('on'), 'Suchart nicht gesetzt');
  var k = kriterienAusForm();
  pruefe(k.fall === 2 && k.gone === true && k.mode === 'aehnlich' && k.folder === 'inbox/x', 'Kriterien aus dem Formular falsch: ' + JSON.stringify(k));
  el('m-ki').disabled = true;
  kriterienAnwenden({q: 'x', mode: 'ki'});
  pruefe(SUCHMODUS === 'text', 'abgeschaltete Suchart wurde gesetzt');
  filterLeeren();

  // --- history window
  historieFenster();
  await warte(10);
  html = modal.innerHTML;
  pruefe(html.indexOf('Rechnung') >= 0 && html.indexOf('historieLauf(0)') >= 0, 'Verlauf nicht gezeichnet');
  pruefe(html.indexOf('Person · Alice') >= 0 && html.indexOf('.pdf') >= 0, 'Kriterien-Tags fehlen');
  pruefe(html.indexOf('Fall · Nordwind') >= 0, 'Fallkriterium ohne Namen');
  pruefe(html.indexOf('historieLeeren()') >= 0, 'Verlauf leeren fehlt');
  pruefe(html.indexOf('class="act"') < 0, 'Listenfenster mit primaerem Knopf');
  historieLauf(1);
  await warte(5);
  pruefe(el('f-typ').value === 'pdf' && el('f-fall').value === '1' && el('f-gone').checked, 'Verlaufseintrag nicht angewandt');
  pruefe(letzte('/api/search?').pfad.indexOf('case=1') > 0, 'Verlaufssuche ohne Fall');
  historieFenster();
  await warte(10);
  historieLeeren();
  await warte(5);
  pruefe(letzte('/api/suche/historie-leeren'), 'Leeren nicht geschickt');

  // --- saved searches window
  gespeicherteFenster();
  await warte(10);
  html = modal.innerHTML;
  pruefe(html.indexOf('Rechnungen') >= 0 && html.indexOf('gespeichertLauf(0)') >= 0, 'gespeicherte nicht gezeichnet');
  pruefe(html.indexOf('gespeichertLoesen(0)') >= 0 && html.indexOf('gespeichertAnhaengen(1)') >= 0, 'Anhaengen/Loesen fehlen');
  pruefe(html.indexOf('Nordwind') >= 0, 'Fallname fehlt');
  gespeichertLauf(0);
  await warte(5);
  pruefe(letzte('/api/search?').pfad.indexOf('saved=5') > 0, 'gespeicherte Suche laeuft ohne ihre Nummer');
  gespeicherteFenster();
  await warte(10);
  gespeichertUmbenennen(1);
  await warte(5);
  pruefe(letzte('/api/suche/umbenennen').body.name === 'Neu benannt', 'Umbenennen nicht geschickt');
  gespeichertLoeschen(1);
  await warte(5);
  pruefe(letzte('/api/suche/loeschen').body.id === 6, 'Loeschen nicht geschickt');
  // save the current search, attached to a case
  el('q').value = 'Neu';
  speichernFenster(kriterienAusForm(), 'gespeichert');
  html = modal.innerHTML;
  pruefe(html.indexOf('id="speichern-name"') >= 0 && html.indexOf('id="speichern-fall"') >= 0, 'Speicherfenster unvollstaendig');
  pruefe(html.indexOf('value="2"') < 0, 'geschlossener Fall im Speicherfenster');
  document.getElementById('speichern-name').value = 'Meine';
  speichernAusfuehren();
  await warte(10);
  var sp = letzte('/api/suche/speichern');
  pruefe(sp && sp.body.name === 'Meine' && sp.body.kriterien.q === 'Neu', 'Speichern nicht geschickt: ' + JSON.stringify(sp && sp.body));

  // --- the cases tab
  tab('faelle');
  await warte(10);
  pruefe(el('faelle-liste').innerHTML.indexOf('Nordwind') >= 0, 'Fallliste leer');
  pruefe(el('faelle-liste').innerHTML.indexOf('Alt') < 0, 'geschlossener Fall ohne Schalter sichtbar');
  el('faelle-zu').checked = true;
  zeichneFaelle();
  pruefe(el('faelle-liste').innerHTML.indexOf('Alt') >= 0, 'geschlossener Fall trotz Schalter unsichtbar');
  await fallOeffnen(1);
  html = el('fall-inhalt').innerHTML;
  pruefe(!el('fall-detail').classList.contains('hide'), 'Falldetail bleibt versteckt');
  pruefe(html.indexOf('Erste Notiz') >= 0 && html.indexOf('id="notiz-neu"') >= 0, 'Fallbuch nicht gezeichnet');
  pruefe(html.indexOf('Rechnung 4711') >= 0 && html.indexOf('Projekt Alpha') >= 0, 'Eintraege fehlen');
  pruefe(html.indexOf('/source?root=outlook&amp;path=inbox%2Fmail1.eml') >= 0, 'Eintrag ohne Link ins Original');
  pruefe(html.indexOf('fallEntfernen(') >= 0 && html.indexOf('schliessenFenster()') >= 0, 'Aktionen des offenen Falls fehlen');
  pruefe(html.indexOf('sucheImFall(1)') >= 0 && html.indexOf('exportFenster()') >= 0, 'Suchen/Export fehlen');
  pruefe(html.indexOf('Rechnungen') >= 0 && html.indexOf('fallNeuPruefen()') >= 0, 'angehaengte Suche fehlt');
  pruefe(html.indexOf('fallSchreiben') < 0, 'roher Code im Markup');
  document.getElementById('notiz-neu').value = 'Zweite';
  notizHinzufuegen();
  await warte(10);
  pruefe(letzte('/api/faelle/notiz').body.text === 'Zweite', 'Notiz nicht geschickt');
  await fallOeffnen(2);
  html = el('fall-inhalt').innerHTML;
  pruefe(html.indexOf('fallEntfernen(') < 0 && html.indexOf('id="notiz-neu"') < 0, 'geschlossener Fall laesst aendern');
  pruefe(html.indexOf('fallOeffnenWieder()') >= 0 && html.indexOf('exportFenster()') >= 0, 'Wieder oeffnen/Export fehlen');
  // search in this case: the filter is set, the search fired
  sucheImFall(1);
  await warte(10);
  pruefe(el('f-fall').value === '1' && letzte('/api/search?').pfad.indexOf('case=1') > 0, 'Suche im Fall setzt den Filter nicht');
  // export window: one primary, the zip switch, the folder; the run
  await fallOeffnen(1);
  exportFenster();
  html = modal.innerHTML;
  pruefe(html.split('class="act"').length - 1 === 1 && html.indexOf('fall-export-zip') >= 0, 'Exportfenster unvollstaendig');
  pruefe(html.indexOf('/tmp/exporte') >= 0, 'Exportordner nicht genannt');
  pruefe(html.indexOf('class="zahnrad"') >= 0, 'kein Zahnrad zur Einstellung');
  document.getElementById('fall-export-zip').checked = true;
  fallExportStarten(true);
  await warte(10);
  var ex = letzte('/api/faelle/export');
  pruefe(ex && ex.body.id === 1 && ex.body.zip === true, 'Export nicht geschickt: ' + JSON.stringify(ex && ex.body));
  pruefe(LAUF.eigener === true, 'Der Export oeffnet das Lauffenster nicht');
  // close with export first
  schliessenFenster();
  pruefe(modal.innerHTML.indexOf('fall-zu-export') >= 0, 'Schliessfenster ohne Exportschalter');
  document.getElementById('fall-zu-export').checked = true;
  fallSchliessen();
  await warte(20);
  pruefe(letzte('/api/faelle/schliessen') && letzte('/api/faelle/schliessen').body.id === 1, 'Schliessen nicht geschickt');
  // new case window: one primary, posts, opens the case
  fallNeuFenster();
  pruefe(modal.innerHTML.split('class="act"').length - 1 === 1, 'Neuer-Fall-Fenster ohne genau einen Knopf');
  document.getElementById('fall-name').value = 'Berlin';
  fallFormularSenden();
  await warte(20);
  pruefe(letzte('/api/faelle/anlegen').body.name === 'Berlin', 'Anlegen nicht geschickt');
  fallLoeschen();
  await warte(10);
  pruefe(letzte('/api/faelle/loeschen'), 'Loeschen nicht geschickt');
  pruefe(el('fall-detail').classList.contains('hide'), 'Detail nach dem Loeschen offen');

  // --- settings: the three fields travel with the save
  cfgGefuellt = false;
  fuelleEinstellungen(Object.assign({}, statusGeruest().config, {search_history: '365', case_export_dir: '/x', mcp_cases_write: true}));
  pruefe(el('c-search_history').value === '365' && el('c-case_export_dir').value === '/x' && el('c-mcp_cases_write').checked === true, 'Einstellungen nicht gefuellt');
  speichereEinstellungen();
  await warte(10);
  var cfg = letzte('/api/config').body;
  pruefe(cfg.search_history === '365' && cfg.case_export_dir === '/x' && cfg.mcp_cases_write === true, 'Einstellungen nicht gespeichert: ' + JSON.stringify(cfg));
  console.log('OK');
})().catch(function(e){ console.log('FEHLER ' + (e.stack || e)); process.exit(1); });
"""


def test_die_seite_fuehrt_faelle_durch():
    _in_node(PRUEFUNG_SEITE)


def test_mockup_texte_sind_in_allen_sprachen_da():
    """The (i) texts and buttons of the new door exist in every language,
    and no English slipped into the German file."""
    import i18n
    de, en, fr = (i18n.strings(c) for c in ("de", "en", "fr"))
    for k in ("nav.cases", "cases.i", "cases.add.i", "search.history.i", "search.saved.i",
              "settings.search_history.i", "settings.case_export_dir.i", "settings.mcp_cases_write.i"):
        assert de[k] != en[k] != fr[k]
    assert "Fälle" in de["nav.cases"] and "Cases" in en["nav.cases"] and "Dossiers" in fr["nav.cases"]
    time.sleep(0)


# --------------------------------------------------------------------------
# An index from before 11.0 gets its keys on the next run, whatever the
# exports brought – the skip gate must not hold the index step back.
# --------------------------------------------------------------------------
def test_index_ohne_schluessel_wird_nicht_uebersprungen(sandbox, monkeypatch):  # noqa: F811
    import sqlite3
    import store_layout
    cfg = app_mod.load_config()

    def index_schritt():
        return next(s for s in app_mod.build_steps(cfg, {"index": True}) if s["key"] == "index")

    # no index yet: the target is named, the gate sees it missing
    db = store_layout.db_path(sandbox / app_mod.STORE_DIR)
    assert index_schritt()["nur_bei_neuem"] is True and index_schritt()["ziel"] == db
    assert store_layout.veraltet(db) is False
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE chunks(id INTEGER, uid TEXT, text TEXT)")   # 10.2: no key column
    con.commit()
    con.close()
    assert store_layout.mit_schluesseln(db) is False and store_layout.veraltet(db) is True
    assert index_schritt()["ziel"] is None, "an index without keys must be rebuilt"
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE chunks ADD COLUMN key TEXT")
    con.commit()
    con.close()
    assert store_layout.mit_schluesseln(db) is True
    assert index_schritt()["ziel"] == db
    # the runner's gate follows the target: with none, the step runs even
    # when the exports reported nothing new
    r = app_mod.JobRunner()
    r.neu = 0
    assert r._erspart({"nur_bei_neuem": True, "ziel": None}) is False
    assert r._erspart({"nur_bei_neuem": True, "ziel": db}) is True
    db.write_bytes(b"kein sqlite")
    assert store_layout.mit_schluesseln(db) is False and store_layout.veraltet(db) is True
