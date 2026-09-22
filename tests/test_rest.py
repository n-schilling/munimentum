"""The versioned surface (/api/v1) and the HTTP manners around it.

Two things are tested here: that a caller who is not the page gets what
the standards lead them to expect – resources, real methods, `Allow`,
problem details as RFC 9457 describes them – and that the app's own
routes keep answering exactly as they did.
"""

import http.client
import json
import re
import threading
from pathlib import Path

import pytest

import app as app_mod
import rest
from hilfen import call

WURZEL = Path(__file__).resolve().parents[1]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for name, wert in (("WURZEL", tmp_path), ("HEIM", tmp_path), ("BASE", tmp_path),
                       ("STORE_PFAD", tmp_path / app_mod.STORE_DIR),
                       ("CONFIG_FILE", tmp_path / "app_config.json"),
                       ("TOKEN_FILE", tmp_path / "gx_token.txt")):
        monkeypatch.setattr(app_mod, name, wert)
    return tmp_path


@pytest.fixture
def server(sandbox, monkeypatch):
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": False, "models": [], "has_model": False,
                            "has_chat_model": False, "error": None, "model": model,
                            "chat_model": chat_model, "url": url})
    a = app_mod.App(app_mod.load_config())
    httpd = app_mod.make_server(a, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield a, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def roh(port, methode, pfad, body=None, kopf=None):
    """One request; (status, headers, parsed body or raw text)."""
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    kopfzeilen = {"Content-Type": "application/json", **(kopf or {})}
    con.request(methode, pfad, json.dumps(body) if body is not None else None, kopfzeilen)
    r = con.getresponse()
    daten = r.read()
    kopfe = dict(r.getheaders())
    con.close()
    try:
        return r.status, kopfe, json.loads(daten)
    except ValueError:
        return r.status, kopfe, daten.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Cases as a resource
# --------------------------------------------------------------------------
def test_ein_fall_durchlaeuft_die_methoden(server):
    """Create, read, change, remove – one resource, four methods, and the
    answer to a create names where the new thing lives."""
    _, port = server
    code, kopf, r = roh(port, "POST", "/api/v1/cases",
                        {"name": "Nordwind", "description": "Alles zur Rechnung"})
    assert code == 201, r
    kennung = r["case"]["id"]
    assert kopf["Location"] == f"/api/v1/cases/{kennung}"
    assert r["case"]["description"] == "Alles zur Rechnung"
    assert r["case"]["status"] == "open"

    code, _, r = roh(port, "GET", "/api/v1/cases")
    assert code == 200 and [f["id"] for f in r["items"]] == [kennung]

    code, _, r = roh(port, "GET", f"/api/v1/cases/{kennung}")
    assert code == 200 and r["case"]["name"] == "Nordwind"
    assert r["case"]["item_list"] == [] and r["case"]["folder_list"] == []

    code, _, r = roh(port, "PATCH", f"/api/v1/cases/{kennung}", {"name": "Nordwind 2026"})
    assert code == 200 and r["case"]["name"] == "Nordwind 2026"

    code, _, r = roh(port, "PATCH", f"/api/v1/cases/{kennung}", {"status": "closed"})
    assert code == 200 and r["case"]["status"] == "closed" and r["case"]["closed_at"]
    # Closed is closed – the same refusal the app's own route gives.
    code, _, r = roh(port, "PATCH", f"/api/v1/cases/{kennung}", {"name": "X"})
    assert code == 409 and r["error"]["k"] == "srv.case.closed"
    code, _, r = roh(port, "PATCH", f"/api/v1/cases/{kennung}", {"status": "open"})
    assert code == 200 and r["case"]["status"] == "open"

    code, kopf, r = roh(port, "DELETE", f"/api/v1/cases/{kennung}")
    assert code == 204 and r == ""
    assert roh(port, "GET", f"/api/v1/cases/{kennung}")[0] == 404
    assert roh(port, "DELETE", f"/api/v1/cases/{kennung}")[0] == 404


def test_der_fall_spricht_englisch(server):
    """The case book keeps its German words; the versioned surface does
    not pass a single one of them on – not in the case, not in its
    lists."""
    _, port = server
    code, _, r = roh(port, "POST", "/api/v1/cases", {"name": "Nordwind"})
    kennung = r["case"]["id"]
    call(port, "POST", f"/api/v1/cases/{kennung}/folders", {"name": "Belege"})
    call(port, "POST", f"/api/v1/cases/{kennung}/notes", {"text": "Erste Notiz"})
    call(port, "POST", f"/api/v1/cases/{kennung}/items", {"items": [
        {"key": "k1", "src": "outlook", "root": "outlook", "rel": "inbox/a.eml",
         "title": "Rechnung", "date": "2026-03-04", "who": "Alice Beispiel"}]})
    code, _, r = roh(port, "GET", f"/api/v1/cases/{kennung}")
    fall = r["case"]
    deutsch = set(rest.FALL) | set(rest.EINTRAG) | set(rest.NOTIZ) | set(rest.ORDNER)
    assert not set(fall) & deutsch, f"deutsche Schlüssel: {set(fall) & deutsch}"
    for feld in ("id", "name", "description", "status", "created", "changed",
                 "items", "items_by_source", "notes", "folders", "folder_list",
                 "item_list", "note_list", "search_list"):
        assert feld in fall, feld
    for liste in ("item_list", "note_list", "folder_list"):
        assert fall[liste], liste
        for eintrag in fall[liste]:
            assert not set(eintrag) & deutsch, f"{liste}: {set(eintrag) & deutsch}"
    eintrag = fall["item_list"][0]
    assert eintrag["title"] == "Rechnung" and eintrag["via"] == "ui"
    assert eintrag["thread_open"] == 0 and "who_mail" in eintrag


def test_eine_abgelehnte_patch_aendert_nichts(server):
    """Whole or not at all: the status must not stay changed behind a
    refusal, and renaming while closing has to work in one call."""
    _, port = server
    _, _, r = roh(port, "POST", "/api/v1/cases", {"name": "Nordwind"})
    kennung = r["case"]["id"]
    code, _, r = roh(port, "PATCH", f"/api/v1/cases/{kennung}",
                     {"status": "closed", "name": "Nordwind 2026"})
    assert code == 200 and r["case"]["status"] == "closed"
    assert r["case"]["name"] == "Nordwind 2026"
    # A closed case refuses the rename – and stays as it was.
    code, _, r = roh(port, "PATCH", f"/api/v1/cases/{kennung}",
                     {"status": "open", "name": ""})
    assert code == 400 and r["error"]["k"] == "srv.case.noname"
    assert roh(port, "GET", f"/api/v1/cases/{kennung}")[2]["case"]["status"] == "closed"
    assert roh(port, "GET", f"/api/v1/cases/{kennung}")[2]["case"]["name"] == "Nordwind 2026"
    # Reopening and renaming in one call works – the status goes first.
    code, _, r = roh(port, "PATCH", f"/api/v1/cases/{kennung}",
                     {"status": "open", "name": "Nordwind 2027"})
    assert code == 200 and r["case"]["status"] == "open"
    assert r["case"]["name"] == "Nordwind 2027"
    # And a status that is already the case's is no refusal.
    assert roh(port, "PATCH", f"/api/v1/cases/{kennung}", {"status": "open"})[0] == 200


def test_die_versionierte_suche_merkt_sich_nur_auf_ansage(server, monkeypatch):
    """The search history is the page's. A script paging the archive would
    otherwise fill the rows a human is offered – so nothing is written
    unless the caller asks, and the page asks."""
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    roh(port, "GET", "/api/v1/search?q=budget")
    assert call(port, "GET", "/api/v1/searches/history")[1]["items"] == []
    roh(port, "GET", "/api/v1/search?q=budget&remember=1")
    verlauf = call(port, "GET", "/api/v1/searches/history")[1]["items"]
    assert [h["criteria"]["q"] for h in verlauf] == ["budget"]


def test_eine_ressource_hat_eine_schreibweise(server):
    """A trailing slash makes a different path here as everywhere else –
    `Location` hands out one spelling, and that is the one."""
    _, port = server
    roh(port, "POST", "/api/v1/cases", {"name": "Nordwind"})
    assert roh(port, "GET", "/api/v1/cases")[0] == 200
    assert roh(port, "GET", "/api/v1/cases/")[0] == 404
    assert roh(port, "GET", "/api/v1/cases/1")[0] == 200
    assert roh(port, "GET", "/api/v1/cases/1/")[0] == 404


def test_ein_unbrauchbarer_pfad_nennt_nichts_das_es_gibt(server):
    _, port = server
    assert roh(port, "GET", "/api/v1/cases/abc")[0] == 404
    assert roh(port, "GET", "/api/v1/cases/99")[0] == 404
    assert roh(port, "GET", "/api/v1/gibtsnicht")[0] == 404
    code, _, r = roh(port, "PATCH", "/api/v1/cases/1", {"status": "halboffen"})
    assert code == 404                      # no such case at all – checked first
    roh(port, "POST", "/api/v1/cases", {"name": "Nordwind"})
    code, _, r = roh(port, "PATCH", "/api/v1/cases/1", {"status": "halboffen"})
    assert code == 400 and r["error"]["k"] == "srv.case.badstatus"
    code, _, r = roh(port, "POST", "/api/v1/cases", {"name": "   "})
    assert code == 400 and r["error"]["k"] == "srv.case.noname"


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------
class FakeSuche:
    """An index that always has one more hit than asked for – enough to see
    what the surface makes of it."""
    STATE = {"semantic": False}
    gesehen = {}

    @staticmethod
    def browse_messages(**kw):
        # Nicht jeder Aufrufer pagiert: die Antwort-Route fragt ohne
        # `offset`, und eine Attrappe, die daran scheitert, prueft die
        # falsche Sache.
        FakeSuche.gesehen = dict(kw)
        k, offset = int(kw.get("k") or 1), int(kw.get("offset") or 0)
        return {"backend": "bm25", "count": k,
                "results": [{"uid": f"u{offset + i}"} for i in range(k)]}

    @staticmethod
    def search_messages(**kw):
        return FakeSuche.browse_messages(**kw)

    @staticmethod
    def similar_messages(**kw):
        return {"backend": "semantic", "count": 0, "results": []}

    @staticmethod
    def get_thread(**kw):
        return {"thread": "x", "count": 0, "messages": []}

    @staticmethod
    def adressen(**kw):
        FakeSuche.gesehen = dict(kw)
        if kw.get("role") not in ("from", "to", "cc", "bcc"):
            return {"error": f'Unknown line: "{kw.get("role")}" (from, to, cc, bcc).',
                    "count": 0, "addresses": []}
        return {"count": 1, "role": kw["role"],
                "addresses": [{"address": "alice.beispiel@nordwind.example",
                               "messages": 12}]}


def test_die_suche_paged_mit_limit_und_offset(server, monkeypatch):
    """`limit` and `offset` are the names the rest of the API uses, and
    `has_more` says whether another page follows – a total would mean
    ranking the whole archive."""
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    code, _, r = roh(port, "GET", "/api/v1/search?q=budget&limit=5&offset=10")
    assert code == 200
    assert FakeSuche.gesehen["k"] == 6 and FakeSuche.gesehen["offset"] == 10
    assert [h["uid"] for h in r["items"]] == [f"u{10 + i}" for i in range(5)]
    assert r == {**r, "limit": 5, "offset": 10, "has_more": True, "backend": "bm25"}
    assert "error" not in r and "hits" not in r

    monkeypatch.setattr(FakeSuche, "browse_messages",
                        staticmethod(lambda **kw: {"backend": "bm25", "count": 2,
                                                   "results": [{"uid": "a"}, {"uid": "b"}]}))
    code, _, r = roh(port, "GET", "/api/v1/search?limit=5")
    assert code == 200 and len(r["items"]) == 2 and r["has_more"] is False


def test_die_mailzeilen_gehen_als_eigene_parameter_durch(server, monkeypatch):
    """The four lines of a mail have their own names – `from` and `to` were
    taken by the date range long before – and they reach the engine as they
    were typed."""
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    code, _, r = roh(port, "GET", "/api/v1/search?source=outlook"
                                  "&mail_from=alice%40nordwind.example&mail_to=bob"
                                  "&mail_cc=carla&mail_bcc=dana&from=2026-01-01")
    assert code == 200
    assert FakeSuche.gesehen["mail_from"] == "alice@nordwind.example"
    assert FakeSuche.gesehen["mail_to"] == "bob"
    assert FakeSuche.gesehen["mail_cc"] == "carla"
    assert FakeSuche.gesehen["mail_bcc"] == "dana"
    assert FakeSuche.gesehen["date_from"], "der Zeitraum bleibt from/to"
    # Nothing given, nothing sent on.
    roh(port, "GET", "/api/v1/search?q=budget")
    assert FakeSuche.gesehen["mail_to"] == ""
    # An index that predates the lines refuses – the engine's own sentence,
    # and 409, because the request is fine and the index is not.
    monkeypatch.setattr(FakeSuche, "browse_messages",
                        staticmethod(lambda **kw: {"error": "This index predates the mail lines.",
                                                   "count": 0, "results": []}))
    code, _, r = roh(port, "GET", "/api/v1/search?mail_to=bob")
    assert code == 409 and r["detail"] == "This index predates the mail lines."
    assert r["hits"] == [] and r["count"] == 0


def test_die_adressen_einer_zeile_sind_eine_sammlung(server, monkeypatch):
    """What the Mail filter offers while one types – a collection like every
    other on this surface: `items`."""
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    code, _, r = roh(port, "GET", "/api/v1/addresses?role=cc&contains=carla&limit=5")
    assert code == 200 and r["items"][0]["messages"] == 12
    # One above the cap: that is where `has_more` comes from.
    assert FakeSuche.gesehen == {"role": "cc", "q": "carla", "limit": 6}
    assert "addresses" not in r, "eine Sammlung heisst hier items"
    # The default line is the sender's.
    roh(port, "GET", "/api/v1/addresses")
    assert FakeSuche.gesehen["role"] == "from" and FakeSuche.gesehen["limit"] == 13
    # A line that does not exist is a bad request, not an empty list.
    code, _, r = roh(port, "GET", "/api/v1/addresses?role=envelope")
    assert code == 400 and r["items"] == []
    assert roh(port, "GET", "/api/v1/addresses?limit=viele")[0] == 400
    # And without an index the same 503 as everywhere else.
    monkeypatch.setattr(a.search, "ensure", lambda cfg: None)
    code, _, r = roh(port, "GET", "/api/v1/addresses")
    assert code == 503 and r["items"] == []


def test_der_gespeicherte_modus_laeuft_ohne_uebersetzung(server, monkeypatch):
    """A saved search stores the interface's names for its three modes; the
    engine ranks under other ones. The route takes both, so a stored
    criteria set can be handed back unchanged."""
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    for gespeichert, erwartet in (("text", "lexical"), ("aehnlich", "semantic"),
                                  ("ki", "hybrid"), ("lexical", "lexical"),
                                  ("semantic", "semantic"), ("", "auto")):
        roh(port, "GET", f"/api/v1/search?q=Rechnung&mode={gespeichert}")
        assert FakeSuche.gesehen.get("mode") == erwartet, (gespeichert, FakeSuche.gesehen)


def test_eine_unbrauchbare_seitenzahl_ist_eine_schlechte_anfrage(server, monkeypatch):
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    code, _, r = roh(port, "GET", "/api/v1/search?limit=viele")
    assert code == 400 and r["error"]["k"] == "srv.badparam"
    roh(port, "GET", "/api/v1/search?limit=9999")
    assert FakeSuche.gesehen["k"] == 101          # clamped to the largest page


# --------------------------------------------------------------------------
# Methods
# --------------------------------------------------------------------------
def test_die_falsche_methode_ist_405_mit_allow(server):
    """A path that exists under another method is not a missing route."""
    _, port = server
    code, kopf, r = roh(port, "GET", "/api/v1/searches/history")   # DELETE und GET
    assert code == 200
    code, kopf, r = roh(port, "POST", "/api/v1/searches/history")
    assert code == 405 and kopf["Allow"] == "DELETE, GET, HEAD, OPTIONS"
    assert r["error"]["k"] == "srv.method" and r["status"] == 405
    code, kopf, _ = roh(port, "PUT", "/api/v1/cases/1")
    assert code == 405 and "PATCH" in kopf["Allow"] and "PUT" not in kopf["Allow"]
    code, kopf, _ = roh(port, "DELETE", "/api/v1/config")
    assert code == 405 and kopf["Allow"] == "GET, HEAD, OPTIONS, PATCH"
    # Nothing of the sort: still a 404.
    assert roh(port, "DELETE", "/api/gibtsnicht")[0] == 404
    assert roh(port, "GET", "/api/gibtsnicht")[0] == 404


def test_options_sagt_was_hier_geht(server):
    _, port = server
    code, kopf, _ = roh(port, "OPTIONS", "/api/v1/cases/1")
    assert code == 204 and kopf["Allow"] == "DELETE, GET, HEAD, OPTIONS, PATCH"
    code, kopf, _ = roh(port, "OPTIONS", "/api/v1/status")
    assert code == 204 and kopf["Allow"] == "GET, HEAD, OPTIONS"
    code, kopf, _ = roh(port, "OPTIONS", "/api/v1/profiles")
    assert code == 204 and kopf["Allow"] == "GET, HEAD, OPTIONS, PATCH, POST"
    assert "Access-Control-Allow-Origin" not in kopf     # one origin, ours
    assert roh(port, "OPTIONS", "/api/gibtsnicht")[0] == 404


def test_die_frage_im_rumpf_ist_ein_query(server):
    """Three routes are reads whose question does not fit in a URL, so they
    answer QUERY (RFC 10008) and nothing else: OPTIONS says so, the POST
    they used to be is a wrong method like any other, and the body still
    has to be JSON."""
    _, port = server
    pfad = "/api/v1/sources/outlook/folder-plan"
    for weg in (pfad, "/api/v1/reports", "/api/v1/answer"):
        code, kopf, _ = roh(port, "OPTIONS", weg)
        assert code == 204 and kopf["Allow"] == "OPTIONS, QUERY", weg
        # RFC 10008, Section 3: the route names the format it takes a
        # question in – beside Allow, and on the refusal as well.
        assert kopf["Accept-Query"] == "application/json", weg
        code, kopf, r = roh(port, "POST", weg, {})
        assert code == 405 and kopf["Allow"] == "OPTIONS, QUERY", weg
        assert kopf["Accept-Query"] == "application/json", weg
        assert r["error"]["k"] == "srv.method" and "POST" in r["detail"]
    # Nowhere else: a path that takes no question says nothing about one.
    assert "Accept-Query" not in roh(port, "OPTIONS", "/api/v1/status")[1]
    # The report is the one of the three that answers without an archive.
    code, _, r = roh(port, "QUERY", "/api/v1/reports", {"hint": "Absturz"})
    assert code == 200 and r["title"] == "Absturz"
    code, _, r = roh(port, "QUERY", "/api/v1/reports", {},
                     kopf={"Content-Type": "text/plain"})
    assert code == 415 and r["error"]["k"] == "srv.mediatype"


def test_eine_query_ohne_frage_ist_eine_schlechte_anfrage(server):
    """RFC 10008, Section 2: the content *is* the question, and a server
    has to fail a request whose media type is missing. So QUERY is the one
    method here that does not fall back to the defaults when the body is
    absent – it says 400 instead."""
    _, port = server
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("QUERY", "/api/v1/reports", "{}")          # no Content-Type
    antwort = con.getresponse()
    r = json.loads(antwort.read())
    con.close()
    assert antwort.status == 400 and r["error"]["k"] == "srv.query.notype"
    code, _, r = roh(port, "QUERY", "/api/v1/reports")     # no body at all
    assert code == 400 and r["error"]["k"] == "srv.query.empty"
    # The methods that write keep their manners: no body is no error there.
    assert roh(port, "PATCH", "/api/v1/config")[0] == 200


def test_der_laufmonitor_baut_nicht_den_ganzen_zustand(server, monkeypatch):
    """`GET /api/v1/runs/current` is polled while a run is on – it must not
    re-read the token, probe Ollama and ask the MCP subprocess for that."""
    a, port = server
    monkeypatch.setattr(a, "status", lambda: (_ for _ in ()).throw(
        AssertionError("app.status() für einen Lauf gebaut")))
    code, _, r = roh(port, "GET", "/api/v1/runs/current")
    assert code == 404 and r["error"]["k"] == "srv.run.none"
    # DELETE says the same thing the same way: there is no run.
    code, _, r = roh(port, "DELETE", "/api/v1/runs/current")
    assert code == 404 and r["error"]["k"] == "srv.run.none"
    # While a run is on, the wish counts even between two steps – when
    # there is no process to end, the runner still stops at the next.
    monkeypatch.setattr(type(a.jobs), "busy", property(lambda self: True))
    monkeypatch.setattr(a.jobs, "cancel", lambda: False)
    assert roh(port, "DELETE", "/api/v1/runs/current")[0] == 204
    monkeypatch.setattr(type(a.jobs), "busy", property(lambda self: False))
    monkeypatch.setattr(a.jobs, "snapshot",
                        lambda: {"job": {"label": "job.export", "steps": ["a"],
                                         "step": "a", "index": 0, "progress": None,
                                         "started": "2026-03-05T06:00:02",
                                         "log_seq": 0}})
    code, _, r = roh(port, "GET", "/api/v1/runs/current")
    assert code == 200 and r["run"]["label"] == "job.export"


def test_die_routentabelle_wird_je_anfrage_einmal_gelesen(server, monkeypatch):
    """One pass over the route table per request: the 405 decision and
    the dispatch read the same resolved list. A PATCH used to walk it
    twice, a miss three times."""
    import app as app_mod
    _, port = server
    echt, zaehler = app_mod.muster_passt, []
    monkeypatch.setattr(app_mod, "muster_passt",
                        lambda muster, pfad: zaehler.append(1) or echt(muster, pfad))
    for methode, pfad in (("PATCH", "/api/v1/mcp"), ("GET", "/api/v1/nirgends"),
                          ("DELETE", "/api/v1/status"), ("QUERY", "/api/v1/answer")):
        zaehler.clear()
        roh(port, methode, pfad, {})
        assert len(zaehler) == len(app_mod.ROUTEN_V1), (methode, pfad, len(zaehler))


def test_ein_profil_liest_nur_seine_eigenen_dateien(server, monkeypatch):
    """GET /profiles/{name} used to build the whole profile status – every
    profile's files – to answer one; now it reads that one."""
    import app as app_mod
    _, port = server
    monkeypatch.setattr(app_mod, "profil_status",
                        lambda: (_ for _ in ()).throw(AssertionError("read every profile")))
    code, _, r = roh(port, "GET", f"/api/v1/profiles/{app_mod.PROFIL}")
    assert code == 200 and r["profile"]["name"] == app_mod.PROFIL and r["profile"]["aktiv"]
    code, _, r = roh(port, "GET", "/api/v1/profiles/nirgends")
    assert code == 404 and r["error"]["k"] == "srv.profile.unknown"


def test_anmelden_startet_den_geraetecode(server, monkeypatch):
    """The route nobody tested: it called a method the app does not have,
    so every sign-in was a 500. Now it answers the code the user types at
    Microsoft."""
    a, port = server
    monkeypatch.setattr(a, "login_starten",
                        lambda: (True, {"code": "ABCD-EFGH",
                                        "url": "https://microsoft.com/devicelogin",
                                        "expires_in": 900}))
    code, _, r = roh(port, "POST", "/api/v1/access/session", {})
    assert code == 200 and r["device"]["code"] == "ABCD-EFGH"
    monkeypatch.setattr(a, "login_starten", lambda: (False, {"error": "boom"}))
    code, _, r = roh(port, "POST", "/api/v1/access/session", {})
    assert code == 500 and r["error"]["k"] == "srv.login.failed"
    # The reason is in the sentence, not left as a placeholder.
    assert "boom" in r["detail"] and "{detail}" not in r["detail"], r


def test_eine_unbekannte_quelle_ist_ueberall_ein_404(server):
    """Every route under /sources answers the same way for a source that
    does not exist – and the balance, whose rows are finer than the
    eight, answers the same 404 under its own path."""
    _, port = server
    for weg, methode in (("refetch", "POST"), ("rebuild", "POST"), ("open", "POST")):
        code, _, r = roh(port, methode, f"/api/v1/sources/nonesuch/{weg}", {})
        assert code == 404, (weg, code)
        assert r["error"]["k"] == "srv.archiv.unknown", weg
    for pfad, methode in (("/api/v1/balance/nonesuch", "GET"),
                          ("/api/v1/balance/nonesuch/fetch", "POST")):
        code, _, r = roh(port, methode, pfad, {})
        assert code == 404 and r["error"]["k"] == "srv.archiv.unknown", pfad
    assert roh(port, "POST", "/api/v1/balance/outlook_mail/fetch", {})[0] != 404


def test_eine_query_auf_nichts_ist_kein_kaputter_rumpf(server):
    """First the route, then the body: a QUERY at a path that does not
    exist is a 404, and one at a route that only reads is a 405 with
    `Allow` – not "your question is missing"."""
    _, port = server
    code, _, r = roh(port, "QUERY", "/api/v1/gibtsnicht")
    assert code == 404 and r["error"]["k"] == "srv.notfound"
    code, kopf, r = roh(port, "QUERY", "/api/v1/status")
    assert code == 405 and "GET" in kopf["Allow"] and r["error"]["k"] == "srv.method"
    # Die Strenge gilt weiter, wo die Route wirklich eine Frage erwartet.
    assert roh(port, "QUERY", "/api/v1/reports")[0] == 400


def test_ein_fehlendes_feld_ist_kein_plattformproblem(server):
    """`srv.profile.impossible` says profiles do not work on this system –
    for a body that simply forgot a field that is a false trail."""
    _, port = server
    code, _, r = roh(port, "PATCH", "/api/v1/profiles", {})
    assert code == 400 and r["error"]["k"] == "srv.profile.nofield"
    assert r["error"]["v"]["name"] == "ask_at_start"


def test_auch_eine_unbekannte_methode_bekommt_json(server):
    """What the base class refuses answers the same body – its HTML error
    page would be the one answer nobody can parse."""
    _, port = server
    code, kopf, r = roh(port, "TRACE", "/api/v1/status")
    assert code == 501
    assert kopf["Content-Type"].startswith("application/problem+json")
    assert r["error"]["k"] == "srv.method" and "TRACE" in r["detail"]
    assert "Python" not in kopf["Server"]               # nor the version


def test_kein_handler_name_zweimal():
    """A second `def` of the same name in the class silently replaces the
    first, and the route table then calls the wrong one – a 500 that no
    signature check catches. This is how `_v1_liste` was caught."""
    quelle = (WURZEL / "app.py").read_text(encoding="utf-8")
    for klasse, ende in (("class Handler(", "class Server("), ("class Wahl(", None)):
        i = quelle.index(klasse)
        block = quelle[i:quelle.index(ende)] if ende else quelle[i:]
        namen = re.findall(r"\n    def (\w+)\(", block)
        doppelt = sorted({n for n in namen if namen.count(n) > 1})
        assert not doppelt, f"{klasse}: {doppelt}"
    # The same for the route modules: a second `def` of a name would
    # replace the first – and the table would call the wrong one.
    for modul in ("api.py", "api_cases.py", "api_explore.py", "api_archive.py", "api_app.py"):
        namen = re.findall(r"\ndef (\w+)\(", (WURZEL / modul).read_text(encoding="utf-8"))
        doppelt = sorted({n for n in namen if namen.count(n) > 1})
        assert not doppelt, f"{modul}: {doppelt}"
    import sys
    import app as app_mod
    # Every route names a function its module still holds under that name
    # (a rename that forgot the table would call a stale object), or one
    # of the handler's own – the three that write to the socket.
    fehlend = [n for _, _, n in app_mod.ROUTEN_V1
               if (not hasattr(app_mod.Handler, n) if isinstance(n, str)
                   else getattr(sys.modules[n.__module__], n.__name__, None) is not n)]
    assert not fehlend, f"Routen ohne Handler: {fehlend}"


def test_nichts_nennt_eine_route_von_vor_13_0():
    """Since 13.0 every path is a `/api/v1` one. What is left over does not
    fail anywhere – a link in the page renders, a comment reads plausibly,
    the smoke test calls a route that answers 404 only when the packaging
    CI runs. This is the sweep that catches all three: the dead link to
    `/api/openapi` in the expert card is what it was written for."""
    dateien = ["page.html", "profil.html", "openapi.yaml", "app.py", "rest.py",
               "api.py", "api_cases.py", "api_explore.py", "api_archive.py", "api_app.py",
               "mcp_server.py", "README.md", "DESIGN.md", "PRIVACY.md",
               "packaging/smoke_test.py", "packaging/app.spec"]
    alt = {}
    for name in dateien:
        text = (WURZEL / name).read_text(encoding="utf-8")
        # `/api/` followed by anything but the version – in code, in markup
        # and in prose alike. Ollama's own /api/chat lives in its client.
        treffer = {m for m in re.findall(r"/api/(?!v1\b)[A-Za-z0-9_{}.-]*", text)
                   if m != "/api/"}          # app.py builds the prefix itself
        if treffer:
            alt[name] = sorted(treffer)
    assert not alt, f"Routen von vor 13.0: {alt}"


def test_die_routentabelle_stimmt_mit_den_verteilern(server):
    """The table that answers 405 and OPTIONS is a second copy of what the
    dispatchers serve – this is what keeps it honest."""
    quelle = (WURZEL / "app.py").read_text(encoding="utf-8")
    handler = quelle[quelle.index("class Handler("):quelle.index("class Server(")]

    def pfade(name):
        i = handler.index(f"    def {name}(self)")
        j = handler.index("\n    def ", i + 10)
        block = handler[i:j]
        out = set(re.findall(r'u\.path == "([^"]+)"', block))
        for t in re.findall(r"u\.path in \(([^)]*)\)", block):
            out.update(re.findall(r'"([^"]+)"', t))
        return out

    tabelle = app_mod.Handler.ROUTEN
    for methode in ("GET", "POST"):
        aus_quelle = pfade("do_" + methode)
        genannt = {p for p, m in tabelle.items() if methode in m}
        assert aus_quelle == genannt, f"{methode}: {aus_quelle ^ genannt}"


# --------------------------------------------------------------------------
# The body, and what a refusal looks like
# --------------------------------------------------------------------------
def test_die_ablehnung_ist_ein_problem_detail(server):
    """RFC 9457: type, title, status, detail, instance – plus `ok` and
    `error`, which the page reads. On the versioned surface the field 11.3
    used is already gone."""
    _, port = server
    code, kopf, r = roh(port, "GET", "/api/gibtsnicht")
    assert code == 404
    assert kopf["Content-Type"] == "application/problem+json; charset=utf-8"
    assert r["type"] == "urn:munimentum:error:srv.notfound"
    assert r["title"] == "Not Found" and r["status"] == 404
    assert r["detail"] == "No such route: /api/gibtsnicht"
    assert r["instance"] == "/api/gibtsnicht"
    assert r["ok"] is False and r["error"]["k"] == "srv.notfound"
    assert "message" not in r      # one shape, no second spelling (13.0)
    assert "message" not in roh(port, "GET", "/api/v1/cases/9")[2]


def test_ohne_index_kommt_503_mit_retry_after(server):
    _, port = server
    code, kopf, r = roh(port, "GET", "/api/v1/search?q=x")
    assert code == 503 and kopf["Retry-After"] == "30"
    assert r["error"]["k"] == "srv.noindex" and r["title"] == "Service Unavailable"


def test_der_rumpf_muss_json_sein_und_es_sagen(server):
    _, port = server
    # No body at all is no error – the route simply finds no name.
    code, _, r = roh(port, "POST", "/api/v1/cases", None, {"Content-Type": "text/plain"})
    assert code == 400 and r["error"]["k"] == "srv.case.noname"
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("POST", "/api/v1/cases", "name=Nordwind", {"Content-Type": "text/plain"})
    antwort = con.getresponse()
    daten = json.loads(antwort.read())
    con.close()
    assert antwort.status == 415 and daten["error"]["k"] == "srv.mediatype"
    assert call(port, "POST", "/api/v1/cases", {"name": "Nordwind"})[0] == 201


def test_ein_riesiger_rumpf_wird_abgewiesen(server):
    """Unread, its rest would be taken for the next request on the same
    connection – so the answer closes it."""
    _, port = server
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.putrequest("POST", "/api/v1/cases")
    con.putheader("Content-Type", "application/json")
    con.putheader("Content-Length", str(5 * 1024 * 1024))
    con.endheaders()
    try:
        con.send(b'{"name": "' + b"x" * 1000 + b'"}')
    except OSError:
        pass                        # the server may already have hung up
    antwort = con.getresponse()
    daten = json.loads(antwort.read())
    con.close()
    assert antwort.status == 413 and daten["error"]["k"] == "srv.toobig"


# --------------------------------------------------------------------------
# What a refusal does to the connection
# --------------------------------------------------------------------------
def _pipeline(port, erste, zweite):
    """Two requests written in one go. What comes back second says whether
    the server took the first one's body for a request of its own."""
    import socket
    wirt = f"127.0.0.1:{port}".encode()
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(erste.replace(b"__HOST__", wirt) + zweite.replace(b"__HOST__", wirt))
    daten = b""
    s.settimeout(2)
    try:
        while True:
            stueck = s.recv(65536)
            if not stueck:
                break
            daten += stueck
    except (TimeoutError, OSError):
        pass
    s.close()
    return daten.decode("utf-8", "replace")


def test_eine_abgelehnte_anfrage_verschluckt_ihren_rumpf(server):
    """A body that is refused unread would be taken for the next request on
    the same connection – the answer to a request nobody sent."""
    _, port = server
    antwort = _pipeline(
        port,
        b"POST /api/v1/cases HTTP/1.1\r\nHost: __HOST__\r\n"
        b"Content-Type: text/plain\r\nContent-Length: 6\r\n\r\nname=x",
        b"GET /api/v1/status HTTP/1.1\r\nHost: __HOST__\r\n\r\n")
    assert "415" in antwort and "name=x" not in antwort
    assert antwort.count("HTTP/1.") == 2 and "200 OK" in antwort


def test_auch_die_basisklasse_schliesst_nach_ihrer_absage(server):
    """What the base class refuses – an unknown verb – it refuses before
    reading anything, so its answer has to end the connection."""
    _, port = server
    antwort = _pipeline(
        port,
        b"TRACE /api/v1/status HTTP/1.1\r\nHost: __HOST__\r\n"
        b"Content-Length: 6\r\n\r\nABCDEF",
        b"GET /api/v1/status HTTP/1.1\r\nHost: __HOST__\r\n\r\n")
    assert "501" in antwort and "ABCDEF" not in antwort
    assert "Connection: close" in antwort and antwort.count("HTTP/1.") == 1


def test_kein_rumpf_kein_content_length(server):
    """RFC 9110 §8.6: a 204 carries neither."""
    _, port = server
    code, kopf, _ = roh(port, "OPTIONS", "/api/v1/status")
    assert code == 204 and "Content-Length" not in kopf and "Content-Type" not in kopf
    roh(port, "POST", "/api/v1/cases", {"name": "Nordwind"})
    code, kopf, _ = roh(port, "DELETE", "/api/v1/cases/1")
    assert code == 204 and "Content-Length" not in kopf


def test_head_nennt_die_methode_die_gefragt_hat(server):
    """do_HEAD goes through do_GET – the refusal must still say HEAD."""
    _, port = server
    code, kopf, _ = roh(port, "HEAD", "/api/v1/access/token")
    assert code == 405 and kopf["Allow"] == "OPTIONS, PUT"


def test_auch_die_hostpruefung_verschluckt_den_rumpf(server):
    """The Host check answers before any route – and before the body would
    have been read."""
    _, port = server
    antwort = _pipeline(
        port,
        b"POST /api/v1/cases HTTP/1.1\r\nHost: angreifer.example.com\r\n"
        b"Content-Type: application/json\r\nContent-Length: 13\r\n\r\n{\"name\": \"x\"}",
        b"GET /api/v1/status HTTP/1.1\r\nHost: __HOST__\r\n\r\n")
    assert "403" in antwort and antwort.count("HTTP/1.") == 2
    assert "200 OK" in antwort and '{"name"' not in antwort.split("403")[1][:400]


def test_eine_unbrauchbare_zahl_ist_ueberall_eine_schlechte_anfrage(server, monkeypatch):
    """A caller's typo is a 400 on every route, not the 500 an uncaught
    ValueError used to make of it."""
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    for pfad in ("/api/v1/search?limit=abc", "/api/v1/search?offset=abc", "/api/v1/similar?cid=abc",
                 "/api/v1/threads?key=x&limit=abc", "/api/v1/runs?limit=abc",
                 "/api/v1/runs?limit=abc", "/api/v1/log?since=abc",
                 "/api/v1/addresses?limit=abc", "/api/v1/searches/history?limit=abc"):
        code, _, r = roh(port, "GET", pfad)
        assert code == 400, (pfad, code, r)
        assert r["error"]["k"] == "srv.badparam", pfad


def test_jede_antwort_nennt_programm_und_vertrag(server):
    """Two versions, two headers: the path says which contract answers and
    stays there as long as the shape holds; the program's own version is
    where a script can read it without guessing from the path."""
    import version as version_mod
    _, port = server
    erwartet = version_mod.VERSION + (f" ({version_mod.build()})"
                                      if version_mod.build() else "")
    for methode, pfad in (("GET", "/api/v1/status"), ("GET", "/api/v1/cases"),
                          ("GET", "/api/gibtsnicht"), ("OPTIONS", "/api/v1/status"),
                          ("POST", "/api/v1/cases"),
                          # Die drei, die ihren Kopf selbst schreiben und die
                          # beiden Zeilen bis 13.0 weggelassen haben.
                          ("QUERY", "/api/v1/answer"),
                          ("GET", "/api/v1/files/content?root=outlook&path=x.eml")):
        koerper = {"q": "x"} if methode in ("POST", "QUERY") else None
        _, kopf, _ = roh(port, methode, pfad, koerper)
        assert kopf["X-Munimentum-Version"] == erwartet, (methode, pfad)
        assert kopf["X-Munimentum-Api"] == app_mod.API_VERSION, (methode, pfad)
    assert app_mod.API_V1 == "/api/" + app_mod.API_VERSION


def test_auch_der_strom_und_die_datei_nennen_sich(server, sandbox, monkeypatch):
    """The two answers that write their own header block, on the way they
    take when they succeed: the NDJSON stream sends its head before it
    knows how the answer ends, and a file goes out as bytes. Both left the
    two headers off until 13.0."""
    import answer as answer_mod
    a, port = server

    # Eine Datei, die es wirklich gibt.
    datei = sandbox / app_mod.OUTLOOK_DIR / "Posteingang" / "a.eml"
    datei.parent.mkdir(parents=True, exist_ok=True)
    datei.write_text("Subject: Rechnung\n\nText\n", encoding="utf-8")
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    monkeypatch.setattr(FakeSuche, "_resolve_source",
                        staticmethod(lambda root, rel: (datei, None)), raising=False)
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("GET", "/api/v1/files/content?root=outlook&path=Posteingang%2Fa.eml")
    r = con.getresponse()
    r.read()
    con.close()
    assert r.status == 200, r.status
    assert r.getheader("X-Munimentum-Api") == app_mod.API_VERSION
    assert r.getheader("X-Munimentum-Version")

    # Und der Strom, mit einem Modell, das antwortet.
    monkeypatch.setattr(FakeSuche, "STATE", {"semantic": True}, raising=False)
    monkeypatch.setattr(app_mod, "check_ollama",
                        lambda url, model, chat_model=None, timeout=1.5: {
                            "running": True, "models": [chat_model], "has_model": True,
                            "has_chat_model": True, "error": None, "model": model,
                            "chat_model": chat_model, "url": url})
    monkeypatch.setattr(FakeSuche, "get_document",
                        staticmethod(lambda **kw: {"text": "Der Rechnungstext."}),
                        raising=False)
    monkeypatch.setattr(answer_mod, "stream", lambda *args, **kw: iter([{"text": "Ja."}]))
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    con.request("QUERY", "/api/v1/answer", json.dumps({"q": "Rechnung"}),
                {"Content-Type": "application/json"})
    r = con.getresponse()
    roh_text = r.read().decode("utf-8")
    con.close()
    assert r.status == 200 and "x-ndjson" in r.getheader("Content-Type", ""), roh_text[:200]
    assert r.getheader("X-Munimentum-Api") == app_mod.API_VERSION
    assert r.getheader("X-Munimentum-Version")


def test_die_umgebung_nennt_die_api_version(server):
    """The settings page shows it next to the program version. It is not in
    the polled status: it never changes, and every answer carries it as a
    header anyway."""
    _, port = server
    assert call(port, "GET", "/api/v1/app")[1]["api_version"] == app_mod.API_VERSION


# --------------------------------------------------------------------------
# The rest of the Explore door
# --------------------------------------------------------------------------
class FakeLesen(FakeSuche):
    """An engine that answers every listing with one entry."""
    @staticmethod
    def list_folders(**kw):
        return {"folders": [{"path": "E-Mail/Inbox", "messages": 3}], "count": 1}

    @staticmethod
    def list_filetypes(**kw):
        return {"filetypes": [{"type": "pdf", "messages": 2}], "total_distinct": 1}

    @staticmethod
    def list_people(**kw):
        return {"people": [{"name": "Alice Beispiel", "messages": 5}], "total_distinct": 1}

    @staticmethod
    def get_thread(**kw):
        return {"thread": "tix:1", "count": 1, "messages": [{"uid": "u1"}]}

    @staticmethod
    def list_files(**kw):
        return {"root": "onedrive", "path": "", "dirs": ["Belege"],
                "files": [{"name": "a.pdf", "rel": "a.pdf", "date": "2026-01-01"}]}

    STATE = {"semantic": False, "onedrive_dir": "/nirgends"}


def test_die_tuer_liest_ueber_die_versionierte_oberflaeche(server, monkeypatch):
    """Files, folders, file types, people and a conversation – the same
    answers as the app's own routes, with one name for every list."""
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeLesen)
    code, _, r = roh(port, "GET", "/api/v1/folders?limit=300")
    assert code == 200 and r["items"] == [{"path": "E-Mail/Inbox", "messages": 3}]
    assert "folders" not in r
    code, _, r = roh(port, "GET", "/api/v1/filetypes")
    assert code == 200 and r["items"][0]["type"] == "pdf" and "filetypes" not in r
    code, _, r = roh(port, "GET", "/api/v1/people?contains=Ali")
    assert code == 200 and r["items"][0]["name"] == "Alice Beispiel" and "people" not in r
    code, _, r = roh(port, "GET", "/api/v1/threads?key=tix:1")
    assert code == 200 and r["thread"] == "tix:1" and r["items"] == [{"uid": "u1"}]
    assert "messages" not in r
    code, _, r = roh(port, "GET", "/api/v1/files?root=onedrive")
    assert code == 200 and r["dirs"] == ["Belege"] and r["files"][0]["name"] == "a.pdf"


