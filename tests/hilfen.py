"""Helpers shared by the test modules – one copy, imported where needed."""

import http.client
import json


def call(port, method, path, body=None, host=None):
    """One request against a test server; (status, json or text)."""
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if host:
        headers["Host"] = host
    con.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = con.getresponse()
    raw = r.read()
    con.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode("utf-8", "replace")


def call_kopf(port, method, path, body=None, host=None):
    """Like `call`, plus the response headers – for the ones that say
    something: `Location` on a create, `Allow` on a 405."""
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if host:
        headers["Host"] = host
    con.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = con.getresponse()
    raw = r.read()
    kopf = dict(r.getheaders())
    con.close()
    try:
        return r.status, json.loads(raw), kopf
    except ValueError:
        return r.status, raw.decode("utf-8", "replace"), kopf


def ohne_schluesselspalte(db):
    """Turn a store into one from before 11.0: the chunks table rebuilt
    without its key column. By hand rather than ALTER TABLE … DROP COLUMN –
    the SQLite that ships with CI's Python trips over the column comments
    in the table's SQL ("incomplete input"), the one on macOS does not."""
    import sqlite3
    con = sqlite3.connect(db)
    try:
        spalten = [r for r in con.execute("PRAGMA table_info(chunks)") if r[1] != "key"]
        defs = ", ".join(f"{r[1]} {r[2]}{' PRIMARY KEY' if r[5] else ''}{' NOT NULL' if r[3] else ''}"
                         for r in spalten)
        namen = ", ".join(r[1] for r in spalten)
        andere = [r[0] for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('index', 'trigger') "
            "AND tbl_name = 'chunks' AND sql IS NOT NULL AND name != 'ix_chunks_key'")]
        con.executescript(f"""
            CREATE TABLE chunks_alt({defs});
            INSERT INTO chunks_alt SELECT {namen} FROM chunks;
            DROP TABLE chunks;
            ALTER TABLE chunks_alt RENAME TO chunks;
        """)
        for sql in andere:
            con.execute(sql)
        con.commit()
    finally:
        con.close()
