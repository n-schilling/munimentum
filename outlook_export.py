#!/usr/bin/env python3
"""
Outlook/Exchange export as .eml via Microsoft Graph (delegated, no admin needed).

- Every mail as .eml (full MIME via /messages/{id}/$value, incl. attachments
  and inline images). Importable directly into any mail client.
- Optionally selectable in addition: calendars as .ics (events, times in UTC)
  and contacts as .vcf – in their own subfolders kalender/ and kontakte/.
- The mailbox's folder structure is mirrored as a directory tree under E-Mail/
  (recursively) – alongside kalender/ and kontakte/.
- PARALLEL: up to 4 downloads at once. Exchange Online allows only 4 concurrent
  requests per mailbox (MailboxConcurrency, a fixed limit) – more just produces
  429s. A global semaphore keeps listing + downloads together under this
  limit; on 429 we back off per Retry-After.
- ROBUST: network errors (timeout, dropped connection, TLS) are retried with
  backoff; a folder that cannot be listed completely is skipped instead of
  aborting the run (the next run catches it up).

Runs as a subprogram of app.py: the app passes the output folder as the only
argument and every setting as an environment variable (EXPORT_CATEGORIES,
FOLDER_RULES, CALENDAR_RULES, SKIP_FOLDERS, INCLUDE_HIDDEN, EXPORT_WORKERS,
GRAPH_TOKEN/GRAPH_AUTH; environment beats app_config.json, see settings.py).
There are no prompts; progress, results and failures come back as structured
lines (see progress.py).

Which folders and calendars come along is decided by ordered rules over the
stored folder and calendar lists (folders.py; they live in the output
folder's state.db).

Special runs, none of which exports: --folders and --calendars refresh those
stored lists, --check reports completeness against the mailbox.

Resume: the done log in the output folder's state.db, one row per finished
mail – a new run skips everything already there. Delete the database for a
full re-export.

Change tracking: every mail folder, calendar and contact folder is read as a
Graph delta round – the first time in full, afterwards only what changed
since the stored link (state.db, kv "delta:…"). Deletions arrive as removed
entries; changed appointments and contacts are rewritten. OUTLOOK_SINCE
bounds a folder's first export, CALENDAR_MONTHS_BACK the calendar window,
CALENDAR_FULL reads the calendars once in full. FULL_SYNC (the source's
"Force full sync", see export_util.voll_neu) drops every stored link first
and writes every mail, event and contact again – the resume log is not
asked. SYNC_CADENCE gates calendar
and contacts as categories (outlook:calendar, outlook:contacts) and mail per
folder: "outlook:mail" for all, "outlook:mail:<folder path>" for a folder and
everything below it until a deeper folder sets its own.
"""

import os
import sys
import re
import html
import json
from collections import Counter
import time
import threading
from calendar import monthrange
from datetime import datetime, timedelta, UTC
from itertools import chain
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED


import auth
import export_util
import completeness
import folders
import state_db
import graph_client
import settings
import progress
import versions

try:
    # msal is only needed in auth.py (and only in login mode) – checked here
    # just so the missing-packages message arrives early and in one place.
    import msal  # noqa: F401
    import requests
except ImportError:
    print("Fehlende Pakete. Bitte installieren:  pip install msal requests")
    raise SystemExit(1) from None

export_util.erzwinge_utf8()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Mail.Read", RES + "Calendars.Read", RES + "Contacts.Read", RES + "User.Read"]
# Environment variable > app_config.json > default here (see settings.py)
INCLUDE_HIDDEN = settings.flag("INCLUDE_HIDDEN", "include_hidden")
PAGE = 50                   # $top for list requests
DELTA_PAGE = 200            # Prefer: odata.maxpagesize for delta rounds (no documented cap)
YEARS_AHEAD = 10            # the calendar window always reaches this far ahead
EPOCH = "1970-01-01T00:00:00Z"     # the window's start when it has no start
UTC_PREF = 'outlook.timezone="UTC"'  # times in UTC -> correct .ics
MAIL_DIR = "E-Mail"          # the mailbox folder tree lives below (next to kalender/kontakte)
KALENDER_DIR = "kalender"    # one subfolder per calendar, the .ics inside

# These mailbox folders are NOT included by default with "all" (Enter) – only
# by explicit selection. Compared case-insensitively by display name (DE + EN).
BUILTIN_SKIP_FOLDERS = {
    "archive", "archiv",
    "entwürfe", "drafts",
    "erneut erinnern aktiviert",
    "gelöschte elemente", "deleted items",
    "junk-e-mail", "junk email", "junk-email",
    "postausgang", "outbox",
}


DEFAULT_SKIP_FOLDERS = settings.folders("SKIP_FOLDERS", "skip_folders")

# Network, throttling, retry and paging live in graph_client.py – one layer
# shared by all the exports.
STOP = threading.Event()                     # signal: token dead -> start nothing new

# Sign-in, token mode and configuration live in auth.py.
TokenExpired = auth.TokenExpired
load_pasted_token = auth.load_pasted_token
TokenClient = graph_client.TokenClient


def graph_login(nur_still=False):
    """Signed-in access with the mail/calendar/contacts scopes."""
    return graph_client.Graph(SCOPES, nur_still=nur_still)


# ---------------------------------------------------------------------------
# Progress: append-only log, thread-safe (scales to tens of thousands of mails)
# ---------------------------------------------------------------------------
DoneLog = state_db.DbDoneLog     # resume log, one row per mail in state.db


# ---------------------------------------------------------------------------
# Selection – no prompts: the app is the only caller, and nobody would see
# a question asked by a subprocess.
# ---------------------------------------------------------------------------
def list_calendars(graph):
    """Reads the calendar list for targeted selection. Empty list when the
    permission is missing – no calendar entries appear in the menu then."""
    try:
        cals = list(graph.paged(f"{GRAPH}/me/calendars", {"$top": PAGE}))
    except TokenExpired:
        raise
    except Exception as e:
        progress.event("run.unreadable", "warn",
                       name=progress.atom("export.cat.calendar"), error=str(e))
        return []
    cals.sort(key=lambda c: (not c.get("isDefaultCalendar"), (c.get("name") or "").lower()))
    return cals


env_categories = export_util.env_categories


def selected_categories():
    """Which of mail/calendar/contacts to export – from EXPORT_CATEGORIES.

    The app sets the variable on every run; without it, everything is taken.
    Returns a subset of {"mail", "calendar", "contacts"}.
    """
    options = [("mail", ""), ("calendar", ""), ("contacts", "")]
    env = env_categories(options)
    if env is not None:
        return env
    progress.event("run.default_selection")
    return {k for k, _ in options}


def outlook_since():
    """First export of a folder only: nothing older than this day – or None.

    Applied while listing, not as a Graph $filter: a filtered delta round
    stops at 5,000 messages (documented), and a folder that silently ends
    there would be a gap nobody sees.
    """
    roh = os.environ.get("OUTLOOK_SINCE")
    if roh is None:
        roh = settings.value("outlook_since", "")
    roh = str(roh or "").strip()
    return roh if re.fullmatch(r"\d{4}-\d{2}-\d{2}", roh) else None


def calendar_months_back():
    """How far back the calendar window reaches; 0 means every appointment."""
    return settings.number("CALENDAR_MONTHS_BACK", "calendar_months_back", low=0)


def calendar_full():
    """One full read of the calendars: window and change links set aside once."""
    return settings.flag("CALENDAR_FULL", "calendar_full", False)


def kalender_eintraege(cals):
    """The calendar list in the shape folders.py reckons with.

    The path is the one under which the calendar lands on disk
    (kalender/<name>). That way the same rules, the same preview and the
    same counting apply as for mailbox folders – and a renamed calendar
    stands out in the preview as "only in the archive now" instead of
    silently lying around twice.
    """
    return [{
        "id": c.get("id") or f"{KALENDER_DIR}/{safe(c.get('name') or 'Kalender')}",
        "pfad": f"{KALENDER_DIR}/{safe(c.get('name') or 'Kalender')}",
        "name": c.get("name") or "Kalender",
        "standard": bool(c.get("isDefaultCalendar")),
        "elemente": 0,      # Graph doesn't count events; the preview counts
    } for c in cals or ()]  # what already lies in the archive instead


def kalender_regeln(daten=None):
    """Which calendars get exported – environment beats file beats default.

    Without rules of your own it stays the default calendar: besides its own,
    a mailbox often carries birthdays, holidays and other people's shares,
    and nobody who ticks "calendar" meant those.
    """
    roh = os.environ.get("CALENDAR_RULES")
    if roh is None:
        roh = settings.value("calendar_rules", None)
    if roh and roh.strip():
        return folders.lies_regeln(roh)
    return folders.nur_standard((daten or {}).get("ordner", []))


def waehle_kalender(graph, out):
    """Pick calendars from the stored list; fetch it once when it is missing."""
    daten = folders.lade(out, folders.KALENDER)
    if daten is None:
        progress.event("run.calendars.loading")
        eintraege = kalender_eintraege(list_calendars(graph))
        if not eintraege:
            # Usually the missing Calendars.Read permission. Storing an empty
            # list would mean never fetching it again – and the export would
            # stay silently empty forever, with nobody seeing why.
            progress.event("run.calendars.none", "warn")
            return []
        daten = folders.speichere(out, eintraege, datei=folders.KALENDER)
    regeln = kalender_regeln(daten)
    gewaehlt = folders.gewaehlt(daten, regeln)
    alle = daten.get("ordner", [])
    progress.event("run.selection", chosen=len(gewaehlt), total=len(alle),
                   unit=progress.atom("progress.unit.calendars"))
    if daten.get("neu"):
        progress.event("run.selection.new", n=len(daten["neu"]))
    return [{"id": e["id"], "name": e["name"]} for e in gewaehlt]