class FakeDrei(FakeSuche):
    """An engine with three of everything that honours `limit` as the real
    one does – so `has_more` can be checked to the item."""
    @staticmethod
    def list_folders(**kw):
        return {"folders": [{"path": f"E-Mail/{i}", "messages": i} for i in (3, 2, 1)][:kw["limit"]]}

    @staticmethod
    def list_filetypes(**kw):
        return {"filetypes": [{"type": t, "messages": 1} for t in ("pdf", "docx", "xlsx")][:kw["limit"]],
                "total_distinct": 3}

    @staticmethod
    def list_people(**kw):
        return {"people": [{"name": n, "messages": 1}
                           for n in ("Alice Beispiel", "Bob Baumeister", "Carla Chef")][:kw["limit"]]}

    @staticmethod
    def get_thread(**kw):
        seite = [{"uid": f"u{i}"} for i in (1, 2, 3)][:kw["limit"]]
        return {"thread": "tix:1", "count": len(seite), "messages": seite}

    STATE = {"semantic": False}


def test_has_more_sagt_nur_ja_wenn_wirklich_etwas_fehlt(server, monkeypatch):
    """The engine is asked for one more than the cap; whether it came says
    whether the cap cut something off. An archive with exactly as many
    folders as the limit used to answer `has_more: true` – and a caller
    narrowing to fetch "the rest" found nothing."""
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeDrei)
    for pfad in ("/api/v1/folders", "/api/v1/filetypes", "/api/v1/people", "/api/v1/threads?key=tix:1"):
        trenner = "&" if "?" in pfad else "?"
        code, _, r = roh(port, "GET", f"{pfad}{trenner}limit=3")
        assert code == 200 and len(r["items"]) == 3 and r["has_more"] is False, (pfad, r)
        code, _, r = roh(port, "GET", f"{pfad}{trenner}limit=2")
        assert len(r["items"]) == 2 and r["has_more"] is True and r["limit"] == 2, (pfad, r)
        if "count" in r:
            assert r["count"] == 2, (pfad, r)
    for i in range(3):
        a.faelle.suche_merken({"q": f"wort{i}"}, 1)
        a.history.start_run("job.export", "manual")
    for pfad in ("/api/v1/searches/history", "/api/v1/runs"):
        assert roh(port, "GET", f"{pfad}?limit=3")[2]["has_more"] is False, pfad
        r = roh(port, "GET", f"{pfad}?limit=2")[2]
        assert len(r["items"]) == 2 and r["has_more"] is True, (pfad, r)


