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
import urllib.parse

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
    assert mail2["cases"] == [{"id": fid, "name": "Nordwind", "status": "offen", "ordner": None}]
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
    code, r = call(port, "POST", "/api/faelle/export", {"id": fid})
    assert code == 200 and r["ok"] and r["message"] is None, r
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
    code, r = call(port, "POST", "/api/faelle/export", {"id": fid})
    assert code == 200 and r["ok"]
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
    code, r = call(port, "POST", "/api/faelle/export", {"id": fid})
    assert code == 409 and r["message"]["k"] == "srv.busy"


def test_ordner_im_fall(welt):
    """Folders: made, renamed, dropped; items move in and out; a list and
    a saved search file into one; the search narrows to one."""
    port = welt["port"]
    fid = _fall(welt)
    code, r = call(port, "POST", "/api/faelle/ordner-anlegen", {"id": fid, "name": " Belege "})
    assert code == 200 and r["ok"] and r["case"]["ordner_liste"][0]["name"] == "Belege"
    belege = r["ordner"]
    code, r = call(port, "POST", "/api/faelle/ordner-anlegen", {"id": fid, "name": ""})
    assert code == 400 and r["message"]["k"] == "srv.case.noname"
    vertraege = call(port, "POST", "/api/faelle/ordner-anlegen", {"id": fid, "name": "Verträge"})[1]["ordner"]
    code, r = call(port, "POST", "/api/faelle/ordner-umbenennen", {"id": fid, "ordner": vertraege, "name": "Belege"})
    assert code == 409 and r["message"]["k"] == "srv.case.folder.exists"
    code, r = call(port, "POST", "/api/faelle/ordner-umbenennen", {"id": fid, "ordner": 999, "name": "x"})
    assert code == 404 and r["message"]["k"] == "srv.case.nofolder"
    code, r = call(port, "POST", "/api/faelle/ordner-umbenennen", {"id": fid, "ordner": vertraege, "name": "Contracts"})
    assert code == 200 and [o["name"] for o in r["case"]["ordner_liste"]] == ["Belege", "Contracts"]
    # items into a folder, straight from the search and by moving
    hits = _treffer(welt, "")["results"]
    mail1 = next(h for h in hits if h["path"] == "inbox/mail1.eml")
    mail2 = next(h for h in hits if h["path"] == "inbox/mail2.eml")
    code, r = call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(mail1)], "ordner": belege})
    assert code == 200 and r["added"] == 1
    call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(mail2)]})
    code, r = call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(mail2)], "ordner": 999})
    assert code == 404 and r["message"]["k"] == "srv.case.nofolder"
    fall = call(port, "GET", f"/api/faelle/fall?id={fid}")[1]["case"]
    je = {e["key"]: e["ordner"] for e in fall["eintraege_liste"]}
    assert je[mail1["key"]] == belege and je[mail2["key"]] is None
    assert [(o["name"], o["anzahl"]) for o in fall["ordner_liste"]] == [("Belege", 1), ("Contracts", 0)]
    assert all(e["quelle"] == "ui" for e in fall["eintraege_liste"])
    code, r = call(port, "POST", "/api/faelle/verschieben", {"id": fid, "keys": [mail2["key"]], "ordner": vertraege})
    assert code == 200 and r["moved"] == 1
    # the search narrows to the folder; the mark names it
    im_ordner = _treffer(welt, "", case=str(fid), case_folder=str(belege))["results"]
    assert [h["path"] for h in im_ordner] == ["inbox/mail1.eml"]
    assert im_ordner[0]["cases"] == [{"id": fid, "name": "Nordwind", "status": "offen", "ordner": "Belege"}]
    assert "no folder" in _treffer(welt, "", case=str(fid), case_folder="999")["error"]
    h = call(port, "GET", "/api/suche/historie")[1]["searches"][0]["kriterien"]
    assert h["fall"] == fid and h["ordner"] == belege
    # a list and a saved search file into a folder
    code, r = call(port, "POST", "/api/faelle/liste", {"id": fid, "kriterien": {"q": "Rechnung", "mode": "text"}, "ordner": belege})
    assert code == 200 and r["ok"]
    sid = call(port, "POST", "/api/suche/speichern",
               {"name": "R", "kriterien": {"q": "Rechnung"}, "fall": fid, "ordner": belege})[1]["id"]
    fall = call(port, "GET", f"/api/faelle/fall?id={fid}")[1]["case"]
    assert fall["listen_liste"][0]["ordner"] == belege and fall["suchen_liste"][0]["ordner_name"] == "Belege"
    code, r = call(port, "POST", "/api/suche/anhaengen", {"id": sid, "fall": fid, "ordner": 999})
    assert code == 404 and r["message"]["k"] == "srv.case.nofolder"
    neu = call(port, "GET", f"/api/faelle/neu?id={fid}")[1]["searches"][0]
    assert neu["ordner"] == belege and neu["ordner_name"] == "Belege"
    # dropping the folder keeps its items, unsorted
    code, r = call(port, "POST", "/api/faelle/ordner-loeschen", {"id": fid, "ordner": belege})
    assert code == 200 and [o["name"] for o in r["case"]["ordner_liste"]] == ["Contracts"]
    assert r["case"]["eintraege"] >= 2 and all(e["ordner"] in (None, vertraege) for e in r["case"]["eintraege_liste"])
    call(port, "POST", "/api/faelle/schliessen", {"id": fid})
    assert call(port, "POST", "/api/faelle/ordner-anlegen", {"id": fid, "name": "x"})[0] == 409