def gleiche_kalender_ab(argv):
    """--calendars: only fetch and store the calendar list, export nothing."""
    out = export_util.ausgabeordner(argv)
    graph = auth.waehle_zugang(TokenClient, graph_login)
    vorher = folders.lade(out, folders.KALENDER)
    daten = folders.speichere(out, kalender_eintraege(list_calendars(graph)),
                              vorher, datei=folders.KALENDER)
    gewaehlt = folders.gewaehlt(daten, kalender_regeln(daten))
    progress.event("run.sync.result", total=len(daten["ordner"]),
                   chosen=len(gewaehlt),
                   unit=progress.atom("progress.unit.calendars"))
    if daten["neu"] or daten["verschwunden"] or daten["umbenannt"]:
        progress.event("run.sync.changed", new=len(daten["neu"]),
                       gone=len(daten["verschwunden"]),
                       renamed=len(daten["umbenannt"]))
    progress.ergebnis(len(daten["neu"]),
                      extra={"total": len(daten["ordner"]),
                             "chosen": len(gewaehlt),
                             "gone": len(daten["verschwunden"])})


# ---------------------------------------------------------------------------
# Helpers (shared in export_util.py)
# ---------------------------------------------------------------------------
safe = export_util.safe
short_id = export_util.kuerzel


def mail_filename(msg):
    dt = msg.get("receivedDateTime") or msg.get("sentDateTime") or ""
    geparst = export_util.graph_zeit(dt)
    stamp = geparst.astimezone().strftime("%Y-%m-%d_%H%M") if geparst else dt[:10]
    subj = (msg.get("subject") or "").strip() or "(kein Betreff)"
    prefix = (stamp + "__") if stamp else ""
    return f"{prefix}{safe(subj, 90)}__{short_id(msg['id'])}.eml"


def folder_params():
    p = {"$top": 100}
    if INCLUDE_HIDDEN:
        p["includeHiddenFolders"] = "true"
    return p


def list_children(graph, folder):
    """Lists the direct subfolders – independent of childFolderCount."""
    try:
        return list(graph.paged(f"{GRAPH}/me/mailFolders/{folder['id']}/childFolders",
                                folder_params()))
    except TokenExpired:
        raise
    except Exception as e:
        progress.event("run.unreadable", "warn",
                       name=str(folder.get("displayName")), error=str(e))
        return []


def _subtree(graph, folder, rel_path, acc):
    """Appends (folder, rel_path) for the folder and ALL descendants to acc.
    Does not rely on childFolderCount but always lists the children – so
    even deeply nested subfolders are captured reliably."""
    acc.append((folder, rel_path))
    for child in list_children(graph, folder):
        cname = safe(child.get("displayName") or "Ordner")
        _subtree(graph, child, f"{rel_path}/{cname}", acc)


def build_tree(graph):
    """Reads the complete folder structure ONCE and yields, per top-level
    folder, the subtree with its recursive item count. The result is used for
    the selection AND the export (no re-listing during the parallel download)."""
    tops = []
    roots = list(graph.paged(f"{GRAPH}/me/mailFolders", folder_params()))
    count = 0
    for tf in roots:
        rel = f"{MAIL_DIR}/{safe(tf.get('displayName') or 'Ordner')}"
        sub = []
        _subtree(graph, tf, rel, sub)
        items = sum((f.get("totalItemCount") or 0) for f, _ in sub)
        tops.append({"folder": tf, "rel": rel, "subtree": sub,
                     "items": items, "nfolders": len(sub)})
        count += len(sub)
    progress.event("run.folders_listed", n=count)
    return tops


# ---------------------------------------------------------------------------
# Detecting vanished mails
#
# An archive that only grows fails to answer the most important question:
# what was here once and is now gone? The file stays put, of course – all
# that gets recorded is that it no longer shows up in the mailbox.
#
# The trap here is confusing deleted with moved. A mail that wanders into a
# folder this run does not export (the archive isn't in the default
# selection) would look vanished. That is why every suspicion is checked
# with Graph: 404 means truly gone, everything else means moved.
# ---------------------------------------------------------------------------


class Bestand:
    """What really lay in the mailbox during this run – and what the run
    owes the next one."""

    def __init__(self):
        self.gesehen = set()        # IDs from completely listed folders
        self.briefe = set()         # their internetMessageId (survives a move)
        self.vollstaendig = []      # their paths, with a trailing slash
        self.entfernt = set()       # IDs Graph reported as removed (delta rounds)
        self.links = {}             # folder path -> (folder id, delta link)
        self.gestoert = set()       # folder paths with a failed download or listing
        self.per_link = set()       # folder paths read from their link this run
        self.ausgelassen = set()    # folder paths the cadence left out this run

    def ordner_fertig(self, rel_path):
        self.vollstaendig.append(rel_path.rstrip("/") + "/")

    def aus_gelistetem_ordner(self, rel):
        """Does the file's folder count as listed in full?

        A folder read from its link, or left out by its cadence, is
        excluded by name: a full round of its parent must not turn its
        unlisted mails into suspects. The prefix test stays, so a folder
        that vanished with its mails still resolves through the parent.
        """
        ordner = rel.rsplit("/", 1)[0]
        if ordner in self.per_link or ordner in self.ausgelassen:
            return False
        return any(rel.startswith(p) for p in self.vollstaendig)

    def ordner_gestoert(self, rel_path):
        """A listing that broke off: nothing this folder reported counts."""
        self.gestoert.add(rel_path)

    def link_merken(self, rel_path, folder_id, link):
        self.links[rel_path] = (folder_id, link)

    def download_fehlgeschlagen(self, rel):
        self.gestoert.add(rel.rsplit("/", 1)[0])

    def sauber(self, rel):
        """No failed download in the folder this file belongs to."""
        return rel.rsplit("/", 1)[0] not in self.gestoert

    def gemeldet(self, done):
        """Removed entries that name an exported mail, as (id, rel).

        Only from folders without a download error: such a folder keeps
        its old link, so the next round reports its removals again.
        """
        return sorted((mid, done.done[mid]) for mid in self.entfernt
                      if mid in done.done and self.sauber(done.done[mid]))

    def links_sichern(self, db):
        """Store the links of the folders that finished clean – and mark
        that clean run for the folder's cadence, keyed by id so a rename
        does not reset the clock.

        A folder with a failed download keeps its old start: the mail has
        to show up again in the next round, or it would never be fetched.
        """
        jetzt = str(time.time())
        for rel_path, (fid, link) in self.links.items():
            if rel_path not in self.gestoert:
                db.kv_schreiben(f"delta:{fid}", link)
                db.kv_schreiben(f"last_sync:mail:{fid}", jetzt)


def brief_kennung(pfad):
    """The Message-ID from the header of a stored .eml.

    Only the header is read: loading a whole .eml with a 40 MB attachment
    to pull one line out of it would be expensive across hundreds of
    suspects. Folded continuation lines practically never occur for this
    header line, but are taken along so an edge case cannot produce a wrong
    answer.
    """
    try:
        with open(pfad, "rb") as f:
            wert = None
            for roh in f:
                if roh in (b"\r\n", b"\n"):        # end of the header
                    break
                if wert is not None:
                    if roh[:1] in (b" ", b"\t"):     # continuation
                        wert += roh.strip()
                        continue
                    break
                if roh[:11].lower() == b"message-id:":
                    wert = roh[11:].strip()
            return wert.decode("utf-8", "replace").strip() if wert else None
    except OSError:
        return None


def verschoben_statt_weg(out, kandidaten, bestand):
    """Weed out suspects whose letter shows up in the mailbox again.

    Exchange assigns a NEW message ID on a move. Asking Graph about the old
    one therefore returns 404 – and a mail merely shoved into another folder
    counted as deleted. On a real archive, 16 of 19 records were wrong that
    way.

    The internetMessageId survives the move. It sits in every stored .eml
    and comes along with the listing at no extra cost, so the case can be
    decided here without a single further request.
    """
    if not bestand.briefe:
        return kandidaten, 0
    bleibt, verschoben = [], 0
    for mid, rel in kandidaten:
        kennung = brief_kennung(out / rel)
        if kennung and kennung in bestand.briefe:
            verschoben += 1
        else:
            bleibt.append((mid, rel))
    return bleibt, verschoben


def zuruecknehmen(out, bekannt, bestand):
    """Check earlier records: what lies in the mailbox again was never deleted.

    Without this the mistake would stand forever – those records were made
    under the old, wrong assumption.
    """
    if not bestand.briefe:
        return bekannt, 0
    behalten = {}
    for rel, wann in bekannt.items():
        kennung = brief_kennung(out / rel)
        if kennung and kennung in bestand.briefe:
            continue
        behalten[rel] = wann
    return behalten, len(bekannt) - len(behalten)


def verdaechtige(done, bestand):
    """Exported before, not seen in this run any more.

    Only from folders that were listed completely – an aborted listing must
    not declare half a folder deleted.
    """
    return sorted((mid, rel) for mid, rel in done.done.items()
                  if mid not in bestand.gesehen and bestand.aus_gelistetem_ordner(rel))


def wirklich_weg(graph, kandidaten, grenze=2000):
    """Ask Graph about every suspicion – twenty per request, as a JSON
    batch. Returns (gone, moved).

    Only a 404 counts as gone. Anything else (throttling, network, a batch
    that never came back) counts as "not gone": better to report a
    deletion later than a wrong one now.
    """
    weg, verschoben = [], 0
    urls = {f"{GRAPH}/me/messages/{mid}?$select=id": rel
            for mid, rel in kandidaten[:grenze]}
    antworten = {}
    if urls:
        try:
            antworten = graph.batch_get(list(urls))
        except TokenExpired:
            raise
        except Exception:
            antworten = {}          # unclear – claim nothing
    for url, rel in urls.items():
        status = (antworten.get(url) or (None,))[0]
        if status == 404:
            weg.append(rel)
        elif status is not None and 200 <= status < 300:
            verschoben += 1
    if len(kandidaten) > grenze:
        progress.event("run.gone.deferred", n=len(kandidaten) - grenze)
    return weg, verschoben


