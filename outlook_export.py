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
"""

import os
import sys
import re
import html
import threading
from datetime import datetime, UTC
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED


import auth
import export_util
import folders
import state_db
import graph_client
import settings
import progress

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
    """What really lay in the mailbox during this run."""

    def __init__(self):
        self.gesehen = set()        # IDs from completely listed folders
        self.briefe = set()         # their internetMessageId (survives a move)
        self.vollstaendig = []      # their paths, with a trailing slash

    def ordner_fertig(self, rel_path):
        self.vollstaendig.append(rel_path.rstrip("/") + "/")

    def aus_gelistetem_ordner(self, rel):
        return any(rel.startswith(p) for p in self.vollstaendig)


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
    """Ask Graph about every suspicion. Returns (gone, moved).

    An error that is not a 404 (throttling, network) counts as "not gone":
    better to report a deletion later than a wrong one now.
    """
    weg, verschoben = [], 0
    for mid, rel in kandidaten[:grenze]:
        try:
            graph.get(f"{GRAPH}/me/messages/{mid}", {"$select": "id"})
            verschoben += 1
        except TokenExpired:
            raise
        except Exception as e:
            if "404" in str(e) or getattr(e, "status", None) == 404:
                weg.append(rel)
            # otherwise: unclear – claim nothing
    if len(kandidaten) > grenze:
        progress.event("run.gone.deferred", n=len(kandidaten) - grenze)
    return weg, verschoben




def iter_messages_to_export(graph, out, done, stats, selected, bestand=None):
    """Mirrors the folders onto the filesystem and yields (mid, rel) for
    every mail not yet exported. Listing runs in the main thread (lazily)."""
    # internetMessageId costs nothing extra and is the only key that survives
    # a move – see brief_kennung and pruefe_verschwundene.
    select = ("id,internetMessageId,subject,receivedDateTime,sentDateTime,"
              "from,hasAttachments")
    for top in selected:
        for folder, rel_path in top["subtree"]:
            (out / rel_path).mkdir(parents=True, exist_ok=True)
            total = folder.get("totalItemCount")
            if total is not None:
                progress.event("run.folder", name=rel_path, n=int(total))
            else:
                progress.event("run.folder_plain", name=rel_path)
            seen = 0
            try:
                for msg in graph.paged(f"{GRAPH}/me/mailFolders/{folder['id']}/messages",
                                       {"$top": PAGE, "$select": select}):
                    seen += 1
                    mid = msg["id"]
                    if bestand is not None:
                        bestand.gesehen.add(mid)
                        if msg.get("internetMessageId"):
                            bestand.briefe.add(msg["internetMessageId"].strip())
                    if done.is_done(out, mid):
                        stats["skipped"] += 1
                        continue
                    yield mid, f"{rel_path}/{mail_filename(msg)}"
            except TokenExpired:
                raise
            except Exception as e:
                # A permanently stuck folder must not kill the whole run:
                # skip the rest, on to the next one. What is already exported
                # sits in the done log – the next run fetches the rest.
                stats["folder_errors"] = stats.get("folder_errors", 0) + 1
                progress.event("run.folder_incomplete", "err", name=rel_path,
                               error=f"{type(e).__name__}: {e}")
                continue
            # Only a fully traversed folder is fit for comparison – after a
            # break above we never get here at all.
            if bestand is not None:
                bestand.ordner_fertig(rel_path)
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
        return ("error", f"{mid[:16]}…: {e}")
    try:
        (out / rel).write_bytes(content)
    except Exception as e:
        return ("error", f"{rel}: {e}")
    done.mark(mid, rel)
    return ("ok", rel)


def run_export(graph, out, done, stats, selected, workers, bestand=None):
    gen = iter_messages_to_export(graph, out, done, stats, selected, bestand)
    cap = max(workers * 8, workers)      # this many tasks in the pipeline at once
    pending = set()
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
                pending.add(ex.submit(download_one, graph, out, done, mid, rel))

        fill()
        while pending:
            finished, rest = wait(pending, return_when=FIRST_COMPLETED)
            pending = set(rest)
            for fut in finished:
                try:
                    status, info = fut.result()
                except Exception as e:
                    status, info = "error", str(e)
                if status == "ok":
                    stats["new"] += 1
                    # No total: the generator only discovers the mails as it
                    # goes. So only the running count is reported.
                    progress.melde(stats["new"], what="mails")
                elif status == "expired":
                    expired = True
                    STOP.set()
                elif status == "error":
                    progress.event("run.mail_skipped", "warn", detail=str(info))
                # "stopped" -> ignore
            if not expired:
                fill()

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


def export_calendar(graph, out, done, stats, cals):
    if not cals:
        return
    progress.event("run.section", name=progress.atom("export.cat.calendar"))
    pref = {"Prefer": 'outlook.timezone="UTC"'}      # times in UTC -> correct .ics
    select = ("id,iCalUId,subject,start,end,isAllDay,location,organizer,attendees,"
              "body,showAs,isCancelled,recurrence,seriesMasterId,type,"
              "createdDateTime,lastModifiedDateTime")
    for cal in cals:
        cname = safe(cal.get("name") or "Kalender")
        url = (f"{GRAPH}/me/calendars/{cal['id']}/events" if cal.get("id")
               else f"{GRAPH}/me/events")
        progress.event("run.folder_plain", name=cname)
        seen = 0
        try:
            for ev in graph.paged(url, {"$top": PAGE, "$select": select}, extra_headers=pref):
                seen += 1
                if seen % 100 == 0:
                    # Heartbeat: large calendars page for minutes with no
                    # other line – the bar must show life.
                    progress.melde(seen, what="events")
                rel = f"kalender/{cname}/{event_filename(ev)}"
                # Events without a Graph ID: the file path as a stable
                # fallback key, else None lands in the log and resume never
                # kicks in.
                key = ev.get("id") or ev.get("iCalUId") or rel
                if done.is_done(out, key):
                    stats["skipped"] += 1
                    continue
                (out / "kalender" / cname).mkdir(parents=True, exist_ok=True)
                try:
                    (out / rel).write_text(build_ics(ev), encoding="utf-8")
                except Exception as e:
                    progress.event("run.event_skipped", "warn", detail=str(e))
                    continue
                done.mark(key, rel)
                stats["new"] += 1
        except TokenExpired:
            raise
        except Exception as e:
            progress.event("run.folder_incomplete", "err", name=cname, error=str(e))
            continue
        if seen:
            progress.event("run.scanned", n=seen,
                           unit=progress.atom("progress.unit.events"))


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


def export_contacts(graph, out, done, stats):
    progress.event("run.section", name=progress.atom("export.cat.contacts"))
    sources = [("", f"{GRAPH}/me/contacts")]          # default contacts (no folder)
    try:
        folders = list(graph.paged(f"{GRAPH}/me/contactFolders", {"$top": PAGE}))
    except TokenExpired:
        raise
    except Exception as e:
        progress.event("run.unreadable", "warn",
                       name=progress.atom("export.cat.contacts"), error=str(e))
        folders = []
    for f in folders:
        sources.append((safe(f.get("displayName") or "Ordner"),
                        f"{GRAPH}/me/contactFolders/{f['id']}/contacts"))
    select = ("id,displayName,givenName,surname,middleName,companyName,department,"
              "jobTitle,emailAddresses,businessPhones,homePhones,mobilePhone,personalNotes")
    for sub, url in sources:
        rel_dir = "kontakte" + (f"/{sub}" if sub else "")
        seen = 0
        try:
            for c in graph.paged(url, {"$top": PAGE, "$select": select}):
                if seen and seen % 100 == 0:
                    progress.melde(seen, what="contacts")
                seen += 1
                rel = f"{rel_dir}/{contact_filename(c)}"
                # Contacts without a Graph ID: the file path as a stable
                # fallback key (else key None in the log and a re-export on
                # every run).
                key = c.get("id") or rel
                if done.is_done(out, key):
                    stats["skipped"] += 1
                    continue
                (out / rel_dir).mkdir(parents=True, exist_ok=True)
                try:
                    (out / rel).write_text(build_vcf(c), encoding="utf-8")
                except Exception as e:
                    progress.event("run.contact_skipped", "warn", detail=str(e))
                    continue
                done.mark(key, rel)
                stats["new"] += 1
        except TokenExpired:
            raise
        except Exception as e:
            progress.event("run.folder_incomplete", "err", name=rel_dir, error=str(e))
            continue
        if seen:
            progress.event("run.scanned_in", name=rel_dir, n=seen,
                           unit=progress.atom("progress.unit.contacts"))


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def pruefe_verschwundene(graph, out, done, bestand):
    """What has vanished from the mailbox since the last run.

    Runs only after a clean pass: after an abort or an incompletely listed
    folder we would not know whether something is missing or we just did not
    look. Better no statement at all than a wrong one.
    """
    db = state_db.StateDb(out)
    bekannt = db.verschwunden_lesen()
    # First clean up what was recorded wrongly under the old assumption.
    bekannt, geheilt = zuruecknehmen(out, bekannt, bestand)
    if geheilt:
        db.verschwunden_ersetzen(bekannt)
        progress.event("run.gone.healed", n=geheilt)

    kandidaten = verdaechtige(done, bestand)
    if not kandidaten:
        return {"gone_healed": geheilt} if geheilt else {}
    progress.event("run.gone.checking", n=len(kandidaten))
    # Without a single request: whatever sits elsewhere in the mailbox under
    # the same Message-ID was moved, not deleted.
    kandidaten, verschoben_lokal = verschoben_statt_weg(out, kandidaten, bestand)
    weg, verschoben = wirklich_weg(graph, kandidaten)
    verschoben += verschoben_lokal
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
    """--check: only report completeness, export nothing."""
    out = export_util.ausgabeordner(argv)
    graph = auth.waehle_zugang(TokenClient, graph_login)
    bericht = pruefe_vollstaendigkeit(
        graph, out, state_db.StateDb(out).verschwunden_lesen())
    # No prose: the check UI renders the report file, the result event
    # carries the counts.
    state_db.StateDb(out).bericht_schreiben(bericht)
    progress.ergebnis(0, excluded=bericht["ausgelassen"],
                      extra={"expected": bericht["erwartet"],
                             "present": bericht["vorhanden"],
                             "missing": bericht["fehlt"]})


# ---------------------------------------------------------------------------
# Completeness: what Graph counts against what lies on disk
#
# Graph delivers totalItemCount with the folder list anyway – so the
# comparison costs nothing extra. Alone it would only be an indicator:
# deleted mails create a difference that is no gap. Only together with the
# tombstones does it become a balance sheet in which every number is
# explained.
# ---------------------------------------------------------------------------


def zaehle_dateien(ordner):
    try:
        return sum(1 for p in Path(ordner).glob("*.eml") if p.is_file())
    except OSError:
        return 0


def _is_default_skip(top):
    """Is the top-level folder one of the default exclusions (archive, junk …)?"""
    name = (top["folder"].get("displayName") or "").strip().lower()
    return name in DEFAULT_SKIP_FOLDERS


def pruefe_vollstaendigkeit(graph, out, weg=None):
    """Per mailbox folder: expected, present, deleted, difference.

    `weg` holds the paths recorded as vanished – they explain why less lies
    on disk than Graph counts.
    """
    weg = weg or {}
    weg_je_ordner = {}
    for rel in weg:
        ordner = rel.rsplit("/", 1)[0] if "/" in rel else ""
        weg_je_ordner[ordner] = weg_je_ordner.get(ordner, 0) + 1

    zeilen = []
    for top in build_tree(graph):
        # Folders the selection leaves out (archive, deleted items, junk …)
        # are not incomplete – they are intentionally empty. Reporting them
        # as a gap was, on the first real run, a false alarm over almost
        # 20,000 mails, and a report that shows nonsense the first time is
        # never opened again.
        ausgelassen = _is_default_skip(top)
        for folder, rel_path in top["subtree"]:
            erwartet = folder.get("totalItemCount")
            if erwartet is None:
                continue
            da = zaehle_dateien(out / rel_path)
            geloescht = weg_je_ordner.get(rel_path, 0)
            zeilen.append({
                "ordner": rel_path,
                "erwartet": int(erwartet),
                "vorhanden": da,
                "geloescht": geloescht,
                "ausgelassen": ausgelassen,
                # Positive means: something is missing. Deleted items do not
                # count as a gap – they still lie in the archive, just no
                # longer in the mailbox.
                "fehlt": 0 if ausgelassen else max(0, int(erwartet) - (da - geloescht)),
            })
    zeilen.sort(key=lambda z: (-z["fehlt"], z["ordner"]))
    gezaehlt = [z for z in zeilen if not z["ausgelassen"]]
    return {
        "geprueft": datetime.now(UTC).isoformat(timespec="seconds"),
        "ordner": zeilen,
        "erwartet": sum(z["erwartet"] for z in gezaehlt),
        "vorhanden": sum(z["vorhanden"] for z in gezaehlt),
        "geloescht": sum(z["geloescht"] for z in gezaehlt),
        "fehlt": sum(z["fehlt"] for z in gezaehlt),
        # What the selection deliberately leaves out – as a number, not a gap.
        "ausgelassen": sum(z["erwartet"] for z in zeilen if z["ausgelassen"]),
        "ausgelassene_ordner": sorted({z["ordner"].split("/")[1]
                                       for z in zeilen if z["ausgelassen"]
                                       and "/" in z["ordner"]}),
    }




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
    stats = {"new": 0, "skipped": 0, "folder_errors": 0}
    result = "done"

    try:
        categories = selected_categories()
        selected_mail, sel_cals, want_con = [], [], False

        if "mail" in categories:
            selected_mail = waehle_ordner(graph, out)
        if "calendar" in categories:
            sel_cals = waehle_kalender(graph, out)
        want_con = "contacts" in categories

        if selected_mail:
            bestand = Bestand()
            result = run_export(graph, out, done, stats, selected_mail, workers, bestand)
            if result == "done" and not stats.get("folder_errors"):
                stats.update(pruefe_verschwundene(graph, out, done, bestand))
        if result != "expired" and sel_cals:
            try:
                export_calendar(graph, out, done, stats, sel_cals)
            except TokenExpired:
                result = "expired"
        if result != "expired" and want_con:
            try:
                export_contacts(graph, out, done, stats)
            except TokenExpired:
                result = "expired"
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
                      errors=stats.get("folder_errors"),
                      extra={k: v for k, v in stats.items()
                             if k in ("gone_new", "moved", "gone_healed")})
    if stats.get("folder_errors"):
        progress.event("run.folders_failed", "warn", n=stats["folder_errors"])


if __name__ == "__main__":
    main()
