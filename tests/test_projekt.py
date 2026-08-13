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


# Bibliotheken, die trotz fehlendem __main__ ausgeben dürfen – mit Grund.
# Eine Bibliothek soll sys.stdout NICHT global umstellen; das ginge alle an, die
# sie importieren. Wer hier steht, muss seine Ausgabe stattdessen selbst
# absichern.
DUERFEN_AUSGEBEN = {
    # Eine einzige Zeile, rein ASCII (json.dumps escapt alles andere), und der
    # print steht in einem try/except – UnicodeEncodeError ist ein ValueError,
    # eine misslungene Fortschrittsmeldung hält also nie einen Lauf auf.
    "progress.py",
    # Meldet Wartezeiten bei Drosselung/Netzfehlern über _meld(), das
    # UnicodeEncodeError schluckt – die Meldung ist verzichtbar, der Lauf nicht.
    "graph_client.py",
}


def test_bibliotheken_geben_nichts_unabgesichertes_aus():
    """Module ohne __main__ brauchen die Umstellung nicht – solange sie auch
    nichts ausgeben. Täten sie es, gälte für sie dieselbe Falle."""
    startbar = {p.name for p in startbare_skripte()}
    for p in sorted(WURZEL.glob("*.py")):
        if p.name in startbar or p.name in DUERFEN_AUSGEBEN:
            continue
        quelle = p.read_text(encoding="utf-8")
        assert "print(" not in quelle, (
            f"{p.name} gibt etwas aus, hat aber kein __main__ – entweder die "
            f"UTF-8-Umstellung ergänzen, die Ausgabe absichern (siehe "
            f"DUERFEN_AUSGEBEN) oder sie dem Aufrufer überlassen.")


# --------------------------------------------------------------------------
# App-Symbol
#
# Bis 1.1.0 stand icon=None in der Spec – im Dock erschien PyInstallers
# Standardsymbol (Python-Logo auf einer Diskette). Das fiel niemandem auf, weil
# nichts fehlschlug: ein Bündel ohne Symbol baut anstandslos.
# --------------------------------------------------------------------------
ICON = WURZEL / "packaging" / "icon"


def test_symbol_ist_in_der_spec_verdrahtet():
    text = (WURZEL / "packaging" / "app.spec").read_text(encoding="utf-8")
    assert "icon=str(ICON_ICO)" in text, "Windows-Symbol nicht gesetzt"
    assert "icon=str(ICON_ICNS)" in text, "macOS-Symbol nicht gesetzt"
    assert "icon=None" not in text


def test_symbol_hat_eine_quelle():
    """Ohne die SVG ließe sich das Symbol nicht mehr nachbauen."""
    assert (ICON / "icon.svg").read_text(encoding="utf-8").lstrip().startswith("<svg")


def test_icns_ist_eine_echte_icns():
    roh = (ICON / "icon.icns").read_bytes()
    assert roh[:4] == b"icns", "keine gültige .icns-Datei"
    # Die Längenangabe im Kopf muss zur Datei passen – eine abgeschnittene
    # Datei baut durch und zeigt dann im Finder nichts.
    assert int.from_bytes(roh[4:8], "big") == len(roh)


def test_ico_enthaelt_die_kleinen_groessen():
    """16 und 32 px sind die, die man wirklich sieht: Taskleiste und Titelzeile.
    Ein .ico nur mit 256 px lässt Windows hässlich herunterrechnen."""
    roh = (ICON / "icon.ico").read_bytes()
    reserviert, typ, anzahl = (int.from_bytes(roh[i:i + 2], "little")
                               for i in (0, 2, 4))
    assert (reserviert, typ) == (0, 1), "keine gültige .ico-Datei"
    # 0 im Breitenbyte heißt laut Format 256.
    groessen = {roh[6 + i * 16] or 256 for i in range(anzahl)}
    assert {16, 32}<= groessen, f"kleine Größen fehlen: {sorted(groessen)}"


def test_ausnahmen_sichern_ihre_ausgabe_wirklich_ab():
    """Wer auf der Liste steht, muss seinen print auch tatsächlich fangen."""
    for name in DUERFEN_AUSGEBEN:
        quelle = (WURZEL / name).read_text(encoding="utf-8")
        assert "try:" in quelle and "except" in quelle, \
            f"{name} steht auf der Ausnahmeliste, fängt aber nichts ab"


# --------------------------------------------------------------------------
# --help darf nichts anlegen
#
# Aus der Praxis: im Repo lag ein Ordner namens „--help“ mit einer leeren
# exported.tsv darin – und war sogar eingecheckt. outlook_export.py deutet das
# erste freie Argument als Ausgabeordner, also legte `--help` brav einen an und
# begann zu exportieren.
# --------------------------------------------------------------------------
EIGENE_ARGUMENTE = ["outlook_export.py", "teams_export.py", "combined_search.py"]


