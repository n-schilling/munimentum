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
from datetime import datetime, UTC
from pathlib import Path

# File names that the Outlook and OneDrive exports use alike: what has
# disappeared from the source, and the completeness check's report.


def resource_dir():
    """Where the shipped files lie – the scripts, `lang/`, the page. In a
    bundle that is the unpacked archive, not the folder a module happens
    to sit in; as a script it is the project folder. One answer for the
    app and the MCP server – the 7.0.0 config bug came from a second
    one."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


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
def fehlertext(e):
    """An exception as the log names it: type and message – and, for a
    request Graph refused, the service's own error code and message from
    the response body. The status alone says that something was rejected;
    the body says what ("The query parameter '$top' is not supported")."""
    text = f"{type(e).__name__}: {e}"
    roh = getattr(getattr(e, "response", None), "text", "") or ""
    if roh:
        try:
            fehler = (json.loads(roh) or {}).get("error") or {}
        except (ValueError, AttributeError):
            fehler = {}
        if isinstance(fehler, dict) and (fehler.get("code") or fehler.get("message")):
            text += f" – {fehler.get('code') or '?'}: {fehler.get('message') or ''}".rstrip(": ")
    return text


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
    this flag – the cadence gate steps aside once. A full sync (voll_neu)
    counts as one: nothing may wait for its cadence in a run that reads
    everything."""
    return bool((os.environ.get("SYNC_NOW") or "").strip()) or abgleich()


def abgleich():
    """"Fetch now" and "Fetch again" (RESYNC): the run sets the stored
    change pointers aside – delta links, walk pointers, chat watermarks –
    but keeps every file's version, so the source is listed once in full
    and only what is not here comes: a mail whose file is gone, a file the
    last delta round never named, a card whose attachment is missing. What
    lies here and is current is neither fetched nor written over; nothing
    is deleted. Every cadence gate steps aside (sync_jetzt). A full sync
    (voll_neu) forgets the pointers as well, so it counts as one."""
    return bool((os.environ.get("RESYNC") or "").strip()) or voll_neu()


def abgleich_ordner():
    """RESYNC_FOLDERS: the folders the balance found something open in –
    rel paths as the export lays them out ("E-Mail/…", "kalender/<name>",
    "kontakte/…"). A resync that carries them lists only those and leaves
    every other folder, calendar and contact folder untouched: a fetch for
    26 open mails need not read the whole mailbox. None on a plain resync;
    then everything is listed."""
    roh = (os.environ.get("RESYNC_FOLDERS") or "").strip()
    if not roh:
        return None
    try:
        daten = json.loads(roh)
    except ValueError:
        return None
    return [str(p) for p in daten if isinstance(p, str)] if isinstance(daten, list) else None


def nachhol_liste():
    """"Fetch again" (FETCH_LIST): the path of a JSON file naming the files
    the archive check found missing or incomplete – rels below the source's
    folder. An export that finds it fetches exactly those, each through its
    own bookkeeping (the mail's id, the drive item, the conversation, the
    unit that holds it), reads nothing else and touches nothing else; a
    file Microsoft no longer has is said so and stays an entry without a
    file. None on a regular run; an unreadable list counts as empty."""
    eintraege = nachhol_eintraege()
    if eintraege is None:
        return None
    return [e["rel"] if isinstance(e, dict) else e for e in eintraege]


