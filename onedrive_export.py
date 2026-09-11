#!/usr/bin/env python3
"""
OneDrive export: the user's own drive as a local mirror.

What that means is written down here, because reading the code alone sets
the wrong expectation:

  What is kept is the CURRENT version of each file. When it changes, it is
  overwritten – this mirror does not preserve earlier versions.

  When a file is deleted in OneDrive, it STAYS here and gets a tombstone
  entry in the state.db. The same promise as for the mailbox: an archive
  that only grows fails to answer the most important question – what was
  here once and is now gone?

Why delta and not listing: /me/drive/root/delta delivers changes AND
deletions with one token, and on the next run only what is new.

The mirror machinery itself lives in drive_mirror.py – a SharePoint library
is the same kind of drive, so both exports share one core. This module only
supplies what is OneDrive: the ``/me/drive`` base, the scopes, and which
settings feed the Selection.

Runs as a subprogram of app.py: output folder as the only argument, settings
as environment variables (ONEDRIVE_RULES – include/exclude rules on paths,
one per line, like the mailbox; ONEDRIVE_MAX_MB – skip larger files, 0 = no
limit; MIRROR_WORKERS – parallel requests; SYNC_CADENCE key "onedrive" –
how often the mirror runs at most, SYNC_NOW steps over it once; environment
beats app_config.json, see settings.py). Special runs: --folders syncs the
folder tree, --check reports what is missing – both outside the cadence.

Folder cadences: SYNC_CADENCE may also carry "onedrive:<folder path>" keys
(the path as the folder tree stores it, "Dateien/Fotos"); a value applies
to the folder and everything below until a deeper folder sets its own.
The drive has one delta stream, so such a cadence gates downloads, not the
listing: the run lists whenever any unit is due, files of a unit not due
wait in the state.db (records "wartend") and come when their cadence is
round – processed before the delta, so nothing is forgotten although the
delta token moved on. Each unit has its own stamp.

A changed selection (rules, size cap) makes the next run read the drive
once in full, so files the wider selection now takes arrive although they
did not change (drive_mirror keeps the kennzeichen in the state.db).

Resume: the output folder's state.db (inventory, delta pointer, walk
    staging). An aborted run does NOT advance the delta pointer – the next
    run picks the stored walk back up and skips, by cTag, everything that
    already lies here. An aborted run must never swallow a change.
"""

import os
import sys

import auth
import export_util
import folders
import progress
import settings

try:
    import msal  # noqa: F401
    import requests  # noqa: F401 – check early, needed in graph_client
except ImportError:
    print("Fehlende Pakete. Bitte installieren:  pip install msal requests")
    raise SystemExit(1) from None

import drive_mirror
import graph_client
import state_db
from drive_mirror import (  # noqa: F401 – re-exported for the tests' benefit
    DATEI_DIR, Bestand, Selection, geaendert_am, plane,
    pruefe_vollstaendigkeit, rel_pfad, safe, verschiebe,
)

export_util.erzwinge_utf8()

GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Files.Read.All", RES + "User.Read"]

KADENZ_KEY = "onedrive"          # SYNC_CADENCE key of the whole mirror


workers = drive_mirror.workers


def max_bytes():
    """Per-file upper limit in bytes; 0 means no limit.

    low=0 is crucial here: settings.number otherwise raises the value to at
    least 1. That is right for "parallel downloads" and wrong here – the
    disabled limit turned into one of one megabyte, and the mirror silently
    left every larger file behind.
    """
    return max(0, settings.number("ONEDRIVE_MAX_MB", "onedrive_max_mb",
                                  low=0)) * 1024 * 1024


def aktuelle_regeln():
    """Include/exclude on paths – the same mechanics as for the mailbox.

    Without rules everything comes along: whoever configures nothing wants
    their drive, not emptiness.
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
    """Signed-in access; the sign-in itself lives in auth.Login."""

    def __init__(self, nur_still=False):
        super().__init__(SCOPES, nur_still=nur_still)


class TokenClient(drive_mirror.DriveOps, graph_client.TokenClient):
    """Ready-made bearer token from the Graph Explorer; 401 means TokenExpired."""


def einheiten(out):
    """The drive and its folder cadences as units over the drive's state.db."""
    return drive_mirror.Einheiten(export_util.kadenzen(), KADENZ_KEY,
                                  state_db.StateDb(out))


def lauf(graph, out):
    """The mirror run behind its cadence gate.

    The drive's own state.db remembers the last clean run per unit – the
    drive and every folder with a cadence of its own. A run in which no
    unit is due says so with one line and ends with an empty result;
    otherwise the delta is listed and each unit's files are downloaded or
    held back by its own cadence (drive_mirror.Einheiten). SYNC_NOW (the
    "sync now" button) makes everything due. Returns the mirror's counts,
    None when skipped."""
    takt = einheiten(out)
    name = progress.atom("settings.onedrive.title")
    if not takt.irgendeine_faellig():
        progress.event("run.cadence.skip", name=name,
                       cadence=progress.atom(f"cadence.{takt.kadenz('')}"))
        progress.ergebnis(0, extra={"skipped": 1})
        return None
    zahlen = drive_mirror.lauf(graph, out, auswahl(), workers(), still=True,
                               name=name, einheiten=takt)
    if takt.ordner:
        # One line for the folders held back, never one per file – and the
        # count of what waits for them goes into the result.
        n = len(takt.zurueckgehalten())
        if n:
            progress.event("run.onedrive.folders_paced", n=n)
        drive_mirror.ergebnis_melden(zahlen, waiting=zahlen["waiting"])
    else:
        drive_mirror.ergebnis_melden(zahlen)
    return zahlen


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
    out = export_util.ausgabeordner(argv)
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
