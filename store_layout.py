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
from pathlib import Path

INFO = "info.json"
DB = "corpus.db"


def db_path(store):
    """The store's database – one name, not the same literal nine times."""
    return Path(store) / DB

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
