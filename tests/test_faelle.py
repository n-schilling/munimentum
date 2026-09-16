"""faelle.py – the case book: history, saved searches, cases with their
entries, result lists and notes. One SQLite file, no network."""

import pytest

import faelle


@pytest.fixture
def buch(tmp_path):
    return faelle.Fallbuch(tmp_path / "faelle.db")


def test_kriterien_haben_eine_form():
    """Whatever comes in – API params, a stored JSON, a form – the criteria
    come out in one shape: known keys, one of three modes, a boolean gone,
    an integer or None for the case."""
    k = faelle.kriterien({"q": " budget ", "mode": "lexical", "gone": "1", "case": "3",
                          "person": "Alice", "unbekannt": "x"})
    assert k == {"q": "budget", "mode": "text", "person": "Alice", "source": "all", "from": "",
                 "to": "", "folder": "", "filetype": "", "gone": True, "fall": 3, "ordner": None,
                 "party": "all"}
    # the parties filter: one of two narrowings, anything else is "all"
    assert faelle.kriterien({"party": " External "})["party"] == "external"
    assert faelle.kriterien({"party": "x"})["party"] == "all"
    # the folder inside the case – only together with a case
    assert faelle.kriterien({"case": "3", "case_folder": "7"})["ordner"] == 7
    assert faelle.kriterien({"fall": 3, "ordner": "x"})["ordner"] is None
    assert faelle.kriterien({"ordner": 7})["ordner"] is None
    assert faelle.kriterien('{"mode": "semantic", "fall": null}')["mode"] == "aehnlich"
    assert faelle.kriterien({"mode": "ki", "fall": "x"})["fall"] is None
    assert faelle.kriterien("kaputt") == faelle.kriterien({})
    assert faelle.leer({}) and faelle.leer({"source": "all"})
    assert not faelle.leer({"source": "teams"}) and not faelle.leer({"q": "x"})


def test_historie_merkt_sich_kriterien_nicht_treffer_und_faltet_wiederholungen(buch):
    assert buch.suche_merken({}) is None, "an empty search is no history"
    a = buch.suche_merken({"q": "budget", "person": "Alice"}, 42)
    b = buch.suche_merken({"q": "budget", "person": "Alice"}, 43)
    assert a == b, "the same search again moves the entry, it does not pile up"
    c = buch.suche_merken({"q": "invoice"}, 3)
    assert c != a
    eintraege = buch.suchen()
    assert [e["kriterien"]["q"] for e in eintraege] == ["invoice", "budget"]
    assert eintraege[1]["treffer"] == 43 and eintraege[1]["wann"]
    assert "results" not in eintraege[0] and set(eintraege[0]) == {"id", "wann", "kriterien", "treffer"}
    assert buch.suchen(limit=1)[0]["kriterien"]["q"] == "invoice"
    assert buch.suchen_leeren() == 2 and buch.suchen() == []


def test_historie_wird_nach_tagen_aufgeraeumt(buch, monkeypatch):
    buch.suche_merken({"q": "alt"}, 1)
    import sqlite3
    con = sqlite3.connect(buch.pfad)
    con.execute("UPDATE suchen SET wann = '2020-01-01T00:00:00+00:00'")
    con.commit()
    con.close()
    buch.suche_merken({"q": "neu"}, 1)
    assert buch.aufraeumen(None) == 0 and len(buch.suchen()) == 2
    assert buch.aufraeumen(90) == 1 and [e["kriterien"]["q"] for e in buch.suchen()] == ["neu"]
    assert buch.aufraeumen(0) == 1 and buch.suchen() == []


def test_gespeicherte_suchen(buch):
    with pytest.raises(ValueError):
        buch.speichern("  ", {"q": "x"})
    a = buch.speichern("Nordwind budget", {"q": "budget 2026", "person": "Alice"})
    b = buch.speichern("Alles von Bob", {"person": "Bob"})
    namen = [g["name"] for g in buch.gespeicherte()]
    assert namen == ["Alles von Bob", "Nordwind budget"]
    g = buch.gespeichert(a)
    assert g["kriterien"]["q"] == "budget 2026" and g["zuletzt"] is None and g["fall"] is None
    buch.gelaufen(a, 42)
    assert buch.gespeichert(a)["treffer"] == 42 and buch.gespeichert(a)["zuletzt"]
    assert buch.umbenennen(a, "Nordwind") and buch.gespeichert(a)["name"] == "Nordwind"
    assert buch.loeschen(b) and not buch.loeschen(b)
    assert [g["id"] for g in buch.gespeicherte()] == [a]


