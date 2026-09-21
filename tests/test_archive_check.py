"""archive_check.py – the bookkeeping against the disk, and the index
against the archive. Everything here is built on disk in miniature; no
Graph, no network, and the check must never write anything but its
report."""

import json
import sqlite3

import archive_check as ac
import completeness
import export_util
import progress
import state_db


def _datei(pfad, inhalt=b"x"):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_bytes(inhalt)
    return pfad


def _zahlen(z):
    return (z["stimmig"], z["fehlt"], z["fremd"], z["unvollstaendig"], z["verloren"])


# --------------------------------------------------------------------------
# Outlook: the resume log holds mails, events and contacts alike
# --------------------------------------------------------------------------
def test_outlook_haelt_das_resume_log_gegen_die_platte(tmp_path):
    db = state_db.StateDb(tmp_path)
    done = state_db.DbDoneLog(db)
    _datei(tmp_path / "E-Mail/Posteingang/a.eml")
    _datei(tmp_path / "kalender/Arbeit/t.ics")
    done.mark("m1", "E-Mail/Posteingang/a.eml")
    done.mark("m2", "E-Mail/Posteingang/fehlt.eml")
    done.mark("e1", "kalender/Arbeit/t.ics")
    done.close()
    _datei(tmp_path / "E-Mail/Posteingang/fremd.eml")
    _datei(tmp_path / "E-Mail/Posteingang/notiz.txt")      # not an archive file
    db.verschwunden_ergaenzen(["E-Mail/Alt/weg.eml"], "2026-01-01")
    z = ac.pruefe_outlook(tmp_path)
    assert (z["quelle"], z["einheit"], z["stand"]) == ("outlook", "files", "ganz")
    assert _zahlen(z) == (2, 1, 1, 0, 1)
    assert [(r["pfad"], r["fehlt"], r["fremd"], r["verloren"]) for r in z["zeilen"]] == \
        [("E-Mail/Posteingang", 1, 1, 0), ("E-Mail/Alt", 0, 0, 1)]


def test_outlook_ohne_befund_hat_keine_zeilen(tmp_path):
    db = state_db.StateDb(tmp_path)
    done = state_db.DbDoneLog(db)
    _datei(tmp_path / "E-Mail/Posteingang/a.eml")
    done.mark("m1", "E-Mail/Posteingang/a.eml")
    done.close()
    z = ac.pruefe_outlook(tmp_path)
    assert _zahlen(z) == (1, 0, 0, 0, 0) and z["zeilen"] == []


# --------------------------------------------------------------------------
# Mirrors: the inventory, sizes included
# --------------------------------------------------------------------------
def test_spiegel_misst_die_groesse_und_kennt_fremde_dateien(tmp_path):
    db = state_db.StateDb(tmp_path)
    db.bestand_schreiben({
        "a": {"rel": "Dateien/a.pdf", "ctag": "c", "size": 3},
        "b": {"rel": "Dateien/halb.pdf", "ctag": "c", "size": 10},
        "c": {"rel": "Dateien/weg.pdf", "ctag": "c", "size": 1}})
    _datei(tmp_path / "Dateien/a.pdf", b"xxx")
    _datei(tmp_path / "Dateien/halb.pdf", b"xx")
    _datei(tmp_path / "Dateien/Fotos/fremd.jpg", b"x")
    db.verschwunden_ergaenzen(["Dateien/alt.pdf"], "2026-01-01")
    _datei(tmp_path / "Dateien/alt.pdf", b"x")              # kept, as promised
    z = ac.pruefe_onedrive(tmp_path)
    assert _zahlen(z) == (1, 1, 1, 1, 0)
    assert {r["pfad"]: (r["fehlt"], r["fremd"], r["unvollstaendig"]) for r in z["zeilen"]} == \
        {"Dateien": (1, 0, 1), "Dateien/Fotos": (0, 1, 0)}


def test_sharepoint_prueft_jede_bibliothek_als_zeile(tmp_path):
    for site, lib, da in (("Nordwind", "Dokumente", True), ("Nordwind", "Archiv", False)):
        db = state_db.StateDb(tmp_path / site / lib)
        db.bestand_schreiben({"a": {"rel": "Dateien/a.pdf", "ctag": "c", "size": 1}})
        if da:
            _datei(tmp_path / site / lib / "Dateien/a.pdf", b"x")
    z = ac.pruefe_sharepoint(tmp_path)
    assert _zahlen(z) == (1, 1, 0, 0, 0)
    assert [(r["pfad"], r["fehlt"]) for r in z["zeilen"]] == [("Nordwind/Archiv", 1)]


def test_seiten_und_grabsteine(tmp_path):
    db = state_db.StateDb(tmp_path)
    db.seiten_schreiben({"p1": {"rel": "Team X/Home.html", "etag": "e"},
                         "p2": {"rel": "Team X/Weg.html", "etag": "e"}})
    _datei(tmp_path / "Team X/Home.html")
    _datei(tmp_path / "Team X/Fremd.html")
    db.verschwunden_ergaenzen(["Team X/Alt.html"], "2026-01-01")
    z = ac.pruefe_seiten(tmp_path)
    assert _zahlen(z) == (1, 1, 1, 0, 1)


