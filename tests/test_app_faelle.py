"""
Search history, saved searches and cases (11.0) through the app: the
resources under /api/v1/searches and /api/v1/cases, the `case` filter on
/api/v1/search, the settings that govern them, the export as a run – and
the page's side of it in node: the third door, the tick on a hit, the
mark, the windows.

The store is built with keys the way an index run assigns them
(schluessel.zuweisen), so a case can point at its hits.
"""

import re
import time
import json
import threading
import urllib.parse

import pytest

import app as app_mod
import corpus
import faelle
import rag_index
import schluessel
import case_export
from tests.hilfen import call, call_kopf
from tests.test_app import (sandbox, _warte, _in_node, GRUNDZUSTAND, TEAMS_HTML)  # noqa: F401

MAIL = ("Message-ID: <m1@example.com>\nFrom: Carla Chef <carla@example.com>\n"
        "To: alice@example.com\nSubject: Rechnung 4711 freigegeben\nDate: Tue, 10 Jun 2025 08:00:00 +0000\n\n"
        "Hallo zusammen, die Rechnung 4711 ist freigegeben.\n")
MAIL2 = ("Message-ID: <m2@example.com>\nFrom: Alice Beispiel <alice@example.com>\n"
         "To: bob@example.com\nSubject: Urlaubsantrag August\nDate: Tue, 1 Jul 2025 12:00:00 +0000\n\n"
         "Hiermit beantrage ich Urlaub.\n")


# A reply to MAIL: the two make one conversation – without a word the
# other tests search for, so their counts stay.
MAIL3 = ("Message-ID: <m3@example.com>\nIn-Reply-To: <m1@example.com>\nReferences: <m1@example.com>\n"
         "From: Alice Beispiel <alice@example.com>\nTo: carla@example.com\nSubject: Re: Freigabe\n"
         "Date: Wed, 11 Jun 2025 09:00:00 +0000\n\nDanke, dann buche ich sie.\n")


@pytest.fixture
def welt(sandbox, with_ollama):  # noqa: F811
    """A store with keys, the app on a port, an empty case book."""
    yield from _welt(sandbox, {"mail1.eml": MAIL, "mail2.eml": MAIL2})


@pytest.fixture
def welt_thread(sandbox, with_ollama):  # noqa: F811
    """The same, with a reply that makes mail1 a conversation of two."""
    yield from _welt(sandbox, {"mail1.eml": MAIL, "mail2.eml": MAIL2, "mail3.eml": MAIL3})


def _welt(wurzel, mails):
    teams = wurzel / "teams_export" / "1on1"
    teams.mkdir(parents=True)
    (teams / "alice__abc.html").write_text(TEAMS_HTML, encoding="utf-8")
    outlook = wurzel / "outlook_export" / "inbox"
    outlook.mkdir(parents=True)
    for name, text in mails.items():
        (outlook / name).write_text(text, encoding="utf-8")
    recs = corpus.load_records(str(wurzel / "teams_export"), str(wurzel / "outlook_export"))
    chunks = corpus.chunk_records(recs)
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    schluessel.zuweisen(chunks, {"teams": wurzel / "teams_export", "outlook": wurzel / "outlook_export"})
    (wurzel / "rag_store").mkdir()
    rag_index.write_db(wurzel / "rag_store", chunks)
    rag_index.write_info(wurzel / "rag_store", None, 0, len(chunks))
    a = app_mod.App(app_mod.load_config())
    a.cfg["case_export_dir"] = str(wurzel / "exporte")
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"app": a, "port": httpd.server_address[1], "sandbox": wurzel, "chunks": chunks}
    httpd.shutdown()
    httpd.server_close()


def _treffer(welt, q="Rechnung", **extra):
    """One page of hits through the versioned surface. It answers `items`
    like every collection there; the tests read `results` as the engine
    names them, so the helper hands both over."""
    # The history is the page's: the versioned surface files a search only
    # when the caller asks for it, and the page asks (12.0).
    p = "&".join(f"{k}={v}" for k, v in {"q": q, "remember": "1", **extra}.items())
    code, r = call(welt["port"], "GET", "/api/v1/search?" + p)
    assert code == 200, r
    return {**r, "results": r.get("items", []), "count": len(r.get("items", []))}


def _fall(welt, name="Nordwind", beschreibung=""):
    code, r = call(welt["port"], "POST", "/api/v1/cases", {"name": name, "description": beschreibung})
    assert code == 201, r
    return r["case"]["id"]


def _eid(port, fid, key):
    """The id the case gave an item – what the API names it by."""
    fall = call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]
    return next(e["id"] for e in fall["item_list"] if e["key"] == key)


def _eintrag(h):
    """One item as a caller hands it over – English, as the API takes it."""
    return {"key": h["key"], "src": h["source"], "root": h["root"], "rel": h["path"],
            "title": h["title"], "date": h["date"], "who": h["who"]}


# --------------------------------------------------------------------------
# The search history
# --------------------------------------------------------------------------
def test_suche_landet_in_der_historie_mit_kriterien_ohne_treffer(welt):
    port = welt["port"]
    r = _treffer(welt, "Rechnung", source="outlook", mode="lexical")
    assert r["count"] >= 1
    code, h = call(port, "GET", "/api/v1/searches/history")
    assert code == 200 and h["retention"] == "90"
    assert len(h["items"]) == 1
    k = h["items"][0]["criteria"]
    assert k["q"] == "Rechnung" and k["source"] == "outlook" and k["mode"] == "text"
    assert h["items"][0]["hits"] == r["count"]
    # the criteria, never the hits themselves
    assert "results" not in h["items"][0] and "items" not in h["items"][0]
    # the second page of the same search is not a second search
    _treffer(welt, "Rechnung", source="outlook", mode="lexical", offset="20")
    # the same criteria again move the entry, they do not pile up
    _treffer(welt, "Rechnung", source="outlook", mode="lexical")
    assert len(call(port, "GET", "/api/v1/searches/history")[1]["items"]) == 1
    # a different search is a new entry, a browse without criteria is none
    _treffer(welt, "Urlaub")
    _treffer(welt, "")
    assert len(call(port, "GET", "/api/v1/searches/history")[1]["items"]) == 2
    # clearing the history removes the resource, so it is a DELETE
    code, r = call(port, "DELETE", "/api/v1/searches/history")
    assert code == 204 and r in (None, "", {})
    assert call(port, "GET", "/api/v1/searches/history")[1]["items"] == []


def test_historie_aus_merkt_nichts_und_leert(welt):
    port = welt["port"]
    _treffer(welt, "Rechnung")
    assert len(call(port, "GET", "/api/v1/searches/history")[1]["items"]) == 1
    code, r = call(port, "PATCH", "/api/v1/config", {"search_history": "off"})
    assert code == 200 and r["config"]["search_history"] == "off"
    h = call(port, "GET", "/api/v1/searches/history")[1]
    assert h["items"] == [] and h["retention"] == "off"
    _treffer(welt, "Urlaub")
    assert call(port, "GET", "/api/v1/searches/history")[1]["items"] == []
    # an unknown value is ignored, the known ones stick
    call(port, "PATCH", "/api/v1/config", {"search_history": "sometimes"})
    assert welt["app"].cfg["search_history"] == "off"
    call(port, "PATCH", "/api/v1/config", {"search_history": "forever"})
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
    code, r, kopf = call_kopf(port, "POST", "/api/v1/searches/saved",
                              {"name": "Rechnungen",
                               "criteria": {"q": "Rechnung", "mode": "lexical", "source": "outlook"}})
    assert code == 201 and r["search"]["name"] == "Rechnungen"
    sid = r["search"]["id"]
    assert kopf["Location"] == f"/api/v1/searches/saved/{sid}"
    assert r["search"]["criteria"]["mode"] == "text" and r["search"]["last_run"] is None
    code, r = call(port, "POST", "/api/v1/searches/saved", {"name": "  ", "criteria": {}})
    assert code == 400 and r["error"]["k"] == "srv.case.noname"
    liste = call(port, "GET", "/api/v1/searches/saved")[1]["items"]
    assert [g["name"] for g in liste] == ["Rechnungen"]
    # running it records when and how many – on the first page only
    r = _treffer(welt, "Rechnung", source="outlook", saved=str(sid))
    g = call(port, "GET", "/api/v1/searches/saved")[1]["items"][0]
    assert g["last_run"] and g["hits"] == r["count"]
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"name": "Alle Rechnungen"})
    assert code == 200 and r["search"]["name"] == "Alle Rechnungen"
    for weg in ("999", "abc"):
        code, r = call(port, "GET", f"/api/v1/searches/saved/{weg}")
        assert code == 404 and r["error"]["k"] == "srv.search.unknown", weg
    code, r = call(port, "PATCH", "/api/v1/searches/saved/999", {"name": "x"})
    assert code == 404 and r["error"]["k"] == "srv.search.unknown"
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"name": " "})
    assert code == 400 and r["error"]["k"] == "srv.case.noname"
    assert call(port, "GET", "/api/v1/searches/saved")[1]["items"][0]["name"] == "Alle Rechnungen"
    assert call(port, "DELETE", f"/api/v1/searches/saved/{sid}")[0] == 204
    assert call(port, "DELETE", f"/api/v1/searches/saved/{sid}")[0] == 404
    assert call(port, "GET", "/api/v1/searches/saved")[1]["items"] == []


def test_case_folder_allein_setzt_den_ordner_der_angehaengten_suche(welt):
    """`case_folder` on its own files new hits into a folder of the case
    the search is attached to – it used to be ignored unless `case` came
    along. Without a case there is nothing to file into, and a case that
    is no id is a malformed request, not a missing case."""
    port = welt["port"]
    fid = _fall(welt)
    ordner = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": "Belege"})[1]["folder"]
    sid = call(port, "POST", "/api/v1/searches/saved",
               {"name": "Rechnungen", "criteria": {"q": "Rechnung"}})[1]["search"]["id"]
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case_folder": ordner})
    assert code == 400 and r["error"]["k"] == "srv.case.folder.nocase", r
    code, r = call(port, "POST", "/api/v1/searches/saved",
                   {"name": "x", "criteria": {}, "case_folder": ordner})
    assert code == 400 and r["error"]["k"] == "srv.case.folder.nocase", r
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case": fid})
    assert code == 200 and r["search"]["case"] == fid and r["search"]["case_folder"] is None
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case_folder": ordner})
    assert code == 200 and r["search"]["case"] == fid and r["search"]["case_folder"] == ordner, r
    # Only the named fields change: `case` alone keeps the folder while the
    # case stays, and drops it only when the case changes.
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case": fid})
    assert code == 200 and r["search"]["case_folder"] == ordner, r
    anderer = _fall(welt, "Anderer")
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case": anderer})
    assert code == 200 and r["search"]["case"] == anderer and r["search"]["case_folder"] is None
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case": fid, "case_folder": ordner})
    assert code == 200 and r["search"]["case_folder"] == ordner
    # The object round-trips: what it answers is what a PATCH takes.
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}",
                   {k: r["search"][k] for k in ("case", "case_folder")})
    assert code == 200 and r["search"]["case_folder"] == ordner and r["search"]["case_folder_name"] == "Belege"
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case_folder": 999})
    assert code == 404 and r["error"]["k"] == "srv.case.nofolder"
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case_folder": None})
    assert code == 200 and r["search"]["case_folder"] is None
    for body in ({"case": "abc"}, {"case_folder": "abc"}):
        code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", body)
        assert code == 400 and r["error"]["k"].startswith("srv.case.bad"), (body, r)
    code, r = call(port, "POST", "/api/v1/searches/saved",
                   {"name": "x", "criteria": {}, "case": "abc"})
    assert code == 400 and r["error"]["k"] == "srv.case.badcase" and r["error"]["v"]["value"] == "abc"


