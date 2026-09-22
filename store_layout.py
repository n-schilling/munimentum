#!/usr/bin/env python3
"""
store_layout.py – which file lives where in the store.

An index consists of three parts: corpus.db (text passages and full-text
index), a vector file (embeddings, row i belongs to chunks.id = i+1) and
info.json (model, format – and the NAME of the vector file).

Why the name is not a fixed "vectors.npy":

    Every reader opens the vector file via mmap and keeps it open for as
    long as it runs – the MCP server, the search in the app itself, a server
    started by Claude Desktop. On Windows a file mapped like that cannot be
    replaced: os.replace ends with "access denied". The index run therefore
    died right there on its last line, after embedding everything – and
    reliably so, as long as anyone had the index open. On macOS and Linux
    this goes unnoticed – there the old file survives the rename as an
    inode, and the reader finishes it undisturbed.

    So a run writes a NEW file and records its name here. Nothing gets
    replaced any more; whoever still has the old one open keeps a valid
    copy in hand. What nobody needs any more is swept up – and what refuses
    to be deleted stays around until the next run.

Standard library only: mcp_server.py pulls this module in and itself
deliberately gets by without numpy.
"""

import json
import re
import sqlite3
from pathlib import Path

INFO = "info.json"
DB = "corpus.db"

# The four lines of a mail and the column each one lives in (13.0). The
# writer (rag_index) and the reader (mcp_server) share this one map; "from"
# has had its column since 11.1, the other three came with the addresses.
MAIL_SPALTEN = {"from": "who_mail", "to": "to_ppl", "cc": "cc_ppl", "bcc": "bcc_ppl"}
# Which of them an index must have before the Mail filter can be offered.
MAIL_NEU = ("to_ppl", "cc_ppl", "bcc_ppl")

# What the parsers in corpus.py make of a file. An incremental run takes
# the chunks of every unchanged file straight from the old index, so a
# correction in a parser would never reach a file nobody touches again.
# Raising this number is how such a correction is made to count: the next
# index run reads every file once more (the embeddings are matched by the
# text's hash, so only what really changed is computed again).
#
#   2  the name of a Teams conversation is its own heading, not the
#      headings a message pasted in brought with it
PARSER = 2


def parser_stand(store):
    """The reader's generation this index was built with; an index from
    before the number knows none and counts as the first."""
    try:
        return int(info(store).get("parser") or 1)
    except (TypeError, ValueError):
        return 1


def db_path(store):
    """The store's database – one name, not the same literal nine times."""
    return Path(store) / DB


def mit_schluesseln(db):
    """Does this index carry what the interface needs – the item keys of
    11.0 (schluessel.py), the sender addresses of 11.1 and the mail lines
    of 13.0? An older one – or none – does not, and the next index run
    builds it afresh instead of waiting for new exports."""
    db = Path(db)
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            spalten = {r[1] for r in con.execute("PRAGMA table_info(chunks)")}
            return {"key", "who_mail", "domains", *MAIL_NEU} <= spalten
        finally:
            con.close()
    except sqlite3.Error:
        return False


def veraltet(db):
    """An index that exists but predates the item keys, the sender
    addresses or the mail lines: the next index step rebuilds it whether or
    not the exports brought anything, so cases can point at its items, name
    their people, and the search can ask who stood in which line. No index
    at all is not "outdated" – that case the run gate handles by the
    missing file."""
    return Path(db).exists() and not mit_schluesseln(db)

# The fixed name of stores up to 4.1.0. Such a store carries no entry in
# info.json – there this file remains the valid one, otherwise an existing
# index would sit without its embeddings after the update.
LEGACY = "vectors.npy"

_MUSTER = re.compile(r"^vectors-(\d+)\.npy$")


def info(store):
    """info.json as a dict; missing or unreadable yields {}."""
    try:
        daten = json.loads((Path(store) / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return daten if isinstance(daten, dict) else {}


def vectors_path(store, daten=None):
    """The current vector file – or None when this index has none.

    Three cases, and the middle one is the reason for the distinction:

        entry present     -> exactly that file
        entry explicitly empty -> NO vectors (a run without embeddings has
                             retired them). Falling back to the old name
                             here would be wrong: the file may still be
                             lying around because it refused deletion, and
                             its rows then no longer match the DB.
        no entry          -> store from 4.1.0 or older, fixed name
    """
    sp = Path(store)
    daten = info(store) if daten is None else daten
    if "vectors" in daten:
        name = daten.get("vectors")
        if not name:
            return None
        p = sp / str(name)
        return p if p.exists() else None
    p = sp / LEGACY
    return p if p.exists() else None


def next_vectors_path(store):
    """A name that does not exist here yet: vectors-1.npy, vectors-2.npy, …

    Counting goes by what lies in the folder, not by the entry in
    info.json: a file that did not go away during cleanup must not be
    overwritten by a later run – that would disturb exactly the reader it
    was left behind for.
    """
    sp = Path(store)
    hoechste = 0
    for p in sp.glob("vectors-*.npy"):
        m = _MUSTER.match(p.name)
        if m:
            hoechste = max(hoechste, int(m.group(1)))
    return sp / f"vectors-{hoechste + 1}.npy"


def prune_vectors(store, behalten=None):
    """Sweep up vector files nobody needs any more.

    Whatever is currently mapped cannot be deleted on Windows. That is not
    an error case but the normal state right after a run: the still-running
    MCP server holds the previous version. It costs space until the next
    run, which simply tries again.

    Returns the number of files actually deleted.
    """
    sp = Path(store)
    behalten = Path(behalten).name if behalten else None
    weg = 0
    for p in sorted(sp.glob("vectors-*.npy")) + [sp / LEGACY]:
        if p.name == behalten or not p.exists():
            continue
        try:
            p.unlink()
            weg += 1
        except OSError:
            pass                  # still open – try again next time
    return weg
