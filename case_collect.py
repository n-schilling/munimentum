#!/usr/bin/env python3
"""
case_collect.py – the automatic searches of the cases, run once.

    case_collect.py --faelle <faelle.db> --store <index folder>
                    [--domains "example.com nordwind.example"]
                    [--case <id>] [--search <id>]

A saved search attached to a case can be switched to *automatic*
(faelle.py, `automatisch`). This step runs every such search against the
index – the text search, nothing else ranks sharply enough to file
unasked – and puts what the case does not hold yet into the search's
folder, marked as collected by it. What was taken out of the case by
hand stays out (the case book's `entfernt` marks); a closed case is not
touched.

The app appends this step to every run that indexes, as long as one
automatic search exists, and starts it alone for "Collect now" on a case
(`--case`) or on one search (`--search`, switched on or not: a one-off).
The MCP server's collect_case does the same in-process, through
`einsammeln`. No Microsoft, nothing leaves the machine.

Runs as a step of the app (progress.py protocol) – the run window shows
it, the run history keeps it.
"""

import sys
import argparse

import evidence
import faelle
import progress
import export_util

export_util.erzwinge_utf8()

# Hits looked at per search and run. A search that finds more than this
# is too wide to file by itself – the log says so, the first ones count.
GRENZE = 500


def eintrag_aus(h):
    """One hit of the engine as the case book stores it."""
    return {"key": h["key"], "src": h.get("source"), "root": h.get("root"), "rel": h.get("path"),
            "titel": h.get("title"), "datum": h.get("date"), "wer": h.get("who")}


def einsammeln(buch, suchen, suche_fn, grenze=GRENZE, melde=None):
    """Every search in `suchen` (rows of the case book, `automatische`)
    once: `suche_fn(kriterien, n)` answers like the engine does (`results`
    or `error`), the new hits go into the search's case and folder.
    `melde(key, level, **vars)` gets one line per search. Returns the
    report – one dict per search – and the sums."""
    melde = melde or (lambda *a, **kw: None)
    bericht, summe = [], {"new": 0, "skipped": 0, "already": 0, "errors": 0}
    for g in suchen:
        zeile = {"id": g["id"], "search": g["name"], "case": g["fall_name"], "case_id": g["fall"],
                 "folder": g.get("ordner_name"), "hits": 0, "new": 0, "skipped": 0, "already": 0}
        res = suche_fn(g["kriterien"], grenze)
        if res.get("error"):
            zeile["error"] = str(res["error"])
            summe["errors"] += 1
            melde("run.collect.error", "err", case=g["fall_name"], search=g["name"], error=zeile["error"])
            bericht.append(zeile)
            continue
        treffer = [h for h in (res.get("results") or ()) if h.get("key")]
        zeile["hits"] = len(treffer)
        # The search ran – its row says so, as after a run by hand
        buch.gelaufen(g["id"], len(treffer))
        if len(treffer) >= grenze:
            melde("run.collect.capped", "warn", search=g["name"], n=grenze)
        keys = buch.keys(g["fall"])
        kandidaten = [eintrag_aus(h) for h in treffer if h["key"] not in keys]
        zeile["already"] = len(treffer) - len(kandidaten)
        try:
            neu, weg = buch.einsammeln(g["fall"], kandidaten, g.get("ordner"), g["id"])
        except (faelle.FallGeschlossen, faelle.KeinFall, faelle.KeinOrdner) as e:
            zeile["error"] = type(e).__name__
            summe["errors"] += 1
            melde("run.collect.error", "err", case=g["fall_name"], search=g["name"], error=zeile["error"])
            bericht.append(zeile)
            continue
        zeile["new"], zeile["skipped"] = neu, weg
        buch.eingesammelt(g["id"], neu, weg)
        for k in ("new", "skipped", "already"):
            summe[k] += zeile[k]
        melde("run.collect.search", "info", case=g["fall_name"], search=g["name"], hits=len(treffer),
              new=neu, skipped=weg, folder=g.get("ordner_name") or "")
        bericht.append(zeile)
    return bericht, summe


def _suchen(buch, fall_id=None, suche_id=None):
    """What this run works through: one search (a one-off, automatic or
    not), one case's automatic searches, or all of them."""
    if suche_id is not None:
        g = buch.gespeichert(suche_id)
        if g is None or g["fall"] is None:
            return None
        return [g]
    return buch.automatische(fall_id)


def _engine(store, domains):
    """The search engine on the index, lexical only – the same module the
    app and the MCP server search with, set up for one run."""
    import store_layout
    db = store_layout.db_path(store)
    if not db.exists():
        return None, None
    import mcp_server
    mcp_server.STATE.update(db=str(db), V=None, np=None, semantic=False, vector_dtype=None,
                            internal_domains=domains or "", cases_write=False)
    return mcp_server, db


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--faelle", required=True, help="the profile's faelle.db")
    ap.add_argument("--store", required=True, help="the index folder")
    ap.add_argument("--domains", default="", help="the user's own mail domains (the 'external' mark)")
    ap.add_argument("--case", type=int, default=None, help="only this case's automatic searches")
    ap.add_argument("--search", type=int, default=None, help="only this saved search, on or off")
    ap.add_argument("--data", default="", help="the data folder – pins what comes in (evidence.py)")
    a = ap.parse_args()
    buch = faelle.Fallbuch(a.faelle)
    if a.data:
        buch.pinner = evidence.pinner(a.data)
    suchen = _suchen(buch, a.case, a.search)
    if suchen is None:
        progress.event("run.collect.nosearch", "err", id=a.search)
        progress.ergebnis(0, errors=1)
        sys.exit(1)
    if not suchen:
        progress.event("run.collect.nothing")
        progress.ergebnis(0)
        return
    mod, db = _engine(a.store, a.domains)
    if mod is None:
        progress.event("run.collect.noindex", "err")
        progress.ergebnis(0, errors=1)
        sys.exit(1)
    if a.search is not None and suchen[0]["kriterien"].get("mode") != "text":
        progress.event("run.collect.notext", "err", search=suchen[0]["name"])
        progress.ergebnis(0, errors=1)
        sys.exit(1)
    mcp_server = mod
    mcp_server.STATE["faelle_db"] = str(buch.pfad)
    progress.event("run.collect.start", n=len(suchen), m=len({g["fall"] for g in suchen}))

    def suche(k, n):
        return mcp_server._mit_kriterien(k, n, preview_chars=0)

    bericht, summe = einsammeln(buch, suchen, suche, melde=progress.event)
    progress.event("run.collect.done", n=summe["new"], skipped=summe["skipped"])
    progress.ergebnis(summe["new"], unchanged=summe["already"], excluded=summe["skipped"],
                      errors=summe["errors"], extra={"searches": len(suchen)})


if __name__ == "__main__":
    main()