def test_gespeicherte_suche_an_fall_haengen_und_loesen(welt):
    port = welt["port"]
    fid = _fall(welt)
    sid = call(port, "POST", "/api/v1/searches/saved",
               {"name": "Rechnungen", "criteria": {"q": "Rechnung"}, "case": fid})[1]["search"]["id"]
    g = call(port, "GET", "/api/v1/searches/saved")[1]["items"][0]
    assert g["case"] == fid and g["case_name"] == "Nordwind"
    fall = call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]
    assert [s["id"] for s in fall["search_list"]] == [sid] and fall["searches"] == 1
    # detaching is the same PATCH with no case
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case": None})
    assert code == 200 and r["search"]["case"] is None
    assert call(port, "GET", "/api/v1/searches/saved")[1]["items"][0]["case"] is None
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case": 999})
    assert code == 404 and r["error"]["k"] == "srv.case.unknown"
    call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"case": fid})
    assert code == 409 and r["error"]["k"] == "srv.case.closed"
    code, r = call(port, "POST", "/api/v1/searches/saved",
                   {"name": "x", "criteria": {}, "case": fid})
    assert code == 409 and r["error"]["k"] == "srv.case.closed"


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------
def test_fall_anlegen_aendern_schliessen_oeffnen_loeschen(welt):
    port = welt["port"]
    code, r = call(port, "POST", "/api/v1/cases", {"name": "  "})
    assert code == 400 and r["error"]["k"] == "srv.case.noname"
    fid = _fall(welt, "Nordwind", "Alles zur Rechnung")
    code, r = call(port, "GET", "/api/v1/cases")
    assert code == 200 and [f["name"] for f in r["items"]] == ["Nordwind"]
    assert r["items"][0]["status"] == "open" and r["items"][0]["items"] == 0
    # Where an export lands is a path, so it is part of the environment.
    umgebung = call(port, "GET", "/api/v1/app")[1]
    assert umgebung["case_export_dir"] == str(welt["sandbox"] / "exporte")
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}", {"name": "Nordwind 2026"})
    assert code == 200 and r["case"]["name"] == "Nordwind 2026" and r["case"]["description"] == "Alles zur Rechnung"
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}", {"description": ""})
    assert r["case"]["description"] == ""
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}", {"name": ""})
    assert code == 400
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    assert code == 200 and r["case"]["status"] == "closed" and r["case"]["closed_at"]
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}", {"name": "x"})
    assert code == 409 and r["error"]["k"] == "srv.case.closed"
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    assert code == 200 and r["case"]["status"] == "closed", "zweimal schliessen aendert nichts"
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "open"})
    assert code == 200 and r["case"]["status"] == "open" and r["case"]["closed_at"] is None
    code, r = call(port, "GET", f"/api/v1/cases/{999}")
    assert code == 404 and r["error"]["k"] == "srv.case.unknown" and r["detail"]
    code, r = call(port, "PATCH", f"/api/v1/cases/{999}", {"name": "x"})
    assert code == 404
    code, r = call(port, "DELETE", f"/api/v1/cases/{fid}")
    assert code == 204 and r in (None, "", {})
    assert call(port, "GET", "/api/v1/cases")[1]["items"] == []
    assert call(port, "DELETE", f"/api/v1/cases/{fid}")[0] == 404


def test_treffer_in_den_fall_und_wieder_heraus(welt):
    port = welt["port"]
    fid = _fall(welt)
    hits = _treffer(welt, "Rechnung")["results"]
    assert hits and all(h["key"] for h in hits) and all(h["cases"] == [] for h in hits)
    mail = next(h for h in hits if h["source"] == "outlook")
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/items",
                   {"items": [_eintrag(mail), {"key": "", "title": "ohne"}, "kein dict"]})
    assert code == 201 and (r["added"], r["already"]) == (1, 0)
    assert r["case"]["items"] == 1, "jede Aenderung antwortet mit dem Fall"
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(mail)]})
    assert code == 200 and (r["added"], r["already"]) == (0, 1)
    # the hit now wears the mark, the case holds what the hit said
    mail2 = next(h for h in _treffer(welt, "Rechnung")["results"] if h["source"] == "outlook")
    assert mail2["cases"] == [{"id": fid, "name": "Nordwind", "status": "open", "folder": None}]
    fall = call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]
    assert fall["items"] == 1 and fall["items_by_source"] == {"outlook": 1}
    e = fall["item_list"][0]
    assert e["key"] == mail["key"] and e["title"] == "Rechnung 4711 freigegeben"
    assert e["rel"] == "inbox/mail1.eml" and e["root"] == "outlook" and e["list"] is None
    code, r = call(port, "DELETE", f"/api/v1/cases/{fid}/items/{e['id']}")
    assert code == 200 and r["case"]["items"] == 0
    assert call(port, "DELETE", f"/api/v1/cases/{fid}/items/{e['id']}")[0] == 404
    call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(mail)]})
    assert code == 409 and r["error"]["k"] == "srv.case.closed"


def test_der_fallfilter_sucht_nur_im_fall(welt):
    port = welt["port"]
    fid = _fall(welt)
    hits = _treffer(welt, "")["results"]                    # browse: everything
    assert len(hits) >= 3
    mail = next(h for h in hits if h["path"] == "inbox/mail1.eml")
    call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(mail)]})
    im_fall = _treffer(welt, "", case=str(fid))["results"]
    assert [h["path"] for h in im_fall] == ["inbox/mail1.eml"]
    assert [h["uid"] for h in _treffer(welt, "Rechnung", case=str(fid))["results"]] == [mail["uid"]]
    assert _treffer(welt, "Urlaub", case=str(fid))["results"] == []
    code, r = call(port, "GET", "/api/v1/search?q=Rechnung&case=999")
    assert code == 409 and r["hits"] == [] and "No case named" in r["detail"]
    # the history keeps the case as a criterion
    h = call(port, "GET", "/api/v1/searches/history")[1]["items"]
    assert h[0]["criteria"]["case"] == fid


def test_ergebnisliste_wird_festgehalten(welt):
    port = welt["port"]
    fid = _fall(welt)
    alle = _treffer(welt, "")["count"]
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/lists",
                   {"criteria": {"q": "", "source": "outlook"}})
    assert code == 201 and r["hits"] == 2 and r["added"] == 2
    lid = r["list"]
    fall = call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]
    assert fall["items"] == 2 and fall["lists"] == 1
    li = fall["list_list"][0]
    assert li["id"] == lid and li["hits"] == 2 and li["criteria"]["source"] == "outlook"
    assert all(e["list"] == lid for e in fall["item_list"])
    # a second list with words: hits already there are not new
    code, r2 = call(port, "POST", f"/api/v1/cases/{fid}/lists", {"criteria": {"q": "Rechnung", "mode": "text"}})
    assert r2["hits"] >= 1 and r2["added"] == r2["hits"] - 1
    assert r2["hits"] <= alle
    # dropping the first list takes its two mails along; what the second
    # list brought stays
    code, r = call(port, "DELETE", f"/api/v1/cases/{fid}/lists/{lid}")
    assert code == 200 and r["case"]["lists"] == 1 and r["case"]["items"] == r2["added"]
    # a bad search is refused, nothing stored
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/lists", {"criteria": {"q": "x", "case": 999}})
    assert code == 409 and "No case named" in r["detail"]
    assert call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]["lists"] == 1


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
    code, r, kopf = call_kopf(port, "POST", f"/api/v1/cases/{fid}/notes", {"text": " Erste Notiz "})
    assert code == 201 and r["note"]
    nid = r["note"]
    assert kopf["Location"] == f"/api/v1/cases/{fid}/notes/{nid}"
    assert [n["text"] for n in r["case"]["note_list"]] == ["Erste Notiz"]
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/notes", {"text": "  "})
    assert code == 400 and r["error"]["k"] == "srv.case.noname"
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/notes/{nid}", {"text": "Geändert"})
    assert code == 200 and [n["text"] for n in r["case"]["note_list"]] == ["Geändert"]
    code, r = call(port, "DELETE", f"/api/v1/cases/{fid}/notes/{nid}")
    assert code == 200 and r["case"]["note_list"] == [] and r["case"]["notes"] == 0
    call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    assert call(port, "POST", f"/api/v1/cases/{fid}/notes", {"text": "spät"})[0] == 409


def test_ein_unlesbarer_ordner_legt_nichts_still_ab(welt):
    """`folder: null` means "out of its folder" – so a value nobody can
    read must not mean the same thing. It used to: the item was unfiled
    and the answer looked like a success."""
    port = welt["port"]
    fid = _fall(welt)
    mail = next(h for h in _treffer(welt, "")["results"] if h["path"] == "inbox/mail1.eml")
    ordner = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": "Belege"})[1]["folder"]
    call(port, "POST", f"/api/v1/cases/{fid}/items",
         {"items": [_eintrag(mail)], "folder": ordner})
    eid = call(port, "GET", f"/api/v1/cases/{fid}/items")[1]["items"][0]["id"]

    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/items/{eid}",
                   {"remark": "Beleg", "folder": "Belege"})     # der Name, nicht die id
    assert code == 400 and r["error"]["k"] == "srv.case.badfolder"
    # Nichts ist passiert: weder die Bemerkung noch der Ordner.
    eintrag = call(port, "GET", f"/api/v1/cases/{fid}/items/{eid}")[1]["item"]
    assert eintrag["folder"] == ordner and not eintrag.get("remark")
    # Und `null` heisst weiterhin: heraus aus dem Ordner.
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/items/{eid}", {"folder": None})
    assert code == 200
    assert call(port, "GET", f"/api/v1/cases/{fid}/items/{eid}")[1]["item"]["folder"] is None


def test_eine_bemerkung_bleibt_aus_wenn_der_ordner_fremd_ist(welt):
    """One PATCH, one outcome: a folder of another case is refused before
    the remark is written – it used to be committed first, and the 404
    arrived with the case already changed."""
    port = welt["port"]
    fid, fremd = _fall(welt), _fall(welt, "Anderer")
    mail = next(h for h in _treffer(welt, "")["results"] if h["path"] == "inbox/mail1.eml")
    assert call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(mail)]})[0] == 201
    eid = _eid(port, fid, mail["key"])
    ordner = call(port, "POST", f"/api/v1/cases/{fremd}/folders", {"name": "Drueben"})[1]["folder"]
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/items/{eid}",
                   {"remark": "x", "folder": ordner})
    assert code == 404 and r["error"]["k"] == "srv.case.nofolder", r
    eintrag = call(port, "GET", f"/api/v1/cases/{fid}/items/{eid}")[1]["item"]
    assert not eintrag.get("remark") and eintrag["folder"] is None


def test_ein_ordnername_der_schon_da_ist_ist_ein_409(welt):
    """201 means created. The case book hands back the existing folder
    for a repeated name; the route used to pass that on as 201 with a
    Location – the spec promised the 409 a rename onto it gets."""
    port = welt["port"]
    fid = _fall(welt)
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": "Belege"})
    assert code == 201
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": " Belege "})
    assert code == 409 and r["error"]["k"] == "srv.case.folder.exists", r
    assert len(call(port, "GET", f"/api/v1/cases/{fid}/folders")[1]["items"]) == 1
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": " "})
    assert code == 400 and r["error"]["k"] == "srv.case.noname"


