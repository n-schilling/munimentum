#!/usr/bin/env python3
"""
Calendar and contact evaluation of the Outlook export as JSON.

Reads the export's .ics and .vcf files and writes appointments, contacts and
reconstructed appointments into a JSON file. app.py renders the calendar and
the address book from it – so the reconstruction exists once in the project
rather than twice in slightly different flavours.

Deleted appointments are reconstructed from the mails: invitations, replies
and cancellations carry the complete appointment including its UID in the
text/calendar part. If that UID is missing from the calendar export, the
appointment still shows up in the calendar – as "deleted" (when a
cancellation exists) or "not in the calendar" (merely invited/accepted). To
keep this from creating ghost copies, foreign UIDs embedded in Exchange IDs
are unwrapped (see norm_uid) and hits whose title and start minute already
sit in the calendar are discarded.

Runs as a subprogram of app.py; standard library only.

    Arguments: outlook-folder --json target.json [--no-reconstruct]

--no-reconstruct skips restoring deleted appointments from mails. It is by
far the most expensive part – every .eml gets read, minutes on a large
mailbox – and is not needed for appointments and contacts alone.

Reading the mails is incremental: kalender.db in the Outlook folder keeps a
manifest with one row per .eml – its (mtime_ns, size) and the invitation
facts it yielded, an empty list for the vast majority that carry no
text/calendar part. A run stats every file, parses only new and changed
ones, drops the rows of vanished files and builds calendar.json from the
union; the result is what a full parse would produce. Without a usable
manifest everything is simply read again – slower, never wrong.
"""

import os
import sys
import re
import json
import sqlite3
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

import export_util
import progress
import settings
import state_db
# The parser primitives for .eml, iCalendar and vCard live in corpus.py –
# they are only reused here, not maintained a second time.
from corpus import addr_people, hdr, _demail, _ics_when, _pval, _prop, _unescape, _unfold

export_util.erzwinge_utf8()

BODY_CAP = 4000


# ===========================================================================
# Outlook: collecting invitation/cancellation mails (.eml with text/calendar)
# ===========================================================================
def mail_ical(msg):
    """(METHOD, iCalendar text) from a mail's text/calendar part."""
    for part in msg.walk():
        if part.get_content_type() != "text/calendar":
            continue
        try:
            txt = part.get_content()
        except Exception:
            txt = None
        if not isinstance(txt, str):
            raw = part.get_payload(decode=True) or b""
            txt = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        return (part.get_param("method") or "").upper(), txt
    return "", ""


def _mail_when(raw_date):
    """(timestamp, display) of a mail's Date header – (None, raw) where it
    does not parse. Derived when the invites are built, not stored: the
    display string follows the machine's time zone."""
    ts, disp = None, raw_date
    try:
        dt = parsedate_to_datetime(raw_date)
        if dt is not None:
            ts = dt.timestamp()
            disp = dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return ts, disp


def eml_facts(path):
    """The invitation facts of one .eml – all the reconstruction needs of
    it and exactly what the manifest stores: one entry per VEVENT with a
    UID, an empty list for a mail without a text/calendar part. Plain
    JSON-serialisable data; raises when the file cannot be read."""
    with open(path, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)
    method, ical = mail_ical(msg)
    if not ical:
        return []
    m2, evs = parse_vevents(ical)
    meth = method or m2
    # Reply mails carry no ORGANIZER, only the replying ATTENDEE –
    # the organiser is their recipient. Invitations and cancellations
    # come the other way round, from the organiser themselves.
    fn, fe = addr_people(msg, "from")
    hn, hm = addr_people(msg, "to") if meth in ("REPLY", "COUNTER") else (fn, fe)
    hint = [hn[0] if hn else "", hm[0] if hm else ""]
    date = hdr(msg, "date")
    return [{"method": meth, "ev": ev, "org_hint": hint, "date": date}
            for ev in evs if ev["uid"]]


