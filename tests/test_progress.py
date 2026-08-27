"""Tests für progress.py – Fortschritt maschinenlesbar melden.

Der Kanal existiert, damit der Balken in der App nicht davon abhängt, wie ein
Skript seine Fortschrittssätze formuliert. Die tragende Zusage: lies() erkennt
gewöhnliche Ausgabe als solche – eine Skriptzeile, die zufällig nach
Fortschritt aussieht, darf nicht als Zahl gedeutet werden. Gesendet wird
immer; die App ist der einzige Aufrufer und filtert die Marker selbst.
"""

import json

import pytest

import progress


# --------------------------------------------------------------------------
# Melden
# --------------------------------------------------------------------------
def test_melde_mit_gesamtzahl(capsys):
    progress.melde(37, 1200, "chats")
    zeile = capsys.readouterr().out.strip()
    assert zeile.startswith(progress.MARKE)
    assert json.loads(zeile[len(progress.MARKE):]) == {
        "done": 37, "total": 1200, "what": "chats"}


def test_melde_ohne_gesamtzahl(capsys):
    """Der Outlook-Export entdeckt seine Mails erst im Laufen – eine erfundene
    Prozentzahl wäre schlechter als gar keine."""
    progress.melde(1234, what="mails")
    daten = json.loads(capsys.readouterr().out.strip()[len(progress.MARKE):])
    assert daten == {"done": 1234, "what": "mails"}
    assert "total" not in daten


def test_melde_haelt_keinen_lauf_auf(monkeypatch, capsys):
    """Eine misslungene Meldung darf einen stundenlangen Export nicht beenden."""

    def kaputt(*a, **kw):
        raise OSError("Rohr zu")
    monkeypatch.setattr("builtins.print", kaputt)
    progress.melde(1, 2)                      # wirft nicht


# --------------------------------------------------------------------------
# Lesen
# --------------------------------------------------------------------------
def test_lies_erkennt_die_eigene_zeile():
    assert progress.lies('@@PROGRESS@@ {"done": 5, "total": 9}') == {"done": 5, "total": 9}
    assert progress.lies('  @@PROGRESS@@ {"done": 1}  ') == {"done": 1}


@pytest.mark.parametrize("zeile", [
    "✓ [37/1200] neu · Chat: Alice",           # echte Ausgabe des Skripts
    "  … 500/12000 eingebettet",
    "@@PROGRESS@@ kein json",
    '@@PROGRESS@@ {"ohne": "done"}',
    '@@PROGRESS@@ [1, 2]',
    "", None,
])
def test_lies_gibt_gewoehnliche_zeilen_zurueck(zeile):
    """None heißt: ins Protokoll damit, nicht in den Balken."""
    assert progress.lies(zeile) is None


def test_melden_und_lesen_passen_zusammen(capsys):
    progress.melde(7, 8, "embeddings")
    assert progress.lies(capsys.readouterr().out) == {
        "done": 7, "total": 8, "what": "embeddings"}


# --------------------------------------------------------------------------
# Ergebnis: was der Schritt bewirkt hat
# --------------------------------------------------------------------------
def test_ergebnis_melden_und_lesen(capsys):
    progress.ergebnis(0, unchanged=67, excluded=4, errors=1,
                      extra={"moved": 2})
    assert progress.lies_ergebnis(capsys.readouterr().out) == {
        "new": 0, "unchanged": 67, "excluded": 4, "errors": 1,
        "extra": {"moved": 2}}


def test_ergebnis_haelt_keinen_lauf_auf(monkeypatch):

    def kaputt(*a, **kw):
        raise OSError("Rohr zu")
    monkeypatch.setattr("builtins.print", kaputt)
    progress.ergebnis(3)                       # wirft nicht


@pytest.mark.parametrize("zeile", [
    "Fertig. Neu exportiert: 0, übersprungen: 67.",   # echte Ausgabe des Skripts
    '@@PROGRESS@@ {"done": 5}',                       # der andere Kanal
    '@@RESULT@@ {"ohne": "neu"}',
    '@@RESULT@@ kein json',
    "", None,
])
def test_lies_ergebnis_gibt_gewoehnliche_zeilen_zurueck(zeile):
    assert progress.lies_ergebnis(zeile) is None


def test_die_beiden_kanaele_verwechseln_sich_nicht(capsys):
    """Beide laufen über dieselbe Leitung – jeder darf nur seine Zeile lesen."""
    progress.melde(5, 10)
    progress.ergebnis(7)
    fortschritt, fazit = capsys.readouterr().out.strip().splitlines()
    assert progress.lies(fortschritt) == {"done": 5, "total": 10}
    assert progress.lies_ergebnis(fortschritt) is None
    assert progress.lies_ergebnis(fazit) == {"new": 7}
    assert progress.lies(fazit) is None


# --------------------------------------------------------------------------
# Die Skripte melden auch wirklich
# --------------------------------------------------------------------------
@pytest.mark.parametrize("modul", ["teams_export", "outlook_export",
                                   "onedrive_export", "rag_index",
                                   "combined_search"])
def test_export_meldet_sein_ergebnis(modul):
    """Ohne diese Meldung indiziert die App nach jedem Lauf blind weiter –
    und die Lauf-Historie bliebe für den Schritt leer."""
    from pathlib import Path
    quelle = (Path(__file__).resolve().parent.parent / f"{modul}.py").read_text(
        encoding="utf-8")
    assert "progress.ergebnis(" in quelle, f"{modul} meldet sein Ergebnis nicht"



@pytest.mark.parametrize("modul,stelle", [
    ("teams_export", "chats"),          # kennt die Gesamtzahl
    ("outlook_export", "mails"),        # kennt sie nicht
    ("rag_index", "embeddings"),
    ("combined_search", "mails"),
])
def test_skript_meldet_fortschritt(modul, stelle):
    """Sonst bliebe der Balken bei einem Schritt stehen, ohne dass es auffällt."""
    from pathlib import Path
    quelle = (Path(__file__).resolve().parent.parent / f"{modul}.py").read_text(
        encoding="utf-8")
    assert "import progress" in quelle, f"{modul} bindet progress nicht ein"
    assert 'progress.melde(' in quelle, f"{modul} meldet nichts"
    assert f'"{stelle}"' in quelle, f"{modul} meldet nicht als {stelle}"


# --------------------------------------------------------------------------
# Fehler-Ereignis: strukturiert statt Prosa-Muster
# --------------------------------------------------------------------------
def test_fehler_melden_und_lesen(capsys):
    progress.fehler("token_expired")
    zeile = capsys.readouterr().out.strip()
    assert progress.lies_fehler(zeile) == {"error": "token_expired"}
    assert progress.lies(zeile) is None and progress.lies_ergebnis(zeile) is None


def test_lies_fehler_laesst_gewoehnliche_zeilen_durch():
    assert progress.lies_fehler("Abgebrochen: Token abgelaufen.") is None
    assert progress.lies_fehler('@@ERROR@@ kein json') is None
    assert progress.lies_fehler(None) is None