@pytest.mark.parametrize("name", EIGENE_ARGUMENTE)
def test_hilfe_legt_nichts_an(name, tmp_path):
    """Ohne Argumentparser muss die Abfrage von Hand kommen – sonst wird der
    Schalter zum Ordnernamen."""
    import subprocess
    import sys
    # stdin zu: fehlt die Abfrage, landet das Skript in seiner interaktiven
    # Auswahl. Mit offenem stdin bliebe der Test dort hängen statt zu scheitern –
    # genau das ist beim Gegenprüfen einmal passiert.
    r = subprocess.run([sys.executable, str(WURZEL / name), "--help"],
                       capture_output=True, text=True, cwd=tmp_path,
                       stdin=subprocess.DEVNULL, timeout=20)
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.strip(), "keine Hilfe ausgegeben"
    angelegt = sorted(p.name for p in tmp_path.iterdir())
    assert angelegt == [], f"{name} --help legte an: {angelegt}"


@pytest.mark.parametrize("name", EIGENE_ARGUMENTE)
def test_hilfe_kennt_die_ueblichen_schreibweisen(name):
    quelle = (WURZEL / name).read_text(encoding="utf-8")
    assert "_hilfe_gewuenscht" in quelle
    for form in ('"-h"', '"--help"'):
        assert form in quelle, f"{name} kennt {form} nicht"


def test_kein_ordner_aus_einem_schalter():
    """Wäre der Ordner je wieder da, hätte ihn jemand erneut erzeugt."""
    for name in ("--help", "-h", "--default"):
        assert not (WURZEL / name).exists(), (
            f"Ordner „{name}“ im Projekt – ein Schalter wurde als Ausgabeordner "
            f"gedeutet.")


def test_spec_listet_die_geteilten_module():
    """Sie stecken zwar ohnehin über den Importgraphen im Bündel – aber genau
    diese Liste ist das Sicherheitsnetz, und sie ist mir einmal durch ein
    `git checkout` bei einer Gegenprobe abhandengekommen."""
    text = (WURZEL / "packaging" / "app.spec").read_text(encoding="utf-8")
    for modul in ("auth", "folders", "graph_client", "settings", "progress",
                  "answer", "corpus", "store_layout"):
        assert f'"{modul}"' in text, f"{modul} fehlt in TEILPROGRAMME"


# --------------------------------------------------------------------------
# Der Prozess-Pool im Bündel
#
# Aus der Praxis: das Indizieren endete mit BrokenProcessPool, sobald eine
# Quelle genug Dateien hatte, dass corpus._pmap den Pool überhaupt aufmachte
# (Schwelle 200). Der Grund lag nicht bei den Dateien: außerhalb von Linux
# startet Python einen Arbeitsprozess, indem es
# sich selbst noch einmal aufruft – gebündelt also die App-Datei, mit
# "--multiprocessing-fork pipe_handle=…" statt eigener Argumente. Ohne
# multiprocessing.freeze_support() lief das Kind in den Argumentparser von
# app.main(), beendete sich mit Code 2 ("unrecognized arguments"), und der
# Elternprozess sah nur noch einen abgestürzten Arbeiter.
#
# Zu prüfen ist deshalb nicht, ob die Zeile dasteht, sondern wohin der Aufruf
# läuft: in den Arbeiterzweig oder in den Argumentparser.
# --------------------------------------------------------------------------
def test_app_beantwortet_den_aufruf_als_arbeitsprozess():
    import subprocess
    import sys

    app = WURZEL / "app.py"
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys, runpy\n"
         # sys.frozen ist das Einzige, woran multiprocessing erkennt, dass es
         # die ausführbare Datei selbst aufrufen muss statt python -c.
         "sys.frozen = True\n"
         f"sys.argv = [{str(app)!r}, '--multiprocessing-fork', 'pipe_handle=999999']\n"
         f"runpy.run_path({str(app)!r}, run_name='__main__')\n"],
        capture_output=True, text=True, timeout=120, cwd=WURZEL)
    ausgabe = r.stdout + r.stderr

    assert "unrecognized arguments" not in ausgabe, (
        "app.py hält den Aufruf eines Arbeitsprozesses für Benutzereingabe und "
        "gibt ihn an argparse weiter – genau so entsteht BrokenProcessPool.\n"
        f"{ausgabe[-800:]}")
    # Angekommen ist er stattdessen in multiprocessing. Dass er dort an einer
    # ausgedachten Dateinummer scheitert, ist der Beweis und nicht der Fehler:
    # weiter kommt man ohne echten Elternprozess nicht.
    assert "multiprocessing" in ausgabe, (
        f"Der Aufruf landete weder bei argparse noch bei multiprocessing:\n"
        f"{ausgabe[-800:]}")
