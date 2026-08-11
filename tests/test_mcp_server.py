"""Tests für mcp_server.py – MCP-Tools über einem kleinen, echten Store.

Der Store (corpus.db + Vektordatei) wird pro Test mit den Schreib-Helfern aus
rag_index.py in tmp_path aufgebaut – damit ist das Schema garantiert identisch
mit dem, was mcp_server.py erwartet. Es werden KEINE Netzwerkaufrufe gemacht:
_embed_query wird immer gestubbt (Standard: wirft, wie bei "Ollama down");
Tests der semantischen Suche setzen deterministische Einheitsvektoren.
"""

import json
from datetime import date, datetime
from urllib.parse import quote

import anyio
import sqlite3

import numpy as np
import pytest
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import LATEST_PROTOCOL_VERSION

import corpus
import mcp_server
import rag_index
import store_layout

# --------------------------------------------------------------------------
# Testdaten: kleiner Korpus mit Teams-, Outlook-, Kalender- und Kontakt-Einträgen
# --------------------------------------------------------------------------
DIM = 16  # Vektor-Dimension: jeder Chunk bekommt einen eigenen Einheitsvektor

UID_T0 = "teams:1on1/alice__chat.html:0"
UID_T1 = "teams:1on1/alice__chat.html:1"
UID_T2 = "teams:1on1/alice__chat.html:2"
UID_TX = "teams:1on1/max__chat.html:0"
UID_M1 = "outlook:inbox/mail1.eml:0"
UID_M2 = "outlook:inbox/mail2.eml:0"
UID_M3 = "outlook:sent/protokoll.eml:0"
UID_CAL = "kalender:kalender/Arbeit/termin.ics:0"
UID_CON = "kontakte:kontakte/Team/alice.vcf:0"

# Lange Mail → mehrere überlappende Chunks (Test für _join_chunks/get_document)
LONG_TEXT = " ".join(
    f"Absatz {i}: die Quartalsplanung wurde ausführlich besprochen und Punkt {i} im Protokoll festgehalten."
    for i in range(50))

# Bewusst reines ASCII: die Fenster-Tests von read_source_file schneiden an
# Byte-Grenzen; Mehrbyte-Zeichen würden dort (korrekt) zu Ersatzzeichen.
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
             "2025-06-01 09:30", "Projekt Alpha", "1:1-Chat",
             "Hallo Bob, die Rechnung 4711 für Projekt Alpha ist fertig."),
        _rec(UID_T1, "teams", "teams", "1on1/alice__chat.html", "Bob Baumeister",
             "bob baumeister projekt alpha", _ts("2025-06-01 09:35"),
             "2025-06-01 09:35", "Projekt Alpha", "1:1-Chat",
             "Danke Alice, ich prüfe die Rechnung morgen früh."),
        _rec(UID_T2, "teams", "teams", "1on1/alice__chat.html", "Alice Beispiel",
             "alice beispiel projekt alpha", _ts("2025-06-01 09:40"),
             "2025-06-01 09:40", "Projekt Alpha", "1:1-Chat",
             "Perfekt, dann bis morgen im Büro!"),
        _rec(UID_TX, "teams", "teams", "1on1/max__chat.html", "(unbekannt)",
             "max mustermann", _ts("2025-06-02 10:00"),
             "2025-06-02 10:00", "Max", "1:1-Chat",
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
             "Kalender: Arbeit", "Ort: Raum 42. Agenda folgt."),
        _rec(UID_CON, "kontakte", "outlook", "kontakte/Team/alice.vcf", "",
             "alice beispiel alice@example.com", None, "", "Alice Beispiel",
             "Kontakte: Team", "Firma GmbH · Entwicklung. E-Mail: alice@example.com"),
    ]


# Neueste zuerst; Kontakt (ts = NULL) am Ende – erwartete browse-Reihenfolge
BROWSE_ORDER = [UID_M2, UID_CAL, UID_M1, UID_TX, UID_T2, UID_T1, UID_T0,
                UID_M3, UID_CON]