def test_eine_umbenennung_bleibt_aus_wenn_der_fall_zu_ist(welt):
    """One PATCH, one outcome: a closed case refuses the attach before
    the rename is written – the name used to be committed first."""
    port = welt["port"]
    fid = _fall(welt)
    sid = call(port, "POST", "/api/v1/searches/saved",
               {"name": "Rechnungen", "criteria": {"q": "Rechnung"}, "case": fid})[1]["search"]["id"]
    call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}", {"name": "Neu", "case_folder": None})
    assert code == 409 and r["error"]["k"] == "srv.case.closed", r
    assert call(port, "GET", f"/api/v1/searches/saved/{sid}")[1]["search"]["name"] == "Rechnungen"


def test_eine_unlesbare_unterkennung_nennt_ihre_sammlung(welt):
    """`/cases/1/notes/abc` used to answer "no such case" for a case that
    is right there. The collection it could not find says so."""
    port = welt["port"]
    fid = _fall(welt)
    for unter, erwartet in (("notes", "srv.case.nonote"), ("lists", "srv.case.nolist"),
                            ("items", "srv.case.noitem"), ("folders", "srv.case.nofolder")):
        code, r = call(port, "GET", f"/api/v1/cases/{fid}/{unter}/abc")
        assert code == 404 and r["error"]["k"] == erwartet, (unter, r)


def test_eine_fremde_notiz_oder_liste_ist_ein_404(welt):
    """DELETE and PATCH on a note, DELETE on a list: the case book says
    whether there was one. It used to be ignored – 200 with the case
    unchanged, while GET on the same id said 404."""
    port = welt["port"]
    fid = _fall(welt)
    code, r = call(port, "DELETE", f"/api/v1/cases/{fid}/notes/999")
    assert code == 404 and r["error"]["k"] == "srv.case.nonote", r
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/notes/999", {"text": "x"})
    assert code == 404 and r["error"]["k"] == "srv.case.nonote", r
    code, r = call(port, "DELETE", f"/api/v1/cases/{fid}/lists/999")
    assert code == 404 and r["error"]["k"] == "srv.case.nolist", r
    # Those that are there go on as before.
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/notes", {"text": "da"})
    assert code == 201
    nid = r["note"]
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/notes/{nid}", {"text": "geaendert"})
    assert code == 200 and r["case"]["note_list"][0]["text"] == "geaendert"
    assert call(port, "DELETE", f"/api/v1/cases/{fid}/notes/{nid}")[0] == 200
    assert call(port, "GET", f"/api/v1/cases/{fid}/notes")[1]["items"] == []


def test_nur_eintraege_ohne_key_sind_nichts_zum_hinzufuegen(welt):
    """A row without a key is no item and is dropped, as the case book
    drops it – but a body of nothing else used to answer 200 with
    added: 0, and the caller could not tell its rows were malformed."""
    port = welt["port"]
    fid = _fall(welt)
    for items in ([{"title": "x"}], [{"key": ""}, {"key": "  "}], ["kein dict"]):
        code, r = call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": items})
        assert code == 400 and r["error"]["k"] == "srv.case.noitems", (items, r)
    assert call(port, "GET", f"/api/v1/cases/{fid}/items")[1]["items"] == []


def test_eine_liste_auf_einem_geschlossenen_fall_sucht_nicht_erst(welt, monkeypatch):
    """The search is the expensive part of a stored list; a closed case,
    or a folder the case does not have, used to be refused only after
    it had run to the end."""
    import app as app_mod
    port = welt["port"]
    fid = _fall(welt)

    def nicht(*_a, **_k):
        raise AssertionError("searched before the refusal")
    monkeypatch.setattr(app_mod.Handler, "_alle_treffer", nicht)
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/lists",
                   {"criteria": {"q": "x"}, "folder": 999})
    assert code == 404 and r["error"]["k"] == "srv.case.nofolder", r
    call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/lists", {"criteria": {"q": "x"}})
    assert code == 409 and r["error"]["k"] == "srv.case.closed", r


def test_ein_schreibzugriff_auf_einen_eintrag_geht_einmal_durch_den_index(welt, monkeypatch):
    """Resolving the item needs the case book alone; the answer walks the
    index once. It used to be twice per write – and once to read a note,
    which needs it not at all."""
    import app as app_mod
    port = welt["port"]
    fid = _fall(welt)
    treffer = _treffer(welt, "")["results"]
    mail = next(h for h in treffer if h["path"] == "inbox/mail1.eml")
    andere = next(h for h in treffer if h["path"] != "inbox/mail1.eml")
    assert call(port, "POST", f"/api/v1/cases/{fid}/items",
                {"items": [_eintrag(mail), _eintrag(andere)]})[0] == 201
    eid = _eid(port, fid, mail["key"])
    nid = call(port, "POST", f"/api/v1/cases/{fid}/notes", {"text": "n"})[1]["note"]
    echt = app_mod.Handler._index_stand
    laeufe = []          # how many items each walk was asked about
    monkeypatch.setattr(app_mod.Handler, "_index_stand",
                        lambda self, fall: laeufe.append(len(fall["eintraege_liste"])) or echt(self, fall))
    assert call(port, "PATCH", f"/api/v1/cases/{fid}/items/{eid}", {"remark": "r"})[0] == 200
    assert laeufe == [2]                  # the answer is the whole case
    laeufe.clear()
    assert call(port, "GET", f"/api/v1/cases/{fid}/notes/{nid}")[0] == 200
    assert call(port, "GET", f"/api/v1/cases/{fid}/notes")[0] == 200
    assert call(port, "GET", f"/api/v1/cases/{fid}/folders")[0] == 200
    assert laeufe == []
    # An item on its own carries the index's word, like the list does –
    # asked for that one item, not for the whole case's worth.
    assert "thread_open" in call(port, "GET", f"/api/v1/cases/{fid}/items/{eid}")[1]["item"]
    assert laeufe == [1]
    laeufe.clear()
    assert call(port, "DELETE", f"/api/v1/cases/{fid}/items/{eid}")[0] == 200
    assert laeufe == [1]                  # one item left in the case


def test_eintraege_und_gespraeche_greifen_zusammen_oder_gar_nicht(welt, monkeypatch):
    """One request, one outcome: adding items and completing their
    conversations in the same POST either takes hold whole or leaves the
    case as it was. The items used to be written first and the refusal to
    arrive afterwards – with the case already changed."""
    import app as app_mod
    port = welt["port"]
    fid = _fall(welt)
    mail = next(h for h in _treffer(welt, "")["results"] if h["path"] == "inbox/mail1.eml")
    # Ein Index, der keine Gespraeche kennt – wie einer von vor 11.0.
    monkeypatch.setattr(app_mod.Handler, "_gespraeche_moeglich",
                        lambda self: {"k": "srv.case.nothread", "v": {}})
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/items",
                   {"items": [_eintrag(mail)], "threads": ["mail:<m1@example.com>"]})
    assert code == 409 and r["error"]["k"] == "srv.case.nothread"
    # Und der Fall ist so leer wie vorher.
    assert call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]["items"] == 0


def test_neue_treffer_sprechen_dieselbe_sprache(welt):
    """A new hit carries the case marks of the cases it already sits in –
    and those spoke the case book's German while every other route
    translated them. A client reading `folder` and `status` saw neither."""
    port = welt["port"]
    fid = _fall(welt)
    zweiter = call(port, "POST", "/api/v1/cases", {"name": "Zweiter"})[1]["case"]["id"]
    hits = _treffer(welt, "")["results"]
    mail = next(h for h in hits if h["path"] == "inbox/mail1.eml")
    ordner = call(port, "POST", f"/api/v1/cases/{zweiter}/folders",
                  {"name": "Belege"})[1]["folder"]
    call(port, "POST", f"/api/v1/cases/{zweiter}/items",
         {"items": [_eintrag(mail)], "folder": ordner})
    call(port, "POST", "/api/v1/searches/saved",
         {"name": "Alle", "criteria": {"q": "", "source": "outlook"}, "case": fid})

    code, r = call(port, "GET", f"/api/v1/cases/{fid}/new-hits")
    assert code == 200
    marken = [m for b in r["items"] for h in b["new"] for m in (h.get("cases") or [])]
    assert marken, r
    for m in marken:
        assert m["status"] in ("open", "closed"), m
        assert "ordner" not in m and "offen" not in str(m.get("status")), m
        if m["id"] == zweiter:
            assert m.get("folder") == "Belege", m


def test_was_location_nennt_laesst_sich_holen(welt):
    """`Location` on a create means: the thing is there, at this address.
    So every address a create names answers a GET – the folder, the note,
    the stored list, the saved search, and the item beside them."""
    port = welt["port"]
    fid = _fall(welt)
    mail = next(h for h in _treffer(welt, "")["results"] if h["path"] == "inbox/mail1.eml")
    orte = {}
    for unter, koerper, feld in (
            ("folders", {"name": "Belege"}, "folder"),
            ("notes", {"text": "Notiz"}, "note"),
            ("lists", {"name": "Alles", "criteria": {"q": "", "source": "outlook"}}, "list")):
        code, r, kopf = call_kopf(port, "POST", f"/api/v1/cases/{fid}/{unter}", koerper)
        assert code == 201 and kopf["Location"], unter
        orte[feld] = kopf["Location"]
    code, r, kopf = call_kopf(port, "POST", "/api/v1/searches/saved",
                              {"name": "Alle Mails", "criteria": {"q": ""}})
    assert code == 201
    orte["search"] = kopf["Location"]
    call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(mail)]})
    eid = call(port, "GET", f"/api/v1/cases/{fid}/items")[1]["items"][0]["id"]
    orte["item"] = f"/api/v1/cases/{fid}/items/{eid}"

    for feld, ort in orte.items():
        code, r = call(port, "GET", ort)
        assert code == 200, (feld, ort, r)
        assert feld in r and r[feld], (feld, r)
    # Und was es nicht gibt, sagt das auch.
    assert call(port, "GET", f"/api/v1/cases/{fid}/notes/{999}")[0] == 404
    assert call(port, "GET", f"/api/v1/cases/{fid}/lists/{999}")[0] == 404
    assert call(port, "GET", "/api/v1/searches/saved/999")[0] == 404


def test_jede_unterkollektion_laesst_sich_auch_lesen(welt):
    """What POST creates into, GET reads out of: the four collections under
    a case answer on their own, not only nested in the case. The page reads
    them from the case it gets back – a script has no case in hand, and a
    collection one can only write to is not a resource."""
    port = welt["port"]
    fid = _fall(welt)
    mail = next(h for h in _treffer(welt, "")["results"] if h["path"] == "inbox/mail1.eml")
    ordner = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": "Belege"})[1]["folder"]
    call(port, "POST", f"/api/v1/cases/{fid}/items",
         {"items": [_eintrag(mail)], "folder": ordner})
    call(port, "POST", f"/api/v1/cases/{fid}/notes", {"text": "Notiz"})
    call(port, "POST", f"/api/v1/cases/{fid}/lists",
         {"name": "Alles", "criteria": {"q": "", "source": "outlook"}})
    fall = call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]
    for unter, drin in (("items", "item_list"), ("folders", "folder_list"),
                        ("notes", "note_list"), ("lists", "list_list")):
        code, r = call(port, "GET", f"/api/v1/cases/{fid}/{unter}")
        assert code == 200, (unter, r)
        assert r["items"] == fall[drin], unter      # dasselbe wie im Fall
        assert r["items"], f"{unter}: leer"
    assert call(port, "GET", f"/api/v1/cases/{999}/items")[1]["error"]["k"] == "srv.case.unknown"


