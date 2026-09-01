"""analytics_db.py – the materialised analytics block: read paths that must
never crash a page. The aggregation itself is covered in test_app.py against
real corpus stores."""

import sqlite3

import analytics_db


def test_lies_ohne_index_und_ohne_block(tmp_path):
    # Kein Index: nichts da, kein Krach.
    assert analytics_db.lies(tmp_path) is None
    assert analytics_db.baue(tmp_path, {}) is None
    # Index einer älteren Fassung: corpus.db ohne analytics-Tabelle.
    con = sqlite3.connect(tmp_path / "corpus.db")
    con.execute("CREATE TABLE chunks(uid TEXT, seq INTEGER, src TEXT, ts REAL)")
    con.close()
    assert analytics_db.lies(tmp_path) is None


def test_kaputter_block_liest_sich_als_fehlend(tmp_path):
    con = sqlite3.connect(tmp_path / "corpus.db")
    con.execute("CREATE TABLE analytics(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO analytics VALUES ('payload', '{kaputt')")
    con.commit()
    con.close()
    assert analytics_db.lies(tmp_path) is None
