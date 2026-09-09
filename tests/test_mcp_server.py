"""Tests for mcp_server.py – MCP tools on top of a small, real store.

The store (corpus.db + vector file) is built per test in tmp_path with the
write helpers from rag_index.py – this guarantees the schema is identical to
what mcp_server.py expects. NO network calls are made: _embed_query is always
stubbed (default: raises, as with "Ollama down"); tests of the semantic
search install deterministic unit vectors.
"""

import json
from datetime import date, datetime
from urllib.parse import quote

import anyio
import sqlite3

import numpy as np
import sys

import pytest
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import LATEST_PROTOCOL_VERSION

import corpus
import mcp_server
import rag_index
import store_layout

# --------------------------------------------------------------------------
# Test data: small corpus with Teams, Outlook, calendar and contact entries
# --------------------------------------------------------------------------
DIM = 16  # vector dimension: every chunk gets a unit vector of its own

UID_T0 = "teams:1on1/alice__chat.html:0"
UID_T1 = "teams:1on1/alice__chat.html:1"
UID_T2 = "teams:1on1/alice__chat.html:2"
UID_TX = "teams:1on1/max__chat.html:0"
UID_M1 = "outlook:inbox/mail1.eml:0"
UID_M2 = "outlook:inbox/mail2.eml:0"
UID_M3 = "outlook:sent/protokoll.eml:0"
UID_CAL = "kalender:kalender/Arbeit/termin.ics:0"
UID_CON = "kontakte:kontakte/Team/alice.vcf:0"

# Long mail → several overlapping chunks (exercises _join_chunks/get_document)
LONG_TEXT = " ".join(
    f"Absatz {i}: die Quartalsplanung wurde ausführlich besprochen und Punkt {i} im Protokoll festgehalten."
    for i in range(50))

# Deliberately pure ASCII: the window tests of read_source_file cut at byte
# boundaries; multi-byte characters would (correctly) turn into replacement
# characters there.
TEAMS_FILE_CONTENT = "<html><body>Chatverlauf Alice und Bob - Projekt Alpha</body></html>"
MAIL_FILE_CONTENT = "From: carla@example.com\nSubject: Rechnung 4711\n\nDie Rechnung ist freigegeben.\n"


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


def _rec(uid, src, root, rel, who, ppl, ts, date, title, ctx, text):
    return {"uid": uid, "src": src, "root": root, "rel": rel, "who": who,
            "ppl": ppl, "ts": ts, "date": date, "title": title, "ctx": ctx,
            "text": text}


def _sample_records():
    return [
        _rec(UID_T0, "teams", "teams", "1on1/alice__chat.html", "Alice Beispiel",
             "alice beispiel projekt alpha", _ts("2025-06-01 09:30"),
             "2025-06-01 09:30", "Projekt Alpha", "1on1",
             "Hallo Bob, die Rechnung 4711 für Projekt Alpha ist fertig."),
        _rec(UID_T1, "teams", "teams", "1on1/alice__chat.html", "Bob Baumeister",
             "bob baumeister projekt alpha", _ts("2025-06-01 09:35"),
             "2025-06-01 09:35", "Projekt Alpha", "1on1",
             "Danke Alice, ich prüfe die Rechnung morgen früh."),
        _rec(UID_T2, "teams", "teams", "1on1/alice__chat.html", "Alice Beispiel",
             "alice beispiel projekt alpha", _ts("2025-06-01 09:40"),
             "2025-06-01 09:40", "Projekt Alpha", "1on1",
             "Perfekt, dann bis morgen im Büro!"),
        _rec(UID_TX, "teams", "teams", "1on1/max__chat.html", "(unbekannt)",
             "max mustermann", _ts("2025-06-02 10:00"),
             "2025-06-02 10:00", "Max", "1on1",
             "Kurze Notiz ohne bekannten Absender."),
        _rec(UID_M1, "outlook", "outlook", "inbox/mail1.eml", "Carla Chef",
             "carla chef carla@example.com alice beispiel alice@example.com",
             _ts("2025-06-10 08:00"), "2025-06-10 08:00",
             "Rechnung 4711 freigegeben", "inbox",
             "Hallo zusammen, die Rechnung 4711 ist freigegeben und kann verschickt werden."),
        _rec(UID_M2, "outlook", "outlook", "inbox/mail2.eml", "Alice Beispiel",
             "alice beispiel alice@example.com", _ts("2025-07-01 12:00"),
             "2025-07-01 12:00", "Urlaubsantrag August", "inbox",
             "Hiermit beantrage ich Urlaub vom 4. bis 15. August. Viele Grüße, Alice"),
        _rec(UID_M3, "outlook", "outlook", "sent/protokoll.eml", "Doris Docs",
             "doris docs doris@example.com", _ts("2025-05-20 16:00"),
             "2025-05-20 16:00", "Protokoll Quartalsplanung", "sent", LONG_TEXT),
        _rec(UID_CAL, "kalender", "outlook", "kalender/Arbeit/termin.ics",
             "Alice Beispiel", "alice beispiel bob baumeister",
             _ts("2025-06-15 14:00"), "2025-06-15 14:00", "Quartalsplanung",
             "kalender/Arbeit", "Ort: Raum 42. Agenda folgt."),
        _rec(UID_CON, "kontakte", "outlook", "kontakte/Team/alice.vcf", "",
             "alice beispiel alice@example.com", None, "", "Alice Beispiel",
             "kontakte/Team", "Firma GmbH · Entwicklung. E-Mail: alice@example.com"),
    ]


# Newest first; contact (ts = NULL) at the end – expected browse order
BROWSE_ORDER = [UID_M2, UID_CAL, UID_M1, UID_TX, UID_T2, UID_T1, UID_T0,
                UID_M3, UID_CON]


