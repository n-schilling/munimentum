"""
profile_db.py – the one way into the profile's database (faelle.db): the
cases, saved searches and history (faelle.py), the contacts added in the
app (own_contacts.py), the footers an index run keeps (footers.py) and
the notes on items (item_notes.py).

Several processes open that file: the app, the index run (it keeps the
footers), the case steps (case_collect, case_export) and the MCP server
(it reads cases, cards and notes, and adds to cases). So the rules are
the same for every table, and they live here once:

- Writing (`connect`) creates what is missing – tables, and columns a
  later release added to a table an older one made – and waits as long as
  any writer of the file does (TIMEOUT): a short wait beside a long one
  made the short one fail with "database is locked".
- Reading (`rows`) creates nothing: a reader beside the app, or a profile
  from before a table existed, finds none. No file and no table are
  "nothing there"; any other trouble – a file another process holds
  locked, one that is no database – is an error and says so. Read as
  "nothing there" it would show an empty note a save then writes over,
  or footers an index run reads all over again.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

TIMEOUT = 30            # seconds a connection waits for the file another process writes


class BadValue(ValueError):
    """A field's value is not what the field takes; `field` names it."""

    def __init__(self, field):
        super().__init__(field)
        self.field = field


def now():
    """This moment, as every table here stamps it (UTC, ISO 8601)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path, schema, columns=()):
    """A connection to write through: the folder and the tables in
    `schema` made where missing, and each of `columns` – (table, column,
    declaration) – added to a table that lacks it: a column, not a move of
    data; rows keep everything. Rows readable by name."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=TIMEOUT)
    con.row_factory = sqlite3.Row
    con.executescript(schema)
    for table, column, declaration in columns:
        if not any(row[1] == column for row in con.execute(f"PRAGMA table_info({table})")):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    if columns:
        con.commit()
    return con


def rows(path, query, args=()):
    """The rows of a read that creates nothing – [] while the file or the
    table is not there; anything else raises."""
    path = Path(path)
    if not path.is_file():
        return []
    # As a URI, escaped: a folder named "Archive #2" must not end the path at its "#".
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=TIMEOUT)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(query, args).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise
    finally:
        con.close()
