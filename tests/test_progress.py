"""Tests für progress.py – Fortschritt maschinenlesbar melden.

Der Kanal existiert, damit der Balken in der App nicht davon abhängt, wie ein
Skript seine Fortschrittssätze formuliert. Zwei Zusagen tragen das:

  * Ohne EXPORT_PROGRESS bleibt die Ausgabe unverändert. Wer die Skripte von
    Hand im Terminal aufruft, soll die Protokollzeilen nicht sehen.
  * lies() erkennt gewöhnliche Ausgabe als solche. Eine Skriptzeile, die
    zufällig nach Fortschritt aussieht, darf nicht als Zahl gedeutet werden.
"""

import json

import pytest

import progress


@pytest.fixture(autouse=True)
def leise(monkeypatch):
    monkeypatch.delenv("EXPORT_PROGRESS", raising=False)


# --------------------------------------------------------------------------
# Nur melden, wenn jemand zuhört
# --------------------------------------------------------------------------
@pytest.mark.parametrize("wert,erwartet", [
    (None, False), ("", False), ("0", False), ("false", False), ("no", False),
    ("nein", False), ("off", False), ("1", True), ("true", True), ("ja", True),
])
def test_aktiv(monkeypatch, wert, erwartet):
    if wert is not None:
        monkeypatch.setenv("EXPORT_PROGRESS", wert)
    assert progress.aktiv() is erwartet


def test_melde_schweigt_ohne_variable(capsys):
    """Beim Aufruf von Hand bleibt die Ausgabe genau wie zuvor."""
    progress.melde(5, 10, "chats")
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------
# Melden
# --------------------------------------------------------------------------
def test_melde_mit_gesamtzahl(monkeypatch, capsys):
    monkeypatch.setenv("EXPORT_PROGRESS", "1")
    progress.melde(37, 1200, "chats")
    zeile = capsys.readouterr().out.strip()
    assert zeile.startswith(progress.MARKE)
    assert json.loads(zeile[len(progress.MARKE):]) == {
        "done": 37, "total": 1200, "what": "chats"}


def test_melde_ohne_gesamtzahl(monkeypatch, capsys):
    """Der Outlook-Export entdeckt seine Mails erst im Laufen – eine erfundene
    Prozentzahl wäre schlechter als gar keine."""
    monkeypatch.setenv("EXPORT_PROGRESS", "1")
    progress.melde(1234, what="mails")
    daten = json.loads(capsys.readouterr().out.strip()[len(progress.MARKE):])
    assert daten == {"done": 1234, "what": "mails"}
    assert "total" not in daten


def test_melde_haelt_keinen_lauf_auf(monkeypatch, capsys):
    """Eine misslungene Meldung darf einen stundenlangen Export nicht beenden."""
    monkeypatch.setenv("EXPORT_PROGRESS", "1")

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


def test_melden_und_lesen_passen_zusammen(monkeypatch, capsys):
    monkeypatch.setenv("EXPORT_PROGRESS", "1")
    progress.melde(7, 8, "embeddings")
    assert progress.lies(capsys.readouterr().out) == {
        "done": 7, "total": 8, "what": "embeddings"}


# --------------------------------------------------------------------------
# Die Skripte melden auch wirklich
# --------------------------------------------------------------------------
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
