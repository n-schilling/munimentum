"""
MCP × cases (11.0): the `case` filter of search/browse, the case tools,
the saved-search tools and the write switch.

The store is the one from test_mcp_server, with keys assigned the way
the index does it (schluessel.zuweisen – here without export folders, so
every key is a path key); the case book lies next to it in a temp
folder and STATE names it, exactly as the app and main() do.
"""

import anyio
import numpy as np
import pytest
from mcp.client.client import Client

import corpus
import faelle
import mcp_server
import rag_index
import schluessel
import store_layout
from tests.test_mcp_server import (_build_store, _sample_records,
                                   UID_M1, UID_M2, UID_T0, UID_T1, UID_T2, UID_CAL, _payload)
from tests.hilfen import ohne_schluesselspalte


@pytest.fixture
def welt(tmp_path, monkeypatch):
    """Store with keys + an empty case book, STATE pointing at both."""
    store, chunks, teams_dir, outlook_dir = _build_store(tmp_path)
    # _build_store writes chunks without keys – give them theirs and
    # rewrite, as rag_index.lese_bestand does at the end of a run.
    chunks = corpus.chunk_records(_sample_records())
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    schluessel.zuweisen(chunks, {})
    rag_index.write_db(store, chunks)
    buch = faelle.Fallbuch(tmp_path / "heim" / faelle.DB_NAME)
    old = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    V = np.load(store_layout.vectors_path(store), mmap_mode="r")
    mcp_server.STATE.update(
        db=str(store / "corpus.db"), V=V, np=np, semantic=True,
        vector_dtype=str(V.dtype), teams_dir=str(teams_dir),
        outlook_dir=str(outlook_dir), embed_model="test-embed",
        ollama="http://127.0.0.1:1",
        faelle_db=str(buch.pfad), cases_write=False)

    def _kein_netz(text):
        raise RuntimeError("Embedding nicht gestubbt")
    monkeypatch.setattr(mcp_server, "_embed_query", _kein_netz)
    yield {"store": store, "chunks": chunks, "buch": buch, "tmp": tmp_path}
    mcp_server.STATE.clear()
    mcp_server.STATE.update(old)


def _key(welt, uid):
    return next(c["key"] for c in welt["chunks"] if c["uid"] == uid)


def _eintrag(welt, uid):
    c = next(c for c in welt["chunks"] if c["uid"] == uid)
    return {"key": c["key"], "src": c["src"], "root": c["root"], "rel": c["rel"],
            "titel": c["title"], "datum": c["date"], "wer": c["who"]}


def _nordwind(welt, *uids):
    """A case "Nordwind" holding the given items."""
    buch = welt["buch"]
    fid = buch.fall_anlegen("Nordwind", "Die Rechnung 4711")
    buch.hinzufuegen(fid, [_eintrag(welt, u) for u in uids])
    return fid


# --------------------------------------------------------------------------
# The case filter on search and browse
# --------------------------------------------------------------------------
def test_die_temp_tabelle_geht_auf_der_lesenden_verbindung(welt):
    """_db() opens read-only – the temp table for the keys must still work."""
    con = mcp_server._db()
    try:
        mcp_server._keys_tabelle(con, {"a", "b"})
        assert con.execute("SELECT COUNT(*) FROM fallkeys").fetchone()[0] == 2
        mcp_server._keys_tabelle(con, {"c"})           # refilled, not appended
        assert [r[0] for r in con.execute("SELECT key FROM fallkeys")] == ["c"]
    finally:
        con.close()


def test_suche_im_fall_liefert_nur_dessen_treffer(welt):
    _nordwind(welt, UID_M1, UID_T0)
    alle = mcp_server.search_messages("Rechnung 4711")
    assert alle["count"] >= 3                       # mail + chat + more
    im_fall = mcp_server.search_messages("Rechnung 4711", case="Nordwind")
    assert {h["uid"] for h in im_fall["results"]} == {UID_M1, UID_T0}
    # by id and case-insensitively by name as well
    fid = welt["buch"].faelle()[0]["id"]
    assert mcp_server.search_messages("Rechnung", case=str(fid))["count"] == 2
    assert mcp_server.search_messages("Rechnung", case="nordwind")["count"] == 2


def test_jeder_treffer_traegt_schluessel_und_faelle(welt):
    fid = _nordwind(welt, UID_M1)
    res = mcp_server.search_messages("Rechnung 4711")
    m1 = next(h for h in res["results"] if h["uid"] == UID_M1)
    assert m1["key"] == _key(welt, UID_M1)
    assert m1["cases"] == [{"id": fid, "name": "Nordwind", "status": "offen"}]
    andere = next(h for h in res["results"] if h["uid"] != UID_M1)
    assert andere["cases"] == []
    assert andere["key"]