def _build_store(tmp_path):
    """Store + Export-Ordner in tmp_path anlegen (Schreibpfad aus rag_index.py)."""
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
    # Datei AUSSERHALB der Exporte – darf über read_source_file nie erreichbar sein
    (tmp_path / "geheim.txt").write_text("STRENG GEHEIM", encoding="utf-8")

    chunks = corpus.chunk_records(_sample_records())
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    assert len(chunks) <= DIM, "Testkorpus zu groß für die Vektor-Dimension"
    # Chunk i → Einheitsvektor e_i: Kosinus zum Query-Vektor q ist exakt q[i]
    V = np.zeros((len(chunks), DIM), dtype="float32")
    for i in range(len(chunks)):
        V[i, i] = 1.0
    rag_index.write_db(store, chunks)
    _, vp = rag_index.save_vectors(store, V)
    rag_index.write_info(store, "test-embed", DIM, len(chunks), vp)
    return store, chunks, teams_dir, outlook_dir


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Store bauen, STATE füllen und nach dem Test wiederherstellen.

    _embed_query wirft standardmäßig (kein Netzwerk!); semantische Tests
    überschreiben den Stub mit deterministischen Vektoren.
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
    """STATE leeren (Server nicht initialisiert) und danach wiederherstellen."""
    old = dict(mcp_server.STATE)
    mcp_server.STATE.clear()
    yield
    mcp_server.STATE.clear()
    mcp_server.STATE.update(old)


def _stub_semantic(monkeypatch, chunks, weights):
    """_embed_query so stubben, dass uid → Gewicht die Kosinus-Rangfolge vorgibt."""
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
# Hilfsfunktionen (ohne Store)
# --------------------------------------------------------------------------
def test_to_ts_parses_and_clamps_day_end():
    assert mcp_server._to_ts("2025-06-01", False) == datetime(2025, 6, 1).timestamp()
    assert mcp_server._to_ts("2025-06-01", True) == datetime(2025, 6, 1, 23, 59, 59).timestamp()
    assert mcp_server._to_ts("01.06.2025", False) is None  # falsches Format
    assert mcp_server._to_ts("", False) is None
    assert mcp_server._to_ts(None, True) is None


def test_where_builds_fragments():
    w, p = mcp_server._where("", None, None, "all")
    # _WHERE_ALL ist der Schnellpfad-Marker in _semantic_rank – Wert festnageln
    assert w == "1=1" and w == mcp_server._WHERE_ALL and p == []
    w, p = mcp_server._where("Alice", 1.0, 2.0, "teams")
    assert "src = ?" in w and "ppl LIKE ?" in w
    assert "ts >= ?" in w and "ts <= ?" in w
    assert p == ["teams", "%alice%", 1.0, 2.0]  # Person wird kleingeschrieben


def test_fts_match_sanitizes_query():
    # Freitext wird zu einer ODER-Liste zitierter Tokens – FTS5-Syntax
    # (AND/OR/NEAR, Klammern, Anführungszeichen) kann nicht injiziert werden.
    assert mcp_server._fts_match('Rechnung: 4711 AND "x(y)') == '"rechnung" OR "4711" OR "and" OR "x" OR "y"'
    assert mcp_server._fts_match("Größe") == '"größe"'
    assert mcp_server._fts_match("...!!!") == ""
    assert mcp_server._fts_match("") == ""


def test_rrf_merge_orders_by_reciprocal_rank():
    sem = [(1, 0.9), (2, 0.5)]
    lex = [(2, -1.0), (3, -2.0)]
    merged = mcp_server._rrf_merge(sem, lex)
    assert [cid for cid, _ in merged] == [2, 1, 3]  # 2 ist in beiden Listen
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
    f.write_bytes("ää".encode())  # 4 Bytes
    text, total, start, more = mcp_server._read_window(f, 0, 3)
    assert total == 4 and start == 0 and more
    assert text.startswith("ä") and "�" in text  # zerschnittene Sequenz


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
    assert n_outlook > 3  # die lange Mail wurde wirklich in mehrere Chunks geteilt
    assert out["by_source"]["outlook"] == {"chunks": n_outlook, "messages": 3}
    assert out["semantic_available"] is True
    assert out["default_backend"] == "hybrid"
    assert out["embed_model"] == "test-embed"
    assert out["vector_dtype"] == "float16"
    assert out["teams_dir"] == str(state["teams_dir"])