def test_gespraech_in_den_fall(welt_thread):
    """The rest of a conversation follows an item into its folder; the case
    says per item how much of its conversation it lacks; the conversation
    route marks every message with its cases."""
    welt = welt_thread
    port = welt["port"]
    fid = _fall(welt)
    belege = call(port, "POST", "/api/faelle/ordner-anlegen", {"id": fid, "name": "Belege"})[1]["ordner"]
    hits = _treffer(welt, "")["results"]
    m1 = next(h for h in hits if h["path"] == "inbox/mail1.eml")
    m3 = next(h for h in hits if h["path"] == "inbox/mail3.eml")
    assert m1["thread"] and m1["thread"] == m3["thread"]
    call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(m1)], "ordner": belege})
    fall = call(port, "GET", f"/api/faelle/fall?id={fid}")[1]["case"]
    assert {e["key"]: e["thread_offen"] for e in fall["eintraege_liste"]} == {m1["key"]: 1}
    assert fall["eintraege_liste"][0]["wer_mail"] == "carla@example.com"
    # the conversation's messages say which cases hold them
    code, r = call(port, "GET", "/api/thread?key=" + urllib.parse.quote(m1["thread"], safe=""))
    assert code == 200 and r["count"] == 2
    assert {m["path"]: [c["id"] for c in m["cases"]] for m in r["messages"]} == \
        {"inbox/mail1.eml": [fid], "inbox/mail3.eml": []}
    code, r = call(port, "POST", "/api/faelle/thread", {"id": fid, "keys": [m1["key"], "nix"]})
    assert code == 200 and r["added"] == 1
    je = {e["key"]: (e["ordner"], e["thread_offen"]) for e in r["case"]["eintraege_liste"]}
    assert je == {m1["key"]: (belege, 0), m3["key"]: (belege, 0)}
    assert call(port, "POST", "/api/faelle/thread", {"id": fid, "keys": [m1["key"]]})[1]["added"] == 0
    call(port, "POST", "/api/faelle/schliessen", {"id": fid})
    assert call(port, "POST", "/api/faelle/thread", {"id": fid, "keys": [m1["key"]]})[0] == 409


