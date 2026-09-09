"""Project-wide promises no single module can check on its own.

So far exactly one: every runnable script must switch its output to UTF-8.
Windows consoles otherwise use a legacy codepage (cp1252), and a single
"→" or "✓" in a progress line ends the run with UnicodeEncodeError.
Exactly that happened to packaging/smoke_test.py – as the only script it
lacked the switch, and the smoke test died on Windows before it had
checked anything.
"""

import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent

# The switch every script in the project carries at the top.
UMSTELLUNG = re.compile(
    r"for _stream in \(sys\.stdout, sys\.stderr\):\s*\n"
    r"\s*try:\s*\n"
    r'\s*_stream\.reconfigure\(encoding="utf-8", errors="replace"\)')


def startbare_skripte():
    """Everything with a __main__ – so it runs by hand or as a subprocess."""
    dateien = sorted(WURZEL.glob("*.py")) + sorted((WURZEL / "packaging").glob("*.py"))
    return [p for p in dateien
            if '__main__' in p.read_text(encoding="utf-8")]


def test_es_gibt_startbare_skripte():
    """Safeguard against a search error that silently empties the list."""
    namen = {p.name for p in startbare_skripte()}
    assert {"app.py", "teams_export.py", "outlook_export.py", "rag_index.py",
            "smoke_test.py"} <= namen


@pytest.mark.parametrize("pfad", startbare_skripte(),
                         ids=lambda p: p.name)
def test_startbares_skript_stellt_auf_utf8(pfad):
    """Either its own block (smoke_test.py runs without project modules) or
    the shared helper export_util.erzwinge_utf8()."""
    quelle = pfad.read_text(encoding="utf-8")
    assert UMSTELLUNG.search(quelle) or "erzwinge_utf8()" in quelle, (
        f"{pfad.name} stellt seine Ausgabe nicht auf UTF-8 – auf Windows "
        f"beendet das erste Sonderzeichen den Lauf.")


# Libraries allowed to print despite lacking a __main__ – with a reason.
# A library must NOT switch sys.stdout globally; that would affect everyone
# who imports it. Whoever is listed here must secure their own output
# instead.
DUERFEN_AUSGEBEN = {
    # A single line, pure ASCII (json.dumps escapes everything else), and
    # the print sits in a try/except – UnicodeEncodeError is a ValueError,
    # so a failed progress report never holds up a run.
    "progress.py",
    # Reports wait times on throttling/network errors via _meld(), which
    # swallows UnicodeEncodeError – the report is expendable, the run is not.
    "graph_client.py",
}


def test_bibliotheken_geben_nichts_unabgesichertes_aus():
    """Modules without a __main__ do not need the switch – as long as they
    print nothing either. If they did, the same trap would apply to them."""
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
# App icon
#
# With icon=None in the spec, the Dock shows PyInstaller's default icon
# (Python logo on a floppy disk). Nobody notices, because nothing fails:
# a bundle without an icon builds without complaint.
# --------------------------------------------------------------------------
ICON = WURZEL / "packaging" / "icon"


def test_symbol_ist_in_der_spec_verdrahtet():
    text = (WURZEL / "packaging" / "app.spec").read_text(encoding="utf-8")
    assert "icon=str(ICON_ICO)" in text, "Windows-Symbol nicht gesetzt"
    assert "icon=str(ICON_ICNS)" in text, "macOS-Symbol nicht gesetzt"
    assert "icon=None" not in text


def test_symbol_hat_eine_quelle():
    """Without the SVG the icon could no longer be rebuilt."""
    assert (ICON / "icon.svg").read_text(encoding="utf-8").lstrip().startswith("<svg")


def test_icns_ist_eine_echte_icns():
    roh = (ICON / "icon.icns").read_bytes()
    assert roh[:4] == b"icns", "keine gültige .icns-Datei"
    # The length field in the header must match the file – a truncated
    # file builds fine and then shows nothing in the Finder.
    assert int.from_bytes(roh[4:8], "big") == len(roh)


def test_ico_enthaelt_die_kleinen_groessen():
    """16 and 32 px are the ones actually seen: taskbar and title bar.
    An .ico with only 256 px makes Windows downscale it ugly."""
    roh = (ICON / "icon.ico").read_bytes()
    reserviert, typ, anzahl = (int.from_bytes(roh[i:i + 2], "little")
                               for i in (0, 2, 4))
    assert (reserviert, typ) == (0, 1), "keine gültige .ico-Datei"
    # A 0 in the width byte means 256 according to the format.
    groessen = {roh[6 + i * 16] or 256 for i in range(anzahl)}
    assert {16, 32}<= groessen, f"kleine Größen fehlen: {sorted(groessen)}"


