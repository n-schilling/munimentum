#!/usr/bin/env python3
"""
analytics_db.py – the Analytics tab's numbers, materialised once per index
run instead of computed on every page visit.

The index step touches every message anyway; right after writing corpus.db
it calls baue(), which aggregates the archive (per-month history, gaps,
attachment and file types, top people, folder sizes with the largest files)
and stores ONE JSON document in an `analytics` table inside the same
corpus.db. Same lifecycle as the index: rebuilt with it, gone with it.
/api/analytics is then a plain read – no directory walks, no aggregation in
the request thread, no cache layers with their own invalidation rules.

Communication and files are separate worlds here: the per-month history,
the gaps and "messages" count only mail and chat. A mirrored file carries
its file-system mtime as timestamp – letting it into the history used to
fill communication gaps with PDFs.
"""

import heapq
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import store_layout

KOMM = ("teams", "outlook")            # what counts as communication
GROESSTE_N = 8                         # largest single files kept per archive
TOP_PERSONEN = 40                      # stored; the app filters and cuts to 10


# ---------------------------------------------------------------------------
# Bausteine
# ---------------------------------------------------------------------------
def _monatsreihe(von, bis):
    """Alle Monate von…bis, auch die leeren – sonst fiele eine Lücke nicht
    auf, sie stünde einfach nicht da."""
    j, m = int(von[:4]), int(von[5:7])
    ende = (int(bis[:4]), int(bis[5:7]))
    out = []
    while (j, m) <= ende:
        out.append(f"{j:04d}-{m:02d}")
        j, m = (j + 1, 1) if m == 12 else (j, m + 1)
    return out


def _luecken(monate, vorhanden):
    """Zusammenhängende Monate ohne eine einzige Nachricht – nur INNERHALB
    des Bestands: vor der ersten und nach der letzten ist nichts zu
    vermissen."""
    out, lauf = [], []
    for m in monate:
        if vorhanden.get(m):
            if lauf:
                out.append({"von": lauf[0], "bis": lauf[-1],
                            "monate": len(lauf)})
                lauf = []
        else:
            lauf.append(m)
    return out


def _groesse(pfad):
    """(Bytes gesamt, größte Einzeldateien) – one walk, done here so the
    request thread never has to."""
    gesamt, groesste = 0, []
    try:
        for p in Path(pfad).rglob("*"):
            try:
                if not p.is_file():
                    continue
                n = p.stat().st_size
            except OSError:
                continue
            gesamt += n
            eintrag = (n, str(p.relative_to(pfad)))
            if len(groesste) < GROESSTE_N:
                heapq.heappush(groesste, eintrag)
            else:
                heapq.heappushpop(groesste, eintrag)
    except OSError:
        pass
    return gesamt, sorted(groesste, reverse=True)


def _verlauf(con):
    roh = con.execute(
        "SELECT strftime('%Y-%m', ts, 'unixepoch') m, "
        "       SUM(src = 'teams'), SUM(src = 'outlook'), COUNT(*) "
        "FROM chunks WHERE seq = 0 AND ts IS NOT NULL "
        "AND src IN ('teams', 'outlook') GROUP BY m ORDER BY m").fetchall()
    verlauf, vorhanden = [], {}
    if roh:
        werte = {m: (te, ou, ge) for m, te, ou, ge in roh}
        summe = 0
        for m in _monatsreihe(roh[0][0], roh[-1][0]):
            te, ou, ge = werte.get(m, (0, 0, 0))
            summe += ge
            vorhanden[m] = ge
            verlauf.append({"m": m, "teams": te, "outlook": ou,
                            "gesamt": ge, "summe": summe})
    return verlauf, _luecken(list(vorhanden), vorhanden)


def _anhang_typen(con):
    """Anhangstypen der Kommunikation – die Spiegel haben ihre eigene Liste
    (siehe _datei_typen), sonst bestünde diese hier zur Hälfte aus dem
    Inhalt des Laufwerks."""
    typen = {}
    komm = ", ".join(f"'{s}'" for s in KOMM)
    for (att,) in con.execute(
            f"SELECT att FROM chunks WHERE seq = 0 AND att IS NOT NULL "
            f"AND att != '' AND src IN ({komm})"):
        for name in att.split(" "):
            if "." in name:
                typ = name.rsplit(".", 1)[1].lower()[:8]
                typen[typ] = typen.get(typ, 0) + 1
    top = sorted(typen.items(), key=lambda x: -x[1])[:10]
    rest = sum(typen.values()) - sum(n for _, n in top)
    return ([{"typ": e, "n": n} for e, n in top]
            + ([{"typ": "…", "n": rest}] if rest else []))


def _datei_typen(con, spalten):
    if "ext" not in spalten:
        return None
    zeilen = con.execute(
        "SELECT ext, COUNT(*) n FROM chunks WHERE seq = 0 AND src = 'datei' "
        "AND ext IS NOT NULL AND ext != '' GROUP BY ext "
        "ORDER BY n DESC LIMIT 10").fetchall()
    return [{"typ": e, "n": n} for e, n in zeilen]