def test_neue_treffer_der_angehaengten_suchen(welt):
    port = welt["port"]
    fid = _fall(welt)
    hits = _treffer(welt, "")["results"]
    mail1 = next(h for h in hits if h["path"] == "inbox/mail1.eml")
    call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(mail1)]})
    call(port, "POST", "/api/v1/searches/saved",
         {"name": "Alle Mails", "criteria": {"q": "", "source": "outlook"}, "case": fid})
    call(port, "POST", "/api/v1/searches/saved",
         {"name": "Kaputt", "criteria": {"q": "x", "case": 999}, "case": fid})
    code, r = call(port, "GET", f"/api/v1/cases/{fid}/new-hits")
    assert code == 200 and r["case"]["name"] == "Nordwind"
    bloecke = {b["name"]: b for b in r["items"]}
    assert [h["path"] for h in bloecke["Alle Mails"]["new"]] == ["inbox/mail2.eml"]
    assert bloecke["Alle Mails"]["new_count"] == 1
    assert "No case named" in bloecke["Kaputt"]["error"] and bloecke["Kaputt"]["new"] == []
    assert call(port, "GET", f"/api/v1/cases/{999}/new-hits")[1]["error"]["k"] == "srv.case.unknown"


def test_fall_loeschen_loest_suchen_und_nimmt_die_marke(welt):
    port = welt["port"]
    fid = _fall(welt)
    hits = _treffer(welt, "Rechnung")["results"]
    call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(hits[0])]})
    sid = call(port, "POST", "/api/v1/searches/saved",
               {"name": "S", "criteria": {"q": "R"}, "case": fid})[1]["search"]["id"]
    call(port, "DELETE", f"/api/v1/cases/{fid}")
    g = call(port, "GET", "/api/v1/searches/saved")[1]["items"]
    assert [x["id"] for x in g] == [sid] and g[0]["case"] is None
    assert all(h["cases"] == [] for h in _treffer(welt, "Rechnung")["results"])


# --------------------------------------------------------------------------
# The export as a run
# --------------------------------------------------------------------------
def test_export_ist_ein_lauf_und_schreibt_den_ordner(welt, monkeypatch):
    a, port = welt["app"], welt["port"]
    fid = _fall(welt, "Nordwind", "Alles")
    hits = _treffer(welt, "")["results"]
    call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(h) for h in hits]})
    call(port, "POST", f"/api/v1/cases/{fid}/notes", {"text": "Notiz"})
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/export")
    assert code == 202 and r["run"] and "error" not in r, r
    ziel = r["path"]
    assert ziel.startswith(str(welt["sandbox"] / "exporte")) and "Nordwind" in ziel and ziel.endswith(".zip")
    assert a.jobs.busy
    _warte(a.jobs, 60)
    assert a.jobs.last["ok"], "\n".join(str(z.get("text")) for z in a.jobs.lines)
    ordner = welt["sandbox"] / "exporte"
    import zipfile
    namen = zipfile.ZipFile(ziel).namelist()
    stamm = ziel.rsplit("/", 1)[1][:-4]
    assert f"{stamm}/index.html" in namen and f"{stamm}/Outlook/inbox/mail1.eml" in namen
    assert f"{stamm}/Teams/1on1/alice__abc.html" in namen
    assert [p.name for p in ordner.iterdir()] == [ziel.rsplit("/", 1)[1]], "one ZIP, no folder beside it"
    log = "\n".join(json.dumps(z, ensure_ascii=False) for z in a.jobs.lines)
    assert "run.case.start" in log and "run.case.done" in log
    fall = call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]
    assert fall["exported"].endswith(".zip") and fall["exported_at"]
    # "Show folder": the zip's folder, through the file manager
    geoeffnet = []
    monkeypatch.setattr(app_mod.archive_check, "ordner_oeffnen", lambda p: geoeffnet.append(str(p)) or True)
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/export/open")
    assert code == 200 and geoeffnet == [str(ordner)]
    # a closed case can be exported as well
    call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/export")
    assert code == 202 and r["run"]
    _warte(a.jobs, 60)
    assert a.jobs.last["ok"]


def test_export_verweigert_waehrend_eines_laufs_und_ohne_fall(welt, monkeypatch):
    a, port = welt["app"], welt["port"]
    fid = _fall(welt)
    monkeypatch.setattr(a.jobs, "start", lambda steps, label, **kw: True)
    code, r = call(port, "POST", f"/api/v1/cases/{999}/export")
    assert code == 404 and r["error"]["k"] == "srv.case.unknown"
    gesehen = {}
    monkeypatch.setattr(a.jobs, "start",
                        lambda steps, label, **kw: gesehen.update(steps=steps, label=label) or True)
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/export")
    assert code == 202 and r["run"]
    (schritt,) = gesehen["steps"]
    assert schritt["key"] == "fall_export" and gesehen["label"] == "job.case_export"
    argv = schritt["argv"]
    assert argv[1].endswith("case_export.py") and "--zip" not in argv
    assert argv[argv.index("--fall") + 1] == str(fid)
    assert argv[argv.index("--ziel") + 1] + ".zip" == r["path"]
    assert argv[argv.index("--faelle") + 1] == str(welt["sandbox"] / faelle.DB_NAME)
    assert "--lang" in argv and "--res" in argv
    assert schritt["env"]["MUNIMENTUM_HOME"] == str(welt["sandbox"])
    monkeypatch.setattr(type(a.jobs), "busy", property(lambda self: True))
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/export")
    assert code == 409 and r["error"]["k"] == "srv.busy"


def test_ordner_im_fall(welt):
    """Folders: made, renamed, dropped; items move in and out; a list and
    a saved search file into one; the search narrows to one."""
    port = welt["port"]
    fid = _fall(welt)
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": " Belege "})
    assert code == 201 and r["case"]["folder_list"][0]["name"] == "Belege"
    belege = r["folder"]
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": ""})
    assert code == 400 and r["error"]["k"] == "srv.case.noname"
    vertraege = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": "Verträge"})[1]["folder"]
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/folders/{vertraege}", {"name": "Belege"})
    assert code == 409 and r["error"]["k"] == "srv.case.folder.exists"
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/folders/{999}", {"name": "x"})
    assert code == 404 and r["error"]["k"] == "srv.case.nofolder"
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/folders/{vertraege}", {"name": "Contracts"})
    assert code == 200 and [o["name"] for o in r["case"]["folder_list"]] == ["Belege", "Contracts"]
    # items into a folder, straight from the search and by moving
    hits = _treffer(welt, "")["results"]
    mail1 = next(h for h in hits if h["path"] == "inbox/mail1.eml")
    mail2 = next(h for h in hits if h["path"] == "inbox/mail2.eml")
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(mail1)], "folder": belege})
    assert code == 201 and r["added"] == 1
    call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(mail2)]})
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(mail2)], "folder": 999})
    assert code == 404 and r["error"]["k"] == "srv.case.nofolder"
    fall = call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]
    je = {e["key"]: e["folder"] for e in fall["item_list"]}
    assert je[mail1["key"]] == belege and je[mail2["key"]] is None
    assert [(o["name"], o["items"]) for o in fall["folder_list"]] == [("Belege", 1), ("Contracts", 0)]
    assert all(e["via"] == "ui" for e in fall["item_list"])
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/items", {"keys": [mail2["key"]], "folder": vertraege})
    assert code == 200 and r["moved"] == 1
    # the search narrows to the folder; the mark names it
    im_ordner = _treffer(welt, "", case=str(fid), case_folder=str(belege))["results"]
    assert [h["path"] for h in im_ordner] == ["inbox/mail1.eml"]
    assert im_ordner[0]["cases"] == [{"id": fid, "name": "Nordwind", "status": "open", "folder": "Belege"}]
    code, r = call(port, "GET", f"/api/v1/search?q=&case={fid}&case_folder=999")
    assert code == 409 and "no folder" in r["detail"]
    h = call(port, "GET", "/api/v1/searches/history")[1]["items"][0]["criteria"]
    assert h["case"] == fid and h["case_folder"] == belege
    # a list and a saved search file into a folder
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/lists", {"criteria": {"q": "Rechnung", "mode": "text"}, "folder": belege})
    assert code == 201 and r["list"]
    sid = call(port, "POST", "/api/v1/searches/saved",
               {"name": "R", "criteria": {"q": "Rechnung"}, "case": fid,
                "case_folder": belege})[1]["search"]["id"]
    fall = call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]
    assert fall["list_list"][0]["folder"] == belege and fall["search_list"][0]["case_folder_name"] == "Belege"
    code, r = call(port, "PATCH", f"/api/v1/searches/saved/{sid}",
                   {"case": fid, "case_folder": 999})
    assert code == 404 and r["error"]["k"] == "srv.case.nofolder"
    neu = call(port, "GET", f"/api/v1/cases/{fid}/new-hits")[1]["items"][0]
    assert neu["folder"] == belege and neu["folder_name"] == "Belege"
    # dropping the folder keeps its items, unsorted
    code, r = call(port, "DELETE", f"/api/v1/cases/{fid}/folders/{belege}")
    assert code == 200 and [o["name"] for o in r["case"]["folder_list"]] == ["Contracts"]
    assert r["case"]["items"] >= 2 and all(e["folder"] in (None, vertraege) for e in r["case"]["item_list"])
    call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    assert call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": "x"})[0] == 409


def test_gespraech_in_den_fall(welt_thread):
    """The rest of a conversation follows an item into its folder; the case
    says per item how much of its conversation it lacks; the conversation
    route marks every message with its cases."""
    welt = welt_thread
    port = welt["port"]
    fid = _fall(welt)
    belege = call(port, "POST", f"/api/v1/cases/{fid}/folders", {"name": "Belege"})[1]["folder"]
    hits = _treffer(welt, "")["results"]
    m1 = next(h for h in hits if h["path"] == "inbox/mail1.eml")
    m3 = next(h for h in hits if h["path"] == "inbox/mail3.eml")
    assert m1["thread"] and m1["thread"] == m3["thread"]
    call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(m1)], "folder": belege})
    fall = call(port, "GET", f"/api/v1/cases/{fid}")[1]["case"]
    assert {e["key"]: e["thread_open"] for e in fall["item_list"]} == {m1["key"]: 1}
    assert fall["item_list"][0]["who_mail"] == "carla@example.com"
    # the conversation's messages say which cases hold them
    code, r = call(port, "GET", "/api/v1/threads?key=" + urllib.parse.quote(m1["thread"], safe=""))
    assert code == 200 and r["count"] == 2
    assert {m["path"]: [c["id"] for c in m["cases"]] for m in r["items"]} == \
        {"inbox/mail1.eml": [fid], "inbox/mail3.eml": []}
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/items", {"threads": [m1["key"], "nix"]})
    assert code == 201 and r["added"] == 1
    je = {e["key"]: (e["folder"], e["thread_open"]) for e in r["case"]["item_list"]}
    assert je == {m1["key"]: (belege, 0), m3["key"]: (belege, 0)}
    assert call(port, "POST", f"/api/v1/cases/{fid}/items", {"threads": [m1["key"]]})[1]["added"] == 0
    call(port, "PATCH", f"/api/v1/cases/{fid}", {"status": "closed"})
    assert call(port, "POST", f"/api/v1/cases/{fid}/items", {"threads": [m1["key"]]})[0] == 409


def test_der_bestand_nennt_die_adressspalten_des_index(welt):
    """The page offers the parties filter, the People view's addresses and
    the mail lines only when the index says it has the columns – so the
    inventory must name them (11.1.1 did not, and the filter never
    appeared; since 13.0 they ride with the inventory, not the poll)."""
    code, r = call(welt["port"], "GET", "/api/v1/inventory")
    assert code == 200
    # The mail lines are one word, `mail_lines` – all of store_layout.MAIL_NEU.
    assert {"key", "who_mail", "domains", "thread",
            "mail_lines"} <= set(r["store"]["features"]), r["store"]["features"]


