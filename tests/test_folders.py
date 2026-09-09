"""Tests for folders.py – the folder tree and the selection rules.

Reading the complete structure on every run is expensive (a couple of
minutes for several hundred folders on a real mailbox), and a selection that
only works at the top level via display names makes a branch with hundreds of
subfolders an all-or-nothing decision – hence the stored tree and path-based
rules.

Two promises take centre stage here:

  * The LAST matching rule wins. Only that way can "everything except
    Archiv, but that one subfolder of it after all" be expressed.
  * `*` stays within one level. Otherwise "E-Mail/*" would drag along half
    the mailbox.
"""

import json

import pytest

import folders


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------
@pytest.mark.parametrize("pfad,muster,erwartet", [
    ("E-Mail/Archiv", "E-Mail/Archiv/**", True),        # the folder itself …
    ("E-Mail/Archiv/Alt", "E-Mail/Archiv/**", True),    # … and everything below
    ("E-Mail/Archiv/A/B/C", "E-Mail/Archiv/**", True),
    ("E-Mail/Archivx", "E-Mail/Archiv/**", False),      # no prefix match
    ("E-Mail/Kunden/A", "E-Mail/*/A", True),
    ("E-Mail/Kunden/X/A", "E-Mail/*/A", False),         # * does not skip a /
    ("E-Mail/Kunden/X/A", "E-Mail/**/A", True),
    ("E-Mail/Kunden/Alt 2019", "E-Mail/Kunden/Alt *", True),
    ("E-Mail/ARCHIV", "E-Mail/Archiv/**", True),        # case does not matter
    ("E-Mail/A+B", "E-Mail/A+B", True),                 # special chars literal
    ("E-Mail/AxB", "E-Mail/A?B", True),
    ("E-Mail/Beliebig", "**", True),
    ("E-Mail/X", "", False),                            # empty pattern never hits
])
def test_muster(pfad, muster, erwartet):
    assert folders.passt(pfad, muster) is erwartet


def test_stern_zieht_nicht_das_halbe_postfach_mit():
    """If * crossed /, "E-Mail/*" would match the whole tree, not one level."""
    assert folders.passt("E-Mail/Kunden", "E-Mail/*")
    assert not folders.passt("E-Mail/Kunden/Contoso", "E-Mail/*")


# --------------------------------------------------------------------------
# Rules: the last one wins
# --------------------------------------------------------------------------
REGELN = folders.lies_regeln("- E-Mail/Archiv/**\n+ E-Mail/Archiv/Wichtig/**")


@pytest.mark.parametrize("pfad,erwartet", [
    ("E-Mail/Posteingang", True),                # no rule matches -> default
    ("E-Mail/Archiv", False),
    ("E-Mail/Archiv/Alt", False),
    ("E-Mail/Archiv/Wichtig", True),             # the later rule wins
    ("E-Mail/Archiv/Wichtig/2024", True),
])
def test_letzte_regel_gewinnt(pfad, erwartet):
    assert folders.gilt(pfad, REGELN) is erwartet


def test_reihenfolge_ist_nicht_egal():
    """Swap the two and everything ends up off – exactly the difference
    from a mere set of patterns."""
    andersrum = list(reversed(REGELN))
    assert folders.gilt("E-Mail/Archiv/Wichtig", andersrum) is False


def test_ohne_regeln_kommt_alles_mit():
    """Whoever configures nothing gets their mailbox – not emptiness."""
    assert folders.gilt("E-Mail/Irgendwas", []) is True


def test_erklaere_nennt_die_entscheidende_regel():
    an, regel = folders.erklaere("E-Mail/Archiv/Wichtig", REGELN)
    assert an is True and regel == (True, "E-Mail/Archiv/Wichtig/**")
    an, regel = folders.erklaere("E-Mail/Posteingang", REGELN)
    assert an is True and regel is None      # nobody decided


# --------------------------------------------------------------------------
# Reading and writing rules
# --------------------------------------------------------------------------
def test_zeilen_lesen():
    r = folders.lies_regeln(
        "# Kommentar\n\n- E-Mail/Archiv/**\n  + E-Mail/Kunden/**  \nE-Mail/Sonstiges\n-\n")
    assert r == [(False, "E-Mail/Archiv/**"), (True, "E-Mail/Kunden/**"),
                 (True, "E-Mail/Sonstiges")]


def test_ohne_vorzeichen_heisst_einschliessen():
    """Whoever writes down a list of folders almost always means "these"."""
    assert folders.lies_regeln("E-Mail/Kunden") == [(True, "E-Mail/Kunden")]


def test_schreiben_und_lesen_passen_zusammen():
    text = folders.schreibe_regeln(REGELN)
    assert folders.lies_regeln(text) == REGELN


def test_alte_namensliste_wird_uebersetzt():
    """Nobody should retype a selection curated over years."""
    r = folders.aus_namensliste(["Archiv", "Junk-E-Mail"])
    assert r == [(False, "E-Mail/Archiv/**"), (False, "E-Mail/Junk-E-Mail/**")]
    assert folders.gilt("E-Mail/Archiv/Alt", r) is False
    assert folders.gilt("E-Mail/Posteingang", r) is True


# --------------------------------------------------------------------------
# The tree on disk
# --------------------------------------------------------------------------
def _eintrag(i, pfad, n=1):
    return {"id": f"id{i}", "pfad": pfad, "name": pfad.rsplit("/", 1)[-1], "elemente": n}


