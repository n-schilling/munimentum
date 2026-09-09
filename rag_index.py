#!/usr/bin/env python3
"""
rag_index.py – builds the index for local RAG search and the MCP server.

Reads both exports (via corpus.py), embeds every chunk through Ollama and puts
everything into a store folder:

    corpus.db     SQLite: chunks + metadata, FTS5 full-text index (BM25),
                  precomputed people list. Queried directly by mcp_server.py
                  – no need to load anything into RAM.
    vectors-N.npy Embedding matrix, float16 (half the space, practically the
                  same cosine ranking). Row i belongs to chunks.id = i+1.
                  Every run writes a NEW file instead of replacing the
                  existing one – otherwise it would fail on Windows while a
                  reader keeps it mapped (see store_layout.py).
    info.json     Model/dimension/format – and which vector file is current.

Incremental: a repeated run recomputes only new/changed chunks (matched via
content hash); existing vectors are reused.

Runs as a subprogram of app.py, which passes every folder explicitly:

    teams outlook onedrive --sharepoint DIR --pages DIR --planner DIR
    --store DIR [--model M] [--ollama URL] [--batch N] [--no-embeddings]

Only model, ollama and batch still fall back to the schema in settings.py.
Embeddings need a running Ollama; --no-embeddings builds only
corpus.db with the FTS5 full-text index – search and the MCP server then
work lexically (BM25), only the semantic half of hybrid ranking is missing.
"""

import json
import time
import sqlite3
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

import analytics_db
import corpus
import export_util
import ollama_client
import progress
import settings
import store_layout

export_util.erzwinge_utf8()

# From the schema, not repeated here: the tests compare against it.
DEFAULT_MODEL = settings.VORGABEN["embed_model"]
DEFAULT_OLLAMA = settings.VORGABEN["ollama"]
FORMAT = 2                     # 2 = corpus.db + float16 vectors
PPL_TOKEN_CAP = 60             # people tokens per person in the people table
STALE_VECTORS = "vectors_stale.npz"   # set-aside embeddings, hash-indexed

# Minimum length before a chunk gets embedded at all.
#
# Measured on a real archive: 22 % of all chunks are shorter than this —
# "ok", "thanks", "see you tomorrow" — and together they cost a quarter of an
# hour per run. They carry no meaning anyone would search for: what they
# contain appears a hundred times in almost every chat and answers no question.
#
# They stay fully in the index and findable via text search; only their
# vector stays zero. That is no special case in the search code: cosine 0
# lies below any sensible floor (SEMANTIC_MIN, default 0.45), so such rows
# can never surface as hits.
MIN_EMBED_ZEICHEN = 40