def test_stoebern_im_fall(welt):
    _nordwind(welt, UID_M2, UID_CAL)
    res = mcp_server.browse_messages(case="Nordwind", k=50)
    assert {h["uid"] for h in res["results"]} == {UID_M2, UID_CAL}
    # newest first, as browse always does
    assert [h["uid"] for h in res["results"]] == [UID_M2, UID_CAL]
    # combined with the other filters
    nur_mail = mcp_server.browse_messages(case="Nordwind", source="outlook")
    assert [h["uid"] for h in nur_mail["results"]] == [UID_M2]


def test_unbekannter_fall_ist_ein_fehler_kein_leeres_ergebnis(welt):
    res = mcp_server.search_messages("Rechnung", case="Gibt es nicht")
    assert "No case named" in res["error"] and res["results"] == []
    res = mcp_server.browse_messages(case="77")
    assert "No case named" in res["error"]


def test_ohne_fallbuch_sagt_der_server_das(welt):
    mcp_server.STATE["faelle_db"] = None
    res = mcp_server.search_messages("Rechnung", case="Nordwind")
    assert "case book" in res["error"]
    assert mcp_server.list_cases()["cases"] == [] and "case book" in mcp_server.list_cases()["error"]
    assert "case book" in mcp_server.get_case("Nordwind")["error"]
    # without a case the search does not even look for the book
    assert mcp_server.search_messages("Rechnung")["count"] >= 1
    assert "cases" not in mcp_server.search_messages("Rechnung")["results"][0]


def test_alter_index_ohne_schluesselspalte(welt):
    """An index from before 11.0: the filter says why instead of failing."""
    _nordwind(welt, UID_M1)
    ohne_schluesselspalte(welt["store"] / "corpus.db")
    res = mcp_server.search_messages("Rechnung", case="Nordwind")
    assert "predates item keys" in res["error"]
    assert "predates item keys" in mcp_server.case_timeline("Nordwind")["error"]
    assert "predates item keys" in mcp_server.case_people("Nordwind")["error"]
    # plain hits still work, with key None and no cases lookup crashing
    treffer = mcp_server.search_messages("Rechnung")["results"]
    assert treffer and treffer[0]["key"] is None and treffer[0]["cases"] == []


# --------------------------------------------------------------------------
# The case tools
# --------------------------------------------------------------------------
def test_list_cases_und_get_case(welt):
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1, UID_T0)
    buch.notiz(fid, "Erste Notiz")
    zu = buch.fall_anlegen("Alt", "")
    buch.schliessen(zu)
    res = mcp_server.list_cases()
    assert res["count"] == 2
    assert [c["name"] for c in res["cases"]] == ["Nordwind", "Alt"]     # closed last
    nw = res["cases"][0]
    assert nw["status"] == "open" and nw["items"] == 2 and nw["notes"] == 1
    assert nw["items_per_source"] == {"outlook": 1, "teams": 1}
    assert mcp_server.list_cases(include_closed=False)["count"] == 1
    voll = mcp_server.get_case("Nordwind")
    assert voll["description"] == "Die Rechnung 4711"
    assert [n["text"] for n in voll["notes"]] == ["Erste Notiz"]
    assert {e["key"] for e in voll["items"]} == {_key(welt, UID_M1), _key(welt, UID_T0)}
    mail = next(e for e in voll["items"] if e["source"] == "outlook")
    assert mail["title"] == "Rechnung 4711 freigegeben" and mail["source_label"] == "Mail"
    assert mail["uri"] == mcp_server._source_uri("outlook", "inbox/mail1.eml")
    assert voll["result_lists"] == [] and voll["saved_searches"] == []


def test_get_case_kennt_listen_und_angehaengte_suchen(welt):
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1)
    k = faelle.kriterien({"q": "Rechnung", "source": "outlook"})
    lid, neu = buch.liste_anlegen(fid, k, [_eintrag(welt, UID_M2), _eintrag(welt, UID_M1)])
    assert neu == 1                                  # M1 was already there
    buch.speichern("Rechnungen", k, fid)
    voll = mcp_server.get_case(str(fid))
    assert len(voll["result_lists"]) == 1
    li = voll["result_lists"][0]
    assert li["hits"] == 2 and li["criteria"]["query"] == "Rechnung"
    assert li["criteria"]["source"] == "outlook"
    m2 = next(e for e in voll["items"] if e["key"] == _key(welt, UID_M2))
    assert m2["from_result_list"] == lid
    assert voll["saved_searches"][0]["name"] == "Rechnungen"
    assert voll["saved_searches"][0]["case"] == "Nordwind"


