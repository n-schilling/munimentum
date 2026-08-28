#!/usr/bin/env python3
"""
progress.py – Fortschritt maschinenlesbar melden, für den Balken in der App.

Die Skripte schreiben ihren Fortschritt für Menschen ("✓ [37/1200] neu · …").
Daraus einen Balken zu bauen hieße, diese Sätze mit Mustern auszulesen – und
jede Umformulierung bräche ihn. Stattdessen melden sie zusätzlich eine Zeile,
die nur die Zahlen enthält:

    @@PROGRESS@@ {"done": 37, "total": 1200, "what": "chats"}

Die Marker werden immer gesendet – die App ist der einzige Aufrufer und
filtert sie aus dem Protokoll heraus. Bis 5.4 hing das an EXPORT_PROGRESS,
einer Weiche, die nur für Handaufrufe existierte.

"total" darf fehlen. Der Outlook-Export entdeckt seine Mails erst beim Laufen –
er kennt keine Gesamtzahl, und einen Prozentwert zu erfinden wäre schlechter
als keiner. Die App zeigt dann die Zahl statt eines gefüllten Balkens.
"""

import json

MARKE = "@@PROGRESS@@"
MARKE_ERGEBNIS = "@@RESULT@@"
MARKE_FEHLER = "@@ERROR@@"
MARKE_LOG = "@@LOG@@"


def melde(done, total=None, what=None):
    """Eine Fortschrittszeile ausgeben."""
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


def event(key, level="info", **vars):
    """A translatable log line: a text key plus its variables.

    The app resolves the key in the interface language (a variable may itself
    be a nested {"k": …, "v": …} message). This is what replaced the scripts'
    German prose – one vocabulary for every subprogram.
    """
    daten = {"k": str(key), "level": level}
    if vars:
        daten["v"] = vars
    try:
        print(f"{MARKE_LOG} {json.dumps(daten, ensure_ascii=False)}", flush=True)
    except (OSError, ValueError):
        pass


def atom(key):
    """A nested message with no variables – e.g. a unit or category name."""
    return {"k": str(key), "v": {}}


def fehler(art):
    """A structured failure event, e.g. "token_expired".

    The app acts on this instead of pattern-matching the human log text –
    the prose message stays for the log, this line carries the meaning.
    """
    try:
        print(f"{MARKE_FEHLER} {json.dumps({'error': str(art)})}", flush=True)
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


def lies_fehler(zeile):
    """Gegenstück zu fehler(). Gleiche Zusage: None heißt „gewöhnliche Zeile“."""
    return _lies(zeile, MARKE_FEHLER, "error")


def lies_event(zeile):
    """Gegenstück zu event(). Gleiche Zusage: None heißt „gewöhnliche Zeile“."""
    return _lies(zeile, MARKE_LOG, "k")
