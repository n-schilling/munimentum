"""Tests für folders.py – der Ordnerbaum und die Auswahlregeln.

Bis 2.x lief beides in einem: der Export las bei jedem Lauf die komplette
Struktur und entschied dabei, was er holt. Am echten Postfach 110 s für 417
Ordner – und die Auswahl griff nur auf oberster Ebene und nur über den
Anzeigenamen. „Kunden“ mit 288 Unterordnern war eine Entscheidung: ganz oder
gar nicht.

Zwei Zusagen stehen hier im Mittelpunkt:

  * Die LETZTE zutreffende Regel gewinnt. Nur so ist „alles außer Archiv, dort
    aber den einen Unterordner doch“ sagbar.
  * `*` bleibt in einer Ebene. Sonst zöge „E-Mail/*“ das halbe Postfach mit.
"""

import json

import pytest

import folders


# --------------------------------------------------------------------------
# Muster
# --------------------------------------------------------------------------
@pytest.mark.parametrize("pfad,muster,erwartet", [
    ("E-Mail/Archiv", "E-Mail/Archiv/**", True),        # der Ordner selbst …
    ("E-Mail/Archiv/Alt", "E-Mail/Archiv/**", True),    # … und alles darunter
    ("E-Mail/Archiv/A/B/C", "E-Mail/Archiv/**", True),
    ("E-Mail/Archivx", "E-Mail/Archiv/**", False),      # kein Präfixtreffer
    ("E-Mail/Kunden/A", "E-Mail/*/A", True),
    ("E-Mail/Kunden/X/A", "E-Mail/*/A", False),         # * überspringt kein /
    ("E-Mail/Kunden/X/A", "E-Mail/**/A", True),
    ("E-Mail/Kunden/Alt 2019", "E-Mail/Kunden/Alt *", True),
    ("E-Mail/ARCHIV", "E-Mail/Archiv/**", True),        # Groß/klein egal
    ("E-Mail/A+B", "E-Mail/A+B", True),                 # Sonderzeichen wörtlich
    ("E-Mail/AxB", "E-Mail/A?B", True),
    ("E-Mail/Beliebig", "**", True),
    ("E-Mail/X", "", False),                            # leeres Muster trifft nie
])
def test_muster(pfad, muster, erwartet):
    assert folders.passt(pfad, muster) is erwartet


def test_stern_zieht_nicht_das_halbe_postfach_mit():
    """Würde * über / laufen, hätte „E-Mail/*“ 417 Ordner statt 13 getroffen."""
    assert folders.passt("E-Mail/Kunden", "E-Mail/*")
    assert not folders.passt("E-Mail/Kunden/Contoso", "E-Mail/*")


# --------------------------------------------------------------------------
# Regeln: die letzte gewinnt
# --------------------------------------------------------------------------
REGELN = folders.lies_regeln("- E-Mail/Archiv/**\n+ E-Mail/Archiv/Wichtig/**")


@pytest.mark.parametrize("pfad,erwartet", [
    ("E-Mail/Posteingang", True),                # keine Regel trifft -> Vorgabe
    ("E-Mail/Archiv", False),
    ("E-Mail/Archiv/Alt", False),
    ("E-Mail/Archiv/Wichtig", True),             # die spätere Regel gewinnt
    ("E-Mail/Archiv/Wichtig/2024", True),
])
def test_letzte_regel_gewinnt(pfad, erwartet):
    assert folders.gilt(pfad, REGELN) is erwartet


def test_reihenfolge_ist_nicht_egal():
    """Dreht man die beiden um, bleibt am Ende alles aus – genau das ist der
    Unterschied zu einer Menge von Mustern."""
    andersrum = list(reversed(REGELN))
    assert folders.gilt("E-Mail/Archiv/Wichtig", andersrum) is False


def test_ohne_regeln_kommt_alles_mit():
    """Wer nichts einstellt, bekommt sein Postfach – nicht Leere."""
    assert folders.gilt("E-Mail/Irgendwas", []) is True


def test_erklaere_nennt_die_entscheidende_regel():
    an, regel = folders.erklaere("E-Mail/Archiv/Wichtig", REGELN)
    assert an is True and regel == (True, "E-Mail/Archiv/Wichtig/**")
    an, regel = folders.erklaere("E-Mail/Posteingang", REGELN)
    assert an is True and regel is None      # niemand hat entschieden