def test_bemerkung_am_eintrag_ueber_die_app(welt):
    port = welt["port"]
    fid = _fall(welt)
    hit = _treffer(welt)["results"][0]
    call(port, "POST", f"/api/v1/cases/{fid}/items", {"items": [_eintrag(hit)]})
    eid = _eid(port, fid, hit["key"])
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/items/{eid}", {"remark": " Der Beleg "})
    assert code == 200 and r["case"]["item_list"][0]["remark"] == "Der Beleg"
    # An item that is not in this case is not there: the id decides.
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/items/999", {"remark": "x"})
    assert code == 404 and r["error"]["k"] == "srv.case.noitem"
    assert call(port, "DELETE", f"/api/v1/cases/{fid}/items/999")[0] == 404
    code, r = call(port, "PATCH", f"/api/v1/cases/{fid}/items/{eid}", {"remark": ""})
    assert code == 200 and r["case"]["item_list"][0]["remark"] == ""


def test_export_ordner_ohne_export(welt, monkeypatch):
    port = welt["port"]
    fid = _fall(welt)
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/export/open")
    assert code == 404 and r["error"]["k"] == "srv.case.noexport"
    (welt["sandbox"] / "exporte").mkdir()
    geoeffnet = []
    monkeypatch.setattr(app_mod.archive_check, "ordner_oeffnen", lambda p: geoeffnet.append(str(p)) or True)
    code, r = call(port, "POST", f"/api/v1/cases/{fid}/export/open")
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
    code, r = call(port, "PATCH", "/api/v1/config",
                   {"case_export_dir": "  /tmp/x  ", "mcp_cases_write": True, "search_history": "30",
                    "internal_domains": " example.com, example.org ", "own_name": " Carla Chef "})
    cfg = r["config"]
    assert cfg["case_export_dir"] == "/tmp/x" and cfg["mcp_cases_write"] is True and cfg["search_history"] == "30"
    assert cfg["internal_domains"] == "example.com, example.org" and cfg["own_name"] == "Carla Chef"
    code, r = call(port, "PATCH", "/api/v1/config", {"case_export_dir": "", "mcp_cases_write": False})
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
    # the door's head is the name and its (i) – no explaining sentence beside it
    assert "cases.sub" not in seite and 'data-i18n-title="cases.i"' in block
    # the tour's case chapter: in the full tour's order, and offered by the empty door
    assert "var TOUR_REIHE = ['archiv', 'quelle', 'suche', 'faelle', 'insights', 'claude'];" in seite
    assert "tourStart(\\'faelle\\')" in seite.split("function zeichneFaelle")[1].split("function geschlosseneZeigen")[0]
    # the overview: a quiet label and the one arrow – no counts, no filter
    kopf = block[block.index('class="liste-kopf"'):block.index('id="faelle-liste"')]
    assert 'data-i18n="cases.overview"' in kopf and 'id="leiste-knopf"' in kopf
    assert "faelle-stand" not in kopf and "<input" not in kopf
    # the case's frame: the filter field, the fold buttons, "New folder…", the move bar
    for kennung in ("fall-filter", "fall-ordner-neu", "fall-verschieben", "fall-auswahl", "fall-fuss",
                    "fall-sichten", "fall-zeit", "fall-personen", "fall-zeit-richtung"):
        assert f'id="{kennung}"' in block, kennung
    # the three views are the strip the search page uses, under the head and above the tools
    assert block.index('id="fall-kopf"') < block.index('class="sichten fall-sichten"') < block.index('id="fall-filter"')
    assert block.count('data-fallsicht=') == 3
    # the case filter counts but does not search, like every filter – and
    # starts hidden: it appears with the first case
    feld = re.search(r'<select id="f-fall"[^>]*>', seite).group(0)
    assert "zeigeFilterstand()" in feld and "doSearch" not in feld and 'class="hide"' in feld
    # every setting row has its (i)
    for key in ("search_history", "case_export_dir", "mcp_cases_write"):
        assert f'data-i18n-title="settings.{key}.i"' in seite and f'id="c-{key}"' in seite


