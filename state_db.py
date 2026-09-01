#!/usr/bin/env python3
"""
state_db.py – one state.db per export folder.

Every export keeps its state in ONE SQLite file inside its output folder:
the library mirrors one per <site>/<library>, the pages export one at its
root, and since 6.2 Outlook, Teams and OneDrive as well (migrate_state.py
moves their historical loose files in once, at app start).

What the file holds, by export:

    bestand       inventory, id -> (rel, ctag, size)   [drive mirrors]
    seiten        inventory, id -> (rel, etag)         [pages export]
    done          resume log, mail id -> rel           [Outlook]
    verschwunden  tombstones, rel -> gone-since        (append-only)
    walk          checkpointed enumeration            [drive mirrors]
    kv            delta pointer, folder tree, calendar list, completeness
                  report, Teams conversation state (JSON blobs)

The win over the loose files is the transaction: inventory and delta pointer
advance atomically instead of by documented write order. Locality stays –
delete the folder and its state is gone with it.

Writes happen from one thread per run (the collectors already funnel through
the main loop) except the Outlook resume log, which keeps its own locked
connection; readers elsewhere (app, corpus, MCP) open read-only.
"""

import json
import sqlite3
import threading
from pathlib import Path

import drive_mirror
import folders

DB_NAME = "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bestand(
    id   TEXT PRIMARY KEY,
    rel  TEXT NOT NULL,
    ctag TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seiten(
    id   TEXT PRIMARY KEY,
    rel  TEXT NOT NULL,
    etag TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verschwunden(
    rel  TEXT PRIMARY KEY,
    seit TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv(
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS walk(
    nr    INTEGER PRIMARY KEY AUTOINCREMENT,
    daten TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS done(
    mid TEXT PRIMARY KEY,
    rel TEXT NOT NULL
);
"""


class StateDb:
    """The one state file of an export folder. Every write is a transaction."""

    def __init__(self, ordner):
        self.pfad = Path(ordner) / DB_NAME

    # -- plumbing ----------------------------------------------------------
    def _verbinden(self, lesend=False, threadsafe=False):
        # threadsafe: the caller serialises access itself (DbDoneLog's lock)
        # – needed because mark() runs from the export's worker threads.
        if lesend and not self.pfad.exists():
            return None
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.pfad, timeout=10,
                              check_same_thread=not threadsafe)
        con.executescript(_SCHEMA)
        return con

    def _kv_lesen(self, key):
        con = self._verbinden(lesend=True)
        if con is None:
            return None
        try:
            row = con.execute("SELECT value FROM kv WHERE key = ?",
                              (key,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def _kv_schreiben(self, key, value):
        con = self._verbinden()
        try:
            with con:
                con.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (key, value))
        finally:
            con.close()

    # Public names for the exports that store whole JSON blobs (Teams state,
    # folder trees) – the underscore pair stays for compatibility.
    kv_lesen = _kv_lesen
    kv_schreiben = _kv_schreiben

    # -- inventory (library mirror) ---------------------------------------
    def bestand_lesen(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return {}
        try:
            return {r[0]: {"rel": r[1], "ctag": r[2], "size": r[3]}
                    for r in con.execute(
                        "SELECT id, rel, ctag, size FROM bestand")}
        finally:
            con.close()

    def bestand_schreiben(self, eintraege, delta_link=None):
        """Replace the inventory – and advance the delta pointer in the SAME
        transaction when one is passed: the two must never disagree."""
        con = self._verbinden()
        try:
            with con:
                con.execute("DELETE FROM bestand")
                con.executemany(
                    "INSERT INTO bestand(id, rel, ctag, size) VALUES(?,?,?,?)",
                    [(k, e["rel"], e["ctag"], int(e["size"]))
                     for k, e in eintraege.items()])
                if delta_link:
                    con.execute(
                        "INSERT INTO kv(key, value) VALUES('delta', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (delta_link,))
        finally:
            con.close()

    # -- inventory (pages) -------------------------------------------------
    def seiten_lesen(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return {}
        try:
            return {r[0]: {"rel": r[1], "etag": r[2]}
                    for r in con.execute("SELECT id, rel, etag FROM seiten")}
        finally:
            con.close()

    def seiten_schreiben(self, eintraege):
        con = self._verbinden()
        try:
            with con:
                con.execute("DELETE FROM seiten")
                con.executemany(
                    "INSERT INTO seiten(id, rel, etag) VALUES(?,?,?)",
                    [(k, e["rel"], e["etag"]) for k, e in eintraege.items()])
        finally:
            con.close()

    # -- tombstones (append-only) -----------------------------------------
    def verschwunden_lesen(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return {}
        try:
            return dict(con.execute("SELECT rel, seit FROM verschwunden"))
        finally:
            con.close()

    def verschwunden_ergaenzen(self, rels, jetzt):
        """Add tombstones; an existing entry keeps its first timestamp –
        the 'gone since' answer would otherwise creep forward."""
        if not rels:
            return
        con = self._verbinden()
        try:
            with con:
                con.executemany(
                    "INSERT INTO verschwunden(rel, seit) VALUES(?, ?) "
                    "ON CONFLICT(rel) DO NOTHING",
                    [(rel, jetzt) for rel in rels])
        finally:
            con.close()

    def verschwunden_ersetzen(self, eintraege):
        """Replace the tombstones wholesale – Outlook's healing path: a mail
        that reappears (it was merely moved) gets its marker withdrawn."""
        con = self._verbinden()
        try:
            with con:
                con.execute("DELETE FROM verschwunden")
                con.executemany(
                    "INSERT INTO verschwunden(rel, seit) VALUES(?, ?)",
                    list(eintraege.items()))
        finally:
            con.close()

    # -- delta pointer, tree, report --------------------------------------
    def delta_lesen(self):
        return self._kv_lesen("delta") or None

    def delta_loeschen(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return
        try:
            with con:
                con.execute("DELETE FROM kv WHERE key = 'delta'")
        finally:
            con.close()

    def baum_lesen(self):
        roh = self._kv_lesen("baum")
        if not roh:
            return None
        try:
            daten = json.loads(roh)
        except ValueError:
            return None
        return daten if isinstance(daten.get("ordner"), list) else None

    def baum_schreiben(self, eintraege, vorher=None):
        """Store the folder tree and report what changed – the same contract
        as folders.speichere, minus the loose file."""
        daten = folders.baum_diff(eintraege, vorher)
        self._kv_schreiben("baum", json.dumps(daten, ensure_ascii=False))
        return daten

    def bericht_lesen(self):
        roh = self._kv_lesen("bericht")
        try:
            return json.loads(roh) if roh else None
        except ValueError:
            return None

    def bericht_schreiben(self, bericht):
        self._kv_schreiben("bericht", json.dumps(bericht, ensure_ascii=False))

    # -- resume log (Outlook) ----------------------------------------------
    def done_ersetzen(self, paare):
        """Seed the resume log wholesale – the migration's path; a repeated
        mail id keeps the last entry, like the appended TSV did."""
        con = self._verbinden()
        try:
            with con:
                con.execute("DELETE FROM done")
                con.executemany(
                    "INSERT OR REPLACE INTO done(mid, rel) VALUES(?, ?)",
                    list(paare))
        finally:
            con.close()

    # -- walk staging (checkpointed enumeration) ---------------------------
    def walk_status(self):
        con = self._verbinden(lesend=True)
        n = 0
        if con is not None:
            try:
                n = con.execute("SELECT COUNT(*) FROM walk").fetchone()[0]
            finally:
                con.close()
        return {"cursor": self._kv_lesen("walk_cursor"),
                "fertig": self._kv_lesen("walk_fertig"), "n": n}

    def walk_ergaenzen(self, eintraege, cursor):
        """One delta page and its resume link in ONE transaction – a crash
        never leaves entries without the cursor that follows them."""
        con = self._verbinden()
        try:
            with con:
                con.executemany(
                    "INSERT INTO walk(daten) VALUES(?)",
                    [(json.dumps(e, ensure_ascii=False),) for e in eintraege])
                if cursor:
                    con.execute(
                        "INSERT INTO kv(key, value) VALUES('walk_cursor', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (cursor,))
        finally:
            con.close()

    def walk_abschliessen(self, delta_link):
        con = self._verbinden()
        try:
            with con:
                con.execute("DELETE FROM kv WHERE key = 'walk_cursor'")
                if delta_link:
                    con.execute(
                        "INSERT INTO kv(key, value) VALUES('walk_fertig', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (delta_link,))
        finally:
            con.close()

    def walk_eintraege(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return
        try:
            for (roh,) in con.execute("SELECT daten FROM walk ORDER BY nr"):
                try:
                    yield json.loads(roh)
                except ValueError:
                    continue
        finally:
            con.close()

    def walk_leeren(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return
        try:
            with con:
                con.execute("DELETE FROM walk")
                con.execute("DELETE FROM kv WHERE key IN "
                            "('walk_cursor', 'walk_fertig')")
        finally:
            con.close()


class DbDoneLog:
    """Outlook's resume log – one row per finished mail, same interface as
    the historical DoneLog on exported.tsv.

    mark() runs once per exported mail, so this class keeps one connection
    open (WAL, synchronous=NORMAL) instead of reconnecting 45k times."""

    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._con = db._verbinden(threadsafe=True)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self.done = dict(self._con.execute("SELECT mid, rel FROM done"))

    def is_done(self, out, mid):
        rel = self.done.get(mid)
        return bool(rel) and (Path(out) / rel).exists()

    def mark(self, mid, rel):
        with self._lock:
            self.done[mid] = rel
            with self._con:
                self._con.execute(
                    "INSERT INTO done(mid, rel) VALUES(?, ?) "
                    "ON CONFLICT(mid) DO UPDATE SET rel = excluded.rel",
                    (mid, rel))

    def remap(self, fn):
        """fn(rel)->rel over every entry, in one transaction – for one-off
        path migrations (resume survives)."""
        with self._lock:
            self.done = {mid: fn(rel) for mid, rel in self.done.items()}
            with self._con:
                self._con.execute("DELETE FROM done")
                self._con.executemany(
                    "INSERT INTO done(mid, rel) VALUES(?, ?)",
                    list(self.done.items()))

    def close(self):
        try:
            self._con.close()
        except Exception:
            pass


class DbBestand(drive_mirror.Bestand):
    """The mirror inventory, backed by the folder's state.db.

    Same interface as the file-backed Bestand – plane/hole_alle cannot tell
    them apart; only loading and writing differ."""

    def __init__(self, db):
        self.db = db
        self.pfad = db.pfad
        self.eintraege = db.bestand_lesen()
        self._lock = threading.Lock()

    def schreibe(self, delta_link=None):
        self.db.bestand_schreiben(self.eintraege, delta_link)


class DbZustand:
    """drive_mirror's state interface, backed by one state.db per folder."""

    def __init__(self, out):
        self.db = StateDb(out)

    def bestand(self):
        return DbBestand(self.db)

    def delta_lesen(self):
        return self.db.delta_lesen()

    def delta_schreiben(self, link, bestand=None):
        if not link:
            return             # nichts zu merken heißt: der alte Zeiger gilt
        if isinstance(bestand, DbBestand):
            # The transactional win over the loose files: inventory and
            # pointer can never disagree after a crash.
            bestand.schreibe(delta_link=link)
        else:
            self.db._kv_schreiben("delta", link)

    def delta_loeschen(self):
        self.db.delta_loeschen()

    def verschwunden_lesen(self):
        return self.db.verschwunden_lesen()

    def verschwunden_ergaenzen(self, rels, jetzt):
        self.db.verschwunden_ergaenzen(rels, jetzt)

    def baum_lesen(self):
        return self.db.baum_lesen()

    def baum_schreiben(self, eintraege, vorher):
        return self.db.baum_schreiben(eintraege, vorher)

    def bericht_schreiben(self, bericht):
        self.db.bericht_schreiben(bericht)

    def walk_status(self):
        return self.db.walk_status()

    def walk_ergaenzen(self, eintraege, cursor):
        self.db.walk_ergaenzen(eintraege, cursor)

    def walk_abschliessen(self, delta_link):
        self.db.walk_abschliessen(delta_link)

    def walk_eintraege(self):
        return self.db.walk_eintraege()

    def walk_leeren(self):
        self.db.walk_leeren()
