#!/usr/bin/env python3
"""
export_util.py – gemeinsame Helfer der Exportskripte.

Bis 5.3 trug jedes Skript eigene Kopien: Dateinamen entschärfen, Graph-Zeiten
parsen, Vermerke atomar schreiben, die Frage „sitzt hier jemand vor einem
Terminal?". Die Kopien wichen in Kleinigkeiten voneinander ab, ohne dass eine
Abweichung je gewollt war – hier steht jede Antwort einmal.

Nur Standardbibliothek.
"""

import os
import re
import sys
import json
import hashlib
from datetime import datetime
from pathlib import Path

# Dateinamen, die Outlook- und OneDrive-Export gleich benutzen: was aus der
# Quelle verschwunden ist, und der Bericht der Vollständigkeitsprüfung.
GONE_FILE = "verschwunden.tsv"
BERICHT_DATEI = "vollstaendigkeit.json"


def erzwinge_utf8():
    """stdout/stderr auf UTF-8 stellen (auf macOS/Linux ein No-op).

    Windows-Konsolen nutzen sonst eine Legacy-Codepage (z. B. cp1252), und bei
    Umleitung in eine Datei die Locale-Kodierung. Beides lässt jede Ausgabe an
    Zeichen wie →, ✓ oder Emoji mit UnicodeEncodeError scheitern und bricht
    den Lauf ab.
    """
    for strom in (sys.stdout, sys.stderr):
        try:
            strom.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def hilfe_gewuenscht(argv):
    """-h/--help beantworten, statt einen Ordner dieses Namens anzulegen.

    Die Exportskripte deuten das erste freie Argument als Ausgabeordner. Ohne
    diese Abfrage legte `python3 outlook_export.py --help` brav einen Ordner
    namens „--help" an und begann zu exportieren – einmal passiert und dann
    sogar eingecheckt.
    """
    return any(a in ("-h", "--help", "-help", "help") for a in argv)


# ---------------------------------------------------------------------------
# Interaktive Abfragen
# ---------------------------------------------------------------------------
def lies_eingabe(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def ist_interaktiv():
    """Ob überhaupt jemand antworten kann.

    stdin allein reicht als Nachweis nicht: unter Windows meldet auch das
    Nullgerät isatty() == True, weil NUL ein Zeichengerät ist. Ein Aufruf aus
    der App (stdin auf DEVNULL, stdout in einer Pipe) sähe damit interaktiv aus
    und bliebe an einer Frage stehen, die niemand sieht. Ein echtes Terminal
    hat beide Enden.
    """
    for strom in (sys.stdin, sys.stdout):
        try:
            if not strom.isatty():
                return False
        except (AttributeError, ValueError):
            return False
    return True


def parse_indices(raw, n):
    out = []
    for tok in re.split(r"[\s,]+", raw.strip()):
        if tok.isdigit():
            v = int(tok)
            if 1 <= v <= n and v not in out:
                out.append(v)
    return out


def env_categories(options):
    """Auswahl aus EXPORT_CATEGORIES, z. B. "mail,contacts" oder "1on1,group".

    Für Aufrufer ohne Terminal (app.py, Scheduler, Cron). Unbekannte Namen
    werden ignoriert; bleibt nichts übrig, zählt die Variable als nicht
    gesetzt -> None (normale Abfrage bzw. Standardauswahl).
    """
    raw = os.environ.get("EXPORT_CATEGORIES")
    if not raw:
        return None
    picked = {t.strip().lower() for t in raw.replace(";", ",").split(",")}
    sel = {k for k, _ in options if k.lower() in picked}
    return sel or None


# ---------------------------------------------------------------------------
# Namen und Zeiten
# ---------------------------------------------------------------------------
def kuerzel(s):
    """Acht Hex-Zeichen aus dem Inhalt – macht gekürzte Namen wieder eindeutig."""
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:8]


def safe(name, maxlen=80):
    """Ein Namensstück, dem das Dateisystem trauen kann.

    OneDrive hat eine eigene Fassung, die beim Kürzen die Endung erhält –
    dort entscheidet sie über den Dateityp auf der Platte.
    """
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:maxlen] or "unbenannt"


def graph_zeit(iso):
    """ISO-8601 aus Graph -> datetime (UTC-bewusst) oder None.

    Graph liefert teils 7-stellige Sekundenbruchteile, die fromisoformat nicht
    nimmt – sie werden auf 6 gekürzt. Unparsebares ergibt None, nie eine
    Ausnahme: ein kaputter Zeitstempel darf keinen Export beenden.
    """
    if not iso:
        return None
    try:
        s = str(iso).replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Atomar schreiben und die geteilten Vermerk-Dateien
# ---------------------------------------------------------------------------
def schreibe_atomar(ziel, text):
    """Erst .tmp, dann ersetzen – ein Abbruch hinterlässt nie eine halbe Datei,
    die beim nächsten Lauf als fertig gälte."""
    ziel = Path(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(ziel.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(ziel)
    return ziel


def lies_verschwunden(pfad):
    """rel -> Zeitpunkt des ersten Fehlens."""
    out = {}
    try:
        for zeile in Path(pfad).read_text(encoding="utf-8").splitlines():
            if "\t" in zeile:
                rel, wann = zeile.split("\t", 1)
                out[rel] = wann
    except OSError:
        pass
    return out


def schreibe_verschwunden(pfad, bekannt, neue, jetzt):
    """Bestehende Vermerke behalten, neue ergänzen. Atomar."""
    zusammen = dict(bekannt)
    for rel in neue:
        zusammen.setdefault(rel, jetzt)
    schreibe_atomar(pfad, "".join(f"{rel}\t{wann}\n"
                                  for rel, wann in sorted(zusammen.items())))
    return zusammen


def schreibe_bericht(out, bericht):
    """Das Ergebnis der Vollständigkeitsprüfung ablegen – für die App."""
    return schreibe_atomar(Path(out) / BERICHT_DATEI,
                           json.dumps(bericht, ensure_ascii=False))
