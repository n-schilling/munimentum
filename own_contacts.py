"""
own_contacts.py – the contacts the user added to the address book in the
app itself.

The Outlook address book is what Microsoft holds and the export copies; a
person the user writes with every week is often on no card there. Such a
person can be added here: a card of the app's own, begun from what the
mail says (the name the address writes under, the numbers and links of
its footer) and completed by hand – title, company, department, numbers,
links, a note.

A card of the app's own is never a file in an export folder: everything
there is what Microsoft delivered, and the evidence chain holds it to
that. The cards live in the profile's small database (faelle.db, beside
the cases), one row per mail address – a person is an address. An Outlook
card for the same address always wins: the app's own card stays in the
table and is not shown while Outlook has one.

An address can also be set to be ignored (ignored_contacts): a shared
mailbox, a newsletter the mail's rules did not tell from a person, or
somebody the user does not want a card for. It stays what the mail says
of it and is no longer listed among those the address book lacks.

A card has a history: what a change, a footer read again or a removal
replaces is kept as an earlier version (own_contact_versions), by address
– a card added again for the same address continues it. The history lies
beside the cards, not in the evidence chain: that one holds what
Microsoft delivered.
"""

import json
import re
import sqlite3
from pathlib import Path

import profile_db

KINDS = ("phone", "mobile", "fax")
TEXT_MOST = 200         # a name, a title, a company, a department, a number, a link
NOTE_MOST = 4000
LIST_MOST = 20          # numbers, links
ORIGIN = "munimentum"   # what a reader is told such a card comes from
REASONS = ("changed", "reread", "removed")      # what replaced a version
VERSIONS_MOST = 200     # earlier versions one answer lists, newest first