def test_faelle_anlegen_aendern_schliessen_loeschen(buch):
    with pytest.raises(ValueError):
        buch.fall_anlegen("")
    f = buch.fall_anlegen("Nordwind offer 2026", "  Everything around the offer ")
    g = buch.fall_anlegen("Berlin office")
    faelle_ = buch.faelle()
    assert [x["name"] for x in faelle_] == ["Berlin office", "Nordwind offer 2026"], "newest change first"
    assert faelle_[1]["beschreibung"] == "Everything around the offer"
    assert faelle_[1]["status"] == "offen" and faelle_[1]["eintraege"] == 0
    buch.fall_aendern(f, name="Nordwind 2026", beschreibung="x")
    assert buch.fall(f)["name"] == "Nordwind 2026"
    buch.schliessen(f)
    fall = buch.fall(f)
    assert fall["status"] == "zu" and fall["geschlossen"]
    with pytest.raises(faelle.FallGeschlossen):
        buch.fall_aendern(f, name="nein")
    assert [x["id"] for x in buch.faelle()] == [g, f], "closed cases go last"
    assert [x["id"] for x in buch.faelle(mit_geschlossenen=False)] == [g]
    buch.oeffnen(f)
    assert buch.fall(f)["status"] == "offen" and buch.fall(f)["geschlossen"] is None
    with pytest.raises(faelle.KeinFall):
        buch.oeffnen(999)
    assert buch.fall(999) is None
    assert buch.fall_loeschen(g) and not buch.fall_loeschen(g)


def test_eintraege_kommen_einmal_und_kennen_ihre_faelle(buch):
    f = buch.fall_anlegen("Nordwind")
    g = buch.fall_anlegen("Audit")
    hits = [{"key": "mail:<a@x>", "src": "outlook", "root": "outlook", "rel": "E-Mail/a.eml",
             "title": "Offer", "date": "2026-08-28", "who": "Alice"},
            {"key": "file:i1", "src": "datei", "root": "sharepoint", "rel": "S/L/Dateien/x.xlsx",
             "titel": "x.xlsx", "datum": "2026-08-26", "wer": ""},
            {"key": "", "src": "outlook"}]
    assert buch.hinzufuegen(f, hits) == 2
    assert buch.hinzufuegen(f, hits) == 0, "already there"
    assert buch.hinzufuegen(g, hits[1:2]) == 1
    fall = buch.fall(f)
    assert fall["eintraege"] == 2 and fall["je_quelle"] == {"outlook": 1, "datei": 1}
    e = [x for x in fall["eintraege_liste"] if x["key"] == "mail:<a@x>"][0]
    assert (e["titel"], e["datum"], e["wer"], e["rel"]) == ("Offer", "2026-08-28", "Alice", "E-Mail/a.eml")
    assert buch.keys(f) == {"mail:<a@x>", "file:i1"}
    zug = buch.zugehoerigkeit(["file:i1", "mail:<a@x>", "nirgends"])
    assert [c["name"] for c in zug["file:i1"]] == ["Audit", "Nordwind"]
    assert [c["name"] for c in zug["mail:<a@x>"]] == ["Nordwind"] and "nirgends" not in zug
    assert buch.zugehoerigkeit([]) == {}
    assert buch.entfernen(f, "mail:<a@x>") and not buch.entfernen(f, "mail:<a@x>")
    assert buch.keys(f) == {"file:i1"}
    buch.schliessen(f)
    with pytest.raises(faelle.FallGeschlossen):
        buch.hinzufuegen(f, hits)
    with pytest.raises(faelle.FallGeschlossen):
        buch.entfernen(f, "file:i1")
    with pytest.raises(faelle.KeinFall):
        buch.hinzufuegen(999, hits)


def test_ergebnisliste_haelt_kriterien_und_treffer_zum_zeitpunkt(buch):
    f = buch.fall_anlegen("Nordwind")
    buch.hinzufuegen(f, [{"key": "mail:<direkt@x>", "src": "outlook"}])
    treffer = [{"key": "mail:<direkt@x>", "src": "outlook"},
               {"key": "mail:<b@x>", "src": "outlook", "title": "B"},
               {"key": "file:i9", "src": "datei", "root": "onedrive", "rel": "Dateien/n.pdf"}]
    liste_id, neu = buch.liste_anlegen(f, {"q": "budget", "source": "outlook"}, treffer)
    assert neu == 2, "the item already in the case stays the direct one"
    liste = buch.liste(liste_id)
    assert liste["anzahl"] == 3 and liste["kriterien"]["q"] == "budget" and liste["fall"] == f
    assert [t["key"] for t in liste["treffer"]] == ["mail:<b@x>", "file:i9"]
    fall = buch.fall(f)
    assert fall["listen"] == 1 and fall["eintraege"] == 3
    assert fall["listen_liste"][0]["id"] == liste_id
    assert buch.liste_loeschen(f, liste_id)
    assert buch.keys(f) == {"mail:<direkt@x>"}, "only what came with the list goes with it"
    assert buch.liste(liste_id) is None