def test_corpus_stats_lexical_when_semantic_off(state):
    mcp_server.STATE["semantic"] = False
    out = mcp_server.corpus_stats()
    assert out["default_backend"] == "lexical"
    assert out["semantic_available"] is False
    assert out["embed_model"] is None


# --------------------------------------------------------------------------
# search_messages – lexikalischer Pfad (FTS5/BM25)
# --------------------------------------------------------------------------
def test_search_lexical_finds_and_dedupes(state):
    res = mcp_server.search_messages("Rechnung", mode="lexical")
    assert res["backend"] == "lexical"
    uids = _uids(res)
    assert set(uids) == {UID_T0, UID_T1, UID_M1}
    assert len(uids) == len(set(uids))  # eine Nachricht nur einmal
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
    # Personenfilter läuft über die ppl-Spalte (kleingeschriebene Namen + Adressen)
    res = mcp_server.search_messages("Rechnung", person="Carla", mode="lexical")
    assert _uids(res) == [UID_M1]
    res = mcp_server.search_messages("Rechnung", person="carla@example.com",
                                     mode="lexical")
    assert _uids(res) == [UID_M1]
    res = mcp_server.search_messages("Rechnung", person="Niemand", mode="lexical")
    assert res["count"] == 0


def test_search_date_filters(state):
    # Teams-Treffer sind vom 01.06., die Mail vom 10.06.
    res = mcp_server.search_messages("Rechnung", date_from="2025-06-05",
                                     mode="lexical")
    assert _uids(res) == [UID_M1]
    res = mcp_server.search_messages("Rechnung", date_to="2025-06-05",
                                     mode="lexical")
    assert set(_uids(res)) == {UID_T0, UID_T1}
    # date_to ist inklusiv (bis 23:59:59 des Tages)
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
# search_messages – semantischer Pfad und Hybrid-Fusion (RRF)
# --------------------------------------------------------------------------
def test_search_semantic_ranks_by_stubbed_cosine(state, monkeypatch):
    # Query-Vektor: Urlaubsmail am ähnlichsten, Rechnungsmail auf Platz 2.
    # Die Untergrenze steht hier bewusst aus: Platz 2 liegt mit 0,45 genau auf
    # ihr, und geprüft wird die Reihenfolge, nicht das Aussortieren.
    monkeypatch.setattr(mcp_server, "SEM_MIN", 0.0)
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.5})
    res = mcp_server.search_messages("freie Tage im Sommer", mode="semantic")
    assert res["backend"] == "semantic"
    uids = _uids(res)
    assert uids[0] == UID_M2 and uids[1] == UID_M1
    scores = [h["score"] for h in res["results"]]
    assert scores == sorted(scores, reverse=True)
    # Kosinus entspricht den (normierten) Stub-Gewichten: Platz 2 = halber Score
    assert scores[1] == pytest.approx(scores[0] * 0.5, abs=0.01)
    assert scores[0] > 0.8


def test_search_hybrid_fuses_semantic_and_lexical(state, monkeypatch):
    # Semantik: M1 vor M2. Lexikalisch trifft "Rechnung 4711" M1/T0/T1, aber
    # nie M2 – M2 kann nur über den semantischen Zweig in die Liste kommen.
    _stub_semantic(monkeypatch, state["chunks"], {UID_M1: 1.0, UID_M2: 0.6})
    res = mcp_server.search_messages("Rechnung 4711", mode="hybrid")
    assert res["backend"] == "hybrid"
    uids = _uids(res)
    assert uids[0] == UID_M1        # Platz 1 in beiden Backends → RRF-Sieger
    assert UID_M2 in uids           # reiner Semantik-Treffer bleibt erhalten
    assert UID_T0 in uids           # reiner BM25-Treffer bleibt erhalten


def test_search_auto_falls_back_to_lexical_when_ollama_down(state):
    # Der Fixture-Stub für _embed_query wirft – wie ein nicht erreichbares Ollama
    res = mcp_server.search_messages("Rechnung", mode="auto")
    assert res["backend"] == "lexical"
    assert set(_uids(res)) == {UID_T0, UID_T1, UID_M1}
    assert "nicht gestubbt" in mcp_server.STATE["last_semantic_error"]


