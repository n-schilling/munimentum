#!/usr/bin/env python3
"""
state_db.py – one state.db per export folder.

Every export keeps its state in ONE SQLite file inside its output folder:
the library mirrors one per <site>/<library>, the pages export one at its
root, Outlook, Teams and OneDrive one each.

What the file holds, by export:

    bestand       inventory, id -> (rel, ctag, size)   [drive mirrors]
    seiten        inventory, id -> (rel, etag)         [pages export]
    done          resume log, mail id -> rel           [Outlook]
    verschwunden  tombstones, rel -> gone-since        (append-only)
    walk          checkpointed enumeration            [drive mirrors]
    kv            delta pointer, folder tree, calendar list, completeness
                  report, small JSON blobs
    saetze        records by area, (area, key) -> JSON – one row per
                  conversation, page, task or resource, so an export updates
                  the rows it touched instead of rewriting one big blob

The win over the loose files is the transaction: inventory and delta pointer
advance atomically instead of by documented write order. Locality stays –
delete the folder and its state is gone with it.

Connections: one per StateDb instance and thread, opened on first use and
kept (the schema runs once per connection, not once per call – an export
touches its state thousands of times per run). Writes happen from one
thread per run (the collectors already funnel through the main loop) except
the Outlook resume log, which keeps its own locked connection; readers
elsewhere (app, corpus, MCP) open read-only and are short-lived.
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
CREATE TABLE IF NOT EXISTS saetze(
    bereich TEXT NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY(bereich, key)
);
"""


