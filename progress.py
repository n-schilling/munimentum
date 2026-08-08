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


def ergebnis(neu, **weitere):
    """Was der Schritt bewirkt hat – am Ende einmal, für den Aufrufer.

    `neu` ist die Zahl der tatsächlich geschriebenen Stücke. Ist sie null, hat
    sich am Bestand nichts geändert, und die App kann sich das Indizieren und
    den Kalenderaufbau sparen. Die Skripte sagen das längst („Neu exportiert:
    0“), nur eben in Prosa – die auszulesen bräche bei jeder Umformulierung.
    """
    if not aktiv():
        return
    daten = {"neu": int(neu)}
    daten.update({k: int(v) for k, v in weitere.items()})
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
    return _lies(zeile, MARKE_ERGEBNIS, "neu")