def nachhol_eintraege():
    """The list as written: rels, or {id, rel} pairs where the caller knows
    the item (the balance's open files, or its open Teams conversations –
    there `rel` may be empty, a conversation never exported has no file
    yet). The exports fetch those by id without asking the inventory.
    None on a regular run."""
    pfad = (os.environ.get("FETCH_LIST") or "").strip()
    if not pfad:
        return None
    try:
        daten = json.loads(Path(pfad).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    dateien = daten.get("dateien") if isinstance(daten, dict) else daten
    out = []
    for d in dateien or []:
        if isinstance(d, str):
            out.append(d)
        elif isinstance(d, dict) and d.get("id"):
            e = {"id": str(d["id"]), "rel": str(d.get("rel") or "")}
            for k in ("pfad", "art"):       # the balance's row and kind, where it names them
                if d.get(k):
                    e[k] = str(d[k])
            out.append(e)
    return out


def nachholen_melden(geholt, weg=0, fehler=0, unbekannt=0):
    """The one result of a targeted fetch: what came, what Microsoft no
    longer has, what failed, what no bookkeeping knew."""
    import progress
    progress.event("run.nachholen.done", n=geholt, gone=weg, failed=fehler,
                   unknown=unbekannt)
    progress.ergebnis(geholt, errors=fehler, extra={"gone": weg, "unknown": unbekannt})


def http_status(e):
    """The HTTP status an exception carries (requests.HTTPError), or None."""
    return getattr(getattr(e, "response", None), "status_code", None)


# ---------------------------------------------------------------------------
# Permanent failures: what a refused request says about the item
# ---------------------------------------------------------------------------
# The client retries what passes (401, 429, 5xx) and raises the rest. Of
# the rest, two answers are verdicts, not hiccups: Microsoft refuses the
# item (403 – a permission that is not there, a file flagged as malware),
# or the item is no longer there (404, 410). Asking again every night
# changes nothing about either, so every export records such an item
# (permanent_mark) in its state.db and leaves it alone until a full sync
# – and the pointers and cadences advance as if the item had come.
REFUSED, GONE = "refused", "gone"
PERMANENT_EVENTS = {REFUSED: "run.item.refused", GONE: "run.item.gone"}
_REFUSED_CODES = {"malwaredetected"}
_STATUS_IM_TEXT = re.compile(r"\bHTTP (\d{3})\b")


def graph_code(body):
    """The service's error code inside a Graph error body, lower-cased."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("code") or "").lower()
    return ""


def verdict_status(status, body=None):
    """What an answer says about the item asked for: REFUSED (403, or a
    malware verdict whatever the status), GONE (404, 410), or None – a
    passing condition, worth another try next run."""
    if graph_code(body) in _REFUSED_CODES:
        return REFUSED
    if status == 403:
        return REFUSED
    if status in (404, 410):
        return GONE
    return None


def verdict(e):
    """verdict_status for an exception: the status and body of a
    requests.HTTPError, else the "HTTP nnn" the message names."""
    status = http_status(e)
    body = None
    roh = getattr(getattr(e, "response", None), "text", "") or ""
    if roh:
        try:
            body = json.loads(roh)
        except ValueError:
            body = None
    if status is None:
        m = _STATUS_IM_TEXT.search(str(e))
        status = int(m.group(1)) if m else None
    return verdict_status(status, body)


def permanent_mark(kind, error, name=None, rel=None, unit=None, version=None, quiet=False):
    """The record of an item no run asks for again: the kind (REFUSED or
    GONE), the error in one line, the name the log used, the path its
    copy has or would have had, the unit it belongs to (a conversation,
    a task, a list), and the version the verdict was given for – a
    changed version is asked once more. A `quiet` mark keeps a run from
    asking but is no item of the archive (an inline image in a message):
    the inward check does not count it."""
    mark = {"kind": kind, "error": str(error)[:200], "name": name, "rel": rel,
            "unit": unit, "version": version or "",
            "when": datetime.now(UTC).isoformat(timespec="seconds")}
    if quiet:
        mark["quiet"] = True
    return mark


def permanent_event(kind, name, error):
    """The one log line a verdict gets."""
    import progress
    progress.event(PERMANENT_EVENTS[kind], "warn", name=str(name or "?")[:60],
                   error=str(error)[:120])


def voll_neu():
    """The source's "Force full sync" button (FULL_SYNC): the run sets
    every stored change pointer of the source aside – delta links, chat
    watermarks, inventory versions, page stamps, task etags – and reads
    the source once as on its first export, writing everything again.
    Nothing in the archive is deleted: a file Microsoft no longer has
    stays, with its tombstone if it ever got one. The pointers are
    forgotten, not bypassed, so a run cut short continues on the next
    regular one wherever an export keeps its stamps per item. Every
    cadence gate steps aside as well (sync_jetzt)."""
    return bool((os.environ.get("FULL_SYNC") or "").strip())


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