# --------------------------------------------------------------------------
# Teams: conversation files, fetched files, channel mirrors
# --------------------------------------------------------------------------
def test_teams_prueft_konversationen_dateien_und_spiegel(tmp_path):
    db = state_db.StateDb(tmp_path)
    db.saetze_schreiben("conversations", {
        "c1": json.dumps({"done": True, "rel": "1on1/Alice__x.html", "category": "1on1"}),
        "c2": json.dumps({"done": True, "rel": "group/Weg__x.html", "category": "group"}),
        "c3": json.dumps({"done": True, "empty": True, "rel": None, "category": "group"}),
        "ch:k1": json.dumps({"done": True, "rel": "channels/Nordwind/Allgemein__x.html",
                             "category": "channels"})})
    _datei(tmp_path / "1on1/Alice__x.html")
    _datei(tmp_path / "1on1/Fremd__y.html")
    _datei(tmp_path / "channels/Nordwind/Allgemein__x.html")
    db.kv_schreiben("files:c1", json.dumps({
        "https://x/a.pdf": {"rel": "1on1/Anhaenge/Alice__x/a.pdf", "size": 3},
        "https://x/b.pdf": {"rel": "1on1/Anhaenge/Alice__x/b.pdf", "size": 3},
        "https://x/d.pdf": {"rel": "1on1/Anhaenge/Alice__x/d.pdf", "size": 3}}))
    db.permanent_schreiben({
        # refused by Microsoft, never fetched: its own number, by name
        "https://x/c.pptx": export_util.permanent_mark("refused", "HTTP 403 accessDenied", name="Plan.pptx",
                                                       unit="1on1/Alice__x.html"),
        # refused after a copy was fetched: the copy counts, the refusal not
        "https://x/d.pdf": export_util.permanent_mark("refused", "HTTP 403", name="d.pdf",
                                                      rel="1on1/Anhaenge/Alice__x/d.pdf",
                                                      unit="1on1/Alice__x.html"),
        # gone at Microsoft before a copy came: the other number
        "https://x/e.pdf": export_util.permanent_mark("gone", "HTTP 404 itemNotFound", name="e.pdf",
                                                      unit="group/Weg__x.html")})
    _datei(tmp_path / "1on1/Anhaenge/Alice__x/d.pdf", b"xxx")
    _datei(tmp_path / "1on1/Anhaenge/Alice__x/a.pdf", b"xxx")
    _datei(tmp_path / "1on1/Anhaenge/Alice__x/b.pdf", b"x")     # half
    # A team's mirrored channel folders: their own state.db, their own rows.
    spiegel = state_db.StateDb(tmp_path / "channels/Nordwind/Dateien")
    spiegel.bestand_schreiben({"f": {"rel": "Dateien/Allgemein/plan.pdf", "ctag": "c", "size": 1}})
    _datei(tmp_path / "channels/Nordwind/Dateien/Dateien/Allgemein/plan.pdf", b"x")
    _datei(tmp_path / "channels/Nordwind/Dateien/Dateien/Allgemein/seite.html", b"x")  # mirrored, not a chat
    z = ac.pruefe_teams(tmp_path)
    assert _zahlen(z) == (5, 1, 2, 1, 0)
    assert z["verweigert"] == 1 and z["befunde"]["verweigert"] == ["1on1/Alice__x.html: Plan.pptx"]
    assert z["weg"] == 1 and z["befunde"]["weg"] == ["group/Weg__x.html: e.pdf"]
    je = {r["pfad"]: (r["fehlt"], r["fremd"], r["unvollstaendig"], r["verweigert"], r["weg"])
          for r in z["zeilen"]}
    assert je == {"1on1": (0, 1, 1, 1, 0), "group": (1, 0, 0, 0, 1), "channels/Nordwind": (0, 1, 0, 0, 0)}


# --------------------------------------------------------------------------
# Planner, To Do, OneNote: one folder per unit
# --------------------------------------------------------------------------
def test_planner_prueft_board_und_referenzen(tmp_path):
    board = tmp_path / "Team X Board__abc"
    db = state_db.StateDb(board)
    db.kv_schreiben("tasks", json.dumps({"t1": {"etag": "e"}}))
    db.kv_schreiben("anhaenge", json.dumps({"https://x/a.pdf": {"rel": "Anhaenge/a__k.pdf", "ctag": "c"}}))
    _datei(board / "Anhaenge/a__k.pdf")
    _datei(board / "Anhaenge/fremd.pdf")
    z = ac.pruefe_planner(tmp_path)                       # board.html is missing
    assert _zahlen(z) == (1, 1, 1, 0, 0)
    assert [(r["pfad"], r["fehlt"], r["fremd"]) for r in z["zeilen"]] == [("Team X Board__abc", 1, 1)]


def test_todo_prueft_liste_und_anhaenge(tmp_path):
    liste = tmp_path / "Einkauf__abc"
    db = state_db.StateDb(liste)
    db.kv_schreiben("tasks", json.dumps({"t1": {"etag": "e", "anhaenge": [
        {"id": "a1", "name": "Bon.pdf", "rel": "Anhaenge/t1_Bon.pdf"}]}}))
    _datei(liste / "list.html")
    _datei(liste / "Anhaenge/t1_Bon.pdf")
    z = ac.pruefe_todo(tmp_path)
    assert _zahlen(z) == (2, 0, 0, 0, 0) and z["zeilen"] == []


