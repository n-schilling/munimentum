"""What the hit's detail shows per kind of item (11.2).

The search page draws one detail per kind – mail, chat message,
appointment, contact, file, SharePoint page, OneNote page, Planner task,
To Do task – and only the facts that kind is known by. Most of them the
index row already holds; the rest sits in the source file, and reading
it once when the detail opens is cheap: the .eml for to and cc and the
attachments' sizes, the .ics for when, where and who takes part, the
.vcf for a contact, the mirror for a file's size, the task's own record
for due, state and steps.

One function per kind, one dict out. A key that is missing means "this
item has none of that" – the page makes no row of it, it never writes
"none". Nothing here raises for a broken or vanished file: the facts the
row knows come back either way.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

import corpus

# A file's kind is its mirror – the page's tag says OneDrive, SharePoint
# or Teams; the facts are the same for all three.
_STATE_PLANNER = {0: "notstarted", 100: "done"}
_STATE_TODO = {"notStarted": "notstarted", "inProgress": "inprogress",
               "completed": "completed"}


def fakten(row, text, ziel, state):
    """The facts of one index row. `ziel` is the source file (or None),
    `state` the search module's STATE with the export directories."""
    src = row["src"]
    if src == "outlook":
        out = _mail(row, text, ziel)
    elif src == "teams":
        out = _chat(row, text)
    elif src == "kalender":
        out = _termin(row, text, ziel)
    elif src == "kontakte":
        out = _kontakt(row, ziel)
    elif src == "datei":
        out = _datei(row, ziel)
    elif src == "planner":
        out = _planner(row, text, state)
    elif src == "todo":
        out = _todo(row, text, state)
    else:                       # pages, onenote: the page itself is the content
        out = {"folder": row["ctx"] or "", "modified": row["date"] or "", "text": text}
    out["uid"] = row["uid"]
    out["kind"] = src
    return out


# --------------------------------------------------------------------------
# Mail: from, to, cc, bcc, date, folder, attachments – the body as text
# --------------------------------------------------------------------------
def _adressen(msg, *headers):
    roh = []
    for h in headers:
        roh += [str(v) for v in (msg.get_all(h) or [])]
    out = []
    for name, adresse in getaddresses(roh):
        name, adresse = name.strip(), adresse.strip().lower()
        if name or adresse:
            out.append({"name": name, "mail": adresse})
    return out


def _anhaenge(msg):
    """Real attachments with their size – inline images (signature logos)
    are noise, the same rule corpus.anhaenge applies."""
    out = []
    try:
        teile = list(msg.walk())
    except Exception:
        return out
    for p in teile:
        if p.get_content_disposition() != "attachment" or not p.get_filename():
            continue
        name = corpus.sicherer_dateiname(str(p.get_filename()))
        if any(a["name"] == name for a in out):
            continue
        try:
            groesse = len(p.get_payload(decode=True) or b"")
        except Exception:
            groesse = None
        out.append({"name": name, "size": groesse})
    return out


def _mail(row, text, ziel):
    out = {"from": {"name": row["who"] or "", "mail": row["who_mail"] or ""},
           "to": [], "cc": [], "bcc": [],
           "date": row["date"] or "", "folder": row["ctx"] or "",
           "attachments": [{"name": a, "size": None} for a in (row["att"] or "").split() if a],
           "text": text}
    msg = None
    if ziel is not None:
        try:
            with open(ziel, "rb") as f:
                msg = BytesParser(policy=policy.default).parse(f)
        except Exception:
            msg = None
    if msg is not None:
        von = _adressen(msg, "from")
        if von:
            # The header may carry the address alone – the row's name stays
            out["from"] = {"name": von[0]["name"] or out["from"]["name"],
                           "mail": von[0]["mail"] or out["from"]["mail"]}
        out["to"] = _adressen(msg, "to")
        out["cc"] = _adressen(msg, "cc")
        # Blind copies stand in what one sent oneself – and nowhere else.
        # Not showing them while the search filters by them would leave the
        # hit looking as though it had none.
        out["bcc"] = _adressen(msg, "bcc")
        out["attachments"] = _anhaenge(msg)
    return out