def test_bemerkung_am_eintrag_ueber_die_app(welt):
    port = welt["port"]
    fid = _fall(welt)
    hit = _treffer(welt)["results"][0]
    call(port, "POST", "/api/faelle/hinzufuegen", {"id": fid, "eintraege": [_eintrag(hit)]})
    code, r = call(port, "POST", "/api/faelle/bemerkung", {"id": fid, "key": hit["key"], "text": " Der Beleg "})
    assert code == 200 and r["case"]["eintraege_liste"][0]["bemerkung"] == "Der Beleg"
    code, r = call(port, "POST", "/api/faelle/bemerkung", {"id": fid, "key": "nix", "text": "x"})
    assert code == 404 and r["message"]["k"] == "srv.case.noitem"
    code, r = call(port, "POST", "/api/faelle/bemerkung", {"id": fid, "key": hit["key"], "text": ""})
    assert code == 200 and r["case"]["eintraege_liste"][0]["bemerkung"] == ""


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
                   {"case_export_dir": "  /tmp/x  ", "mcp_cases_write": True, "search_history": "30",
                    "internal_domains": " example.com, example.org ", "own_name": " Carla Chef "})
    cfg = r["config"]
    assert cfg["case_export_dir"] == "/tmp/x" and cfg["mcp_cases_write"] is True and cfg["search_history"] == "30"
    assert cfg["internal_domains"] == "example.com, example.org" and cfg["own_name"] == "Carla Chef"
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
var FAELLE_ANTWORT = {cases: [
  {id: 1, name: 'Nordwind', status: 'offen', eintraege: 3, je_quelle: {outlook: 2, teams: 1}, listen: 0, notizen: 1, suchen: 1,
   ordner: 1, ordner_liste: [{id: 3, name: 'Belege', angelegt: '2026-09-02T10:00:00+00:00', anzahl: 1}],
   angelegt: '2026-09-01T10:00:00+00:00', geaendert: '2026-09-14T10:00:00+00:00', geschlossen: null},
  {id: 2, name: 'Alt', status: 'zu', eintraege: 1, je_quelle: {outlook: 1}, listen: 0, notizen: 0, suchen: 0, ordner: 0, ordner_liste: [],
   angelegt: '2026-01-01T10:00:00+00:00', geaendert: '2026-02-01T10:00:00+00:00', geschlossen: '2026-02-01T10:00:00+00:00'}
 ], export_dir: '/tmp/exporte'};