def test_search_semantic_mode_reports_error_when_ollama_down(state):
    res = mcp_server.search_messages("Rechnung", mode="semantic")
    assert set(res) == {"error"}
    assert "Semantic ranking failed" in res["error"]


def test_search_lexical_mode_never_touches_embeddings(state):
    # mode="lexical" darf _embed_query gar nicht erst aufrufen
    res = mcp_server.search_messages("Urlaub", mode="lexical")
    assert res["backend"] == "lexical"
    assert _uids(res) == [UID_M2]
    assert "last_semantic_error" not in mcp_server.STATE


# --------------------------------------------------------------------------
# browse_messages
# --------------------------------------------------------------------------
def test_browse_newest_first_nulls_last(state):
    res = mcp_server.browse_messages(k=50)
    assert _uids(res) == BROWSE_ORDER  # ts absteigend, Kontakt ohne ts am Ende
    assert res["count"] == len(BROWSE_ORDER)
    assert res["results"][0]["score"] is None  # browse hat keine Relevanzwertung


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
    assert out["text"] == LONG_TEXT  # Überlappungen exakt entfernt
    assert out["title"] == "Protokoll Quartalsplanung"
    assert out["source"] == "outlook" and out["source_label"] == "Mail"
    assert out["uri"] == "o365://outlook/" + quote("sent/protokoll.eml", safe="")
    assert "context_before" not in out  # ohne Kontext-Parameter kein Kontext


def test_get_document_unknown_uid(state):
    out = mcp_server.get_document("outlook:gibtsnicht.eml:0")
    assert "error" in out and "gibtsnicht" in out["error"]


def test_get_document_conversation_context(state):
    out = mcp_server.get_document(UID_T1, context_before=1, context_after=1)
    assert [e["uid"] for e in out["context_before"]] == [UID_T0]
    assert [e["uid"] for e in out["context_after"]] == [UID_T2]
    assert out["context_before"][0]["who"] == "Alice Beispiel"
    assert "Rechnung 4711" in out["context_before"][0]["text"]
    # Kontext stammt nur aus derselben Datei – die fremde Teams-Datei fehlt
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
    assert out["people"][0]["name"] == "Alice Beispiel"  # häufigste zuerst
    assert out["total_distinct"] == 4
    assert "(unbekannt)" not in people and "" not in people


def test_list_people_source_contains_and_limit(state):
    out = mcp_server.list_people(source="teams")
    assert {p["name"]: p["messages"] for p in out["people"]} == \
        {"Alice Beispiel": 2, "Bob Baumeister": 1}
    # contains matcht Name ODER ppl-Tokens (auch E-Mail-Adressen)
    out = mcp_server.list_people(contains="carla")
    assert [p["name"] for p in out["people"]] == ["Carla Chef"]
    out = mcp_server.list_people(contains="doris@example.com")
    assert [p["name"] for p in out["people"]] == ["Doris Docs"]
    out = mcp_server.list_people(limit=1)
    assert out["count"] == 1 and out["total_distinct"] == 4


# --------------------------------------------------------------------------
# read_source_file – inkl. Path-Traversal-Schutz (sicherheitsrelevant!)
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
    # Kleine Fenster + offset müssen die Datei lückenlos rekonstruieren
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
    # Die Geheimdatei liegt direkt über den Export-Ordnern
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
    # Symlink INNERHALB des Exports, Ziel außerhalb → muss abgelehnt werden
    link = state["outlook_dir"] / "inbox" / "link.eml"
    link.symlink_to(state["tmp"] / "geheim.txt")
    out = mcp_server.read_source_file("outlook", "inbox/link.eml")
    assert out == {"error": "Path outside the export directory."}


def test_read_source_file_invalid_root_and_missing_file(state):
    out = mcp_server.read_source_file("kalender", "termin.ics")
    assert out == {"error": "source_root must be 'teams' or 'outlook'."}
    out = mcp_server.read_source_file("teams", "1on1/fehlt.html")
    assert out == {"error": "File not found: 1on1/fehlt.html"}
    out = mcp_server.read_source_file("teams", "")  # Verzeichnis, keine Datei
    assert "error" in out