def _invite_rec(fact, href):
    """One reconstruction input from a fact, parsed or stored. Link and
    mail date are derived here, so the manifest depends on neither the
    link base nor the time zone."""
    ts, disp = _mail_when(fact["date"])
    return {"method": fact["method"], "ev": fact["ev"],
            "org_hint": tuple(fact["org_hint"]),
            "href": href, "mts": ts, "md": disp}


# The manifest: area "kalender_manifest" in the Outlook folder's kalender.db,
# one row per .eml keyed by its path relative to the export root, holding
# {"v", "mtime_ns", "size", "facts"}. Some 200–300 bytes a row, so a
# mailbox of 45,000 mails costs around 10 MB.
MANIFEST_AREA = "kalender_manifest"
# Bump when the stored facts change shape (a new VEVENT field, say): rows
# of another version count as changed and their files are parsed again.
MANIFEST_DB = "kalender.db"      # its own file: the export's state.db mtime dates the last run
MANIFEST_VERSION = 1
# Parsed files per write: an aborted first run keeps what it got through.
_MANIFEST_FLUSH = 1000


def _manifest_row_ok(row, ev_keys):
    """Only a row of the current shape is trusted; anything else counts as
    "not there" and its file is parsed again."""
    if not (isinstance(row, dict) and row.get("v") == MANIFEST_VERSION
            and isinstance(row.get("mtime_ns"), int)
            and isinstance(row.get("size"), int)
            and isinstance(row.get("facts"), list)):
        return False
    return all(isinstance(f, dict)
               and isinstance(f.get("method"), str)
               and isinstance(f.get("date"), str)
               and isinstance(f.get("org_hint"), list) and len(f["org_hint"]) == 2
               and isinstance(f.get("ev"), dict) and ev_keys <= f["ev"].keys()
               for f in row["facts"])


def _manifest_lesen(db):
    """rel -> row of the previous run. Empty when there is none or it cannot
    be read – then everything is parsed, which is only slower, never wrong."""
    try:
        rows = db.saetze_lesen(MANIFEST_AREA)
    except (sqlite3.Error, OSError):
        return {}
    ev_keys = _new_event().keys()
    out = {}
    for rel, raw in rows.items():
        try:
            row = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if _manifest_row_ok(row, ev_keys):
            out[rel] = row
    return out


def _manifest_schreiben(db, neu, weg=()):
    """Upsert `neu` (rel -> row) and drop the rows in `weg`. A failure
    costs nothing but the next run's head start."""
    try:
        db.saetze_schreiben(MANIFEST_AREA, {rel: json.dumps(row, ensure_ascii=False)
                                            for rel, row in neu.items()})
        db.saetze_loeschen(MANIFEST_AREA, weg)
        return True
    except (sqlite3.Error, OSError):
        return False


def read_outlook(root, out_dir, invites, db=None):
    """Collect appointment parts (text/calendar) from all .eml files.

    That is the only reason mails are read here – their contents are
    indexed by corpus.py. With `db` (the export folder's StateDb) the
    manifest does most of the work: a file whose mtime and size still
    match is served from its stored facts, only new and changed files are
    parsed, rows of vanished files are dropped. Without `db` every file is
    parsed. Returns (files reused, files parsed).
    """
    dateien = sorted(root.rglob("*.eml"))
    progress.melde(0, len(dateien), "mails")
    alt = _manifest_lesen(db) if db is not None else {}
    neu, gesehen = {}, set()
    wieder = gelesen = 0
    for n, p in enumerate(dateien, 1):
        if n % 200 == 0:
            progress.melde(n, len(dateien), "mails")
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        row = alt.get(rel)
        if row is not None and (row["mtime_ns"], row["size"]) == (st.st_mtime_ns, st.st_size):
            facts = row["facts"]
            wieder += 1
        else:
            try:
                facts = eml_facts(p)
            except Exception:
                continue      # unreadable: no row, so the next run tries again
            gelesen += 1
            if db is not None:
                neu[rel] = {"v": MANIFEST_VERSION, "mtime_ns": st.st_mtime_ns,
                            "size": st.st_size, "facts": facts}
                if len(neu) >= _MANIFEST_FLUSH:
                    _manifest_schreiben(db, neu)
                    neu = {}
        gesehen.add(rel)
        href = link(p, out_dir)
        invites.extend(_invite_rec(f, href) for f in facts)
    if db is not None:
        _manifest_schreiben(db, neu, [rel for rel in alt if rel not in gesehen])
    return wieder, gelesen