def test_onenote_prueft_seiten_und_ressourcen(tmp_path):
    nb = tmp_path / "Projekte"
    db = state_db.StateDb(nb)
    db.saetze_schreiben("pages", {
        "p1": json.dumps({"rel": "Allgemein/Besprechung__a.html", "lm": "x"}),
        "p2": json.dumps({"rel": "Allgemein/Weg__b.html", "lm": "x", "deleted": "2026-01-01"})})
    db.saetze_schreiben("ressourcen", {
        "img": json.dumps({"rel": "Allgemein/Besprechung__a.html", "inline": True}),
        "pdf": json.dumps({"rel": "Allgemein/Besprechung__a.files/Protokoll.pdf"})})
    _datei(nb / "Allgemein/Besprechung__a.html")
    _datei(nb / "Allgemein/Besprechung__a.files/Protokoll.pdf")
    _datei(nb / "Allgemein/Besprechung__a.files/ohne_satz.png")   # the page's folder: hers
    _datei(nb / "Allgemein/Verwaist__c.html")                    # no record at all
    z = ac.pruefe_onenote(tmp_path)
    # The marked page keeps its file – so its absence is a finding; a file
    # in a known page's folder is not, a page nobody recorded is.
    assert _zahlen(z) == (2, 1, 1, 0, 0)
    assert [(r["pfad"], r["fehlt"], r["fremd"]) for r in z["zeilen"]] == [("Projekte", 1, 1)]
    assert z["befunde"]["fremd"] == ["Projekte/Allgemein/Verwaist__c.html"]


# --------------------------------------------------------------------------
# A damaged state.db, the index, the whole archive
# --------------------------------------------------------------------------
def test_beschaedigte_buchhaltung_wird_gesagt_nicht_geraten(tmp_path):
    (tmp_path / state_db.DB_NAME).write_bytes(b"kein sqlite")
    z = ac.pruefe_outlook(tmp_path)
    assert z["stand"] == "nicht" and z["grund"] == ac.DB_BESCHAEDIGT
    assert _zahlen(z) == (0, 0, 0, 0, 0) and z["fehler"]
    assert state_db.StateDb(tmp_path / "leer").integritaet() == "ok"


def _index(store, dateien):
    import store_layout
    store.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(store_layout.db_path(store))
    con.execute("CREATE TABLE dateien(root TEXT, rel TEXT, mtime_ns INTEGER, size INTEGER)")
    con.executemany("INSERT INTO dateien VALUES(?,?,?,?)", dateien)
    con.commit()
    con.close()


def test_index_gegen_archiv(tmp_path):
    outlook = tmp_path / "outlook"
    a = _datei(outlook / "E-Mail/Posteingang/a.eml", b"x")
    b = _datei(outlook / "E-Mail/Posteingang/b.eml", b"x")
    store = tmp_path / "store"
    st = a.stat()
    _index(store, [("outlook", "E-Mail/Posteingang/a.eml", st.st_mtime_ns, st.st_size),
                   ("outlook", "E-Mail/Posteingang/b.eml", 1, 1),            # changed since
                   ("outlook", "E-Mail/Posteingang/weg.eml", 1, 1)])         # gone since
    _datei(outlook / "E-Mail/Posteingang/neu.eml", b"x")
    ix = ac.pruefe_index(store, {"outlook": outlook})
    assert (ix["stand"], ix["geaendert"], ix["neu"], ix["weg"]) == ("ganz", 1, 1, 1)
    assert ix["gebaut"]
    assert b.exists()
    assert ac.pruefe_index(tmp_path / "keiner", {"outlook": outlook})["grund"] == ac.KEIN_INDEX


def test_pruefe_alles_schreibt_nur_den_bericht(tmp_path, capsys):
    """Every folder that exists gets a row, the report names them in the
    overview's order – and the archive is byte for byte as before."""
    outlook = tmp_path / "outlook"
    db = state_db.StateDb(outlook)
    done = state_db.DbDoneLog(db)
    done.mark("m1", "E-Mail/Posteingang/a.eml")
    done.close()
    _datei(outlook / "E-Mail/Posteingang/a.eml")
    todo = tmp_path / "todo"
    state_db.StateDb(todo / "Einkauf__x").kv_schreiben("tasks", json.dumps({"t": {}}))
    vorher = sorted((p.relative_to(tmp_path).as_posix(), p.stat().st_size)
                    for p in tmp_path.rglob("*") if p.is_file() and not p.name.startswith("state.db"))
    bericht = ac.pruefe_alles({"outlook": outlook, "todo": todo, "teams": tmp_path / "nein"},
                              tmp_path / "store")
    assert [q["quelle"] for q in bericht["quellen"]] == ["outlook", "todo"]
    assert bericht["quellen"][1]["fehlt"] == 1               # list.html is missing
    assert bericht["index"]["stand"] == "nicht"
    nachher = sorted((p.relative_to(tmp_path).as_posix(), p.stat().st_size)
                     for p in tmp_path.rglob("*") if p.is_file() and not p.name.startswith("state.db"))
    assert vorher == nachher, "the check touched the archive"
    events = [e for e in (progress.lies_event(z) for z in capsys.readouterr().out.splitlines()) if e]
    assert [e["v"]["name"]["k"] for e in events if e["k"] == "run.archiv.quelle"] == \
        ["ana.archiv.quelle.outlook", "ana.archiv.quelle.todo"]