def test_erster_abgleich_meldet_nichts_als_neu(tmp_path):
    """"Every folder is new" would be formally true and still nonsense."""
    d = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A"), _eintrag(2, "E-Mail/B")])
    assert d["neu"] == [] and d["verschwunden"] == []


def test_zweiter_abgleich_meldet_die_aenderung(tmp_path):
    erst = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A")])
    zweit = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A"), _eintrag(2, "E-Mail/B")], erst)
    assert zweit["neu"] == ["E-Mail/B"]
    dritt = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A")], zweit)
    assert dritt["verschwunden"] == ["E-Mail/B"]


def test_umbenennen_faellt_nicht_aus_dem_export(tmp_path):
    """The reason each folder's ID is stored along: by path alone a renamed
    folder would look like "gone" plus "new"."""
    erst = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/Alt")])
    zweit = folders.speichere(tmp_path, [_eintrag(1, "E-Mail/Neu")], erst)
    assert zweit["neu"] == [] and zweit["verschwunden"] == []
    assert zweit["umbenannt"] == ["E-Mail/Alt -> E-Mail/Neu"]


def test_laden_und_speichern(tmp_path):
    import state_db
    folders.speichere(tmp_path, [_eintrag(1, "E-Mail/A", 5)])
    d = folders.lade(tmp_path)
    assert d["ordner"][0]["pfad"] == "E-Mail/A"
    # The tree lives in the folder's state.db, not in a loose file.
    assert (tmp_path / state_db.DB_NAME).exists()
    assert not (tmp_path / folders.DATEI).exists()


def test_kaputter_eintrag_macht_keinen_krach(tmp_path):
    import state_db
    state_db.StateDb(tmp_path).kv_schreiben("baum", "kein json")
    assert folders.lade(tmp_path) is None


def test_fremder_eintrag_wird_nicht_geglaubt(tmp_path):
    import state_db
    state_db.StateDb(tmp_path).kv_schreiben(
        "baum", json.dumps({"etwas": "anderes"}))
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
    assert z["mails_gewaehlt"] == 112        # inbox + Archiv/Wichtig


# --------------------------------------------------------------------------
# The export plan: what the next run would do, without starting it
# --------------------------------------------------------------------------
def _mails(ordner, pfad, anzahl):
    ziel = ordner / pfad
    ziel.mkdir(parents=True, exist_ok=True)
    for i in range(anzahl):
        (ziel / f"m{i}.eml").write_text("x", encoding="utf-8")


def test_plan_trennt_gewaehlt_ausgelassen_und_nur_noch_im_archiv(tmp_path):
    """The three lists one would otherwise have to assemble in one's head."""
    folders.speichere(tmp_path, [
        _eintrag(1, "E-Mail/Posteingang", 100),
        _eintrag(2, "E-Mail/Archiv", 14000),
        _eintrag(3, "E-Mail/Archiv/Wichtig", 12),
    ])
    _mails(tmp_path, "E-Mail/Posteingang", 3)
    _mails(tmp_path, "E-Mail/Archiv", 2)
    _mails(tmp_path, "E-Mail/Weg", 4)          # deleted in Outlook, kept here

    p = folders.plan(tmp_path, REGELN)
    assert [z["pfad"] for z in p["an"]] == ["E-Mail/Posteingang", "E-Mail/Archiv/Wichtig"]
    assert [z["pfad"] for z in p["aus"]] == ["E-Mail/Archiv"]
    assert [z["pfad"] for z in p["weg"]] == ["E-Mail/Weg"]
    assert p["mails_an"] == 112 and p["mails_aus"] == 14000 and p["mails_weg"] == 4


def test_plan_nennt_die_regel_die_entschied(tmp_path):
    """Without the reason, "why is this folder off?" would go unanswered."""
    folders.speichere(tmp_path, [
        _eintrag(1, "E-Mail/Posteingang", 1),
        _eintrag(2, "E-Mail/Archiv", 2),
        _eintrag(3, "E-Mail/Archiv/Wichtig", 3),
    ])
    p = folders.plan(tmp_path, REGELN)
    grund = {z["pfad"]: z["regel"] for z in p["an"] + p["aus"]}
    assert grund["E-Mail/Archiv"] == "- E-Mail/Archiv/**"
    assert grund["E-Mail/Archiv/Wichtig"] == "+ E-Mail/Archiv/Wichtig/**"
    # Without a match the default decided, not a rule – hence no reason.
    assert grund["E-Mail/Posteingang"] is None


def test_plan_zeigt_ausgelassene_ordner_mit_bestand(tmp_path):
    """"Skipped" does not mean "empty": what is already there stays put."""
    folders.speichere(tmp_path, [_eintrag(2, "E-Mail/Archiv", 14000)])
    _mails(tmp_path, "E-Mail/Archiv", 7)
    p = folders.plan(tmp_path, REGELN)
    assert p["aus"][0]["archiv"] == 7


def test_plan_haelt_kalender_und_kontakte_heraus(tmp_path):
    """Otherwise they would show up as "no longer in the mailbox" – they
    never were a mailbox folder."""
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
    assert gefunden == {"E-Mail/A": 2}          # empty folder does not show up


def test_auf_platte_ohne_wurzel(tmp_path):
    assert folders.auf_platte(tmp_path, ["gibtsnicht"]) == {}