# --------------------------------------------------------------------------
# Regeln lesen und schreiben
# --------------------------------------------------------------------------
def test_zeilen_lesen():
    r = folders.lies_regeln(
        "# Kommentar\n\n- E-Mail/Archiv/**\n  + E-Mail/Kunden/**  \nE-Mail/Sonstiges\n-\n")
    assert r == [(False, "E-Mail/Archiv/**"), (True, "E-Mail/Kunden/**"),
                 (True, "E-Mail/Sonstiges")]


def test_ohne_vorzeichen_heisst_einschliessen():
    """Wer eine Liste von Ordnern hinschreibt, meint fast immer „diese“."""
    assert folders.lies_regeln("E-Mail/Kunden") == [(True, "E-Mail/Kunden")]


def test_schreiben_und_lesen_passen_zusammen():
    text = folders.schreibe_regeln(REGELN)
    assert folders.lies_regeln(text) == REGELN


def test_alte_namensliste_wird_uebersetzt():
    """Niemand soll seine über Jahre gepflegte Auswahl neu eintippen."""
    r = folders.aus_namensliste(["Archiv", "Junk-E-Mail"])
    assert r == [(False, "E-Mail/Archiv/**"), (False, "E-Mail/Junk-E-Mail/**")]
    assert folders.gilt("E-Mail/Archiv/Alt", r) is False
    assert folders.gilt("E-Mail/Posteingang", r) is True


# --------------------------------------------------------------------------
# Der Baum auf der Platte
# --------------------------------------------------------------------------
def _eintrag(i, pfad, n=1):
    return {"id": f"id{i}", "pfad": pfad, "name": pfad.rsplit("/", 1)[-1], "elemente": n}


def test_erster_abgleich_meldet_nichts_als_neu(tmp_path):
    """„417 Ordner neu dazugekommen“ wäre formal wahr und trotzdem Unsinn."""
    d = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A"), _eintrag(2, "E-Mail/B")])
    assert d["neu"] == [] and d["verschwunden"] == []


def test_zweiter_abgleich_meldet_die_aenderung(tmp_path):
    erst = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A")])
    zweit = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A"), _eintrag(2, "E-Mail/B")], erst)
    assert zweit["neu"] == ["E-Mail/B"]
    dritt = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A")], zweit)
    assert dritt["verschwunden"] == ["E-Mail/B"]


def test_umbenennen_faellt_nicht_aus_dem_export(tmp_path):
    """Der Grund, warum je Ordner die ID mitgespeichert wird: nur über den Pfad
    sähe ein umbenannter Ordner aus wie „weg“ plus „neu“."""
    erst = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/Alt")])
    zweit = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/Neu")], erst)
    assert zweit["neu"] == [] and zweit["verschwunden"] == []
    assert zweit["umbenannt"] == ["E-Mail/Alt -> E-Mail/Neu"]


def test_laden_und_speichern(tmp_path):
    folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A", 5)])
    d = folders.lade(tmp_path)
    assert d["ordner"][0]["pfad"] == "E-Mail/A"
    assert not folders.pfad(tmp_path).with_name(folders.DATEI + ".tmp").exists()


def test_kaputte_datei_macht_keinen_krach(tmp_path):
    folders.pfad(tmp_path).write_text("kein json", encoding="utf-8")
    assert folders.lade(tmp_path) is None


def test_fremde_datei_wird_nicht_geglaubt(tmp_path):
    folders.pfad(tmp_path).write_text(json.dumps({"etwas": "anderes"}), encoding="utf-8")
    assert folders.lade(tmp_path) is None


def test_ohne_datei(tmp_path):
    assert folders.lade(tmp_path / "leer") is None


def test_zusammenfassung_zaehlt_die_auswahl(tmp_path):
    folders.speichere(tmp_path, [
        _eintrag(1, "E-Mail/Posteingang", 100),
        _eintrag(2, "E-Mail/Archiv", 14000),
        _eintrag(3, "E-Mail/Archiv/Wichtig", 12),
    ])
    z = folders.zusammenfassung(folders.lade(tmp_path), REGELN)
    assert z["ordner_gesamt"] == 3 and z["ordner_gewaehlt"] == 2
    assert z["mails_gesamt"] == 14112
    assert z["mails_gewaehlt"] == 112        # Posteingang + Archiv/Wichtig


