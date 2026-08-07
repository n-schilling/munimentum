"""Projektweite Zusagen, die kein einzelnes Modul für sich prüfen kann.

Bisher genau eine: jedes startbare Skript muss seine Ausgabe auf UTF-8 stellen.
Windows-Konsolen nutzen sonst eine Legacy-Codepage (cp1252), und ein einzelnes
„→“ oder „✓“ in einer Fortschrittszeile beendet den Lauf mit UnicodeEncodeError.
Genau das ist packaging/smoke_test.py passiert – als einzigem Skript fehlte die
Umstellung, und der Rauchtest starb auf Windows, bevor er irgendetwas geprüft
hatte.
"""

import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent

# Die Umstellung, die alle Skripte des Projekts oben tragen.
UMSTELLUNG = re.compile(
    r"for _stream in \(sys\.stdout, sys\.stderr\):\s*\n"
    r"\s*try:\s*\n"
    r'\s*_stream\.reconfigure\(encoding="utf-8", errors="replace"\)')


def startbare_skripte():
    """Alles, was ein __main__ hat – also von Hand oder als Unterprozess läuft."""
    dateien = sorted(WURZEL.glob("*.py")) + sorted((WURZEL / "packaging").glob("*.py"))
    return [p for p in dateien
            if '__main__' in p.read_text(encoding="utf-8")]


def test_es_gibt_startbare_skripte():
    """Sicherung gegen einen Suchfehler, der die Liste unbemerkt leert."""
    namen = {p.name for p in startbare_skripte()}
    assert {"app.py", "teams_export.py", "outlook_export.py", "rag_index.py",
            "smoke_test.py"} <= namen


@pytest.mark.parametrize("pfad", startbare_skripte(),
                         ids=lambda p: p.name)
def test_startbares_skript_stellt_auf_utf8(pfad):
    quelle = pfad.read_text(encoding="utf-8")
    assert UMSTELLUNG.search(quelle), (
        f"{pfad.name} stellt seine Ausgabe nicht auf UTF-8 – auf Windows "
        f"beendet das erste Sonderzeichen den Lauf.")


def test_bibliotheken_geben_nichts_aus():
    """Module ohne __main__ brauchen die Umstellung nicht – solange sie auch
    nichts ausgeben. Täten sie es, gälte für sie dieselbe Falle."""
    startbar = {p.name for p in startbare_skripte()}
    for p in sorted(WURZEL.glob("*.py")):
        if p.name in startbar:
            continue
        quelle = p.read_text(encoding="utf-8")
        assert "print(" not in quelle, (
            f"{p.name} gibt etwas aus, hat aber kein __main__ – entweder die "
            f"UTF-8-Umstellung ergänzen oder die Ausgabe dem Aufrufer überlassen.")