# ---------------------------------------------------------------------------
# Bauen und Lesen
# ---------------------------------------------------------------------------
def baue(store, ordner):
    """Aggregate the archive and store the result in corpus.db.

    ``ordner`` maps the size-tile names (teams, outlook, onedrive,
    sharepoint, pages) to their export folders. Returns the payload; None
    when there is no index to aggregate."""
    db = store_layout.db_path(store)
    if not db.exists():
        return None
    out = {"built_at": datetime.now(UTC).isoformat(timespec="seconds")}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        spalten = {r[1] for r in con.execute("PRAGMA table_info(chunks)")}
        komm = ", ".join(f"'{s}'" for s in KOMM)
        out["quellen"] = [{"src": src, "n": n} for src, n in con.execute(
            "SELECT src, COUNT(DISTINCT uid) FROM chunks GROUP BY src "
            "ORDER BY 2 DESC")]
        je_quelle = {q["src"]: q["n"] for q in out["quellen"]}

        # None heißt „weiß ich nicht", 0 hieße „keine": ein Index aus einer
        # älteren Fassung kennt die Spalten nicht.
        out["komm"] = {
            "nachrichten": sum(je_quelle.get(s, 0) for s in KOMM),
            "gespraeche": None, "mit_anhang": None, "verschwunden": None,
            "personen": con.execute(
                "SELECT COUNT(DISTINCT who) FROM people "
                "WHERE who != ''").fetchone()[0],
            "von": None, "bis": None,
        }
        von, bis = con.execute(
            f"SELECT MIN(ts), MAX(ts) FROM chunks WHERE ts IS NOT NULL "
            f"AND src IN ({komm})").fetchone()
        out["komm"]["von"], out["komm"]["bis"] = von, bis
        if "thread" in spalten:
            out["komm"]["gespraeche"] = con.execute(
                f"SELECT COUNT(DISTINCT thread) FROM chunks "
                f"WHERE thread IS NOT NULL AND thread != '' "
                f"AND src IN ({komm})").fetchone()[0]
        if "att" in spalten:
            out["komm"]["mit_anhang"] = con.execute(
                f"SELECT COUNT(*) FROM chunks WHERE seq = 0 "
                f"AND att IS NOT NULL AND att != '' "
                f"AND src IN ({komm})").fetchone()[0]
        if "gone" in spalten:
            out["komm"]["verschwunden"] = con.execute(
                f"SELECT COUNT(*) FROM chunks WHERE seq = 0 "
                f"AND gone IS NOT NULL AND src IN ({komm})").fetchone()[0]

        out["dateien"] = {
            "n": je_quelle.get("datei", 0), "pages": je_quelle.get("pages", 0),
            "onedrive": 0, "sharepoint": 0, "verschwunden": None}
        if "root" in spalten:
            for wurzel, n in con.execute(
                    "SELECT root, COUNT(DISTINCT uid) FROM chunks "
                    "WHERE src = 'datei' GROUP BY root"):
                if wurzel in ("onedrive", "sharepoint"):
                    out["dateien"][wurzel] = n
        if "gone" in spalten:
            out["dateien"]["verschwunden"] = con.execute(
                "SELECT COUNT(*) FROM chunks WHERE seq = 0 "
                "AND gone IS NOT NULL "
                "AND src IN ('datei', 'pages')").fetchone()[0]

        verlauf, luecken = _verlauf(con)
        out["verlauf"], out["luecken"] = verlauf, luecken
        out["anhang_typen"] = _anhang_typen(con) if "att" in spalten else None
        out["datei_typen"] = _datei_typen(con, spalten)
        out["top_personen"] = [{"who": w, "n": n} for w, n in con.execute(
            "SELECT who, SUM(messages) m FROM people WHERE who != '' "
            "GROUP BY who ORDER BY m DESC LIMIT ?", (TOP_PERSONEN,))]
    finally:
        con.close()

    out["groesse"], grosse = {}, []
    for name, pfad in (ordner or {}).items():
        n, groesste = _groesse(pfad)
        out["groesse"][name] = n
        grosse += [{"quelle": name, "bytes": b, "pfad": rel}
                   for b, rel in groesste]
    out["groesse"]["index"], _ = _groesse(store)
    out["grosse_dateien"] = sorted(grosse, key=lambda x: -x["bytes"])[:GROESSTE_N]

    schreib = sqlite3.connect(db, timeout=10)
    try:
        with schreib:
            schreib.execute("CREATE TABLE IF NOT EXISTS analytics("
                            "key TEXT PRIMARY KEY, value TEXT)")
            schreib.execute(
                "INSERT INTO analytics(key, value) VALUES('payload', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(out, ensure_ascii=False),))
    finally:
        schreib.close()
    return out


def lies(store):
    """The stored payload, or None (no index, older index, broken row)."""
    db = store_layout.db_path(store)
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT value FROM analytics "
                          "WHERE key = 'payload'").fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError):
        return None
    finally:
        con.close()