def test_zustand_behaelt_nur_auffaellige_zeilen():
    z = completeness.zustand("outlook", stimmig=5,
                             zeilen=[completeness.zustandszeile("a"),
                                     completeness.zustandszeile("b", fehlt=2),
                                     completeness.zustandszeile("c", fremd=1, verloren=3)])
    assert [r["pfad"] for r in z["zeilen"]] == ["c", "b"]
    assert z["stand"] == "ganz" and z["einheit"] == "files"


# --------------------------------------------------------------------------
# The findings behind the numbers, and the set-aside folder
# --------------------------------------------------------------------------
def test_befunde_nennen_die_dateien_unter_der_quelle(tmp_path):
    """Every number has its files, named relative to the source folder –
    for a library or a team's mirror with that prefix."""
    lib = tmp_path / "Nordwind" / "Dokumente"
    state_db.StateDb(lib).bestand_schreiben({"a": {"rel": "Dateien/weg.pdf", "ctag": "c", "size": 1}})
    _datei(lib / "Dateien/fremd.pdf")
    z = ac.pruefe_sharepoint(tmp_path)
    assert z["befunde"]["fehlt"] == ["Nordwind/Dokumente/Dateien/weg.pdf"]
    assert z["befunde"]["fremd"] == ["Nordwind/Dokumente/Dateien/fremd.pdf"]
    assert z["gekappt"] is False and z["beiseite"] == 0


def test_befunde_sind_gekappt_nicht_endlos(tmp_path, monkeypatch):
    monkeypatch.setattr(ac, "GRENZE", 2)
    db = state_db.StateDb(tmp_path)
    for i in range(4):
        _datei(tmp_path / f"E-Mail/Posteingang/f{i}.eml")
    z = ac.pruefe_outlook(tmp_path)
    assert z["fremd"] == 4 and len(z["befunde"]["fremd"]) == 2 and z["gekappt"] is True
    db.close()


def test_beiseitegelegtes_ist_weder_archiv_noch_fremd(tmp_path):
    """Files under _fremd/ are neither counted as foreign nor read by the
    corpus – they are what "set aside" moved out, and a number of their
    own."""
    import corpus
    db = state_db.StateDb(tmp_path)
    done = state_db.DbDoneLog(db)
    _datei(tmp_path / "E-Mail/Posteingang/a.eml")
    done.mark("m1", "E-Mail/Posteingang/a.eml")
    done.close()
    _datei(tmp_path / f"{ac.FREMD_DIR}/20260913-100000/E-Mail/Posteingang/alt.eml")
    _datei(tmp_path / f"{ac.FREMD_DIR}/20260913-100000/{ac.LISTE}", b"{}")
    z = ac.pruefe_outlook(tmp_path)
    assert (z["fremd"], z["beiseite"]) == (0, 1)
    assert [p.name for p in corpus.DATEIEN["outlook"](tmp_path)] == ["a.eml"]
    assert corpus.DATEIEN["pages"](tmp_path) == [] and corpus.DATEIEN["teams"](tmp_path) == []


# --------------------------------------------------------------------------
# The actions: note, set aside, put back, rebuild – nothing deletes
# --------------------------------------------------------------------------
def test_vermerken_notiert_verlorene_und_laesst_den_grabstein(tmp_path):
    db = state_db.StateDb(tmp_path)
    db.verschwunden_ergaenzen(["E-Mail/Alt/weg.eml", "E-Mail/Alt/da.eml"], "2026-01-01")
    _datei(tmp_path / "E-Mail/Alt/da.eml")
    assert ac.pruefe_outlook(tmp_path)["verloren"] == 1
    assert ac.vermerken(tmp_path, "outlook") == 1
    assert ac.pruefe_outlook(tmp_path)["verloren"] == 0
    assert set(db.verloren_lesen()) == {"E-Mail/Alt/weg.eml"}
    assert set(db.verschwunden_lesen()) == {"E-Mail/Alt/weg.eml", "E-Mail/Alt/da.eml"}
    assert ac.vermerken(tmp_path, "outlook") == 0


def test_vermerken_findet_die_buchhaltung_der_bibliothek(tmp_path):
    lib = tmp_path / "Nordwind" / "Dokumente"
    state_db.StateDb(lib).verschwunden_ergaenzen(["Dateien/weg.pdf"], "2026-01-01")
    assert ac.vermerken(tmp_path, "sharepoint") == 1
    assert set(state_db.StateDb(lib).verloren_lesen()) == {"Dateien/weg.pdf"}
    assert state_db.StateDb(tmp_path).verloren_lesen() == {}