# ===========================================================================
# Calendar (.ics) and contacts (.vcf) – part of the Outlook export
# ===========================================================================
def _new_event():
    """The VEVENT fields the reconstruction works with – a fresh dict each
    time, the lists must not be shared between events."""
    return {"uid": "", "recid": "", "summary": "", "location": "",
            "description": "", "dtstart": "", "dateonly": False,
            "tzstart": "", "dtend": "", "enddateonly": False,
            "tzend": "", "status": "", "seq": 0,
            "org_cn": "", "org_mail": "",
            "att_names": [], "att_mails": []}


def parse_vevents(text):
    """iCalendar text -> (METHOD, [VEVENT fields, …]).

    Block-aware, so properties from VTIMEZONE/VALARM don't land in the
    appointment: Exchange invitation mails contain a VTIMEZONE with its own
    DTSTART (e.g. 16010101T030000) that a flat parser would read as the
    appointment date.
    """
    method, events, stack, ev = "", [], [], None
    for line in _unfold(text):
        name, params, value = _prop(line)
        if not name:
            continue
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                ev = _new_event()
            continue
        if name == "END":
            if value.strip().upper() == "VEVENT" and ev is not None:
                events.append(ev)
                ev = None
            if stack:
                stack.pop()
            continue
        if name == "METHOD" and ev is None:
            method = value.strip().upper()
        if ev is None or (stack and stack[-1] != "VEVENT"):
            continue
        if name == "UID":
            ev["uid"] = value.strip()
        elif name == "RECURRENCE-ID":
            ev["recid"] = value.strip()
        elif name == "SUMMARY":
            ev["summary"] = _unescape(value)
        elif name == "LOCATION":
            ev["location"] = _unescape(value)
        elif name == "DESCRIPTION":
            ev["description"] = _unescape(value)
        elif name == "DTSTART":
            ev["dtstart"] = value.strip()
            ev["dateonly"] = "VALUE=DATE" in (params or "").upper()
            ev["tzstart"] = _pval(params, "TZID")
        elif name == "DTEND":
            ev["dtend"] = value.strip()
            ev["enddateonly"] = "VALUE=DATE" in (params or "").upper()
            ev["tzend"] = _pval(params, "TZID")
        elif name == "STATUS":
            ev["status"] = value.strip().lower()
        elif name == "SEQUENCE":
            try:
                ev["seq"] = int(value.strip())
            except ValueError:
                pass
        elif name == "ORGANIZER":
            ev["org_cn"], ev["org_mail"] = _pval(params, "CN"), _demail(value)
        elif name == "ATTENDEE":
            cn, mail = _pval(params, "CN"), _demail(value)
            if cn:
                ev["att_names"].append(cn)
            if mail:
                ev["att_mails"].append(mail)
    return method, events


def event_rec(ev, *, ctx, href, status, cal):
    """Shared record for calendar and reconstructed appointments."""
    ts, disp = _ics_when(ev["dtstart"], ev["dateonly"], ev.get("tzstart", ""))
    te, _ = _ics_when(ev["dtend"], ev["enddateonly"], ev.get("tzend", ""))
    names = [ev["org_cn"], ev["org_mail"]] + ev["att_names"] + ev["att_mails"]
    text = ((f"Ort: {ev['location']}. " if ev["location"] else "") + ev["description"]).strip()
    return {
        "src": "kalender",
        "who": ev["org_cn"] or ev["org_mail"] or "(unbekannt)",
        "ppl": " ".join(x for x in names if x).lower(),
        "ts": ts, "d": disp,
        "title": ev["summary"] or "(kein Betreff)",
        "ctx": ctx,
        "x": text[:BODY_CAP],
        "p": href,
        # extra fields for the calendar view
        "te": te,
        "ad": 1 if ev["dateonly"] else 0,
        "st": status,
        "cal": cal,
        "loc": ev["location"],
        "att": (ev["att_names"] or ev["att_mails"])[:25],
        "uid": ev["uid"],
    }