def test_gespeicherte_suche_haengt_am_fall(buch):
    f = buch.fall_anlegen("Nordwind")
    s = buch.speichern("Nordwind budget", {"q": "budget"}, fall_id=f)
    assert buch.gespeichert(s)["fall_name"] == "Nordwind"
    assert [g["id"] for g in buch.gespeicherte(fall_id=f)] == [s]
    assert [g["id"] for g in buch.fall(f)["suchen_liste"]] == [s]
    assert buch.anhaengen(s, None) and buch.gespeichert(s)["fall"] is None
    buch.anhaengen(s, f)
    buch.schliessen(f)
    with pytest.raises(faelle.FallGeschlossen):
        buch.anhaengen(buch.speichern("x", {"q": "y"}), f)
    buch.oeffnen(f)
    assert buch.fall_loeschen(f)
    assert buch.gespeichert(s)["fall"] is None, "the search survives the case, detached"


def test_casebook_notizen(buch):
    f = buch.fall_anlegen("Nordwind")
    with pytest.raises(ValueError):
        buch.notiz(f, "   ")
    a = buch.notiz(f, "Case opened after the review.")
    b = buch.notiz(f, "Final offer is the v3 mail.")
    assert [n["id"] for n in buch.notizen(f)] == [b, a], "newest first"
    assert buch.notiz_aendern(f, a, "Opened.") and buch.notizen(f)[1]["text"] == "Opened."
    assert buch.notiz_loeschen(f, b) and [n["id"] for n in buch.notizen(f)] == [a]
    assert buch.fall(f)["notizen"] == 1 and buch.fall(f)["notizen_liste"][0]["text"] == "Opened."
    buch.schliessen(f)
    with pytest.raises(faelle.FallGeschlossen):
        buch.notiz(f, "nein")
    with pytest.raises(faelle.FallGeschlossen):
        buch.notiz_loeschen(f, a)


# ---------------------------------------------------------------------------
# 11.1: folders inside a case, and who wrote what
# ---------------------------------------------------------------------------
def _e(key, **extra):
    return {"key": key, "src": "outlook", "root": "outlook", "rel": f"inbox/{key}.eml",
            "titel": key, "datum": "2026-09-01", "wer": "Alice Beispiel", **extra}


def test_ordner_anlegen_umbenennen_loeschen(buch):
    f = buch.fall_anlegen("Nordwind")
    with pytest.raises(ValueError):
        buch.ordner_anlegen(f, "  ")
    belege = buch.ordner_anlegen(f, "Belege")
    assert buch.ordner_anlegen(f, "Belege") == belege, "the same name is the same folder"
    vertraege = buch.ordner_anlegen(f, "Verträge")
    assert [(o["name"], o["anzahl"]) for o in buch.ordner(f)] == [("Belege", 0), ("Verträge", 0)]
    assert buch.fall(f)["ordner"] == 2 and [o["id"] for o in buch.fall(f)["ordner_liste"]] == [belege, vertraege]
    buch.ordner_umbenennen(f, belege, "Rechnungen")
    with pytest.raises(ValueError):
        buch.ordner_umbenennen(f, belege, "Verträge")          # taken
    with pytest.raises(faelle.KeinOrdner):
        buch.ordner_umbenennen(f, 999, "x")
    anderer = buch.fall_anlegen("Berlin")
    with pytest.raises(faelle.KeinOrdner):
        buch.ordner_umbenennen(anderer, belege, "x")          # not this case's folder
    buch.hinzufuegen(f, [_e("m1"), _e("m2")], ordner_id=belege)
    buch.hinzufuegen(f, [_e("m3")])
    assert buch.keys(f, belege) == {"m1", "m2"} and buch.keys(f) == {"m1", "m2", "m3"}
    buch.ordner_loeschen(f, belege)
    assert [o["name"] for o in buch.ordner(f)] == ["Verträge"]
    assert buch.keys(f) == {"m1", "m2", "m3"}, "the folder went, its items stay – unsorted"
    assert all(e["ordner"] is None for e in buch.eintraege(f))
    buch.schliessen(f)
    with pytest.raises(faelle.FallGeschlossen):
        buch.ordner_anlegen(f, "Neu")
    with pytest.raises(faelle.FallGeschlossen):
        buch.ordner_loeschen(f, vertraege)