def test_die_tuer_sagt_ohne_index_bescheid(server):
    """Every one of them needs an index and says so with 503, not with an
    empty list at 200."""
    _, port = server
    for pfad in ("/api/v1/files", "/api/v1/folders", "/api/v1/filetypes",
                 "/api/v1/people", "/api/v1/threads?key=x",
                 "/api/v1/documents?uid=x", "/api/v1/documents/facts?uid=x"):
        code, kopf, r = roh(port, "GET", pfad)
        assert code == 503, (pfad, code)
        assert r["error"]["k"] == "srv.noindex" and kopf["Retry-After"] == "30"
    # The calendar needs no index, only the step's file.
    code, _, r = roh(port, "GET", "/api/v1/calendar")
    assert code == 404 and r["error"]["k"] == "cal.missing" and r["recs"] == []


def test_die_einstellungen_sind_eine_ressource(server):
    """They used to travel with every status poll – a third of its weight –
    although they change only when someone saves them."""
    _, port = server
    code, _, r = roh(port, "GET", "/api/v1/config")
    assert code == 200 and r["config"]["workers"] == app_mod.DEFAULT_CONFIG["workers"]
    assert "config" not in roh(port, "GET", "/api/v1/status")[2]

    code, _, r = roh(port, "PATCH", "/api/v1/config", {"workers": 6})
    assert code == 200 and r["config"]["workers"] == 6
    assert "ok" not in r                       # the resource, not a receipt
    # PATCH means: these keys, the rest stays.
    assert r["config"]["search_results"] == app_mod.DEFAULT_CONFIG["search_results"]
    assert roh(port, "GET", "/api/v1/config")[2]["config"]["workers"] == 6
    # Out of bounds is clamped, as on the app's own route.
    assert roh(port, "PATCH", "/api/v1/config", {"workers": 99})[2]["config"]["workers"] <= 8


