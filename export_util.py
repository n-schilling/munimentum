#!/usr/bin/env python3
"""
export_util.py – shared helpers of the export scripts.

Each script used to carry its own copies: defusing file names, parsing Graph
timestamps, writing markers atomically, the question "is someone sitting at
a terminal here?". The copies diverged in small ways without any divergence
ever being intended – here every answer lives once.

Standard library only.
"""

import json
import os
import re
import sys
import hashlib
from datetime import datetime
from pathlib import Path

# File names that the Outlook and OneDrive exports use alike: what has
# disappeared from the source, and the completeness check's report.


def erzwinge_utf8():
    """Set stdout/stderr to UTF-8 (a no-op on macOS/Linux).

    Windows consoles otherwise use a legacy codepage (e.g. cp1252), and the
    locale encoding when redirected to a file. Either makes any output of
    characters like →, ✓ or emoji fail with UnicodeEncodeError and aborts
    the run.
    """
    for strom in (sys.stdout, sys.stderr):
        try:
            strom.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def ausgabeordner(argv):
    """The output directory, always the first positional argument.

    7.0 removed the scripts' own defaults: the app is the only caller and
    passes the directory explicitly, so a bare call must fail loudly instead
    of quietly writing next to the current working directory.
    """
    if not argv:
        raise SystemExit("output directory missing - "
                         "the app passes it as the first argument")
    return Path(argv[0])


def hilfe_gewuenscht(argv):
    """Answer -h/--help instead of creating a folder of that name.

    The export scripts read the first free argument as the output
    directory. Without this check, `python3 outlook_export.py --help`
    dutifully created a folder named "--help" and started exporting – it
    happened once, and even got checked in.
    """
    return any(a in ("-h", "--help", "-help", "help") for a in argv)


# ---------------------------------------------------------------------------
# The app's category selection (the scripts never ask back)
# ---------------------------------------------------------------------------
def env_categories(options):
    """Selection from EXPORT_CATEGORIES, e.g. "mail,contacts" or "1on1,group".

    For callers without a terminal (app.py, scheduler, cron). Unknown names
    are ignored; if nothing remains, the variable counts as not set -> None
    (normal prompt or default selection).
    """
    raw = os.environ.get("EXPORT_CATEGORIES")
    if not raw:
        return None
    picked = {t.strip().lower() for t in raw.replace(";", ",").split(",")}
    sel = {k for k, _ in options if k.lower() in picked}
    return sel or None


# ---------------------------------------------------------------------------
# Names and times
# ---------------------------------------------------------------------------
def kuerzel(s):
    """Eight hex characters from the content – makes shortened names unique again."""
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:8]


def safe(name, maxlen=80):
    """A name fragment the file system can trust.

    OneDrive has its own version that preserves the extension when
    shortening – there it decides the file type on disk.
    """
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:maxlen] or "unbenannt"


def graph_zeit(iso):
    """ISO 8601 from Graph -> datetime (UTC-aware) or None.

    Graph sometimes delivers 7-digit fractional seconds, which fromisoformat
    rejects – they are trimmed to 6. Anything unparsable yields None, never
    an exception: a broken timestamp must not end an export.
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
# Atomic writes and the shared marker files
# ---------------------------------------------------------------------------
def schreibe_atomar(ziel, text):
    """First .tmp, then replace – an abort never leaves a half file that the
    next run would take as finished."""
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
# Sync cadence per source URL – shared by the SharePoint and Planner exports
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


def kadenz_fuer(kadenzen, praefix, pfad, vorgabe="always"):
    """The cadence that applies to one unit inside a hierarchy.

    `kadenzen` holds "<praefix>:<path>" keys – a whole category ("teams:1on1",
    "outlook:mail") as well as any folder, team, channel or chat below it
    ("outlook:mail:E-Mail/Archiv", "teams:channels/Nordwind/Releases"). The
    deepest key on the unit's path wins, the category key is the last
    resort, then `vorgabe`. A value set on a parent therefore reaches every
    child until a child sets its own."""
    teile = [t for t in str(pfad or "").split("/") if t]
    while teile:
        wert = kadenzen.get(f"{praefix}:{'/'.join(teile)}")
        if wert:
            return wert
        teile.pop()
    return kadenzen.get(praefix) or vorgabe


def einheit_faellig(db, kadenz, kv_key="last_sync"):
    if sync_jetzt() or (kadenz or "always") == "always":
        return True
    roh = db.kv_lesen(kv_key)
    letzter = float(roh) if roh else None
    return cadence_faellig(kadenz, letzter)
