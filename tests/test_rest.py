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
    call(port, "POST", "/api/faelle/ordner-anlegen", {"id": kennung, "name": "Belege"})
    call(port, "POST", "/api/faelle/notiz", {"id": kennung, "text": "Erste Notiz"})
    call(port, "POST", "/api/faelle/hinzufuegen", {"id": kennung, "eintraege": [
        {"key": "k1", "src": "outlook", "root": "outlook", "rel": "inbox/a.eml",
         "titel": "Rechnung", "datum": "2026-03-04", "wer": "Alice Beispiel"}]})
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
    assert call(port, "GET", "/api/suche/historie")[1]["searches"] == []
    roh(port, "GET", "/api/v1/search?q=budget&remember=1")
    verlauf = call(port, "GET", "/api/suche/historie")[1]["searches"]
    assert [h["kriterien"]["q"] for h in verlauf] == ["budget"]


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
        FakeSuche.gesehen = dict(kw)
        return {"backend": "bm25", "count": kw["k"],
                "results": [{"uid": f"u{kw['offset'] + i}"} for i in range(kw["k"])]}

    @staticmethod
    def search_messages(**kw):
        return FakeSuche.browse_messages(**kw)

    @staticmethod
    def similar_messages(**kw):
        return {"backend": "semantic", "count": 0, "results": []}

    @staticmethod
    def get_thread(**kw):
        return {"thread": "x", "count": 0, "messages": []}


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
    code, kopf, r = roh(port, "GET", "/api/run")
    assert code == 405 and kopf["Allow"] == "OPTIONS, POST"
    assert r["error"]["k"] == "srv.method" and r["status"] == 405
    code, kopf, _ = roh(port, "PUT", "/api/v1/cases/1")
    assert code == 405 and "PATCH" in kopf["Allow"] and "PUT" not in kopf["Allow"]
    code, kopf, _ = roh(port, "DELETE", "/api/config")
    assert code == 405 and kopf["Allow"] == "OPTIONS, POST"
    # Nothing of the sort: still a 404.
    assert roh(port, "DELETE", "/api/gibtsnicht")[0] == 404
    assert roh(port, "GET", "/api/gibtsnicht")[0] == 404


def test_options_sagt_was_hier_geht(server):
    _, port = server
    code, kopf, _ = roh(port, "OPTIONS", "/api/v1/cases/1")
    assert code == 204 and kopf["Allow"] == "DELETE, GET, HEAD, OPTIONS, PATCH"
    code, kopf, _ = roh(port, "OPTIONS", "/api/status")
    assert code == 204 and kopf["Allow"] == "GET, HEAD, OPTIONS"
    code, kopf, _ = roh(port, "OPTIONS", "/api/profiles")
    assert code == 204 and kopf["Allow"] == "GET, HEAD, OPTIONS, POST"
    assert "Access-Control-Allow-Origin" not in kopf     # one origin, ours
    assert roh(port, "OPTIONS", "/api/gibtsnicht")[0] == 404


def test_auch_eine_unbekannte_methode_bekommt_json(server):
    """What the base class refuses answers the same body – its HTML error
    page would be the one answer nobody can parse."""
    _, port = server
    code, kopf, r = roh(port, "TRACE", "/api/status")
    assert code == 501
    assert kopf["Content-Type"].startswith("application/problem+json")
    assert r["error"]["k"] == "srv.method" and "TRACE" in r["detail"]
    assert "Python" not in kopf["Server"]               # nor the version


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
    assert r["message"]["k"] == "srv.notfound"          # the app's own surface
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
    con.putrequest("POST", "/api/faelle/anlegen")
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
        b"POST /api/config HTTP/1.1\r\nHost: __HOST__\r\n"
        b"Content-Type: text/plain\r\nContent-Length: 6\r\n\r\nname=x",
        b"GET /api/status HTTP/1.1\r\nHost: __HOST__\r\n\r\n")
    assert "415" in antwort and "name=x" not in antwort
    assert antwort.count("HTTP/1.") == 2 and "200 OK" in antwort


def test_auch_die_basisklasse_schliesst_nach_ihrer_absage(server):
    """What the base class refuses – an unknown verb – it refuses before
    reading anything, so its answer has to end the connection."""
    _, port = server
    antwort = _pipeline(
        port,
        b"TRACE /api/status HTTP/1.1\r\nHost: __HOST__\r\n"
        b"Content-Length: 6\r\n\r\nABCDEF",
        b"GET /api/status HTTP/1.1\r\nHost: __HOST__\r\n\r\n")
    assert "501" in antwort and "ABCDEF" not in antwort
    assert "Connection: close" in antwort and antwort.count("HTTP/1.") == 1


def test_kein_rumpf_kein_content_length(server):
    """RFC 9110 §8.6: a 204 carries neither."""
    _, port = server
    code, kopf, _ = roh(port, "OPTIONS", "/api/status")
    assert code == 204 and "Content-Length" not in kopf and "Content-Type" not in kopf
    roh(port, "POST", "/api/v1/cases", {"name": "Nordwind"})
    code, kopf, _ = roh(port, "DELETE", "/api/v1/cases/1")
    assert code == 204 and "Content-Length" not in kopf