def test_get_case_fehler(welt):
    assert "Name a case" in mcp_server.get_case("")["error"]
    assert "No case named" in mcp_server.get_case("Nirgends")["error"]


def test_case_timeline_ist_chronologisch_mit_auszug(welt):
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1, UID_T1, UID_T0, UID_CAL)
    # an item the index does not hold any more
    buch.hinzufuegen(fid, [{"key": "mail:weg", "src": "outlook", "root": "outlook",
                            "rel": "inbox/weg.eml", "titel": "Verschwunden"}])
    res = mcp_server.case_timeline("Nordwind")
    assert res["case"] == "Nordwind" and res["count"] == 4
    assert [h["uid"] for h in res["items"]] == [UID_T0, UID_T1, UID_M1, UID_CAL]
    assert res["items"][0]["preview"].startswith("Hallo Bob")
    assert [e["title"] for e in res["not_in_index"]] == ["Verschwunden"]
    assert mcp_server.case_timeline("Nordwind", limit=2)["count"] == 2
    ohne = mcp_server.case_timeline("Nordwind", preview_chars=0)
    assert "preview" not in ohne["items"][0]


def test_case_people_zaehlt_die_beteiligten(welt):
    _nordwind(welt, UID_M1, UID_T0, UID_T1, UID_T2, UID_CAL)
    res = mcp_server.case_people("Nordwind")
    assert res["people"][0] == {"name": "Alice Beispiel", "items": 3}
    namen = [p["name"] for p in res["people"]]
    assert namen == ["Alice Beispiel", "Bob Baumeister", "Carla Chef"]
    assert mcp_server.case_people("Nordwind", limit=1)["count"] == 1


def test_case_people_laesst_unbekannte_weg(welt):
    from tests.test_mcp_server import UID_TX
    _nordwind(welt, UID_TX)
    assert mcp_server.case_people("Nordwind")["people"] == []


def test_case_new_hits_findet_was_der_fall_noch_nicht_hat(welt):
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1)
    buch.speichern("Rechnungen", faelle.kriterien({"q": "Rechnung 4711"}), fid)
    buch.speichern("Alles Mail", faelle.kriterien({"source": "outlook"}), fid)   # browse
    res = mcp_server.case_new_hits("Nordwind")
    assert [b["search"] for b in res["searches"]] == ["Alles Mail", "Rechnungen"]
    rech = next(b for b in res["searches"] if b["search"] == "Rechnungen")
    assert UID_M1 not in {h["uid"] for h in rech["new"]}
    assert UID_T0 in {h["uid"] for h in rech["new"]}
    mail = next(b for b in res["searches"] if b["search"] == "Alles Mail")
    assert {h["uid"] for h in mail["new"]} == {UID_M2, "outlook:sent/protokoll.eml:0"}
    assert mail["new_count"] == 2
    # nothing attached: nothing to say
    leer = buch.fall_anlegen("Leer", "")
    assert mcp_server.case_new_hits(str(leer))["searches"] == []


def test_case_new_hits_traegt_fehler_je_suche(welt):
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1)
    buch.speichern("Semantisch", faelle.kriterien({"q": "Rechnung", "mode": "aehnlich"}), fid)
    res = mcp_server.case_new_hits("Nordwind")
    assert res["searches"][0]["error"]               # embedding is stubbed to fail
    assert res["searches"][0]["new"] == []


# --------------------------------------------------------------------------
# Saved searches
# --------------------------------------------------------------------------
def test_gespeicherte_suchen_laufen_wie_gespeichert(welt):
    buch = welt["buch"]
    sid = buch.speichern("Mails von Carla", faelle.kriterien(
        {"q": "", "person": "Carla", "source": "outlook"}))
    res = mcp_server.list_saved_searches()
    assert res["count"] == 1
    g = res["searches"][0]
    assert g["name"] == "Mails von Carla" and g["last_run"] is None and g["case"] is None
    assert g["criteria"] == {"query": "", "mode": "text", "person": "Carla",
                             "source": "outlook", "date_from": "", "date_to": "",
                             "folder": "", "filetype": "", "only_gone": False, "case": None}
    lauf = mcp_server.run_saved_search("Mails von Carla")
    assert lauf["search"] == "Mails von Carla"
    assert [h["uid"] for h in lauf["results"]] == [UID_M1]      # browse: person + source
    # ran: last_run and hits_then are recorded
    danach = buch.gespeichert(sid)
    assert danach["zuletzt"] and danach["treffer"] == 1
    # by id, with words → search_messages
    sid2 = buch.speichern("Rechnung", faelle.kriterien({"q": "Rechnung 4711", "mode": "text"}))
    lauf2 = mcp_server.run_saved_search(str(sid2), k=1)
    assert lauf2["count"] == 1 and lauf2["results"][0]["score"] is not None
    assert "No saved search" in mcp_server.run_saved_search("Nie")["error"]


