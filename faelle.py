#!/usr/bin/env python3
"""
faelle.py – the case book: cases, saved searches, the search history.

Three things that all live on the search side of the app and share one
shape, the **criteria** of a search – the words, the mode, the filters,
never the hits:

    history         every search that ran, by its criteria and its hit
                    count; kept for as long as the setting says and
                    cleared on request, never sent anywhere
    saved searches  criteria under a name; run again any time, or attached
                    to a case so the case knows what is new – and, switched
                    to automatic, so a run files what is new into the case
    cases           a matter someone works on: items from every source
                    (by their stable key, see schluessel.py), whole result
                    lists as they stood at one moment, saved searches, and
                    the casebook – short notes in order

A case points at the archive, it copies nothing. What it remembers about
an item is the key and enough to show a row when the item is gone from the
index: source, path, title, date, who. Closing a case makes it read-only;
reopening it lifts that; deleting a case deletes only the case.

Everything lives in one small SQLite file per profile (faelle.db), next to
the settings and the run history. Writes go through one connection each
and raise on a closed case (FallGeschlossen) – the app turns that into a
409.
"""

import json
import sqlite3
from datetime import datetime, UTC
from pathlib import Path

DB_NAME = "faelle.db"
OFFEN, ZU = "offen", "zu"
MODI = ("text", "aehnlich", "ki")
# The engine's names for the same three: the interface named them after
# what they do for the user, the engine after how it ranks. One table for
# both directions – the app's routes read it too.
ENGINE_MODUS = {"text": "lexical", "aehnlich": "semantic", "ki": "hybrid"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suchen(
    id        INTEGER PRIMARY KEY,
    wann      TEXT NOT NULL,             -- ISO, UTC
    kriterien TEXT NOT NULL,             -- JSON, normalised (kriterien())
    treffer   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_suchen_wann ON suchen(wann);
CREATE TABLE IF NOT EXISTS gespeichert(
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    kriterien TEXT NOT NULL,
    angelegt  TEXT NOT NULL,
    zuletzt   TEXT,                      -- last run
    treffer   INTEGER,                   -- hits then
    fall_id   INTEGER,                   -- attached to this case, or NULL
    ordner_id INTEGER,                   -- its new hits go into this folder
    automatisch INTEGER NOT NULL DEFAULT 0,  -- a run files its new hits by itself
    auto_zuletzt TEXT,                   -- when a run last did, and what it found:
    auto_neu INTEGER,                    --   filed into the case
    auto_uebersprungen INTEGER           --   left out, because removed by hand before
);
CREATE TABLE IF NOT EXISTS faelle(
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    beschreibung TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'offen',
    angelegt     TEXT NOT NULL,
    geaendert    TEXT NOT NULL,
    geschlossen  TEXT,
    exportiert   TEXT,                   -- where the last export went
    exportiert_wann TEXT
);
CREATE TABLE IF NOT EXISTS ordner(
    id       INTEGER PRIMARY KEY,
    fall_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    angelegt TEXT NOT NULL,
    UNIQUE(fall_id, name)
);
CREATE TABLE IF NOT EXISTS eintraege(
    id           INTEGER PRIMARY KEY,
    fall_id      INTEGER NOT NULL,
    key          TEXT NOT NULL,
    src          TEXT,
    root         TEXT,
    rel          TEXT,
    titel        TEXT,
    datum        TEXT,
    wer          TEXT,
    hinzugefuegt TEXT NOT NULL,
    liste_id     INTEGER,                -- came with this result list, or NULL
    ordner_id    INTEGER,                -- the folder inside the case, NULL = unsorted
    quelle       TEXT,                   -- who wrote it: 'ui' (the page), 'mcp' or 'auto'
    suche_id     INTEGER,                -- collected by this saved search ('auto')
    UNIQUE(fall_id, key)
);
CREATE INDEX IF NOT EXISTS ix_eintraege_key ON eintraege(key);
CREATE TABLE IF NOT EXISTS entfernt(
    fall_id INTEGER NOT NULL,            -- taken out of this case by hand:
    key     TEXT NOT NULL,               --   an automatic search leaves it out
    wann    TEXT NOT NULL,
    PRIMARY KEY(fall_id, key)
);
CREATE TABLE IF NOT EXISTS listen(
    id        INTEGER PRIMARY KEY,
    fall_id   INTEGER NOT NULL,
    kriterien TEXT NOT NULL,
    wann      TEXT NOT NULL,
    anzahl    INTEGER NOT NULL,
    ordner_id INTEGER                    -- filed into this folder as a whole
);
CREATE TABLE IF NOT EXISTS notizen(
    id      INTEGER PRIMARY KEY,
    fall_id INTEGER NOT NULL,
    wann    TEXT NOT NULL,
    text    TEXT NOT NULL,
    quelle  TEXT                         -- 'ui' or 'mcp'
);
"""

# 11.1 added columns to tables 11.0 created. They are added in place when
# the file is opened – a column, not a move of data; rows keep everything.
_ERGAENZUNGEN = (("gespeichert", "ordner_id", "INTEGER"), ("eintraege", "ordner_id", "INTEGER"),
                 ("eintraege", "quelle", "TEXT"), ("listen", "ordner_id", "INTEGER"),
                 ("notizen", "quelle", "TEXT"), ("eintraege", "bemerkung", "TEXT"),
                 # 13.3: the automatic search, and the search that filed an item
                 ("gespeichert", "automatisch", "INTEGER NOT NULL DEFAULT 0"),
                 ("gespeichert", "auto_zuletzt", "TEXT"), ("gespeichert", "auto_neu", "INTEGER"),
                 ("gespeichert", "auto_uebersprungen", "INTEGER"), ("eintraege", "suche_id", "INTEGER"))

UI, MCP, AUTO = "ui", "mcp", "auto"       # who wrote an item or a note: the page,
                                          # Claude, or an automatic search (case_collect)

_LEER = {"q": "", "mode": "text", "person": "", "source": "all", "from": "",
         "to": "", "folder": "", "filetype": "", "gone": False, "attachments": False,
         "fall": None, "ordner": None,
         "party": "all", "mail_from": "", "mail_to": "", "mail_cc": "", "mail_bcc": ""}
# The four lines of a mail (13.0). `from` and `to` were taken by the date
# range long before, hence the prefix.
MAIL = ("mail_from", "mail_to", "mail_cc", "mail_bcc")
PARTEIEN = ("internal", "external")       # the "party" filter's two narrowings


class FallGeschlossen(Exception):
    """A write to a closed case."""


class KeinFall(Exception):
    """No case with that id."""


class KeinOrdner(Exception):
    """No folder with that id in this case."""


def jetzt():
    return datetime.now(UTC).isoformat(timespec="seconds")


def kriterien(daten):
    """The criteria of a search in one shape, whatever came in: the API
    parameters, a stored JSON, a form. Unknown keys drop, missing ones are
    empty, the mode is one of three, `fall` an integer or None."""
    daten = daten or {}
    if isinstance(daten, str):
        try:
            daten = json.loads(daten)
        except ValueError:
            daten = {}
    out = dict(_LEER)
    for k in ("q", "person", "from", "to", "folder", "filetype", *MAIL):
        out[k] = str(daten.get(k) or "").strip()
    out["source"] = str(daten.get("source") or "all").strip() or "all"
    mode = str(daten.get("mode") or "text").strip()
    out["mode"] = mode if mode in MODI else _modus_vom_server(mode)
    for schalter in ("gone", "attachments"):
        wert = daten.get(schalter)
        out[schalter] = wert if isinstance(wert, bool) else str(wert).lower() in ("1", "true", "ja")
    party = str(daten.get("party") or "all").strip().lower()
    out["party"] = party if party in PARTEIEN else "all"
    fall = daten.get("fall", daten.get("case"))
    try:
        out["fall"] = int(fall) if fall not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        out["fall"] = None
    # The folder inside the case – only with a case.
    ordner = daten.get("ordner", daten.get("case_folder"))
    try:
        out["ordner"] = int(ordner) if out["fall"] and ordner not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        out["ordner"] = None
    return out


def _modus_vom_server(mode):
    return {v: k for k, v in ENGINE_MODUS.items()}.get(mode, "text")


def leer(k):
    """Nothing to remember: no words, no filter."""
    k = kriterien(k)
    return not (k["q"] or k["person"] or k["from"] or k["to"] or k["folder"]
                or k["filetype"] or k["gone"] or k["attachments"] or k["fall"]
                or k["source"] != "all" or any(k[m] for m in MAIL))


def auto_pruefen(k, fall_id, an):
    """May this search collect by itself? Only attached to a case (else
    there is nothing to file into) and only a text search – the two
    other kinds rank by likeness and would file noise unasked. Returns
    the flag as a bool; raises ValueError("auto_case") / ("auto_mode")."""
    an = bool(an)
    if not an:
        return False
    if fall_id is None:
        raise ValueError("auto_case")
    if (k or {}).get("mode", "text") != "text":
        raise ValueError("auto_mode")
    return True


def _json(k):
    return json.dumps(kriterien(k), ensure_ascii=False, sort_keys=True)


class Fallbuch:
    """The case book on one SQLite file. Reads never raise on a missing or
    empty file – the schema is created on first use."""

    def __init__(self, pfad):
        self.pfad = Path(pfad)

    def _connect(self):
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.pfad, timeout=5)
        con.row_factory = sqlite3.Row
        con.executescript(_SCHEMA)
        for tabelle, spalte, art in _ERGAENZUNGEN:
            if not any(r[1] == spalte for r in con.execute(f"PRAGMA table_info({tabelle})")):
                con.execute(f"ALTER TABLE {tabelle} ADD COLUMN {spalte} {art}")
        con.commit()
        return con

    # -- history -----------------------------------------------------------
    def suche_merken(self, k, treffer=None):
        """One search ran. The same criteria as the newest entry move that
        entry forward instead of piling up; nothing is kept for an empty
        search. Returns the row id, or None."""
        if leer(k):
            return None
        text = _json(k)
        con = self._connect()
        try:
            letzte = con.execute("SELECT id, kriterien FROM suchen ORDER BY wann DESC, id DESC LIMIT 1").fetchone()
            # Compared in today's shape: a row written before a criterion
            # existed (13.0 added the mail lines) still names the same search.
            if letzte and _json(letzte["kriterien"]) == text:
                con.execute("UPDATE suchen SET wann = ?, treffer = ?, kriterien = ? WHERE id = ?",
                            (jetzt(), treffer, text, letzte["id"]))
                con.commit()
                return letzte["id"]
            cur = con.execute("INSERT INTO suchen(wann, kriterien, treffer) VALUES(?,?,?)",
                              (jetzt(), text, treffer))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def suchen(self, limit=200):
        con = self._connect()
        try:
            return [{"id": r["id"], "wann": r["wann"], "kriterien": kriterien(r["kriterien"]),
                     "treffer": r["treffer"]}
                    for r in con.execute("SELECT * FROM suchen ORDER BY wann DESC, id DESC LIMIT ?",
                                         (max(1, int(limit)),))]
        finally:
            con.close()

    def suchen_leeren(self):
        con = self._connect()
        try:
            n = con.execute("DELETE FROM suchen").rowcount
            con.commit()
            return n
        finally:
            con.close()

    def aufraeumen(self, tage):
        """Drop history older than `tage` days; None keeps everything, 0
        keeps nothing. Returns what went."""
        if tage is None:
            return 0
        if int(tage) <= 0:
            return self.suchen_leeren()
        grenze = datetime.now(UTC).timestamp() - int(tage) * 86400
        stichtag = datetime.fromtimestamp(grenze, UTC).isoformat(timespec="seconds")
        con = self._connect()
        try:
            n = con.execute("DELETE FROM suchen WHERE wann < ?", (stichtag,)).rowcount
            con.commit()
            return n
        finally:
            con.close()

    # -- saved searches ----------------------------------------------------
    def speichern(self, name, k, fall_id=None, ordner_id=None, automatisch=False):
        name = str(name or "").strip()
        if not name:
            raise ValueError("name")
        automatisch = auto_pruefen(k, fall_id, automatisch)
        con = self._connect()
        try:
            if fall_id is not None:
                self._fall_offen(con, fall_id)
                ordner_id = self._ordner_pruefen(con, fall_id, ordner_id)
            else:
                ordner_id = None
            cur = con.execute(
                "INSERT INTO gespeichert(name, kriterien, angelegt, fall_id, ordner_id, automatisch) "
                "VALUES(?,?,?,?,?,?)", (name, _json(k), jetzt(), fall_id, ordner_id, int(automatisch)))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def gespeicherte(self, fall_id=None):
        con = self._connect()
        try:
            sql = ("SELECT g.*, f.name AS fall_name, o.name AS ordner_name FROM gespeichert g "
                   "LEFT JOIN faelle f ON f.id = g.fall_id LEFT JOIN ordner o ON o.id = g.ordner_id")
            params = ()
            if fall_id is not None:
                sql += " WHERE g.fall_id = ?"
                params = (fall_id,)
            return [self._gespeichert(r) for r in
                    con.execute(sql + " ORDER BY lower(g.name), g.id", params)]
        finally:
            con.close()

    def gespeichert(self, kennung):
        con = self._connect()
        try:
            r = con.execute("SELECT g.*, f.name AS fall_name, o.name AS ordner_name FROM gespeichert g "
                            "LEFT JOIN faelle f ON f.id = g.fall_id LEFT JOIN ordner o ON o.id = g.ordner_id "
                            "WHERE g.id = ?", (kennung,)).fetchone()
            return self._gespeichert(r) if r else None
        finally:
            con.close()

    @staticmethod
    def _gespeichert(r):
        return {"id": r["id"], "name": r["name"], "kriterien": kriterien(r["kriterien"]),
                "angelegt": r["angelegt"], "zuletzt": r["zuletzt"], "treffer": r["treffer"],
                "fall": r["fall_id"], "fall_name": r["fall_name"],
                "ordner": r["ordner_id"], "ordner_name": r["ordner_name"],
                # The automatic search (13.3): on or off, and what its last
                # collecting run did – None until one ran.
                "automatisch": bool(r["automatisch"]), "auto_zuletzt": r["auto_zuletzt"],
                "auto_neu": r["auto_neu"], "auto_uebersprungen": r["auto_uebersprungen"]}

    def umbenennen(self, kennung, name):
        name = str(name or "").strip()
        if not name:
            raise ValueError("name")
        con = self._connect()
        try:
            n = con.execute("UPDATE gespeichert SET name = ? WHERE id = ?", (name, kennung)).rowcount
            con.commit()
            return n > 0
        finally:
            con.close()

    def loeschen(self, kennung):
        con = self._connect()
        try:
            n = con.execute("DELETE FROM gespeichert WHERE id = ?", (kennung,)).rowcount
            con.commit()
            return n > 0
        finally:
            con.close()

    def gelaufen(self, kennung, treffer):
        con = self._connect()
        try:
            con.execute("UPDATE gespeichert SET zuletzt = ?, treffer = ? WHERE id = ?",
                        (jetzt(), treffer, kennung))
            con.commit()
        finally:
            con.close()

    def anhaengen(self, kennung, fall_id, ordner_id=None):
        """Attach a saved search to a case (None detaches), filing its new
        hits into the folder. Detached, it stops collecting: automatic
        is a thing between a search and its case."""
        con = self._connect()
        try:
            if fall_id is not None:
                self._fall_offen(con, fall_id)
                ordner_id = self._ordner_pruefen(con, fall_id, ordner_id)
            else:
                ordner_id = None
            n = con.execute("UPDATE gespeichert SET fall_id = ?, ordner_id = ?, "
                            "automatisch = CASE WHEN ? IS NULL THEN 0 ELSE automatisch END WHERE id = ?",
                            (fall_id, ordner_id, fall_id, kennung)).rowcount
            con.commit()
            return n > 0
        finally:
            con.close()

    def automatisch(self, kennung, an):
        """Switch a saved search's collecting on or off. On needs a case
        to file into and a text search (auto_pruefen); the search's last
        collecting run is forgotten with the switch-off. Returns whether
        the search exists."""
        con = self._connect()
        try:
            r = con.execute("SELECT kriterien, fall_id FROM gespeichert WHERE id = ?", (kennung,)).fetchone()
            if r is None:
                return False
            an = auto_pruefen(kriterien(r["kriterien"]), r["fall_id"], an)
            if an:
                con.execute("UPDATE gespeichert SET automatisch = 1 WHERE id = ?", (kennung,))
            else:
                con.execute("UPDATE gespeichert SET automatisch = 0, auto_zuletzt = NULL, "
                            "auto_neu = NULL, auto_uebersprungen = NULL WHERE id = ?", (kennung,))
            con.commit()
            return True
        finally:
            con.close()

    def automatische(self, fall_id=None):
        """The searches a collecting run works through: switched on and
        attached to an open case – all of them, or one case's."""
        con = self._connect()
        try:
            sql = ("SELECT g.*, f.name AS fall_name, o.name AS ordner_name FROM gespeichert g "
                   "JOIN faelle f ON f.id = g.fall_id LEFT JOIN ordner o ON o.id = g.ordner_id "
                   "WHERE g.automatisch = 1 AND f.status = 'offen'")
            params = ()
            if fall_id is not None:
                sql += " AND g.fall_id = ?"
                params = (fall_id,)
            return [self._gespeichert(r) for r in con.execute(sql + " ORDER BY f.id, lower(g.name), g.id", params)]
        finally:
            con.close()

    def eingesammelt(self, kennung, neu, uebersprungen):
        """A collecting run went through this search: when, and what it did."""
        con = self._connect()
        try:
            con.execute("UPDATE gespeichert SET auto_zuletzt = ?, auto_neu = ?, auto_uebersprungen = ? "
                        "WHERE id = ?", (jetzt(), int(neu), int(uebersprungen), kennung))
            con.commit()
        finally:
            con.close()

    # -- cases -------------------------------------------------------------
    def _fall_offen(self, con, fall_id):
        r = con.execute("SELECT status FROM faelle WHERE id = ?", (fall_id,)).fetchone()
        if r is None:
            raise KeinFall(fall_id)
        if r["status"] != OFFEN:
            raise FallGeschlossen(fall_id)

    def _beruehrt(self, con, fall_id):
        con.execute("UPDATE faelle SET geaendert = ? WHERE id = ?", (jetzt(), fall_id))

    def fall_anlegen(self, name, beschreibung=""):
        name = str(name or "").strip()
        if not name:
            raise ValueError("name")
        con = self._connect()
        try:
            wann = jetzt()
            cur = con.execute(
                "INSERT INTO faelle(name, beschreibung, status, angelegt, geaendert) "
                "VALUES(?,?,?,?,?)", (name, str(beschreibung or "").strip(), OFFEN, wann, wann))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def faelle(self, mit_geschlossenen=True):
        con = self._connect()
        try:
            sql = "SELECT * FROM faelle"
            if not mit_geschlossenen:
                sql += " WHERE status = 'offen'"
            out = []
            for r in con.execute(sql + " ORDER BY status = 'zu', geaendert DESC, id DESC"):
                out.append(self._fall_kurz(con, r))
            return out
        finally:
            con.close()

    def _fall_kurz(self, con, r):
        je_quelle = {}
        for s, n in con.execute("SELECT src, COUNT(*) FROM eintraege WHERE fall_id = ? GROUP BY src",
                                (r["id"],)):
            je_quelle[s or "?"] = n
        listen = con.execute("SELECT COUNT(*) FROM listen WHERE fall_id = ?", (r["id"],)).fetchone()[0]
        notizen = con.execute("SELECT COUNT(*) FROM notizen WHERE fall_id = ?", (r["id"],)).fetchone()[0]
        suchen = con.execute("SELECT COUNT(*) FROM gespeichert WHERE fall_id = ?", (r["id"],)).fetchone()[0]
        ordner = con.execute("SELECT COUNT(*) FROM ordner WHERE fall_id = ?", (r["id"],)).fetchone()[0]
        return {"id": r["id"], "name": r["name"], "beschreibung": r["beschreibung"],
                "status": r["status"], "angelegt": r["angelegt"], "geaendert": r["geaendert"],
                "geschlossen": r["geschlossen"],
                "exportiert": r["exportiert"], "exportiert_wann": r["exportiert_wann"],
                "eintraege": sum(je_quelle.values()), "je_quelle": je_quelle,
                "listen": listen, "notizen": notizen, "suchen": suchen, "ordner": ordner,
                # The folders themselves travel with the list: the page's
                # case filter and its choice windows name them.
                "ordner_liste": self._ordner(con, r["id"])}

    def fall(self, fall_id):
        """The whole case: its entries, result lists, notes and attached
        searches – None when there is no such case."""
        con = self._connect()
        try:
            r = con.execute("SELECT * FROM faelle WHERE id = ?", (fall_id,)).fetchone()
            if r is None:
                return None
            fall = self._fall_kurz(con, r)
            fall["eintraege_liste"] = [self._eintrag(e) for e in con.execute(
                "SELECT * FROM eintraege WHERE fall_id = ? ORDER BY src, datum DESC, id", (fall_id,))]
            fall["listen_liste"] = [self._liste(row) for row in con.execute(
                "SELECT * FROM listen WHERE fall_id = ? ORDER BY wann DESC, id DESC", (fall_id,))]
            fall["notizen_liste"] = [self._notiz(n) for n in con.execute(
                "SELECT * FROM notizen WHERE fall_id = ? ORDER BY wann DESC, id DESC", (fall_id,))]
            fall["suchen_liste"] = [self._gespeichert(g) for g in con.execute(
                "SELECT g.*, f.name AS fall_name, o.name AS ordner_name FROM gespeichert g "
                "LEFT JOIN faelle f ON f.id = g.fall_id LEFT JOIN ordner o ON o.id = g.ordner_id "
                "WHERE g.fall_id = ? ORDER BY lower(g.name)", (fall_id,))]
            return fall
        finally:
            con.close()

    def fall_aendern(self, fall_id, name=None, beschreibung=None):
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            if name is not None:
                name = str(name).strip()
                if not name:
                    raise ValueError("name")
                con.execute("UPDATE faelle SET name = ? WHERE id = ?", (name, fall_id))
            if beschreibung is not None:
                con.execute("UPDATE faelle SET beschreibung = ? WHERE id = ?",
                            (str(beschreibung).strip(), fall_id))
            self._beruehrt(con, fall_id)
            con.commit()
        finally:
            con.close()

    def schliessen(self, fall_id):
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            con.execute("UPDATE faelle SET status = ?, geschlossen = ?, geaendert = ? WHERE id = ?",
                        (ZU, jetzt(), jetzt(), fall_id))
            con.commit()
        finally:
            con.close()

    def export_vermerken(self, fall_id, pfad):
        """Where the case was last exported to – open or closed, the export
        changes nothing in the case, so it does not touch `geaendert`."""
        con = self._connect()
        try:
            n = con.execute("UPDATE faelle SET exportiert = ?, exportiert_wann = ? WHERE id = ?",
                            (str(pfad), jetzt(), fall_id)).rowcount
            con.commit()
            if not n:
                raise KeinFall(fall_id)
        finally:
            con.close()

    def oeffnen(self, fall_id):
        con = self._connect()
        try:
            if con.execute("SELECT 1 FROM faelle WHERE id = ?", (fall_id,)).fetchone() is None:
                raise KeinFall(fall_id)
            con.execute("UPDATE faelle SET status = ?, geschlossen = NULL, geaendert = ? WHERE id = ?",
                        (OFFEN, jetzt(), fall_id))
            con.commit()
        finally:
            con.close()

    def fall_loeschen(self, fall_id):
        """The case and what belongs to it – attached searches stay, detached."""
        con = self._connect()
        try:
            n = con.execute("DELETE FROM faelle WHERE id = ?", (fall_id,)).rowcount
            for tabelle in ("eintraege", "listen", "notizen", "ordner", "entfernt"):
                con.execute(f"DELETE FROM {tabelle} WHERE fall_id = ?", (fall_id,))
            con.execute("UPDATE gespeichert SET fall_id = NULL, automatisch = 0 WHERE fall_id = ?", (fall_id,))
            con.commit()
            return n > 0
        finally:
            con.close()

    # -- folders -----------------------------------------------------------
    # One level: every item sits in exactly one folder or in none
    # ("unsorted", ordner_id NULL). A folder is a name inside one case.
    @staticmethod
    def _ordner_pruefen(con, fall_id, ordner_id):
        """None passes; an id must name a folder of this case."""
        if ordner_id in (None, "", 0, "0"):
            return None
        ordner_id = int(ordner_id)
        if con.execute("SELECT 1 FROM ordner WHERE id = ? AND fall_id = ?",
                       (ordner_id, fall_id)).fetchone() is None:
            raise KeinOrdner(ordner_id)
        return ordner_id

    @staticmethod
    def _ordner(con, fall_id):
        return [{"id": o["id"], "name": o["name"], "angelegt": o["angelegt"],
                 "anzahl": con.execute("SELECT COUNT(*) FROM eintraege WHERE ordner_id = ?",
                                       (o["id"],)).fetchone()[0]}
                for o in con.execute("SELECT * FROM ordner WHERE fall_id = ? ORDER BY lower(name), id",
                                     (fall_id,))]

    def ordner(self, fall_id):
        con = self._connect()
        try:
            return self._ordner(con, fall_id)
        finally:
            con.close()

    def ordner_anlegen(self, fall_id, name):
        """A folder in an open case; the same name again is the same
        folder. Returns its id."""
        name = str(name or "").strip()
        if not name:
            raise ValueError("name")
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            da = con.execute("SELECT id FROM ordner WHERE fall_id = ? AND name = ?",
                             (fall_id, name)).fetchone()
            if da:
                return da["id"]
            cur = con.execute("INSERT INTO ordner(fall_id, name, angelegt) VALUES(?,?,?)",
                              (fall_id, name, jetzt()))
            self._beruehrt(con, fall_id)
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def ordner_umbenennen(self, fall_id, ordner_id, name):
        name = str(name or "").strip()
        if not name:
            raise ValueError("name")
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            ordner_id = self._ordner_pruefen(con, fall_id, ordner_id)
            if ordner_id is None:
                raise KeinOrdner(ordner_id)
            anderer = con.execute("SELECT id FROM ordner WHERE fall_id = ? AND name = ? AND id != ?",
                                  (fall_id, name, ordner_id)).fetchone()
            if anderer:
                raise ValueError("exists")
            con.execute("UPDATE ordner SET name = ? WHERE id = ?", (name, ordner_id))
            self._beruehrt(con, fall_id)
            con.commit()
        finally:
            con.close()

    def ordner_loeschen(self, fall_id, ordner_id):
        """The folder goes; what it held becomes unsorted – nothing leaves
        the case."""
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            ordner_id = self._ordner_pruefen(con, fall_id, ordner_id)
            if ordner_id is None:
                raise KeinOrdner(ordner_id)
            for tabelle in ("eintraege", "listen", "gespeichert"):
                con.execute(f"UPDATE {tabelle} SET ordner_id = NULL WHERE ordner_id = ?", (ordner_id,))
            con.execute("DELETE FROM ordner WHERE id = ?", (ordner_id,))
            self._beruehrt(con, fall_id)
            con.commit()
        finally:
            con.close()

    def verschieben(self, fall_id, keys, ordner_id):
        """Items into a folder (None: unsorted). Returns how many moved."""
        keys = [str(k) for k in (keys or ()) if k]
        if not keys:
            return 0
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            ordner_id = self._ordner_pruefen(con, fall_id, ordner_id)
            n = 0
            for s in range(0, len(keys), 500):
                teil = keys[s:s + 500]
                q = ",".join("?" * len(teil))
                n += con.execute(f"UPDATE eintraege SET ordner_id = ? WHERE fall_id = ? AND key IN ({q})",
                                 (ordner_id, fall_id, *teil)).rowcount
            if n:
                self._beruehrt(con, fall_id)
            con.commit()
            return n
        finally:
            con.close()

    # -- entries -----------------------------------------------------------
    @staticmethod
    def _eintrag(e):
        return {"id": e["id"], "key": e["key"], "src": e["src"], "root": e["root"],
                "rel": e["rel"], "titel": e["titel"], "datum": e["datum"], "wer": e["wer"],
                "hinzugefuegt": e["hinzugefuegt"], "liste": e["liste_id"],
                "ordner": e["ordner_id"], "quelle": e["quelle"] or UI,
                # The remark: one or two sentences on why the item is here
                "bemerkung": e["bemerkung"] or "",
                # The saved search that collected it ('auto'), else None
                "suche": e["suche_id"]}

    @staticmethod
    def _liste(row):
        return {"id": row["id"], "wann": row["wann"], "kriterien": kriterien(row["kriterien"]),
                "anzahl": row["anzahl"], "ordner": row["ordner_id"]}

    @staticmethod
    def _notiz(n):
        return {"id": n["id"], "wann": n["wann"], "text": n["text"], "quelle": n["quelle"] or UI}

    def hinzufuegen(self, fall_id, eintraege, liste_id=None, ordner_id=None, quelle=UI,
                    suche_id=None):
        """Add items to a case: dicts with key (required), src, root, rel,
        titel, datum, wer and optionally a bemerkung – into the folder,
        marked with who wrote them. An item already in the case is left as
        it is, folder, remark and all. Returns how many were new.

        Someone adding an item by hand (the page, Claude) means it: a mark
        that it was removed before goes. An automatic search (AUTO, with
        the `suche_id` that found it) is the other way round – it leaves
        out what was removed by hand, see `einsammeln`."""
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            ordner_id = self._ordner_pruefen(con, fall_id, ordner_id)
            wer = quelle if quelle in (MCP, AUTO) else UI
            neu, keys = 0, []
            for e in eintraege:
                key = str((e or {}).get("key") or "").strip()
                if not key:
                    continue
                keys.append(key)
                cur = con.execute(
                    "INSERT OR IGNORE INTO eintraege(fall_id, key, src, root, rel, titel, datum, "
                    "wer, hinzugefuegt, liste_id, ordner_id, quelle, bemerkung, suche_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fall_id, key, e.get("src"), e.get("root"), e.get("rel"),
                     e.get("titel") or e.get("title"), e.get("datum") or e.get("date"),
                     e.get("wer") or e.get("who"), jetzt(), liste_id, ordner_id,
                     wer, str(e.get("bemerkung") or "").strip(),
                     suche_id if wer == AUTO else None))
                neu += cur.rowcount
            if wer != AUTO and keys:
                self._marken_loeschen(con, fall_id, keys)
            if neu:
                self._beruehrt(con, fall_id)
            con.commit()
            return neu
        finally:
            con.close()

    def einsammeln(self, fall_id, eintraege, ordner_id, suche_id):
        """What an automatic search found, into the case – all but what
        was taken out of this case by hand (entfernt). Returns (new,
        left out)."""
        weg = self.entfernte(fall_id)
        dazu = [e for e in eintraege if str((e or {}).get("key") or "").strip() not in weg]
        neu = self.hinzufuegen(fall_id, dazu, ordner_id=ordner_id, quelle=AUTO, suche_id=suche_id)
        return neu, len(eintraege) - len(dazu)

    @staticmethod
    def _marken_setzen(con, fall_id, keys):
        """Removed by hand: an automatic search must not bring it back."""
        wann = jetzt()
        con.executemany("INSERT OR REPLACE INTO entfernt(fall_id, key, wann) VALUES(?,?,?)",
                        [(fall_id, k, wann) for k in keys])

    @staticmethod
    def _marken_loeschen(con, fall_id, keys):
        for s in range(0, len(keys), 500):
            teil = keys[s:s + 500]
            q = ",".join("?" * len(teil))
            con.execute(f"DELETE FROM entfernt WHERE fall_id = ? AND key IN ({q})", (fall_id, *teil))

    def entfernte(self, fall_id):
        """The keys taken out of a case by hand – what no automatic
        search puts back."""
        con = self._connect()
        try:
            return {r[0] for r in con.execute("SELECT key FROM entfernt WHERE fall_id = ?", (fall_id,))}
        finally:
            con.close()

    def bemerkung_setzen(self, fall_id, key, text):
        """The remark on one item – empty text removes it. Returns whether
        the case holds the item."""
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            n = con.execute("UPDATE eintraege SET bemerkung = ? WHERE fall_id = ? AND key = ?",
                            (str(text or "").strip(), fall_id, str(key or ""))).rowcount
            if n:
                self._beruehrt(con, fall_id)
            con.commit()
            return n > 0
        finally:
            con.close()

    def entfernen(self, fall_id, key):
        """An item out of the case – and marked, so that no automatic
        search files it again."""
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            n = con.execute("DELETE FROM eintraege WHERE fall_id = ? AND key = ?",
                            (fall_id, key)).rowcount
            if n:
                self._marken_setzen(con, fall_id, [str(key)])
                self._beruehrt(con, fall_id)
            con.commit()
            return n > 0
        finally:
            con.close()

    def eintraege(self, fall_id):
        con = self._connect()
        try:
            return [self._eintrag(e) for e in con.execute(
                "SELECT * FROM eintraege WHERE fall_id = ? ORDER BY id", (fall_id,))]
        finally:
            con.close()

    def keys(self, fall_id, ordner_id=None):
        """The keys of a case – of one folder with `ordner_id`."""
        con = self._connect()
        try:
            if ordner_id is None:
                return {r[0] for r in con.execute("SELECT key FROM eintraege WHERE fall_id = ?",
                                                  (fall_id,))}
            return {r[0] for r in con.execute(
                "SELECT key FROM eintraege WHERE fall_id = ? AND ordner_id = ?", (fall_id, int(ordner_id)))}
        finally:
            con.close()

    def zugehoerigkeit(self, keys):
        """key -> [{id, name, status}] of the cases holding it – the mark on
        a hit. One query for a page of hits."""
        keys = [k for k in (keys or ()) if k]
        if not keys:
            return {}
        con = self._connect()
        try:
            out = {}
            for s in range(0, len(keys), 500):
                teil = keys[s:s + 500]
                q = ",".join("?" * len(teil))
                for r in con.execute(
                        f"SELECT e.key, f.id, f.name, f.status, o.name FROM eintraege e "
                        f"JOIN faelle f ON f.id = e.fall_id LEFT JOIN ordner o ON o.id = e.ordner_id "
                        f"WHERE e.key IN ({q}) ORDER BY f.status = 'zu', f.name", teil):
                    out.setdefault(r[0], []).append({"id": r[1], "name": r[2], "status": r[3],
                                                     "ordner": r[4]})
            return out
        finally:
            con.close()

    def liste_anlegen(self, fall_id, k, treffer, ordner_id=None, quelle=UI):
        """A result list as it stands now: the criteria, dated, and its hits
        as entries of the case, filed into the folder. Returns (list id,
        hits new to the case)."""
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            ordner_id = self._ordner_pruefen(con, fall_id, ordner_id)
            cur = con.execute("INSERT INTO listen(fall_id, kriterien, wann, anzahl, ordner_id) VALUES(?,?,?,?,?)",
                              (fall_id, _json(k), jetzt(), len(treffer), ordner_id))
            con.commit()
            liste_id = cur.lastrowid
        finally:
            con.close()
        neu = self.hinzufuegen(fall_id, treffer, liste_id=liste_id, ordner_id=ordner_id, quelle=quelle)
        return liste_id, neu

    def liste(self, liste_id):
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM listen WHERE id = ?", (liste_id,)).fetchone()
            if row is None:
                return None
            out = self._liste(row)
            out["fall"] = row["fall_id"]
            out["treffer"] = [self._eintrag(e) for e in con.execute(
                "SELECT * FROM eintraege WHERE liste_id = ? ORDER BY id", (liste_id,))]
            return out
        finally:
            con.close()

    def liste_loeschen(self, fall_id, liste_id):
        """The list and the entries that came with it and only with it –
        removed by hand like any other, so they stay out of the case."""
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            n = con.execute("DELETE FROM listen WHERE id = ? AND fall_id = ?",
                            (liste_id, fall_id)).rowcount
            keys = [r[0] for r in con.execute("SELECT key FROM eintraege WHERE liste_id = ? AND fall_id = ?",
                                              (liste_id, fall_id))]
            con.execute("DELETE FROM eintraege WHERE liste_id = ? AND fall_id = ?",
                        (liste_id, fall_id))
            if keys:
                self._marken_setzen(con, fall_id, keys)
            if n:
                self._beruehrt(con, fall_id)
            con.commit()
            return n > 0
        finally:
            con.close()

    # -- casebook ----------------------------------------------------------
    def notiz(self, fall_id, text, quelle=UI):
        text = str(text or "").strip()
        if not text:
            raise ValueError("text")
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            cur = con.execute("INSERT INTO notizen(fall_id, wann, text, quelle) VALUES(?,?,?,?)",
                              (fall_id, jetzt(), text, MCP if quelle == MCP else UI))
            self._beruehrt(con, fall_id)
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def notizen(self, fall_id):
        con = self._connect()
        try:
            return [self._notiz(n) for n in con.execute(
                "SELECT * FROM notizen WHERE fall_id = ? ORDER BY wann DESC, id DESC", (fall_id,))]
        finally:
            con.close()

    def notiz_aendern(self, fall_id, notiz_id, text):
        text = str(text or "").strip()
        if not text:
            raise ValueError("text")
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            n = con.execute("UPDATE notizen SET text = ? WHERE id = ? AND fall_id = ?",
                            (text, notiz_id, fall_id)).rowcount
            if n:
                self._beruehrt(con, fall_id)
            con.commit()
            return n > 0
        finally:
            con.close()

    def notiz_loeschen(self, fall_id, notiz_id):
        con = self._connect()
        try:
            self._fall_offen(con, fall_id)
            n = con.execute("DELETE FROM notizen WHERE id = ? AND fall_id = ?",
                            (notiz_id, fall_id)).rowcount
            if n:
                self._beruehrt(con, fall_id)
            con.commit()
            return n > 0
        finally:
            con.close()