def test_beiseitelegen_und_zurueckholen(tmp_path):
    """Foreign files move into _fremd/<stamp>/ with their path and a list;
    put back restores them and takes the empty batch with it. A file whose
    place is taken stays in the batch."""
    db = state_db.StateDb(tmp_path)
    done = state_db.DbDoneLog(db)
    _datei(tmp_path / "E-Mail/Posteingang/a.eml", b"bleibt")
    done.mark("m1", "E-Mail/Posteingang/a.eml")
    done.close()
    _datei(tmp_path / "E-Mail/Posteingang/fremd.eml", b"f1")
    _datei(tmp_path / "E-Mail/Kunden/fremd2.eml", b"f2")
    n, uebersprungen, stapel = ac.beiseitelegen(tmp_path, "outlook")
    assert (n, uebersprungen) == (2, 0) and stapel
    basis = tmp_path / ac.FREMD_DIR / stapel
    assert (basis / "E-Mail/Posteingang/fremd.eml").read_bytes() == b"f1"
    assert not (tmp_path / "E-Mail/Posteingang/fremd.eml").exists()
    assert (tmp_path / "E-Mail/Posteingang/a.eml").read_bytes() == b"bleibt"
    liste = json.loads((basis / ac.LISTE).read_text(encoding="utf-8"))
    assert sorted(liste["dateien"]) == ["E-Mail/Kunden/fremd2.eml", "E-Mail/Posteingang/fremd.eml"]
    z = ac.pruefe_outlook(tmp_path)
    assert (z["fremd"], z["beiseite"]) == (0, 2)
    assert ac.beiseitelegen(tmp_path, "outlook") == (0, 0, None)
    # One place got taken meanwhile: that file stays in the batch.
    _datei(tmp_path / "E-Mail/Kunden/fremd2.eml", b"neu")
    assert ac.zurueckholen(tmp_path) == (1, 1)
    assert (tmp_path / "E-Mail/Posteingang/fremd.eml").read_bytes() == b"f1"
    assert (tmp_path / "E-Mail/Kunden/fremd2.eml").read_bytes() == b"neu"
    assert (basis / "E-Mail/Kunden/fremd2.eml").read_bytes() == b"f2"
    (tmp_path / "E-Mail/Kunden/fremd2.eml").unlink()
    assert ac.zurueckholen(tmp_path) == (1, 0)
    assert not (tmp_path / ac.FREMD_DIR).exists(), "an emptied batch leaves no folder"
    assert ac.zurueckholen(tmp_path) == (0, 0)


def test_retten_kopiert_eine_lesbare_buchhaltung_ganz(tmp_path):
    db = state_db.StateDb(tmp_path)
    db.verschwunden_ergaenzen(["a.eml"], "2026-01-01")
    db.kv_schreiben("delta:f1", "link")
    db.close()
    neu = tmp_path / "state.db.neu"
    assert ac._retten(tmp_path / state_db.DB_NAME, neu) == "ganz"
    frisch = state_db.StateDb(tmp_path, neu.name)
    assert frisch.verschwunden_lesen() == {"a.eml": "2026-01-01"} and frisch.kv_lesen("delta:f1") == "link"


def test_neu_aufbauen_legt_die_beschaedigte_buchhaltung_beiseite(tmp_path):
    """A file that is no SQLite at all: nothing to salvage, the old file
    stays under a dated name, a fresh empty one takes its place – and not
    one archive file is touched."""
    (tmp_path / state_db.DB_NAME).write_bytes(b"kein sqlite")
    _datei(tmp_path / "E-Mail/Posteingang/a.eml", b"x")
    n, gerettet = ac.neu_aufbauen(tmp_path, "outlook")
    assert n == 1 and gerettet == {"outlook": "nichts"}
    beiseite = list(tmp_path.glob("state.db.beschaedigt-*"))
    assert len(beiseite) == 1 and beiseite[0].read_bytes() == b"kein sqlite"
    assert state_db.StateDb(tmp_path).integritaet() == "ok"
    assert (tmp_path / "E-Mail/Posteingang/a.eml").read_bytes() == b"x"
    assert ac.pruefe_outlook(tmp_path)["fremd"] == 1       # known to nobody now
    assert ac.neu_aufbauen(tmp_path, "outlook") == (0, {}), "a healthy file is left alone"


def test_bericht_aktualisieren_ersetzt_nur_die_eine_zeile(tmp_path):
    bericht = tmp_path / "archivpruefung.json"
    bericht.write_text(json.dumps({"geprueft": "x", "quellen": [
        {"quelle": "teams", "stimmig": 1}, {"quelle": "outlook", "stimmig": 0}],
        "index": {"stand": "ganz"}}), encoding="utf-8")
    outlook = tmp_path / "outlook"
    db = state_db.StateDb(outlook)
    done = state_db.DbDoneLog(db)
    _datei(outlook / "E-Mail/a.eml")
    done.mark("m1", "E-Mail/a.eml")
    done.close()
    neu = ac.bericht_aktualisieren(bericht, "outlook", outlook)
    assert neu["stimmig"] == 1
    daten = json.loads(bericht.read_text(encoding="utf-8"))
    assert [q["quelle"] for q in daten["quellen"]] == ["outlook", "teams"]
    assert daten["index"] == {"stand": "ganz"} and daten["geprueft"] == "x"
    zustand = completeness.zustand("outlook", stand="nicht", grund="ana.archiv.reason.rebuilding")
    assert ac.bericht_aktualisieren(bericht, "outlook", outlook, zustand=zustand)["grund"] == \
        "ana.archiv.reason.rebuilding"