# --------------------------------------------------------------------------
# Die Exportliste: was der nächste Lauf täte, ohne ihn zu starten
# --------------------------------------------------------------------------
def _mails(ordner, pfad, anzahl):
    ziel = ordner / pfad
    ziel.mkdir(parents=True, exist_ok=True)
    for i in range(anzahl):
        (ziel / f"m{i}.eml").write_text("x", encoding="utf-8")


def test_plan_trennt_gewaehlt_ausgelassen_und_nur_noch_im_archiv(tmp_path):
    """Die drei Listen, die man sonst im Kopf zusammensetzen müsste."""
    folders.speichere(tmp_path, [
        _eintrag(1, "E-Mail/Posteingang", 100),
        _eintrag(2, "E-Mail/Archiv", 14000),
        _eintrag(3, "E-Mail/Archiv/Wichtig", 12),
    ])
    _mails(tmp_path, "E-Mail/Posteingang", 3)
    _mails(tmp_path, "E-Mail/Archiv", 2)
    _mails(tmp_path, "E-Mail/Weg", 4)          # in Outlook gelöscht, hier geblieben

    p = folders.plan(tmp_path, REGELN)
    assert [z["pfad"] for z in p["an"]] == ["E-Mail/Posteingang", "E-Mail/Archiv/Wichtig"]
    assert [z["pfad"] for z in p["aus"]] == ["E-Mail/Archiv"]
    assert [z["pfad"] for z in p["weg"]] == ["E-Mail/Weg"]
    assert p["mails_an"] == 112 and p["mails_aus"] == 14000 and p["mails_weg"] == 4


def test_plan_nennt_die_regel_die_entschied(tmp_path):
    """Ohne den Grund bliebe „warum ist der Ordner aus?“ unbeantwortet."""
    folders.speichere(tmp_path, [
        _eintrag(1, "E-Mail/Posteingang", 1),
        _eintrag(2, "E-Mail/Archiv", 2),
        _eintrag(3, "E-Mail/Archiv/Wichtig", 3),
    ])
    p = folders.plan(tmp_path, REGELN)
    grund = {z["pfad"]: z["regel"] for z in p["an"] + p["aus"]}
    assert grund["E-Mail/Archiv"] == "- E-Mail/Archiv/**"
    assert grund["E-Mail/Archiv/Wichtig"] == "+ E-Mail/Archiv/Wichtig/**"
    # Ohne Treffer entschied die Vorgabe, nicht eine Regel – also kein Grund.
    assert grund["E-Mail/Posteingang"] is None


def test_plan_zeigt_ausgelassene_ordner_mit_bestand(tmp_path):
    """„Ausgelassen“ heißt nicht „leer“: was schon da ist, bleibt liegen."""
    folders.speichere(tmp_path, [_eintrag(2, "E-Mail/Archiv", 14000)])
    _mails(tmp_path, "E-Mail/Archiv", 7)
    p = folders.plan(tmp_path, REGELN)
    assert p["aus"][0]["archiv"] == 7


def test_plan_haelt_kalender_und_kontakte_heraus(tmp_path):
    """Sonst stünden sie als „nicht mehr im Postfach“ da – sie waren nie ein
    Postfachordner."""
    folders.speichere(tmp_path, [_eintrag(1, "E-Mail/Posteingang", 1)])
    _mails(tmp_path, "kalender", 3)
    _mails(tmp_path, "kontakte", 2)
    assert folders.plan(tmp_path, [])["weg"] == []


def test_plan_ohne_baum(tmp_path):
    p = folders.plan(tmp_path, REGELN)
    assert p["an"] == [] and p["aus"] == [] and p["weg"] == []


def test_auf_platte_zaehlt_nur_eml(tmp_path):
    _mails(tmp_path, "E-Mail/A", 2)
    (tmp_path / "E-Mail/A/notiz.txt").write_text("x", encoding="utf-8")
    (tmp_path / "E-Mail/Leer").mkdir(parents=True)
    gefunden = folders.auf_platte(tmp_path, ["E-Mail"])
    assert gefunden == {"E-Mail/A": 2}          # leerer Ordner taucht nicht auf


def test_auf_platte_ohne_wurzel(tmp_path):
    assert folders.auf_platte(tmp_path, ["gibtsnicht"]) == {}