def test_der_status_wiegt_weniger_ohne_die_einstellungen(server):
    """The number this was about: what a poll every few seconds costs."""
    import json as _json
    _, port = server
    status = _json.dumps(roh(port, "GET", "/api/v1/status")[2])
    konfig = _json.dumps(roh(port, "GET", "/api/v1/config")[2])
    zusammen = len(status) + len(konfig)
    assert len(status) < 0.75 * zusammen, "die Einstellungen wogen ein Viertel und mehr"
    assert "outlook_categories" not in status


def test_der_status_traegt_nur_was_sich_von_selbst_aendert(server):
    """Data minimisation on the one answer that is polled: no paths, no
    folder names, no settings – each of those has its own route and is
    asked for when it can have changed."""
    _, port = server
    status = roh(port, "GET", "/api/v1/status")[2]
    for weg in ("config", "exports", "folders", "calendars", "notebooks",
                "conversations", "lists", "folders_onedrive", "data_dir",
                "home_dir", "index_dir", "app_location", "scope_queries",
                "ollama_hint", "skip_folders_default", "graph_explorer",
                # The index's own state changes with a run, not on its own:
                # since 13.0 it travels with the inventory (its columns are
                # a list of names that was repeated in every poll).
                "store"):
        assert weg not in status, weg
    assert "config" not in status["mcp"], "der Client-Schnipsel nennt Pfade"
    for da in ("token", "ollama", "jobs", "mcp", "update", "auth",
               "wizard", "schedule_next", "profile", "calendar"):
        assert da in status, da
    # The contract's version is on every answer as a header; repeating it in
    # a polled body says nothing new.
    assert "api_version" not in status
    assert roh(port, "GET", "/api/v1/app")[2]["api_version"] == app_mod.API_VERSION

    umgebung = roh(port, "GET", "/api/v1/app")[2]
    assert umgebung["data_dir"] and umgebung["scope_queries"]
    bestand = roh(port, "GET", "/api/v1/inventory")[2]
    assert "exports" in bestand and "folders" in bestand
    assert set(bestand["store"]) == {"exists", "features", "semantic", "built_at"}

    import json as _json
    assert len(_json.dumps(status)) < 1200, "der Status ist wieder schwer geworden"


