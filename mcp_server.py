#!/usr/bin/env python3
"""
mcp_server.py – expose the Teams + Outlook exports to Claude as an MCP server.

Instead of generating answers with a local LLM, this server hands the
*retrieval* to Claude as MCP tools and lets Claude be the reasoning/answer
layer. It reads the store built by rag_index.py:

    corpus.db     SQLite with all chunks + an FTS5 (BM25) full-text index and a
                  precomputed people table. Queried on demand – the server keeps
                  (almost) nothing in RAM and starts instantly.
    vectors-N.npy float16 embedding matrix, memory-mapped – the OS pages in only
                  what a query touches. info.json names the current one; a
                  new index run writes a new file instead of replacing this
                  one, which is what lets it run while this server holds the
                  mapping (see store_layout.py).

Ranking backends, per query:
  • hybrid   – default when embeddings are available: FTS5/BM25 and semantic
               cosine ranking run side by side and are merged with Reciprocal
               Rank Fusion. Exact tokens (invoice numbers, names) and
               paraphrases both hit.
  • semantic – cosine only (needs numpy + Ollama for the query embedding).
  • lexical  – FTS5/BM25 only, standard library, no Ollama needed. Automatic
               fallback when Ollama is down.

Tools: search_messages, browse_messages, get_document, list_people,
read_source_file, corpus_stats. Every hit carries an o365:// resource URI;
the corresponding MCP resource returns the raw source file.

Install (SDK required; numpy/requests only for semantic/hybrid ranking):
    pip install -r requirements.txt   # pinned; mcp 2.x (MCPServer API)

Run (HTTP, default – one shared server for all Claude sessions):
    python3 mcp_server.py --store rag_store \
        --teams teams_export --outlook outlook_export
    # → MCP endpoint at http://127.0.0.1:8365/mcp

    Register in Claude Code (.mcp.json):
        {"mcpServers": {"munimentum":
            {"type": "http", "url": "http://127.0.0.1:8365/mcp"}}}

    The server binds to 127.0.0.1 and has no authentication – it serves your
    complete mail and chat history, so keep it local. On loopback the SDK
    validates the Host and Origin headers, which stops a web page you happen
    to visit from talking to the server through your browser (DNS rebinding).

    That protection does not apply to any other bind address, so binding one
    requires naming the hostnames clients will use; the server refuses to
    start otherwise:
        python3 mcp_server.py --host 0.0.0.0 --allowed-host nas.local

Run (stdio – auto-launched per client, the classic setup):
    python3 mcp_server.py --transport stdio [--store …]

Switched off in the app (Settings → MCP server), this program serves nothing –
over stdio as well as HTTP. It does not exit: it runs a server that offers a
single tool saying so, which the client's model reads and passes on. Exiting
would leave the user with a failed connection and the reason in a log file. The
check sits in main() and nowhere else: the app calls the same functions
in-process for its own search, and what is switched off is the SERVER, not
reading the index. --force serves anyway.
"""

import os
import re
import sys
import sqlite3
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta
from urllib.parse import quote, unquote

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import export_util
import ollama_client
import settings
import store_layout
import version

# Windows consoles default to a legacy code page; force UTF-8 so logging the
# Unicode in messages never raises (no-op on macOS/Linux).
export_util.erzwinge_utf8()

STATE = {}          # populated in main(): db path, V (mmap), np, dirs, flags

# Server-level guidance, handed to the client together with the tool list –
# i.e. *before* a tool is picked, unlike the per-tool docstrings below. Keep it
# about choosing between the tools; details belong in the docstrings.
_INSTRUCTIONS = """\
Offline archive of the user's own Teams chats, Outlook mail, calendar and
contacts. Everything is local and read-only; there is no live mailbox access,
so anything not exported is simply absent.

Which tool to use:
  • search_messages – the default entry point. Ranking depends on this archive:
    with embeddings it fuses BM25 and semantic scoring, so exact tokens and
    paraphrases both work; without them it is BM25 only, and a paraphrase will
    miss. Every result says which was used in its "backend" field ("hybrid",
    "semantic" or "lexical") – when it reads "lexical", search with the words
    that would literally appear in the text rather than describing the idea.
    corpus_stats says the same thing up front.
  • browse_messages – when there is no query, only filters ("everything from
    Bob in June"). Newest first.
  • get_document    – full text of one hit, via the uid from a search/browse
    result. For chats, context_before/context_after return the neighbouring
    messages of the conversation.
  • list_people     – resolve a name before filtering; the person filter is a
    substring match over names and addresses.
  • list_folders / list_filetypes – what the folder and filetype filters can
    take, per source.
  • corpus_stats    – what is indexed and which ranking backend is live.
  • read_source_file – last resort: the raw .eml/.html file. Teams
    conversations can exceed 100 MB and come back windowed, so prefer
    get_document with context for chat history.

Notes: dates are "YYYY-MM-DD", and days=N is a shorthand for the last N days
(no need to work out the date); folder restricts to one folder and everything
below it – "E-Mail/Kunden", "kalender/Privat", "channels" for every Teams
channel – and list_folders shows what exists, per source; results are one hit
per message – page with offset rather than raising k; a hit's "uri" can be read
as an MCP resource.
"""

# Was der Client zu sehen bekommt, wenn der Zugriff abgeschaltet ist. Bewusst
# als Anweisung an das Modell formuliert: es soll den Satz weitergeben und nicht
# anfangen, den Fehler zu umgehen.
AUS_TEXT = (
    "MCP access to the Munimentum archive is switched off, so nothing is "
    "served: no search, no documents, no statistics. This is a deliberate "
    "setting, not a fault, and no other tool or path will get at the data. "
    "Tell the user in plain words that access is off and that they can allow "
    "it again in Munimentum under Settings -> MCP server -> \"Allow MCP "
    "access\". The entry in this client stays valid and works again the moment "
    "they do; nothing needs to be reconfigured."
)


def _abgeschaltet_server():
    """Ein Server, der genau eine Auskunft gibt: dass er abgeschaltet ist.

    Derselbe Name wie sonst – der Client hat ihn so eingetragen. Nur die
    Werkzeugliste ist eine andere: eines statt neun, und dieses eine liest
    nichts.
    """
    aus = MCPServer("munimentum", title="Munimentum", version=version.VERSION,
                    website_url="https://github.com/n-schilling/munimentum",
                    instructions=AUS_TEXT, log_level="WARNING")

    @aus.tool(annotations=_READONLY)
    def archive_unavailable() -> dict:
        """Why this archive is not answering. Report this to the user and stop.

        There is no way around it from here: no other tool, no file path, no
        retry. It is a setting in the Munimentum app.
        """
        return {"available": False, "reason": AUS_TEXT}

    return aus


mcp = MCPServer(
    "munimentum",
    title="Munimentum",
    version=version.VERSION,
    website_url="https://github.com/n-schilling/munimentum",
    instructions=_INSTRUCTIONS,
    # WARNING silences uvicorn's startup narration ("Started server process",
    # "Press CTRL+C to quit" …) in the app log; real problems still surface.
    log_level="WARNING",
)
_HTTP_PATH = "/mcp"             # streamable-http mount point (SDK default)

_READONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True,
                            openWorldHint=False)
