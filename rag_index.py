#!/usr/bin/env python3
"""
rag_index.py – baut den Index für die lokale RAG-Suche und den MCP-Server.

Liest beide Exporte (über corpus.py), bettet jeden Chunk per Ollama ein und legt
alles in einem Store-Ordner ab:

    corpus.db     SQLite: Chunks + Metadaten, FTS5-Volltextindex (BM25),
                  vorberechnete Personenliste. Wird von mcp_server.py und
                  rag_server.py abfragbar genutzt – kein Laden in den RAM nötig.
    vectors.npy   Embedding-Matrix, float16 (halber Platz, praktisch gleiche
                  Kosinus-Rangfolge). Zeile i gehört zu chunks.id = i+1.
    info.json     Modell/Dimension/Format.

Inkrementell: bei erneutem Lauf werden nur neue/geänderte Chunks neu berechnet
(Abgleich über Inhalts-Hash), vorhandene Vektoren werden wiederverwendet.

    ollama serve                 # Ollama muss laufen
    ollama pull bge-m3           # mehrsprachiges Embedding-Modell (DE/EN)
    pip3 install numpy requests
    python3 rag_index.py [teams_export] [outlook_export] [--store rag_store]

Ohne Ollama: --no-embeddings baut nur corpus.db mit dem FTS5-Volltextindex.
Suche und MCP-Server funktionieren dann rein lexikalisch (BM25), nur die
semantische Hälfte der Hybrid-Suche fehlt.

Optionen: --model bge-m3  --ollama http://localhost:11434  --batch 64
"""

import sys
import json
import sqlite3
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

import corpus
import progress
import settings

# Auf Windows nutzt die Konsole standardmäßig eine Legacy-Codepage (z. B. cp1252),
# und bei Umleitung in eine Datei die Locale-Kodierung. Beides lässt print() an
# Unicode-Zeichen wie → oder … mit UnicodeEncodeError scheitern. UTF-8 erzwingen
# (auf macOS/Linux ein No-op).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEFAULT_MODEL = "bge-m3"
DEFAULT_OLLAMA = "http://localhost:11434"
FORMAT = 2                     # 2 = corpus.db + float16-Vektoren
PPL_TOKEN_CAP = 60             # Personen-Tokens pro Person in der people-Tabelle
STALE_VECTORS = "vectors_stale.npz"   # beiseitegelegte Embeddings, hash-indiziert


def embed(texts, model, url, timeout=600):
    import requests
    try:
        r = requests.post(f"{url}/api/embed",
                          json={"model": model, "input": texts}, timeout=timeout)
    except requests.exceptions.ConnectionError:
        raise SystemExit(f"Keine Verbindung zu Ollama unter {url}. "
                         f"Läuft 'ollama serve'?") from None
    if r.status_code == 404:
        raise SystemExit(f"Modell '{model}' nicht gefunden. Vorher: ollama pull {model}")
    r.raise_for_status()
    data = r.json()
    embs = data.get("embeddings")
    if embs is None and "embedding" in data:      # ältere Single-Form
        embs = [data["embedding"]]
    if not embs:
        raise SystemExit(f"Unerwartete Embedding-Antwort: {str(data)[:200]}")
    return embs


# --------------------------------------------------------------------------
# SQLite-Store schreiben
# --------------------------------------------------------------------------
def _chunk_row(i, c):
    seq = int(c["cid"].rsplit("#", 1)[1])
    try:
        msg_idx = int(c["uid"].rsplit(":", 1)[1])
    except ValueError:
        msg_idx = 0
    return (i + 1, c["uid"], seq, msg_idx, c["src"], c["root"], c["rel"],
            c.get("who"), c.get("ppl"), c.get("ts"), c.get("date"),
            c.get("title"), c.get("ctx"), c.get("text"), c.get("hash"))


def _people_rows(chunks):
    """(src, who) → Nachrichtenzahl + Personen-Token für die contains-Suche."""
    agg = {}
    for c in chunks:
        if not c["cid"].endswith("#0"):           # eine Nachricht nur einmal zählen
            continue
        key = (c["src"], (c.get("who") or "").strip())
        cnt, toks = agg.setdefault(key, [0, set()])
        agg[key][0] = cnt + 1
        if len(toks) < PPL_TOKEN_CAP:
            toks.update((c.get("ppl") or "").split()[:PPL_TOKEN_CAP])
    return [(src, who, cnt, " ".join(sorted(toks)))
            for (src, who), (cnt, toks) in agg.items()]


