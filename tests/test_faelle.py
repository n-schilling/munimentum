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
                 "to": "", "folder": "", "filetype": "", "gone": True, "fall": 3}
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
