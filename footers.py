"""
footers.py – the footer under each contact's mails, read ahead and kept.

The address book marks who has a footer and shows it without reading a
mail first. So the footers are read once per index run (rag_index.py
calls `fill` at its end) for everyone the address book lists, and kept
in the profile's database (faelle.db, table `footers`) – one row per
address: the lines, what they rest on, the mail they stand in, and when
they were read. An address without new mail since is not read again; one
no footer was found for keeps its empty row, so it is not asked on every
run either.

What is kept is a reading of that moment: the page says when it was
taken. The profile's database, never an export folder – the evidence
chain would call a file there a write from outside.
"""

import json
import sqlite3
from pathlib import Path

import correspondents
import faelle
import own_contacts

_SCHEMA = """
CREATE TABLE IF NOT EXISTS footers(
    address TEXT PRIMARY KEY,
    lines   TEXT NOT NULL,      -- JSON list, empty when no footer was found
    basis   TEXT,               -- "repeated" or "single"
    uid     TEXT,               -- the mail the footer was read from
    date    TEXT,               -- that mail's day
    read    TEXT NOT NULL,      -- when it was read
    newest  REAL,               -- the address's newest mail at that time
    mails   INTEGER             -- how many mails it had sent then
);
"""

BATCH = 25                      # addresses read at a time (get_signatures)


def listed(people, most):
    """Of the correspondents (correspondents.people) those the address
    book lists: everyone with a card, and of the rest the `most` with the
    most mail – and every frequent contact, whatever the cap."""
    out, beyond = [], 0
    for person in people:
        if not person.get("card"):
            if beyond >= most and not person["frequent"]:
                continue
            beyond += 1
        out.append(person)
    return out


def asked(person):
    """The address a person's footer is read under: the one their mail
    comes from. None for someone who never wrote."""
    if not person.get("received") or not person.get("addresses"):
        return None
    return person.get("writes_from") or person["addresses"][0]


class Footers:
    """The kept footers of one profile."""

    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        con.executescript(_SCHEMA)
        return con

    def _read(self, query, args=()):
        """Rows without creating anything: a reader beside the app (the
        MCP process, a page before the first run) finds no table."""
        if not self.path.is_file():
            return []
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(query, args).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()

    def found(self):
        """The addresses a footer is kept for."""
        return {row["address"] for row in
                self._read("SELECT address FROM footers WHERE lines <> '[]'")}

    def stamps(self):
        """address → (newest mail, mails) as of the reading."""
        return {row["address"]: (row["newest"], row["mails"]) for row in
                self._read("SELECT address, newest, mails FROM footers")}

    def one(self, address):
        rows = self._read("SELECT * FROM footers WHERE address = ?",
                          (own_contacts.clean_address(address),))
        if not rows:
            return None
        row = dict(rows[0])
        out = {"lines": json.loads(row["lines"]), "read": row["read"]}
        out.update({k: row[k] for k in ("basis", "uid", "date") if row[k]})
        return out

    def keep(self, address, found, stamp=(None, None)):
        """Keep what was read under `address` (an entry of get_signatures).
        True when the lines are other than the ones kept before."""
        address = own_contacts.clean_address(address)
        lines = json.dumps(list(found.get("lines") or []), ensure_ascii=False)
        has = bool(found.get("lines"))
        con = self._connect()
        try:
            with con:
                before = con.execute("SELECT lines FROM footers WHERE address = ?",
                                     (address,)).fetchone()
                con.execute(
                    "INSERT OR REPLACE INTO footers VALUES (?,?,?,?,?,?,?,?)",
                    (address, lines, found.get("basis") if has else None,
                     found.get("uid") if has else None, found.get("date") if has else None,
                     own_contacts.now(), stamp[0], stamp[1]))
        finally:
            con.close()
        return (before["lines"] if before else "[]") != lines


def _stamps(con, addresses):
    """address → (newest mail, mails sent), from the index."""
    if not addresses or not correspondents._has_table(con):
        return {}
    out = {}
    wanted = sorted(addresses)
    for start in range(0, len(wanted), 500):
        part = wanted[start:start + 500]
        marks = ",".join("?" * len(part))
        for sender, newest, mails in con.execute(
                f"SELECT sender, MAX(ts), COUNT(*) FROM correspondence "
                f"WHERE sender IN ({marks}) GROUP BY sender", part):
            out[sender] = (newest, mails)
    return out


def fill(home, store, outlook, most=500, months=12, mails=12):
    """Read the footers of everyone the address book lists who has mail
    newer than the last reading, and keep them. Returns (read, changed,
    with a footer among those read), or None when there is nothing to
    read from: no index, an index from before the mail lines, no mail
    files at hand."""
    import mcp_server
    import store_layout
    db = store_layout.db_path(store)
    if not db.exists():
        return None
    book = Footers(Path(home) / faelle.DB_NAME)
    mcp_server.STATE.update(db=str(db), V=None, np=None, semantic=False, vector_dtype=None,
                            outlook_dir=str(outlook), home=str(home),
                            faelle_db=str(book.path))
    people = mcp_server.correspondent_people(months, mails)
    if people is None:
        return None
    wanted = [a for a in (asked(p) for p in listed(people, most)) if a]
    con = mcp_server._db()
    try:
        now = _stamps(con, wanted)
    finally:
        con.close()
    kept = book.stamps()
    due = [a for a in wanted if a not in kept or tuple(kept[a]) != tuple(now.get(a, (None, None)))]
    read = changed = with_footer = 0
    for start in range(0, len(due), BATCH):
        res = mcp_server.get_signatures(due[start:start + BATCH])
        if res.get("error"):
            return None if not read else (read, changed, with_footer)
        for entry in res["signatures"]:
            read += 1
            with_footer += bool(entry["lines"])
            changed += book.keep(entry["address"], entry, now.get(entry["address"], (None, None)))
    return read, changed, with_footer
