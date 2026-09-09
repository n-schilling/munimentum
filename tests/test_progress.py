"""Tests for progress.py – reporting progress machine-readably.

The channel exists so that the bar in the app does not depend on how a
script phrases its progress sentences. The load-bearing promise: lies()
recognises ordinary output as such – a script line that happens to look
like progress must not be read as a number. Sending always happens; the
app is the only caller and filters the markers itself.
"""

import json

import pytest

import progress


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def test_melde_mit_gesamtzahl(capsys):
    progress.melde(37, 1200, "chats")
    zeile = capsys.readouterr().out.strip()
    assert zeile.startswith(progress.MARKE)
    assert json.loads(zeile[len(progress.MARKE):]) == {
        "done": 37, "total": 1200, "what": "chats"}


def test_melde_ohne_gesamtzahl(capsys):
    """The Outlook export discovers its mails only while running – an
    invented percentage would be worse than none."""
    progress.melde(1234, what="mails")
    daten = json.loads(capsys.readouterr().out.strip()[len(progress.MARKE):])
    assert daten == {"done": 1234, "what": "mails"}
    assert "total" not in daten


def test_melde_haelt_keinen_lauf_auf(monkeypatch, capsys):
    """A failed report must not end an export that runs for hours."""

    def kaputt(*a, **kw):
        raise OSError("Rohr zu")
    monkeypatch.setattr("builtins.print", kaputt)
    progress.melde(1, 2)                      # does not raise


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def test_lies_erkennt_die_eigene_zeile():
    assert progress.lies('@@PROGRESS@@ {"done": 5, "total": 9}') == {"done": 5, "total": 9}
    assert progress.lies('  @@PROGRESS@@ {"done": 1}  ') == {"done": 1}


@pytest.mark.parametrize("zeile", [
    "✓ [37/1200] neu · Chat: Alice",           # real script output
    "  … 500/12000 eingebettet",
    "@@PROGRESS@@ kein json",
    '@@PROGRESS@@ {"ohne": "done"}',
    '@@PROGRESS@@ [1, 2]',
    "", None,
])
def test_lies_gibt_gewoehnliche_zeilen_zurueck(zeile):
    """None means: into the log with it, not into the bar."""
    assert progress.lies(zeile) is None


def test_melden_und_lesen_passen_zusammen(capsys):
    progress.melde(7, 8, "embeddings")
    assert progress.lies(capsys.readouterr().out) == {
        "done": 7, "total": 8, "what": "embeddings"}


# --------------------------------------------------------------------------
# Result: what the step accomplished
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
    progress.ergebnis(3)                       # does not raise


@pytest.mark.parametrize("zeile", [
    "Fertig. Neu exportiert: 0, übersprungen: 67.",   # real script output
    '@@PROGRESS@@ {"done": 5}',                       # the other channel
    '@@RESULT@@ {"ohne": "neu"}',
    '@@RESULT@@ kein json',
    "", None,
])
def test_lies_ergebnis_gibt_gewoehnliche_zeilen_zurueck(zeile):
    assert progress.lies_ergebnis(zeile) is None


def test_die_beiden_kanaele_verwechseln_sich_nicht(capsys):
    """Both run over the same pipe – each may only read its own line."""
    progress.melde(5, 10)
    progress.ergebnis(7)
    fortschritt, fazit = capsys.readouterr().out.strip().splitlines()
    assert progress.lies(fortschritt) == {"done": 5, "total": 10}
    assert progress.lies_ergebnis(fortschritt) is None
    assert progress.lies_ergebnis(fazit) == {"new": 7}
    assert progress.lies(fazit) is None


# --------------------------------------------------------------------------
# The scripts really do report
# --------------------------------------------------------------------------
@pytest.mark.parametrize("modul", ["teams_export", "outlook_export",
                                   "onedrive_export", "sharepoint_export",
                                   "rag_index", "combined_search"])
def test_export_meldet_sein_ergebnis(modul):
    """Without this report the app would keep indexing blindly after every
    run – and the run history would stay empty for the step."""
    from pathlib import Path
    wurzel = Path(__file__).resolve().parent.parent
    quelle = (wurzel / f"{modul}.py").read_text(encoding="utf-8")
    if "import drive_mirror" in quelle:
        # The mirror reports via the shared core – the contract holds there.
        quelle += (wurzel / "drive_mirror.py").read_text(encoding="utf-8")
    assert "progress.ergebnis(" in quelle, f"{modul} meldet sein Ergebnis nicht"



@pytest.mark.parametrize("modul,stelle", [
    ("teams_export", "chats"),          # knows the total
    ("outlook_export", "mails"),        # does not know it
    ("rag_index", "embeddings"),
    ("combined_search", "mails"),
])
def test_skript_meldet_fortschritt(modul, stelle):
    """Otherwise the bar would stall on a step without anyone noticing."""
    from pathlib import Path
    quelle = (Path(__file__).resolve().parent.parent / f"{modul}.py").read_text(
        encoding="utf-8")
    assert "import progress" in quelle, f"{modul} bindet progress nicht ein"
    assert 'progress.melde(' in quelle, f"{modul} meldet nichts"
    assert f'"{stelle}"' in quelle, f"{modul} meldet nicht als {stelle}"


# --------------------------------------------------------------------------
# Error event: structured instead of prose patterns
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