def _build_store(tmp_path):
    """Create store + export folders in tmp_path (write path from rag_index.py)."""
    store = tmp_path / "rag_store"
    store.mkdir()
    teams_dir = tmp_path / "teams_export"
    outlook_dir = tmp_path / "outlook_export"
    (teams_dir / "1on1").mkdir(parents=True)
    (outlook_dir / "inbox").mkdir(parents=True)
    (teams_dir / "1on1" / "alice__chat.html").write_text(
        TEAMS_FILE_CONTENT, encoding="utf-8")
    (outlook_dir / "inbox" / "mail1.eml").write_text(
        MAIL_FILE_CONTENT, encoding="utf-8")
    # File OUTSIDE the exports – must never be reachable via read_source_file
    (tmp_path / "geheim.txt").write_text("STRENG GEHEIM", encoding="utf-8")

    chunks = corpus.chunk_records(_sample_records())
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    assert len(chunks) <= DIM, "Testkorpus zu groß für die Vektor-Dimension"
    # Chunk i → unit vector e_i: cosine to the query vector q is exactly q[i]
    V = np.zeros((len(chunks), DIM), dtype="float32")
    for i in range(len(chunks)):
        V[i, i] = 1.0
    rag_index.write_db(store, chunks)
    _, vp = rag_index.save_vectors(store, V)
    rag_index.write_info(store, "test-embed", DIM, len(chunks), vp)
    return store, chunks, teams_dir, outlook_dir


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Build the store, fill STATE, and restore it after the test.

    _embed_query raises by default (no network!); semantic tests override
    the stub with deterministic vectors.
    """
    store, chunks, teams_dir, outlook_dir = _build_store(tmp_path)
    old = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    V = np.load(store_layout.vectors_path(store), mmap_mode="r")
    mcp_server.STATE.update(
        db=str(store / "corpus.db"), V=V, np=np, semantic=True,
        vector_dtype=str(V.dtype), teams_dir=str(teams_dir),
        outlook_dir=str(outlook_dir), embed_model="test-embed",
        ollama="http://127.0.0.1:1")

    def _kein_netz(text):
        raise RuntimeError("Embedding nicht gestubbt (Tests machen kein Netzwerk)")

    monkeypatch.setattr(mcp_server, "_embed_query", _kein_netz)
    yield {"store": store, "chunks": chunks, "tmp": tmp_path,
           "teams_dir": teams_dir, "outlook_dir": outlook_dir}
    mcp_server.STATE.clear()
    mcp_server.STATE.update(old)


@pytest.fixture
def empty_state():
    """Clear STATE (server not initialized) and restore it afterwards."""
    old = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    yield
    mcp_server.STATE.clear()
    mcp_server.STATE.update(old)


def _stub_semantic(monkeypatch, chunks, weights):
    """Stub _embed_query so that uid → weight dictates the cosine ranking."""
    q = np.zeros(DIM, dtype="float32")
    for i, c in enumerate(chunks):
        q[i] = weights.get(c["uid"], 0.0)
    nrm = np.linalg.norm(q)
    if nrm:
        q = q / nrm
    monkeypatch.setattr(mcp_server, "_embed_query", lambda text: q)


def _uids(res):
    return [h["uid"] for h in res["results"]]


# --------------------------------------------------------------------------
# Helper functions (no store needed)
# --------------------------------------------------------------------------
def test_to_ts_parses_and_clamps_day_end():
    assert mcp_server._to_ts("2025-06-01", False) == datetime(2025, 6, 1).timestamp()
    assert mcp_server._to_ts("2025-06-01", True) == datetime(2025, 6, 1, 23, 59, 59).timestamp()
    assert mcp_server._to_ts("", False) is None
    assert mcp_server._to_ts(None, True) is None
    # Given but unreadable: an error, not a missing value. Otherwise the
    # search would run without this bound and present that as the answer.
    for schlecht in ("01.06.2025", "2021-06-31", "gestern"):
        with pytest.raises(ValueError):
            mcp_server._to_ts(schlecht, False)


def test_where_builds_fragments():
    w, p = mcp_server._where("", None, None, "all")
    # _WHERE_ALL is the fast-path marker in _semantic_rank – pin its value
    assert w == "1=1" and w == mcp_server._WHERE_ALL and p == []
    w, p = mcp_server._where("Alice", 1.0, 2.0, "teams")
    assert "src = ?" in w and "ppl LIKE ?" in w
    assert "ts >= ?" in w and "ts <= ?" in w
    assert p == ["teams", "%alice%", 1.0, 2.0]  # person is lowercased


def test_fts_match_sanitizes_query():
    # Free text becomes an OR list of quoted tokens – FTS5 syntax
    # (AND/OR/NEAR, parentheses, quotation marks) cannot be injected.
    assert mcp_server._fts_match('Rechnung: 4711 AND "x(y)') == '"rechnung" OR "4711" OR "and" OR "x" OR "y"'
    assert mcp_server._fts_match("Größe") == '"größe"'
    assert mcp_server._fts_match("...!!!") == ""
    assert mcp_server._fts_match("") == ""


def test_rrf_merge_orders_by_reciprocal_rank():
    sem = [(1, 0.9), (2, 0.5)]
    lex = [(2, -1.0), (3, -2.0)]
    merged = mcp_server._rrf_merge(sem, lex)
    assert [cid for cid, _ in merged] == [2, 1, 3]  # 2 is in both lists
    scores = dict(merged)
    assert scores[2] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[1] == pytest.approx(1 / 61)


def test_join_chunks_removes_overlap():
    rows = [{"text": "abcdef"}, {"text": "defghi"}, {"text": "xyz"}]
    assert mcp_server._join_chunks(rows) == "abcdefghixyz"
    assert mcp_server._join_chunks([{"text": ""}, {"text": "abc"}]) == "abc"
    assert mcp_server._join_chunks([]) == ""


def test_source_uri_percent_encodes_path():
    uri = mcp_server._source_uri("teams", "1on1/alice chat.html")
    assert uri == "o365://teams/1on1%2Falice%20chat.html"


def test_read_window_replaces_clipped_utf8(tmp_path):
    f = tmp_path / "umlaut.txt"
    f.write_bytes("ää".encode())  # 4 bytes
    text, total, start, more = mcp_server._read_window(f, 0, 3)
    assert total == 4 and start == 0 and more
    assert text.startswith("ä") and "�" in text  # clipped sequence


# --------------------------------------------------------------------------
# corpus_stats
# --------------------------------------------------------------------------
def test_corpus_stats_counts_per_source(state):
    chunks = state["chunks"]
    out = mcp_server.corpus_stats()
    assert out["chunks"] == len(chunks)
    assert out["by_source"]["teams"] == {"chunks": 4, "messages": 4}
    assert out["by_source"]["kalender"] == {"chunks": 1, "messages": 1}
    assert out["by_source"]["kontakte"] == {"chunks": 1, "messages": 1}
    n_outlook = sum(1 for c in chunks if c["src"] == "outlook")
    assert n_outlook > 3  # the long mail really was split into several chunks
    assert out["by_source"]["outlook"] == {"chunks": n_outlook, "messages": 3}
    assert out["semantic_available"] is True
    assert out["default_backend"] == "hybrid"
    assert out["embed_model"] == "test-embed"
    assert out["vector_dtype"] == "float16"
    assert out["teams_dir"] == str(state["teams_dir"])


def test_corpus_stats_kennt_die_raender_des_archivs(state):
    """Claude used to answer over an archive whose reach it did not know.
    Coverage and gaps come from the analytics block, the last successful
    run per source from runs.db – both read-only, both optional."""
    import time
    from datetime import date
    import analytics_db
    import run_history
    analytics_db.baue(state["store"], {"teams": state["teams_dir"],
                                       "outlook": state["outlook_dir"]})
    h = run_history.RunHistory(state["tmp"] / "runs.db")
    rid = h.start_run("job.export", "manual")
    h.record_step(rid, "outlook", "job.step.outlook", time.time() - 60,
                  duration_s=1, ok=True)
    h.record_step(rid, "index", "job.step.index", time.time() - 30,
                  duration_s=1, ok=True)
    h.record_step(rid, "teams", "job.step.teams", time.time() - 20,
                  duration_s=1, ok=False)              # a failed one
    h.finish_run(rid, "done")
    mcp_server.STATE["runs_db"] = str(state["tmp"] / "runs.db")
    out = mcp_server.corpus_stats()
    block = analytics_db.lies(state["store"])
    assert out["index_built_at"] == block["built_at"]
    if block["komm"]["von"]:
        assert out["coverage"]["from"] == date.fromtimestamp(
            block["komm"]["von"]).isoformat()
        assert out["coverage"]["to"] >= out["coverage"]["from"]
    assert isinstance(out["gaps"], list)
    assert set(out["last_successful_runs"]) == {"outlook", "index"}
    assert "teams" not in out["last_successful_runs"], "failed run counted"


def test_corpus_stats_ohne_block_und_historie_bleibt_ruhig(state):
    mcp_server.STATE.pop("runs_db", None)
    out = mcp_server.corpus_stats()
    assert out["coverage"] == {"from": None, "to": None}
    assert out["gaps"] == [] and out["last_successful_runs"] == {}
    assert out["index_built_at"] is None


def test_archive_analytics_liefert_den_block(state):
    import analytics_db
    assert "error" in mcp_server.archive_analytics()
    analytics_db.baue(state["store"], {"teams": state["teams_dir"],
                                       "outlook": state["outlook_dir"]})
    block = mcp_server.archive_analytics()
    assert block["quellen"] and "komm" in block and block["built_at"]


def test_corpus_stats_lexical_when_semantic_off(state):
    mcp_server.STATE["semantic"] = False
    out = mcp_server.corpus_stats()
    assert out["default_backend"] == "lexical"
    assert out["semantic_available"] is False
    assert out["embed_model"] is None


# --------------------------------------------------------------------------
# search_messages – lexical path (FTS5/BM25)
# --------------------------------------------------------------------------
def test_search_lexical_finds_and_dedupes(state):
    res = mcp_server.search_messages("Rechnung", mode="lexical")
    assert res["backend"] == "lexical"
    uids = _uids(res)
    assert set(uids) == {UID_T0, UID_T1, UID_M1}
    assert len(uids) == len(set(uids))  # each message only once
    hit = res["results"][0]
    assert hit["source_label"] in ("Teams", "Mail")
    assert hit["uri"].startswith("o365://")
    assert hit["score"] is not None
    assert "Rechnung" in hit["preview"]


def test_search_lexical_no_hits_and_empty_query(state):
    assert mcp_server.search_messages("xyzzyplugh", mode="lexical")["count"] == 0
    res = mcp_server.search_messages("", mode="lexical")
    assert res["count"] == 0 and res["results"] == []


def test_search_source_filter(state):
    res = mcp_server.search_messages("Rechnung", source="outlook", mode="lexical")
    assert _uids(res) == [UID_M1]
    res = mcp_server.search_messages("Rechnung", source="teams", mode="lexical")
    assert set(_uids(res)) == {UID_T0, UID_T1}


def test_search_person_filter(state):
    # Person filter goes through the ppl column (lowercased names + addresses)
    res = mcp_server.search_messages("Rechnung", person="Carla", mode="lexical")
    assert _uids(res) == [UID_M1]
    res = mcp_server.search_messages("Rechnung", person="carla@example.com",
                                     mode="lexical")
    assert _uids(res) == [UID_M1]
    res = mcp_server.search_messages("Rechnung", person="Niemand", mode="lexical")
    assert res["count"] == 0


def test_search_date_filters(state):
    # The Teams hits are dated June 1, the mail June 10.
    res = mcp_server.search_messages("Rechnung", date_from="2025-06-05",
                                     mode="lexical")
    assert _uids(res) == [UID_M1]
    res = mcp_server.search_messages("Rechnung", date_to="2025-06-05",
                                     mode="lexical")
    assert set(_uids(res)) == {UID_T0, UID_T1}
    # date_to is inclusive (up to 23:59:59 of that day)
    res = mcp_server.search_messages("Rechnung", date_to="2025-06-10",
                                     mode="lexical")
    assert UID_M1 in _uids(res)


def test_search_k_and_offset_page_through_results(state):
    page1 = mcp_server.search_messages("Rechnung", k=2, offset=0, mode="lexical")
    page2 = mcp_server.search_messages("Rechnung", k=2, offset=2, mode="lexical")
    assert page1["count"] == 2 and page2["count"] == 1
    assert page1["offset"] == 0 and page2["offset"] == 2
    assert set(_uids(page1)) | set(_uids(page2)) == {UID_T0, UID_T1, UID_M1}
    assert not set(_uids(page1)) & set(_uids(page2))


def test_search_preview_chars(state):
    res = mcp_server.search_messages("Rechnung", mode="lexical", preview_chars=10)
    assert all(len(h["preview"]) <= 10 for h in res["results"])
    res = mcp_server.search_messages("Rechnung", mode="lexical", preview_chars=0)
    assert all("preview" not in h for h in res["results"])


# --------------------------------------------------------------------------
# search_messages – semantic path and hybrid fusion (RRF)
# --------------------------------------------------------------------------
def test_search_semantic_ranks_by_stubbed_cosine(state, monkeypatch):
    # Query vector: vacation mail most similar, invoice mail in second place.
    # The lower bound is deliberately off here: place 2 sits at exactly 0.45,
    # and what is checked is the ordering, not the filtering.
    monkeypatch.setattr(mcp_server, "SEM_MIN", 0.0)
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.5})
    res = mcp_server.search_messages("freie Tage im Sommer", mode="semantic")
    assert res["backend"] == "semantic"
    uids = _uids(res)
    assert uids[0] == UID_M2 and uids[1] == UID_M1
    scores = [h["score"] for h in res["results"]]
    assert scores == sorted(scores, reverse=True)
    # Cosine matches the (normalized) stub weights: place 2 = half the score
    assert scores[1] == pytest.approx(scores[0] * 0.5, abs=0.01)
    assert scores[0] > 0.8


def test_search_hybrid_fuses_semantic_and_lexical(state, monkeypatch):
    # Semantics: M1 before M2. Lexically "Rechnung 4711" hits M1/T0/T1 but
    # never M2 – M2 can only enter the list via the semantic branch.
    _stub_semantic(monkeypatch, state["chunks"], {UID_M1: 1.0, UID_M2: 0.6})
    res = mcp_server.search_messages("Rechnung 4711", mode="hybrid")
    assert res["backend"] == "hybrid"
    uids = _uids(res)
    assert uids[0] == UID_M1        # first place in both backends → RRF winner
    assert UID_M2 in uids           # semantic-only hit is preserved
    assert UID_T0 in uids           # BM25-only hit is preserved


def test_search_auto_falls_back_to_lexical_when_ollama_down(state):
    # The fixture stub for _embed_query raises – like an unreachable Ollama
    res = mcp_server.search_messages("Rechnung", mode="auto")
    assert res["backend"] == "lexical"
    assert set(_uids(res)) == {UID_T0, UID_T1, UID_M1}
    assert "nicht gestubbt" in mcp_server.STATE["last_semantic_error"]


def test_search_semantic_mode_reports_error_when_ollama_down(state):
    res = mcp_server.search_messages("Rechnung", mode="semantic")
    assert set(res) == {"error"}
    assert "Semantic ranking failed" in res["error"]


def test_search_lexical_mode_never_touches_embeddings(state):
    # mode="lexical" must not call _embed_query in the first place
    res = mcp_server.search_messages("Urlaub", mode="lexical")
    assert res["backend"] == "lexical"
    assert _uids(res) == [UID_M2]
    assert "last_semantic_error" not in mcp_server.STATE


# --------------------------------------------------------------------------
# browse_messages
# --------------------------------------------------------------------------
def test_browse_newest_first_nulls_last(state):
    res = mcp_server.browse_messages(k=50)
    assert _uids(res) == BROWSE_ORDER  # ts descending, contact without ts last
    assert res["count"] == len(BROWSE_ORDER)
    assert res["results"][0]["score"] is None  # browse has no relevance score


def test_browse_pagination(state):
    page1 = mcp_server.browse_messages(k=4, offset=0)
    page2 = mcp_server.browse_messages(k=4, offset=4)
    page3 = mcp_server.browse_messages(k=4, offset=8)
    assert _uids(page1) == BROWSE_ORDER[:4]
    assert _uids(page2) == BROWSE_ORDER[4:8]
    assert _uids(page3) == BROWSE_ORDER[8:]
    assert mcp_server.browse_messages(k=4, offset=100)["count"] == 0


def test_browse_filters(state):
    assert _uids(mcp_server.browse_messages(source="teams")) == \
        [UID_TX, UID_T2, UID_T1, UID_T0]
    assert _uids(mcp_server.browse_messages(source="kontakte")) == [UID_CON]
    res = mcp_server.browse_messages(person="bob", source="teams")
    assert _uids(res) == [UID_T1]
    res = mcp_server.browse_messages(date_from="2025-06-10", date_to="2025-06-30")
    assert _uids(res) == [UID_CAL, UID_M1]


def test_browse_preview_toggle(state):
    res = mcp_server.browse_messages(source="kalender")
    assert res["results"][0]["preview"].startswith("Ort: Raum 42.")
    res = mcp_server.browse_messages(source="kalender", preview_chars=0)
    assert "preview" not in res["results"][0]


# --------------------------------------------------------------------------
# get_document
# --------------------------------------------------------------------------
def test_get_document_rejoins_chunks_to_full_text(state):
    assert sum(c["uid"] == UID_M3 for c in state["chunks"]) > 1
    out = mcp_server.get_document(UID_M3)
    assert out["text"] == LONG_TEXT  # overlaps removed exactly
    assert out["title"] == "Protokoll Quartalsplanung"
    assert out["source"] == "outlook" and out["source_label"] == "Mail"
    assert out["uri"] == "o365://outlook/" + quote("sent/protokoll.eml", safe="")
    assert "context_before" not in out  # no context without context parameters


def test_get_document_unknown_uid(state):
    out = mcp_server.get_document("outlook:gibtsnicht.eml:0")
    assert "error" in out and "gibtsnicht" in out["error"]


def test_get_document_conversation_context(state):
    out = mcp_server.get_document(UID_T1, context_before=1, context_after=1)
    assert [e["uid"] for e in out["context_before"]] == [UID_T0]
    assert [e["uid"] for e in out["context_after"]] == [UID_T2]
    assert out["context_before"][0]["who"] == "Alice Beispiel"
    assert "Rechnung 4711" in out["context_before"][0]["text"]
    # Context comes only from the same file – the other Teams file is absent
    out = mcp_server.get_document(UID_T0, context_before=5, context_after=5)
    ctx_uids = {e["uid"] for e in out["context_before"] + out["context_after"]}
    assert ctx_uids == {UID_T1, UID_T2}


# --------------------------------------------------------------------------
# list_people
# --------------------------------------------------------------------------
def test_list_people_counts_and_excludes_unknown(state):
    out = mcp_server.list_people()
    people = {p["name"]: p["messages"] for p in out["people"]}
    assert people == {"Alice Beispiel": 4, "Bob Baumeister": 1,
                      "Carla Chef": 1, "Doris Docs": 1}
    assert out["people"][0]["name"] == "Alice Beispiel"  # most frequent first
    assert out["total_distinct"] == 4
    assert "(unbekannt)" not in people and "" not in people


def test_list_people_source_contains_and_limit(state):
    out = mcp_server.list_people(source="teams")
    assert {p["name"]: p["messages"] for p in out["people"]} == \
        {"Alice Beispiel": 2, "Bob Baumeister": 1}
    # contains matches name OR ppl tokens (including e-mail addresses)
    out = mcp_server.list_people(contains="carla")
    assert [p["name"] for p in out["people"]] == ["Carla Chef"]
    out = mcp_server.list_people(contains="doris@example.com")
    assert [p["name"] for p in out["people"]] == ["Doris Docs"]
    out = mcp_server.list_people(limit=1)
    assert out["count"] == 1 and out["total_distinct"] == 4


# --------------------------------------------------------------------------
# read_source_file – incl. path-traversal protection (security-relevant!)
# --------------------------------------------------------------------------
def test_read_source_file_reads_export_file(state):
    out = mcp_server.read_source_file("teams", "1on1/alice__chat.html")
    assert out["content"] == TEAMS_FILE_CONTENT
    assert out["suffix"] == ".html"
    assert out["total_bytes"] == len(TEAMS_FILE_CONTENT.encode())
    assert out["offset"] == 0 and out["truncated"] is False
    out = mcp_server.read_source_file("outlook", "inbox/mail1.eml")
    assert "Rechnung 4711" in out["content"]


def test_read_source_file_windows_with_offset(state):
    # Small windows + offset must reconstruct the file without gaps
    total = len(TEAMS_FILE_CONTENT.encode())
    parts, offset = [], 0
    while True:
        out = mcp_server.read_source_file("teams", "1on1/alice__chat.html",
                                          max_chars=10, offset=offset)
        parts.append(out["content"])
        offset += 10
        if not out["truncated"]:
            break
    assert "".join(parts) == TEAMS_FILE_CONTENT
    assert len(parts) == -(-total // 10)


def test_read_source_file_rejects_path_traversal(state):
    # The secret file sits directly above the export folders
    for evil in ("../geheim.txt", "../../geheim.txt", "1on1/../../geheim.txt"):
        out = mcp_server.read_source_file("teams", evil)
        assert out == {"error": "Path outside the export directory."}
    out = mcp_server.read_source_file("outlook", "../geheim.txt")
    assert "error" in out and "GEHEIM" not in str(out)


def test_read_source_file_rejects_absolute_paths(state):
    secret = state["tmp"] / "geheim.txt"
    out = mcp_server.read_source_file("teams", str(secret))
    assert out == {"error": "Path outside the export directory."}
    out = mcp_server.read_source_file("outlook", "/etc/passwd")
    assert out == {"error": "Path outside the export directory."}


def test_read_source_file_rejects_symlink_escape(state):
    # Symlink INSIDE the export, target outside → must be rejected
    link = state["outlook_dir"] / "inbox" / "link.eml"
    link.symlink_to(state["tmp"] / "geheim.txt")
    out = mcp_server.read_source_file("outlook", "inbox/link.eml")
    assert out == {"error": "Path outside the export directory."}


def test_read_source_file_invalid_root_and_missing_file(state):
    out = mcp_server.read_source_file("kalender", "termin.ics")
    assert out == {"error": "source_root must be 'teams', 'outlook', 'onedrive', "
           "'sharepoint', 'pages' or 'planner'."}
    out = mcp_server.read_source_file("teams", "1on1/fehlt.html")
    assert out == {"error": "File not found: 1on1/fehlt.html"}
    out = mcp_server.read_source_file("teams", "")  # a directory, not a file
    assert "error" in out


# --------------------------------------------------------------------------
# MCP resource o365://{root}/{path}
# --------------------------------------------------------------------------
def test_source_resource_returns_file_by_encoded_uri(state):
    content = mcp_server.source_resource(
        "teams", quote("1on1/alice__chat.html", safe=""))
    assert content == TEAMS_FILE_CONTENT


def test_source_resource_rejects_traversal(state):
    with pytest.raises(ValueError, match="outside the export directory"):
        mcp_server.source_resource("teams", quote("../geheim.txt", safe=""))
    with pytest.raises(ValueError):
        mcp_server.source_resource("wurzel", "x")


# --------------------------------------------------------------------------
# Uninitialized STATE
# --------------------------------------------------------------------------
def test_tools_without_initialized_state(empty_state):
    # read_source_file fails in a controlled way (no export directory known) …
    out = mcp_server.read_source_file("teams", "x.html")
    assert out == {"error": "source_root must be 'teams', 'outlook', 'onedrive', "
           "'sharepoint', 'pages' or 'planner'."}
    # … the DB-backed tools raise a KeyError for lack of STATE["db"]
    # (current behavior – pinned down here)
    with pytest.raises(KeyError):
        mcp_server.corpus_stats()
    with pytest.raises(KeyError):
        mcp_server.search_messages("test", mode="lexical")
    with pytest.raises(KeyError):
        mcp_server.browse_messages()


def test_list_people_contains_ist_umlaut_unabhaengig(tmp_path):
    """SQLite LIKE is only ASCII-case-insensitive – py_lower() lets even
    upper-case umlaut input ("MÜLLER") find the name."""
    store = tmp_path / "store_umlaut"
    store.mkdir()
    recs = [_rec("teams:x.html:0", "teams", "teams", "x.html", "Jörg Müller",
                 "jörg müller joerg@example.com", _ts("2025-06-01 10:00"),
                 "2025-06-01 10:00", "Chat", "1:1-Chat", "Servus!")]
    chunks = corpus.chunk_records(recs)
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    rag_index.write_db(store, chunks)

    old = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    mcp_server.STATE["db"] = str(store / "corpus.db")
    try:
        out = mcp_server.list_people(contains="MÜLLER")
        assert [p["name"] for p in out["people"]] == ["Jörg Müller"]
        out = mcp_server.list_people(contains="JOERG@EXAMPLE.COM")  # ppl-Token
        assert [p["name"] for p in out["people"]] == ["Jörg Müller"]
        out = mcp_server.list_people(contains="gibtsnicht")
        assert out["people"] == []
    finally:
        mcp_server.STATE.clear()
        mcp_server.STATE.update(old)


# --------------------------------------------------------------------------
# Fast path in _semantic_rank (unfiltered)
# --------------------------------------------------------------------------
def test_semantic_schnellpfad_ist_deckungsgleich(state, monkeypatch):
    """Without filters the matrix is read in one piece instead of via an id list.

    Both paths must deliver the same hits with the same scores. The SQL path
    is forced through an equivalent but not literally identical WHERE
    ("1=1 AND 1=1") that matches the same rows.
    """
    chunks = state["chunks"]
    _stub_semantic(monkeypatch, chunks, {c["uid"]: 1.0 / (i + 1)
                                         for i, c in enumerate(chunks)})
    con = mcp_server._db()
    try:
        schnell = mcp_server._semantic_rank(con, "q", mcp_server._WHERE_ALL, [], 10)
        ueber_sql = mcp_server._semantic_rank(con, "q", "1=1 AND 1=1", [], 10)
    finally:
        con.close()
    assert schnell, "Schnellpfad liefert nichts"
    assert dict(schnell) == dict(ueber_sql)      # same ids, same scores


def test_semantic_schnellpfad_bei_leerer_matrix(state, monkeypatch):
    monkeypatch.setitem(mcp_server.STATE, "V", np.zeros((0, DIM), dtype="float16"))
    con = mcp_server._db()
    try:
        assert mcp_server._semantic_rank(con, "q", mcp_server._WHERE_ALL, [], 5) == []
    finally:
        con.close()


# --------------------------------------------------------------------------
# Transport hardening (DNS rebinding) for the HTTP transport
# --------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_ueberlaesst_die_pruefung_dem_sdk(host):
    # None = SDK default handling; it covers exactly the loopback addresses
    assert mcp_server._transport_security(host, 8365, []) is None


def test_nicht_loopback_ohne_allowed_host_startet_nicht():
    with pytest.raises(SystemExit, match="--allowed-host"):
        mcp_server._transport_security("0.0.0.0", 8365, [])


def test_nicht_loopback_mit_allowed_host_erzwingt_pruefung():
    s = mcp_server._transport_security("0.0.0.0", 8365,
                                       ["nas.local", "192.168.1.5:9000"])
    assert s.enable_dns_rebinding_protection is True
    assert s.allowed_hosts == ["nas.local:8365", "192.168.1.5:9000"]
    assert "http://nas.local:8365" in s.allowed_origins
    assert "https://192.168.1.5:9000" in s.allowed_origins


def test_with_port_verwechselt_ipv6_nicht_mit_port():
    assert mcp_server._with_port("host", 8365) == "host:8365"
    assert mcp_server._with_port("host:9000", 8365) == "host:9000"
    # IPv6 literal without port: the part after the last ":" is not a number
    assert mcp_server._with_port("[fe80::1]", 8365) == "[fe80::1]:8365"
    assert mcp_server._with_port("[fe80::1]:9000", 8365) == "[fe80::1]:9000"


# --------------------------------------------------------------------------
# MCP protocol level
#
# All tests above call the tool functions directly – they would stay green
# even if registration with the SDK stopped working entirely. The following
# tests therefore speak real MCP: Client(mcp) connects in-process directly
# to the server object – no subprocess, no HTTP, but the same path over the
# wire that Claude takes.
# --------------------------------------------------------------------------
TOOL_NAMES = {"search_messages", "browse_messages", "get_document",
              "get_thread", "list_people", "list_folders", "list_filetypes",
              "list_files", "read_source_file", "corpus_stats",
              "archive_analytics"}


def _via_client(fn):
    """Run fn(client) against the in-memory client."""
    async def run():
        async with Client(mcp_server.mcp) as c:
            return await fn(c)
    return anyio.run(run)


def _payload(res):
    """Extract a tool's return value from the CallToolResult.

    The tools are annotated with "-> dict" (no value type), so the SDK
    generates no output_schema and no structured_content: the dict arrives
    as JSON text in the content.
    """
    assert res.is_error is False
    assert len(res.content) == 1
    return json.loads(res.content[0].text)


def test_server_metadaten_werden_ausgeliefert():
    """Name/version/instructions go to the client with the initialization."""
    async def run():
        async with Client(mcp_server.mcp) as c:
            return c.server_info, c.instructions, c.protocol_version

    info, instr, proto = anyio.run(run)
    assert info.name == "munimentum"
    assert info.version                          # not empty
    assert proto == LATEST_PROTOCOL_VERSION      # latest revision, not the 2025 one
    # The instructions are meant to help with tool selection – the expensive
    # fallback read_source_file must be named as such.
    assert instr and "search_messages" in instr
    assert "read_source_file" in instr


def test_alle_tools_sind_beim_sdk_registriert():
    tools = _via_client(lambda c: c.list_tools()).tools
    assert {t.name for t in tools} == TOOL_NAMES
    for t in tools:
        # Without a docstring Claude gets no description to look at
        assert t.description, f"{t.name} hat keine Beschreibung"
        assert t.annotations is not None, f"{t.name} hat keine Annotations"
        assert t.annotations.read_only_hint is True
        assert t.annotations.idempotent_hint is True
        assert t.annotations.open_world_hint is False


def test_tool_schema_enthaelt_alle_parameter():
    tools = _via_client(lambda c: c.list_tools()).tools
    schema = next(t for t in tools if t.name == "search_messages").input_schema
    assert set(schema["properties"]) == {
        "query", "person", "date_from", "date_to", "days", "source", "k",
        "offset", "mode", "preview_chars", "only_gone", "folder", "filetype"}
    assert schema["required"] == ["query"]      # only query is required


def test_resource_template_ist_registriert():
    tpl = _via_client(lambda c: c.list_resource_templates()).resource_templates
    assert [t.uri_template for t in tpl] == ["o365://{root}/{path}"]


def test_call_tool_ueber_sdk_liefert_ergebnis(state):
    res = _via_client(lambda c: c.call_tool(
        "search_messages", {"query": "Rechnung", "mode": "lexical"}))
    payload = _payload(res)
    assert payload["backend"] == "lexical"
    assert UID_M1 in [h["uid"] for h in payload["results"]]


def test_read_resource_ueber_sdk_liefert_quelldatei(state):
    uri = "o365://teams/" + quote("1on1/alice__chat.html", safe="")
    res = _via_client(lambda c: c.read_resource(uri))
    assert [c.text for c in res.contents] == [TEAMS_FILE_CONTENT]


def test_call_tool_meldet_fehler_statt_ihn_zu_verschlucken(state):
    """read_source_file returns an error field on traversal (no crash)."""
    res = _via_client(lambda c: c.call_tool(
        "read_source_file", {"source_root": "teams", "path": "../geheim.txt"}))
    assert "outside the export directory" in _payload(res)["error"]


def test_resource_traversal_wird_vom_sdk_abgewiesen(state):
    """mcp 2.x rejects traversal in resource URIs before the handler runs.

    The second line of defense remains _resolve_source – see
    test_source_resource_rejects_traversal, which calls the function directly.
    """
    uri = "o365://teams/" + quote("../geheim.txt", safe="")

    # The error must be caught inside the client context: if it escapes
    # anyio.run(), the TaskGroup wraps it in an ExceptionGroup.
    async def run():
        async with Client(mcp_server.mcp) as c:
            with pytest.raises(MCPError, match="Unknown resource"):
                await c.read_resource(uri)

    anyio.run(run)


# --------------------------------------------------------------------------
# get_thread – a single hit often says too little
# --------------------------------------------------------------------------
def test_get_thread_liefert_das_gespraech_chronologisch(state):
    con = sqlite3.connect(state["store"] / "corpus.db")
    con.execute("UPDATE chunks SET thread = 'tix:abc' WHERE seq = 0")
    con.commit()
    con.close()

    r = mcp_server.get_thread(thread="tix:abc")
    assert r["count"] >= 2
    zeiten = [m["date"] for m in r["messages"]]
    datiert = [z for z in zeiten if z]
    assert datiert == sorted(datiert), "Verlauf ist nicht chronologisch"
    # Undated entries go last: slotting them between two days would be made up.
    assert zeiten[:len(datiert)] == datiert, "Undatiertes steht mittendrin"
    assert all("uid" in m for m in r["messages"])


def test_get_thread_ohne_schluessel(state):
    assert mcp_server.get_thread(thread="")["count"] == 0


def test_get_thread_unbekannt(state):
    assert mcp_server.get_thread(thread="tix:gibtesnicht")["messages"] == []


def test_treffer_tragen_ihre_gespraechskennung(state):
    con = sqlite3.connect(state["store"] / "corpus.db")
    con.execute("UPDATE chunks SET thread = 'tix:xyz'")
    con.commit()
    con.close()
    treffer = mcp_server.browse_messages(k=1)["results"]
    assert treffer and treffer[0]["thread"] == "tix:xyz", \
        "ohne Kennung am Treffer liesse sich der Verlauf nicht nachladen"


def test_only_gone_zeigt_nur_verschwundenes(state):
    con = sqlite3.connect(state["store"] / "corpus.db")
    con.execute("UPDATE chunks SET gone = '2026-03-12T09:00:00' "
                "WHERE uid = (SELECT uid FROM chunks WHERE seq = 0 LIMIT 1)")
    con.commit()
    con.close()

    alle = mcp_server.browse_messages(k=50)["count"]
    nur = mcp_server.browse_messages(k=50, only_gone=True)
    assert 0 < nur["count"] < alle
    assert all(m["gone"] for m in nur["results"])
    # And the normal case still shows everything, deleted items included.
    assert mcp_server.browse_messages(k=50)["count"] == alle


def test_treffer_sagen_ob_die_mail_noch_da_ist(state):
    treffer = mcp_server.browse_messages(k=1)["results"][0]
    assert "gone" in treffer and treffer["gone"] is None


# --------------------------------------------------------------------------
# The folder as a search criterion
# --------------------------------------------------------------------------
def _setze_ordner(store, zuordnung):
    con = sqlite3.connect(store / "corpus.db")
    for uid, ctx in zuordnung.items():
        con.execute("UPDATE chunks SET ctx = ?, src = 'outlook' WHERE uid = ?",
                    (ctx, uid))
    con.commit()
    con.close()


def test_ordnerfilter_nimmt_auch_die_unterordner(state):
    uids = [r[0] for r in sqlite3.connect(
        state["store"] / "corpus.db").execute(
        "SELECT DISTINCT uid FROM chunks ORDER BY uid")]
    assert len(uids) >= 3
    _setze_ordner(state["store"], {
        uids[0]: "E-Mail/Kunden",
        uids[1]: "E-Mail/Kunden/Contoso",
        uids[2]: "E-Mail/Posteingang"})

    r = mcp_server.browse_messages(k=50, folder="E-Mail/Kunden")
    ordner = {m["context"] for m in r["results"]}
    assert ordner == {"E-Mail/Kunden", "E-Mail/Kunden/Contoso"}, (
        "Wer einen Ordner wählt, will nicht 288 Häkchen setzen")


def test_ordnerfilter_trifft_keinen_namensvetter(state):
    uids = [r[0] for r in sqlite3.connect(
        state["store"] / "corpus.db").execute(
        "SELECT DISTINCT uid FROM chunks ORDER BY uid")]
    _setze_ordner(state["store"], {uids[0]: "E-Mail/Kunden",
                                   uids[1]: "E-Mail/KundenAlt"})
    r = mcp_server.browse_messages(k=50, folder="E-Mail/Kunden")
    assert {m["context"] for m in r["results"]} == {"E-Mail/Kunden"}


def test_ohne_ordner_bleibt_alles(state):
    alle = mcp_server.browse_messages(k=50)["count"]
    assert mcp_server.browse_messages(k=50, folder="")["count"] == alle


def test_list_folders_nennt_was_da_ist(state):
    uids = [r[0] for r in sqlite3.connect(
        state["store"] / "corpus.db").execute(
        "SELECT DISTINCT uid FROM chunks ORDER BY uid")]
    _setze_ordner(state["store"], {uids[0]: "E-Mail/Kunden",
                                   uids[1]: "E-Mail/Kunden"})
    r = mcp_server.list_folders()
    pfade = {f["path"]: f["messages"] for f in r["folders"]}
    assert pfade.get("E-Mail/Kunden") == 2


# --------------------------------------------------------------------------
# days: "the last seven days", without the caller having to do the math
#
# Computing the date yourself is the most common opportunity to slip up –
# especially across a month boundary. Hence calendar days and a pinned
# "today" in the tests: otherwise the outcome would depend on the calendar
# of the day they run on.
# --------------------------------------------------------------------------
@pytest.fixture
def heute(monkeypatch):
    """A fixed "today" – June 10, 2025, right in the middle of the test data."""
    class Fix(date):
        @classmethod
        def today(cls):
            return date(2025, 6, 10)
    monkeypatch.setattr(mcp_server, "date", Fix)
    return date(2025, 6, 10)


def test_seit_tagen_zaehlt_heute_mit(heute):
    """7 means today and the six days before it – not eight, not six."""
    assert mcp_server._seit_tagen(7) == datetime(2025, 6, 4).timestamp()
    assert mcp_server._seit_tagen(1) == datetime(2025, 6, 10).timestamp()
    # Across the month boundary: exactly the case people miscalculate by hand.
    assert mcp_server._seit_tagen(30) == datetime(2025, 5, 12).timestamp()


@pytest.mark.parametrize("wert", [0, -3, None, "sieben"])
def test_seit_tagen_ohne_brauchbare_zahl(wert, heute):
    assert mcp_server._seit_tagen(wert) is None


def test_zeitraum_genanntes_datum_schlaegt_die_abkuerzung(heute):
    von, bis = mcp_server._zeitraum("2025-01-01", "", 7)
    assert von == datetime(2025, 1, 1).timestamp()
    assert bis is None, "days hat die obere Grenze gesetzt, obwohl von genannt war"


def test_zeitraum_days_begrenzt_beide_enden(heute):
    """Otherwise "the last seven days" would also pull the next months'
    calendar appointments – they too lie after the start date."""
    von, bis = mcp_server._zeitraum("", "", 7)
    assert von == datetime(2025, 6, 4).timestamp()
    assert bis == datetime(2025, 6, 10, 23, 59, 59).timestamp()


def test_zeitraum_genanntes_ende_bleibt_stehen(heute):
    von, bis = mcp_server._zeitraum("", "2025-06-30", 7)
    assert von == datetime(2025, 6, 4).timestamp()
    assert bis == datetime(2025, 6, 30, 23, 59, 59).timestamp()


def test_zeitraum_ohne_alles(heute):
    assert mcp_server._zeitraum("", "", 0) == (None, None)


def test_browse_letzte_tage(state, heute):
    """Against the store: days=7 yields exactly the June 4-10 window."""
    # June 10 08:00 is inside, June 2 and June 15 are not.
    assert _uids(mcp_server.browse_messages(days=7)) == [UID_M1]
    # 9 days reach back to June 2.
    assert _uids(mcp_server.browse_messages(days=9)) == [UID_M1, UID_TX]
    assert mcp_server.browse_messages(days=1)["count"] == 1      # today only
    # Without the upper bound the June 15 appointment and the July 1 mail
    # would be included – both lie in the future.
    assert UID_CAL not in _uids(mcp_server.browse_messages(days=7))
    assert UID_M2 not in _uids(mcp_server.browse_messages(days=7))


def test_browse_days_mit_ordner_und_quelle(state, heute):
    """The case it is meant for: a folder plus a time range."""
    res = mcp_server.browse_messages(days=9, source="outlook", folder="inbox")
    assert _uids(res) == [UID_M1]


def test_search_letzte_tage(state, heute):
    res = mcp_server.search_messages("Rechnung", days=7, mode="lexical")
    assert _uids(res) == [UID_M1]         # the June 1 Teams hits are out
    assert len(mcp_server.search_messages("Rechnung", mode="lexical")["results"]) > 1


def test_days_ohne_wirkung_wenn_date_from_genannt(state, heute):
    """An explicit date wins – even when it reaches much further back."""
    res = mcp_server.browse_messages(days=1, date_from="2025-05-01", source="teams")
    assert _uids(res) == [UID_TX, UID_T2, UID_T1, UID_T0]


def test_list_folders_kennt_beide_quellen(state, tmp_path):
    """The search's folder filter must also offer mirrored OneDrive
    folders – they are in the index as ctx, just like mailbox folders."""
    con = sqlite3.connect(mcp_server.STATE["db"])
    con.execute("INSERT INTO chunks (uid, seq, msg_idx, src, root, rel, ctx, text) "
                "VALUES ('datei:Dateien/Kunden/a.pdf:0', 0, 0, 'datei', 'onedrive', "
                "'Dateien/Kunden/a.pdf', 'Dateien/Kunden', 'Dateien / Kunden / a.pdf')")
    con.commit()
    con.close()
    pfade = {f["path"] for f in mcp_server.list_folders(limit=100)["folders"]}
    assert "Dateien/Kunden" in pfade, "OneDrive-Ordner fehlt im Filter"
    assert any(p.startswith("inbox") or "/" in p for p in pfade), "Postfach fehlt jetzt"


def test_list_folders_kennt_auch_kalender(state):
    """The calendar selection from the settings must show up in the search."""
    pfade = {f["path"] for f in mcp_server.list_folders(limit=100)["folders"]}
    assert "kalender/Arbeit" in pfade
    treffer = mcp_server.search_messages("Quartalsplanung", folder="kalender/Arbeit",
                                         mode="lexical")
    assert _uids(treffer) == [UID_CAL]
    assert mcp_server.search_messages("Quartalsplanung", folder="kalender/Privat",
                                      mode="lexical")["count"] == 0


def test_person_mit_stern(state):
    """The asterisk is the wildcard – it must not be searched for literally."""
    ohne = _uids(mcp_server.browse_messages(person="alice"))
    assert ohne, "Teilstringsuche findet nichts mehr"
    assert _uids(mcp_server.browse_messages(person="alice*")) == ohne
    assert _uids(mcp_server.browse_messages(person="ali*spiel")) == ohne
    assert mcp_server.browse_messages(person="zzz*")["count"] == 0
    # And what is a wildcard to SQL is none here: "a_ice" must not match
    # "alice", even though _ stands for any single character in LIKE.
    assert mcp_server.browse_messages(person="a_ice")["count"] == 0
    assert mcp_server.browse_messages(person="%")["count"] == 0


def test_list_people_zaehlt_auch_die_summe(state):
    """The "all with …" line counts messages, just like the lines above it."""
    r = mcp_server.list_people(contains="beispiel")
    assert r["total_messages"] == sum(p["messages"] for p in r["people"])
    assert r["total_distinct"] == len(r["people"])
    # The asterisk works in the suggestions as well.
    assert mcp_server.list_people(contains="bei*iel")["people"] == r["people"]
    assert mcp_server.list_people(contains="zzz")["total_messages"] == 0


def test_unmoegliches_datum_ist_ein_fehler(state):
    """Otherwise the search would run without this bound and present that
    as the answer."""
    with pytest.raises(ValueError):
        mcp_server.browse_messages(date_to="2021-06-31")
    assert mcp_server.browse_messages(date_to="2021-06-30")["count"] >= 0


def test_list_folders_je_quelle(state):
    """Whoever picks a source up front should only see its folders below."""
    con = sqlite3.connect(mcp_server.STATE["db"])
    con.execute("INSERT INTO chunks (uid, seq, msg_idx, src, root, rel, ctx, text) "
                "VALUES ('datei:Dateien/Kunden/a.pdf:0', 0, 0, 'datei', 'onedrive', "
                "'Dateien/Kunden/a.pdf', 'Dateien/Kunden', 'a')")
    con.commit()
    con.close()

    def pfade(**kw):
        return {f["path"] for f in mcp_server.list_folders(limit=100, **kw)["folders"]}

    assert pfade(source="kalender") == {"kalender/Arbeit"}
    assert pfade(source="datei") == {"Dateien/Kunden"}
    assert pfade(source="kontakte") == {"kontakte/Team"}
    assert "Dateien/Kunden" not in pfade(source="outlook")
    # Empty and "all" are the same: everything that can be enumerated.
    assert pfade(source="all") == pfade() >= {"kalender/Arbeit", "Dateien/Kunden"}
    # Unknown source: better nothing than accidentally everything.
    assert pfade(source="gibtsnicht") == set()


def test_list_folders_fasst_kanaele_zusammen(state):
    """A team easily has twenty channels - the choice is between the kinds."""
    con = sqlite3.connect(mcp_server.STATE["db"])
    for i, ctx in enumerate(["channels/Team A", "channels/Team B", "1on1"]):
        con.execute("INSERT INTO chunks (uid, seq, msg_idx, src, root, rel, ctx, text) "
                    f"VALUES ('teams:x{i}.html:0', 0, 0, 'teams', 'teams', "
                    f"'x{i}.html', ?, 'a')", (ctx,))
    con.commit()
    con.close()
    ordner = {f["path"]: f["messages"]
              for f in mcp_server.list_folders(source="teams", limit=100)["folders"]}
    assert set(ordner) == {"channels", "1on1"}
    assert ordner["channels"] == 2          # both teams under one entry
    # And the filter copes without extra work: a path means everything below it.
    assert mcp_server.browse_messages(folder="channels")["count"] == 2
    assert mcp_server.browse_messages(folder="channels/Team A")["count"] == 1


def test_dateityp_filtert_in_allen_sucharten(state):
    """The filter lives in SQL, not in the full text - otherwise it would be
    silently ineffective in the semantic search and the AI answer."""
    con = sqlite3.connect(mcp_server.STATE["db"])
    con.execute("UPDATE chunks SET att = 'Vertrag.pdf Anlage.xlsx', "
                "ext = 'pdf xlsx' WHERE uid = ?", (UID_M1,))
    con.execute("UPDATE chunks SET att = 'Notiz.doc', ext = 'doc' WHERE uid = ?",
                (UID_M2,))
    con.commit()
    con.close()

    assert _uids(mcp_server.browse_messages(filetype="pdf")) == [UID_M1]
    assert _uids(mcp_server.browse_messages(filetype="xlsx")) == [UID_M1]
    # "doc" must not match "docx" and vice versa.
    assert _uids(mcp_server.browse_messages(filetype="doc")) == [UID_M2]
    assert mcp_server.browse_messages(filetype="docx")["count"] == 0
    # A leading dot is an obvious input, not a different question.
    assert _uids(mcp_server.browse_messages(filetype=".PDF")) == [UID_M1]
    # And in the search, combined with the search term.
    assert _uids(mcp_server.search_messages("Rechnung", mode="lexical",
                                            filetype="pdf")) == [UID_M1]
    assert mcp_server.search_messages("Rechnung", mode="lexical",
                                      filetype="xyz")["count"] == 0


def test_list_filetypes_zaehlt_je_typ(state):
    con = sqlite3.connect(mcp_server.STATE["db"])
    con.execute("UPDATE chunks SET ext = 'pdf xlsx' WHERE uid = ?", (UID_M1,))
    con.execute("UPDATE chunks SET ext = 'pdf' WHERE uid = ?", (UID_M2,))
    con.commit()
    con.close()
    r = mcp_server.list_filetypes()
    zahl = {e["type"]: e["messages"] for e in r["filetypes"]}
    # A message with two attachments counts once for each type.
    assert zahl == {"pdf": 2, "xlsx": 1}
    assert [e["type"] for e in r["filetypes"]] == ["pdf", "xlsx"]   # most frequent first
    assert mcp_server.list_filetypes(limit=1)["filetypes"] == [{"type": "pdf", "messages": 2}]
    assert mcp_server.list_filetypes(limit=1)["total_distinct"] == 2
    assert mcp_server.list_filetypes(source="teams")["filetypes"] == []


def test_vorschau_zeigt_die_fundstelle(state):
    """Field report: a search returned mails in which the word only appears
    far into the text. A preview of the first 200 characters shows none of
    it – the hit looks like a miss even though it is spot on."""
    lang = "Vorspann ohne Bezug. " * 20 + "Hier steht Betriebsrat mittendrin."
    assert "Betriebsrat" not in lang[:200]
    v = mcp_server._ausschnitt(lang, ["betriebsrat"], 200)
    assert "Betriebsrat" in v and v.startswith("…") and len(v) <= 200


def test_vorschau_ohne_fundstelle_bleibt_der_anfang():
    """When browsing there is no search term – then the beginning is the
    best information there is."""
    text = "Erster Satz. Zweiter Satz."
    assert mcp_server._ausschnitt(text, [], 12) == text[:12]
    assert mcp_server._ausschnitt(text, ["kommtnichtvor"], 12) == text[:12]


def test_vorschau_haelt_die_zugesagte_laenge(state):
    for n in (10, 40, 200):
        res = mcp_server.search_messages("Rechnung", mode="lexical", preview_chars=n)
        assert all(len(h["preview"]) <= n for h in res["results"])


# --------------------------------------------------------------------------
# Lower bound of the semantic search
#
# Without it, it ALWAYS returns the best k within the filter – even when
# nothing matches. Reported from the field: one day narrowed down, one word
# searched, 18 hits received; two of them contained the word, the remaining
# sixteen were simply everything that arrived that day.
# --------------------------------------------------------------------------
def test_schwache_treffer_fallen_raus(state, monkeypatch):
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.2})
    monkeypatch.setattr(mcp_server, "SEM_MIN", 0.45)
    uids = _uids(mcp_server.search_messages("freie Tage im Sommer", mode="semantic"))
    assert UID_M2 in uids, "der starke Treffer fehlt"
    assert UID_M1 not in uids, "der schwache Treffer ist geblieben"


def test_ohne_grenze_kaeme_alles_zurueck(state, monkeypatch):
    """The cross-check in the test itself: with a bound of 0 the weak hit
    stays in."""
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.2})
    monkeypatch.setattr(mcp_server, "SEM_MIN", 0.0)
    assert UID_M1 in _uids(mcp_server.search_messages("irgendwas", mode="semantic"))


def test_grenze_gilt_auch_mit_filter(state, monkeypatch):
    """The reported case went through the filtered branch – it has code of
    its own and would otherwise not apply the bound."""
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.2})
    monkeypatch.setattr(mcp_server, "SEM_MIN", 0.45)
    uids = _uids(mcp_server.search_messages("egal", mode="semantic", source="outlook"))
    assert UID_M2 in uids and UID_M1 not in uids


def test_nichts_passt_nichts_kommt(state, monkeypatch):
    """If nothing clears the bound, nothing comes back – not the least
    unfitting message. (The bound here exceeds any possible cosine;
    _stub_semantic normalizes the weights, so "everything weak" could not
    be expressed otherwise.)"""
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.9})
    monkeypatch.setattr(mcp_server, "SEM_MIN", 1.5)
    assert mcp_server.search_messages("xylophon quastenflosser", mode="semantic")["count"] == 0


def test_untergrenze_versteht_prozent_und_kosinus(monkeypatch):
    """The UI shows a percentage, the code a cosine. Whoever writes it into
    the file by hand does it one way or the other."""
    for roh, erwartet in (("45", 0.45), ("0.45", 0.45), ("60", 0.60),
                          ("0", 0.0), ("unsinn", 0.45), ("500", 0.99)):
        monkeypatch.setenv("SEMANTIC_MIN", roh)
        assert mcp_server._sem_min() == pytest.approx(erwartet, abs=0.001), roh


# --------------------------------------------------------------------------
# Similar messages to a hit
#
# The difference to the semantic search is the starting vector: here it is
# already in the index. That is why this needs no Ollama – and exactly that
# keeps the entry in the hit menu usable even when the variant above is
# locked.
# --------------------------------------------------------------------------
@pytest.fixture
def aehnliche_vektoren(state):
    """The test corpus uses orthogonal unit vectors – nothing resembles
    anything there, and the lower bound consequently discards everything.
    So for this test: a matrix whose rows really are close to each other."""
    V = mcp_server.STATE["V"]
    n, dim = V.shape
    nah = np.zeros((n, dim), dtype="float32")
    for i in range(n):
        nah[i, 0] = 1.0                       # shared main direction
        nah[i, 1 + (i % (dim - 1))] = 0.35    # a bit of individuality per row
        nah[i] /= np.linalg.norm(nah[i])
    alt = mcp_server.STATE["V"]
    mcp_server.STATE["V"] = nah
    yield
    mcp_server.STATE["V"] = alt


def test_aehnliche_brauchen_kein_ollama(aehnliche_vektoren, monkeypatch):
    def kein_netz(text):
        raise AssertionError("similar_messages hat eingebettet – das soll es nicht")
    monkeypatch.setattr(mcp_server, "_embed_query", kein_netz)

    erste = mcp_server.browse_messages(k=1)["results"][0]
    cid = erste["cid"]
    assert cid, "der Treffer trägt keine Chunk-Kennung"
    r = mcp_server.similar_messages(cid=cid, k=5)
    assert r["backend"] == "semantic"
    assert r["count"] >= 1


def test_aehnliche_geben_nicht_den_treffer_selbst_zurueck(aehnliche_vektoren):
    cid = mcp_server.browse_messages(k=1)["results"][0]["cid"]
    r = mcp_server.similar_messages(cid=cid, k=5)
    assert cid not in [h["cid"] for h in r["results"]], \
        "der Ausgangstreffer steht in seiner eigenen Ähnlichkeitsliste"


def test_aehnliche_bei_unbekannter_kennung(state):
    assert mcp_server.similar_messages(cid=99999, k=5)["results"] == []


def test_aehnliche_ohne_embeddings(state):
    alt = mcp_server.STATE.get("semantic")
    mcp_server.STATE["semantic"] = False
    try:
        r = mcp_server.similar_messages(cid=1, k=5)
        assert r["results"] == [] and "error" in r
    finally:
        mcp_server.STATE["semantic"] = alt


# --------------------------------------------------------------------------
# Ollama switched off
#
# Falling back to BM25 only when a request fails would be accident rather
# than intent: whoever has deselected Ollama should not see it attempted
# at all.
# --------------------------------------------------------------------------
def test_ohne_ollama_wird_nicht_eingebettet(state, monkeypatch):
    def kein_netz(text):
        raise AssertionError("es wurde eingebettet, obwohl Ollama abgewählt ist")
    monkeypatch.setattr(mcp_server, "_embed_query", kein_netz)
    alt = dict(mcp_server.STATE)
    mcp_server.STATE.update(semantic=False, V=None, np=None)
    try:
        r = mcp_server.search_messages(query="Rechnung", k=3)
        assert r["backend"] == "lexical"
        assert "results" in r
    finally:
        mcp_server.STATE.clear()
        mcp_server.STATE.update(alt)


def test_jeder_treffer_nennt_das_verfahren(state):
    """Claude should see from the result what did the ranking – otherwise
    the instructions promise paraphrases a full-text index cannot deliver."""
    r = mcp_server.search_messages(query="Rechnung", k=3)
    assert r["backend"] in ("hybrid", "semantic", "lexical")


def test_die_anleitung_verspricht_keine_embeddings():
    """Claiming that paraphrases are found would be plainly untrue for an
    index without vectors."""
    text = mcp_server._INSTRUCTIONS
    assert "backend" in text, "das Feld, an dem man es sieht, wird nicht genannt"
    assert "lexical" in text, "der Fall ohne Embeddings kommt nicht vor"
    behauptung = "Ranks with BM25 and embeddings\nfused"
    assert behauptung not in text.replace("  ", " ")


# --------------------------------------------------------------------------
# The hard switch: off means off, for both transports
# --------------------------------------------------------------------------
def test_abgeschaltet_liefert_der_server_nichts_aus(state, monkeypatch):
    """A switch that only stopped the HTTP endpoint would be exactly the
    promise it does not keep: over stdio the client starts this program
    itself, without the app running at all."""
    echt, aus = [], []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: echt.append(kw))

    class FakeServer:
        def run(self, **kw):
            aus.append(kw)

    monkeypatch.setattr(mcp_server, "_abgeschaltet_server", FakeServer)
    monkeypatch.setenv("MCP_ENABLED", "0")

    for transport in ("stdio", "http"):
        aus.clear()
        monkeypatch.setattr(sys, "argv",
                            ["mcp_server.py", "--data-dir", str(state["tmp"]),
                             "--transport", transport, "--no-ollama"])
        mcp_server.main()
        assert not echt, f"{transport}: der echte Server lief"
        assert aus, f"{transport}: gar kein Server – der Client saehe nur einen Fehler"


@pytest.mark.anyio
async def test_abgeschalteter_server_hat_nur_die_auskunft():
    """A server that dies leaves the human a log file. This one tells the
    model, and the model passes it on."""
    aus = mcp_server._abgeschaltet_server()
    werkzeuge = await aus.list_tools()
    assert [w.name for w in werkzeuge] == ["archive_unavailable"]
    # Nothing that reads data – not even by accident.
    assert not ({"search_messages", "browse_messages", "get_document",
                 "read_source_file"} & {w.name for w in werkzeuge})
    assert "switched off" in aus.instructions
    assert "Settings" in aus.instructions


def test_force_serviert_trotzdem(state, monkeypatch):
    """Whoever starts the program by hand does not have the switch in
    front of them."""
    gestartet = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: gestartet.append(kw))
    monkeypatch.setenv("MCP_ENABLED", "0")
    monkeypatch.setattr(sys, "argv",
                        ["mcp_server.py", "--data-dir", str(state["tmp"]),
                         "--transport", "stdio", "--no-ollama", "--force"])
    mcp_server.main()
    assert gestartet and gestartet[0]["transport"] == "stdio"


def test_erlaubt_laeuft_wie_bisher(state, monkeypatch):
    gestartet = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: gestartet.append(kw))
    monkeypatch.setenv("MCP_ENABLED", "1")
    monkeypatch.setattr(sys, "argv",
                        ["mcp_server.py", "--data-dir", str(state["tmp"]),
                         "--transport", "stdio", "--no-ollama"])
    mcp_server.main()
    assert gestartet and gestartet[0]["transport"] == "stdio"


# --------------------------------------------------------------------------
# Split mirrors: OneDrive and SharePoint are sources of their own
# --------------------------------------------------------------------------
def test_where_trennt_die_spiegel_ueber_root():
    wo, params = mcp_server._where("", None, None, "onedrive")
    assert "(src = 'datei' AND root = ?)" in wo and params == ["onedrive"]
    wo, params = mcp_server._where("", None, None, "sharepoint")
    assert params == ["sharepoint"]
    # The umbrella source stays: old callers keep seeing both mirrors.
    wo, params = mcp_server._where("", None, None, "datei")
    assert wo == "src = ?" and params == ["datei"]


@pytest.fixture
def spiegel_db(tmp_path, monkeypatch):
    """A minimal DB holding only mirrored files of both roots."""
    db = tmp_path / "corpus.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE chunks(id INTEGER PRIMARY KEY, uid TEXT,"
                " seq INT, src TEXT, root TEXT, rel TEXT, who TEXT, ppl TEXT,"
                " ts REAL, date TEXT, title TEXT, ctx TEXT, text TEXT,"
                " gone TEXT, att TEXT, ext TEXT, thread TEXT)")
    zeilen = [
        ("od1", "onedrive", "Dateien/Doks/plan.pdf", None),
        ("sp1", "sharepoint", "Team X/Projects/Dateien/N/x.pdf", None),
        ("sp2", "sharepoint", "Team X/Projects/Dateien/N/tief/y.docx",
         "2026-02-01"),
    ]
    for i, (uid, root, rel, gone) in enumerate(zeilen):
        con.execute("INSERT INTO chunks(id, uid, seq, src, root, rel, date,"
                    " gone, ctx) VALUES(?,?,0,'datei',?,?,?,?,?)",
                    (i + 1, uid, root, rel, f"2026-01-0{i + 1}", gone,
                     rel.rsplit("/", 1)[0]))
    con.commit()
    con.close()
    alt = mcp_server.STATE.get("db")
    mcp_server.STATE["db"] = str(db)
    yield db
    mcp_server.STATE["db"] = alt


def test_list_files_wurzeln_und_ebene(spiegel_db):
    wurzeln = mcp_server.list_files()["roots"]
    assert [(w["root"], w["path"], w["files"]) for w in wurzeln] == [
        ("onedrive", "", 1), ("sharepoint", "Team X/Projects", 2)]

    ebene = mcp_server.list_files("sharepoint", "Team X/Projects/Dateien/N")
    assert [d["name"] for d in ebene["dirs"]] == ["tief"]
    assert [f["name"] for f in ebene["files"]] == ["x.pdf"]
    assert ebene["dirs"][0]["files"] == 1


def test_list_folders_kennt_die_spiegel_getrennt(spiegel_db):
    nur_sp = mcp_server.list_folders(source="sharepoint")["folders"]
    assert nur_sp and all(f["path"].startswith("Team X/") for f in nur_sp)
    nur_od = mcp_server.list_folders(source="onedrive")["folders"]
    assert [f["path"] for f in nur_od] == ["Dateien/Doks"]


def test_read_source_file_liest_die_spiegelwurzeln(state, tmp_path):
    """Source-file links of the mirrors: both roots serve the file."""
    for wurzel, schluessel in (("onedrive", "onedrive_dir"),
                               ("sharepoint", "sharepoint_dir")):
        basis = tmp_path / f"{wurzel}_export"
        (basis / "Team X/Lib/Dateien").mkdir(parents=True)
        datei = basis / "Team X/Lib/Dateien/a.txt"
        datei.write_text("inhalt", encoding="utf-8")
        mcp_server.STATE[schluessel] = str(basis)
        out = mcp_server.read_source_file(wurzel, "Team X/Lib/Dateien/a.txt")
        assert "error" not in out and "inhalt" in str(out)


def test_list_people_vertraegt_die_spiegelquellen(state):
    """The people table has no root column – the mirror sources must fold
    into src='datei' instead of raising OperationalError."""
    for quelle in ("onedrive", "sharepoint"):
        out = mcp_server.list_people(source=quelle)
        assert "people" in out


def test_list_folders_einheiten_je_spiegelquelle(state):
    """One board, one library, one site = ONE folder entry – the deep
    subpaths belong to the respective source, not to the filter menu."""
    con = sqlite3.connect(mcp_server.STATE["db"])
    zeilen = [
        ("planner:x/t1:0", "planner", "planner", "x/board.html",
         "Team X Board/Offen"),
        ("planner:x/t2:0", "planner", "planner", "x/board.html",
         "Team X Board/Erledigt"),
        ("sharepoint:Nordwind/Projects/Dateien/N/a.pdf:0", "datei",
         "sharepoint", "Nordwind/Projects/Dateien/N/a.pdf",
         "Nordwind/Projects/Dateien/N"),
        ("pages:Team X/Sub/seite.html:0", "pages", "pages",
         "Team X/Sub/seite.html", "Team X/Sub"),
    ]
    con.executemany(
        "INSERT INTO chunks (uid, seq, msg_idx, src, root, rel, ctx, text) "
        "VALUES (?, 0, 0, ?, ?, ?, ?, 'x')", zeilen)
    con.commit()
    con.close()

    def pfade(**kw):
        return {f["path"] for f in
                mcp_server.list_folders(limit=100, **kw)["folders"]}

    assert pfade(source="planner") == {"Team X Board"}
    assert pfade(source="sharepoint") == {"Nordwind/Projects"}
    assert pfade(source="pages") == {"Team X"}
    # OneDrive keeps the full folder tree.
    assert "Dateien/Kunden" not in pfade(source="sharepoint")
