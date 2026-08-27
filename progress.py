#!/usr/bin/env python3
"""
progress.py – Fortschritt maschinenlesbar melden, für den Balken in der App.

Die Skripte schreiben ihren Fortschritt für Menschen ("✓ [37/1200] neu · …").
Daraus einen Balken zu bauen hieße, diese Sätze mit Mustern auszulesen – und
jede Umformulierung bräche ihn. Stattdessen melden sie zusätzlich eine Zeile,
die nur die Zahlen enthält:

    @@PROGRESS@@ {"done": 37, "total": 1200, "what": "chats"}

Nur wenn EXPORT_PROGRESS gesetzt ist. Beim Aufruf von Hand im Terminal bleibt
die Ausgabe damit unverändert; die App setzt die Variable und filtert die
Zeilen aus dem Protokoll wieder heraus.

"total" darf fehlen. Der Outlook-Export entdeckt seine Mails erst beim Laufen –
er kennt keine Gesamtzahl, und einen Prozentwert zu erfinden wäre schlechter
als keiner. Die App zeigt dann die Zahl statt eines gefüllten Balkens.
"""

import json
import os

MARKE = "@@PROGRESS@@"
MARKE_ERGEBNIS = "@@RESULT@@"


def aktiv():
    """Meldet die App gerade zu? Sonst bleibt die Ausgabe wie gewohnt."""
    return os.environ.get("EXPORT_PROGRESS", "").strip().lower() not in (
        "", "0", "false", "no", "nein", "off")


def melde(done, total=None, what=None):
    """Eine Fortschrittszeile ausgeben – tut nichts, wenn niemand zuhört."""
    if not aktiv():
        return
    daten = {"done": int(done)}
    if total is not None:
        daten["total"] = int(total)
    if what:
        daten["what"] = str(what)
    try:
        print(f"{MARKE} {json.dumps(daten)}", flush=True)
    except (OSError, ValueError):
        pass                       # eine Meldung darf nie einen Lauf aufhalten


def ergebnis(new, unchanged=None, excluded=None, errors=None, extra=None):
    """What the step achieved – emitted once at the end, for the caller.

    One schema for every subprogram, so the app collects the same data
    everywhere (run history, skip logic):

        new        pieces actually written this run
        unchanged  already present and left untouched
        excluded   deliberately left out (rules, size limits)
        errors     pieces that failed
        extra      dict with step-specific counts (e.g. moved, healed)

    `new` == 0 means the corpus did not change, and the app can skip
    indexing and the calendar rebuild.
    """
    if not aktiv():
        return
    daten = {"new": int(new)}
    for key, wert in (("unchanged", unchanged), ("excluded", excluded),
                      ("errors", errors)):
        if wert is not None:
            daten[key] = int(wert)
    if extra:
        daten["extra"] = {k: int(v) for k, v in extra.items()}
    try:
        print(f"{MARKE_ERGEBNIS} {json.dumps(daten)}", flush=True)
    except (OSError, ValueError):
        pass


def _lies(zeile, marke, pflicht):
    text = (zeile or "").strip()
    if not text.startswith(marke):
        return None
    try:
        daten = json.loads(text[len(marke):])
    except ValueError:
        return None
    if not isinstance(daten, dict) or pflicht not in daten:
        return None
    return daten


def lies(zeile):
    """Gegenstück für die App: Zahlen aus der Zeile, sonst None.

    None heißt „das ist eine gewöhnliche Ausgabezeile“ – der Aufrufer schreibt
    sie dann ins Protokoll, statt sie als Fortschritt zu deuten.
    """
    return _lies(zeile, MARKE, "done")


def lies_ergebnis(zeile):
    """Gegenstück zu ergebnis(). Gleiche Zusage: None heißt „gewöhnliche Zeile“."""
    return _lies(zeile, MARKE_ERGEBNIS, "new")