# --------------------------------------------------------------------------
# Chat message: who, when, which chat or channel – the message as text
# --------------------------------------------------------------------------
def _chat(row, text):
    return {"from": {"name": row["who"] or "", "mail": ""}, "date": row["date"] or "",
            "folder": row["ctx"] or "", "chat": row["title"] or "", "text": text}


# --------------------------------------------------------------------------
# Appointment: when, where, organiser, attendees, calendar – the description
# --------------------------------------------------------------------------
def _termin(row, text, ziel):
    out = {"start": row["date"] or "", "end": "", "allday": False, "location": "",
           "organiser": {"name": row["who"] or "", "mail": row["who_mail"] or ""},
           "attendees": [], "calendar": row["ctx"] or "", "text": text}
    if ziel is None:
        return out
    try:
        zeilen = corpus._unfold(ziel.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return out
    beschreibung, ende_roh, ende_tag = None, "", False
    for line in zeilen:
        name, params, value = corpus._prop(line)
        if not name:
            continue
        if name == "DTSTART":
            tag = "VALUE=DATE" in (params or "").upper()
            out["allday"] = tag
            out["start"] = corpus._ics_when(value.strip(), tag, corpus._pval(params, "TZID"))[1]
        elif name == "DTEND":
            ende_tag = "VALUE=DATE" in (params or "").upper()
            ende_roh = corpus._ics_when(value.strip(), ende_tag, corpus._pval(params, "TZID"))[1]
        elif name == "LOCATION":
            out["location"] = corpus._unescape(value)
        elif name == "DESCRIPTION":
            beschreibung = corpus._unescape(value)
        elif name == "ORGANIZER":
            out["organiser"] = {"name": corpus._pval(params, "CN") or "",
                                "mail": (corpus._demail(value) or "").lower()}
        elif name == "ATTENDEE":
            out["attendees"].append({"name": corpus._pval(params, "CN") or "",
                                     "mail": (corpus._demail(value) or "").lower()})
    # An all-day end is exclusive: "16 Sep to 17 Sep" is one day, 16 Sep.
    if ende_roh and ende_tag and out["allday"]:
        try:
            letzter = datetime.strptime(ende_roh[:10], "%Y-%m-%d") - timedelta(days=1)
            ende_roh = "" if letzter.strftime("%Y-%m-%d") == out["start"][:10] \
                else letzter.strftime("%Y-%m-%d")
        except ValueError:
            pass
    out["end"] = ende_roh
    if beschreibung is not None:
        # The index text carries "Ort: …" up front so the place is searchable;
        # the detail shows the place as a fact and the description alone.
        out["text"] = corpus.collapse(beschreibung)
    return out


# --------------------------------------------------------------------------
# Contact: organisation, role, addresses, phones, folder – the note
# --------------------------------------------------------------------------
def _kontakt(row, ziel):
    out = {"org": "", "role": "", "emails": [], "phones": [], "note": "",
           "folder": row["ctx"] or ""}
    if ziel is None:
        return out
    try:
        zeilen = corpus._unfold(ziel.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return out
    for line in zeilen:
        name, params, value = corpus._prop(line)
        if name == "ORG":
            out["org"] = " · ".join(x for x in corpus._unescape(value).split(";") if x)
        elif name == "TITLE":
            out["role"] = corpus._unescape(value)
        elif name == "EMAIL":
            out["emails"].append(value.strip())
        elif name == "TEL":
            out["phones"].append(value.strip())
        elif name == "NOTE":
            out["note"] = corpus._unescape(value)
    out["text"] = out["note"]
    return out


# --------------------------------------------------------------------------
# File: type, size, modified, where – no content, none is indexed
# --------------------------------------------------------------------------
def _datei(row, ziel):
    name = Path(row["rel"]).name
    out = {"name": name, "ext": Path(name).suffix.lstrip(".").lower(),
           "size": None, "modified": row["date"] or "", "folder": row["ctx"] or ""}
    if ziel is not None:
        try:
            st = ziel.stat()
            out["size"] = st.st_size
            out["modified"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            pass
    return out


# --------------------------------------------------------------------------
# Tasks: the record the export keeps in the board's or list's state.db
# --------------------------------------------------------------------------
def _task_eintrag(row, state, schluessel):
    """The task's own record: uid is `<src>:<folder>/<task id>:0`, the
    folder is the export's directory of the board or list."""
    basis = state.get(schluessel)
    if not basis:
        return None, {}
    try:
        import state_db
        ordner = row["rel"].split("/", 1)[0]
        tid = row["uid"].split(":", 1)[1].rsplit(":", 1)[0].split("/", 1)[1]
        db = state_db.StateDb(Path(basis) / ordner)
        eintraege = json.loads(db.kv_lesen("tasks") or "{}")
        namen = json.loads(db.kv_lesen("namen") or "{}")
    except Exception:
        return None, {}
    return eintraege.get(tid), namen


def _tag(iso):
    """A Graph date-time – a string or {dateTime, timeZone} – as a day."""
    if isinstance(iso, dict):
        iso = iso.get("dateTime")
    return str(iso or "")[:10]


def _planner(row, text, state):
    ctx = row["ctx"] or ""
    plan, _, bucket = ctx.partition("/")
    out = {"plan": plan, "bucket": bucket, "folder": ctx,
           "assigned": [w for w in (row["who"] or "").split(", ") if w],
           "due": "", "state": "", "checklist": None,
           "attachments": [a for a in (row["att"] or "").split() if a],
           "text": text, "comments": []}
    e, namen = _task_eintrag(row, state, "planner_dir")
    if not e:
        return out
    task, det = e.get("task") or {}, e.get("details") or {}
    out["assigned"] = sorted(namen.get(k, k) for k in (task.get("assignments") or {})) or out["assigned"]
    out["due"] = _tag(task.get("dueDateTime"))
    prozent = task.get("percentComplete") or 0
    out["state"] = _STATE_PLANNER.get(prozent, "inprogress")
    liste = list((det.get("checklist") or {}).values())
    if liste:
        out["checklist"] = {"done": sum(1 for c in liste if c.get("isChecked")),
                            "total": len(liste)}
    out["text"] = str(det.get("description") or "")
    out["comments"] = [
        {"who": namen.get(k.get("wer") or "", k.get("wer") or ""),
         "when": str(k.get("wann") or ""),
         "text": corpus.collapse(corpus.strip_html(k.get("html") or ""))}
        for k in (e.get("kommentare") or [])]
    return out


def _todo(row, text, state):
    out = {"list": row["ctx"] or "", "folder": row["ctx"] or "", "due": "", "state": "",
           "completed": "", "steps": None, "linked": [],
           "attachments": [a for a in (row["att"] or "").split() if a], "text": text}
    e, _namen = _task_eintrag(row, state, "todo_dir")
    if not e:
        return out
    task = e.get("task") or {}
    out["due"] = _tag(task.get("dueDateTime"))
    out["state"] = _STATE_TODO.get(task.get("status") or "", "")
    out["completed"] = _tag(task.get("completedDateTime"))
    schritte = task.get("checklistItems") or []
    if schritte:
        out["steps"] = {"done": sum(1 for s in schritte if s.get("isChecked")),
                        "total": len(schritte)}
    out["linked"] = [str(r.get("displayName") or r.get("applicationName") or "")
                     for r in (task.get("linkedResources") or [])]
    body = task.get("body") or {}
    inhalt = str(body.get("content") or "")
    if (body.get("contentType") or "text") == "html":
        inhalt = corpus.collapse(corpus.strip_html(inhalt))
    out["text"] = inhalt.strip()
    return out