def read_calendar(root, out_dir):
    recs = []
    for p in sorted(root.rglob("*.ics")):
        _, events = parse_vevents(p.read_text(encoding="utf-8", errors="replace"))
        if not events:
            continue
        ev = events[0]                       # the export stores one event per file
        segs = p.relative_to(root).as_posix().split("/")
        cal = segs[1] if len(segs) >= 3 and segs[0] == "kalender" else "Kalender"
        recs.append(event_rec(ev, ctx=f"Kalender: {cal}", href=link(p, out_dir),
                              status=ev["status"] or "confirmed", cal=cal))
    return recs


# Outlook puts a status in front of the subject of reply/cancellation mails –
# noise for the reconstructed appointment; the view shows the status itself.
REPLY_PREFIX = re.compile(
    r"^(Abgesagt|Canceled|Cancelled|Angenommen|Accepted|Abgelehnt|Declined|"
    r"Mit Vorbehalt|Tentative|Vorläufig zugesagt|Aktualisiert|Updated|"
    r"Weitergeleitet|Forwarded|Zeitvorschlag|New Time Proposed)\s*:\s*", re.I)

# Which mail describes an event best? Invitation over cancellation over reply.
METHOD_RANK = {"REQUEST": 4, "PUBLISH": 3, "CANCEL": 2, "COUNTER": 1, "REPLY": 0}

_VCAL_UID = b"vCal-Uid\x01\x00\x00\x00"


def norm_uid(uid):
    """Bring an appointment UID into a comparable form.

    Exchange wraps foreign UIDs (Google, Zoom, …) in its own Global Object
    ID: a hex blob containing the original UID as ASCII behind the marker
    "vCal-Uid". The calendar export delivers this blob, the invitation mail
    the bare UID – without unwrapping, the same appointment would count as
    deleted.
    """
    u = (uid or "").strip()
    if len(u) < len(_VCAL_UID) * 2 or len(u) % 2:
        return u.lower()
    try:
        raw = bytes.fromhex(u)
    except ValueError:
        return u.lower()
    i = raw.find(_VCAL_UID)
    if i < 0:
        return u.lower()
    inner = raw[i + len(_VCAL_UID):].split(b"\x00", 1)[0]
    return inner.decode("ascii", "replace").lower() or u.lower()