# ---------------------------------------------------------------------------
# Change tracking (delta queries)
#
# Graph remembers, per collection, where a client left off: a round of
# delta calls hands out every entry once, then a link the next round starts
# from – and from then on only what changed, deletions as "@removed"
# entries. The links live in the output folder's state.db, one per mail
# folder, calendar and contact folder.
# ---------------------------------------------------------------------------
_TOKEN_TOT = re.compile(
    r"^(HTTP )?410\b|syncStateNotFound|resyncRequired|SyncStateInvalid", re.I)


def token_ungueltig(e):
    """Does Graph no longer know the stored link? 410 Gone, or an error
    naming the sync state – either way the round starts over in full."""
    antwort = getattr(e, "response", None)
    if getattr(antwort, "status_code", None) == 410:
        return True
    text = str(e)
    try:
        text += " " + (antwort.text or "")
    except Exception:
        pass
    return bool(_TOKEN_TOT.search(text))


def delta_seiten(graph, url, params=None, prefer=()):
    """One delta round, page by page: (entries, link) – the link only with
    the last page. Pages are handed on as they arrive, so a big folder is
    never held in memory at once."""
    headers = {"Prefer": ", ".join((f"odata.maxpagesize={DELTA_PAGE}", *prefer))}
    daten = graph.get(url, params, headers)
    while True:
        weiter = daten.get("@odata.nextLink")
        yield daten.get("value") or [], (None if weiter else daten.get("@odata.deltaLink"))
        if not weiter:
            return
        daten = graph.get(weiter, extra_headers=headers)


def delta_runde(graph, db, key, url, params=None, prefer=(), name=""):
    """Start a round: from the stored link when there is one, from the top
    otherwise. Returns (pages, full) – full says whether the round lists
    the whole collection or only the changes since the last one.

    The first page is fetched here: a dead link shows on that request, and
    the answer is to forget it, say so, and read the collection once more
    in full.
    """
    token = db.kv_lesen(key)
    if token and export_util.abgleich():
        # "Fetch again" and "Force full sync": the link goes before the
        # round starts, so a run cut short lists the collection in full
        # again next time. The resync then skips what the resume log knows
        # and finds on disk; the full sync writes everything over.
        db.kv_schreiben(key, None)
        token = None
    if token:
        seiten = delta_seiten(graph, token, prefer=prefer)
        try:
            erste = next(seiten)
        except TokenExpired:
            raise
        except Exception as e:
            if not token_ungueltig(e):
                raise
            db.kv_schreiben(key, None)
            progress.event("run.outlook.delta_reset", "warn", name=name)
        else:
            return chain([erste], seiten), False
    seiten = delta_seiten(graph, url, params, prefer)
    return chain([next(seiten)], seiten), True


class Stempel:
    """lastModifiedDateTime per exported event or contact – the records
    area a change is measured against ("events", "contacts").

    Writes are collected and land in one go per source, before the
    source's link is stored: a crash in between costs one repeated
    comparison, never a missed change.
    """

    def __init__(self, db, bereich):
        self.db, self.bereich = db, bereich
        self.alt = db.saetze_lesen(bereich)
        self.neu, self.weg = {}, set()

    def bekannt(self, key):
        roh = self.neu.get(key) or self.alt.get(key)
        if not roh:
            return None
        try:
            return (json.loads(roh) or {}).get("lm")
        except ValueError:
            return None

    def merke(self, key, lm):
        self.neu[key] = json.dumps({"lm": lm or ""})
        self.weg.discard(key)

    def vergiss(self, key):
        self.neu.pop(key, None)
        if key in self.alt:
            self.weg.add(key)

    def schreibe(self):
        self.db.saetze_loeschen(self.bereich, self.weg)
        self.db.saetze_schreiben(self.bereich, self.neu)
        for key in self.weg:
            self.alt.pop(key, None)
        self.alt.update(self.neu)
        self.neu, self.weg = {}, set()


def veraendert(out, done, stempel, key, lm, datei_stempel=None):
    """New, or changed since its file was written?

    Archives from before change tracking carry no record: for those the
    file itself answers when it can (an .ics holds the DTSTAMP the export
    wrote), and an unchanged item is adopted without a rewrite. A full
    sync writes every item again.
    """
    if export_util.voll_neu() or not done.is_done(out, key):
        return True
    alt = stempel.bekannt(key)
    if alt is not None:
        return (lm or "") != alt
    if datei_stempel is not None and lm:
        vorher = datei_stempel(out / done.done[key])
        if vorher is not None and vorher != _ics_stempel(lm):
            return True
    stempel.merke(key, lm)
    return False


def _alte_datei_weg(out, alt, rel):
    """A rewrite under a new name (subject or start changed): the old file
    would stand as a second entry."""
    if alt and alt != rel:
        versions.remove(out / alt, successor=out / rel)


MAIL_SELECT = "id,internetMessageId,subject,receivedDateTime,sentDateTime"


def iter_messages_to_export(graph, out, done, stats, selected, bestand=None, marks=None):
    """Mirrors the folders onto the filesystem and yields (mid, rel) for
    every mail not yet exported. Listing runs in the main thread (lazily).

    Every folder is a delta round of its own: the first one lists it in
    full, later ones only what changed since the stored link. A round that
    breaks off counts as a folder error; its link is never stored, so the
    next run reads the folder from the same start again. A mail Microsoft
    refused or no longer had (`marks`, export_util.permanent_mark) is not
    yielded – the delta names it again only when it changes.
    """
    db = state_db.StateDb(out)
    seit = outlook_since()
    # A full sync writes every mail again – the resume log is not asked.
    alles = export_util.voll_neu()
    for top in selected:
        for folder, rel_path in top["subtree"]:
            (out / rel_path).mkdir(parents=True, exist_ok=True)
            total = folder.get("totalItemCount")
            if total is not None:
                progress.event("run.folder", name=rel_path, n=int(total))
            else:
                progress.event("run.folder_plain", name=rel_path)
            seen, link = 0, None
            try:
                seiten, voll = delta_runde(
                    graph, db, f"delta:{folder['id']}",
                    f"{GRAPH}/me/mailFolders/{folder['id']}/messages/delta",
                    {"$select": MAIL_SELECT}, name=rel_path)
                if bestand is not None and not voll:
                    bestand.per_link.add(rel_path)
                for eintraege, ende in seiten:
                    link = ende or link      # the link comes with the last page
                    for msg in eintraege:
                        mid = msg.get("id")
                        if not mid:
                            continue
                        if "@removed" in msg:
                            if bestand is not None:
                                bestand.entfernt.add(mid)
                            continue
                        seen += 1
                        if bestand is not None:
                            bestand.gesehen.add(mid)
                            if msg.get("internetMessageId"):
                                bestand.briefe.add(msg["internetMessageId"].strip())
                        if not alles and (done.is_done(out, mid) or (marks and mid in marks)):
                            stats["skipped"] += 1
                            continue
                        empfangen = (msg.get("receivedDateTime") or "")[:10]
                        if voll and seit and empfangen and empfangen < seit:
                            # First export only: older than the cut-off day.
                            stats["excluded"] = stats.get("excluded", 0) + 1
                            continue
                        yield mid, f"{rel_path}/{mail_filename(msg)}"
            except TokenExpired:
                raise
            except Exception as e:
                # A permanently stuck folder must not kill the whole run:
                # skip the rest, on to the next one. What is already exported
                # sits in the done log – the next run fetches the rest.
                stats["folder_errors"] = stats.get("folder_errors", 0) + 1
                if bestand is not None:
                    # Removals it reported before the break are not trusted:
                    # tombstones come from clean folders only. Its link is
                    # withheld anyway, so the next round reports them again.
                    bestand.ordner_gestoert(rel_path)
                progress.event("run.folder_incomplete", "err", name=rel_path,
                               error=f"{type(e).__name__}: {e}")
                continue
            if bestand is not None:
                # Only a fully traversed folder is fit for comparison – after
                # a break above we never get here at all. A round from a
                # link lists changes, not the folder: it names its removals
                # itself instead.
                if voll:
                    bestand.ordner_fertig(rel_path)
                if link:
                    bestand.link_merken(rel_path, folder["id"], link)
            if seen:
                progress.event("run.scanned", n=seen,
                               unit=progress.atom("progress.unit.mails"))


# ---------------------------------------------------------------------------
# Worker + parallel driver
# ---------------------------------------------------------------------------
def download_one(graph, out, done, mid, rel):
    if STOP.is_set():
        return ("stopped", mid)
    try:
        content, _ = graph.get_bytes(f"{GRAPH}/me/messages/{mid}/$value", label=" (MIME)")
    except TokenExpired:
        return ("expired", mid)
    except Exception as e:
        kind = export_util.verdict(e)
        if kind:
            return ("verdict", (kind, mid, rel, f"{type(e).__name__}: {e}"))
        return ("error", f"{mid[:16]}…: {e}")
    try:
        versions.write_bytes(out / rel, content)
    except Exception as e:
        return ("error", f"{rel}: {e}")
    done.mark(mid, rel)
    return ("ok", rel)