def test_teams_liest_die_buchhaltung_von_vor_neun_null(tmp_path):
    """An archive the export has not touched since 9.0 keeps its records in
    one kv blob – the check reads it like the export does, and moves
    nothing (that is the export's job)."""
    db = state_db.StateDb(tmp_path)
    db.kv_schreiben("state", json.dumps({"version": 1, "conversations": {
        "c1": {"done": True, "rel": "1on1/Alice__x.html", "count": 3, "empty": False}}}))
    _datei(tmp_path / "1on1/Alice__x.html")
    z = ac.pruefe_teams(tmp_path)
    assert _zahlen(z) == (1, 0, 0, 0, 0)
    assert db.saetze_lesen("conversations") == {}, "the check moved the blob"


def test_onenote_liest_alte_seitenliste_und_kennt_die_seitenordner(tmp_path):
    """Pages from the kv blob of a notebook from before 9.0, and a page's
    .files folder belongs to the page – with or without resource records."""
    nb = tmp_path / "Projekte"
    db = state_db.StateDb(nb)
    db.kv_schreiben("pages", json.dumps({"p1": {"rel": "Allgemein/Besprechung__a.html", "lm": "x"}}))
    _datei(nb / "Allgemein/Besprechung__a.html")
    _datei(nb / "Allgemein/Besprechung__a.files/Protokoll.pdf")
    _datei(nb / "Allgemein/Besprechung__a.files/bild.png")
    _datei(nb / "Allgemein/Fremd__b.files/x.png")            # no such page
    z = ac.pruefe_onenote(tmp_path)
    assert _zahlen(z) == (1, 0, 1, 0, 0)
    assert z["befunde"]["fremd"] == ["Projekte/Allgemein/Fremd__b.files/x.png"]
    assert db.saetze_lesen("pages") == {}, "the check moved the blob"



# --------------------------------------------------------------------------
# Dead cards: what a resync did not bring back, and what the user noted
# --------------------------------------------------------------------------
def test_vermerkte_fehlende_zaehlen_als_vermerkt_nicht_als_fehlend(tmp_path):
    """A missing file the user noted as lost troubles no row and no
    finding – the record stays, the count says how many were noted."""
    db = state_db.StateDb(tmp_path)
    done = state_db.DbDoneLog(db)
    done.mark("m1", "E-Mail/Posteingang/weg.eml")
    done.mark("m2", "E-Mail/Posteingang/auch_weg.eml")
    done.close()
    assert ac.pruefe_outlook(tmp_path)["fehlt"] == 2
    db.verloren_vermerken(["E-Mail/Posteingang/weg.eml"], "2026-09-14T08:00:00+00:00")
    z = ac.pruefe_outlook(tmp_path)
    assert (z["fehlt"], z["vermerkt"]) == (1, 1)
    assert z["befunde"]["fehlt"] == ["E-Mail/Posteingang/auch_weg.eml"]
    assert [(r["pfad"], r["fehlt"]) for r in z["zeilen"]] == [("E-Mail/Posteingang", 1)]
    # The mirrors and Teams read the note the same way.
    spiegel = tmp_path / "od"
    sdb = state_db.StateDb(spiegel)
    sdb.bestand_schreiben({"1": {"rel": "Dateien/weg.pdf", "ctag": "c", "size": 3}})
    sdb.verloren_vermerken(["Dateien/weg.pdf"], "2026-09-14T08:00:00+00:00")
    z = ac.pruefe_onedrive(spiegel)
    assert (z["fehlt"], z["vermerkt"]) == (0, 1)
    teams = tmp_path / "teams"
    tdb = state_db.StateDb(teams)
    tdb.saetze_schreiben("conversations", {"c1": json.dumps(
        {"done": True, "rel": "group/weg__c1.html", "empty": False})})
    tdb.verloren_vermerken(["group/weg__c1.html"], "2026-09-14T08:00:00+00:00")
    z = ac.pruefe_teams(teams)
    assert (z["fehlt"], z["vermerkt"]) == (0, 1)


def test_vermerken_nimmt_fehlendes_nur_auf_wunsch(tmp_path):
    """The note settles lost tombstones by default; files a resync did not
    bring back only when asked for (arten) – the page asks for that only
    after such a run."""
    db = state_db.StateDb(tmp_path)
    done = state_db.DbDoneLog(db)
    done.mark("m1", "E-Mail/Posteingang/weg.eml")
    done.close()
    db.verschwunden_ergaenzen(["E-Mail/Alt/grab.eml"], "2026-01-01")
    assert ac.vermerken(tmp_path, "outlook") == 1
    z = ac.pruefe_outlook(tmp_path)
    assert (z["fehlt"], z["verloren"], z["vermerkt"]) == (1, 0, 0)
    assert ac.vermerken(tmp_path, "outlook", ("verloren", "fehlt", "egal")) == 1
    z = ac.pruefe_outlook(tmp_path)
    assert (z["fehlt"], z["verloren"], z["vermerkt"]) == (0, 0, 1)
    assert set(db.verloren_lesen()) == {"E-Mail/Alt/grab.eml", "E-Mail/Posteingang/weg.eml"}
    assert set(db.done_lesen().values()) == {"E-Mail/Posteingang/weg.eml"}, "the record stays"