_WORD = re.compile(r"\w+", re.UNICODE)
_SOURCE_LABEL = {"teams": "Teams", "outlook": "Mail", "datei": "Datei",
                 "onedrive": "OneDrive", "sharepoint": "SharePoint",
                 "kalender": "Kalender", "kontakte": "Kontakte"}
_WHERE_ALL = "1=1"              # _where() with no filters – the unfiltered case
_RRF_K = 60                     # standard reciprocal-rank-fusion constant
_POOL_MIN, _POOL_MAX = 100, 1000  # candidate pool per backend before merging

# Untergrenze für die Bedeutungssuche (Kosinus, normalisierte Vektoren).
#
# Ohne sie liefert sie IMMER die besten k innerhalb des Filters – auch wenn
# nichts passt. Wer einen Tag eingrenzt und nach einem Wort sucht, bekommt dann
# alle Nachrichten dieses Tages, nach Ähnlichkeit sortiert. Genau so berichtet:
# 18 Treffer, wovon 2 das Wort enthielten.
#
# An einem echten Index gemessen (bge-m3): eine Unsinnsanfrage kommt über 0,435
# nicht hinaus, während echte Anfragen noch beim 40. Treffer bei 0,50–0,63
# liegen. Dazwischen ist Platz. Wer ein anderes Modell benutzt, stellt es um.
def _sem_min():
    """Als Prozentzahl eingestellt (0–95), hier als Kosinus gebraucht.

    Prozent, weil die Oberfläche dann ein normales Zahlenfeld benutzen kann und
    niemand über ein Komma stolpert. Eine Zahl über 1 wird deshalb als Prozent
    gelesen – auch wenn jemand sie in der Datei von Hand einträgt.
    """
    roh = os.environ.get("SEMANTIC_MIN")
    if roh is None:
        roh = settings.value("semantic_min")
    try:
        wert = float(roh)
    except (TypeError, ValueError):
        return 0.45
    if wert > 1:
        wert /= 100.0
    return min(max(wert, 0.0), 0.99)


SEM_MIN = _sem_min()


