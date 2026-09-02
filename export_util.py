#!/usr/bin/env python3
"""
export_util.py – gemeinsame Helfer der Exportskripte.

Bis 5.3 trug jedes Skript eigene Kopien: Dateinamen entschärfen, Graph-Zeiten
parsen, Vermerke atomar schreiben, die Frage „sitzt hier jemand vor einem
Terminal?". Die Kopien wichen in Kleinigkeiten voneinander ab, ohne dass eine
Abweichung je gewollt war – hier steht jede Antwort einmal.

Nur Standardbibliothek.
"""

import json
import os
import re
import sys
import hashlib
from datetime import datetime
from pathlib import Path

# Dateinamen, die Outlook- und OneDrive-Export gleich benutzen: was aus der
# Quelle verschwunden ist, und der Bericht der Vollständigkeitsprüfung.


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
# Kategorien-Auswahl der App (die Skripte fragen nie zurück)
# ---------------------------------------------------------------------------
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

# Sync cadence: how often a source gets synced at most. 0 = always; the
# minute of slack keeps an hourly schedule from missing the daily boundary
# by a hair. Shared between the app (service level) and the SharePoint
# export (per library/site).
CADENCE_S = {"always": 0, "daily": 86400, "weekly": 7 * 86400,
             "monthly": 30 * 86400}


def cadence_faellig(cadence, letzter, jetzt=None):
    periode = CADENCE_S.get(cadence or "always", 0)
    if not periode or letzter is None:
        return True
    import time as _time
    return (jetzt if jetzt is not None else _time.time()) - letzter >= periode - 60


# ---------------------------------------------------------------------------
# Sync-Kadenz je Quell-URL – geteilt von den SharePoint- und Planner-Exporten
# ---------------------------------------------------------------------------
def kadenzen():
    """Cadence per source URL, e.g. "planner-url:<url>" – from the app via
    the SYNC_CADENCE environment, from settings when run without it."""
    import settings
    roh = os.environ.get("SYNC_CADENCE")
    if roh is None:
        roh = json.dumps(settings.value("sync_cadence", {}) or {})
    try:
        daten = json.loads(roh)
    except ValueError:
        return {}
    return daten if isinstance(daten, dict) else {}


def sync_jetzt():
    """The per-row "Sync now" button: the run carries just that URL and
    this flag – the cadence gate steps aside once."""
    return bool((os.environ.get("SYNC_NOW") or "").strip())


_KADENZ_RANG = {"always": 0, "daily": 1, "weekly": 2, "monthly": 3}


def haeufigere(a, b):
    """Two URLs feeding one unit: the more frequent cadence wins."""
    return a if _KADENZ_RANG.get(a, 0) <= _KADENZ_RANG.get(b, 0) else b


def einheit_faellig(db, kadenz, kv_key="last_sync"):
    if sync_jetzt() or (kadenz or "always") == "always":
        return True
    roh = db.kv_lesen(kv_key)
    letzter = float(roh) if roh else None
    return cadence_faellig(kadenz, letzter)