class StateDb:
    """The one state file of an export folder. Every write is a transaction."""

    def __init__(self, ordner, dateiname=DB_NAME):
        # `dateiname`: a second file next to the export's own – the calendar
        # step keeps its manifest apart, so the export's file (whose mtime
        # dates the last run) stays untouched by it.
        self.pfad = Path(ordner) / dateiname
        self._lokal = threading.local()

    # -- plumbing ----------------------------------------------------------
    def _neu_verbinden(self, threadsafe=False):
        # threadsafe: the caller serialises access itself (DbDoneLog's lock)
        # – needed because mark() runs from the export's worker threads.
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.pfad, timeout=10,
                              check_same_thread=not threadsafe)
        con.executescript(_SCHEMA)
        return con

    def _verbinden(self, lesend=False):
        """The thread's connection to this file – opened once, then kept.
        A read on a file that does not exist returns None and creates
        nothing: empty folders stay empty."""
        con = getattr(self._lokal, "con", None)
        if con is not None:
            return con
        if lesend and not self.pfad.exists():
            return None
        con = self._neu_verbinden()
        self._lokal.con = con
        return con

    def close(self):
        """Close this thread's connection; the next call reopens it."""
        con = getattr(self._lokal, "con", None)
        if con is not None:
            self._lokal.con = None
            try:
                con.close()
            except Exception:
                pass

    def _kv_lesen(self, key):
        con = self._verbinden(lesend=True)
        if con is None:
            return None
        row = con.execute("SELECT value FROM kv WHERE key = ?",
                          (key,)).fetchone()
        return row[0] if row else None

    def _kv_schreiben(self, key, value):
        con = self._verbinden()
        with con:
            con.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, value))

    def kv_loeschen(self, key):
        con = self._verbinden(lesend=True)
        if con is None:
            return
        with con:
            con.execute("DELETE FROM kv WHERE key = ?", (key,))

    # -- records by area ---------------------------------------------------
    def satz_lesen(self, bereich, key):
        con = self._verbinden(lesend=True)
        if con is None:
            return None
        row = con.execute("SELECT value FROM saetze WHERE bereich = ? AND key = ?",
                          (bereich, key)).fetchone()
        return row[0] if row else None

    def saetze_lesen(self, bereich):
        """Every record of an area, key -> value (a string, usually JSON)."""
        con = self._verbinden(lesend=True)
        if con is None:
            return {}
        return dict(con.execute(
            "SELECT key, value FROM saetze WHERE bereich = ? ORDER BY key",
            (bereich,)))

    def saetze_schreiben(self, bereich, eintraege):
        """Upsert the given records of an area – rows not named stay."""
        if not eintraege:
            return
        con = self._verbinden()
        with con:
            con.executemany(
                "INSERT INTO saetze(bereich, key, value) VALUES(?, ?, ?) "
                "ON CONFLICT(bereich, key) DO UPDATE SET value = excluded.value",
                [(bereich, k, v) for k, v in eintraege.items()])

    def saetze_loeschen(self, bereich, keys):
        keys = list(keys)
        if not keys:
            return
        con = self._verbinden(lesend=True)
        if con is None:
            return
        with con:
            con.executemany(
                "DELETE FROM saetze WHERE bereich = ? AND key = ?",
                [(bereich, k) for k in keys])

    def saetze_leeren(self, bereich):
        con = self._verbinden(lesend=True)
        if con is None:
            return
        with con:
            con.execute("DELETE FROM saetze WHERE bereich = ?", (bereich,))

    # Public names for the exports that store whole JSON blobs (Teams state,
    # folder trees) – the underscore pair stays for compatibility.
    kv_lesen = _kv_lesen
    kv_schreiben = _kv_schreiben

    # -- inventory (library mirror) ---------------------------------------
    def bestand_lesen(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return {}
        return {r[0]: {"rel": r[1], "ctag": r[2], "size": r[3]}
                for r in con.execute(
                    "SELECT id, rel, ctag, size FROM bestand")}

    @staticmethod
    def _delta_setzen(con, delta_link):
        if delta_link:
            con.execute(
                "INSERT INTO kv(key, value) VALUES('delta', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (delta_link,))

    def bestand_schreiben(self, eintraege, delta_link=None):
        """Replace the inventory – and advance the delta pointer in the SAME
        transaction when one is passed: the two must never disagree."""
        con = self._verbinden()
        with con:
            con.execute("DELETE FROM bestand")
            con.executemany(
                "INSERT INTO bestand(id, rel, ctag, size) VALUES(?,?,?,?)",
                [(k, e["rel"], e["ctag"], int(e["size"]))
                 for k, e in eintraege.items()])
            self._delta_setzen(con, delta_link)

    def bestand_aktualisieren(self, geaendert, geloescht=(), delta_link=None):
        """Write only what moved: upsert the changed rows, delete the gone
        ones, advance the pointer – one transaction. A mirror of 100k files
        with 5k downloads used to rewrite the whole table 500 times."""
        geloescht = list(geloescht)
        if not geaendert and not geloescht and not delta_link:
            return
        con = self._verbinden()
        with con:
            if geloescht:
                con.executemany("DELETE FROM bestand WHERE id = ?",
                                [(k,) for k in geloescht])
            if geaendert:
                con.executemany(
                    "INSERT INTO bestand(id, rel, ctag, size) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET rel = excluded.rel, "
                    "ctag = excluded.ctag, size = excluded.size",
                    [(k, e["rel"], e["ctag"], int(e["size"]))
                     for k, e in geaendert.items()])
            self._delta_setzen(con, delta_link)

    # -- inventory (pages) -------------------------------------------------
    def seiten_lesen(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return {}
        return {r[0]: {"rel": r[1], "etag": r[2]}
                for r in con.execute("SELECT id, rel, etag FROM seiten")}

    def seiten_schreiben(self, eintraege):
        con = self._verbinden()
        with con:
            con.execute("DELETE FROM seiten")
            con.executemany(
                "INSERT INTO seiten(id, rel, etag) VALUES(?,?,?)",
                [(k, e["rel"], e["etag"]) for k, e in eintraege.items()])

    def seiten_aktualisieren(self, geaendert, geloescht=()):
        """Upsert the changed pages, delete the gone ones – nothing else
        is touched."""
        geloescht = list(geloescht)
        if not geaendert and not geloescht:
            return
        con = self._verbinden()
        with con:
            if geloescht:
                con.executemany("DELETE FROM seiten WHERE id = ?",
                                [(k,) for k in geloescht])
            if geaendert:
                con.executemany(
                    "INSERT INTO seiten(id, rel, etag) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET rel = excluded.rel, "
                    "etag = excluded.etag",
                    [(k, e["rel"], e["etag"]) for k, e in geaendert.items()])

    # -- tombstones (append-only) -----------------------------------------
    def verschwunden_lesen(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return {}
        return dict(con.execute("SELECT rel, seit FROM verschwunden"))

    def verschwunden_ergaenzen(self, rels, jetzt):
        """Add tombstones; an existing entry keeps its first timestamp –
        the 'gone since' answer would otherwise creep forward."""
        if not rels:
            return
        con = self._verbinden()
        with con:
            con.executemany(
                "INSERT INTO verschwunden(rel, seit) VALUES(?, ?) "
                "ON CONFLICT(rel) DO NOTHING",
                [(rel, jetzt) for rel in rels])

    def verschwunden_ersetzen(self, eintraege):
        """Replace the tombstones wholesale – Outlook's healing path: a mail
        that reappears (it was merely moved) gets its marker withdrawn."""
        con = self._verbinden()
        with con:
            con.execute("DELETE FROM verschwunden")
            con.executemany(
                "INSERT INTO verschwunden(rel, seit) VALUES(?, ?)",
                list(eintraege.items()))

    # -- delta pointer, tree, report --------------------------------------
    def delta_lesen(self):
        return self._kv_lesen("delta") or None

    def delta_loeschen(self):
        con = self._verbinden(lesend=True)
        if con is None:
            return
        with con:
            con.execute("DELETE FROM kv WHERE key = 'delta'")

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

    # -- walk staging (checkpointed enumeration) ---------------------------
    def walk_status(self):
        con = self._verbinden(lesend=True)
        n = 0
        if con is not None:
            n = con.execute("SELECT COUNT(*) FROM walk").fetchone()[0]
        return {"cursor": self._kv_lesen("walk_cursor"),
                "fertig": self._kv_lesen("walk_fertig"), "n": n}

    def walk_ergaenzen(self, eintraege, cursor):
        """One delta page and its resume link in ONE transaction – a crash
        never leaves entries without the cursor that follows them."""
        con = self._verbinden()
        with con:
            con.executemany(
                "INSERT INTO walk(daten) VALUES(?)",
                [(json.dumps(e, ensure_ascii=False),) for e in eintraege])
            if cursor:
                con.execute(
                    "INSERT INTO kv(key, value) VALUES('walk_cursor', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (cursor,))

    def walk_abschliessen(self, delta_link):
        con = self._verbinden()
        with con:
            con.execute("DELETE FROM kv WHERE key = 'walk_cursor'")
            if delta_link:
                con.execute(
                    "INSERT INTO kv(key, value) VALUES('walk_fertig', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (delta_link,))

    def walk_eintraege(self):
        if not self.pfad.exists():
            return
        # Streamed on a connection of its own: a walk of a million entries
        # must not sit in memory, and a caller that writes through the
        # cached connection while iterating must not disturb the cursor.
        con = self._neu_verbinden()
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
        with con:
            con.execute("DELETE FROM walk")
            con.execute("DELETE FROM kv WHERE key IN "
                        "('walk_cursor', 'walk_fertig')")


class DbDoneLog:
    """Outlook's resume log – one row per finished mail, same interface as
    the historical DoneLog on exported.tsv.

    mark() runs once per exported mail, so this class keeps one connection
    open (WAL, synchronous=NORMAL) instead of reconnecting 45k times."""

    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._con = db._neu_verbinden(threadsafe=True)
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
        self._geaendert = {}
        self._geloescht = set()

    def merke(self, kennung, rel, ctag, groesse):
        with self._lock:
            eintrag = {"rel": rel, "ctag": ctag, "size": groesse}
            self.eintraege[kennung] = eintrag
            self._geaendert[kennung] = eintrag
            self._geloescht.discard(kennung)

    def vergiss(self, kennung):
        with self._lock:
            alt = self.eintraege.pop(kennung, None)
            if alt is not None:
                self._geloescht.add(kennung)
                self._geaendert.pop(kennung, None)
            return alt

    def schreibe(self, delta_link=None):
        """Persist what changed since the last write – rows, not the table."""
        with self._lock:
            geaendert, geloescht = self._geaendert, self._geloescht
            self._geaendert, self._geloescht = {}, set()
        self.db.bestand_aktualisieren(geaendert, geloescht, delta_link)


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
            return             # nothing to note means: the old pointer stands
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
