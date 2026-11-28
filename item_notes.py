"""
item_notes.py – a note of the keeper's own on any item of the archive.

Whatever the item is and wherever it came from – a mail from Microsoft
365, a file added by hand – the keeper can say something about it: where
it came from, what it is for. One note per item, kept in the profile's
database (faelle.db, table `item_notes`) under the item's stable key
(schluessel.py), so it stays with the item through a rename or a move.
Never in an export folder: the note is the keeper's word about the item,
not part of it, and not in the evidence chain.

A note has a history: what a change or a removal replaces is kept
(`item_note_versions`), newest first.

Apart from the cases: a case's remark says why an item lies in that
case; a note says something about the item itself, in every place it
appears.
"""

from pathlib import Path

import profile_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_notes(
    key     TEXT PRIMARY KEY,       -- the item's stable key
    text    TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_note_versions(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    key      TEXT NOT NULL,
    text     TEXT NOT NULL,          -- the note as it was
    written  TEXT NOT NULL,          -- when that text was written
    replaced TEXT NOT NULL           -- when it stopped being the note
);
CREATE INDEX IF NOT EXISTS item_note_versions_key ON item_note_versions(key, id);
"""

MOST = 20000                    # characters of one note
VERSIONS_MOST = 200             # versions listed for one item


BadValue = profile_db.BadValue    # not a note, or not an item's key
now = profile_db.now


def clean_key(key):
    key = key.strip() if isinstance(key, str) else ""
    if not key or len(key) > 2000:
        raise BadValue("key")
    return key


def clean_text(text):
    if not isinstance(text, str) or len(text) > MOST:
        raise BadValue("text")
    return text.strip()


class ItemNotes:
    """The notes of one profile."""

    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        return profile_db.connect(self.path, _SCHEMA)

    def _read(self, query, args=()):
        # Creates nothing; no table is no note, a locked file an error (profile_db).
        return profile_db.rows(self.path, query, args)

    def get(self, key):
        """The item's note – `text` "" and `updated` None when it has
        none – and how many earlier versions are kept."""
        key = clean_key(key)
        rows = self._read("SELECT (SELECT text FROM item_notes WHERE key = ?), "
                          "(SELECT updated FROM item_notes WHERE key = ?), "
                          "(SELECT COUNT(*) FROM item_note_versions WHERE key = ?)", (key, key, key))
        text, updated, kept = tuple(rows[0]) if rows else (None, None, 0)
        return {"key": key, "text": text or "", "updated": updated, "versions": kept or 0}

    def put(self, key, text):
        """Write the item's note; an empty text takes it away. What it
        replaces becomes an earlier version; the same text again changes
        nothing, not even the note's date."""
        key, text = clean_key(key), clean_text(text)
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            before = con.execute("SELECT text, updated FROM item_notes WHERE key = ?", (key,)).fetchone()
            if (before["text"] if before else "") != text:
                stamp = now()
                if before:
                    con.execute("INSERT INTO item_note_versions(key, text, written, replaced) VALUES(?,?,?,?)",
                                (key, before["text"], before["updated"], stamp))
                if text:
                    con.execute("INSERT OR REPLACE INTO item_notes(key, text, updated) VALUES(?,?,?)",
                                (key, text, stamp))
                else:
                    con.execute("DELETE FROM item_notes WHERE key = ?", (key,))
            con.commit()
        finally:
            con.close()
        return self.get(key)

    def keys(self):
        """The keys of every item that carries a note."""
        return [row[0] for row in self._read("SELECT key FROM item_notes ORDER BY key")]

    def versions(self, key):
        """What the note said before, newest first: the text, when it was
        written and when it was replaced."""
        return [{"text": row["text"], "written": row["written"], "replaced": row["replaced"]}
                for row in self._read("SELECT text, written, replaced FROM item_note_versions "
                                      "WHERE key = ? ORDER BY id DESC LIMIT ?",
                                      (clean_key(key), VERSIONS_MOST))]