def embed(texts, model, url, timeout=600):
    """A batch of texts -> vectors; errors end here with a clear message.

    The HTTP part lives in ollama_client; this script only translates into
    aborts, because nobody is around to read a traceback mid index run.
    """
    import requests
    try:
        return ollama_client.embed(texts, model, url, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise SystemExit(f"Keine Verbindung zu Ollama unter {url}. "
                         f"Läuft 'ollama serve'?") from None
    except ollama_client.ModellFehlt:
        raise SystemExit(f"Modell '{model}' nicht gefunden. "
                         f"Vorher: ollama pull {model}") from None
    except RuntimeError as e:
        raise SystemExit(str(e)) from None


# --------------------------------------------------------------------------
# Writing the SQLite store
# --------------------------------------------------------------------------
def _chunk_row(i, c):
    seq = int(c["cid"].rsplit("#", 1)[1])
    try:
        msg_idx = int(c["uid"].rsplit(":", 1)[1])
    except ValueError:
        msg_idx = 0
    return (i + 1, c["uid"], seq, msg_idx, c["src"], c["root"], c["rel"],
            c.get("who"), c.get("ppl"), c.get("ts"), c.get("date"),
            c.get("title"), c.get("ctx"), c.get("text"), c.get("hash"),
            c.get("thread"), c.get("gone"), c.get("att"),
            # From the same names as att, but as its own column: SQL filters
            # on it, and that has to work in all three search modes.
            corpus.endungen(c.get("att")) or None)


def _people_rows(chunks):
    """(src, who) → message count + people tokens for the contains search."""
    agg = {}
    for c in chunks:
        if not c["cid"].endswith("#0"):           # count each message only once
            continue
        key = (c["src"], (c.get("who") or "").strip())
        cnt, toks = agg.setdefault(key, [0, set()])
        agg[key][0] = cnt + 1
        if len(toks) < PPL_TOKEN_CAP:
            toks.update((c.get("ppl") or "").split()[:PPL_TOKEN_CAP])
    return [(src, who, cnt, " ".join(sorted(toks)))
            for (src, who), (cnt, toks) in agg.items()]


def write_db(store, chunks):
    """Rewrite corpus.db atomically (first .tmp, then replace)."""
    dbp = store_layout.db_path(store)
    tmp = dbp.with_name(dbp.name + ".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE chunks(
            id      INTEGER PRIMARY KEY,   -- Vektorzeile = id - 1
            uid     TEXT NOT NULL,         -- Nachricht (mehrere Chunks möglich)
            seq     INTEGER NOT NULL,      -- Chunk-Nr. innerhalb der Nachricht
            msg_idx INTEGER NOT NULL,      -- Nachrichten-Nr. innerhalb der Datei
            src     TEXT NOT NULL, root TEXT NOT NULL, rel TEXT NOT NULL,
            who TEXT, ppl TEXT, ts REAL, date TEXT,
            title TEXT, ctx TEXT, text TEXT, hash TEXT,
            thread TEXT,                  -- Gesprächskennung, siehe corpus.thread_key
            gone TEXT,                    -- seit wann nicht mehr im Postfach
            att TEXT,                     -- Namen der Anhänge, siehe corpus.anhaenge
            ext TEXT);                    -- deren Dateitypen, siehe corpus.endungen
        CREATE INDEX ix_chunks_uid ON chunks(uid);
        -- „Verlauf anzeigen“ holt alle Nachrichten eines Gesprächs. Ohne den
        -- Index wäre das ein voller Scan über alle Chunks.
        CREATE INDEX ix_chunks_thread ON chunks(thread) WHERE seq = 0;
        -- „Nur Gelöschtes“ ist ein schmaler Ausschnitt – ein Teilindex reicht
        -- und kostet fast nichts, weil die allermeisten Zeilen NULL sind.
        CREATE INDEX ix_chunks_gone ON chunks(gone) WHERE gone IS NOT NULL;
        -- Der Ordner als Suchkriterium: ohne Index wäre jede Einschränkung
        -- ein voller Scan über alle Chunks.
        CREATE INDEX ix_chunks_ctx ON chunks(ctx);
        -- Der Dateityp als Filter trifft nur Nachrichten mit Anhang und
        -- Dateien – ein Teilindex ist damit schmal und spart den vollen Scan.
        CREATE INDEX ix_chunks_ext ON chunks(ext) WHERE ext IS NOT NULL;
        CREATE INDEX ix_chunks_src_ts ON chunks(src, ts);
        CREATE INDEX ix_chunks_file ON chunks(root, rel, msg_idx);
        -- browse_messages listet Nachrichten (seq = 0) nach Datum. Ohne diesen
        -- Teilindex scannt SQLite alle Chunks und sortiert sie temporär: 48 ms
        -- statt 0,06 ms bei 270k Chunks. Kostet ~1 MB.
        CREATE INDEX ix_chunks_msg_ts ON chunks(ts DESC) WHERE seq = 0;
        CREATE TABLE people(src TEXT, who TEXT, messages INTEGER, ppl TEXT);
        CREATE INDEX ix_people_who ON people(who);
        -- Anhangnamen als eigene Spalte statt angehängt an den Text: sonst
        -- stünden sie in jeder Vorschau und im Kontext der KI-Antwort.
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            title, text, att, content='chunks', content_rowid='id');
    """)
    con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_chunk_row(i, c) for i, c in enumerate(chunks)))
    con.executemany("INSERT INTO people VALUES (?,?,?,?)", _people_rows(chunks))
    con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    con.commit()
    con.close()
    tmp.replace(dbp)


def save_vectors(store, V):
    """Store normalised as float16 (half the space, ranking ~identical).

    Written under a NEW name, never over the existing file – see store_layout.
    Returns (matrix, path); the name must then go into info.json, or nobody
    will find it.
    """
    V = V.astype("float32")
    norms = np.linalg.norm(V, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    V = (V / norms).astype("float16")
    ziel = store_layout.next_vectors_path(store)
    tmp = ziel.with_suffix(".npy.tmp")
    with open(tmp, "wb") as f:                 # file object: np.save appends no .npy
        np.save(f, V)
    # Via a temp file here too, even though the target is new: an abort in
    # the middle of writing then leaves no half-written matrix under a name
    # the next run might take for valid.
    tmp.replace(ziel)
    return V, ziel


def write_info(store, model, dim, n, vectors=None):
    """The closing act of a run: only this makes the new state count.

    `vectors` is the filename of the embeddings – None explicitly means "this
    index has none". The entry is always present, even when empty; store_layout
    uses it to tell a run without embeddings from an old store.
    """
    (Path(store) / "info.json").write_text(json.dumps({
        "model": model, "dim": int(dim), "chunks": int(n),
        "dtype": "float16", "format": FORMAT,
        "vectors": Path(vectors).name if vectors else None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Reading the old store (for incremental runs)
# --------------------------------------------------------------------------
def _load_old_store(store):
    """(hashes_in_order, V) of the existing store."""
    sp = Path(store)
    vp = store_layout.vectors_path(sp)
    # No mmap: the vectors are needed in full right away, and a read handle
    # on the old file would be exactly what blocks the cleanup at the end of
    # the run.
    V = np.load(vp) if vp else None
    dbp = store_layout.db_path(sp)
    if dbp.exists():
        con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        hashes = [r[0] for r in con.execute("SELECT hash FROM chunks ORDER BY id")]
        con.close()
        return hashes, V
    return [], V


def _load_stale(store):
    """Set-aside embeddings from a lexical run (hash -> vector)."""
    p = Path(store) / STALE_VECTORS
    if not p.exists():
        return {}
    try:
        with np.load(p) as z:
            V, hashes = z["V"], z["hashes"].tolist()
        return {h: V[i] for i, h in enumerate(hashes) if h and i < len(V)}
    except Exception:
        progress.event("run.index.stale_unreadable", "warn")
        return {}


def load_old_vectors(store):
    out = _load_stale(store)          # fallback, overruled by the vectors
    try:
        hashes, V = _load_old_store(store)
        if V is None or not hashes:
            return out
        out.update({h: V[i] for i, h in enumerate(hashes) if h and i < len(V)})
        return out
    except Exception:
        progress.event("run.index.rebuild", "warn")
        return out


def retire_vectors(store):
    """Before a lexical rebuild: save embeddings hash-indexed and retire the
    vector file. Returns the number of rescued vectors.

    Needed because the vectors hang off corpus.db row by row (row i belongs
    to id i+1). Once the DB is rewritten without embeddings, that mapping no
    longer holds – simply leaving the file in place would mean ranking wrong
    vectors later. Keyed by content hash they stay valid, and a later run
    with Ollama only has to embed what is genuinely new instead of everything.

    "Retire" means: the entry in info.json goes away (write_info does that
    right after), and the file is deleted as far as it lets itself be
    deleted. Both together – valid is what info.json names, not what happens
    to sit in the folder.
    """
    sp = Path(store)
    if not store_layout.vectors_path(sp):
        return 0
    keep = load_old_vectors(store)
    if keep:
        items = sorted(keep.items())
        tmp = sp / (STALE_VECTORS + ".tmp")
        with open(tmp, "wb") as f:
            np.savez(f, hashes=np.array([h for h, _ in items]),
                     V=np.vstack([v for _, v in items]).astype("float16"))
        tmp.replace(sp / STALE_VECTORS)
    store_layout.prune_vectors(sp)
    return len(keep)


# --------------------------------------------------------------------------
# Building the index
# --------------------------------------------------------------------------
def build_index(teams_dir, outlook_dir, store, model, url, batch=128,
                embeddings=True, onedrive_dir=None, sharepoint_dir=None,
                pages_dir=None, planner_dir=None):
    # Reading a large archive takes a minute or more and used to be silent –
    # long enough for someone watching the log to suspect a hang.
    progress.event("run.index.reading")
    begonnen = time.time()
    recs = corpus.load_records(teams_dir, outlook_dir, onedrive_dir,
                               sharepoint_dir, pages_dir, planner_dir)
    if corpus.POOL_FEHLER:
        # Don't keep quiet about this: the index is correct, but reading ran
        # on one core instead of all, and with large archives that shows.
        progress.event("run.index.no_pool", "warn",
                       error=str(corpus.POOL_FEHLER))
    chunks = corpus.chunk_records(recs)
    if not chunks:
        raise SystemExit("No content found - are the export folders right?")
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)
    progress.event("run.index.read", n=len(recs), chunks=len(chunks),
                   s=int(time.time() - begonnen))

    Path(store).mkdir(parents=True, exist_ok=True)
    if not embeddings:
        # Only corpus.db + FTS5: full-text search (BM25) runs without Ollama,
        # the semantic half of hybrid search is missing. write_info below
        # explicitly records no vector file; mcp_server.py and app.py then
        # rank purely lexically.
        saved = retire_vectors(store)
        if saved:
            progress.event("run.index.saved_stale", n=saved)
        write_db(store, chunks)
        write_info(store, None, 0, len(chunks))
        return len(chunks), 0, 0

    old = load_old_vectors(store)
    vectors = [None] * len(chunks)
    dim = len(next(iter(old.values()))) if old else None
    # Embed each unique content hash only ONCE and spread the result over all
    # equal chunks (identical signatures/disclaimers are common).
    uniq = {}                       # hash -> list of chunk indices with this hash
    for i, c in enumerate(chunks):
        v = old.get(c["hash"])
        if v is not None:
            vectors[i] = np.asarray(v, dtype="float32")
        else:
            uniq.setdefault(c["hash"], []).append(i)

    todo_groups = list(uniq.values())          # per unique text: all target indices
    new_total = sum(len(g) for g in todo_groups)
    # Too short to carry meaning: vector stays zero (see MIN_EMBED_ZEICHEN).
    zu_kurz = sum(len(g) for g in todo_groups
                  if len(chunks[g[0]].get("text") or "") < MIN_EMBED_ZEICHEN)
    todo_groups = [g for g in todo_groups
                   if len(chunks[g[0]].get("text") or "") >= MIN_EMBED_ZEICHEN]
    # Sorted by length: a batch gets padded up to its longest sequence, and
    # mixed lengths pay for that padding on every single item.
    todo_groups.sort(key=lambda g: len(chunks[g[0]].get("text") or ""))
    todo_texts = [corpus.embed_text(chunks[idxs[0]]) for idxs in todo_groups]
    progress.event("run.index.plan", chunks=len(chunks),
                   reused=len(chunks) - new_total, new=new_total,
                   short=zu_kurz)

    if todo_texts:
        done = 0
        # Embedding is GPU-bound and serialised on one slot; with two requests
        # in flight one is always already waiting in the server queue, so the
        # GPU never sits idle between batches (no idle bubble).
        def run(b):
            texts = todo_texts[b:b + batch]
            return b, embed(texts, model, url)
        starts = range(0, len(todo_texts), batch)
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {ex.submit(run, b): b for b in starts}
            for fut in as_completed(futs):
                b, vecs = fut.result()
                for k, vec in enumerate(vecs):
                    arr = np.asarray(vec, dtype="float32")
                    for i in todo_groups[b + k]:
                        vectors[i] = arr
                done += len(vecs)
                dim = dim or len(vecs[0])
                progress.melde(done, len(todo_texts), "embeddings")

    if dim is None:
        # Neither old stock nor anything new: only too-short texts. Ask for
        # the dimension once so the matrix still gets the right shape.
        dim = len(embed([corpus.embed_text(chunks[0])], model, url)[0])
    leer = np.zeros(dim, dtype="float32")
    vectors = [leer if v is None else v for v in vectors]

    V, vp = save_vectors(store, np.vstack(vectors))
    write_db(store, chunks)
    write_info(store, model, V.shape[1], len(chunks), vp)
    # Everything is back in the vector file – the hash backup is no longer
    # needed.
    (Path(store) / STALE_VECTORS).unlink(missing_ok=True)
    # Only now, after info.json points to the new file, may the previous one
    # go. Whoever still has it mapped keeps it – then it stays behind and the
    # next run sweeps it up (see store_layout.prune_vectors).
    store_layout.prune_vectors(store, vp)
    return len(chunks), new_total, int(V.shape[1])


def main():
    ap = argparse.ArgumentParser()
    # No defaults of their own: the app passes every folder (steps.py), and a
    # fallback would quietly index whatever sits next to the working
    # directory – the same reason the export scripts lost theirs.
    ap.add_argument("teams")
    ap.add_argument("outlook")
    ap.add_argument("onedrive")
    ap.add_argument("--sharepoint", required=True)
    ap.add_argument("--pages", required=True)
    ap.add_argument("--planner", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--model", default=settings.value("embed_model"))
    ap.add_argument("--ollama", default=settings.value("ollama"))
    ap.add_argument("--batch", type=int, default=settings.value("index_batch"))
    ap.add_argument("--no-embeddings", action="store_true",
                    help="Build only the full-text index (FTS5/BM25), "
                         "without Ollama. Search and MCP then run purely "
                         "lexically.")
    a = ap.parse_args()

    n, new, dim = build_index(a.teams, a.outlook, a.store, a.model, a.ollama,
                              a.batch, embeddings=not a.no_embeddings,
                              onedrive_dir=a.onedrive,
                              sharepoint_dir=a.sharepoint, pages_dir=a.pages,
                              planner_dir=a.planner)
    # The Analytics tab reads a materialised block instead of aggregating on
    # every visit – this run just touched everything, so build it now. Its
    # failure must not fail the index.
    try:
        analytics_db.baue(a.store, {
            "teams": a.teams, "outlook": a.outlook, "onedrive": a.onedrive,
            "sharepoint": a.sharepoint, "pages": a.pages,
            "planner": a.planner})
    except Exception as e:
        progress.event("run.index.analytics_failed", "warn",
                       error=f"{type(e).__name__}: {e}")
    # Same result schema as every other subprogram: "new" = freshly computed
    # embeddings, "unchanged" = reused ones; the chunk total goes into extra.
    progress.ergebnis(new, unchanged=max(0, n - new), extra={"chunks": n})


if __name__ == "__main__":
    main()