def reconstruct_events(invites, cal_recs):
    """Restore appointments that now exist only in mails.

    Invitation, reply and cancellation mails carry the complete VEVENT with
    its UID. If that UID is missing from the calendar export, the event was
    deleted there – the mail lets us reconstruct it. With a cancellation
    present (METHOD:CANCEL) it counts as cancelled/deleted, otherwise merely
    as "not in the calendar" (e.g. never accepted).

    Returns: (reconstructed records, number of calendar events marked
    cancelled after the fact, number of reconstructions discarded as dupes).
    """
    known, same = {}, set()
    for r in cal_recs:
        if r.get("uid"):
            known.setdefault(norm_uid(r["uid"]), []).append(r)
        if r.get("ts"):
            same.add((r["title"].strip().lower(), int(r["ts"] // 60)))

    groups = {}
    for it in invites:
        ev = it["ev"]
        cancelled = it["method"] == "CANCEL" or ev["status"] == "cancelled"
        rank = (1 if ev["dtstart"] else 0, METHOD_RANK.get(it["method"], 0),
                ev["seq"], it["mts"] or 0)
        g = groups.setdefault((norm_uid(ev["uid"]), ev["recid"]), {"cancel": None, "best": None})
        if cancelled and (g["cancel"] is None or rank > g["cancel"][0]):
            g["cancel"] = (rank, it)
        if g["best"] is None or rank > g["best"][0]:
            g["best"] = (rank, it)

    ghosts, marked, dupes = [], 0, 0
    for (uid, recid), g in groups.items():
        cancel = g["cancel"]
        if uid in known:
            if not recid:
                # event is in the calendar – a cancellation mail heals a stale status
                for r in (known[uid] if cancel else []):
                    if r["st"] != "cancelled":
                        r["st"] = "cancelled"
                        marked += 1
                continue
            if not cancel:
                continue      # instance without cancellation is already in the series event
        it = (cancel or g["best"])[1]
        ev = it["ev"]
        if not ev["dtstart"]:
            continue          # without a start time it cannot be placed in the calendar
        ev = dict(ev, summary=REPLY_PREFIX.sub("", ev["summary"]).strip())
        if not (ev["org_cn"] or ev["org_mail"]):
            ev["org_cn"], ev["org_mail"] = it.get("org_hint") or ("", "")
        state = "deleted" if cancel else "gone"
        note = "abgesagt" if cancel else "nicht im Kalender"
        rec = event_rec(ev, ctx=f"Kalender: {note} · rekonstruiert aus Mail vom {it['md']}",
                        href=it["href"], status=state, cal="(rekonstruiert)")
        # Catches UID formats not yet known here: if the same event (title +
        # start minute) already sits in the calendar, it is not deleted.
        if rec["ts"] and (rec["title"].strip().lower(), int(rec["ts"] // 60)) in same:
            dupes += 1
            continue
        ghosts.append(rec)
    ghosts.sort(key=lambda r: (r["ts"] is None, -(r["ts"] or 0)))
    return ghosts, marked, dupes


def read_contacts(root, out_dir):
    recs = []
    for p in sorted(root.rglob("*.vcf")):
        fn = org = title = note = given = family = ""
        emails, tels = [], []
        for line in _unfold(p.read_text(encoding="utf-8", errors="replace")):
            name, params, value = _prop(line)
            if not name:
                continue
            if name == "FN":
                fn = _unescape(value)
            elif name == "N":
                parts = [_unescape(x) for x in value.split(";")]
                family = parts[0] if len(parts) > 0 else ""
                given = parts[1] if len(parts) > 1 else ""
            elif name == "ORG":
                org = " · ".join(x for x in _unescape(value).split(";") if x)
            elif name == "TITLE":
                title = _unescape(value)
            elif name == "EMAIL":
                emails.append(value.strip())
            elif name == "TEL":
                tels.append(value.strip())
            elif name == "NOTE":
                note = _unescape(value)
        if not fn:
            fn = (given + " " + family).strip() or "(ohne Namen)"
        segs = p.relative_to(root).as_posix().split("/")
        folder = segs[1] if len(segs) >= 3 and segs[0] == "kontakte" else ""
        text = " · ".join(x for x in ([org, title] + emails + tels + ([note] if note else [])) if x)
        recs.append({
            "src": "kontakte",
            "who": org or title or "Kontakt",
            "ppl": " ".join([fn] + emails).lower(),
            "ts": None, "d": "",
            "title": fn,
            "ctx": f"Kontakte: {folder}" if folder else "Kontakte",
            "x": text[:BODY_CAP],
            "p": link(p, out_dir),
            # extra fields for the address book
            "em": emails[:10],
            "tel": tels[:10],
            "org": org,
            "role": title,
        })
    return recs


# ===========================================================================
# Shared
# ===========================================================================
def link(path, out_dir):
    try:
        rel = os.path.relpath(path, start=out_dir).replace(os.sep, "/")
        return "/".join(quote(seg) for seg in rel.split("/"))
    except ValueError:
        return Path(path).as_uri()


def collect_calendar_data(outlook_dir, text_cap=600, reconstruct=True):
    """Deliver calendar, contacts and reconstructed events as plain data.

    Paths come as `root` + `rel` (unencoded) instead of a finished link:
    the app serves the files through its own /source route. Description
    texts are truncated (`text_cap`) – in the calendar they only appear in
    the tooltip and in the search over reconstructed events; untruncated,
    thousands of events bloat the response.
    """
    root = Path(outlook_dir).resolve()
    if not root.is_dir():
        raise SystemExit(f"Outlook-Export nicht gefunden: {outlook_dir}")
    invites = []
    ghosts, marked, dupes = [], 0, 0
    # out_dir = root: link() then yields paths relative to the export root,
    # exactly what the app's /source route expects.
    #
    # Reading all .eml files is the expensive part – minutes on a large
    # mailbox the first time, a stat per file afterwards thanks to the
    # manifest – and happens solely for the invitations. Whoever builds the
    # calendar just for the events or the contacts would otherwise pay for
    # it without getting anything in return.
    if reconstruct:
        db = state_db.StateDb(root, MANIFEST_DB)
        try:
            read_outlook(root, root, invites, db)
        finally:
            db.close()
    cal = read_calendar(root, root)
    if reconstruct:
        ghosts, marked, dupes = reconstruct_events(invites, cal)
    contacts = read_contacts(root, root)

    recs = cal + ghosts + contacts
    for r in recs:
        r["root"] = "outlook"
        r["rel"] = unquote(r.pop("p", ""))
        r.pop("uid", None)          # only needed for the matching above, ~1 MB
        # Only the search over reconstructed events needs the people list
        # and the description. Across all events they make up two thirds of
        # the response without anyone ever reading them.
        if r.get("st") in ("deleted", "gone"):
            if text_cap is not None and r.get("x"):
                r["x"] = r["x"][:text_cap]
        else:
            r.pop("ppl", None)
            r.pop("x", None)
    recs.sort(key=lambda r: (r["ts"] is None, -(r["ts"] or 0)))
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "outlook_dir": str(root),
        # Whether reconstruction ran has to travel along: otherwise the UI
        # could only read an empty list as "there was nothing", not as
        # "nobody looked in the first place".
        "reconstruct": bool(reconstruct),
        "counts": {"kalender": len(cal), "rekonstruiert": len(ghosts),
                   "kontakte": len(contacts), "abgesagt_markiert": marked,
                   "doppel_verworfen": dupes},
        "recs": recs,
    }


def write_calendar_json(outlook_dir, ziel, reconstruct=True):
    """Write the calendar data to `ziel` (atomically). Returns the counts."""
    daten = collect_calendar_data(outlook_dir, reconstruct=reconstruct)
    ziel = Path(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(ziel.name + ".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ziel)
    return daten["counts"]


_hilfe_gewuenscht = export_util.hilfe_gewuenscht


def main():
    if _hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__.strip())
        return

    args = sys.argv[1:]
    kalender_json = None
    # Default from app_config.json, so a standalone call carries the same
    # setting as the app. --no-reconstruct beats it on the command line.
    reconstruct = settings.flag("CALENDAR_RECONSTRUCT", "calendar_reconstruct")
    pos = []
    i = 0
    while i < len(args):
        if args[i] == "--json" and i + 1 < len(args):
            kalender_json = args[i + 1]
            i += 2
        elif args[i] == "--no-reconstruct":
            reconstruct = False
            i += 1
        else:
            pos.append(args[i])
            i += 1
    if not kalender_json:
        raise SystemExit("Usage: python3 combined_search.py outlook-folder "
                         "--json target.json [--no-reconstruct]")
    # The app passes exactly one positional: the Outlook export.
    outlook_dir = export_util.ausgabeordner(pos)

    c = write_calendar_json(outlook_dir, kalender_json, reconstruct=reconstruct)
    # Same result schema as every other subprogram; the file is rebuilt as a
    # whole, so "new" is everything it now contains.
    progress.ergebnis(c["kalender"] + c["rekonstruiert"] + c["kontakte"],
                      extra={"events": c["kalender"],
                             "rebuilt": c["rekonstruiert"],
                             "contacts": c["kontakte"]})


if __name__ == "__main__":
    main()
