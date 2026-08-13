#!/usr/bin/env python3
"""
rag_index.py – baut den Index für die lokale RAG-Suche und den MCP-Server.

Liest beide Exporte (über corpus.py), bettet jeden Chunk per Ollama ein und legt
alles in einem Store-Ordner ab:

    corpus.db     SQLite: Chunks + Metadaten, FTS5-Volltextindex (BM25),
                  vorberechnete Personenliste. Wird von mcp_server.py
                  abfragbar genutzt – kein Laden in den RAM nötig.
    vectors-N.npy Embedding-Matrix, float16 (halber Platz, praktisch gleiche
                  Kosinus-Rangfolge). Zeile i gehört zu chunks.id = i+1.
                  Jeder Lauf schreibt eine NEUE Datei, statt die vorhandene
                  zu ersetzen – sonst scheiterte er unter Windows, solange
                  ein Leser sie abgebildet hält (siehe store_layout.py).
    info.json     Modell/Dimension/Format – und welche Vektordatei gilt.

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
import store_layout

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

# Ab welcher Länge ein Chunk überhaupt eingebettet wird.
#
# Gemessen an einem echten Archiv: 22 % aller Chunks sind kürzer als das —
# „ok", „danke", „bis morgen" — und kosten zusammen eine Viertelstunde je Lauf.
# Eine Bedeutung, nach der jemand sucht, tragen sie nicht: Was sie enthalten,
# steht in fast jedem Chat hundertfach und beantwortet keine Frage.
#
# Sie bleiben vollständig im Index und über die Textsuche auffindbar; nur ihr
# Vektor bleibt null. Das ist kein Sonderfall im Suchcode: Kosinus 0 liegt
# unter jeder sinnvollen Untergrenze (SEMANTIC_MIN, Vorgabe 0,45), solche
# Zeilen können also gar nicht als Treffer erscheinen.
MIN_EMBED_ZEICHEN = 40


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
            c.get("title"), c.get("ctx"), c.get("text"), c.get("hash"),
            c.get("thread"), c.get("gone"), c.get("att"),
            # Aus denselben Namen wie att, aber als eigene Spalte: danach wird
            # in SQL gefiltert, und das muss in allen drei Sucharten wirken.
            corpus.endungen(c.get("att")) or None)


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
    """Normalisiert als float16 speichern (halber Platz, Rangfolge ~identisch).

    Geschrieben wird unter einem NEUEN Namen, nie über die vorhandene Datei –
    siehe store_layout. Liefert (Matrix, Pfad); der Name gehört anschließend in
    info.json, sonst findet ihn niemand.
    """
    V = V.astype("float32")
    norms = np.linalg.norm(V, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    V = (V / norms).astype("float16")
    ziel = store_layout.next_vectors_path(store)
    tmp = ziel.with_suffix(".npy.tmp")
    with open(tmp, "wb") as f:                 # Dateiobjekt: np.save hängt kein .npy an
        np.save(f, V)
    # Auch hier über eine Zwischendatei, obwohl das Ziel neu ist: ein Abbruch
    # mitten im Schreiben hinterlässt so keine halbe Matrix unter einem Namen,
    # den der nächste Lauf für gültig halten könnte.
    tmp.replace(ziel)
    return V, ziel


def write_info(store, model, dim, n, vectors=None):
    """Der Schlusspunkt eines Laufs: erst hiermit gilt der neue Stand.

    `vectors` ist der Dateiname der Embeddings – None heißt ausdrücklich „dieser
    Index hat keine". Der Eintrag steht immer da, auch leer; store_layout
    unterscheidet daran einen Lauf ohne Embeddings von einem alten Store.
    """
    (Path(store) / "info.json").write_text(json.dumps({
        "model": model, "dim": int(dim), "chunks": int(n),
        "dtype": "float16", "format": FORMAT,
        "vectors": Path(vectors).name if vectors else None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Alten Store lesen (für inkrementelle Läufe)
# --------------------------------------------------------------------------
def _load_old_store(store):
    """(hashes_in_order, V) des vorhandenen Stores."""
    sp = Path(store)
    vp = store_layout.vectors_path(sp)
    # Ohne mmap: die Vektoren werden hier gleich vollständig gebraucht, und ein
    # Lesehandle auf die alte Datei wäre genau das, was das Aufräumen am Ende
    # des Laufs blockiert.
    V = np.load(vp) if vp else None
    dbp = store_layout.db_path(sp)
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
    out = _load_stale(store)          # Fallback, von den Vektoren überstimmt
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
    die Vektordatei zurückziehen. Liefert die Zahl der geretteten Vektoren.

    Nötig, weil die Vektoren zeilenweise an corpus.db hängen (Zeile i gehört zu
    id i+1). Wird die DB ohne Embeddings neu geschrieben, stimmt diese Zuordnung
    nicht mehr – die Datei einfach liegen zu lassen hieße, später falsche
    Vektoren zu ranken. Über den Inhalts-Hash bleiben sie dagegen gültig, und
    ein späterer Lauf mit Ollama muss nur wirklich Neues einbetten statt alles.

    „Zurückziehen" heißt: der Eintrag in info.json fällt weg (das erledigt
    write_info im Anschluss), und die Datei wird gelöscht, soweit sie sich
    löschen lässt. Beides zusammen – gültig ist, was info.json nennt, nicht was
    im Ordner liegt.
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
# Index bauen
# --------------------------------------------------------------------------
def build_index(teams_dir, outlook_dir, store, model, url, batch=128,
                embeddings=True, onedrive_dir=None):
    recs = corpus.load_records(teams_dir, outlook_dir, onedrive_dir)
    if corpus.POOL_FEHLER:
        # Nicht verschweigen: der Index stimmt, aber das Einlesen lief auf
        # einem Kern statt auf allen, und bei großen Beständen merkt man das.
        print(f"Hinweis: Einlesen ohne Prozess-Pool, nur ein Kern "
              f"({corpus.POOL_FEHLER}).")
    chunks = corpus.chunk_records(recs)
    if not chunks:
        raise SystemExit("Keine Inhalte gefunden – stimmen die Export-Ordner?")
    for c in chunks:
        c["hash"] = corpus.chunk_hash(c)

    Path(store).mkdir(parents=True, exist_ok=True)
    if not embeddings:
        # Nur corpus.db + FTS5: Volltextsuche (BM25) läuft ohne Ollama, die
        # semantische Hälfte der Hybrid-Suche fehlt. write_info trägt unten
        # ausdrücklich keine Vektordatei ein; mcp_server.py und app.py ranken
        # daraufhin rein lexikalisch.
        saved = retire_vectors(store)
        if saved:
            print(f"{saved} vorhandene Embeddings nach {STALE_VECTORS} gesichert "
                  f"– ein späterer Lauf mit Ollama nutzt sie wieder.")
        write_db(store, chunks)
        write_info(store, None, 0, len(chunks))
        return len(chunks), 0, 0

    old = load_old_vectors(store)
    vectors = [None] * len(chunks)
    dim = len(next(iter(old.values()))) if old else None
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
    new_total = sum(len(g) for g in todo_groups)
    # Zu kurz für eine Bedeutung: Vektor bleibt null (siehe MIN_EMBED_ZEICHEN).
    zu_kurz = sum(len(g) for g in todo_groups
                  if len(chunks[g[0]].get("text") or "") < MIN_EMBED_ZEICHEN)
    todo_groups = [g for g in todo_groups
                   if len(chunks[g[0]].get("text") or "") >= MIN_EMBED_ZEICHEN]
    # Nach Länge sortiert: ein Stapel wird auf seine längste Sequenz aufgefüllt,
    # und gemischte Längen zahlen diese Füllung bei jedem Stück mit.
    todo_groups.sort(key=lambda g: len(chunks[g[0]].get("text") or ""))
    todo_texts = [corpus.embed_text(chunks[idxs[0]]) for idxs in todo_groups]
    print(f"{len(chunks)} Chunks: {len(chunks) - new_total} wiederverwendet, "
          f"{new_total} neu ({len(todo_texts)} eindeutig einzubetten, "
          f"{zu_kurz} zu kurz für Bedeutung).")

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
                dim = dim or len(vecs[0])
                progress.melde(done, len(todo_texts), "embeddings")
                print(f"  … {done}/{len(todo_texts)} eingebettet", end="\r", flush=True)
        print()

    if dim is None:
        # Weder Altbestand noch etwas Neues: nur zu kurze Texte. Die Länge
        # einmal erfragen, damit die Matrix trotzdem die richtige Form bekommt.
        dim = len(embed([corpus.embed_text(chunks[0])], model, url)[0])
    leer = np.zeros(dim, dtype="float32")
    vectors = [leer if v is None else v for v in vectors]

    V, vp = save_vectors(store, np.vstack(vectors))
    write_db(store, chunks)
    write_info(store, model, V.shape[1], len(chunks), vp)
    # Alles wieder in der Vektordatei – die Hash-Sicherung wird nicht mehr
    # gebraucht.
    (Path(store) / STALE_VECTORS).unlink(missing_ok=True)
    # Erst jetzt, nachdem info.json auf die neue Datei zeigt: die vorige darf
    # weg. Wer sie noch abgebildet hat, behält sie – dann bleibt sie liegen und
    # der nächste Lauf räumt sie ab (siehe store_layout.prune_vectors).
    store_layout.prune_vectors(store, vp)
    return len(chunks), new_total, int(V.shape[1])


def main():
    ap = argparse.ArgumentParser()
    # Vorgaben aus app_config.json, sofern vorhanden – die Kommandozeile sticht
    # sie aus (siehe settings.py).
    ap.add_argument("teams", nargs="?", default=settings.value("teams_dir", "teams_export"))
    ap.add_argument("outlook", nargs="?", default=settings.value("outlook_dir", "outlook_export"))
    ap.add_argument("onedrive", nargs="?",
                    default=settings.value("onedrive_dir", "onedrive_export"))
    ap.add_argument("--store", default=settings.value("store_dir", "rag_store"))
    ap.add_argument("--model", default=settings.value("embed_model", DEFAULT_MODEL))
    ap.add_argument("--ollama", default=settings.value("ollama", DEFAULT_OLLAMA))
    ap.add_argument("--batch", type=int, default=settings.value("index_batch", 128))
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
                              a.batch, embeddings=not a.no_embeddings,
                              onedrive_dir=a.onedrive)
    if a.no_embeddings:
        print(f"\nFertig. {n} Chunks im Volltextindex – keine Embeddings.")
        print("Für die semantische/hybride Suche später ohne --no-embeddings "
              "erneut laufen lassen (Ollama nötig).")
    else:
        print(f"\nFertig. {n} Chunks im Index ({dim} Dimensionen), davon {new} neu berechnet.")
    print("Jetzt: python3 mcp_server.py")


if __name__ == "__main__":
    main()