def test_gespeicherte_suche_mit_fallfilter(welt):
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1)
    buch.speichern("Im Fall", faelle.kriterien({"q": "Rechnung", "fall": fid}))
    lauf = mcp_server.run_saved_search("Im Fall")
    assert [h["uid"] for h in lauf["results"]] == [UID_M1]


# --------------------------------------------------------------------------
# Writing – only with the switch
# --------------------------------------------------------------------------
def test_schreiben_ist_ohne_schalter_gesperrt(welt):
    fid = _nordwind(welt)
    res = mcp_server.add_to_case("Nordwind", uids=[UID_M1])
    assert "switched off" in res["error"]
    assert welt["buch"].keys(fid) == set()
    assert "switched off" in mcp_server.add_case_note("Nordwind", "x")["error"]
    assert welt["buch"].notizen(fid) == []


def test_add_to_case_mit_schalter(welt):
    mcp_server.STATE["cases_write"] = True
    fid = _nordwind(welt, UID_M1)
    res = mcp_server.add_to_case("Nordwind", uids=[UID_M1, UID_T0, "outlook:nix:0"],
                                 keys=[_key(welt, UID_CAL)])
    assert res == {"case": "Nordwind", "added": 2, "already_there": 1, "not_found": 1}
    assert welt["buch"].keys(fid) == {_key(welt, UID_M1), _key(welt, UID_T0), _key(welt, UID_CAL)}
    e = next(e for e in welt["buch"].eintraege(fid) if e["key"] == _key(welt, UID_T0))
    assert e["titel"] == "Projekt Alpha" and e["wer"] == "Alice Beispiel" and e["src"] == "teams"
    # the hit now says so
    h = next(h for h in mcp_server.search_messages("Rechnung 4711")["results"] if h["uid"] == UID_T0)
    assert h["cases"][0]["name"] == "Nordwind"


def test_add_to_case_und_notiz_an_geschlossenem_fall(welt):
    mcp_server.STATE["cases_write"] = True
    fid = _nordwind(welt)
    welt["buch"].schliessen(fid)
    assert "closed" in mcp_server.add_to_case("Nordwind", uids=[UID_M1])["error"]
    assert "closed" in mcp_server.add_case_note("Nordwind", "spät")["error"]


def test_add_case_note(welt):
    mcp_server.STATE["cases_write"] = True
    fid = _nordwind(welt)
    res = mcp_server.add_case_note("Nordwind", "  Rückruf am Montag  ")
    assert res["case"] == "Nordwind" and res["note_id"]
    assert [n["text"] for n in welt["buch"].notizen(fid)] == ["Rückruf am Montag"]
    assert mcp_server.add_case_note("Nordwind", "   ")["error"] == "The note is empty."
    assert "No case named" in mcp_server.add_case_note("Nirgends", "x")["error"]


# --------------------------------------------------------------------------
# Over the wire
# --------------------------------------------------------------------------
def test_die_falltools_sprechen_mcp(welt):
    fid = _nordwind(welt, UID_M1)

    async def run():
        async with Client(mcp_server.mcp) as c:
            faelle_ = _payload(await c.call_tool("list_cases", {}))
            fall = _payload(await c.call_tool("get_case", {"case": "Nordwind"}))
            treffer = _payload(await c.call_tool("search_messages",
                                                 {"query": "Rechnung", "case": str(fid)}))
            gesperrt = _payload(await c.call_tool("add_to_case",
                                                  {"case": "Nordwind", "uids": [UID_T0]}))
            return faelle_, fall, treffer, gesperrt
    faelle_, fall, treffer, gesperrt = anyio.run(run)
    assert faelle_["cases"][0]["name"] == "Nordwind"
    assert fall["items"][0]["title"] == "Rechnung 4711 freigegeben"
    assert [h["uid"] for h in treffer["results"]] == [UID_M1]
    assert "switched off" in gesperrt["error"]


def test_die_anleitung_nennt_die_faelle():
    assert "list_cases" in mcp_server._INSTRUCTIONS
    assert "case_new_hits" in mcp_server._INSTRUCTIONS
    assert "run_saved_search" in mcp_server._INSTRUCTIONS