def test_eintraege_wandern_zwischen_ordnern(buch):
    f = buch.fall_anlegen("Nordwind")
    a = buch.ordner_anlegen(f, "A")
    b = buch.ordner_anlegen(f, "B")
    buch.hinzufuegen(f, [_e("m1"), _e("m2"), _e("m3")], ordner_id=a)
    assert buch.verschieben(f, ["m1", "m2", "nicht da"], b) == 2
    assert buch.keys(f, a) == {"m3"} and buch.keys(f, b) == {"m1", "m2"}
    assert buch.verschieben(f, ["m3"], None) == 1
    assert buch.keys(f, a) == set() and buch.eintraege(f)[2]["ordner"] is None
    assert buch.verschieben(f, [], a) == 0
    with pytest.raises(faelle.KeinOrdner):
        buch.verschieben(f, ["m1"], 999)
    # an item already in the case keeps its folder when added again
    assert buch.hinzufuegen(f, [_e("m1")], ordner_id=a) == 0
    assert buch.keys(f, b) == {"m1", "m2"}
    assert [(o["name"], o["anzahl"]) for o in buch.ordner(f)] == [("A", 0), ("B", 2)]


def test_listen_und_suchen_liegen_in_ordnern(buch):
    f = buch.fall_anlegen("Nordwind")
    o = buch.ordner_anlegen(f, "Korrespondenz")
    k = faelle.kriterien({"q": "Rechnung"})
    liste_id, neu = buch.liste_anlegen(f, k, [_e("m1"), _e("m2")], ordner_id=o)
    assert neu == 2 and buch.fall(f)["listen_liste"][0]["ordner"] == o
    assert buch.keys(f, o) == {"m1", "m2"}
    s = buch.speichern("Rechnungen", k, f, o)
    g = buch.gespeichert(s)
    assert g["ordner"] == o and g["ordner_name"] == "Korrespondenz"
    assert buch.anhaengen(s, f, None) and buch.gespeichert(s)["ordner"] is None
    assert buch.anhaengen(s, f, o) and buch.gespeichert(s)["ordner_name"] == "Korrespondenz"
    with pytest.raises(faelle.KeinOrdner):
        buch.speichern("x", k, f, 999)
    with pytest.raises(faelle.KeinOrdner):
        buch.liste_anlegen(f, k, [_e("m9")], ordner_id=999)
    # a search saved without a case has no folder either
    assert buch.gespeichert(buch.speichern("lose", k, None, o))["ordner"] is None
    # dropping the folder detaches the list and the search from it
    buch.ordner_loeschen(f, o)
    assert buch.fall(f)["listen_liste"][0]["ordner"] is None
    assert buch.gespeichert(s)["ordner"] is None and buch.gespeichert(s)["fall"] == f


def test_wer_schrieb_es(buch):
    f = buch.fall_anlegen("Nordwind")
    buch.hinzufuegen(f, [_e("m1")])
    buch.hinzufuegen(f, [_e("m2")], quelle=faelle.MCP)
    buch.hinzufuegen(f, [_e("m3")], quelle="unsinn")
    assert {e["key"]: e["quelle"] for e in buch.eintraege(f)} == {"m1": "ui", "m2": "mcp", "m3": "ui"}
    a = buch.notiz(f, "von der Seite")
    b = buch.notiz(f, "von Claude", quelle=faelle.MCP)
    assert {n["id"]: n["quelle"] for n in buch.notizen(f)} == {a: "ui", b: "mcp"}
    # the mark on a hit names the folder it sits in
    o = buch.ordner_anlegen(f, "Belege")
    buch.verschieben(f, ["m2"], o)
    zug = buch.zugehoerigkeit(["m1", "m2"])
    assert zug["m1"] == [{"id": f, "name": "Nordwind", "status": "offen", "ordner": None}]
    assert zug["m2"][0]["ordner"] == "Belege"


