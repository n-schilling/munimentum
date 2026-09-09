#!/usr/bin/env python3
"""
progress.py – report progress machine-readably, for the bar in the app.

The scripts write their progress for humans ("✓ [37/1200] neu · …").
Building a bar from that would mean reading these sentences with patterns –
and every rewording would break it. Instead they additionally emit a line
that contains only the numbers:

    @@PROGRESS@@ {"done": 37, "total": 1200, "what": "chats"}

The markers are always sent – the app is the only caller and filters them
out of the log.

"total" may be missing. The Outlook export discovers its mails only while
running – it knows no grand total, and inventing a percentage would be
worse than none. The app then shows the number instead of a filled bar.
"""

import json

MARKE = "@@PROGRESS@@"
MARKE_ERGEBNIS = "@@RESULT@@"
MARKE_FEHLER = "@@ERROR@@"
MARKE_LOG = "@@LOG@@"


def melde(done, total=None, what=None):
    """Emit one progress line."""
    daten = {"done": int(done)}
    if total is not None:
        daten["total"] = int(total)
    if what:
        daten["what"] = str(what)
    try:
        print(f"{MARKE} {json.dumps(daten)}", flush=True)
    except (OSError, ValueError):
        pass                       # a report must never hold up a run


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
    """Counterpart for the app: numbers from the line, otherwise None.

    None means "this is an ordinary output line" – the caller then writes it
    to the log instead of reading it as progress.
    """
    return _lies(zeile, MARKE, "done")


def lies_ergebnis(zeile):
    """Counterpart to ergebnis(). Same promise: None means "ordinary line"."""
    return _lies(zeile, MARKE_ERGEBNIS, "new")


def lies_fehler(zeile):
    """Counterpart to fehler(). Same promise: None means "ordinary line"."""
    return _lies(zeile, MARKE_FEHLER, "error")


def lies_event(zeile):
    """Counterpart to event(). Same promise: None means "ordinary line"."""
    return _lies(zeile, MARKE_LOG, "k")