def run_export(graph, out, done, stats, selected, workers, bestand=None):
    db = state_db.StateDb(out)
    marks = db.permanent_lesen()         # mails no run asks for again
    marks_before = dict(marks)
    gen = iter_messages_to_export(graph, out, done, stats, selected, bestand, marks)
    cap = max(workers * 8, workers)      # this many tasks in the pipeline at once
    pending = set()
    ziele = {}                           # future -> (mid, rel): which folder a failure hits
    expired = False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        def fill():
            nonlocal expired
            while len(pending) < cap:
                try:
                    mid, rel = next(gen)
                except StopIteration:
                    return
                except TokenExpired:        # the token can die during the listing already
                    expired = True
                    STOP.set()
                    return
                fut = ex.submit(download_one, graph, out, done, mid, rel)
                pending.add(fut)
                ziele[fut] = (mid, rel)

        fill()
        while pending:
            finished, rest = wait(pending, return_when=FIRST_COMPLETED)
            pending = set(rest)
            for fut in finished:
                mid, rel = ziele.pop(fut, ("", ""))
                try:
                    status, info = fut.result()
                except Exception as e:
                    status, info = "error", str(e)
                if status == "ok":
                    stats["new"] += 1
                    marks.pop(mid, None)          # a full sync brought it after all
                    # No total: the generator only discovers the mails as it
                    # goes. So only the running count is reported.
                    progress.melde(stats["new"], what="mails")
                elif status == "expired":
                    expired = True
                    STOP.set()
                elif status == "verdict":
                    # Refused or gone: recorded, said once, no error – the
                    # folder's link advances past it.
                    kind, mid, rel, text = info
                    marks[mid] = export_util.permanent_mark(
                        kind, text, name=rel.rsplit("/", 1)[-1], rel=rel)
                    stats[kind] = stats.get(kind, 0) + 1
                    export_util.permanent_event(kind, rel, text)
                elif status == "error":
                    # The folder's link must not advance past this mail.
                    stats["mail_errors"] = stats.get("mail_errors", 0) + 1
                    if bestand is not None:
                        bestand.download_fehlgeschlagen(rel)
                    progress.event("run.mail_skipped", "warn", detail=str(info))
                # "stopped" -> ignore
            if not expired:
                fill()

    db.permanent_abgleichen(marks_before, marks)
    return "expired" if expired else "done"


# ---------------------------------------------------------------------------
# Calendars (.ics) and contacts (.vcf)
# ---------------------------------------------------------------------------
_WD = {"monday": "MO", "tuesday": "TU", "wednesday": "WE", "thursday": "TH",
       "friday": "FR", "saturday": "SA", "sunday": "SU"}
_IDX = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}


def _plain_text(body):
    body = body or {}
    c = body.get("content", "") or ""
    if (body.get("contentType") or "").lower() == "html":
        c = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", c)
        c = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</tr>", " ", c)
        c = re.sub(r"<[^>]+>", " ", c)
        c = html.unescape(c)
    return " ".join(c.split())


def _esc(s):
    s = (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def _cn(name):
    return '"' + " ".join((name or "").split()).replace('"', "'") + '"'


def _fold(line):
    """Fold iCal/vCard lines to <=75 octets (CRLF + space)."""
    out, cur = "", 0
    for ch in line:
        w = len(ch.encode("utf-8"))
        if cur + w > 73:
            out += "\r\n "
            cur = 1
        out += ch
        cur += w
    return out


def _graph_dt(s):
    if not s:
        return None
    s = s.strip().replace("Z", "")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


def _ics_dt(node, all_day):
    dt = _graph_dt((node or {}).get("dateTime") or "")
    if dt is None:
        return None
    return dt.strftime("%Y%m%d") if all_day else dt.strftime("%Y%m%dT%H%M%SZ")


def _stamp(node, all_day):
    dt = _graph_dt((node or {}).get("dateTime") or "")
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d") if all_day else dt.strftime("%Y-%m-%d_%H%M")


def build_rrule(recurrence, all_day):
    if not recurrence:
        return None
    try:
        pat = recurrence.get("pattern") or {}
        rng = recurrence.get("range") or {}
        ptype = pat.get("type", "")
        interval = int(pat.get("interval", 1) or 1)
        days = [_WD[d.lower()] for d in (pat.get("daysOfWeek") or []) if d.lower() in _WD]
        idx = _IDX.get(pat.get("index", "first"), 1)
        parts = []
        if ptype == "daily":
            parts.append("FREQ=DAILY")
        elif ptype == "weekly":
            parts.append("FREQ=WEEKLY")
            if days:
                parts.append("BYDAY=" + ",".join(days))
        elif ptype == "absoluteMonthly":
            parts.append("FREQ=MONTHLY")
            if pat.get("dayOfMonth"):
                parts.append(f"BYMONTHDAY={pat['dayOfMonth']}")
        elif ptype == "relativeMonthly":
            parts.append("FREQ=MONTHLY")
            if days:
                parts.append("BYDAY=" + ",".join(f"{idx}{d}" for d in days))
        elif ptype == "absoluteYearly":
            parts.append("FREQ=YEARLY")
            if pat.get("month"):
                parts.append(f"BYMONTH={pat['month']}")
            if pat.get("dayOfMonth"):
                parts.append(f"BYMONTHDAY={pat['dayOfMonth']}")
        elif ptype == "relativeYearly":
            parts.append("FREQ=YEARLY")
            if pat.get("month"):
                parts.append(f"BYMONTH={pat['month']}")
            if days:
                parts.append("BYDAY=" + ",".join(f"{idx}{d}" for d in days))
        else:
            return None
        if interval != 1:
            parts.append(f"INTERVAL={interval}")
        rtype = rng.get("type", "")
        if rtype == "endDate" and rng.get("endDate"):
            d = rng["endDate"].replace("-", "")
            parts.append("UNTIL=" + (d if all_day else d + "T235959Z"))
        elif rtype == "numbered" and rng.get("numberOfOccurrences"):
            parts.append(f"COUNT={int(rng['numberOfOccurrences'])}")
        return ";".join(parts)
    except Exception:
        return None


def event_filename(ev):
    all_day = bool(ev.get("isAllDay"))
    stamp = _stamp(ev.get("start"), all_day)
    subj = (ev.get("subject") or "").strip() or "(kein Betreff)"
    prefix = (stamp + "__") if stamp else ""
    return f"{prefix}{safe(subj, 90)}__{short_id(ev.get('id') or ev.get('iCalUId') or subj)}.ics"


def build_ics(ev):
    all_day = bool(ev.get("isAllDay"))
    uid = ev.get("iCalUId") or ev.get("id") or short_id(ev.get("subject") or "")
    stamp = _graph_dt(ev.get("lastModifiedDateTime") or ev.get("createdDateTime") or "")
    dtstamp = (stamp or datetime.now(UTC).replace(tzinfo=None)).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//outlook_export//Graph//DE",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "BEGIN:VEVENT",
         f"UID:{_esc(uid)}", f"DTSTAMP:{dtstamp}"]
    start, end = _ics_dt(ev.get("start"), all_day), _ics_dt(ev.get("end"), all_day)
    if start:
        L.append(("DTSTART;VALUE=DATE:" if all_day else "DTSTART:") + start)
    if end:
        L.append(("DTEND;VALUE=DATE:" if all_day else "DTEND:") + end)
    L.append("SUMMARY:" + _esc(ev.get("subject") or "(kein Betreff)"))
    loc = (ev.get("location") or {}).get("displayName")
    if loc:
        L.append("LOCATION:" + _esc(loc))
    desc = _plain_text(ev.get("body"))
    if desc:
        L.append("DESCRIPTION:" + _esc(desc))
    org = (ev.get("organizer") or {}).get("emailAddress") or {}
    if org.get("address"):
        L.append(f'ORGANIZER;CN={_cn(org.get("name") or org["address"])}:mailto:{org["address"]}')
    for a in ev.get("attendees") or []:
        em = a.get("emailAddress") or {}
        if em.get("address"):
            L.append(f'ATTENDEE;CN={_cn(em.get("name") or em["address"])}:mailto:{em["address"]}')
    show = ev.get("showAs", "")
    if ev.get("isCancelled"):
        L.append("STATUS:CANCELLED")
    elif show == "tentative":
        L.append("STATUS:TENTATIVE")
    else:
        L.append("STATUS:CONFIRMED")
    L.append("TRANSP:" + ("TRANSPARENT" if show == "free" else "OPAQUE"))
    rr = build_rrule(ev.get("recurrence"), all_day)
    if rr:
        L.append("RRULE:" + rr)
    L += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(x) for x in L) + "\r\n"


def ics_stempel(pfad):
    """The DTSTAMP the export wrote into a stored .ics – or None."""
    try:
        with open(pfad, encoding="utf-8", errors="replace") as f:
            for zeile in f:
                if zeile.startswith("DTSTAMP:"):
                    return zeile[8:].strip()
    except OSError:
        pass
    return None


def _ics_stempel(lm):
    """A Graph timestamp the way build_ics writes DTSTAMP."""
    dt = _graph_dt(lm)
    return dt.strftime("%Y%m%dT%H%M%SZ") if dt else None


def _verschiebe_monate(dt, monate):
    """The same clock time this many months away; the day clamped to the
    target month (the 31st of a month that has 30 days)."""
    m = dt.month - 1 + monate
    jahr, monat = dt.year + m // 12, m % 12 + 1
    return dt.replace(year=jahr, month=monat, day=min(dt.day, monthrange(jahr, monat)[1]))


def kalender_fenster(monate, jetzt=None):
    """(start, end) of the calendar view: this many months back – from the
    start of that day – and ten years ahead. 0 months: no start, meaning
    everything."""
    jetzt = jetzt or datetime.now(UTC)
    heute = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    von = _verschiebe_monate(heute, -monate) if monate else None
    return von, _verschiebe_monate(heute, 12 * YEARS_AHEAD)