def test_bericht_datiert_das_fehlen_und_kennt_den_letzten_abgleich(tmp_path):
    """`fehlt_seit` is the first report that found the missing files and
    stays while something stays missing; `nachgeholt` is what the app
    knows about the source's last resync – the page reads "still missing
    after a fetch" from the two."""
    outlook = tmp_path / "outlook"
    db = state_db.StateDb(outlook)
    done = state_db.DbDoneLog(db)
    done.mark("m1", "E-Mail/Posteingang/weg.eml")
    done.close()
    pfade = {"outlook": outlook}
    erster = ac.pruefe_alles(pfade, tmp_path / "store", vorher=None, nachgeholt={})
    zeile = erster["quellen"][0]
    assert zeile["fehlt"] == 1 and zeile["fehlt_seit"] and zeile["nachgeholt"] is None
    zweiter = ac.pruefe_alles(pfade, tmp_path / "store", vorher=erster,
                              nachgeholt={"outlook": "2026-09-14T09:00:00+00:00"})
    assert zweiter["quellen"][0]["fehlt_seit"] == zeile["fehlt_seit"], "the date holds"
    assert zweiter["quellen"][0]["nachgeholt"] == "2026-09-14T09:00:00+00:00"
    # An action's fresh row keeps both from the stored report.
    bericht = tmp_path / "archivpruefung.json"
    bericht.write_text(json.dumps(zweiter), encoding="utf-8")
    neu = ac.bericht_aktualisieren(bericht, "outlook", outlook)
    assert (neu["fehlt_seit"], neu["nachgeholt"]) == (zeile["fehlt_seit"], "2026-09-14T09:00:00+00:00")
    # Nothing missing any more: the date goes.
    _datei(outlook / "E-Mail/Posteingang/weg.eml")
    dritter = ac.pruefe_alles(pfade, tmp_path / "store", vorher=zweiter, nachgeholt={})
    assert dritter["quellen"][0]["fehlt"] == 0 and dritter["quellen"][0]["fehlt_seit"] is None


def test_aktion_als_schritt_sagt_was_sie_tat_und_schreibt_die_zeile(tmp_path, capsys):
    """An action run as a step: one log line with the numbers, the source's
    row judged afresh into the report, a result – and "nothing to do" for
    a source that has no folder."""
    outlook = tmp_path / "outlook"
    db = state_db.StateDb(outlook)
    done = state_db.DbDoneLog(db)
    done.mark("m1", "E-Mail/Posteingang/a.eml")
    done.close()
    _datei(outlook / "E-Mail/Posteingang/a.eml")
    _datei(outlook / "E-Mail/Posteingang/fremd.eml")
    bericht = tmp_path / "archivpruefung.json"
    pfade = {"outlook": outlook, "teams": tmp_path / "nein"}
    ac.aktion("beiseitelegen", "outlook", pfade, bericht)
    zeilen = capsys.readouterr().out.splitlines()
    events = [e for e in (progress.lies_event(z) for z in zeilen) if e]
    (aside,) = [e for e in events if e["k"] == "run.archiv.aside"]
    assert aside["v"]["n"] == 1 and aside["v"]["name"]["k"] == "ana.archiv.quelle.outlook"
    assert aside["v"]["folder"].startswith("_fremd/")
    assert any(progress.lies_ergebnis(z) for z in zeilen), "no result line"
    gespeichert = json.loads(bericht.read_text(encoding="utf-8"))
    assert [q["quelle"] for q in gespeichert["quellen"]] == ["outlook"]
    assert (gespeichert["quellen"][0]["fremd"], gespeichert["quellen"][0]["beiseite"]) == (0, 1)
    ac.aktion("zurueckholen", "teams", pfade, bericht)
    events = [e for e in (progress.lies_event(z) for z in capsys.readouterr().out.splitlines()) if e]
    assert [e["k"] for e in events] == ["run.archiv.nothing"]
    # The rebuild: the damaged bookkeeping goes aside, the row says so.
    (outlook / "state.db").write_bytes(b"kein sqlite")
    ac.aktion("neu-aufbauen", "outlook", pfade, bericht)
    events = [e for e in (progress.lies_event(z) for z in capsys.readouterr().out.splitlines()) if e]
    assert [e["k"] for e in events] == ["run.archiv.rebuilt"] and events[0]["v"]["n"] == 1
    zeile = json.loads(bericht.read_text(encoding="utf-8"))["quellen"][0]
    assert zeile["stand"] == "nicht" and zeile["grund"] == "ana.archiv.reason.rebuilding"
    assert list(outlook.glob("state.db.beschaedigt-*"))


def test_beschaedigte_nennt_die_kaputten_einheiten(tmp_path):
    lib = tmp_path / "Nordwind" / "Dokumente"
    state_db.StateDb(lib).bestand_schreiben({})
    state_db.StateDb(tmp_path).bestand_schreiben({})
    assert ac.beschaedigte(tmp_path) == []
    (lib / "state.db").write_bytes(b"kein sqlite")
    assert ac.beschaedigte(tmp_path) == [lib]