# --------------------------------------------------------------------------
# MCP-Resource o365://{root}/{path}
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
# Nicht initialisierter STATE
# --------------------------------------------------------------------------
def test_tools_without_initialized_state(empty_state):
    # read_source_file scheitert kontrolliert (kein Export-Verzeichnis bekannt) …
    out = mcp_server.read_source_file("teams", "x.html")
    assert out == {"error": "source_root must be 'teams' or 'outlook'."}
    # … die DB-gestützten Tools werfen mangels STATE["db"] einen KeyError
    # (aktuelles Verhalten – hier festgenagelt)
    with pytest.raises(KeyError):
        mcp_server.corpus_stats()
    with pytest.raises(KeyError):
        mcp_server.search_messages("test", mode="lexical")
    with pytest.raises(KeyError):
        mcp_server.browse_messages()


def test_list_people_contains_ist_umlaut_unabhaengig(tmp_path):
    """SQLite-LIKE ist nur ASCII-case-insensitiv – py_lower() lässt auch
    großgeschriebene Umlaut-Eingaben ("MÜLLER") den Namen finden."""
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
# Schnellpfad in _semantic_rank (ungefiltert)
# --------------------------------------------------------------------------
def test_semantic_schnellpfad_ist_deckungsgleich(state, monkeypatch):
    """Ohne Filter wird die Matrix am Stück gelesen statt über eine id-Liste.

    Beide Wege müssen dieselben Treffer mit denselben Scores liefern. Der
    SQL-Weg wird über ein äquivalentes, aber nicht wörtlich gleiches WHERE
    erzwungen ("1=1 AND 1=1"), das denselben Zeilen entspricht.
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
    assert dict(schnell) == dict(ueber_sql)      # gleiche ids, gleiche Scores


def test_semantic_schnellpfad_bei_leerer_matrix(state, monkeypatch):
    monkeypatch.setitem(mcp_server.STATE, "V", np.zeros((0, DIM), dtype="float16"))
    con = mcp_server._db()
    try:
        assert mcp_server._semantic_rank(con, "q", mcp_server._WHERE_ALL, [], 5) == []
    finally:
        con.close()


# --------------------------------------------------------------------------
# Transport-Absicherung (DNS-Rebinding) für den HTTP-Transport
# --------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_ueberlaesst_die_pruefung_dem_sdk(host):
    # None = SDK-Automatik; die deckt genau die Loopback-Adressen ab
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
    # IPv6-Literal ohne Port: der Teil hinter dem letzten ":" ist keine Zahl
    assert mcp_server._with_port("[fe80::1]", 8365) == "[fe80::1]:8365"
    assert mcp_server._with_port("[fe80::1]:9000", 8365) == "[fe80::1]:9000"


# --------------------------------------------------------------------------
# MCP-Protokollebene
#
# Alle Tests oben rufen die Tool-Funktionen direkt auf – sie würden auch dann
# grün bleiben, wenn die Registrierung beim SDK gar nicht mehr funktioniert
# (genau das ist beim Wechsel FastMCP → MCPServer passiert). Die folgenden
# Tests sprechen deshalb echtes MCP: Client(mcp) verbindet sich in-process
# direkt mit dem Server-Objekt – kein Subprozess, kein HTTP, aber derselbe
# Weg über die Leitung, den auch Claude nimmt.
# --------------------------------------------------------------------------
TOOL_NAMES = {"search_messages", "browse_messages", "get_document",
              "get_thread", "list_people", "list_folders", "read_source_file", "corpus_stats"}


def _via_client(fn):
    """fn(client) gegen den In-Memory-Client laufen lassen."""
    async def run():
        async with Client(mcp_server.mcp) as c:
            return await fn(c)
    return anyio.run(run)


def _payload(res):
    """Rückgabewert eines Tools aus dem CallToolResult holen.

    Die Tools sind mit "-> dict" annotiert (ohne Wertetyp), deshalb erzeugt das
    SDK kein output_schema und keinen structured_content: das dict kommt als
    JSON-Text im content an. Das war unter FastMCP 1.x genauso.
    """
    assert res.is_error is False
    assert len(res.content) == 1
    return json.loads(res.content[0].text)


def test_server_metadaten_werden_ausgeliefert():
    """Name/Version/Instructions gehen mit der Initialisierung an den Client."""
    async def run():
        async with Client(mcp_server.mcp) as c:
            return c.server_info, c.instructions, c.protocol_version

    info, instr, proto = anyio.run(run)
    assert info.name == "office365-export"
    assert info.version                          # nicht leer
    assert proto == LATEST_PROTOCOL_VERSION      # neueste Revision, nicht 2025er
    # Die Instructions sollen bei der Tool-Auswahl helfen – der teure Ausweg
    # read_source_file muss als solcher benannt sein.
    assert instr and "search_messages" in instr
    assert "read_source_file" in instr


def test_alle_tools_sind_beim_sdk_registriert():
    tools = _via_client(lambda c: c.list_tools()).tools
    assert {t.name for t in tools} == TOOL_NAMES
    for t in tools:
        # Ohne Docstring bekommt Claude keine Beschreibung zu sehen
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
        "offset", "mode", "preview_chars", "only_gone", "folder"}
    assert schema["required"] == ["query"]      # nur query ist Pflicht


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
    """read_source_file gibt bei Traversal ein error-Feld zurück (kein Crash)."""
    res = _via_client(lambda c: c.call_tool(
        "read_source_file", {"source_root": "teams", "path": "../geheim.txt"}))
    assert "outside the export directory" in _payload(res)["error"]


def test_resource_traversal_wird_vom_sdk_abgewiesen(state):
    """mcp 2.x weist Traversal in Resource-URIs schon vor dem Handler ab.

    Zweite Verteidigungslinie ist weiterhin _resolve_source – siehe
    test_source_resource_rejects_traversal, das die Funktion direkt aufruft.
    """
    uri = "o365://teams/" + quote("../geheim.txt", safe="")

    # Der Fehler muss innerhalb des Client-Kontexts abgefangen werden: entkommt
    # er anyio.run(), verpackt die TaskGroup ihn in eine ExceptionGroup.
    async def run():
        async with Client(mcp_server.mcp) as c:
            with pytest.raises(MCPError, match="Unknown resource"):
                await c.read_resource(uri)

    anyio.run(run)


# --------------------------------------------------------------------------
# get_thread – ein Treffer allein sagt oft zu wenig
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
    # Undatiertes ans Ende: es zwischen zwei Tage zu schieben, wäre erfunden.
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
    # Und der Normalfall zeigt weiterhin alles, Gelöschtes eingeschlossen.
    assert mcp_server.browse_messages(k=50)["count"] == alle


def test_treffer_sagen_ob_die_mail_noch_da_ist(state):
    treffer = mcp_server.browse_messages(k=1)["results"][0]
    assert "gone" in treffer and treffer["gone"] is None


# --------------------------------------------------------------------------
# Der Ordner als Suchkriterium
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
# days: "die letzten sieben Tage", ohne dass der Anrufer rechnen muss
#
# Das Datum selbst auszurechnen ist die häufigste Gelegenheit, sich zu vertun –
# besonders über einen Monatswechsel hinweg. Deshalb Kalendertage und ein
# festgenageltes „heute“ in den Tests: sonst hinge das Ergebnis am Kalender des
# Tages, an dem sie laufen.
# --------------------------------------------------------------------------
@pytest.fixture
def heute(monkeypatch):
    """Ein festes „heute“ – der 10.06.2025, mitten in den Testdaten."""
    class Fix(date):
        @classmethod
        def today(cls):
            return date(2025, 6, 10)
    monkeypatch.setattr(mcp_server, "date", Fix)
    return date(2025, 6, 10)


def test_seit_tagen_zaehlt_heute_mit(heute):
    """7 heißt heute und die sechs Tage davor – nicht acht, nicht sechs."""
    assert mcp_server._seit_tagen(7) == datetime(2025, 6, 4).timestamp()
    assert mcp_server._seit_tagen(1) == datetime(2025, 6, 10).timestamp()
    # Über den Monatswechsel hinweg: genau der Fall, den man von Hand verrechnet.
    assert mcp_server._seit_tagen(30) == datetime(2025, 5, 12).timestamp()


@pytest.mark.parametrize("wert", [0, -3, None, "sieben"])
def test_seit_tagen_ohne_brauchbare_zahl(wert, heute):
    assert mcp_server._seit_tagen(wert) is None


def test_zeitraum_genanntes_datum_schlaegt_die_abkuerzung(heute):
    von, bis = mcp_server._zeitraum("2025-01-01", "", 7)
    assert von == datetime(2025, 1, 1).timestamp()
    assert bis is None, "days hat die obere Grenze gesetzt, obwohl von genannt war"


def test_zeitraum_days_begrenzt_beide_enden(heute):
    """Sonst holte „die letzten sieben Tage“ aus dem Kalender auch die Termine
    der nächsten Monate mit – die liegen ebenfalls hinter dem Startdatum."""
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
    """Am Store: days=7 liefert genau das Fenster 04.–10.06."""
    # 10.06. 08:00 liegt drin, 02.06. und 15.06. nicht.
    assert _uids(mcp_server.browse_messages(days=7)) == [UID_M1]
    # 9 Tage reichen bis zum 02.06. zurück.
    assert _uids(mcp_server.browse_messages(days=9)) == [UID_M1, UID_TX]
    assert mcp_server.browse_messages(days=1)["count"] == 1      # nur heute
    # Ohne obere Grenze wären der Termin am 15.06. und die Mail vom 01.07. mit
    # dabei – beide liegen in der Zukunft.
    assert UID_CAL not in _uids(mcp_server.browse_messages(days=7))
    assert UID_M2 not in _uids(mcp_server.browse_messages(days=7))


def test_browse_days_mit_ordner_und_quelle(state, heute):
    """Der Fall, für den es gedacht ist: ein Ordner plus ein Zeitraum."""
    res = mcp_server.browse_messages(days=9, source="outlook", folder="inbox")
    assert _uids(res) == [UID_M1]


def test_search_letzte_tage(state, heute):
    res = mcp_server.search_messages("Rechnung", days=7, mode="lexical")
    assert _uids(res) == [UID_M1]         # die Teams-Treffer vom 01.06. sind raus
    assert len(mcp_server.search_messages("Rechnung", mode="lexical")["results"]) > 1


def test_days_ohne_wirkung_wenn_date_from_genannt(state, heute):
    """Ein genanntes Datum gewinnt – auch wenn es viel weiter zurückreicht."""
    res = mcp_server.browse_messages(days=1, date_from="2025-05-01", source="teams")
    assert _uids(res) == [UID_TX, UID_T2, UID_T1, UID_T0]


def test_list_folders_kennt_beide_quellen(state, tmp_path):
    """Der Ordnerfilter in der Suche lud nur Postfachordner – gespiegelte
    OneDrive-Ordner fehlten, obwohl sie als ctx im Index stehen."""
    con = sqlite3.connect(mcp_server.STATE["db"])
    con.execute("INSERT INTO chunks (uid, seq, msg_idx, src, root, rel, ctx, text) "
                "VALUES ('datei:Dateien/Kunden/a.pdf:0', 0, 0, 'datei', 'onedrive', "
                "'Dateien/Kunden/a.pdf', 'Dateien/Kunden', 'Dateien / Kunden / a.pdf')")
    con.commit()
    con.close()
    pfade = {f["path"] for f in mcp_server.list_folders(limit=100)["folders"]}
    assert "Dateien/Kunden" in pfade, "OneDrive-Ordner fehlt im Filter"
    assert any(p.startswith("inbox") or "/" in p for p in pfade), "Postfach fehlt jetzt"


def test_vorschau_zeigt_die_fundstelle(state):
    """Rückmeldung aus dem Betrieb: eine Suche lieferte Mails, in denen das
    Wort erst weit hinten steht. Die Vorschau zeigte die ersten 200 Zeichen und
    damit nichts davon – der Treffer sah aus wie ein Fehlgriff, obwohl er
    goldrichtig war."""
    lang = "Vorspann ohne Bezug. " * 20 + "Hier steht Betriebsrat mittendrin."
    assert "Betriebsrat" not in lang[:200]
    v = mcp_server._ausschnitt(lang, ["betriebsrat"], 200)
    assert "Betriebsrat" in v and v.startswith("…") and len(v) <= 200


def test_vorschau_ohne_fundstelle_bleibt_der_anfang():
    """Beim Blättern gibt es keinen Begriff – dann ist der Anfang die beste
    Auskunft, die es gibt."""
    text = "Erster Satz. Zweiter Satz."
    assert mcp_server._ausschnitt(text, [], 12) == text[:12]
    assert mcp_server._ausschnitt(text, ["kommtnichtvor"], 12) == text[:12]


def test_vorschau_haelt_die_zugesagte_laenge(state):
    for n in (10, 40, 200):
        res = mcp_server.search_messages("Rechnung", mode="lexical", preview_chars=n)
        assert all(len(h["preview"]) <= n for h in res["results"])


# --------------------------------------------------------------------------
# Untergrenze der Bedeutungssuche
#
# Ohne sie liefert sie IMMER die besten k innerhalb des Filters – auch wenn
# nichts passt. Aus dem Betrieb berichtet: ein Tag eingegrenzt, ein Wort
# gesucht, 18 Treffer bekommen; zwei davon enthielten das Wort, die übrigen
# sechzehn waren schlicht alles, was an dem Tag ankam.
# --------------------------------------------------------------------------
def test_schwache_treffer_fallen_raus(state, monkeypatch):
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.2})
    monkeypatch.setattr(mcp_server, "SEM_MIN", 0.45)
    uids = _uids(mcp_server.search_messages("freie Tage im Sommer", mode="semantic"))
    assert UID_M2 in uids, "der starke Treffer fehlt"
    assert UID_M1 not in uids, "der schwache Treffer ist geblieben"


def test_ohne_grenze_kaeme_alles_zurueck(state, monkeypatch):
    """Die Gegenprobe im Test selbst: mit Grenze 0 verhält es sich wie vorher."""
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.2})
    monkeypatch.setattr(mcp_server, "SEM_MIN", 0.0)
    assert UID_M1 in _uids(mcp_server.search_messages("irgendwas", mode="semantic"))


def test_grenze_gilt_auch_mit_filter(state, monkeypatch):
    """Der berichtete Fall lief über den gefilterten Zweig – der hat eigenen
    Code und hätte die Grenze sonst nicht angewandt."""
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.2})
    monkeypatch.setattr(mcp_server, "SEM_MIN", 0.45)
    uids = _uids(mcp_server.search_messages("egal", mode="semantic", source="outlook"))
    assert UID_M2 in uids and UID_M1 not in uids


def test_nichts_passt_nichts_kommt(state, monkeypatch):
    """Wenn nichts die Grenze schafft, kommt nichts – nicht die am wenigsten
    unpassende Nachricht. (Die Grenze liegt hier über jedem möglichen Kosinus;
    _stub_semantic normalisiert die Gewichte, „alles schwach" ließe sich damit
    sonst nicht ausdrücken.)"""
    _stub_semantic(monkeypatch, state["chunks"], {UID_M2: 1.0, UID_M1: 0.9})
    monkeypatch.setattr(mcp_server, "SEM_MIN", 1.5)
    assert mcp_server.search_messages("xylophon quastenflosser", mode="semantic")["count"] == 0


def test_untergrenze_versteht_prozent_und_kosinus(monkeypatch):
    """In der Oberfläche steht eine Prozentzahl, im Code ein Kosinus. Wer sie
    von Hand in die Datei schreibt, tut es mal so, mal so."""
    for roh, erwartet in (("45", 0.45), ("0.45", 0.45), ("60", 0.60),
                          ("0", 0.0), ("unsinn", 0.45), ("500", 0.99)):
        monkeypatch.setenv("SEMANTIC_MIN", roh)
        assert mcp_server._sem_min() == pytest.approx(erwartet, abs=0.001), roh
