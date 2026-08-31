#!/usr/bin/env python3
"""
OneDrive-Export: das eigene Laufwerk als lokaler Spiegel.

Was das heißt, sei hier festgehalten, weil man es beim Lesen des Codes sonst
falsch erwartet:

  Gehalten wird die JEWEILS AKTUELLE Fassung jeder Datei. Ändert sie sich,
  wird sie überschrieben – frühere Fassungen bewahrt dieser Spiegel nicht.

  Wird eine Datei in OneDrive gelöscht, BLEIBT sie hier liegen und bekommt
  einen Vermerk in verschwunden.tsv. Dieselbe Zusage wie beim Postfach: ein
  Archiv, das nur wächst, beantwortet die wichtigste Frage nicht – was war
  hier einmal und ist jetzt weg?

Warum Delta und nicht Auflisten: /me/drive/root/delta liefert Änderungen UND
Löschungen mit einem Token, und beim nächsten Lauf nur noch das Neue.

The mirror machinery itself lives in drive_mirror.py – a SharePoint library
is the same kind of drive, so both exports share one core. This module only
supplies what is OneDrive: the ``/me/drive`` base, the scopes, and which
settings feed the Selection.

Runs as a subprogram of app.py: output folder as the only argument, settings
as environment variables (ONEDRIVE_RULES – include/exclude rules on paths,
one per line, like the mailbox; ONEDRIVE_MAX_MB – skip larger files, 0 = no
limit; MIRROR_WORKERS – parallel requests; environment beats
app_config.json, see settings.py). Special runs: --folders syncs the folder
tree, --check reports what is missing (vollstaendigkeit.json).

Resume: dateien.tsv und delta.txt im Ausgabeordner. Bricht ein Lauf ab, wird
    delta.txt NICHT fortgeschrieben – der nächste Lauf zählt noch einmal auf und
    überspringt anhand des cTag alles, was schon liegt. Ein abgebrochener Lauf
    darf keine Änderung verschlucken.
"""

import os
import sys
from pathlib import Path

import auth
import export_util
import folders
import progress  # noqa: F401 – Teil der gemeinsamen Skript-Schnittstelle
import settings

try:
    import msal  # noqa: F401
    import requests  # noqa: F401 – früh prüfen, gebraucht in graph_client
except ImportError:
    print("Fehlende Pakete. Bitte installieren:  pip install msal requests")
    raise SystemExit(1) from None

import drive_mirror
import graph_client
from drive_mirror import (  # noqa: F401 – re-exported for the tests' benefit
    BESTAND_DATEI, BERICHT_DATEI, DATEI_DIR, GONE_FILE,
    Bestand, Selection, geaendert_am, lies_delta, lies_verschwunden, plane,
    pruefe_vollstaendigkeit, rel_pfad, safe, schreibe_bericht,
    schreibe_delta, schreibe_verschwunden, verschiebe,
)

export_util.erzwinge_utf8()

GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Files.Read.All", RES + "User.Read"]

OUT_ROOT = settings.value("onedrive_dir", settings.ONEDRIVE_DIR)


workers = drive_mirror.workers


def max_bytes():
    """Obergrenze je Datei in Bytes; 0 heißt ohne Grenze.

    low=0 ist hier entscheidend: settings.number zieht sonst auf mindestens 1
    hoch. Das ist bei „Parallele Downloads" richtig und hier falsch – aus der
    ausgeschalteten Grenze wurde eine von einem Megabyte, und der Spiegel ließ
    still jede größere Datei liegen.
    """
    return max(0, settings.number("ONEDRIVE_MAX_MB", "onedrive_max_mb",
                                  low=0)) * 1024 * 1024


def aktuelle_regeln():
    """Include/Exclude auf Pfaden – dieselbe Mechanik wie beim Postfach.

    Ohne Regeln kommt alles mit: wer nichts einstellt, will sein Laufwerk, nicht
    Leere.
    """
    roh = os.environ.get("ONEDRIVE_RULES")
    if roh is None:
        roh = settings.value("onedrive_rules", None)
    return folders.lies_regeln(roh or "")


def auswahl():
    """What this mirror takes – OneDrive has rules and a size cap, no
    extension filters (those are a SharePoint setting)."""
    return Selection(rules=aktuelle_regeln(), max_bytes=max_bytes())


class Graph(drive_mirror.DriveOps, graph_client.Graph):
    """Angemeldeter Zugriff; die Anmeldung selbst steckt in auth.Login."""

    def __init__(self, nur_still=False):
        super().__init__(SCOPES, nur_still=nur_still)


class TokenClient(drive_mirror.DriveOps, graph_client.TokenClient):
    """Fertiger Bearer-Token aus dem Graph Explorer; 401 heißt TokenExpired."""


def lauf(graph, out):
    return drive_mirror.lauf(graph, out, auswahl(), workers())


def nur_pruefen(graph, out):
    return drive_mirror.nur_pruefen(graph, out, auswahl())


def nur_ordner(graph, out):
    return drive_mirror.nur_ordner(graph, out, auswahl())


_hilfe_gewuenscht = export_util.hilfe_gewuenscht


def main():
    argv = sys.argv[1:]
    if _hilfe_gewuenscht(argv):
        print(__doc__)
        return
    struktur = "--folders" in argv
    pruefen = "--check" in argv
    argv = [a for a in argv if not a.startswith("--")]
    out = Path(argv[0]) if argv else Path(OUT_ROOT)
    graph_client.konfiguriere(workers())
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    try:
        (nur_pruefen if pruefen else nur_ordner if struktur else lauf)(graph, out)
    except auth.TokenExpired:
        # Structured ending – the app reacts to the event and shows its wizard.
        progress.fehler("token_expired")
        sys.exit(1)


if __name__ == "__main__":
    main()