def test_aktion_pruefen_datiert_die_zeile_als_nachgeholt(tmp_path, capsys):
    """The last step of a "Fetch again": the row judged afresh and dated as
    fetched – what is still missing is said in the log and reads as a card
    to note."""
    outlook = tmp_path / "outlook"
    db = state_db.StateDb(outlook)
    done = state_db.DbDoneLog(db)
    done.mark("m1", "E-Mail/Posteingang/weg.eml")
    done.close()
    bericht = tmp_path / "archivpruefung.json"
    ac.aktion("pruefen", "outlook", {"outlook": outlook}, bericht)
    zeile = json.loads(bericht.read_text(encoding="utf-8"))["quellen"][0]
    assert zeile["fehlt"] == 1 and zeile["nachgeholt"] and zeile["fehlt_seit"]
    assert zeile["nachgeholt"] >= zeile["fehlt_seit"]
    events = [e for e in (progress.lies_event(z) for z in capsys.readouterr().out.splitlines()) if e]
    assert [e["k"] for e in events] == ["run.archiv.quelle", "run.archiv.stubborn"]
    assert events[1]["v"]["n"] == 1


def test_abgeholt_nimmt_die_dateien_aus_der_bilanz(tmp_path):
    """After a targeted fetch the stored balance is right again without a
    walk: the fetched files leave the open side of their row – matched
    by the longest path prefix, so a library row counts too."""
    db = state_db.StateDb(tmp_path)
    completeness.schreiben(db, completeness.bilanz(
        "sharepoint", "files", da=3, offen=3,
        zeilen=[completeness.zeile("S/A", 1, 2), completeness.zeile("S/B", 2, 1)],
        extra={"offene": [{"id": "1", "rel": "S/A/Dateien/a.pdf"},
                          {"id": "2", "rel": "S/A/Dateien/b.pdf"},
                          {"id": "3", "rel": "S/B/Dateien/c.pdf"}]}))
    completeness.abgeholt(db, "sharepoint", ["S/A/Dateien/a.pdf", "S/B/Dateien/c.pdf", "X/nirgends.pdf"])
    b = completeness.lesen(db, "sharepoint")
    assert (b["da"], b["offen"]) == (5, 1)
    assert [(z["pfad"], z["da"], z["offen"]) for z in b["zeilen"]] == [("S/A", 2, 1)]
    assert b["offene"] == [{"id": "2", "rel": "S/A/Dateien/b.pdf"}]
    assert completeness.abgeholt(db, "onedrive", ["x"]) is None, "no report, nothing to adjust"


# --------------------------------------------------------------------------
# What no run asks for again: refused and gone, in every source
# --------------------------------------------------------------------------
def test_marks_count_as_refused_or_gone_unless_their_copy_lies_here(tmp_path):
    """A mail Microsoft refuses, a file that was gone before it came: each
    its own number, listed by path or by name, never missing – and a mark
    whose older copy lies here counts nothing, the copy does."""
    db = state_db.StateDb(tmp_path)
    done = state_db.DbDoneLog(db)
    _datei(tmp_path / "E-Mail/Posteingang/a.eml")
    done.mark("m1", "E-Mail/Posteingang/a.eml")
    done.close()
    db.permanent_schreiben({
        "m2": export_util.permanent_mark("refused", "HTTP 403", name="Angebot",
                                         rel="E-Mail/Posteingang/2026-01-01_b.eml"),
        "m3": export_util.permanent_mark("gone", "HTTP 404", name="Weg", rel="E-Mail/Alt/c.eml")})
    z = ac.pruefe_outlook(tmp_path)
    assert _zahlen(z) == (1, 0, 0, 0, 0)
    assert (z["verweigert"], z["weg"]) == (1, 1)
    assert z["befunde"]["verweigert"] == ["E-Mail/Posteingang/2026-01-01_b.eml"]
    assert z["befunde"]["weg"] == ["E-Mail/Alt/c.eml"]
    assert z["zeilen"] == [], "a refusal weighs nothing – no row of its own"
    # a mirror: the older copy of a refused newer version counts as here
    spiegel = state_db.StateDb(tmp_path / "od")
    spiegel.bestand_schreiben({"f1": {"rel": "Dateien/plan.pdf", "ctag": "c1", "size": 1}})
    _datei(tmp_path / "od/Dateien/plan.pdf", b"x")
    spiegel.permanent_schreiben({
        "f1": export_util.permanent_mark("refused", "HTTP 403", name="plan.pdf",
                                         rel="Dateien/plan.pdf", version="c2"),
        "f2": export_util.permanent_mark("gone", "HTTP 404", name="alt.pdf", rel="Dateien/alt.pdf")})
    z = ac.pruefe_onedrive(tmp_path / "od")
    assert _zahlen(z) == (1, 0, 0, 0, 0) and (z["verweigert"], z["weg"]) == (0, 1)
    assert z["befunde"]["weg"] == ["Dateien/alt.pdf"] and z["zeilen"] == []
    # a unit source: the unit names the row, the mark names the item
    liste = state_db.StateDb(tmp_path / "todo/Einkauf")
    liste.kv_schreiben("tasks", json.dumps({"t1": {"etag": "e", "anhaenge": []}}))
    _datei(tmp_path / "todo/Einkauf/list.html")
    liste.permanent_schreiben({"attachment:a1": export_util.permanent_mark(
        "refused", "HTTP 403", name="Rechnung.pdf", unit="Milch kaufen")})
    z = ac.pruefe_todo(tmp_path / "todo")
    assert z["verweigert"] == 1 and z["befunde"]["verweigert"] == ["Einkauf/Milch kaufen: Rechnung.pdf"]
    assert z["stand"] == "ganz" and z["zeilen"] == []