def _graph_zeit(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


EVENT_SELECT = ("id,iCalUId,subject,start,end,isAllDay,location,organizer,attendees,"
                "body,showAs,isCancelled,recurrence,seriesMasterId,type,"
                "createdDateTime,lastModifiedDateTime")


def hole_termine(graph, ids):
    """Series masters by id, as JSON batches: {id: (status, event)} – a 404
    means the series is gone meanwhile."""
    urls = {f"{GRAPH}/me/events/{eid}?$select={EVENT_SELECT}": eid for eid in ids}
    antworten = graph.batch_get(list(urls), extra_headers={"Prefer": UTC_PREF})
    return {eid: antworten.get(url) or (0, None) for url, eid in urls.items()}


def schreibe_termin(out, done, stats, stempel, cname, ev, lm):
    """Write or rewrite one .ics. Returns 1 on a write error, else 0."""
    rel = f"kalender/{cname}/{event_filename(ev)}"
    # Events without a Graph ID: the file path as a stable fallback key,
    # else None lands in the log and resume never kicks in.
    key = ev.get("id") or ev.get("iCalUId") or rel
    neu = not done.is_done(out, key)
    (out / "kalender" / cname).mkdir(parents=True, exist_ok=True)
    try:
        versions.write_text(out / rel, build_ics(ev))
    except Exception as e:
        progress.event("run.event_skipped", "warn", detail=str(e))
        return 1
    _alte_datei_weg(out, done.done.get(key), rel)
    done.mark(key, rel)
    stempel.merke(key, lm)
    if neu:
        stats["new"] += 1
    else:
        stats["updated"] = stats.get("updated", 0) + 1
    return 0


def kalender_runde(graph, out, done, stats, stempel, cname, key, url, params, monate):
    """One calendar: the view's round, then the series masters that are
    new or changed. Returns the number of errors."""
    db = stempel.db
    familien, entfernt = {}, []      # series master -> newest date's stamp
    fehler = seen = 0
    link = None
    try:
        seiten, _voll = delta_runde(graph, db, key, url, params,
                                    prefer=(UTC_PREF,), name=cname)
        for eintraege, ende in seiten:
            link = ende or link      # the link comes with the last page
            for ev in eintraege:
                if "@removed" in ev:
                    entfernt.append(ev.get("id"))
                    continue
                seen += 1
                if seen % 100 == 0:
                    # Heartbeat: large calendars page for minutes with no
                    # other line – the bar must show life.
                    progress.melde(seen, what="events")
                master = ev.get("seriesMasterId")
                if master and ev.get("type") in ("occurrence", "exception"):
                    # The view expands a series into its dates; the file is
                    # the series itself, and its newest date speaks for it.
                    lm = ev.get("lastModifiedDateTime") or ""
                    if lm > familien.get(master, ""):
                        familien[master] = lm
                    continue
                rel = f"kalender/{cname}/{event_filename(ev)}"
                ekey = ev.get("id") or ev.get("iCalUId") or rel
                lm = ev.get("lastModifiedDateTime") or ""
                if veraendert(out, done, stempel, ekey, lm, ics_stempel):
                    fehler += schreibe_termin(out, done, stats, stempel, cname, ev, lm)
                else:
                    stats["skipped"] += 1
        offen = []
        for master, lm in familien.items():
            if veraendert(out, done, stempel, master, lm, ics_stempel):
                offen.append(master)
            else:
                stats["skipped"] += 1
        for i in range(0, len(offen), 100):
            for master, (status, ev) in hole_termine(graph, offen[i:i + 100]).items():
                if 200 <= status < 300 and isinstance(ev, dict):
                    fehler += schreibe_termin(out, done, stats, stempel, cname, ev,
                                              familien[master])
                elif status != 404:              # 404: gone meanwhile, nothing to write
                    progress.event("run.event_skipped", "warn",
                                   detail=f"HTTP {status}: {master[:16]}…")
                    fehler += 1
    except TokenExpired:
        raise
    except Exception as e:
        progress.event("run.folder_incomplete", "err", name=cname, error=str(e))
        stempel.schreibe()
        return 1
    for eid in entfernt:
        # An exported event or series Graph no longer lists keeps its file,
        # as it always has – only the record goes, so a return is measured
        # afresh. Removed dates of a living series never had a file.
        if eid in done.done:
            stempel.vergiss(eid)
    stempel.schreibe()
    if link and not fehler:
        db.kv_schreiben(key, link)
        db.kv_schreiben(f"{key}:window", str(monate))
    if seen:
        progress.event("run.scanned", n=seen,
                       unit=progress.atom("progress.unit.events"))
    return fehler


def export_calendar(graph, out, done, stats, cals):
    """Every selected calendar as a window of change tracking: this many
    months back plus everything ahead. Returns the number of errors –
    zero means the category ran clean."""
    if not cals:
        return 0
    progress.event("run.section", name=progress.atom("export.cat.calendar"))
    db = state_db.StateDb(out)
    stempel = Stempel(db, "events")
    monate, voll = calendar_months_back(), calendar_full()
    von, bis = kalender_fenster(monate)
    params = {"startDateTime": _graph_zeit(von) if von and not voll else EPOCH,
              "endDateTime": _graph_zeit(bis)}
    fehler = 0
    for cal in cals:
        cname = safe(cal.get("name") or "Kalender")
        key = f"delta:cal:{cal.get('id') or cname}"
        if voll or db.kv_lesen(f"{key}:window") != str(monate):
            db.kv_schreiben(key, None)      # another window, or a full read asked for
        if von is not None and not voll:
            progress.event("run.calendar.window", name=cal.get("name") or cname,
                           **{"from": von.strftime("%Y-%m-%d")})
        url = (f"{GRAPH}/me/calendars/{cal['id']}/calendarView/delta" if cal.get("id")
               else f"{GRAPH}/me/calendarView/delta")
        progress.event("run.folder_plain", name=cname)
        fehler += kalender_runde(graph, out, done, stats, stempel, cname, key, url,
                                 params, monate)
    return fehler


def contact_filename(c):
    nm = (c.get("displayName")
          or " ".join(x for x in [c.get("givenName"), c.get("surname")] if x)).strip() or "Kontakt"
    return f"{safe(nm, 90)}__{short_id(c.get('id') or nm)}.vcf"


def build_vcf(c):
    given, sur, mid = c.get("givenName") or "", c.get("surname") or "", c.get("middleName") or ""
    fn = c.get("displayName") or " ".join(x for x in [given, sur] if x).strip() or "(ohne Namen)"
    L = ["BEGIN:VCARD", "VERSION:3.0",
         f"N:{_esc(sur)};{_esc(given)};{_esc(mid)};;", "FN:" + _esc(fn)]
    org, dept = c.get("companyName") or "", c.get("department") or ""
    if org or dept:
        L.append("ORG:" + _esc(org) + (";" + _esc(dept) if dept else ""))
    if c.get("jobTitle"):
        L.append("TITLE:" + _esc(c["jobTitle"]))
    for e in c.get("emailAddresses") or []:
        if e.get("address"):
            L.append("EMAIL;TYPE=INTERNET:" + _esc(e["address"]))
    for p in c.get("businessPhones") or []:
        if p:
            L.append("TEL;TYPE=WORK,VOICE:" + _esc(p))
    for p in c.get("homePhones") or []:
        if p:
            L.append("TEL;TYPE=HOME,VOICE:" + _esc(p))
    if c.get("mobilePhone"):
        L.append("TEL;TYPE=CELL,VOICE:" + _esc(c["mobilePhone"]))
    if c.get("personalNotes"):
        L.append("NOTE:" + _esc(c["personalNotes"]))
    if c.get("id"):
        L.append("UID:" + _esc(c["id"]))
    L.append("END:VCARD")
    return "\r\n".join(_fold(x) for x in L) + "\r\n"


CONTACT_SELECT = ("id,displayName,givenName,surname,middleName,companyName,department,"
                  "jobTitle,emailAddresses,businessPhones,homePhones,mobilePhone,"
                  "personalNotes,lastModifiedDateTime")


def _kontakt_seiten(graph, db, key, url, ersatz, name):
    """The source's round – or, should Graph refuse change tracking on the
    default folder, its plain listing as one page without a link."""
    try:
        return delta_runde(graph, db, key, url, {"$select": CONTACT_SELECT}, name=name)
    except TokenExpired:
        raise
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if ersatz is None or status not in (400, 404):
            raise
    alle = list(graph.paged(ersatz, {"$top": PAGE, "$select": CONTACT_SELECT}))
    return iter([(alle, None)]), True


def export_contacts(graph, out, done, stats):
    """The default folder and every contact folder, each a round of change
    tracking. Returns the number of errors – zero means the category ran
    clean."""
    progress.event("run.section", name=progress.atom("export.cat.contacts"))
    db = state_db.StateDb(out)
    stempel = Stempel(db, "contacts")
    quellen = [("", "delta:contacts", f"{GRAPH}/me/contacts/delta", f"{GRAPH}/me/contacts")]
    fehler_gesamt = 0
    try:
        ordner = list(graph.paged(f"{GRAPH}/me/contactFolders", {"$top": PAGE}))
    except TokenExpired:
        raise
    except Exception as e:
        # The folders stay unknown this run: not a clean run, or a cadence
        # would skip them for its whole interval.
        progress.event("run.unreadable", "warn",
                       name=progress.atom("export.cat.contacts"), error=str(e))
        ordner, fehler_gesamt = [], 1
    for f in ordner:
        quellen.append((safe(f.get("displayName") or "Ordner"), f"delta:contacts:{f['id']}",
                        f"{GRAPH}/me/contactFolders/{f['id']}/contacts/delta", None))
    for sub, key, url, ersatz in quellen:
        rel_dir = "kontakte" + (f"/{sub}" if sub else "")
        seen = fehler = 0
        link = None
        try:
            seiten, _voll = _kontakt_seiten(graph, db, key, url, ersatz, rel_dir)
            for eintraege, ende in seiten:
                link = ende or link      # the link comes with the last page
                for c in eintraege:
                    if "@removed" in c:
                        # A gone contact keeps its file, as it always has –
                        # only the record goes.
                        if c.get("id") in done.done:
                            stempel.vergiss(c["id"])
                        continue
                    if seen and seen % 100 == 0:
                        progress.melde(seen, what="contacts")
                    seen += 1
                    rel = f"{rel_dir}/{contact_filename(c)}"
                    # Contacts without a Graph ID: the file path as a stable
                    # fallback key (else key None in the log and a re-export
                    # on every run).
                    ckey = c.get("id") or rel
                    lm = c.get("lastModifiedDateTime") or ""
                    if not veraendert(out, done, stempel, ckey, lm):
                        stats["skipped"] += 1
                        continue
                    neu = not done.is_done(out, ckey)
                    (out / rel_dir).mkdir(parents=True, exist_ok=True)
                    try:
                        versions.write_text(out / rel, build_vcf(c))
                    except Exception as e:
                        progress.event("run.contact_skipped", "warn", detail=str(e))
                        fehler += 1
                        continue
                    _alte_datei_weg(out, done.done.get(ckey), rel)
                    done.mark(ckey, rel)
                    stempel.merke(ckey, lm)
                    if neu:
                        stats["new"] += 1
                    else:
                        stats["updated"] = stats.get("updated", 0) + 1
        except TokenExpired:
            raise
        except Exception as e:
            progress.event("run.folder_incomplete", "err", name=rel_dir, error=str(e))
            stempel.schreibe()
            fehler_gesamt += 1
            continue
        stempel.schreibe()
        if link and not fehler:
            db.kv_schreiben(key, link)
        fehler_gesamt += fehler
        if seen:
            progress.event("run.scanned_in", name=rel_dir, n=seen,
                           unit=progress.atom("progress.unit.contacts"))
    return fehler_gesamt


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def pruefe_verschwundene(graph, out, done, bestand, listing=True):
    """What has vanished from the mailbox since the last run.

    Two signals. A folder read in full is compared against the done log –
    `listing` says whether that comparison is allowed: only after a clean
    pass, since after an incompletely listed folder we would not know
    whether something is missing or we just did not look – and every
    suspicion is confirmed with Graph. A folder read from its link names
    its removals itself; those need no confirmation. Both go through the
    same healing first: what shows up elsewhere under the same Message-ID
    was moved, not deleted.
    """
    db = state_db.StateDb(out)
    bekannt = db.verschwunden_lesen()
    # First clean up what was recorded wrongly under the old assumption.
    bekannt, geheilt = zuruecknehmen(out, bekannt, bestand)
    if geheilt:
        db.verschwunden_ersetzen(bekannt)
        progress.event("run.gone.healed", n=geheilt)

    vermutet = ([k for k in verdaechtige(done, bestand) if bestand.sauber(k[1])]
                if listing else [])
    gemeldet = bestand.gemeldet(done)
    if not vermutet and not gemeldet:
        return {"gone_healed": geheilt} if geheilt else {}
    progress.event("run.gone.checking", n=len(vermutet) + len(gemeldet))
    # Without a single request: whatever sits elsewhere in the mailbox under
    # the same Message-ID was moved, not deleted.
    vermutet, verschoben = verschoben_statt_weg(out, vermutet, bestand)
    gemeldet, verschoben_gemeldet = verschoben_statt_weg(out, gemeldet, bestand)
    weg, bestaetigt = wirklich_weg(graph, vermutet)
    weg = list(dict.fromkeys(weg + [rel for _mid, rel in gemeldet]))
    verschoben += verschoben_gemeldet + bestaetigt
    neue = [rel for rel in weg if rel not in bekannt]
    if neue:
        db.verschwunden_ergaenzen(
            neue, datetime.now(UTC).isoformat(timespec="seconds"))
    progress.event("run.gone.result", gone=len(weg), new=len(neue),
                   moved=verschoben)
    return {"gone_new": len(neue), "gone_total": len(bekannt) + len(neue),
            "moved": verschoben, "gone_healed": geheilt}


_hilfe_gewuenscht = export_util.hilfe_gewuenscht


# ---------------------------------------------------------------------------
# Folder structure: its own step, its own result
#
# Listing the tree takes two minutes for over 400 folders, and it rarely
# changes. Separate means: sync once, after that the export reads it from
# disk.
# ---------------------------------------------------------------------------
def baum_eintraege(graph):
    """The tree as a flat list: path, ID, name, item count."""
    eintraege = []
    for top in build_tree(graph):
        for folder, rel_path in top["subtree"]:
            eintraege.append({
                "id": folder.get("id") or rel_path,
                "pfad": rel_path,
                "name": folder.get("displayName") or rel_path.rsplit("/", 1)[-1],
                "elemente": int(folder.get("totalItemCount") or 0),
            })
    eintraege.sort(key=lambda e: e["pfad"].lower())
    return eintraege


def gleiche_ordner_ab(argv):
    """--folders: only fetch and store the structure, export nothing."""
    out = export_util.ausgabeordner(argv)
    graph = auth.waehle_zugang(TokenClient, graph_login)
    vorher = folders.lade(out)
    daten = folders.speichere(out, baum_eintraege(graph), vorher)
    regeln = aktuelle_regeln()
    z = folders.zusammenfassung(daten, regeln)
    progress.event("run.sync.result", total=z["ordner_gesamt"],
                   chosen=z["ordner_gewaehlt"],
                   unit=progress.atom("progress.unit.folders"))
    if daten["neu"] or daten["verschwunden"] or daten["umbenannt"]:
        progress.event("run.sync.changed", new=len(daten["neu"]),
                       gone=len(daten["verschwunden"]),
                       renamed=len(daten["umbenannt"]))
    progress.ergebnis(len(daten["neu"]),
                      extra={"total": z["ordner_gesamt"],
                             "gone": len(daten["verschwunden"]),
                             "renamed": len(daten["umbenannt"])})


def auswahl_aus_puffer(daten, regeln):
    """Build the selection from the stored tree – as the export expects it.

    A single entry with all chosen folders: the export needs no grouping by
    top level.
    """
    gewaehlt = folders.gewaehlt(daten, regeln)
    if not gewaehlt:
        return []
    return [{"subtree": [({"id": e["id"], "totalItemCount": e.get("elemente")},
                          e["pfad"]) for e in gewaehlt]}]


def waehle_ordner(graph, out):
    """Which folders get exported – from the cache, otherwise fresh.

    The cache is the normal case: nobody wants to pay two minutes for over
    400 folders on every run. If it is missing it is created once; after
    that "sync folder structure" decides when it renews.
    """
    regeln = aktuelle_regeln()
    daten = folders.lade(out)
    if daten:
        z = folders.zusammenfassung(daten, regeln)
        progress.event("run.selection", chosen=z["ordner_gewaehlt"],
                       total=z["ordner_gesamt"],
                       unit=progress.atom("progress.unit.folders"))
        if z["neu"]:
            progress.event("run.selection.new", n=len(z["neu"]))
        auswahl = auswahl_aus_puffer(daten, regeln)
        if auswahl:
            return auswahl
        progress.event("run.selection.empty", "warn")
        return []
    progress.event("run.folders.initial")
    daten = folders.speichere(out, baum_eintraege(graph))
    return auswahl_aus_puffer(daten, regeln)


def aktuelle_regeln():
    """The selection rules – environment beats file beats the old name list.

    Anyone with a maintained SKIP_FOLDERS should not have to retype their
    selection.
    """
    roh = os.environ.get("FOLDER_RULES")
    if roh is None:
        roh = settings.value("folder_rules", None)
    if roh:
        return folders.lies_regeln(roh)
    return folders.aus_namensliste(DEFAULT_SKIP_FOLDERS)


def nur_pruefen(argv):
    """--check: the completeness balance of every ticked category – mail,
    calendar, contacts – against Microsoft's state of now. Exports
    nothing, writes nothing but the reports (completeness.py)."""
    out = export_util.ausgabeordner(argv)
    graph = auth.waehle_zugang(TokenClient, graph_login)
    db = state_db.StateDb(out)
    done = DoneLog(db)
    kategorien = selected_categories()
    berichte = []
    try:
        if "mail" in kategorien:
            berichte.append(pruefe_mails(graph, out, done, db.verschwunden_lesen()))
        if "calendar" in kategorien:
            berichte.append(pruefe_kalender(graph, out, done, db))
        if "contacts" in kategorien:
            berichte.append(pruefe_kontakte(graph, out, done, db))
    finally:
        done.close()
    for b in berichte:
        completeness.schreiben(db, b)
    completeness.melden(*berichte)


# ---------------------------------------------------------------------------
# Completeness: what Graph has against what the export knows it fetched
#
# The mail check costs one folder listing – Graph hands out totalItemCount
# with the folders anyway. "Here" is the resume log, not a count of files:
# a stray file is not an exported mail. Tombstoned mails still lie here
# and are not a gap; nor is a folder the rules leave out, nor a mail from
# before the start day – those are counted as deliberately excluded, the
# start day only in folders where a gap would otherwise remain (one count
# request each).
# ---------------------------------------------------------------------------


def _ordner_von(rel):
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _vor_stichtag(graph, folder, seit):
    """How many mails of the folder Graph dates before the start day – one
    count request; an answer it refuses counts as none."""
    try:
        daten = graph.get(f"{GRAPH}/me/mailFolders/{folder['id']}/messages",
                          {"$filter": f"receivedDateTime lt {seit}T00:00:00Z",
                           "$count": "true", "$top": 1})
        return int(daten.get("@odata.count") or 0)
    except TokenExpired:
        raise
    except Exception:
        return 0


def _alt_auf_platte(out, rel_path, seit):
    """Mails from before the start day that already lie here – their file
    name starts with the received date."""
    try:
        return sum(1 for p in (Path(out) / rel_path).glob("*.eml")
                   if p.name[:10] < seit)
    except OSError:
        return 0


def pruefe_mails(graph, out, done, weg=None):
    """The mail balance, per folder the rules take. A mail Microsoft
    refuses is listed by the folder but will never come: its own number,
    not open. One that was gone before it came is no longer listed."""
    weg = weg or {}
    regeln = aktuelle_regeln()
    seit = outlook_since()
    da_je, weg_je, verweigert_je = Counter(), Counter(), Counter()
    for rel in done.done.values():
        if (Path(out) / rel).exists():
            da_je[_ordner_von(rel)] += 1
    for rel in weg:
        weg_je[_ordner_von(rel)] += 1
    for mark in state_db.StateDb(out).permanent_lesen().values():
        if mark.get("kind") == export_util.REFUSED and mark.get("rel"):
            verweigert_je[_ordner_von(mark["rel"])] += 1
    da = offen = ausgeschlossen = verweigert = 0
    zeilen = []
    for top in build_tree(graph):
        for folder, rel_path in top["subtree"]:
            erwartet = folder.get("totalItemCount")
            if erwartet is None:
                continue
            erwartet = int(erwartet)
            if not folders.gilt(rel_path, regeln):
                ausgeschlossen += erwartet
                continue
            # Tombstoned mails still lie here; what counts against the
            # folder's items is the rest.
            lebend = max(0, da_je[rel_path] - weg_je[rel_path])
            z_verweigert = min(verweigert_je[rel_path], max(0, erwartet - lebend))
            erwartet -= z_verweigert
            verweigert += z_verweigert
            if seit and erwartet > lebend:
                alt = _vor_stichtag(graph, folder, seit) - _alt_auf_platte(out, rel_path, seit)
                alt = max(0, min(erwartet - lebend, alt))
                ausgeschlossen += alt
                erwartet -= alt
            z_da = min(lebend, erwartet)
            zeilen.append(completeness.zeile(rel_path, z_da, erwartet - z_da))
            da += z_da
            offen += erwartet - z_da
    return completeness.bilanz("outlook_mail", "mails", da=da, offen=offen,
                               ausgeschlossen=ausgeschlossen, behalten=len(weg),
                               verweigert=verweigert, zeilen=zeilen)


def _aktuell(out, done, stempel, key, lm):
    """Exported, and the stored change stamp matches – or, for an archive
    from before change tracking, no stamp at all to contradict it."""
    if not done.is_done(out, key):
        return False
    alt = stempel.bekannt(key)
    return alt is None or alt == (lm or "")


VIEW_TAGE = 1800            # calendarView allows 1825 days per request


def _fenster_teile(von, bis, tage=VIEW_TAGE):
    """The window in pieces the view accepts: [start, end) pairs of at
    most `tage` days, back to back, from `von` (the epoch when the
    window has no start) to `bis`."""
    a = von or datetime(1970, 1, 1, tzinfo=UTC)
    teile = []
    while a < bis:
        b = min(a + timedelta(days=tage), bis)
        teile.append((a, b))
        a = b
    return teile


def pruefe_kalender(graph, out, done, db):
    """The calendar balance: the events of the window per chosen calendar,
    a series counted once by its master. Excluded are whole calendars.
    The view takes at most five years a request, so the window is asked
    in pieces; an event that straddles a cut is counted once."""
    cals = waehle_kalender(graph, out)
    alle = len((folders.lade(out, folders.KALENDER) or {}).get("ordner", []))
    stempel = Stempel(db, "events")
    von, bis = kalender_fenster(calendar_months_back())
    # No $select: the view refuses one that names lastModifiedDateTime,
    # and that stamp is what the balance is judged by.
    fenster = [{"startDateTime": _graph_zeit(a), "endDateTime": _graph_zeit(b), "$top": 100}
               for a, b in _fenster_teile(von, bis)]
    gesehen = set()
    da = offen = 0
    zeilen, fehler = [], []
    offene = _Offene(done, "event")
    for cal in cals:
        cname = safe(cal.get("name") or "Kalender")
        pfad = f"kalender/{cname}"
        url = (f"{GRAPH}/me/calendars/{cal['id']}/calendarView" if cal.get("id")
               else f"{GRAPH}/me/calendarView")
        try:
            eintraege = [ev for params in fenster
                         for ev in graph.paged(url, params, {"Prefer": UTC_PREF})]
        except TokenExpired:
            raise
        except Exception as e:
            progress.event("run.folder_incomplete", "err", name=pfad,
                           error=export_util.fehlertext(e))
            fehler.append(completeness.fehler(pfad, "run.folder_incomplete"))
            continue
        familien = {}
        z_da = z_offen = 0
        im_kalender = set()
        for ev in eintraege:
            master = ev.get("seriesMasterId")
            lm = ev.get("lastModifiedDateTime") or ""
            if master and ev.get("type") in ("occurrence", "exception"):
                if lm > familien.get(master, ""):
                    familien[master] = lm
                continue
            key = ev.get("id")
            if not key or key in im_kalender:
                continue                    # straddles a cut: counted once
            im_kalender.add(key)
            gesehen.add(key)
            if _aktuell(out, done, stempel, key, lm):
                z_da += 1
            else:
                z_offen += 1
                offene.merke(key, pfad)
        for master, lm in familien.items():
            gesehen.add(master)
            if _aktuell(out, done, stempel, master, lm):
                z_da += 1
            else:
                z_offen += 1
                offene.merke(master, pfad)
        zeilen.append(completeness.zeile(pfad, z_da, z_offen))
        da += z_da
        offen += z_offen
    behalten = sum(1 for key in stempel.alt
                   if key not in gesehen and done.is_done(out, key))
    return completeness.bilanz("outlook_calendar", "events", da=da, offen=offen,
                               ausgeschlossen=max(0, alle - len(cals)),
                               ausgeschlossen_einheit="calendars",
                               behalten=behalten, zeilen=zeilen, fehler=fehler,
                               extra=offene.extra())


class _Offene:
    """The open items of a balance by id – what "Fetch now" then fetches
    one by one, without a listing: the id, the file the resume log knows
    (empty for an item never exported), the row, and the kind the fetch
    tells the endpoint by. Capped, and the cap is said."""

    def __init__(self, done, art):
        self.done, self.art = done, art
        self.liste, self.gekappt = [], False

    def merke(self, key, pfad):
        if len(self.liste) < completeness.OFFENE_GRENZE:
            self.liste.append({"id": key, "rel": self.done.done.get(key) or "",
                               "pfad": pfad, "art": self.art})
        else:
            self.gekappt = True

    def extra(self):
        return {"offene": self.liste, "offene_gekappt": self.gekappt}


def pruefe_kontakte(graph, out, done, db):
    """The contact balance: the default folder and every contact folder.
    Nothing is excluded – there is no filter."""
    stempel = Stempel(db, "contacts")
    quellen = [("kontakte", f"{GRAPH}/me/contacts")]
    fehler = []
    try:
        ordner = list(graph.paged(f"{GRAPH}/me/contactFolders", {"$top": PAGE}))
    except TokenExpired:
        raise
    except Exception as e:
        progress.event("run.unreadable", "warn", name="kontakte",
                       error=export_util.fehlertext(e))
        fehler.append(completeness.fehler("kontakte", "run.unreadable"))
        ordner = []
    for f in ordner:
        quellen.append((f"kontakte/{safe(f.get('displayName') or 'Ordner')}",
                        f"{GRAPH}/me/contactFolders/{f['id']}/contacts"))
    gesehen = set()
    da = offen = 0
    zeilen = []
    offene = _Offene(done, "contact")
    for pfad, url in quellen:
        try:
            eintraege = list(graph.paged(url, {"$top": PAGE,
                                               "$select": "id,lastModifiedDateTime"}))
        except TokenExpired:
            raise
        except Exception as e:
            progress.event("run.unreadable", "warn", name=pfad,
                           error=export_util.fehlertext(e))
            fehler.append(completeness.fehler(pfad, "run.unreadable"))
            continue
        z_da = z_offen = 0
        for c in eintraege:
            key = c.get("id")
            if not key:
                continue
            gesehen.add(key)
            if _aktuell(out, done, stempel, key, c.get("lastModifiedDateTime")):
                z_da += 1
            else:
                z_offen += 1
                offene.merke(key, pfad)
        zeilen.append(completeness.zeile(pfad, z_da, z_offen))
        da += z_da
        offen += z_offen
    behalten = sum(1 for key in stempel.alt
                   if key not in gesehen and done.is_done(out, key))
    return completeness.bilanz("outlook_contacts", "contacts", da=da, offen=offen,
                               behalten=behalten, zeilen=zeilen, fehler=fehler,
                               extra=offene.extra())


def kategorie_faellig(db, kategorie):
    """The cadence gate of one category (mail, calendar, contacts): due, or
    said to be skipped. SYNC_NOW steps over every gate."""
    kadenz = export_util.kadenzen().get(f"outlook:{kategorie}") or "always"
    if export_util.einheit_faellig(db, kadenz, kv_key=f"last_sync:{kategorie}"):
        return True
    progress.event("run.cadence.skip", name=progress.atom(f"export.cat.{kategorie}"),
                   cadence=progress.atom(f"cadence.{kadenz}"))
    return False


def kategorie_erledigt(db, kategorie):
    """Mark a category's clean run – the cadence counts from here."""
    db.kv_schreiben(f"last_sync:{kategorie}", str(time.time()))


def faellige_ordner(db, auswahl):
    """The mail gate, per folder: (the due part of the selection, the
    paths left out).

    A folder's cadence is its own key, else the nearest parent's, else the
    category's (export_util.kadenz_fuer); its clock is kept by id, so a
    rename does not reset it. One line says what was left out: the
    category line when nothing is due, a count otherwise – never a line
    per folder.
    """
    kadenzen = export_util.kadenzen()
    faellig, ausgelassen = [], []
    for top in auswahl:
        subtree = []
        for folder, rel_path in top["subtree"]:
            kadenz = export_util.kadenz_fuer(kadenzen, "outlook:mail", rel_path)
            if export_util.einheit_faellig(db, kadenz,
                                           kv_key=f"last_sync:mail:{folder['id']}"):
                subtree.append((folder, rel_path))
            else:
                ausgelassen.append(rel_path)
        if subtree:
            faellig.append({**top, "subtree": subtree})
    if ausgelassen and not faellig:
        kadenz = kadenzen.get("outlook:mail") or "always"
        progress.event("run.cadence.skip", name=progress.atom("export.cat.mail"),
                       cadence=progress.atom(f"cadence.{kadenz}"))
    elif ausgelassen:
        progress.event("run.outlook.folders_paced", n=len(ausgelassen))
    return faellig, ausgelassen


def nachholen(graph, out, done, rels):
    """"Fetch again" and "Fetch now": exactly the items named – the files
    the archive check found missing, each by the id the resume log keeps,
    or the events and contacts the balance found open, by id ({id, rel,
    pfad, art} entries; a never exported one has no file yet and gets
    its name here) – mails as MIME, events and contacts rendered anew.
    Nothing is listed. A 404 says the item is gone at Microsoft: the
    entry stays, the log says so, and the check's row calls it a card to
    note. What came is taken off the stored balance. Returns the run's
    outcome."""
    kennung = {rel: key for key, rel in done.done.items()}
    progress.event("run.nachholen.start", n=len(rels))
    db = state_db.StateDb(out)
    stempel = {"events": Stempel(db, "events"), "contacts": Stempel(db, "contacts")}
    stats = {"new": 0, "updated": 0, "skipped": 0}
    geholt = weg = fehler = unbekannt = 0
    gekommen = {"event": [], "contact": []}   # by kind, for the balance
    for eintrag in rels:
        if isinstance(eintrag, dict):
            key, art, pfad = eintrag["id"], eintrag.get("art") or "", eintrag.get("pfad") or ""
            rel = eintrag.get("rel") or done.done.get(key) or ""
        else:
            rel, key, art, pfad = eintrag, kennung.get(eintrag), "", ""
        if not key:
            unbekannt += 1
            continue
        if not art:
            art = ("event" if rel.endswith(".ics") else
                   "contact" if rel.endswith(".vcf") else "mail")
        name = rel or key
        try:
            if art == "event":
                ev = graph.get(f"{GRAPH}/me/events/{key}?$select={EVENT_SELECT}",
                               extra_headers={"Prefer": UTC_PREF})
                teile = (pfad or rel).split("/")
                cname = teile[1] if len(teile) >= 2 and teile[0] == "kalender" else "Kalender"
                if schreibe_termin(out, done, stats, stempel["events"], cname, ev,
                                   ev.get("lastModifiedDateTime") or ""):
                    fehler += 1
                    continue
            elif art == "contact":
                c = graph.get(f"{GRAPH}/me/contacts/{key}")
                rel = rel or f"{pfad or 'kontakte'}/{contact_filename(c)}"
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                versions.write_text(out / rel, build_vcf(c))
                done.mark(key, rel)
                stempel["contacts"].merke(key, c.get("lastModifiedDateTime") or "")
            else:
                if not rel:
                    unbekannt += 1         # a mail has no name without its file
                    continue
                content, _ = graph.get_bytes(f"{GRAPH}/me/messages/{key}/$value",
                                             label=" (MIME)")
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                versions.write_bytes(out / rel, content)
                done.mark(key, rel)
            geholt += 1
            if art in gekommen:
                gekommen[art].append(key)
        except TokenExpired:
            raise
        except Exception as e:
            if export_util.http_status(e) == 404:
                weg += 1
                progress.event("run.nachholen.gone", "warn", name=name)
            else:
                fehler += 1
                progress.event("run.nachholen.failed", "warn", name=name,
                               error=f"{type(e).__name__}: {e}")
    for s in stempel.values():
        s.schreibe()
    for art, quelle in (("event", "outlook_calendar"), ("contact", "outlook_contacts")):
        if gekommen[art]:
            completeness.abgeholt(db, quelle, ids=gekommen[art])
    export_util.nachholen_melden(geholt, weg, fehler, unbekannt)
    return "done"


def exportiere(graph, out, done, stats, workers):
    """The regular run: mail, calendar, contacts – each behind its cadence
    gate, each marking its last clean run. Returns the run's outcome."""
    db_root = state_db.StateDb(out)
    categories = selected_categories()
    if export_util.voll_neu():
        progress.event("run.full_sync")
        db_root.permanent_leeren()      # every refused or gone mail is asked once more
    elif export_util.abgleich():
        progress.event("run.resync")
    selected_mail, ausgelassen, sel_cals = [], [], []
    # "Fetch now" from the balance names the folders with something open:
    # the resync then lists those alone – a calendar or the contacts only
    # when a row of theirs is open – and leaves the rest as it is.
    nur = export_util.abgleich_ordner() if export_util.abgleich() else None
    if nur is not None:
        progress.event("run.resync.folders", n=len(nur))
        nur_mail = {p for p in nur if p.startswith(f"{MAIL_DIR}/")}
        nur_kal = {p[len("kalender/"):] for p in nur if p.startswith("kalender/")}
        nur_kon = any(p.startswith("kontakte") for p in nur)
        categories = set(categories) - {k for k, da in
                                        (("mail", nur_mail), ("calendar", nur_kal),
                                         ("contacts", nur_kon)) if not da}
    if "mail" in categories:
        auswahl = waehle_ordner(graph, out)
        weggelassen = []
        if nur is not None:
            # The folders the list leaves out are not listed – and, like
            # the ones a cadence leaves out, excluded by name: a full round
            # of a parent must not turn their unlisted mails into suspects.
            weggelassen = [rel for top in auswahl for _f, rel in top["subtree"]
                           if rel not in nur_mail]
            auswahl = [{**top, "subtree": [(f, rel) for f, rel in top["subtree"] if rel in nur_mail]}
                       for top in auswahl]
            auswahl = [top for top in auswahl if top["subtree"]]
        selected_mail, ausgelassen = faellige_ordner(db_root, auswahl)
        ausgelassen = [*ausgelassen, *weggelassen]
    if "calendar" in categories and kategorie_faellig(db_root, "calendar"):
        sel_cals = waehle_kalender(graph, out)
        if nur is not None:
            sel_cals = [c for c in sel_cals if safe(c.get("name") or "Kalender") in nur_kal]
    want_con = "contacts" in categories and kategorie_faellig(db_root, "contacts")

    result = "done"
    if selected_mail:
        bestand = Bestand()
        bestand.ausgelassen.update(ausgelassen)     # not listed: no removals inferred
        result = run_export(graph, out, done, stats, selected_mail, workers, bestand)
        if result == "done":
            # Deletions first, links after: a round reports its removals
            # once, and only a stored link makes that round count as read.
            # The clean folders' cadence clocks advance with their links.
            stats.update(pruefe_verschwundene(
                graph, out, done, bestand, listing=not stats.get("folder_errors")))
            bestand.links_sichern(db_root)
    if result != "expired" and sel_cals:
        try:
            if not export_calendar(graph, out, done, stats, sel_cals):
                kategorie_erledigt(db_root, "calendar")
        except TokenExpired:
            result = "expired"
    if result != "expired" and want_con:
        try:
            if not export_contacts(graph, out, done, stats):
                kategorie_erledigt(db_root, "contacts")
        except TokenExpired:
            result = "expired"
    return result


def main():
    if _hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__.strip())
        return
    # For the special runs only the output folder counts; switches like
    # -default mean nothing here and must never pass as a folder.
    nur_ordner = [a for a in sys.argv[1:] if not a.startswith("-")]
    try:
        if "--check" in sys.argv[1:]:
            return nur_pruefen(nur_ordner)
        if "--folders" in sys.argv[1:]:
            return gleiche_ordner_ab(nur_ordner)
        if "--calendars" in sys.argv[1:]:
            return gleiche_kalender_ab(nur_ordner)
    except TokenExpired:
        # Structured ending – the app reacts to the event and shows its wizard.
        progress.fehler("token_expired")
        sys.exit(1)

    workers = settings.number("EXPORT_WORKERS", "workers")
    if workers > 4:
        progress.event("run.workers_hint", "warn", n=workers)
    graph_client.konfiguriere(workers)

    graph = auth.waehle_zugang(TokenClient, graph_login)

    out = export_util.ausgabeordner(sys.argv[1:])
    out.mkdir(parents=True, exist_ok=True)
    done = DoneLog(state_db.StateDb(out))
    stats = {"new": 0, "updated": 0, "skipped": 0, "folder_errors": 0}
    result = "done"

    nachzuholen = export_util.nachhol_eintraege()
    try:
        result = (nachholen(graph, out, done, nachzuholen) if nachzuholen is not None
                  else exportiere(graph, out, done, stats, workers))
    except TokenExpired:
        result = "expired"
    except (requests.exceptions.RequestException, RuntimeError) as e:
        # Network gone for good (all retries used up) – no traceback, the
        # progress in the done log is preserved.
        result = "network"
        progress.event("run.network_gone", "err",
                       error=f"{type(e).__name__}: {e}")
    except KeyboardInterrupt:
        result = "aborted"
    finally:
        done.close()

    if result == "expired":
        progress.fehler("token_expired")
        sys.exit(1)
    if result in ("network", "aborted"):
        progress.event("run.resume_hint", n=stats["new"])
        sys.exit(1)

    # No prose summary: the numbers travel in the result event, and the app
    # logs a translated line from it. The archive totals live in Analytics.
    progress.ergebnis(stats["new"], unchanged=stats["skipped"],
                      excluded=stats.get("excluded"),
                      errors=stats.get("folder_errors"),
                      extra={k: v for k, v in stats.items()
                             if k in ("updated", "gone_new", "moved", "gone_healed")})
    if stats.get("folder_errors"):
        progress.event("run.folders_failed", "warn", n=stats["folder_errors"])


if __name__ == "__main__":
    main()