def test_head_nennt_die_methode_die_gefragt_hat(server):
    """do_HEAD goes through do_GET – the refusal must still say HEAD."""
    _, port = server
    code, kopf, _ = roh(port, "HEAD", "/api/token")
    assert code == 405 and kopf["Allow"] == "OPTIONS, POST"


def test_auch_die_hostpruefung_verschluckt_den_rumpf(server):
    """The Host check answers before any route – and before the body would
    have been read."""
    _, port = server
    antwort = _pipeline(
        port,
        b"POST /api/config HTTP/1.1\r\nHost: angreifer.example.com\r\n"
        b"Content-Type: application/json\r\nContent-Length: 13\r\n\r\n{\"name\": \"x\"}",
        b"GET /api/status HTTP/1.1\r\nHost: __HOST__\r\n\r\n")
    assert "403" in antwort and antwort.count("HTTP/1.") == 2
    assert "200 OK" in antwort and '{"name"' not in antwort.split("403")[1][:400]


def test_eine_unbrauchbare_zahl_ist_ueberall_eine_schlechte_anfrage(server, monkeypatch):
    """A caller's typo is a 400 on every route, not the 500 an uncaught
    ValueError used to make of it."""
    a, port = server
    monkeypatch.setattr(a.search, "ensure", lambda cfg: FakeSuche)
    for pfad in ("/api/search?k=abc", "/api/search?offset=abc", "/api/similar?cid=abc",
                 "/api/thread?key=x&limit=abc", "/api/runs?limit=abc",
                 "/api/run-log?id=abc", "/api/log?since=abc",
                 "/api/faelle/fall?id=abc", "/api/v1/search?limit=abc"):
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
    for methode, pfad in (("GET", "/api/status"), ("GET", "/api/v1/cases"),
                          ("GET", "/api/gibtsnicht"), ("OPTIONS", "/api/status"),
                          ("POST", "/api/v1/cases")):
        _, kopf, _ = roh(port, methode, pfad, {} if methode == "POST" else None)
        assert kopf["X-Munimentum-Version"] == erwartet, (methode, pfad)
        assert kopf["X-Munimentum-Api"] == app_mod.API_VERSION
    assert app_mod.API_V1 == "/api/" + app_mod.API_VERSION


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
    assert "config" not in roh(port, "GET", "/api/status")[2]

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
    status = _json.dumps(roh(port, "GET", "/api/status")[2])
    konfig = _json.dumps(roh(port, "GET", "/api/v1/config")[2])
    zusammen = len(status) + len(konfig)
    assert len(status) < 0.75 * zusammen, "die Einstellungen wogen ein Viertel und mehr"
    assert "outlook_categories" not in status


def test_der_status_traegt_nur_was_sich_von_selbst_aendert(server):
    """Data minimisation on the one answer that is polled: no paths, no
    folder names, no settings – each of those has its own route and is
    asked for when it can have changed."""
    _, port = server
    status = roh(port, "GET", "/api/status")[2]
    for weg in ("config", "exports", "folders", "calendars", "notebooks",
                "conversations", "lists", "folders_onedrive", "data_dir",
                "home_dir", "index_dir", "app_location", "scope_queries",
                "ollama_hint", "skip_folders_default", "graph_explorer"):
        assert weg not in status, weg
    assert "config" not in status["mcp"], "der Client-Schnipsel nennt Pfade"
    for da in ("token", "ollama", "store", "jobs", "mcp", "update", "auth",
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

    import json as _json
    assert len(_json.dumps(status)) < 1200, "der Status ist wieder schwer geworden"


def test_jeder_knoten_des_status_traegt_nur_was_die_seite_braucht(server):
    """Node by node: no list of installed models, no scopes the key has, no
    model names, ports or ids – those are settings, and what follows from
    them is decided here."""
    _, port = server
    s = roh(port, "GET", "/api/status")[2]
    assert set(s["ollama"]) == {"running", "has_model", "has_chat_model", "disabled"}
    assert set(s["token"]) == {"present", "valid", "expired", "account", "name",
                               "expires_in_minutes", "missing"}
    assert set(s["store"]) == {"exists", "features", "semantic", "built_at"}
    assert set(s["mcp"]) <= {"running", "url", "error"}
    assert set(s["auth"]) == {"signed_in", "account", "own_registration", "device"}
    assert set(s["update"]) == {"status", "latest", "url", "newer", "ahead", "error"}
    assert set(s["calendar"]) == {"built_at"}
    assert "schedule_enabled" not in s          # a setting, and the page has it

    # The full probes still exist where they are the point.
    voll = roh(port, "POST", "/api/ollama-recheck", {})[2]
    assert "models" in voll, "die Nachprüfung darf alles sagen"
    umgebung = roh(port, "GET", "/api/v1/app")[2]
    assert umgebung["version"] and umgebung["default_client_id"]