def test_ausnahmen_sichern_ihre_ausgabe_wirklich_ab():
    """Whoever is on the list must actually catch their print."""
    for name in DUERFEN_AUSGEBEN:
        quelle = (WURZEL / name).read_text(encoding="utf-8")
        assert "try:" in quelle and "except" in quelle, \
            f"{name} steht auf der Ausnahmeliste, fängt aber nichts ab"


# --------------------------------------------------------------------------
# --help must not create anything
#
# From the field: the repo held a folder named "--help" with an empty
# exported.tsv inside – even checked in. outlook_export.py reads the first
# free argument as the output folder, so `--help` dutifully created one
# and started exporting.
# --------------------------------------------------------------------------
EIGENE_ARGUMENTE = ["outlook_export.py", "teams_export.py", "combined_search.py"]


@pytest.mark.parametrize("name", EIGENE_ARGUMENTE)
def test_hilfe_legt_nichts_an(name, tmp_path):
    """Without an argument parser the check must come by hand – otherwise
    the switch becomes a folder name."""
    import subprocess
    import sys
    # stdin closed: if the check is missing, the script lands in its
    # interactive selection. With stdin open the test would hang there
    # instead of failing – exactly that happened once while cross-checking.
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
    helfer = (WURZEL / "export_util.py").read_text(encoding="utf-8")
    for form in ('"-h"', '"--help"'):
        assert form in helfer, f"export_util kennt {form} nicht"


def test_kein_ordner_aus_einem_schalter():
    """If the folder ever came back, someone would have created it again."""
    for name in ("--help", "-h", "--default"):
        assert not (WURZEL / name).exists(), (
            f"Ordner „{name}“ im Projekt – ein Schalter wurde als Ausgabeordner "
            f"gedeutet.")


def test_spec_listet_die_geteilten_module():
    """They end up in the bundle via the import graph anyway – but this
    very list is the safety net, and I once lost it to a `git checkout`
    during a cross-check."""
    text = (WURZEL / "packaging" / "app.spec").read_text(encoding="utf-8")
    for modul in ("auth", "export_util", "folders", "graph_client",
                  "ollama_client", "run_history", "settings", "progress",
                  "answer", "corpus", "store_layout"):
        assert f'"{modul}"' in text, f"{modul} fehlt in TEILPROGRAMME"


# --------------------------------------------------------------------------
# The process pool in the bundle
#
# From the field: indexing ended in BrokenProcessPool as soon as a source
# had enough files for corpus._pmap to open the pool at all (threshold
# 200). The files were not the cause: outside Linux, Python starts a
# worker process by calling itself again – bundled, that is the app
# binary, with "--multiprocessing-fork pipe_handle=…" instead of its own
# arguments. Without multiprocessing.freeze_support() the child ran into
# app.main()'s argument parser, exited with code 2 ("unrecognized
# arguments"), and the parent process only saw a crashed worker.
#
# So the thing to check is not whether the line is there, but where the
# call ends up: in the worker branch or in the argument parser.
# --------------------------------------------------------------------------
def test_app_beantwortet_den_aufruf_als_arbeitsprozess():
    import subprocess
    import sys

    app = WURZEL / "app.py"
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys, runpy\n"
         # sys.frozen is the only thing multiprocessing uses to tell that
         # it must call the executable itself instead of python -c.
         "sys.frozen = True\n"
         f"sys.argv = [{str(app)!r}, '--multiprocessing-fork', 'pipe_handle=999999']\n"
         f"runpy.run_path({str(app)!r}, run_name='__main__')\n"],
        capture_output=True, text=True, timeout=120, cwd=WURZEL)
    ausgabe = r.stdout + r.stderr

    assert "unrecognized arguments" not in ausgabe, (
        "app.py hält den Aufruf eines Arbeitsprozesses für Benutzereingabe und "
        "gibt ihn an argparse weiter – genau so entsteht BrokenProcessPool.\n"
        f"{ausgabe[-800:]}")
    # It arrived in multiprocessing instead. That it fails there on a
    # made-up file handle is the proof, not the failure: without a real
    # parent process this is as far as it gets.
    assert "multiprocessing" in ausgabe, (
        f"Der Aufruf landete weder bei argparse noch bei multiprocessing:\n"
        f"{ausgabe[-800:]}")