def test_ein_fallbuch_aus_11_0_bekommt_seine_spalten(tmp_path):
    """A case book written by 11.0 lacks the folder and origin columns;
    opening it adds them in place – rows and everything else stay."""
    import sqlite3
    pfad = tmp_path / "faelle.db"
    con = sqlite3.connect(pfad)
    con.executescript("""
        CREATE TABLE faelle(id INTEGER PRIMARY KEY, name TEXT NOT NULL, beschreibung TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'offen', angelegt TEXT NOT NULL, geaendert TEXT NOT NULL,
            geschlossen TEXT, exportiert TEXT, exportiert_wann TEXT);
        CREATE TABLE eintraege(id INTEGER PRIMARY KEY, fall_id INTEGER NOT NULL, key TEXT NOT NULL,
            src TEXT, root TEXT, rel TEXT, titel TEXT, datum TEXT, wer TEXT, hinzugefuegt TEXT NOT NULL,
            liste_id INTEGER, UNIQUE(fall_id, key));
        CREATE TABLE listen(id INTEGER PRIMARY KEY, fall_id INTEGER NOT NULL, kriterien TEXT NOT NULL,
            wann TEXT NOT NULL, anzahl INTEGER NOT NULL);
        CREATE TABLE notizen(id INTEGER PRIMARY KEY, fall_id INTEGER NOT NULL, wann TEXT NOT NULL, text TEXT NOT NULL);
        CREATE TABLE gespeichert(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kriterien TEXT NOT NULL,
            angelegt TEXT NOT NULL, zuletzt TEXT, treffer INTEGER, fall_id INTEGER);
        INSERT INTO faelle(id, name, angelegt, geaendert) VALUES(1, 'Alt', '2026-09-15T10:00:00+00:00', '2026-09-15T10:00:00+00:00');
        INSERT INTO eintraege(fall_id, key, src, root, rel, titel, hinzugefuegt)
            VALUES(1, 'mail:<a@x>', 'outlook', 'outlook', 'inbox/a.eml', 'A', '2026-09-15T10:00:00+00:00');
        INSERT INTO notizen(fall_id, wann, text) VALUES(1, '2026-09-15T10:00:00+00:00', 'alt');
        INSERT INTO gespeichert(name, kriterien, angelegt, fall_id) VALUES('S', '{"q": "x"}', '2026-09-15T10:00:00+00:00', 1);
    """)
    con.commit()
    con.close()
    buch = faelle.Fallbuch(pfad)
    fall = buch.fall(1)
    assert fall["name"] == "Alt" and fall["ordner"] == 0 and fall["ordner_liste"] == []
    assert fall["eintraege_liste"][0]["ordner"] is None and fall["eintraege_liste"][0]["quelle"] == "ui"
    assert fall["notizen_liste"][0]["quelle"] == "ui" and fall["suchen_liste"][0]["ordner"] is None
    o = buch.ordner_anlegen(1, "Neu")
    assert buch.verschieben(1, ["mail:<a@x>"], o) == 1
    spalten = {r[1] for r in sqlite3.connect(pfad).execute("PRAGMA table_info(eintraege)")}
    assert {"ordner_id", "quelle", "bemerkung"} <= spalten
    assert fall["eintraege_liste"][0]["bemerkung"] == ""


def test_bemerkung_am_eintrag(buch):
    """One remark per item: set, changed, removed; what is added again
    keeps the remark it has; a closed case refuses."""
    fid = buch.fall_anlegen("Nordwind")
    buch.hinzufuegen(fid, [{"key": "a", "src": "outlook", "root": "outlook", "rel": "a.eml", "bemerkung": " Warum "},
                           {"key": "b", "src": "teams", "root": "teams", "rel": "b.html"}])
    assert {e["key"]: e["bemerkung"] for e in buch.eintraege(fid)} == {"a": "Warum", "b": ""}
    assert buch.bemerkung_setzen(fid, "b", " Der Grund ") is True
    assert buch.bemerkung_setzen(fid, "x", "nichts") is False
    assert buch.bemerkung_setzen(fid, "a", "") is True
    assert {e["key"]: e["bemerkung"] for e in buch.eintraege(fid)} == {"a": "", "b": "Der Grund"}
    buch.hinzufuegen(fid, [{"key": "b", "src": "teams", "root": "teams", "rel": "b.html", "bemerkung": "neu"}])
    assert {e["key"]: e["bemerkung"] for e in buch.eintraege(fid)}["b"] == "Der Grund"
    buch.schliessen(fid)
    with pytest.raises(faelle.FallGeschlossen):
        buch.bemerkung_setzen(fid, "b", "x")