def _db():
    """Fresh read-only connection per call – safe across MCP worker threads."""
    con = sqlite3.connect(f'file:{STATE["db"]}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con


# --------------------------------------------------------------------------
# Filters (SQL WHERE fragments shared by all query tools)
# --------------------------------------------------------------------------
def _hat_spalte(con, name):
    """Kennt der Index diese Spalte schon?

    Ein Index aus einer älteren Fassung hat sie nicht. Ohne diese Frage endete
    ein Klick auf „Nur Gelöschtes“ in einem SQL-Fehler statt in einem Hinweis.
    """
    return any(r[1] == name for r in con.execute("PRAGMA table_info(chunks)"))


# Welche Quellen eine Ordnerauswahl anbieten – alle, deren ctx ein Pfad ist.
_LISTBAR = ("outlook", "datei", "onedrive", "sharepoint", "kalender",
            "teams", "kontakte")


def _quelle_cond(quelle):
    """One source value as SQL condition – the mirrors are told apart.

    "onedrive" and "sharepoint" are both src='datei' rows; the stored root
    column separates them. "datei" stays as the umbrella for both, so old
    clients and saved queries keep working.
    """
    if quelle in ("onedrive", "sharepoint"):
        return "(src = 'datei' AND root = ?)", [quelle]
    return "src = ?", [quelle]
_LISTBAR_SQL = ", ".join(f"'{q}'" for q in _LISTBAR)

# Kanäle werden zu einem Eintrag zusammengefasst: ein Team hat schnell zwanzig,
# und "welcher Kanal" ist selten die Frage – "Kanäle statt Chats" dagegen oft.
# Der Filter kann das ohne Zutun, weil ein Pfad immer auch alles darunter meint.
_TEAMS_OBERSTE = (
    "CASE WHEN src = 'teams' AND instr(ctx, '/') > 0 "
    "THEN substr(ctx, 1, instr(ctx, '/') - 1) ELSE ctx END")


def _wie(text):
    """Ein Suchwort als LIKE-Muster: `*` ist der Platzhalter, sonst nichts.

    Die Personensuche war immer schon eine Teilstringsuche – nur konnte niemand
    sie steuern. Wer `*` tippte, suchte den Stern und fand nichts. Umgekehrt
    wirkten `%` und `_` unbeabsichtigt als Platzhalter, weil sie das für SQL
    nun einmal sind: "a_b" fand auch "axb". Beides ist hier geradegezogen.
    """
    roh = (text or "").strip().lower()
    fest = (roh.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
    return "%" + fest.replace("*", "%") + "%"


def _where(person, dfrom, dto, src, only_gone=False, folder="", filetype=""):
    conds, params = [], []
    if filetype:
        # ext hält die Endungen einer Nachricht durch Leerzeichen getrennt
        # ("pdf xlsx"). Mit Leerzeichen umschlossen trifft LIKE genau eine
        # davon – "doc" fände sonst auch "docx". Zeilen ohne Anhang haben
        # NULL und fallen damit von selbst heraus.
        conds.append("(' ' || ext || ' ') LIKE ?")
        params.append(f"% {str(filetype).strip().lower().lstrip('.')} %")
    if folder:
        # Der Ordner steht seit jeher als ctx im Index – er war nur nie
        # abfragbar. Ein Ordner meint immer auch seine Unterordner: wer
        # "E-Mail/Kunden" wählt, will nicht 288 Häkchen setzen.
        pfad = str(folder).strip().strip("/")
        conds.append("(ctx = ? OR ctx LIKE ?)")
        params.extend([pfad, pfad + "/%"])
    if only_gone:
        # Nur was im Postfach nicht mehr steht. Das ist die Frage, für die man
        # ein Archiv überhaupt hat – und sie ist sonst nicht zu beantworten.
        conds.append("gone IS NOT NULL")
    if src and src != "all":
        cond, werte = _quelle_cond(src)
        conds.append(cond)
        params.extend(werte)
    if person:
        conds.append("ppl LIKE ? ESCAPE '\\'")
        params.append(_wie(person))
    if dfrom is not None:
        conds.append("ts >= ?")                # also excludes NULL timestamps
        params.append(dfrom)
    if dto is not None:
        conds.append("ts <= ?")
        params.append(dto)
    return (" AND ".join(conds) or _WHERE_ALL), params


def _to_ts(s, end):
    """"YYYY-MM-DD" als Zeitstempel; None, wenn nichts angegeben wurde.

    Ein angegebenes, aber unlesbares Datum ("2021-06-31" – den gibt es nicht)
    ist ein Fehler und keine fehlende Angabe. Es stillschweigend fallen zu
    lassen hieße, ohne diese Grenze zu suchen und das Ergebnis als Antwort auf
    die gestellte Frage auszugeben.
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f'Kein gültiges Datum: "{s}" (erwartet: YYYY-MM-DD)') from None
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.timestamp()


def _seit_tagen(tage, heute=None):
    """„Die letzten N Tage“ als Zeitstempel – N=7 heißt heute und die sechs
    Tage davor, jeweils ab Mitternacht.

    Der Grund für den Parameter: „letzte 7 Tage“ ist die häufigste Frage ans
    Archiv, und ein Datum dafür auszurechnen ist eine Gelegenheit, sich zu
    vertun – vor allem am Monatsanfang.

    Kalendertage, nicht ein rollendes 7×24-Stunden-Fenster: sonst zeigte
    dieselbe Frage eine Mail von vor sieben Tagen um 8 Uhr je nach Uhrzeit des
    Fragens mal an und mal nicht. Über den Tag hinweg dieselbe Antwort ist
    mehr wert als die Genauigkeit auf die Stunde.
    """
    try:
        tage = int(tage)
    except (TypeError, ValueError):
        return None
    if tage <= 0:
        return None
    start = (heute or date.today()) - timedelta(days=tage - 1)
    return datetime(start.year, start.month, start.day).timestamp()


def _zeitraum(date_from, date_to, tage):
    """(von, bis) aus genannten Daten und/oder der Abkürzung `days`.

    Zwei Entscheidungen stecken darin:

    Ein genanntes date_from schlägt die Abkürzung – und schaltet sie ganz ab.
    Wer ein Datum hinschreibt, hat sich etwas dabei gedacht; ein Fehler bei
    beidem würde den Anrufer eine Runde kosten für etwas, das eindeutig zu
    entscheiden ist.

    `days` setzt BEIDE Grenzen, nicht nur die untere. „Die letzten sieben
    Tage“ ist ein Fenster, kein Anfang – und ohne obere Grenze holte die Frage
    aus dem Kalender auch die Termine der nächsten Monate mit, denn die liegen
    ebenfalls hinter dem Startdatum.
    """
    von, bis = _to_ts(date_from, False), _to_ts(date_to, True)
    if von is not None or not tage:
        return von, bis
    seit = _seit_tagen(tage)
    if seit is None:
        return von, bis
    heute = date.today()
    return seit, (bis if bis is not None else
                  datetime(heute.year, heute.month, heute.day,
                           23, 59, 59).timestamp())


# --------------------------------------------------------------------------
# Lexical backend: FTS5 / BM25
# --------------------------------------------------------------------------
def _fts_match(query):
    """Sanitize free text into an FTS5 OR-query of quoted tokens."""
    toks = _WORD.findall(query.lower())
    return " OR ".join(f'"{t}"' for t in toks)


def _lexical_rank(con, query, where, params, limit):
    match = _fts_match(query)
    if not match:
        return []
    sql = (f"SELECT c.id, bm25(chunks_fts) AS r FROM chunks_fts "
           f"JOIN chunks c ON c.id = chunks_fts.rowid "
           f"WHERE chunks_fts MATCH ? AND {where} ORDER BY r LIMIT ?")
    # bm25(): smaller = better; negate so every backend reports higher = better
    return [(row[0], -row[1]) for row in con.execute(sql, [match, *params, limit])]


# --------------------------------------------------------------------------
# Semantic backend: mmap'd float16 matrix, block-wise cosine scoring
# --------------------------------------------------------------------------
def _embed_query(text):
    np = STATE["np"]
    vec = ollama_client.embed([text], STATE["embed_model"], STATE["ollama"],
                              timeout=120)[0]
    v = np.asarray(vec, dtype="float32")
    nrm = np.linalg.norm(v)
    return v / nrm if nrm else v


def _semantic_rank(con, query, where, params, limit):
    np, V = STATE["np"], STATE["V"]
    B = 32768                                        # ~64 MB float16 per block

    # Unfiltered – the default for search_messages – means "every chunk", so
    # asking SQLite for the id list only to get back 1..n is pure overhead, and
    # gathering those rows copies what is already contiguous. Scoring the matrix
    # in slices instead is ~2.7x faster on a 270k-chunk corpus (105 ms → 39 ms).
    # Safe because chunks.id is a contiguous INTEGER PRIMARY KEY starting at 1
    # (vector row = id - 1) and _open_vectors() has already refused to load a
    # matrix whose row count disagrees with the chunk count.
    if where == _WHERE_ALL and not params:
        n = V.shape[0]
        if n == 0:
            return []
        qvec = _embed_query(query)                   # may raise (Ollama down)
        sims = np.empty(n, dtype=np.float32)
        for s in range(0, n, B):
            sims[s:s + B] = V[s:s + B].astype(np.float32) @ qvec
        take = min(limit, n)
        order = np.argpartition(-sims, take - 1)[:take]
        order = order[np.argsort(-sims[order])]
        return [(int(o) + 1, float(sims[o])) for o in order if sims[o] >= SEM_MIN]

    ids = np.fromiter((r[0] for r in
                       con.execute(f"SELECT id FROM chunks WHERE {where}", params)),
                      dtype=np.int64)
    if ids.size == 0:
        return []
    qvec = _embed_query(query)                       # may raise (Ollama down)
    sims = np.empty(ids.size, dtype=np.float32)
    for s in range(0, ids.size, B):
        block = ids[s:s + B] - 1                     # chunks.id → vector row
        sims[s:s + B] = V[block].astype(np.float32) @ qvec
    take = min(limit, ids.size)
    order = np.argpartition(-sims, take - 1)[:take]
    order = order[np.argsort(-sims[order])]
    return [(int(ids[o]), float(sims[o])) for o in order if sims[o] >= SEM_MIN]


def _rank_wie(con, cid, where, params, limit):
    """Ähnlichste Chunks zu einem, der schon im Index steht.

    Der Unterschied zu _semantic_rank ist der Ausgangsvektor: der liegt hier
    fertig in der Matrix. Es muss nichts eingebettet werden, also braucht das
    kein Ollama – und funktioniert auch dann, wenn die Bedeutungssuche über das
    Eingabefeld gerade nicht zur Verfügung steht.
    """
    np, V = STATE["np"], STATE["V"]
    zeile = cid - 1
    if zeile < 0 or zeile >= V.shape[0]:
        return []
    qvec = V[zeile].astype(np.float32)
    nrm = np.linalg.norm(qvec)
    if nrm:
        qvec = qvec / nrm

    ids = np.fromiter((r[0] for r in
                       con.execute(f"SELECT id FROM chunks WHERE {where}", params)),
                      dtype=np.int64) if where != _WHERE_ALL or params else None
    if ids is None:
        sims = np.empty(V.shape[0], dtype=np.float32)
        for s in range(0, V.shape[0], 32768):
            sims[s:s + 32768] = V[s:s + 32768].astype(np.float32) @ qvec
        ids = np.arange(1, V.shape[0] + 1)
    else:
        if ids.size == 0:
            return []
        sims = np.empty(ids.size, dtype=np.float32)
        for s in range(0, ids.size, 32768):
            sims[s:s + 32768] = V[ids[s:s + 32768] - 1].astype(np.float32) @ qvec
    take = min(limit + 1, ids.size)                  # +1: der Treffer selbst
    order = np.argpartition(-sims, take - 1)[:take]
    order = order[np.argsort(-sims[order])]
    # Sich selbst auszugeben wäre die trivialste und nutzloseste Antwort.
    return [(int(ids[o]), float(sims[o])) for o in order
            if int(ids[o]) != cid and sims[o] >= SEM_MIN][:limit]


# --------------------------------------------------------------------------
# Fusion, dedupe, result shaping
# --------------------------------------------------------------------------
def _rrf_merge(*ranked_lists):
    """Reciprocal Rank Fusion: score = Σ 1/(K + rank). Ignores raw scales."""
    scores = {}
    for lst in ranked_lists:
        for rank, (cid, _) in enumerate(lst):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def _rank(con, query, where, params, k, offset, mode):
    """Ranked (chunk_id, score) list + the backend actually used."""
    pool = min(_POOL_MAX, max(_POOL_MIN, (offset + k) * 5))
    lex = sem = None
    if mode in ("auto", "hybrid", "semantic") and STATE.get("semantic"):
        try:
            sem = _semantic_rank(con, query, where, params, pool)
        except Exception as e:                       # Ollama down, timeout, …
            STATE["last_semantic_error"] = str(e)
            if mode == "semantic":
                raise
    if mode in ("auto", "hybrid", "lexical") or sem is None:
        lex = _lexical_rank(con, query, where, params, pool)
    if sem is not None and lex is not None:
        return _rrf_merge(sem, lex), "hybrid"
    if sem is not None:
        return sem, "semantic"
    return lex or [], "lexical"


def _dedupe_page(con, pairs, k, offset):
    """Collapse chunk hits to messages (best chunk wins), then page."""
    if not pairs:
        return []
    ids = [cid for cid, _ in pairs]
    uid_of = {}
    CHUNK = 500                                      # SQLite variable limit safety
    for s in range(0, len(ids), CHUNK):
        part = ids[s:s + CHUNK]
        q = ",".join("?" * len(part))
        uid_of.update((r[0], r[1]) for r in con.execute(
            f"SELECT id, uid FROM chunks WHERE id IN ({q})", part))
    seen, page = set(), []
    for cid, score in pairs:
        uid = uid_of.get(cid)
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        if len(seen) > offset:
            page.append((cid, score))
            if len(page) >= k:
                break
    return page


def _source_uri(root, rel):
    """MCP resource URI for a source file (rel path percent-encoded)."""
    return f"o365://{root}/{quote(rel, safe='')}"


def _hit(row, score, preview_chars, woerter=()):
    h = {
        "uid": row["uid"],
        # Die Zeile im Index. Nur damit lässt sich später "Ähnliche zu diesem
        # Treffer" fragen, ohne die Anfrage neu einzubetten.
        "cid": row["id"] if "id" in row.keys() else None,
        "source": row["src"],
        # Files name their mirror: a hit from a SharePoint library should not
        # wear the same tag as one from the personal drive.
        "source_label": _SOURCE_LABEL.get(
            row["root"] if row["src"] == "datei" else row["src"], row["src"]),
        "root": row["root"],
        "who": row["who"],
        "date": row["date"],
        "title": row["title"],
        "context": row["ctx"],
        "path": row["rel"],
        "uri": _source_uri(row["root"], row["rel"]),
        "score": round(score, 4) if score is not None else None,
        # Kennung des Gesprächs, zu dem der Treffer gehört – damit lässt sich
        # der ganze Verlauf holen, statt nur die eine Nachricht zu lesen.
        "thread": (row["thread"] if "thread" in row.keys() else None),
        # Namen der Anhänge – der Grund, warum ein Vertrag im Archiv jetzt
        # auffindbar ist und nicht nur daliegt.
        "attachments": [a for a in (row["att"] or "").split(" ")
                        if a] if "att" in row.keys() else [],
        # Seit wann die Nachricht nicht mehr im Postfach steht. Leer heißt: sie
        # ist noch da. Die Datei liegt in beiden Fällen im Archiv.
        "gone": (row["gone"] if "gone" in row.keys() else None),
    }
    if preview_chars > 0:
        h["preview"] = _ausschnitt(row["text"], woerter, preview_chars)
    return h


def _ausschnitt(text, woerter, laenge):
    """Ein Stück Text um die erste Fundstelle – nicht stur der Anfang.

    Der Grund ist eine Rückmeldung aus dem Betrieb: eine Suche nach
    „Betriebsrat" lieferte Mails, in denen das Wort erst nach 900 Zeichen
    steht. Die Vorschau zeigte die ersten 200 und damit nichts davon, und der
    Treffer sah aus wie ein Fehlgriff, obwohl er goldrichtig war.

    Ohne Fundstelle – etwa beim Blättern ohne Suchbegriff – bleibt es der
    Anfang; der ist dann die beste Auskunft, die es gibt.
    """
    text = text or ""
    if not woerter or laenge <= 0:
        return text[:laenge]
    tief = text.lower()
    treffer = [i for i in (tief.find(w) for w in woerter) if i >= 0]
    if not treffer:
        return text[:laenge]
    # Etwas Vorlauf, damit der Fund nicht am linken Rand klebt.
    start = max(0, min(treffer) - laenge // 4)
    if not start:
        return text[:laenge]
    # Das Auslassungszeichen zählt mit: preview_chars ist eine Zusage über die
    # Länge, und ein Aufrufer, der mit 200 Zeichen rechnet, soll 200 bekommen.
    return "…" + text[start:start + laenge - 1].lstrip()


def _rows_for(con, pairs, preview_chars, woerter=()):
    hits = []
    for cid, score in pairs:
        row = con.execute("SELECT * FROM chunks WHERE id = ?", (cid,)).fetchone()
        if row is not None:
            hits.append(_hit(row, score, preview_chars, woerter))
    return hits


def _join_chunks(rows):
    """Reassemble a message's full text from its overlapping chunks (by seq)."""
    text = ""
    for row in rows:
        piece = row["text"] or ""
        if not text:
            text = piece
            continue
        cut = 0
        for L in range(min(len(text), len(piece), 300), 0, -1):  # drop overlap
            if text[-L:] == piece[:L]:
                cut = L
                break
        text += piece[cut:]
    return text


def _message_text(con, uid):
    rows = con.execute("SELECT * FROM chunks WHERE uid = ? ORDER BY seq",
                       (uid,)).fetchall()
    return (rows[0] if rows else None), _join_chunks(rows)


def _resolve_source(source_root, rel):
    """Sandboxed path resolution for an export file. Returns (Path, error_str)."""
    base = {"teams": STATE.get("teams_dir"),
            "outlook": STATE.get("outlook_dir"),
            "onedrive": STATE.get("onedrive_dir"),
            "sharepoint": STATE.get("sharepoint_dir")}.get(source_root)
    if not base:
        return None, ("source_root must be 'teams', 'outlook', 'onedrive' "
                      "or 'sharepoint'.")
    base = Path(base).resolve()
    target = (base / rel).resolve()
    if base != target and base not in target.parents:      # prevent path escape
        return None, "Path outside the export directory."
    if not target.is_file():
        return None, f"File not found: {rel}"
    return target, None


def _read_window(target, offset, max_chars):
    """Read a byte window of a file without ever loading the whole file.

    Some exported Teams conversations exceed 100 MB; decoding them entirely
    would pin gigabytes in a long-lived server process.
    """
    total = target.stat().st_size
    start = max(0, offset)
    n = max(1, min(max_chars, 500000))
    with open(target, "rb") as f:
        f.seek(start)
        data = f.read(n)
    # A window may split a multi-byte UTF-8 sequence at either edge;
    # errors="replace" turns the clipped bytes into a replacement char.
    return data.decode("utf-8", errors="replace"), total, start, start + len(data) < total


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
@mcp.tool(annotations=_READONLY)
def search_messages(query: str, person: str = "", date_from: str = "",
                    date_to: str = "", days: int = 0, source: str = "all",
                    k: int = 12, offset: int = 0, mode: str = "auto",
                    preview_chars: int = 200, only_gone: bool = False,
                    folder: str = "", filetype: str = "") -> dict:
    """Search the exported Teams messages and Outlook mail/calendar/contacts.

    Hybrid ranking (BM25 + semantic embeddings, fused) when available. Results
    are deduped to one hit per message; use get_document(uid) for the full text
    and pass offset to page through more results.

    Args:
        query: Natural-language query or keywords (German or English).
        person: Optional. Filter to messages involving this name or email.
        date_from: Optional. Inclusive lower bound, "YYYY-MM-DD". A date that
            does not exist is an error, not an omission.
        date_to: Optional. Inclusive upper bound, "YYYY-MM-DD".
        days: Shorthand for a date range: only the last N days, counting
            today (7 = today and the six days before). No need to work out the
            date yourself. Bounds the range at both ends, so upcoming calendar
            entries stay out. Ignored when date_from is given.
        source: One of "all", "teams", "outlook", "kalender", "kontakte",
            "datei" (files mirrored from OneDrive — name and path only,
            their contents are not indexed).
        k: Number of results per page (default 12).
        offset: Results to skip, for pagination (default 0).
        mode: "auto" (hybrid if embeddings available, else lexical),
              "hybrid", "semantic", or "lexical".
        preview_chars: Preview length per hit (default 200; 0 disables previews).
        only_gone: Only messages that are no longer in the mailbox (deleted from
            it after they were archived). Everything stays on disk either way.
        folder: Restrict to one folder and everything below it, e.g.
            "E-Mail/Kunden", "kalender/Privat" or "channels" for every Teams
            channel. Use list_folders to see what exists.
        filetype: Restrict to messages carrying an attachment of this type, or
            to mirrored files of it — "pdf", "xlsx". One type; use
            list_filetypes to see what exists.
    """
    con = _db()
    try:
        if only_gone and not _hat_spalte(con, "gone"):
            return {"error": "This index predates deletion tracking. Rebuild it "
                             "(Export tab → “Index only”) to use only_gone.",
                    "count": 0, "results": []}
        von, bis = _zeitraum(date_from, date_to, days)
        where, params = _where(person.strip(), von, bis, source, only_gone, folder,
                               filetype)
        try:
            pairs, used = _rank(con, query.strip(), where, params,
                                max(1, k), max(0, offset), mode)
        except Exception as e:
            return {"error": f"Semantic ranking failed: {e}. "
                             f"Is Ollama running? Try mode='lexical'."}
        page = _dedupe_page(con, pairs, max(1, k), max(0, offset))
        return {"backend": used, "count": len(page), "offset": max(0, offset),
                "results": _rows_for(con, page, max(0, min(preview_chars, 2000)),
                                    _WORD.findall(query.lower()))}
    finally:
        con.close()


def similar_messages(cid: int, k: int = 12, preview_chars: int = 200):
    """Nachrichten, die dieser einen ähneln – ohne neue Anfrage.

    Bewusst kein MCP-Werkzeug, sondern nur für die Oberfläche: Claude formuliert
    seine Anfragen selbst und braucht diesen Umweg nicht. Der Wert liegt beim
    Menschen, der einen Treffer vor sich hat und „mehr davon“ will.

    Und es geht ohne Ollama: der Ausgangsvektor steht schon in der Matrix.
    """
    if not STATE.get("semantic"):
        return {"error": "This index has no embeddings.", "count": 0, "results": []}
    con = _db()
    try:
        pairs = _rank_wie(con, int(cid), _WHERE_ALL, [], max(1, k) * 3)
        page = _dedupe_page(con, pairs, max(1, k), 0)
        return {"backend": "semantic", "count": len(page), "offset": 0,
                "results": _rows_for(con, page,
                                     max(0, min(preview_chars, 2000)), ())}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def browse_messages(person: str = "", date_from: str = "", date_to: str = "",
                    days: int = 0, source: str = "all", k: int = 30,
                    offset: int = 0, preview_chars: int = 200,
                    only_gone: bool = False, folder: str = "",
                    filetype: str = "") -> dict:
    """List messages by filter, newest first, without a search query.

    Useful for "everything from <person> in <month>", "the last week in
    <folder>", or scanning a source. Pass offset to page through more results.

    Args:
        person: Optional name or email to filter by.
        date_from: Optional inclusive "YYYY-MM-DD" lower bound.
        date_to: Optional inclusive "YYYY-MM-DD" upper bound.
        days: Shorthand for a date range: only the last N days, counting
            today (7 = today and the six days before). No need to work out the
            date yourself. Bounds the range at both ends, so upcoming calendar
            entries stay out. Ignored when date_from is given.
        source: One of "all", "teams", "outlook", "kalender", "kontakte",
            "datei" (files mirrored from OneDrive — name and path only,
            their contents are not indexed).
        k: Max results per page (default 30).
        offset: Results to skip, for pagination (default 0).
        preview_chars: Preview length per hit (default 200; 0 disables previews).
        only_gone: Only messages that are no longer in the mailbox (deleted from
            it after they were archived). Everything stays on disk either way.
        folder: Restrict to one folder and everything below it, e.g.
            "E-Mail/Kunden", "kalender/Privat" or "channels" for every Teams
            channel. Use list_folders to see what exists.
        filetype: Restrict to messages carrying an attachment of this type, or
            to mirrored files of it — "pdf", "xlsx". One type; use
            list_filetypes to see what exists.
    """
    con = _db()
    try:
        if only_gone and not _hat_spalte(con, "gone"):
            return {"error": "This index predates deletion tracking. Rebuild it "
                             "(Export tab → “Index only”) to use only_gone.",
                    "count": 0, "results": []}
        von, bis = _zeitraum(date_from, date_to, days)
        where, params = _where(person.strip(), von, bis, source, only_gone, folder,
                               filetype)
        # Plain "ts DESC" rather than "(ts IS NULL), ts DESC": SQLite sorts NULL
        # below every value, so DESC already puts undated messages last – same
        # order, but ix_chunks_msg_ts can serve it without a temp sort.
        rows = con.execute(
            f"SELECT * FROM chunks WHERE seq = 0 AND {where} "
            f"ORDER BY ts DESC LIMIT ? OFFSET ?",
            [*params, max(1, k), max(0, offset)]).fetchall()
        pc = max(0, min(preview_chars, 2000))
        return {"count": len(rows), "offset": max(0, offset),
                "results": [_hit(r, None, pc) for r in rows]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def get_thread(thread: str, limit: int = 50) -> dict:
    """Alle Nachrichten eines Gesprächs, chronologisch.

    Ein Treffer allein sagt oft zu wenig: „Ja, machen wir so“ ist erst mit der
    Frage davor eine Aussage. `thread` steht an jedem Treffer aus
    search_messages.
    """
    if not thread:
        return {"thread": "", "count": 0, "messages": []}
    con = _db()
    try:
        if not _hat_spalte(con, "thread"):
            return {"thread": thread, "count": 0, "messages": [],
                    "error": "This index predates conversation grouping. "
                             "Rebuild it (Export tab → “Index only”)."}
        rows = con.execute(
            "SELECT * FROM chunks WHERE thread = ? AND seq = 0 "
            "ORDER BY ts IS NULL, ts LIMIT ?",
            (thread, max(1, min(int(limit), 500)))).fetchall()
        return {"thread": thread, "count": len(rows),
                "messages": [_hit(r, None, 400) for r in rows]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def get_document(uid: str, context_before: int = 0, context_after: int = 0) -> dict:
    """Return the full text and metadata of a single message/mail by its uid.

    The uid comes from a search_messages / browse_messages result. For chat
    messages, context_before/context_after also return the neighboring messages
    of the same conversation – usually much cheaper than reading the whole
    conversation file.

    Args:
        uid: Message id from a search/browse hit.
        context_before: Neighboring messages before this one (default 0, max 20).
        context_after: Neighboring messages after this one (default 0, max 20).
    """
    con = _db()
    try:
        row, text = _message_text(con, uid)
        if row is None:
            return {"error": f"No message with uid {uid!r}."}
        out = {
            "uid": row["uid"],
            "source": row["src"],
            "source_label": _SOURCE_LABEL.get(row["src"], row["src"]),
            "who": row["who"],
            "date": row["date"],
            "title": row["title"],
            "context": row["ctx"],
            "path": row["rel"],
            "uri": _source_uri(row["root"], row["rel"]),
            "text": text,
        }
        before = max(0, min(context_before, 20))
        after = max(0, min(context_after, 20))
        if before or after:
            idx = row["msg_idx"]
            nb = con.execute(
                "SELECT DISTINCT uid FROM chunks WHERE root = ? AND rel = ? "
                "AND msg_idx BETWEEN ? AND ? AND uid != ? ORDER BY msg_idx",
                (row["root"], row["rel"], idx - before, idx + after, uid)).fetchall()
            ctx_b, ctx_a = [], []
            for (n_uid,) in nb:
                n_row, n_text = _message_text(con, n_uid)
                if n_row is None:
                    continue
                entry = {"uid": n_uid, "who": n_row["who"], "date": n_row["date"],
                         "text": n_text[:800]}
                (ctx_b if n_row["msg_idx"] < idx else ctx_a).append(entry)
            out["context_before"] = ctx_b
            out["context_after"] = ctx_a
        return out
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def list_people(source: str = "all", contains: str = "", limit: int = 100) -> dict:
    """List the people in the corpus (senders / chat authors) with message counts.

    Use this to discover valid values for the `person` filter of search_messages
    and browse_messages.

    Args:
        source: One of "all", "teams", "outlook", "kalender", "kontakte",
            "datei" (files mirrored from OneDrive — name and path only,
            their contents are not indexed).
        contains: Optional. Only people whose name or email contains this text;
            `*` stands for any run of characters.
        limit: Max number of people to return, most frequent first (default 100).
    """
    con = _db()
    try:
        conds = ["who != '' AND who != '(unbekannt)'"]
        params = []
        if source != "all":
            cond, werte = _quelle_cond(source)
            conds.append(cond)
            params.extend(werte)
        if contains.strip():
            # SQLite's LIKE/lower() are ASCII-only; register Python lower() so
            # umlaut-cased input ("MÜLLER") still matches. ppl is stored
            # pre-lowercased by the indexer, so only `who` needs folding.
            con.create_function("py_lower", 1,
                                lambda s: s.lower() if isinstance(s, str) else s,
                                deterministic=True)
            conds.append("(py_lower(who) LIKE ? ESCAPE '\\' "
                         "OR ppl LIKE ? ESCAPE '\\')")
            pat = _wie(contains)
            params += [pat, pat]
        where = " AND ".join(conds)
        rows = con.execute(
            f"SELECT who, SUM(messages) AS m FROM people WHERE {where} "
            f"GROUP BY who ORDER BY m DESC, who LIMIT ?",
            [*params, max(1, limit)]).fetchall()
        # Auch die Summe: die Oberfläche bietet „alle mit diesem Namensteil“ als
        # eigene Zeile an und muss dieselbe Größe nennen wie die Zeilen darüber
        # – sonst stünden Nachrichten neben Personen in einer Liste.
        total, nachrichten = con.execute(
            f"SELECT COUNT(DISTINCT who), COALESCE(SUM(messages), 0) "
            f"FROM people WHERE {where}", params).fetchone()
        return {"count": len(rows), "total_distinct": total,
                "total_messages": nachrichten,
                "people": [{"name": r[0], "messages": r[1]} for r in rows]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def read_source_file(source_root: str, path: str, max_chars: int = 100000,
                     offset: int = 0) -> dict:
    """Read a raw exported source file (e.g. a whole .eml or Teams conversation).

    Large files (some Teams conversations exceed 100 MB) are returned in
    windows: the reply contains total_chars and truncated – pass offset to read
    the next window. Prefer get_document with context_before/context_after for
    chat conversations; it is far cheaper.

    Args:
        source_root: "teams" or "outlook" (the export the file belongs to).
        path: Relative path within that export, as returned in a hit's "path".
        max_chars: Max bytes to return (default 100000, cap 500000).
        offset: Byte position to start reading from (default 0).
    """
    target, err = _resolve_source(source_root, path)
    if err:
        return {"error": err}
    content, total, start, truncated = _read_window(target, offset, max_chars)
    return {"source_root": source_root, "path": path, "suffix": target.suffix,
            "total_bytes": total, "offset": start, "truncated": truncated,
            "content": content}


@mcp.tool(annotations=_READONLY)
def list_folders(contains: str = "", limit: int = 200, source: str = "") -> dict:
    """List the folders present in the archive, with item counts.

    The counterpart to the `folder` filter on search_messages: it tells you
    what can be filtered on. Covers mailbox folders below "E-Mail/", mirrored
    OneDrive folders below "Dateien/", calendars below "kalender/", contact
    folders below "kontakte/" and the four kinds of Teams conversation
    ("1on1", "group", "meeting", "channels").

    Args:
        contains: Only folders whose path contains this text.
        limit: How many folders to return, largest first.
        source: Restrict to one source — "teams", "outlook", "kalender",
            "kontakte" or "datei". Empty (or "all") lists every source.
    """
    con = _db()
    try:
        wo, params = "", []
        if contains.strip():
            wo = "AND ordner LIKE ?"
            params.append(f"%{contains.strip()}%")
        quelle = (source or "").strip().lower()
        if quelle and quelle != "all":
            if quelle not in _LISTBAR:
                return {"count": 0, "folders": []}
            cond, werte = _quelle_cond(quelle)
            wo += f" AND {cond}"
            params.extend(werte)
        rows = con.execute(
            f"SELECT ordner, COUNT(DISTINCT uid) FROM "
            f"(SELECT uid, src, root, {_TEAMS_OBERSTE} AS ordner FROM chunks "
            f" WHERE src IN ('outlook', 'datei', 'kalender', 'teams', 'kontakte')"
            f" AND ctx IS NOT NULL AND ctx != '') "
            f"WHERE 1=1 {wo} "
            f"GROUP BY ordner ORDER BY 2 DESC LIMIT ?",
            [*params, max(1, min(int(limit), 1000))]).fetchall()
        return {"count": len(rows),
                "folders": [{"path": r[0], "messages": r[1]} for r in rows]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def list_filetypes(limit: int = 40, source: str = "") -> dict:
    """List the attachment and file types present in the archive, with counts.

    The counterpart to the `filetype` filter. A count is the number of messages
    carrying at least one attachment of that type — plus, under source
    "datei", the mirrored OneDrive files themselves.

    Args:
        limit: How many types to return, most frequent first.
        source: Restrict to one source — mainly "outlook" (mail attachments)
            or "datei" (mirrored files). Empty lists every source.
    """
    con = _db()
    try:
        wo, params = "", []
        quelle = (source or "").strip().lower()
        if quelle and quelle != "all":
            cond, werte = _quelle_cond(quelle)
            wo = f"AND {cond}"
            params.extend(werte)
        # Eine Zeile trägt alle ihre Endungen ("pdf xlsx"); die Zahl je Typ
        # entsteht daher hier und nicht in SQL. Verschiedene Kombinationen gibt
        # es nur einige hundert, das ist billiger als es aussieht.
        zahl = {}
        for ext, n in con.execute(
                f"SELECT ext, COUNT(DISTINCT uid) FROM chunks "
                f"WHERE ext IS NOT NULL AND ext != '' {wo} GROUP BY ext", params):
            for e in ext.split(" "):
                if e:
                    zahl[e] = zahl.get(e, 0) + n
        oben = sorted(zahl.items(), key=lambda x: (-x[1], x[0]))
        grenze = max(1, min(int(limit), 200))
        return {"count": min(len(oben), grenze), "total_distinct": len(oben),
                "filetypes": [{"type": e, "messages": n} for e, n in oben[:grenze]]}
    finally:
        con.close()


@mcp.tool(annotations=_READONLY)
def corpus_stats() -> dict:
    """Report corpus size, per-source counts, and which ranking backend is active."""
    con = _db()
    try:
        by_src = {r[0]: {"chunks": r[1], "messages": r[2]} for r in con.execute(
            "SELECT src, COUNT(*), COUNT(DISTINCT uid) FROM chunks GROUP BY src")}
        n = sum(v["chunks"] for v in by_src.values())
        return {
            "chunks": n,
            "by_source": by_src,
            "default_backend": "hybrid" if STATE.get("semantic") else "lexical",
            "semantic_available": bool(STATE.get("semantic")),
            "embed_model": STATE.get("embed_model") if STATE.get("semantic") else None,
            "vector_dtype": STATE.get("vector_dtype"),
            "last_semantic_error": STATE.get("last_semantic_error"),
            "teams_dir": STATE.get("teams_dir"),
            "outlook_dir": STATE.get("outlook_dir"),
        }
    finally:
        con.close()


def list_files(root="", path=""):
    """One level of the mirrored file tree – the file browser's data.

    Without a root: the entry points, "onedrive" plus one per mirrored
    SharePoint site/library. With root (and optionally a folder path): the
    immediate subfolders with their file counts, and the files sitting right
    there – name, date, tombstone. Everything comes from the index; sizes
    are the caller's business (the app stats the local mirror).
    """
    con = _db()
    try:
        if not root:
            rows = con.execute("SELECT root, rel FROM chunks "
                               "WHERE src = 'datei' AND seq = 0").fetchall()
            eigene = sum(1 for r in rows if r[0] == "onedrive")
            bibliotheken = {}
            for wurzel, rel in rows:
                if wurzel == "sharepoint":
                    teile = rel.split("/")
                    if len(teile) >= 2:
                        k = "/".join(teile[:2])
                        bibliotheken[k] = bibliotheken.get(k, 0) + 1
            wurzeln = []
            if eigene:
                wurzeln.append({"root": "onedrive", "path": "",
                                "label": "OneDrive", "files": eigene})
            for k in sorted(bibliotheken):
                wurzeln.append({"root": "sharepoint", "path": k,
                                "label": k, "files": bibliotheken[k]})
            return {"roots": wurzeln}
        praefix = (path or "").strip("/")
        wo, params = "src = 'datei' AND seq = 0 AND root = ?", [root]
        if praefix:
            fest = (praefix.replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_"))
            wo += " AND rel LIKE ? ESCAPE '\\'"
            params.append(fest + "/%")
        rows = con.execute(
            f"SELECT rel, date, gone FROM chunks WHERE {wo}", params).fetchall()
        schnitt = len(praefix) + 1 if praefix else 0
        ordner, dateien = {}, []
        for rel, datum, weg in rows:
            rest = rel[schnitt:]
            if "/" in rest:
                kopf = rest.split("/", 1)[0]
                d = ordner.setdefault(kopf, {"name": kopf,
                                             "path": f"{praefix}/{kopf}".strip("/"),
                                             "files": 0})
                d["files"] += 1
            else:
                dateien.append({"name": rest, "rel": rel, "date": datum,
                                "gone": weg})
        dateien.sort(key=lambda e: e["name"].lower())
        return {"root": root, "path": praefix,
                "dirs": sorted(ordner.values(), key=lambda e: e["name"].lower()),
                "files": dateien[:2000]}
    finally:
        con.close()


# --------------------------------------------------------------------------
# MCP resources – fetch a source file by its URI (as advertised in each hit)
# --------------------------------------------------------------------------
@mcp.resource("o365://{root}/{path}")
def source_resource(root: str, path: str) -> str:
    """Return a raw exported source file by URI.

    URI form: o365://{root}/{path}, where {root} is "teams" or "outlook" and
    {path} is the export-relative file path, percent-encoded (slashes as %2F).
    This is the "uri" field returned with every search/browse hit. Files larger
    than 500k characters are truncated – use the read_source_file tool with
    offset to page through the rest.
    """
    target, err = _resolve_source(root, unquote(path))
    if err:
        raise ValueError(err)
    content, total, _, truncated = _read_window(target, 0, 500000)
    if truncated:
        content += (f"\n\n[truncated: {total} bytes total – use the "
                    f"read_source_file tool with offset to read more]")
    return content


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _with_port(host, port):
    """Append the default port unless the value already carries one.

    Written so an IPv6 literal ("[fe80::1]") is not mistaken for host:port –
    only a trailing all-digit segment counts as a port.
    """
    _, sep, tail = host.rpartition(":")
    return host if sep and tail.isdigit() else f"{host}:{port}"


def _transport_security(host, port, allowed):
    """Origin/Host validation (DNS-rebinding protection) for the HTTP transport.

    The SDK switches this on by itself for loopback binds, and there the
    defaults are exactly right. It does *not* for any other address – which is
    the one case where it matters: a server reachable from the network, with no
    authentication, serving the complete mail and chat history. Without Host
    and Origin checks, any web page the user happens to open can POST to this
    endpoint and read the archive out through the browser.

    Guessing the legitimate hostnames is not possible, so they have to be named
    with --allowed-host. Refusing to start beats starting unprotected.
    """
    if host in _LOOPBACK:
        return None                     # SDK default already validates these
    if not allowed:
        raise SystemExit(
            f"Refusing to bind {host} without --allowed-host.\n"
            f"This server has no authentication and serves your whole mail and\n"
            f"chat history. Off the loopback interface, Host/Origin validation\n"
            f"is the only thing standing between it and any web page you open.\n"
            f"  • keep it local:  drop --host (defaults to 127.0.0.1)\n"
            f"  • or name the hostnames clients will use:\n"
            f"      --host {host} --allowed-host myhost.local --allowed-host 192.168.1.5")
    hosts = [_with_port(h, port) for h in allowed]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"{scheme}://{h}" for h in hosts
                         for scheme in ("http", "https")])


def _open_vectors(store, n_chunks):
    """Memory-map the vector file if numpy + the file are present.

    Its name comes from info.json and changes with every index run: the mapping
    below stays open for as long as this process lives, and on Windows a mapped
    file cannot be replaced. Writing a new one each time is what keeps indexing
    possible while a reader is running (see store_layout).
    """
    vp = store_layout.vectors_path(store)
    if not vp:
        return None, None
    try:
        import numpy as np
    except ImportError:
        print("numpy not installed – semantic/hybrid ranking disabled.",
              file=sys.stderr)
        return None, None
    V = np.load(vp, mmap_mode="r")
    if V.shape[0] != n_chunks:
        print(f"Index/DB mismatch ({V.shape[0]} vectors vs {n_chunks} chunks) – "
              f"rebuild with rag_index.py. Lexical ranking only.", file=sys.stderr)
        return None, None
    return np, V


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Defaults come from app_config.json when it exists; flags win (settings.py).
    # Ein Ordner statt drei. Die Unterordner heißen fest, wie überall im
    # Projekt – Claude startet den Server in einem unbekannten Arbeits-
    # verzeichnis, also muss dieser eine Pfad absolut mitkommen.
    ap.add_argument("--data-dir", metavar="ORDNER",
                    help="Datenordner mit rag_store/, teams_export/ und "
                         "outlook_export/ darin. Ohne ihn gilt das aktuelle "
                         "Verzeichnis.")
    ap.add_argument("--store", help=argparse.SUPPRESS)
    ap.add_argument("--teams", help=argparse.SUPPRESS)
    ap.add_argument("--outlook", help=argparse.SUPPRESS)
    ap.add_argument("--onedrive", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--sharepoint", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--embed-model", default=settings.value("embed_model"))
    ap.add_argument("--ollama", default=settings.value("ollama"))
    # Abgeschaltet heißt: gar nicht erst versuchen. Ohne das entscheidet der
    # Server pro Anfrage neu und läuft jedes Mal in denselben Fehler.
    ap.add_argument("--no-ollama", action="store_true",
                    help="Nicht einbetten, auch wenn Vektoren vorhanden sind. "
                         "Rankt rein lexikalisch.")
    ap.add_argument("--transport", choices=["http", "stdio"], default="http",
                    help="http: one shared server, register its URL in Claude "
                         "(default). stdio: launched per client via command.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="HTTP bind address. Keep 127.0.0.1 – the server has no "
                         "auth and serves your mail/chat history.")
    ap.add_argument("--port", type=int, default=settings.value("mcp_port"))
    # Für den Aufruf von Hand: wer das Programm selbst startet, hat den
    # Schalter nicht vor sich und soll nicht rätseln müssen, warum nichts geht.
    ap.add_argument("--force", action="store_true",
                    help="Serve even when MCP access is switched off in the app.")
    ap.add_argument("--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
                    help="Hostname clients may use in the Host/Origin header. "
                         "Required when --host is not the loopback interface; "
                         "repeat for several. Port defaults to --port.")
    a = ap.parse_args()
    # --store/--teams/--outlook gab es bis 5.0.0 einzeln; wer sie in einer alten
    # Claude-Konfiguration stehen hat, soll nicht ins Leere laufen.
    basis = Path(a.data_dir).expanduser() if a.data_dir else Path(".")
    a.store = a.store or str(basis / settings.STORE_DIR)
    a.teams = a.teams or str(basis / settings.TEAMS_DIR)
    a.outlook = a.outlook or str(basis / settings.OUTLOOK_DIR)

    # Der harte Schalter. Er sitzt hier und nicht in den Werkzeugen, weil die
    # App dieselben Funktionen für ihre eigene Suche im selben Prozess aufruft –
    # abgeschaltet gehört der SERVER, nicht das Lesen des Index. Und er sitzt
    # vor beiden Transporten: über stdio startet der Client dieses Programm
    # selbst, ohne dass die App überhaupt läuft. Ein Schalter, der nur den
    # HTTP-Endpunkt anhielte, wäre genau das Versprechen, das er nicht hält.
    #
    # Sich zu beenden wäre das Naheliegende und die schlechtere Antwort: der
    # Client sähe nur einen Server, der nicht startet, und der Grund stünde in
    # einer Logdatei. Stattdessen läuft ein Server, der genau eine Auskunft
    # gibt – die liest das Sprachmodell und sagt sie dem Menschen im Klartext.
    # Ausgeliefert wird dabei nichts: kein Werkzeug, das Daten liest, und der
    # Index wird nicht einmal geöffnet.
    if not a.force and not settings.flag("MCP_ENABLED", "mcp_enabled"):
        print(AUS_TEXT, file=sys.stderr)
        server = _abgeschaltet_server()
        if a.transport == "http":
            server.run(transport="streamable-http", host=a.host, port=a.port,
                       streamable_http_path=_HTTP_PATH,
                       transport_security=_transport_security(
                           a.host, a.port, a.allowed_host))
        else:
            server.run(transport="stdio")
        return

    dbp = store_layout.db_path(a.store)
    if not dbp.exists():
        raise SystemExit(f"No store at '{dbp}'. Build it first:\n"
                         f"  python3 rag_index.py {a.teams} {a.outlook}")

    con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()

    np, V = (None, None) if a.no_ollama else _open_vectors(a.store, n_chunks)
    if a.no_ollama:
        print("Ollama abgeschaltet – es wird rein lexikalisch gerankt.",
              file=sys.stderr)
    STATE.update(db=str(dbp), V=V, np=np, semantic=(np is not None),
                 vector_dtype=str(V.dtype) if V is not None else None,
                 teams_dir=a.teams, outlook_dir=a.outlook,
                 onedrive_dir=a.onedrive or str(
                     Path(a.teams).parent / settings.ONEDRIVE_DIR),
                 sharepoint_dir=a.sharepoint or str(
                     Path(a.teams).parent / settings.SHAREPOINT_DIR),
                 embed_model=a.embed_model, ollama=a.ollama)

    backend = ("hybrid (BM25 + semantic, RRF)" if np is not None
               else "lexical (FTS5/BM25) only")
    print(f"munimentum MCP: {n_chunks} chunks · {backend}", file=sys.stderr)
    if a.transport == "http":
        security = _transport_security(a.host, a.port, a.allowed_host)
        # No endpoint echo here – the app already logs "MCP server started
        # on port N", and the settings show the exact snippet to paste.
        if security is not None:
            print(f"Host/Origin restricted to: {', '.join(security.allowed_hosts)}",
                  file=sys.stderr)
        mcp.run(transport="streamable-http", host=a.host, port=a.port,
                streamable_http_path=_HTTP_PATH, transport_security=security)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