# What the mail lines call an address (correspondents._ADDRESS): whoever
# is listed there can get a card.
# … a sender on a host without a dot among them ("root@server01").
_ADDRESS = re.compile(r"^[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)*$")
_TEXTS = ("name", "title", "company", "department", "note")
_KEPT = ("name", "title", "company", "department", "phones", "urls", "note", "signature")
_CONTROL = re.compile("[\x00-\x09\x0b-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS own_contacts(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- never given twice: a removed card's id names nobody else
    address     TEXT NOT NULL UNIQUE,    -- lower case: who this is
    name        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    department  TEXT NOT NULL DEFAULT '',
    phones      TEXT NOT NULL DEFAULT '[]',   -- JSON: [{"type", "number"}]
    urls        TEXT NOT NULL DEFAULT '[]',   -- JSON: [link]
    note        TEXT NOT NULL DEFAULT '',
    signature   TEXT NOT NULL DEFAULT '{}',   -- JSON: the footer as last read
    created     TEXT NOT NULL,
    updated     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS own_contact_versions(
    id          INTEGER PRIMARY KEY,
    address     TEXT NOT NULL,           -- whose card this was
    replaced    TEXT NOT NULL,           -- when it stopped being the card
    reason      TEXT NOT NULL,           -- one of REASONS
    card        TEXT NOT NULL            -- JSON: the fields of _KEPT as they were
);
CREATE INDEX IF NOT EXISTS own_contact_versions_address ON own_contact_versions(address, id);
CREATE TABLE IF NOT EXISTS ignored_contacts(
    address     TEXT PRIMARY KEY,        -- lower case: who is not to be offered for the address book
    since       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ignored_names(
    name        TEXT PRIMARY KEY,        -- an entry without an address, by the name it stands under
    since       TEXT NOT NULL
);
"""


class Exists(Exception):
    """The address has a card of the app's own already."""


BadValue = profile_db.BadValue    # a field's value is not what the field takes
now = profile_db.now


def clean_address(value):
    address = str(value or "").strip().lower()
    if not _ADDRESS.match(address) or len(address) > TEXT_MOST:
        raise BadValue("address")
    return address


def clean_name(value):
    """A name as the ignore list keeps it: its words, single-spaced."""
    name = " ".join(value.split()) if isinstance(value, str) else ""
    if not name or len(name) > 200:
        raise BadValue("name")
    return name


def _text(field, value, most):
    if not isinstance(value, str):
        raise BadValue(field)
    # No control characters, no marks that turn the writing direction: a
    # card is read by people and handed to MCP clients as it stands.
    if field == "note":
        value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    value = _CONTROL.sub(" " if field != "note" else "", value)
    value = " ".join(value.split()) if field != "note" else value.strip()
    if len(value) > most:
        raise BadValue(field)
    return value


def clean(fields):
    """The fields of a card as they are stored – checked whole before
    anything is written: texts of bounded length, numbers each with a kind
    of KINDS, links as text. An unknown field is a bad value too."""
    out = {}
    for field, value in (fields or {}).items():
        if field in _TEXTS:
            out[field] = _text(field, value, NOTE_MOST if field == "note" else TEXT_MOST)
            if field == "name" and not out[field]:
                raise BadValue("name")
        elif field == "phones":
            if not isinstance(value, list) or len(value) > LIST_MOST:
                raise BadValue(field)
            phones = []
            for row in value:
                if not isinstance(row, dict) or row.get("type") not in KINDS:
                    raise BadValue(field)
                number = _text(field, row.get("number"), TEXT_MOST)
                if number:
                    phones.append({"type": row["type"], "number": number})
            out[field] = phones
        elif field == "urls":
            if not isinstance(value, list) or len(value) > LIST_MOST:
                raise BadValue(field)
            urls = [_text(field, row, TEXT_MOST) for row in value]
            out[field] = list(dict.fromkeys(u for u in urls if u))
        else:
            raise BadValue(field)
    return out


def footer(found):
    """What is kept of a footer as it was read (signature.read's answer,
    or the MCP tool's row): its lines, what they rest on, the mail they
    stand in, and when it was read. Nothing read (no index, no mail file)
    is no reading: no lines and no day."""
    if not found:
        return {"lines": []}
    kept = {"lines": [str(line) for line in found.get("lines") or []], "read": now()}
    for key in ("basis", "uid", "date"):
        if found.get(key):
            kept[key] = str(found[key])
    return kept


def _merged(have, new, key):
    seen = {key(row) for row in have}
    return have + [row for row in new if key(row) not in seen and not seen.add(key(row))]


def _digits(row):
    return "".join(ch for ch in row["number"] if ch.isdigit())


def _said(card):
    """What a card says, field by field – of the footer its lines and
    what they rest on: the same lines read again, on another day or from
    a newer mail, are no change."""
    said = {field: card.get(field) for field in _KEPT}
    said["signature"] = {k: v for k, v in (said["signature"] or {}).items() if k in ("lines", "basis")}
    return said


_EMPTY = {"name": "", "title": "", "company": "", "department": "", "phones": [], "urls": [],
          "note": "", "signature": {"lines": []}}


def _differing(earlier, later):
    earlier, later = _said(earlier), _said(later)
    return [field for field in _KEPT if earlier[field] != later[field]]


def _keep(con, card, reason):
    con.execute("INSERT INTO own_contact_versions(address, replaced, reason, card) VALUES(?,?,?,?)",
                (card["address"], now(), reason,
                 json.dumps({field: card[field] for field in _KEPT}, ensure_ascii=False)))


class OwnContacts:
    """The table on the profile's database file. Reads never raise on a
    missing file – the table is created on first use."""

    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        return profile_db.connect(self.path, _SCHEMA)

    def _kept(self):
        """How many earlier versions each address has – none while the
        table is not there (a read creates nothing)."""
        return {row[0]: row[1] for row in profile_db.rows(
            self.path, "SELECT address, COUNT(*) FROM own_contact_versions GROUP BY address")}

    @staticmethod
    def _card(row, kept=None):
        return {"id": row["id"], "address": row["address"], "name": row["name"],
                "versions": (kept or {}).get(row["address"], 0),
                "title": row["title"], "company": row["company"],
                "department": row["department"],
                "phones": json.loads(row["phones"] or "[]"),
                "urls": json.loads(row["urls"] or "[]"), "note": row["note"],
                "signature": json.loads(row["signature"] or "{}"),
                "created": row["created"], "updated": row["updated"]}

    def all(self):
        """Every card, by name. Reads without creating anything – an MCP
        process reads here too; no file or table is no card, a locked file
        an error ("none" would make the cards disappear from the page)."""
        cards = profile_db.rows(self.path, "SELECT * FROM own_contacts ORDER BY name COLLATE NOCASE, address")
        kept = self._kept() if cards else {}
        return [self._card(r, kept) for r in cards]

    def one(self, ident):
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM own_contacts WHERE id = ?", (ident,)).fetchone()
            if row is None:
                return None
            kept = con.execute("SELECT COUNT(*) FROM own_contact_versions WHERE address = ?",
                               (row["address"],)).fetchone()[0]
            return self._card(row, {row["address"]: kept})
        finally:
            con.close()

    def add(self, address, name="", found=None):
        """A new card for `address`, begun from what the mail says: the
        name it writes under and its footer's numbers and links (`found`).
        Raises Exists for an address that has one."""
        address = clean_address(address)
        fields = clean({"name": str(name or "").strip() or address,
                        "phones": [p for p in (found or {}).get("phones") or []
                                   if isinstance(p, dict) and p.get("type") in KINDS][:LIST_MOST],
                        "urls": [u for u in (found or {}).get("urls") or []
                                 if isinstance(u, str) and len(u) <= TEXT_MOST][:LIST_MOST]})
        con = self._connect()
        try:
            stamp = now()
            try:
                cur = con.execute(
                    "INSERT INTO own_contacts(address, name, phones, urls, signature, created, updated) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (address, fields["name"], json.dumps(fields["phones"], ensure_ascii=False),
                     json.dumps(fields["urls"], ensure_ascii=False),
                     json.dumps(footer(found), ensure_ascii=False), stamp, stamp))
            except sqlite3.IntegrityError:
                raise Exists(address) from None
            # A card says the opposite of "ignore": the mark goes with it.
            con.execute("DELETE FROM ignored_contacts WHERE address = ?", (address,))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def change(self, ident, fields):
        """Set the named fields – all or none. False for a card that is
        not there."""
        fields = clean(fields)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")       # what is read here is what the write replaces
            row = con.execute("SELECT * FROM own_contacts WHERE id = ?", (ident,)).fetchone()
            if row is None:
                return False
            card = self._card(row)
            if not _differing(card, {**card, **fields}):
                return True                      # saved as it stood: no version, no new day
            _keep(con, card, "changed")
            for field, value in fields.items():
                stored = json.dumps(value, ensure_ascii=False) if field in ("phones", "urls") else value
                con.execute(f"UPDATE own_contacts SET {field} = ? WHERE id = ?", (stored, ident))
            con.execute("UPDATE own_contacts SET updated = ? WHERE id = ?", (now(), ident))
            con.commit()
            return True
        finally:
            con.close()

    def reread(self, ident, found):
        """The footer read again: its lines replace the kept ones, and a
        number or a link the card does not have yet joins it. Nothing the
        user typed is taken away or overwritten – and a reading that finds
        no footer (the newest mails are short answers) leaves the kept
        one as it is. False for a card that is not there."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")       # what is read here is what the write replaces
            row = con.execute("SELECT * FROM own_contacts WHERE id = ?", (ident,)).fetchone()
            if row is None:
                return False
            card = self._card(row)
            phones = _merged(card["phones"],
                             [p for p in (found or {}).get("phones") or []
                              if isinstance(p, dict) and p.get("type") in KINDS], _digits)[:LIST_MOST]
            urls = _merged(card["urls"], [u for u in (found or {}).get("urls") or []
                                          if isinstance(u, str) and len(u) <= TEXT_MOST],
                           lambda u: u)[:LIST_MOST]
            kept = footer(found) if (found or {}).get("lines") or not card["signature"].get("lines") \
                else card["signature"]
            if _differing(card, {**card, "phones": phones, "urls": urls, "signature": kept}):
                _keep(con, card, "reread")
            con.execute("UPDATE own_contacts SET phones = ?, urls = ?, signature = ?, updated = ? "
                        "WHERE id = ?",
                        (json.dumps(phones, ensure_ascii=False), json.dumps(urls, ensure_ascii=False),
                         json.dumps(kept, ensure_ascii=False), now(), ident))
            con.commit()
            return True
        finally:
            con.close()

    def remove(self, ident):
        """Take the card out of the address book. What it said last stays
        as an earlier version, for a card added again for the address."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")       # what is read here is what the write replaces
            row = con.execute("SELECT * FROM own_contacts WHERE id = ?", (ident,)).fetchone()
            if row is None:
                return False
            _keep(con, self._card(row), "removed")
            con.execute("DELETE FROM own_contacts WHERE id = ?", (ident,))
            con.commit()
            return True
        finally:
            con.close()

    # -- the addresses not to be offered for the address book ----------------
    def ignored(self):
        """The ignored addresses, sorted. Reads without creating anything."""
        return [row[0] for row in profile_db.rows(self.path, "SELECT address FROM ignored_contacts ORDER BY address")]

    def ignore(self, address, on=True):
        """Set an address to be ignored, or (`on` False) no longer."""
        address = clean_address(address)
        con = self._connect()
        try:
            if on:
                con.execute("INSERT OR IGNORE INTO ignored_contacts(address, since) VALUES(?,?)", (address, now()))
            else:
                con.execute("DELETE FROM ignored_contacts WHERE address = ?", (address,))
            con.commit()
        finally:
            con.close()

    # … and the names: an entry the communication makes without an address
    # (a chat partner, a system's sender name) is ignored by its name.
    def ignored_names(self):
        """The ignored names, sorted. Reads without creating anything."""
        return [row[0] for row in profile_db.rows(self.path, "SELECT name FROM ignored_names ORDER BY name")]

    def ignore_name(self, name, on=True):
        """Set a name to be ignored, or (`on` False) no longer."""
        name = clean_name(name)
        con = self._connect()
        try:
            if on:
                con.execute("INSERT OR IGNORE INTO ignored_names(name, since) VALUES(?,?)", (name, now()))
            else:
                con.execute("DELETE FROM ignored_names WHERE name = ?", (name,))
            con.commit()
        finally:
            con.close()

    def versions(self, ident):
        """The earlier versions of a card, newest first: when each was
        replaced, by what (REASONS), the card as it was, and `changed` –
        the fields in which it differs from what came after it. A version
        a removal kept has no successor – the card added later began anew
        – and names every field that held something.
        None for a card that is not there."""
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM own_contacts WHERE id = ?", (ident,)).fetchone()
            if row is None:
                return None
            after, out = self._card(row), []
            for kept in con.execute(
                    "SELECT replaced, reason, card FROM own_contact_versions WHERE address = ? "
                    "ORDER BY id DESC LIMIT ?", (after["address"], VERSIONS_MOST)):
                card = json.loads(kept["card"])
                # What a removed card said went as a whole: every field
                # that held something, as against an empty card.
                out.append({"replaced": kept["replaced"], "reason": kept["reason"], "card": card,
                            "changed": _differing(card, _EMPTY if kept["reason"] == "removed" else after)})
                after = card
            return out
        finally:
            con.close()