PRUEFUNG_SEITE = GRUNDZUSTAND + """
var anfragen = [];
var FAELLE_ANTWORT = {items: [
  {id: 1, name: 'Nordwind', status: 'open', items: 3, items_by_source: {outlook: 2, teams: 1}, lists: 0, notes: 1, searches: 1,
   folders: 1, folder_list: [{id: 3, name: 'Belege', created: '2026-09-02T10:00:00+00:00', items: 1}],
   created: '2026-09-01T10:00:00+00:00', changed: '2026-09-14T10:00:00+00:00', closed_at: null},
  {id: 2, name: 'Alt', status: 'closed', items: 1, items_by_source: {outlook: 1}, lists: 0, notes: 0, searches: 0, folders: 0, folder_list: [],
   created: '2026-01-01T10:00:00+00:00', changed: '2026-02-01T10:00:00+00:00', closed_at: '2026-02-01T10:00:00+00:00'}
 ]};
var TREFFER_ANTWORT = {count: 2, results: [
  {uid: 'outlook:inbox/mail1.eml:0', key: 'mail:<m1@example.com>', source: 'outlook', root: 'outlook',
   path: 'inbox/mail1.eml', title: 'Rechnung 4711', date: '2025-06-10', who: 'Carla Chef', preview: 'Die Rechnung',
   thread: 'tix:1', domains: ['example.com', 'nordwind.example'], cases: [{id: 1, name: 'Nordwind', status: 'open'}]},
  {uid: 'teams:1on1/alice__abc.html:0', key: null, source: 'teams', root: 'teams',
   path: '1on1/alice__abc.html', title: 'Projekt Alpha', date: '2025-06-01', who: 'Alice', preview: 'Hallo', cases: []}
]};
global.fetch = function(pfad, opt){
  // The method belongs in the record since 13.0: the same path answers
  // GET, PATCH and DELETE, and which one was used is the point.
  anfragen.push({pfad: String(pfad), methode: (opt && opt.method) || 'GET',
                 body: opt && opt.body ? JSON.parse(opt.body) : null});
  var antwort = statusGeruest();
  if((/[/]api[/]v1[/]cases[/][0-9]+$/).test(String(pfad))) antwort = {case: Object.assign({item_list: [
      {id: 11, key: 'mail:<m1@example.com>', src: 'outlook', root: 'outlook', rel: 'inbox/mail1.eml', title: 'Rechnung 4711', date: '2025-06-10 08:00', who: 'Carla Chef', list: null, folder: 3, via: 'ui', remark: 'Der Beleg', thread_open: 2, who_mail: 'carla@example.com'},
      {id: 12, key: 'teams:x#1', src: 'teams', root: 'teams', rel: '1on1/alice__abc.html', title: 'Projekt Alpha', date: '2025-06-01 09:30', who: 'Alice', list: null, folder: null, via: 'mcp', remark: '', thread_open: 0, who_mail: 'alice@nordwind.example'}],
    list_list: [], note_list: [{id: 7, when: '2026-09-14T10:00:00+00:00', text: 'Erste Notiz', via: 'mcp'}],
    search_list: [{id: 5, name: 'Rechnungen', criteria: {q: 'Rechnung', mode: 'text', person: '', source: 'outlook', from: '', to: '', folder: '', filetype: '', gone: false, case: null, case_folder: null}, last_run: null, hits: null, case: 1, case_name: 'Nordwind', case_folder: 3, case_folder_name: 'Belege'}]},
    FAELLE_ANTWORT.items[(/cases[/]2$/).test(String(pfad)) ? 1 : 0])};
  else if(String(pfad) === '/api/v1/cases') antwort = FAELLE_ANTWORT;
  // The environment is fetched once and holds the paths – without this the
  // status skeleton would overwrite it on the first poll.
  else if(String(pfad) === '/api/v1/app') antwort = UMGEBUNG;
  else if(String(pfad).indexOf('/api/v1/search?') === 0) antwort = TREFFER_ANTWORT;
  else if(String(pfad).indexOf('/api/v1/searches/history') === 0) antwort = {retention: '90', items: [
      {id: 1, when: new Date().toISOString(), hits: 4, criteria: {q: 'Rechnung', mode: 'ki', person: 'Alice', source: 'outlook', from: '', to: '', folder: '', filetype: '', gone: false, case: null}},
      {id: 2, when: '2026-01-05T10:00:00+00:00', hits: 0, criteria: {q: '', mode: 'text', person: '', source: 'all', from: '2025-01-01', to: '', folder: '', filetype: 'pdf', gone: true, case: 1}}]};
  else if(String(pfad).indexOf('/api/v1/searches/saved') === 0) antwort = {items: [
      {id: 5, name: 'Rechnungen', criteria: {q: 'Rechnung', mode: 'text', person: '', source: 'outlook', from: '', to: '', folder: '', filetype: '', gone: false, case: null}, last_run: '2026-09-14T10:00:00+00:00', hits: 4, case: 1, case_name: 'Nordwind'},
      {id: 6, name: 'Lose', criteria: {q: 'x', mode: 'text', person: '', source: 'all', from: '', to: '', folder: '', filetype: '', gone: false, case: null}, last_run: null, hits: null, case: null, case_name: null}]};
  else if(String(pfad).indexOf('/api/v1/threads?') === 0) antwort = {thread: 'tix:1', count: 3, items: [
      {key: 'mail:<m1@example.com>', path: 'inbox/mail1.eml', cases: [{id: 1, name: 'Nordwind', status: 'offen'}]},
      {key: 'mail:<m3@example.com>', path: 'inbox/mail3.eml', cases: []},
      {key: 'mail:<m4@example.com>', path: 'inbox/mail4.eml', cases: []}]};
  else if(opt && opt.method === 'POST') antwort = {ok: true, id: 9, added: 2, already: 0, hits: 2, case: FAELLE_ANTWORT.items[0]};
  return Promise.resolve({json: function(){ return Promise.resolve(antwort); }});
};
global.confirm = function(){ return true; };
global.prompt = function(text, wert){ return 'Neu benannt'; };
function warte(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
function letzte(pfad, methode, feld){
  // One path, several jobs: items come and whole conversations come, both
  // as a POST on the same collection – `feld` says which of them is meant.
  return anfragen.filter(function(a){
    return a.pfad.indexOf(pfad) === 0 && (!methode || a.methode === methode) &&
           (!feld || (a.body && a.body[feld]));
  }).pop();
}

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
  pruefe(optionen.indexOf('value="1/3">') >= 0 && optionen.indexOf('Belege') >= 0, 'Ordner fehlt im Fallfilter: ' + optionen);
  kriterienAnwenden({case: 1, case_folder: 3});
  pruefe(el('f-fall').value === '1/3' && kriterienAusForm().case_folder === 3 && kriterienAusForm().case === 1, 'Ordnerkriterium geht nicht durch das Formular');
  kriterienAnwenden({});
  // the parties filter: a select like the others; criteria and the request carry it
  el('f-party').classList.remove('hide');
  kriterienAnwenden({party: 'external'});
  pruefe(el('f-party').value === 'external' && kriterienAusForm().party === 'external' && filterFelder().indexOf('external') >= 0, 'Beteiligtenfilter geht nicht durch das Formular');
  pruefe(kriterienTags({party: 'internal'}).indexOf('Beteiligte') >= 0, 'Kriterien-Tag der Beteiligten fehlt');
  doSearch(0);
  await warte(10);
  pruefe(letzte('/api/v1/search?').pfad.indexOf('party=external') > 0, 'party fehlt in der Anfrage');
  filterLeeren();
  pruefe(el('f-party').value === 'all' && kriterienAusForm().party === 'all', 'Zuruecksetzen laesst den Beteiligtenfilter stehen');

  // --- filters: the case counts, clears, sends
  el('f-fall').value = '1';
  pruefe(filterFelder().indexOf('1') >= 0, 'Fallfilter zaehlt nicht');
  zeigeFilterstand();
  pruefe(el('f-fall').classList.contains('on'), 'Fallfilter leuchtet nicht');
  pruefe(!el('filter-weg').classList.contains('hide'), 'Zuruecksetzen fehlt');
  el('q').value = 'Rechnung';
  doSearch(0, 5);
  var such = letzte('/api/v1/search?');
  pruefe(such && such.pfad.indexOf('case=1') > 0, 'case fehlt in der Anfrage: ' + (such && such.pfad));
  pruefe(such.pfad.indexOf('saved=5') > 0, 'saved fehlt in der Anfrage');
  await warte(5);
  filterLeeren();
  pruefe(el('f-fall').value === '', 'Zuruecksetzen leert den Fallfilter nicht');
  doSearch(20);
  pruefe(letzte('/api/v1/search?').pfad.indexOf('saved=') < 0 || letzte('/api/v1/search?').pfad.indexOf('saved=&') > 0, 'saved auf Seite 2');
  await warte(5);

  // --- hits: tick, mark, head
  var liste = el('results').innerHTML;
  pruefe(liste.indexOf('class="wahl"') >= 0, 'kein Haken am Treffer');
  // "external": a party outside the signed-in domain (a@example.com) – on the one hit that has parties
  pruefe(liste.split('tag extern').length - 1 === 1 && liste.indexOf('Rechnung 4711') < liste.indexOf('tag extern'), 'extern-Marke am Treffer fehlt oder doppelt');
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
  var hinzu = letzte('/api/v1/cases/1/items', 'POST', 'items');
  pruefe(hinzu, 'nichts an den Fall geschickt');
  pruefe(hinzu.body.items.length === 1 && hinzu.body.items[0].key === 'mail:<m1@example.com>', 'falscher Eintrag');
  pruefe(hinzu.body.items[0].rel === 'inbox/mail1.eml' && hinzu.body.items[0].title === 'Rechnung 4711', 'Eintrag ohne Pfad/Titel');
  pruefe(Object.keys(AUSWAHL).length === 0, 'Auswahl nach dem Hinzufuegen nicht geleert');
  pruefe(!el('treffer-meldung').classList.contains('hide'), 'keine Rueckmeldung');
  // the whole list: the criteria go, plus the attached search when asked
  el('q').value = 'Rechnung';
  fallWahl('liste');
  await warte(5);
  pruefe(modal.innerHTML.indexOf('fall-wahl-anhaengen') >= 0, 'kein Anhaengen-Schalter bei der Liste');
  fallWahlAusfuehren();
  await warte(10);
  var li = letzte('/api/v1/cases/1/lists', 'POST');
  pruefe(li && li.body.criteria.q === 'Rechnung' && li.body.criteria.mode === 'text', 'Liste ohne Kriterien: ' + JSON.stringify(li && li.body));
  pruefe(TREFFER[0].cases.length === 1, 'Marke doppelt gesetzt');

  // --- stored criteria set the form without searching (the status above
  // greyed the two rear modes: no Ollama – a stored mode needs them on)
  el('m-aehnlich').disabled = false; el('m-ki').disabled = false;
  var vorher = anfragen.length;
  kriterienAnwenden({q: 'Urlaub', mode: 'aehnlich', person: 'Alice', source: 'outlook', from: '2025-01-01', to: '', folder: 'inbox/x', filetype: 'pdf', gone: true, case: 2});
  pruefe(anfragen.length === vorher, 'kriterienAnwenden hat gesucht');
  pruefe(el('q').value === 'Urlaub' && el('f-person').value === 'Alice' && el('f-source').value === 'outlook', 'Felder nicht gesetzt');
  pruefe(el('f-from').value === '2025-01-01' && el('f-folder').value === 'inbox/x' && el('f-typ').value === 'pdf', 'Felder nicht gesetzt (2)');
  pruefe(el('f-gone').checked === true && el('f-fall').value === '2', 'Felder nicht gesetzt (3)');
  pruefe(SUCHMODUS === 'aehnlich' && el('m-aehnlich').classList.contains('on'), 'Suchart nicht gesetzt');
  var k = kriterienAusForm();
  pruefe(k.case === 2 && k.gone === true && k.mode === 'aehnlich' && k.folder === 'inbox/x', 'Kriterien aus dem Formular falsch: ' + JSON.stringify(k));
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
  pruefe(letzte('/api/v1/search?').pfad.indexOf('case=1') > 0, 'Verlaufssuche ohne Fall');
  historieFenster();
  await warte(10);
  historieLeeren();
  await warte(5);
  // Emptying the history removes it: DELETE on the resource itself.
  pruefe(letzte('/api/v1/searches/history', 'DELETE'), 'Leeren nicht als DELETE geschickt');

  // --- saved searches window
  gespeicherteFenster();
  await warte(10);
  html = modal.innerHTML;
  pruefe(html.indexOf('Rechnungen') >= 0 && html.indexOf('gespeichertLauf(0)') >= 0, 'gespeicherte nicht gezeichnet');
  pruefe(html.indexOf('gespeichertLoesen(0)') >= 0 && html.indexOf('gespeichertAnhaengen(1)') >= 0, 'Anhaengen/Loesen fehlen');
  pruefe(html.indexOf('Nordwind') >= 0, 'Fallname fehlt');
  gespeichertLauf(0);
  await warte(5);
  pruefe(letzte('/api/v1/search?').pfad.indexOf('saved=5') > 0, 'gespeicherte Suche laeuft ohne ihre Nummer');
  gespeicherteFenster();
  await warte(10);
  gespeichertUmbenennen(1);
  await warte(5);
  var umbenannt = letzte('/api/v1/searches/saved/6', 'PATCH');
  pruefe(umbenannt && umbenannt.body.name === 'Neu benannt', 'Umbenennen nicht geschickt');
  gespeichertLoeschen(1);
  await warte(5);
  pruefe(letzte('/api/v1/searches/saved/6', 'DELETE'), 'Loeschen nicht geschickt');
  // save the current search, attached to a case
  el('q').value = 'Neu';
  speichernFenster(kriterienAusForm(), 'gespeichert');
  html = modal.innerHTML;
  pruefe(html.indexOf('id="speichern-name"') >= 0 && html.indexOf('id="speichern-fall"') >= 0, 'Speicherfenster unvollstaendig');
  pruefe(html.indexOf('value="2"') < 0, 'geschlossener Fall im Speicherfenster');
  document.getElementById('speichern-name').value = 'Meine';
  speichernAusfuehren();
  await warte(10);
  var sp = letzte('/api/v1/searches/saved', 'POST');
  pruefe(sp && sp.body.name === 'Meine' && sp.body.criteria.q === 'Neu', 'Speichern nicht geschickt: ' + JSON.stringify(sp && sp.body));

  // --- the cases tab: the overview, then the case
  tab('faelle');
  await warte(10);
  pruefe(el('faelle-liste').innerHTML.indexOf('Nordwind') >= 0, 'Fallliste leer');
  pruefe(el('faelle-liste').innerHTML.indexOf('Alt') < 0, 'geschlossener Fall ohne Link sichtbar');
  pruefe(!el('faelle-zu-zeile').classList.contains('hide') && el('faelle-zu-link').textContent.indexOf('1') >= 0, 'Link zu den geschlossenen fehlt');
  geschlosseneZeigen();
  pruefe(el('faelle-liste').innerHTML.indexOf('Alt') >= 0, 'geschlossener Fall trotz Link unsichtbar');
  pruefe(!el('faelle-split').classList.contains('eng'), 'Uebersicht ohne offenen Fall schon eng');
  // picking a case narrows the overview to the names; the arrow widens it
  await fallWaehlen(1);
  pruefe(el('faelle-split').classList.contains('eng'), 'Uebersicht bleibt breit');
  pruefe(el('leiste-knopf').title.length > 3, 'Pfeil ohne Titel');
  leisteUmschalten();
  pruefe(!el('faelle-split').classList.contains('eng'), 'Pfeil verbreitert nicht');
  leisteUmschalten();
  pruefe(!el('fall-detail').classList.contains('hide'), 'Falldetail bleibt versteckt');
  // the head: name, status, edit – facts and description below, no fold
  pruefe(el('fall-kopf').innerHTML.indexOf('Nordwind') >= 0 && el('fall-kopf').innerHTML.indexOf('fallBearbeitenFenster()') >= 0, 'Kopf unvollstaendig');
  pruefe(el('fall-kopf').innerHTML.indexOf('<details') < 0, 'der Kopf klappt');
  html = el('fall-inhalt').innerHTML;
  // every group is a fold and starts closed
  pruefe(html.indexOf('<details class="gruppe"') >= 0 && html.indexOf('<details class="gruppe" open') < 0, 'Gruppen nicht zugeklappt');
  pruefe(html.indexOf('Erste Notiz') >= 0 && html.indexOf('id="notiz-neu"') >= 0, 'Fallbuch nicht gezeichnet');
  // folders: the case's own, then Unsorted; inside, the sources fold again
  pruefe(html.indexOf('Belege') >= 0 && html.indexOf('Unsortiert') >= 0, 'Ordner fehlen');
  pruefe(html.indexOf('<details class="quelle" open') >= 0, 'erste Quelle im Ordner nicht offen');
  pruefe(html.indexOf('Rechnung 4711') >= 0 && html.indexOf('Projekt Alpha') >= 0, 'Eintraege fehlen');
  pruefe(html.indexOf('/api/v1/files/content?root=outlook&amp;path=inbox%2Fmail1.eml') >= 0, 'Eintrag ohne Link ins Original');
  // "via MCP" sits on the item and on the note – nowhere else
  pruefe(html.split('via MCP').length - 1 === 2, 'via MCP nicht genau am Eintrag und an der Notiz: ' + (html.split('via MCP').length - 1));
  pruefe(html.indexOf('sucheImFall(1, 3)') >= 0 && html.indexOf('ordnerFenster(3)') >= 0 && html.indexOf('ordnerLoeschen(3)') >= 0, 'Ordnerwerkzeuge fehlen');
  pruefe(html.indexOf('ordnerFenster(0)') < 0, 'Unsortiert laesst sich umbenennen');
  // the remark stands under the item; the pen opens its window; the thread button names what is missing
  pruefe(html.indexOf('class="bem"') >= 0 && html.indexOf('Der Beleg') >= 0, 'Bemerkung fehlt in der Zeile');
  pruefe(html.indexOf("threadHolen('mail:") >= 0 && html.indexOf('+2') >= 0 && html.split('threadHolen(').length - 1 === 1, 'Thread-Knopf fehlt oder steht an der falschen Zeile');
  bemerkungFenster('mail:<m1@example.com>');
  pruefe(modal.innerHTML.indexOf('id="bemerkung-text"') >= 0 && modal.innerHTML.split('class="act"').length - 1 === 1, 'Bemerkungsfenster unvollstaendig');
  pruefe(modal.innerHTML.indexOf('bemerkungSenden(true)') >= 0, 'Entfernen fehlt bei bestehender Bemerkung');
  document.getElementById('bemerkung-text').value = 'Neu gesagt';
  bemerkungSenden(false);
  await warte(10);
  var bm = letzte('/api/v1/cases/1/items/11', 'PATCH');
  pruefe(bm && bm.body.remark === 'Neu gesagt', 'Bemerkung nicht geschickt: ' + JSON.stringify(bm && bm.body));
  await fallOeffnen(1);
  threadHolen('mail:<m1@example.com>');
  await warte(10);
  pruefe(letzte('/api/v1/cases/1/items', 'POST', 'threads').body.threads[0] === 'mail:<m1@example.com>', 'Thread nicht geholt');
  pruefe(el('meldung').classList.contains('an'), 'keine Meldung nach dem Thread');
  // the timeline: month headings, the note between the items, the remark, the direction, the filter
  await fallOeffnen(1);
  fallSicht('zeit');
  html = el('fall-zeit').innerHTML;
  pruefe(!el('fall-zeit').classList.contains('hide') && el('fall-inhalt').classList.contains('hide'), 'Zeitleiste nicht sichtbar');
  pruefe(html.indexOf('class="zeit-monat"') >= 0 && html.indexOf('notiz-zeit') >= 0 && html.indexOf('Erste Notiz') >= 0, 'Zeitleiste ohne Monat oder Notiz');
  // the activity band: one bar per month from the first item to the last note, the note as a dot; a click narrows the rows
  var band = el('fall-aktivitaet').innerHTML;
  pruefe(band.split('class="monat').length - 1 === 16 && band.indexOf('class="punkte"><i></i>') >= 0, 'Aktivitaetsband falsch: ' + band.split('class="monat').length);
  pruefe(band.indexOf('class="auswahl-zeile"') >= 0 && band.indexOf("zeitEimerWaehlen('2025-06')") >= 0, 'Balken ohne Monat oder Auswahlzeile');
  zeitEimerWaehlen('2026-09');
  pruefe(el('fall-zeit').innerHTML.indexOf('Rechnung 4711') < 0 && el('fall-zeit').innerHTML.indexOf('Erste Notiz') >= 0, 'Monatswahl grenzt die Zeilen nicht ein');
  pruefe(el('fall-aktivitaet').innerHTML.indexOf('class="monat leer on"') >= 0 && el('fall-aktivitaet').innerHTML.indexOf('zeitEimerWaehlen(null)') >= 0, 'gewaehlter Monat nicht markiert');
  zeitEimerWaehlen('2026-09');
  pruefe(FALL_ZEIT_EIMER === null && el('fall-zeit').innerHTML.indexOf('Rechnung 4711') >= 0, 'zweiter Klick laesst nicht los');
  pruefe(html.indexOf('Projekt Alpha') < html.indexOf('Rechnung 4711'), 'Zeitleiste nicht aelteste zuerst');
  pruefe(html.indexOf('class="bem"') >= 0 && html.indexOf('Belege') >= 0, 'Zeitleiste ohne Bemerkung oder Ordner');
  pruefe(!el('fall-werkzeug-zeit').classList.contains('hide') && el('fall-werkzeug-ordner').classList.contains('hide'), 'Werkzeuge folgen der Sicht nicht');
  fallZeitRichtung('neu');
  pruefe(el('fall-zeit').innerHTML.indexOf('Rechnung 4711') < el('fall-zeit').innerHTML.indexOf('Projekt Alpha'), 'Richtung wirkt nicht');
  fallZeitRichtung('alt');
  fallFiltern('Alpha');
  pruefe(el('fall-zeit').innerHTML.indexOf('Rechnung 4711') < 0 && el('fall-filter-stand').textContent.indexOf('1') >= 0, 'Filter wirkt nicht in der Zeitleiste');
  fallFiltern('');
  // the people: counted from what the items name; Timeline sets the filter, Search asks the archive
  KANN_ADRESSEN = true;
  S.config = S.config || {};
  fallSicht('personen');
  html = el('fall-personen').innerHTML;
  pruefe(html.indexOf('Carla Chef') >= 0 && html.indexOf('Alice') >= 0 && html.indexOf('cases.people') < 0, 'Personen nicht gezeichnet');
  pruefe(el('fall-personen-zahl').textContent === '2', 'Personenzahl fehlt in der Leiste: ' + el('fall-personen-zahl').textContent);
  // the picture: you at the left, four columns, every person a circle with initials; the 2025 items fall under "older"
  pruefe(html.indexOf('class="bild"') >= 0 && html.split('class="periode"').length - 1 === 4 && html.indexOf('class="ich"') >= 0, 'Bild ohne Spalten oder ohne dich');
  pruefe(html.split('class="knoten').length - 1 === 2 && html.indexOf('>CC</span>') >= 0 && html.indexOf('fallPersonWaehlen(') >= 0, 'Personenkreise fehlen');
  pruefe(html.indexOf('person-karte') < 0, 'Karte ohne Wahl');
  fallPersonWaehlen('Carla Chef');
  html = el('fall-personen').innerHTML;
  pruefe(html.indexOf('class="knoten on"') >= 0 && html.indexOf('person-karte') >= 0 && html.indexOf('carla@example.com') >= 0, 'Karte der gewaehlten Person fehlt');
  pruefe(html.indexOf("personZeit('Carla Chef')") >= 0 && html.indexOf("sucheImFall(1, null, 'Carla Chef')") >= 0, 'Wege in der Karte fehlen');
  fallPersonWaehlen('Carla Chef');
  pruefe(el('fall-personen').innerHTML.indexOf('person-karte') < 0, 'zweiter Klick schliesst die Karte nicht');
  html = el('fall-personen').innerHTML;
  // addresses, and "external" against the signed-in domain (a@example.com)
  // the address is not on the circle but in the card – and the filter reads it
  pruefe(html.indexOf('carla@example.com') < 0, 'Adresse am Kreis');
  fallFiltern('nordwind.example');
  pruefe(el('fall-personen').innerHTML.indexOf('Alice') >= 0 && el('fall-personen').innerHTML.indexOf('Carla Chef') < 0, 'Filter liest die Adresse nicht');
  fallFiltern('');
  html = el('fall-personen').innerHTML;
  pruefe(html.split('tag extern').length - 1 === 1 && html.indexOf('Alice<span class="tag extern"') >= 0, 'extern-Marke falsch: ' + html);
  pruefe(html.indexOf('cases.people.noaddr') < 0 && html.indexOf('Indexlauf') < 0, 'Hinweis trotz Adressen');
  // the setting overrides the domain; the name setting leaves you out
  S.config.internal_domains = 'nordwind.example';
  fallSicht('personen');
  pruefe(el('fall-personen').innerHTML.indexOf('Carla Chef<span class="tag extern"') >= 0 && el('fall-personen').innerHTML.indexOf('Alice<span class="tag extern"') < 0, 'interne Domains wirken nicht');
  S.config.internal_domains = '';
  S.config.own_name = 'carla chef';
  fallSicht('personen');
  pruefe(el('fall-personen').innerHTML.indexOf('Carla Chef') < 0 && el('fall-personen').innerHTML.indexOf('1 Personen in 2') >= 0, 'ich selbst nicht ausgenommen: ' + el('fall-personen').innerHTML);
  S.config.own_name = '';
  S.token.account = 'alice@nordwind.example';
  fallSicht('personen');
  pruefe(el('fall-personen').innerHTML.indexOf('Alice') < 0 && el('fall-personen').innerHTML.indexOf('Carla Chef<span class="tag extern"') >= 0, 'Konto-Adresse nimmt mich nicht aus');
  S.token.account = 'a@example.com';
  KANN_ADRESSEN = false;
  fallSicht('personen');
  pruefe(el('fall-personen').innerHTML.indexOf('Indexlauf') >= 0, 'kein Hinweis ohne Adressen im Index');
  KANN_ADRESSEN = true;
  fallSicht('personen');
  personZeit('Carla Chef');
  pruefe(FALL_SICHT === 'zeit' && el('fall-filter').value === 'Carla Chef', 'Zeitleiste der Person nicht gesetzt');
  pruefe(el('fall-zeit').innerHTML.indexOf('Rechnung 4711') >= 0 && el('fall-zeit').innerHTML.indexOf('Projekt Alpha') < 0, 'Zeitleiste der Person falsch');
  fallFiltern('');
  fallSicht('ordner');
  sucheImFall(1, null, 'Carla Chef');
  await warte(10);
  pruefe(el('f-person').value === 'Carla Chef' && letzte('/api/v1/search?').pfad.indexOf('person=Carla+Chef') > 0, 'Suche nach Person im Fall fehlt');
  tab('faelle');
  await fallOeffnen(1);
  html = el('fall-inhalt').innerHTML;
  pruefe(html.indexOf('fallEntfernen(') >= 0 && html.indexOf('class="wahl"') >= 0, 'Aktionen des offenen Falls fehlen');
  pruefe(html.indexOf('Rechnungen') >= 0 && html.indexOf('fallNeuPruefen()') >= 0, 'angehaengte Suche fehlt');
  pruefe(html.indexOf('fallSchreiben') < 0, 'roher Code im Markup');
  // the foot: export at the left, the case's fate at the right
  var fuss = el('fall-fuss').innerHTML;
  pruefe(fuss.indexOf('exportFenster()') >= 0 && fuss.indexOf('schliessenFenster()') >= 0 && fuss.indexOf('fallLoeschen()') >= 0, 'Fuss unvollstaendig');
  pruefe(fuss.indexOf('gruppe-links') < fuss.indexOf('gruppe-rechts'), 'Fuss nicht in zwei Gruppen');
  pruefe(!el('fall-ordner-neu').classList.contains('hide'), 'Neuer Ordner am offenen Fall versteckt');
  // expand all opens every fold, collapse all closes them
  alleFalten(true);
  pruefe(el('fall-inhalt').innerHTML.indexOf('<details class="gruppe" open') >= 0, 'Aufklappen wirkt nicht');
  alleFalten(false);
  pruefe(el('fall-inhalt').innerHTML.indexOf('<details class="gruppe" open') < 0, 'Zuklappen wirkt nicht');
  // the filter: only matching rows, every fold open, the count
  fallFiltern('4711');
  html = el('fall-inhalt').innerHTML;
  pruefe(html.indexOf('Rechnung 4711') >= 0 && html.indexOf('Projekt Alpha') < 0, 'Filter filtert nicht');
  pruefe(html.indexOf('<details class="gruppe" open') >= 0, 'Filter klappt nicht auf');
  // the folds stay the user's while the filter is on: collapse all still closes them
  alleFalten(false);
  pruefe(el('fall-inhalt').innerHTML.indexOf('<details class="gruppe" open') < 0 && el('fall-inhalt').innerHTML.indexOf('Rechnung 4711') >= 0, 'Zuklappen geht nicht bei gesetztem Filter');
  fallFiltern('4711');
  pruefe(el('fall-inhalt').innerHTML.indexOf('<details class="gruppe" open') < 0, 'derselbe Filter klappt erneut auf');
  fallFiltern('Rechnung');
  pruefe(el('fall-inhalt').innerHTML.indexOf('<details class="gruppe" open') >= 0, 'ein neuer Filter klappt nicht auf');
  pruefe(el('fall-filter-stand').textContent.indexOf('1') >= 0 && el('fall-filter-stand').textContent.indexOf('2') >= 0, 'Filterstand fehlt: ' + el('fall-filter-stand').textContent);
  fallFiltern('');
  pruefe(el('fall-inhalt').innerHTML.indexOf('Projekt Alpha') >= 0 && el('fall-filter-stand').textContent === '', 'Filter nicht aufgehoben');
  // ticked rows: the bar, "move to", the move
  pruefe(el('fall-auswahl').classList.contains('hide'), 'Auswahlleiste ohne Auswahl sichtbar');
  fallWahlZeile('teams:x#1', true);
  pruefe(!el('fall-auswahl').classList.contains('hide') && el('fall-auswahl-zahl').textContent.indexOf('1') >= 0, 'Auswahl nicht gezaehlt');
  pruefe(el('fall-verschieben').innerHTML.indexOf('value="3"') >= 0 && el('fall-verschieben').innerHTML.indexOf('value="neu"') >= 0, 'Verschieben-Auswahl unvollstaendig');
  fallVerschieben('3');
  await warte(10);
  var mv = letzte('/api/v1/cases/1/items', 'PATCH');
  pruefe(mv && mv.body.folder === 3 && mv.body.keys[0] === 'teams:x#1', 'Verschieben nicht geschickt: ' + JSON.stringify(mv && mv.body));
  pruefe(Object.keys(FALL_AUSWAHL).length === 0, 'Auswahl nach dem Verschieben nicht geleert');
  // a new folder from the tool row: an overlay with one primary button
  ordnerFenster(null);
  pruefe(modal.innerHTML.indexOf('id="ordner-name"') >= 0 && modal.innerHTML.split('class="act"').length - 1 === 1, 'Ordnerfenster unvollstaendig');
  document.getElementById('ordner-name').value = 'Verträge';
  ordnerSenden();
  await warte(10);
  pruefe(letzte('/api/v1/cases/1/folders', 'POST').body.name === 'Verträge', 'Ordner nicht angelegt');
  // "new folder" while moving: the folder first, then the rows into it
  fallWahlZeile('teams:x#1', true);
  fallVerschieben('neu');
  document.getElementById('ordner-name').value = 'Neu';
  anfragen.length = 0;
  ordnerSenden();
  await warte(20);
  pruefe(letzte('/api/v1/cases/1/folders', 'POST') && letzte('/api/v1/cases/1/items', 'PATCH'), 'neuer Ordner beim Verschieben ohne Verschieben');
  ordnerLoeschen(3);
  await warte(10);
  pruefe(letzte('/api/v1/cases/1/folders/3', 'DELETE'), 'Ordner nicht aufgeloest');
  await fallOeffnen(1);
  document.getElementById('notiz-neu').value = 'Zweite';
  notizHinzufuegen();
  await warte(10);
  pruefe(letzte('/api/v1/cases/1/notes', 'POST').body.text === 'Zweite', 'Notiz nicht geschickt');
  // a closed case: read-only, and without folders the sources fold at the top
  await fallOeffnen(2);
  html = el('fall-inhalt').innerHTML;
  pruefe(html.indexOf('fallEntfernen(') < 0 && html.indexOf('id="notiz-neu"') < 0 && html.indexOf('class="wahl"') < 0, 'geschlossener Fall laesst aendern');
  pruefe(html.indexOf('Unsortiert') < 0 && html.indexOf('<details class="gruppe"') >= 0, 'Fall ohne Ordner zeigt Ordner');
  pruefe(el('fall-fuss').innerHTML.indexOf('fallOeffnenWieder()') >= 0 && el('fall-fuss').innerHTML.indexOf('exportFenster()') >= 0, 'Wieder oeffnen/Export fehlen');
  pruefe(el('fall-ordner-neu').classList.contains('hide'), 'Neuer Ordner am geschlossenen Fall');
  // search in this case, and in one folder: the filter is set, the search fired
  sucheImFall(1);
  await warte(10);
  pruefe(el('f-fall').value === '1' && letzte('/api/v1/search?').pfad.indexOf('case=1') > 0, 'Suche im Fall setzt den Filter nicht');
  sucheImFall(1, 3);
  await warte(10);
  pruefe(el('f-fall').value === '1/3' && letzte('/api/v1/search?').pfad.indexOf('case=1&case_folder=3') > 0, 'Suche im Ordner ohne Ordner: ' + letzte('/api/v1/search?').pfad);
  // export window: the overview, one primary button – always a ZIP, no switch
  await fallOeffnen(1);
  // Where an export lands comes from the environment (/api/v1/app); the
  // page fetched that once at start, with the stub of the moment.
  UMGEBUNG.case_export_dir = '/tmp/exporte';
  exportFenster();
  html = modal.innerHTML;
  pruefe(html.split('class="act"').length - 1 === 1 && html.indexOf('.zip') >= 0, 'Exportfenster unvollstaendig');
  pruefe(html.indexOf('index.html') >= 0 && html.indexOf('items.csv') >= 0 && html.indexOf('casebook.md') >= 0 && html.indexOf('Belege') >= 0, 'Exportuebersicht unvollstaendig');
  pruefe(html.indexOf('/tmp/exporte') >= 0, 'Exportordner nicht genannt');
  pruefe(html.indexOf('zahnrad') < 0 && html.indexOf('type="checkbox"') < 0, 'Exportfenster mit Schalter oder Zahnrad');
  fallExportStarten(true);
  await warte(10);
  var ex = letzte('/api/v1/cases/1/export', 'POST');
  // The case is in the path now; the body says nothing, and no ZIP switch.
  pruefe(ex && !('zip' in ex.body), 'Export nicht geschickt: ' + JSON.stringify(ex && ex.body));
  pruefe(LAUF.eigener === true, 'Der Export oeffnet das Lauffenster nicht');
  // close with export first
  schliessenFenster();
  pruefe(modal.innerHTML.indexOf('fall-zu-export') >= 0, 'Schliessfenster ohne Exportschalter');
  document.getElementById('fall-zu-export').checked = true;
  fallSchliessen();
  await warte(20);
  var zu = letzte('/api/v1/cases/1', 'PATCH');
  pruefe(zu && zu.body.status === 'closed', 'Schliessen nicht geschickt');
  // new case window: one primary, posts, opens the case
  fallNeuFenster();
  pruefe(modal.innerHTML.split('class="act"').length - 1 === 1, 'Neuer-Fall-Fenster ohne genau einen Knopf');
  document.getElementById('fall-name').value = 'Berlin';
  fallFormularSenden();
  await warte(20);
  pruefe(letzte('/api/v1/cases', 'POST').body.name === 'Berlin', 'Anlegen nicht geschickt');
  // the add-to-case window: the folder row follows the chosen case, the
  // conversation switch counts what the case lacks
  KANN_VERLAUF = true;
  fallWahl('einer', 0);
  await warte(20);
  html = modal.innerHTML;
  pruefe(html.indexOf('id="fall-wahl-ordner"') >= 0 && html.indexOf('id="fall-wahl-ordner-neu"') >= 0, 'Ordnerzeile fehlt im Auswahlfenster');
  var faden = document.getElementById('fall-wahl-thread');
  pruefe(!faden.classList.contains('hide') && faden.innerHTML.indexOf('fall-wahl-thread-kipp') >= 0 && faden.innerHTML.indexOf('2') >= 0, 'Gespraechsschalter fehlt: ' + faden.innerHTML);
  pruefe(el('modal').innerHTML.indexOf('value="3"') >= 0 || document.getElementById('fall-wahl-ordner').innerHTML.indexOf('value="3"') >= 0, 'Ordner des Falls fehlen in der Ordnerzeile');
  document.getElementById('fall-wahl-ordner').value = '3';
  fallWahlAusfuehren();
  await warte(20);
  var hz = letzte('/api/v1/cases/1/items', 'POST', 'items');
  pruefe(hz && hz.body.folder === 3, 'Ordner nicht mitgeschickt: ' + JSON.stringify(hz && hz.body));
  var th = letzte('/api/v1/cases/1/items', 'POST', 'threads');
  pruefe(th && th.body.threads[0] === 'mail:<m1@example.com>', 'Gespraech nach dem Hinzufuegen nicht geholt');
  fallLoeschen();
  await warte(10);
  pruefe(letzte('/api/v1/cases/1', 'DELETE'), 'Loeschen nicht geschickt');
  pruefe(el('fall-detail').classList.contains('hide'), 'Detail nach dem Loeschen offen');
  pruefe(!el('faelle-split').classList.contains('eng'), 'Uebersicht nach dem Loeschen eng');

  // --- settings: the three fields travel with the save
  cfgGefuellt = false;
  fuelleEinstellungen(Object.assign({}, statusGeruest().config, {search_history: '365', case_export_dir: '/x', mcp_cases_write: true}));
  pruefe(el('c-search_history').value === '365' && el('c-case_export_dir').value === '/x' && el('c-mcp_cases_write').checked === true, 'Einstellungen nicht gefuellt');
  speichereEinstellungen();
  await warte(10);
  var cfg = letzte('/api/v1/config').body;
  pruefe(cfg.search_history === '365' && cfg.case_export_dir === '/x' && cfg.mcp_cases_write === true, 'Einstellungen nicht gespeichert: ' + JSON.stringify(cfg));

  // --- ein einzelner Eintrag verlaesst den Fall ueber seine id
  await fallOeffnen(1);
  await warte(10);
  anfragen.length = 0;
  fallEntfernen('mail:<m1@example.com>');
  await warte(10);
  var weg = letzte('/api/v1/cases/1/items/', 'DELETE');
  pruefe(weg, 'Einzelnes Entfernen nicht geschickt: ' + JSON.stringify(anfragen.map(function(a){ return a.methode + ' ' + a.pfad; })));
  pruefe(weg.pfad === '/api/v1/cases/1/items/11', 'Falscher Pfad: ' + weg.pfad);

  // --- die angehaengte Suche spricht englisch (last_run, hits, folder_name)
  await fallOeffnen(1);
  await warte(10);
  FALL_OFFEN.search_list[0].last_run = '2026-09-14T10:00:00+00:00';
  FALL_OFFEN.search_list[0].hits = 4;
  zeichneFall();
  fallSicht('liste');
  var sh = el('fall-inhalt').innerHTML;
  pruefe(sh.indexOf(t('search.saved.never')) < 0,
         'Gelaufene Suche steht als "nie gelaufen"');
  pruefe(sh.indexOf(t('search.saved.lastrun', {when: wannKurz('2026-09-14T10:00:00+00:00'), n: '4'})) >= 0,
         'Lauf und Trefferzahl fehlen: ' + sh.slice(0, 300));
  // Der Zielordner steht in der Marke "legt ab in X" – nur wenn es ihn gibt.
  pruefe(sh.indexOf(t('cases.search.filesinto', {name: 'Belege'})) >= 0,
         'Zielordner der Suche fehlt');
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
    assert store_layout.mit_schluesseln(db) is False, "11.0: keys, but no sender addresses yet"
    assert index_schritt()["ziel"] is None
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE chunks ADD COLUMN who_mail TEXT")
    con.execute("ALTER TABLE chunks ADD COLUMN domains TEXT")
    con.commit()
    con.close()
    assert store_layout.mit_schluesseln(db) is False, "11.1: addresses, but no mail lines yet"
    assert index_schritt()["ziel"] is None
    con = sqlite3.connect(db)
    for spalte in store_layout.MAIL_NEU:
        con.execute(f"ALTER TABLE chunks ADD COLUMN {spalte} TEXT")
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


# --------------------------------------------------------------------------
# The hit's detail (11.2): the facts per kind through the server
# --------------------------------------------------------------------------
def test_http_detail_liefert_die_fakten_je_art(welt):
    from urllib.parse import quote
    port = welt["port"]
    h = _treffer(welt, "Rechnung", source="outlook")["results"][0]
    code, d = call(port, "GET", "/api/v1/documents/facts?uid=" + quote(h["uid"], safe=""))
    assert code == 200 and d["kind"] == "outlook" and d["uid"] == h["uid"]
    assert d["from"] == {"name": "Carla Chef", "mail": "carla@example.com"}
    assert d["to"] == [{"name": "", "mail": "alice@example.com"}] and d["cc"] == []
    assert d["folder"] == "inbox" and "4711" in d["text"] and d["attachments"] == []
    # a chat message: who, when, which chat – from the row alone
    h = _treffer(welt, "Rechnung", source="teams")["results"][0]
    code, d = call(port, "GET", "/api/v1/documents/facts?uid=" + quote(h["uid"], safe=""))
    assert code == 200 and d["kind"] == "teams" and d["from"]["name"] and d["chat"]
    # an unknown uid is a 404 with the reason, never a 500
    code, d = call(port, "GET", "/api/v1/documents/facts?uid=nix")
    assert code == 404 and d["error"]["k"] == "srv.detail.none"