var TREFFER_ANTWORT = {count: 2, results: [
  {uid: 'outlook:inbox/mail1.eml:0', key: 'mail:<m1@example.com>', source: 'outlook', root: 'outlook',
   path: 'inbox/mail1.eml', title: 'Rechnung 4711', date: '2025-06-10', who: 'Carla Chef', preview: 'Die Rechnung',
   thread: 'tix:1', domains: ['example.com', 'nordwind.example'], cases: [{id: 1, name: 'Nordwind', status: 'offen'}]},
  {uid: 'teams:1on1/alice__abc.html:0', key: null, source: 'teams', root: 'teams',
   path: '1on1/alice__abc.html', title: 'Projekt Alpha', date: '2025-06-01', who: 'Alice', preview: 'Hallo', cases: []}
]};
global.fetch = function(pfad, opt){
  anfragen.push({pfad: String(pfad), body: opt && opt.body ? JSON.parse(opt.body) : null});
  var antwort = statusGeruest();
  if(String(pfad).indexOf('/api/faelle/fall?') === 0) antwort = {case: Object.assign({eintraege_liste: [
      {key: 'mail:<m1@example.com>', src: 'outlook', root: 'outlook', rel: 'inbox/mail1.eml', titel: 'Rechnung 4711', datum: '2025-06-10 08:00', wer: 'Carla Chef', liste: null, ordner: 3, quelle: 'ui', bemerkung: 'Der Beleg', thread_offen: 2, wer_mail: 'carla@example.com'},
      {key: 'teams:x#1', src: 'teams', root: 'teams', rel: '1on1/alice__abc.html', titel: 'Projekt Alpha', datum: '2025-06-01 09:30', wer: 'Alice', liste: null, ordner: null, quelle: 'mcp', bemerkung: '', thread_offen: 0, wer_mail: 'alice@nordwind.example'}],
    listen_liste: [], notizen_liste: [{id: 7, wann: '2026-09-14T10:00:00+00:00', text: 'Erste Notiz', quelle: 'mcp'}],
    suchen_liste: [{id: 5, name: 'Rechnungen', kriterien: {q: 'Rechnung', mode: 'text', person: '', source: 'outlook', from: '', to: '', folder: '', filetype: '', gone: false, fall: null, ordner: null}, zuletzt: null, treffer: null, fall: 1, fall_name: 'Nordwind', ordner: 3, ordner_name: 'Belege'}]},
    FAELLE_ANTWORT.cases[String(pfad).indexOf('id=2') > 0 ? 1 : 0])};
  else if(String(pfad) === '/api/faelle') antwort = FAELLE_ANTWORT;
  else if(String(pfad).indexOf('/api/search?') === 0) antwort = TREFFER_ANTWORT;
  else if(String(pfad) === '/api/suche/historie') antwort = {retention: '90', searches: [
      {id: 1, wann: new Date().toISOString(), treffer: 4, kriterien: {q: 'Rechnung', mode: 'ki', person: 'Alice', source: 'outlook', from: '', to: '', folder: '', filetype: '', gone: false, fall: null}},
      {id: 2, wann: '2026-01-05T10:00:00+00:00', treffer: 0, kriterien: {q: '', mode: 'text', person: '', source: 'all', from: '2025-01-01', to: '', folder: '', filetype: 'pdf', gone: true, fall: 1}}]};
  else if(String(pfad) === '/api/suche/gespeichert') antwort = {searches: [
      {id: 5, name: 'Rechnungen', kriterien: {q: 'Rechnung', mode: 'text', person: '', source: 'outlook', from: '', to: '', folder: '', filetype: '', gone: false, fall: null}, zuletzt: '2026-09-14T10:00:00+00:00', treffer: 4, fall: 1, fall_name: 'Nordwind'},
      {id: 6, name: 'Lose', kriterien: {q: 'x', mode: 'text', person: '', source: 'all', from: '', to: '', folder: '', filetype: '', gone: false, fall: null}, zuletzt: null, treffer: null, fall: null, fall_name: null}]};
  else if(String(pfad).indexOf('/api/thread?') === 0) antwort = {thread: 'tix:1', count: 3, messages: [
      {key: 'mail:<m1@example.com>', path: 'inbox/mail1.eml', cases: [{id: 1, name: 'Nordwind', status: 'offen'}]},
      {key: 'mail:<m3@example.com>', path: 'inbox/mail3.eml', cases: []},
      {key: 'mail:<m4@example.com>', path: 'inbox/mail4.eml', cases: []}]};
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
  pruefe(optionen.indexOf('value="1/3">') >= 0 && optionen.indexOf('Belege') >= 0, 'Ordner fehlt im Fallfilter: ' + optionen);
  kriterienAnwenden({fall: 1, ordner: 3});
  pruefe(el('f-fall').value === '1/3' && kriterienAusForm().ordner === 3 && kriterienAusForm().fall === 1, 'Ordnerkriterium geht nicht durch das Formular');
  kriterienAnwenden({});
  // the parties filter: a select like the others; criteria and the request carry it
  el('f-party').classList.remove('hide');
  kriterienAnwenden({party: 'external'});
  pruefe(el('f-party').value === 'external' && kriterienAusForm().party === 'external' && filterFelder().indexOf('external') >= 0, 'Beteiligtenfilter geht nicht durch das Formular');
  pruefe(kriterienTags({party: 'internal'}).indexOf('Beteiligte') >= 0, 'Kriterien-Tag der Beteiligten fehlt');
  doSearch(0);
  await warte(10);
  pruefe(letzte('/api/search?').pfad.indexOf('party=external') > 0, 'party fehlt in der Anfrage');
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
  pruefe(html.indexOf('/source?root=outlook&amp;path=inbox%2Fmail1.eml') >= 0, 'Eintrag ohne Link ins Original');
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
  var bm = letzte('/api/faelle/bemerkung');
  pruefe(bm && bm.body.key === 'mail:<m1@example.com>' && bm.body.text === 'Neu gesagt', 'Bemerkung nicht geschickt: ' + JSON.stringify(bm && bm.body));
  await fallOeffnen(1);
  threadHolen('mail:<m1@example.com>');
  await warte(10);
  pruefe(letzte('/api/faelle/thread').body.keys[0] === 'mail:<m1@example.com>', 'Thread nicht geholt');
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
  pruefe(el('f-person').value === 'Carla Chef' && letzte('/api/search?').pfad.indexOf('person=Carla+Chef') > 0, 'Suche nach Person im Fall fehlt');
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
  var mv = letzte('/api/faelle/verschieben');
  pruefe(mv && mv.body.ordner === 3 && mv.body.keys[0] === 'teams:x#1', 'Verschieben nicht geschickt: ' + JSON.stringify(mv && mv.body));
  pruefe(Object.keys(FALL_AUSWAHL).length === 0, 'Auswahl nach dem Verschieben nicht geleert');
  // a new folder from the tool row: an overlay with one primary button
  ordnerFenster(null);
  pruefe(modal.innerHTML.indexOf('id="ordner-name"') >= 0 && modal.innerHTML.split('class="act"').length - 1 === 1, 'Ordnerfenster unvollstaendig');
  document.getElementById('ordner-name').value = 'Verträge';
  ordnerSenden();
  await warte(10);
  pruefe(letzte('/api/faelle/ordner-anlegen').body.name === 'Verträge', 'Ordner nicht angelegt');
  // "new folder" while moving: the folder first, then the rows into it
  fallWahlZeile('teams:x#1', true);
  fallVerschieben('neu');
  document.getElementById('ordner-name').value = 'Neu';
  anfragen.length = 0;
  ordnerSenden();
  await warte(20);
  pruefe(letzte('/api/faelle/ordner-anlegen') && letzte('/api/faelle/verschieben'), 'neuer Ordner beim Verschieben ohne Verschieben');
  ordnerLoeschen(3);
  await warte(10);
  pruefe(letzte('/api/faelle/ordner-loeschen').body.ordner === 3, 'Ordner nicht aufgeloest');
  await fallOeffnen(1);
  document.getElementById('notiz-neu').value = 'Zweite';
  notizHinzufuegen();
  await warte(10);
  pruefe(letzte('/api/faelle/notiz').body.text === 'Zweite', 'Notiz nicht geschickt');
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
  pruefe(el('f-fall').value === '1' && letzte('/api/search?').pfad.indexOf('case=1') > 0, 'Suche im Fall setzt den Filter nicht');
  sucheImFall(1, 3);
  await warte(10);
  pruefe(el('f-fall').value === '1/3' && letzte('/api/search?').pfad.indexOf('case=1&case_folder=3') > 0, 'Suche im Ordner ohne Ordner: ' + letzte('/api/search?').pfad);
  // export window: the overview, one primary button – always a ZIP, no switch
  await fallOeffnen(1);
  exportFenster();
  html = modal.innerHTML;
  pruefe(html.split('class="act"').length - 1 === 1 && html.indexOf('.zip') >= 0, 'Exportfenster unvollstaendig');
  pruefe(html.indexOf('index.html') >= 0 && html.indexOf('items.csv') >= 0 && html.indexOf('casebook.md') >= 0 && html.indexOf('Belege') >= 0, 'Exportuebersicht unvollstaendig');
  pruefe(html.indexOf('/tmp/exporte') >= 0, 'Exportordner nicht genannt');
  pruefe(html.indexOf('zahnrad') < 0 && html.indexOf('type="checkbox"') < 0, 'Exportfenster mit Schalter oder Zahnrad');
  fallExportStarten(true);
  await warte(10);
  var ex = letzte('/api/faelle/export');
  pruefe(ex && ex.body.id === 1 && !('zip' in ex.body), 'Export nicht geschickt: ' + JSON.stringify(ex && ex.body));
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
  var hz = letzte('/api/faelle/hinzufuegen');
  pruefe(hz && hz.body.ordner === 3, 'Ordner nicht mitgeschickt: ' + JSON.stringify(hz && hz.body));
  var th = letzte('/api/faelle/thread');
  pruefe(th && th.body.keys[0] === 'mail:<m1@example.com>' && th.body.id === 1, 'Gespraech nach dem Hinzufuegen nicht geholt');
  fallLoeschen();
  await warte(10);
  pruefe(letzte('/api/faelle/loeschen'), 'Loeschen nicht geschickt');
  pruefe(el('fall-detail').classList.contains('hide'), 'Detail nach dem Loeschen offen');
  pruefe(!el('faelle-split').classList.contains('eng'), 'Uebersicht nach dem Loeschen eng');

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
    assert store_layout.mit_schluesseln(db) is False, "11.0: keys, but no sender addresses yet"
    assert index_schritt()["ziel"] is None
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE chunks ADD COLUMN who_mail TEXT")
    con.execute("ALTER TABLE chunks ADD COLUMN domains TEXT")
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