def write_db(store, chunks):
    """corpus.db atomisch neu schreiben (erst .tmp, dann ersetzen)."""
    dbp = Path(store) / "corpus.db"
    tmp = dbp.with_name("corpus.db.tmp")
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
            title TEXT, ctx TEXT, text TEXT, hash TEXT);
        CREATE INDEX ix_chunks_uid ON chunks(uid);
        CREATE INDEX ix_chunks_src_ts ON chunks(src, ts);
        CREATE INDEX ix_chunks_file ON chunks(root, rel, msg_idx);
        -- browse_messages listet Nachrichten (seq = 0) nach Datum. Ohne diesen
        -- Teilindex scannt SQLite alle Chunks und sortiert sie temporär: 48 ms
        -- statt 0,06 ms bei 270k Chunks. Kostet ~1 MB.
        CREATE INDEX ix_chunks_msg_ts ON chunks(ts DESC) WHERE seq = 0;
        CREATE TABLE people(src TEXT, who TEXT, messages INTEGER, ppl TEXT);
        CREATE INDEX ix_people_who ON people(who);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            title, text, content='chunks', content_rowid='id');
    """)
    con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_chunk_row(i, c) for i, c in enumerate(chunks)))
    con.executemany("INSERT INTO people VALUES (?,?,?,?)", _people_rows(chunks))
    con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    con.commit()
    con.close()
    tmp.replace(dbp)


def save_vectors(store, V):
    """Normalisiert als float16 speichern (halber Platz, Rangfolge ~identisch)."""
    V = V.astype("float32")
    norms = np.linalg.norm(V, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    V = (V / norms).astype("float16")
    tmp = Path(store) / "vectors.npy.tmp"
    with open(tmp, "wb") as f:                 # Dateiobjekt: np.save hängt kein .npy an
        np.save(f, V)
    tmp.replace(Path(store) / "vectors.npy")
    return V


def write_info(store, model, dim, n):
    (Path(store) / "info.json").write_text(json.dumps({
        "model": model, "dim": int(dim), "chunks": int(n),
        "dtype": "float16", "format": FORMAT,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Alten Store lesen (für inkrementelle Läufe)
# --------------------------------------------------------------------------
def _load_old_store(store):
    """(hashes_in_order, V) des vorhandenen Stores."""
    sp = Path(store)
    vp = sp / "vectors.npy"
    V = np.load(vp) if vp.exists() else None
    dbp = sp / "corpus.db"
    if dbp.exists():
        con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        hashes = [r[0] for r in con.execute("SELECT hash FROM chunks ORDER BY id")]
        con.close()
        return hashes, V
    return [], V


def _load_stale(store):
    """Beiseitegelegte Embeddings aus einem lexikalischen Lauf (hash -> Vektor)."""
    p = Path(store) / STALE_VECTORS
    if not p.exists():
        return {}
    try:
        with np.load(p) as z:
            V, hashes = z["V"], z["hashes"].tolist()
        return {h: V[i] for i, h in enumerate(hashes) if h and i < len(V)}
    except Exception:
        print(f"  {STALE_VECTORS} unlesbar – wird ignoriert.")
        return {}


def load_old_vectors(store):
    out = _load_stale(store)          # Fallback, von vectors.npy überstimmt
    try:
        hashes, V = _load_old_store(store)
        if V is None or not hashes:
            return out
        out.update({h: V[i] for i, h in enumerate(hashes) if h and i < len(V)})
        return out
    except Exception:
        print("  Alter Index unlesbar – baue komplett neu.")
        return out


def retire_vectors(store):
    """Vor einem lexikalischen Rebuild: Embeddings hash-indiziert sichern und
    vectors.npy entfernen. Liefert die Zahl der geretteten Vektoren.

    Nötig, weil vectors.npy zeilenweise an corpus.db hängt (Zeile i gehört zu
    id i+1). Wird die DB ohne Embeddings neu geschrieben, stimmt diese Zuordnung
    nicht mehr – die Datei einfach liegen zu lassen hieße, später falsche
    Vektoren zu ranken. Über den Inhalts-Hash bleiben sie dagegen gültig, und
    ein späterer Lauf mit Ollama muss nur wirklich Neues einbetten statt alles.
    """
    sp = Path(store)
    vp = sp / "vectors.npy"
    if not vp.exists():
        return 0
    keep = load_old_vectors(store)
    if keep:
        items = sorted(keep.items())
        tmp = sp / (STALE_VECTORS + ".tmp")
        with open(tmp, "wb") as f:
            np.savez(f, hashes=np.array([h for h, _ in items]),
                     V=np.vstack([v for _, v in items]).astype("float16"))
        tmp.replace(sp / STALE_VECTORS)
    vp.unlink()
    return len(keep)


# --------------------------------------------------------------------------
# Index bauen
# --------------------------------------------------------------------------
def build_index(teams_dir, outlook_dir, store, model, url, batch=64,
                embeddings=True):
    recs = corpus.load_records(teams_dir, outlook_dir)
    chunks = corpus.chunk_records(recs)
    if not chunks:
        raise SystemExit("Keine Inhalte gefunden – stimmen die Export-Ordner?")
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)

    Path(store).mkdir(parents=True, exist_ok=True)
    if not embeddings:
        # Nur corpus.db + FTS5: Volltextsuche (BM25) läuft ohne Ollama, die
        # semantische Hälfte der Hybrid-Suche fehlt. mcp_server.py und app.py
        # erkennen das fehlende vectors.npy und ranken rein lexikalisch.
        saved = retire_vectors(store)
        if saved:
            print(f"{saved} vorhandene Embeddings nach {STALE_VECTORS} gesichert "
                  f"– ein späterer Lauf mit Ollama nutzt sie wieder.")
        write_db(store, chunks)
        write_info(store, None, 0, len(chunks))
        return len(chunks), 0, 0

    old = load_old_vectors(store)
    vectors = [None] * len(chunks)
    # Pro eindeutigem Inhalts-Hash nur EINMAL einbetten und das Ergebnis auf alle
    # gleichen Chunks verteilen (identische Signaturen/Disclaimer kommen oft vor).
    uniq = {}                       # hash -> Liste der Chunk-Indizes mit diesem Hash
    for i, c in enumerate(chunks):
        v = old.get(c["hash"])
        if v is not None:
            vectors[i] = np.asarray(v, dtype="float32")
        else:
            uniq.setdefault(c["hash"], []).append(i)

    todo_groups = list(uniq.values())          # je eindeutiger Text: alle Zielindizes
    todo_texts = [corpus.embed_text(chunks[idxs[0]]) for idxs in todo_groups]
    new_total = sum(len(g) for g in todo_groups)
    print(f"{len(chunks)} Chunks: {len(chunks) - new_total} wiederverwendet, "
          f"{new_total} neu ({len(todo_texts)} eindeutig einzubetten).")

    if todo_texts:
        done = 0
        # Embedding ist GPU-gebunden und serialisiert auf einem Slot; mit zwei
        # Requests „in flight“ liegt immer schon einer in der Server-Queue, sodass
        # die GPU zwischen den Batches nicht leerläuft (kein Idle-Bubble).
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
                progress.melde(done, len(todo_texts), "embeddings")
                print(f"  … {done}/{len(todo_texts)} eingebettet", end="\r", flush=True)
        print()

    V = save_vectors(store, np.vstack(vectors))
    write_db(store, chunks)
    write_info(store, model, V.shape[1], len(chunks))
    # Alles wieder in vectors.npy – die Hash-Sicherung wird nicht mehr gebraucht.
    (Path(store) / STALE_VECTORS).unlink(missing_ok=True)
    return len(chunks), new_total, int(V.shape[1])


def main():
    ap = argparse.ArgumentParser()
    # Vorgaben aus app_config.json, sofern vorhanden – die Kommandozeile sticht
    # sie aus (siehe settings.py).
    ap.add_argument("teams", nargs="?", default=settings.value("teams_dir", "teams_export"))
    ap.add_argument("outlook", nargs="?", default=settings.value("outlook_dir", "outlook_export"))
    ap.add_argument("--store", default=settings.value("store_dir", "rag_store"))
    ap.add_argument("--model", default=settings.value("embed_model", DEFAULT_MODEL))
    ap.add_argument("--ollama", default=settings.value("ollama", DEFAULT_OLLAMA))
    ap.add_argument("--batch", type=int, default=settings.value("index_batch", 64))
    ap.add_argument("--no-embeddings", action="store_true",
                    help="Nur den Volltextindex (FTS5/BM25) bauen, ohne Ollama. "
                         "Suche und MCP laufen dann rein lexikalisch.")
    a = ap.parse_args()
    hinweis = settings.report()
    if hinweis:
        print(hinweis)

    if a.no_embeddings:
        print(f"Index → {a.store}  (nur Volltext, ohne Embeddings)")
    else:
        print(f"Index → {a.store}  (Modell {a.model})")
    n, new, dim = build_index(a.teams, a.outlook, a.store, a.model, a.ollama,
                              a.batch, embeddings=not a.no_embeddings)
    if a.no_embeddings:
        print(f"\nFertig. {n} Chunks im Volltextindex – keine Embeddings.")
        print("Für die semantische/hybride Suche später ohne --no-embeddings "
              "erneut laufen lassen (Ollama nötig).")
    else:
        print(f"\nFertig. {n} Chunks im Index ({dim} Dimensionen), davon {new} neu berechnet.")
    print(f"Jetzt: python3 rag_server.py --store {a.store}  oder  mcp_server.py")


if __name__ == "__main__":
    main()