def test_jeder_knoten_des_status_traegt_nur_was_die_seite_braucht(server):
    """Node by node: no list of installed models, no scopes the key has, no
    model names, ports or ids – those are settings, and what follows from
    them is decided here."""
    _, port = server
    s = roh(port, "GET", "/api/v1/status")[2]
    assert set(s["ollama"]) == {"running", "has_model", "has_chat_model", "disabled"}
    assert set(s["token"]) == {"present", "valid", "expired", "account", "name",
                               "expires_in_minutes", "missing"}
    assert "store" not in s        # with the inventory since 13.0, see above
    assert set(s["mcp"]) <= {"running", "url", "error"}
    assert set(s["auth"]) == {"signed_in", "account", "own_registration", "device"}
    assert set(s["update"]) == {"status", "latest", "url", "newer", "ahead", "error", "retry_at"}
    assert set(s["calendar"]) == {"built_at"}
    assert "schedule_enabled" not in s          # a setting, and the page has it

    # The full probes still exist where they are the point.
    voll = roh(port, "POST", "/api/v1/ollama/recheck", {})[2]
    assert "models" in voll["ollama"], "die Nachprüfung darf alles sagen"
    umgebung = roh(port, "GET", "/api/v1/app")[2]
    assert umgebung["version"] and umgebung["default_client_id"]
