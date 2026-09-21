"""
case_collect.py – the automatic searches, run: what lands in the case,
what stays out, what the step reports. The store and the case book are
the ones the MCP case tests use.
"""

import sys
import subprocess
from pathlib import Path

import faelle
import progress
import mcp_server
import case_collect
from tests.test_mcp_faelle import welt, _key, _eintrag, _nordwind  # noqa: F401
from tests.test_mcp_server import UID_M1, UID_M2, UID_T0

RES = Path(__file__).resolve().parent.parent
UID_M3 = "outlook:sent/protokoll.eml:0"


def _suche(k, n):
    return mcp_server._mit_kriterien(k, n, preview_chars=0)


def test_einsammeln_legt_neue_treffer_ab_und_laesst_entferntes_aus(welt):  # noqa: F811
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1)
    ordner = buch.ordner_anlegen(fid, "Belege")
    # M2 was in the case once and was taken out by hand
    buch.hinzufuegen(fid, [_eintrag(welt, UID_M2)])
    buch.entfernen(fid, _key(welt, UID_M2))
    sid = buch.speichern("Alle Mails", faelle.kriterien({"source": "outlook"}), fid, ordner, automatisch=True)
    zeilen = []
    bericht, summe = case_collect.einsammeln(buch, buch.automatische(), _suche,
                                             melde=lambda k, level="info", **v: zeilen.append((k, level, v)))
    assert summe == {"new": 1, "skipped": 1, "already": 1, "errors": 0}
    assert bericht[0]["search"] == "Alle Mails" and bericht[0]["case"] == "Nordwind" and bericht[0]["hits"] == 3
    assert bericht[0]["folder"] == "Belege" and bericht[0]["new"] == 1 and bericht[0]["skipped"] == 1
    e = {x["key"]: x for x in buch.eintraege(fid)}
    assert set(e) == {_key(welt, UID_M1), _key(welt, UID_M3)}
    neu = e[_key(welt, UID_M3)]
    assert neu["quelle"] == "auto" and neu["suche"] == sid and neu["ordner"] == ordner
    assert neu["titel"] == "Protokoll Quartalsplanung" and neu["wer"] == "Doris Docs" and neu["src"] == "outlook"
    g = buch.gespeichert(sid)
    assert g["auto_zuletzt"] and g["auto_neu"] == 1 and g["auto_uebersprungen"] == 1
    assert g["zuletzt"] and g["treffer"] == 3                 # it ran, like a run by hand
    assert [(z[0], z[1]) for z in zeilen] == [("run.collect.search", "info")]
    assert zeilen[0][2]["new"] == 1 and zeilen[0][2]["folder"] == "Belege"
    # a second pass: nothing new, nothing doubled, the run recorded all the same
    bericht, summe = case_collect.einsammeln(buch, buch.automatische(), _suche)
    assert summe["new"] == 0 and summe["already"] == 2 and len(buch.eintraege(fid)) == 2
    # a search that cannot run reports and moves on; the others still collect
    buch.speichern("Kaputt", faelle.kriterien({"q": "x", "case": 999}), fid, automatisch=True)
    bericht, summe = case_collect.einsammeln(buch, buch.automatische(), _suche)
    assert summe["errors"] == 1 and [z.get("error") is not None for z in bericht] == [False, True]
    # a wide search says so, and the first ones count
    bericht, summe = case_collect.einsammeln(buch, buch.automatische(fid)[:1], _suche, grenze=2,
                                             melde=lambda k, level="info", **v: zeilen.append((k, level, v)))
    assert ("run.collect.capped", "warn") in [(z[0], z[1]) for z in zeilen]


def test_eine_geschlossene_akte_und_ein_fehlender_ordner(welt):  # noqa: F811
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1)
    sid = buch.speichern("Mails", faelle.kriterien({"source": "outlook"}), fid, automatisch=True)
    buch.schliessen(fid)
    assert buch.automatische() == []
    # a one-off on a closed case: the case book refuses, the step reports
    bericht, summe = case_collect.einsammeln(buch, [buch.gespeichert(sid)], _suche)
    assert summe["errors"] == 1 and bericht[0]["error"] == "FallGeschlossen"


# --------------------------------------------------------------------------
# As the app runs it: a subprocess speaking the progress protocol
# --------------------------------------------------------------------------
def _lauf(welt, *extra):  # noqa: F811
    argv = [sys.executable, str(RES / "case_collect.py"), "--faelle", str(welt["buch"].pfad),
            "--store", str(welt["store"]), "--domains", "example.com", *extra]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=str(RES))


def _events(r):
    return [progress.lies_event(z) for z in r.stdout.splitlines() if z.startswith(progress.MARKE_LOG)]


def test_main_meldet_ueber_das_protokoll(welt):  # noqa: F811
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1)
    buch.speichern("Rechnungen", faelle.kriterien({"q": "Rechnung 4711"}), fid, automatisch=True)
    r = _lauf(welt)
    assert r.returncode == 0, r.stdout + r.stderr
    events = _events(r)
    assert [e["k"] for e in events] == ["run.collect.start", "run.collect.search", "run.collect.done"]
    assert events[0]["v"] == {"n": 1, "m": 1}
    assert events[1]["v"]["case"] == "Nordwind" and events[1]["v"]["search"] == "Rechnungen"
    assert events[1]["v"]["new"] >= 1 and events[2]["v"]["n"] == events[1]["v"]["new"]
    ergebnis = [progress.lies_ergebnis(z) for z in r.stdout.splitlines() if z.startswith(progress.MARKE_ERGEBNIS)]
    assert ergebnis[0]["new"] == events[1]["v"]["new"] and ergebnis[0]["extra"] == {"searches": 1}
    assert _key(welt, UID_T0) in buch.keys(fid)          # the chat that mentions the invoice
    # one search by id, switched off: a one-off
    aus = buch.speichern("Mails", faelle.kriterien({"source": "outlook"}), fid)
    r = _lauf(welt, "--search", str(aus))
    assert r.returncode == 0 and _key(welt, UID_M2) in buch.keys(fid)
    # not a text search: refused; no such search: refused
    sem = buch.speichern("Aehnlich", faelle.kriterien({"q": "x", "mode": "aehnlich"}), fid)
    r = _lauf(welt, "--search", str(sem))
    assert r.returncode == 1 and [e["k"] for e in _events(r)] == ["run.collect.notext"]
    r = _lauf(welt, "--search", "999")
    assert r.returncode == 1 and [e["k"] for e in _events(r)] == ["run.collect.nosearch"]
    # a closed case: nothing automatic, nothing to do
    buch.schliessen(fid)
    r = _lauf(welt)
    assert r.returncode == 0 and [e["k"] for e in _events(r)] == ["run.collect.nothing"]


def test_main_ohne_index(welt, tmp_path):  # noqa: F811
    buch = welt["buch"]
    fid = _nordwind(welt, UID_M1)
    buch.speichern("Rechnungen", faelle.kriterien({"q": "Rechnung"}), fid, automatisch=True)
    r = subprocess.run([sys.executable, str(RES / "case_collect.py"), "--faelle", str(buch.pfad),
                        "--store", str(tmp_path / "leer")], capture_output=True, text=True, timeout=120, cwd=str(RES))
    assert r.returncode == 1 and [e["k"] for e in _events(r)] == ["run.collect.noindex"]
    assert buch.keys(fid) == {_key(welt, UID_M1)}
