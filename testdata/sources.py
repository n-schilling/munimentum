"""The eight export folders, written as a real export would leave them –
and the organization below the Teams folder.

One writer per source, all of them fed from testdata/people.py. Where the
app has a builder of its own – the `.ics` and `.vcf` writers, the Teams
conversation renderer, the Planner board, the To Do list, the OneNote page
frame – this module calls it instead of imitating it: that way the
synthetic archive cannot drift away from what the parsers expect, and a
change to a format shows up here as a failing browser test rather than as
an archive nobody can read.

What the readers need, source by source (corpus.py):

    mail        Subject, From, Date and a body, as MIME in <folder>/*.eml
    calendar    SUMMARY, DTSTART, ORGANIZER in kalender/<name>/*.ics
    contacts    FN and EMAIL in kontakte/*.vcf – contacts carry no date
    teams       <h1>, and per message span.name, span.time, div.body
    files       the file itself; title and date come from name and mtime
    pages       <title> plus the text; the date is the mtime
    planner     the plan and its tasks as JSON in the plan folder's state.db
    todo        the list and its tasks likewise; board.html/list.html are
                what a hit opens, not what is read
    onenote     <title> and the text; the date is the mtime

So five of the sources take their date from the file system: this module
sets every mtime explicitly, and testdata/build.py pins the time zone, so
the same archive comes out on every machine.

The bookkeeping (state.db per export) is written too, although only
Planner and To Do strictly need it: it is what gives items their stable
keys, and without those a case would pin an item by its path.
"""

import json
import os
from datetime import UTC, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from pathlib import Path

import export_util
import folders
import onenote_export
import organization
import outlook_export
import planner_export
import settings
import state_db
import teams_export
import todo_export

from testdata import bulk, people
from testdata.people import day

ME, ME_MAIL = people.ME
# The story's cast by name – the rest of the people are the volume's, and
# only testdata/bulk.py reaches for them.
BOB, CARLA = people.COLLEAGUES[:2]
DANA, ERIK, GRETA = people.EXTERNALS[:3]
PROJECT, OFFER = people.PROJECT, people.OFFER

# One 8×8 pixel PNG, so the mirrors hold something that is not text and the
# file browser has a picture to show. Written from a literal instead of
# generated, because a picture library would be one dependency for 200 bytes.
PNG_8x8 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29dc"
    "0000001b4944415408d763fccfc0f09f8114c0a88a01a3aa06462f000059c30a0a"
    "b7f2b9e30000000049454e44ae426082")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def graph_time(when):
    """A naive local timestamp as Graph writes it: UTC with a Z."""
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _touch(path, when):
    """Give the file the date the archive means – five sources read it."""
    stamp = when.replace(tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))


def write(path, content, when=None):
    """One file, its folders, and its date. Text or bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    if when:
        _touch(path, when)
    return path


def addr(person):
    return formataddr(person)


# ---------------------------------------------------------------------------
# Outlook: mail
# ---------------------------------------------------------------------------
# Every mail as the mailbox would hold it: the folder it lies in, who wrote
# it to whom, and a body that says enough for a search to have something to
# rank. `reply_to` names the mail this one answers, so the archive has real
# conversations and not twelve loose sheets; one sent mail carries a `bcc`,
# because that is the only kind that ever does.
MAILS = [
    {"key": "offer-1", "folder": "Inbox", "when": day(2, 10, 9, 12),
     "frm": DANA, "to": [people.ME], "cc": [BOB],
     "subject": f"{OFFER} – our proposal for project {PROJECT}",
     "body": f"""Dear Alice,

attached is our proposal for project {PROJECT}, as discussed last week.
The schedule assumes we can start in March and hand over the first stage
before the summer break.

Two points are still open: the test window in May and who supplies the
hardware. I would suggest we settle both in the kickoff meeting.

Kind regards
Dana Dienstleister""",
     "attachments": [("offer-4711.txt", f"""{OFFER}
Project: {PROJECT}
Stage 1  Analysis and setup      12 days
Stage 2  Rollout, first site     18 days
Stage 3  Handover and training    5 days
Total                            35 days
""")]},

    {"key": "offer-2", "folder": "Sent", "when": day(2, 11, 8, 5),
     "frm": people.ME, "to": [DANA], "cc": [CARLA], "reply_to": "offer-1",
     "subject": f"Re: {OFFER} – our proposal for project {PROJECT}",
     "body": """Hello Dana,

thank you, the proposal looks workable. Carla is in copy, she decides on
the budget.

On the two open points: the test window in May is fine with us, the
hardware we would rather order ourselves – Erik is already looking into
it.

Best regards
Alice"""},

    {"key": "offer-3", "folder": "Inbox", "when": day(2, 14, 16, 40),
     "frm": DANA, "to": [people.ME], "reply_to": "offer-2",
     "subject": f"Re: {OFFER} – our proposal for project {PROJECT}",
     "body": """Hello Alice,

understood, then we plan without the hardware and keep the test window as
it stands. I will send an updated schedule once the kickoff date is set.

Kind regards
Dana"""},

    {"key": "order-1", "folder": "Inbox", "when": day(3, 3, 10, 20),
     "frm": ERIK, "to": [people.ME], "cc": [BOB],
     "subject": f"Purchase order for the {PROJECT} hardware",
     "body": f"""Hi Alice,

here is the order list for {PROJECT}. Prices are the ones from the
framework agreement, delivery is four weeks after we release it.

Please check the quantities – I took them from the proposal and I am not
sure about the spare devices.

Erik""",
     "attachments": [("order-list.csv", """item;quantity;note
Laptop 14";6;one spare
Docking station;5;
Monitor 27";5;
Headset;6;one spare
""")]},

    {"key": "order-2", "folder": "Sent", "when": day(3, 4, 7, 55),
     "frm": people.ME, "to": [ERIK], "reply_to": "order-1",
     "subject": f"Re: Purchase order for the {PROJECT} hardware",
     "body": """Hi Erik,

quantities are right, the spare devices are deliberate. Please release the
order, the four weeks fit the schedule.

Alice"""},

    {"key": "kickoff", "folder": f"Projects/{PROJECT}", "when": day(3, 20, 13, 30),
     "frm": BOB, "to": [people.ME, CARLA],
     "subject": f"{PROJECT} kickoff – minutes",
     "body": f"""Minutes of the kickoff, {day(3, 20).strftime('%d %B %Y')}.

Present: Alice, Carla, Bob, Dana Dienstleister.

1. Schedule accepted as proposed. Stage 1 starts on 23 March.
2. Hardware is ordered by us, Erik has released the order.
3. The test window stays in May; Dana provides the test protocol.
4. Next steering meeting in June.

Bob"""},

    {"key": "budget", "folder": f"Projects/{PROJECT}", "when": day(4, 8, 9, 0),
     "frm": CARLA, "to": [people.ME],
     "subject": f"Budget for {PROJECT} approved",
     "body": f"""Alice,

the budget for {PROJECT} is approved as it stands in {OFFER}. Please keep
the monthly report going, I will need it for the board in July.

Carla"""},

    {"key": "visit", "folder": "Inbox", "when": day(4, 22, 11, 11),
     "frm": GRETA, "to": [people.ME],
     "subject": "Visitor badge for the workshop",
     "body": """Hello Alice,

I will be at the workshop on 7 May. Could you arrange a visitor badge for
me? I arrive around nine.

Thanks and see you there
Greta Gast"""},

    {"key": "status", "folder": "Sent", "when": day(5, 5, 17, 45),
     "frm": people.ME, "to": [BOB, CARLA], "bcc": [ERIK],
     "subject": f"{PROJECT} status, week 19",
     "body": f"""Status {PROJECT}, week 19:

- Stage 1 finished, one day ahead of the schedule.
- Hardware delivered and checked, two docking stations are missing and
  have been reordered.
- Test protocol from Dana is in the notebook.
- Risk: the rollout at the second site depends on the network change,
  which is not scheduled yet.

Alice"""},

    {"key": "rollout", "folder": "Inbox", "when": day(5, 18, 8, 30),
     "frm": DANA, "to": [people.ME], "cc": [ERIK],
     "subject": f"{PROJECT} rollout plan, second draft",
     "body": """Hello Alice,

the second draft of the rollout plan is attached. The change against the
first one: we do the second site in two waves, so the network change has
less weight.

Kind regards
Dana""",
     "attachments": [("rollout-plan.md", f"""# {PROJECT} rollout plan (draft 2)

## Site 1
* 2 June   preparation, backup
* 3 June   rollout, 18 workplaces
* 4 June   walkthrough of the test protocol

## Site 2
* 15 June  wave 1, 9 workplaces
* 22 June  wave 2, 9 workplaces, after the network change
""")]},

    {"key": "protocol", "folder": f"Projects/{PROJECT}", "when": day(6, 2, 14, 15),
     "frm": BOB, "to": [people.ME],
     "subject": f"Test protocol {PROJECT}",
     "body": """Alice,

I walked through the test protocol with Dana. Everything passed except
the printer mapping, which fails on two of the older devices. Dana is
looking at it, it does not block the rollout.

Bob"""},

    {"key": "steering", "folder": "Inbox", "when": day(6, 10, 7, 40),
     "frm": CARLA, "to": [people.ME], "cc": [BOB],
     "subject": f"{PROJECT} steering meeting on Thursday",
     "body": """Alice,

the steering meeting is on Thursday at three. Please bring the status and
a word on the network change – that is the one thing the board will ask
about.

Carla"""},

    # Four mails whose words are not where a plain text part would hold
    # them – on a real mailbox nearly one mail in five, and none of them
    # used to reach the index. The invitation's own words sit in its
    # calendar part (its UID is the steering meeting's, which the calendar
    # holds, so no appointment is rebuilt from it).
    {"key": "invite-steering", "folder": "Inbox", "when": day(6, 11, 9, 0),
     "frm": CARLA, "to": [people.ME], "shape": "invitation",
     "subject": f"Invitation: {PROJECT} steering meeting",
     "invitation": {"uid": f"steering@{people.DOMAIN}", "start": day(6, 18, 15, 0),
                    "location": "Board room",
                    "description": "Please bring the canteen figures for the second site."},
     "body": ""},
    {"key": "site-plan", "folder": "Inbox", "when": day(6, 12, 14, 30),
     "frm": BOB, "to": [people.ME], "shape": "html",
     "subject": "Site plan for the second site",
     "body": "<p>The <b>blueprint</b> of the second site is in the portal now.</p>"},
    {"key": "fwd-terms", "folder": "Inbox", "when": day(6, 13, 8, 15),
     "frm": BOB, "to": [people.ME],
     "subject": "FW: Terms for the devices",
     "body": f"""________________________________
From: {DANA[0]} <{DANA[1]}>
Sent: Monday, 12 June 10:00
Subject: Terms for the devices

The warranty covers every device for three years."""},
    {"key": "photo", "folder": "Inbox", "when": day(3, 21, 9, 30),
     "frm": BOB, "to": [people.ME, CARLA],
     "subject": "Whiteboard photo from the kickoff",
     "body": "",
     "attachments": [("kickoff.png", PNG_8x8)]},
]

# A mail that is no longer in the mailbox but stays in the archive: it is
# what the "no longer at Microsoft" filter and the state of a deleted item
# are there for.
MAIL_GONE = "visit"


def _message_id(key):
    return f"<{key}.{people.PROJECT.lower()}@{people.DOMAIN}>"


def _chain(entry):
    """The keys of the mails this one answers, oldest first."""
    nach_key = {m["key"]: m for m in MAILS}
    kette = []
    vorher = nach_key.get(entry.get("reply_to"))
    while vorher is not None:
        kette.insert(0, vorher["key"])
        vorher = nach_key.get(vorher.get("reply_to"))
    return kette


def _mime(entry):
    """One mail as MIME, the way Graph hands it over."""
    msg = EmailMessage()
    msg["Message-ID"] = _message_id(entry["key"])
    msg["Date"] = format_datetime(entry["when"].replace(tzinfo=UTC))
    msg["From"] = addr(entry["frm"])
    msg["To"] = ", ".join(addr(p) for p in entry["to"])
    if entry.get("cc"):
        msg["Cc"] = ", ".join(addr(p) for p in entry["cc"])
    if entry.get("bcc"):
        msg["Bcc"] = ", ".join(addr(p) for p in entry["bcc"])
    if entry.get("reply_to"):
        # In-Reply-To names the mail answered, References the whole chain
        # with the first mail at its head – that head is what the index
        # takes as the conversation, so every reply has to carry it.
        msg["In-Reply-To"] = _message_id(entry["reply_to"])
        msg["References"] = " ".join(_message_id(k) for k in _chain(entry))
    msg["Subject"] = entry["subject"]
    shape = entry.get("shape")
    if shape == "invitation":
        # As Outlook sends it: an empty text part beside the calendar one.
        msg.set_content("")
        msg.add_alternative(_invitation(entry), subtype="calendar",
                            params={"method": "REQUEST"})
    elif shape == "html":
        # An empty text part beside the HTML that holds the words.
        msg.set_content("")
        msg.add_alternative(entry["body"], subtype="html")
    else:
        msg.set_content(entry["body"])
    for name, content in entry.get("attachments") or []:
        if isinstance(content, bytes):
            msg.add_attachment(content, maintype="image", subtype="png", filename=name)
            continue
        subtype = {"csv": "csv", "md": "markdown"}.get(name.rsplit(".", 1)[-1], "plain")
        msg.add_attachment(content.encode("utf-8"), maintype="text",
                           subtype=subtype, filename=name)
    return msg.as_bytes()


def _invitation(entry):
    """The text/calendar part of an invitation mail."""
    inv = entry["invitation"]
    start = inv["start"].strftime("%Y%m%dT%H%M%SZ")
    ende = (inv["start"] + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
    organizer = entry["frm"]
    return "\r\n".join([
        "BEGIN:VCALENDAR", "METHOD:REQUEST", "BEGIN:VEVENT",
        f"UID:{inv['uid']}", f"SUMMARY:{entry['subject'].split(': ', 1)[-1]}",
        f"DTSTART:{start}", f"DTEND:{ende}",
        f"ORGANIZER;CN={organizer[0]}:mailto:{organizer[1]}",
        f"LOCATION:{inv['location']}", f"DESCRIPTION:{inv['description']}",
        "END:VEVENT", "END:VCALENDAR", ""])


def mail_rel(entry):
    """Where the mail lies, below the export root – the exporter's scheme."""
    name = (f"{entry['when']:%Y-%m-%d_%H%M}__"
            f"{export_util.safe(entry['subject'], 90)}__"
            f"{export_util.kuerzel(_message_id(entry['key']))}.eml")
    return f"{outlook_export.MAIL_DIR}/{entry['folder']}/{name}"


def _done_log(root):
    """Outlook's resume log – the app's own writer, because the archive
    check holds every .eml, .ics and .vcf against it (a file no record
    knows is a finding, and rightly so)."""
    return state_db.DbDoneLog(state_db.StateDb(root))


def write_mail(root):
    written = []
    log = _done_log(root)
    for entry in MAILS:
        rel = mail_rel(entry)
        written.append(write(root / rel, _mime(entry)))
        log.mark(_message_id(entry["key"]), rel)
    log.close()
    # The folder tree, as a sync would have left it: the settings show the
    # rules against it, and "Show export list" has something to list.
    tree, seen = [], {}
    for entry in MAILS:
        seen[entry["folder"]] = seen.get(entry["folder"], 0) + 1
    for i, (folder, count) in enumerate(sorted(seen.items())):
        tree.append({"id": f"mailfolder-{i:02d}",
                     "pfad": f"{outlook_export.MAIL_DIR}/{folder}",
                     "anzahl": count})
    folders.speichere(root, tree)
    # One mail deleted at Microsoft, kept here.
    gone = next(e for e in MAILS if e["key"] == MAIL_GONE)
    state_db.StateDb(root).verschwunden_ergaenzen(
        [mail_rel(gone)], day(6, 12).replace(tzinfo=UTC).isoformat(timespec="seconds"))
    return written


# ---------------------------------------------------------------------------
# Outlook: calendar
# ---------------------------------------------------------------------------
def _event(key, calendar, subject, start, minutes, organizer, attendees,
           location="", body="", all_day=False, rrule=None):
    """A Graph event – built here, turned into .ics by the exporter."""
    ende = start + timedelta(days=1 if all_day else 0, minutes=0 if all_day else minutes)
    def node(when):
        return {"dateTime": when.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                "timeZone": "UTC"}
    ev = {"id": f"event-{key}", "iCalUId": f"{key}@{people.DOMAIN}",
          "subject": subject, "isAllDay": all_day,
          "start": node(start), "end": node(ende),
          "location": {"displayName": location},
          "body": {"contentType": "text", "content": body},
          "organizer": {"emailAddress": {"name": organizer[0], "address": organizer[1]}},
          "attendees": [{"type": "required",
                         "emailAddress": {"name": n, "address": m}}
                        for n, m in attendees],
          "showAs": "busy", "isCancelled": False,
          "createdDateTime": graph_time(start - timedelta(days=7)),
          "lastModifiedDateTime": graph_time(start - timedelta(days=3))}
    if rrule:
        ev["recurrence"] = rrule
    return ev


EVENTS = [
    ("Calendar", _event(
        "offer-review", "Calendar", f"{OFFER} – review with Dana",
        day(2, 12, 10, 0), 60, people.ME, [DANA, BOB],
        location="Meeting room 2",
        body=f"Walk through the proposal for {PROJECT} and decide on the "
             f"open points: test window and hardware.")),
    ("Calendar", _event(
        "kickoff", "Calendar", f"{PROJECT} kickoff",
        day(3, 20, 13, 0), 90, BOB, [people.ME, CARLA, DANA],
        location="Meeting room 1",
        body="Schedule, roles, first stage. Minutes by Bob.")),
    ("Calendar", _event(
        "budget", "Calendar", f"Budget approval {PROJECT}",
        day(4, 8, 9, 0), 30, CARLA, [people.ME], location="Carla's office")),
    ("Calendar", _event(
        "workshop", "Calendar", "Workshop with Greta Gast",
        day(5, 7), 0, people.ME, [GRETA], location="Training room",
        body="All-day workshop, visitor badge arranged.", all_day=True)),
    (PROJECT, _event(
        "walkthrough", PROJECT, "Test protocol walkthrough",
        day(6, 4, 11, 0), 60, BOB, [people.ME, DANA], location="Lab")),
    (PROJECT, _event(
        "steering", PROJECT, f"{PROJECT} steering meeting",
        day(6, 18, 15, 0), 60, CARLA, [people.ME, BOB], location="Board room",
        body="Status, network change, rollout of the second site.")),
    (PROJECT, _event(
        "rollout-start", PROJECT, f"Rollout start {PROJECT}",
        day(7, 1, 9, 0), 120, people.ME, [BOB, DANA, ERIK],
        location="Site 1")),
    ("Calendar", _event(
        "jour-fixe", "Calendar", f"{PROJECT} jour fixe",
        day(4, 6, 8, 30), 30, people.ME, [BOB, CARLA],
        location="Online",
        rrule={"pattern": {"type": "weekly", "interval": 1,
                           "daysOfWeek": ["monday"]},
               "range": {"type": "numbered", "numberOfOccurrences": 8}})),
]


def write_calendar(root):
    written, inventory = [], []
    log = _done_log(root)
    for calendar, ev in EVENTS:
        rel = (f"{outlook_export.KALENDER_DIR}/{export_util.safe(calendar)}/"
               f"{outlook_export.event_filename(ev)}")
        written.append(write(root / rel, outlook_export.build_ics(ev)))
        log.mark(ev["id"], rel)
    log.close()
    for i, name in enumerate(dict.fromkeys(c for c, _ in EVENTS)):
        inventory.append({"id": f"calendar-{i:02d}",
                          "pfad": f"{outlook_export.KALENDER_DIR}/{export_util.safe(name)}",
                          "anzahl": sum(1 for c, _ in EVENTS if c == name)})
    folders.speichere(root, inventory, datei=folders.KALENDER)
    return written


# ---------------------------------------------------------------------------
# Outlook: contacts
# ---------------------------------------------------------------------------
CONTACTS = [
    {"id": "contact-bob", "displayName": BOB[0], "givenName": "Bob",
     "surname": "Baumeister", "companyName": people.COMPANY,
     "department": "IT", "jobTitle": "Systems engineer",
     "emailAddresses": [{"address": BOB[1], "name": BOB[0]}],
     "businessPhones": ["+49 30 000000 12"],
     "personalNotes": f"Technical lead for {PROJECT}."},
    {"id": "contact-carla", "displayName": CARLA[0], "givenName": "Carla",
     "surname": "Chef", "companyName": people.COMPANY,
     "department": "Management", "jobTitle": "Head of operations",
     "emailAddresses": [{"address": CARLA[1], "name": CARLA[0]}],
     "businessPhones": ["+49 30 000000 01"],
     "personalNotes": "Approves the budget."},
    {"id": "contact-dana", "displayName": DANA[0], "givenName": "Dana",
     "surname": "Dienstleister", "companyName": "Beispiel GmbH",
     "jobTitle": "Project manager",
     "emailAddresses": [{"address": DANA[1], "name": DANA[0]}],
     "businessPhones": ["+49 40 000000 77"], "mobilePhone": "+49 170 0000077",
     "personalNotes": f"Service provider for {PROJECT}, wrote {OFFER}."},
    {"id": "contact-erik", "displayName": ERIK[0], "givenName": "Erik",
     "surname": "Einkauf", "companyName": "Beispiel GmbH",
     "jobTitle": "Purchasing",
     "emailAddresses": [{"address": ERIK[1], "name": ERIK[0]}],
     "businessPhones": ["+49 40 000000 12"],
     "personalNotes": "Orders the hardware."},
    {"id": "contact-greta", "displayName": GRETA[0], "givenName": "Greta",
     "surname": "Gast", "companyName": "Beispiel GmbH",
     "jobTitle": "Trainer",
     "emailAddresses": [{"address": GRETA[1], "name": GRETA[0]}],
     "personalNotes": "Runs the workshop."},
    {"id": "contact-desk", "displayName": f"{people.COMPANY} service desk",
     "companyName": people.COMPANY, "jobTitle": "Service desk",
     "emailAddresses": [{"address": f"service.desk@{people.DOMAIN}",
                         "name": f"{people.COMPANY} service desk"}],
     "businessPhones": ["+49 30 000000 99"]},
]


def write_contacts(root):
    written = []
    log = _done_log(root)
    for c in CONTACTS:
        rel = f"kontakte/{outlook_export.contact_filename(c)}"
        written.append(write(root / rel, outlook_export.build_vcf(c)))
        log.mark(c["id"], rel)
    log.close()
    return written


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------
def _msg(key, who, when, text, html=False):
    """One chat message. `html` is what Teams passes on when someone pastes
    something in – markup and all, headings included."""
    return {"id": f"msg-{key}", "messageType": "message",
            "createdDateTime": graph_time(when),
            "from": {"user": {"displayName": who[0]}},
            "body": {"contentType": "html" if html else "text", "content": text}}


CONVERSATIONS = [
    {"id": "chat-bob", "kind": "1on1", "title": BOB[0],
     "subtitle": f"1:1 with {BOB[0]}",
     "messages": [
         _msg("b1", people.ME, day(3, 23, 8, 15),
              f"Morning Bob – stage 1 of {PROJECT} starts today. Do you have "
              "the list of workplaces?"),
         _msg("b2", BOB, day(3, 23, 8, 18),
              "Yes, 18 at site 1. I will put it in the notebook."),
         _msg("b3", people.ME, day(4, 15, 14, 2),
              "The docking stations are short by two. Erik has reordered."),
         _msg("b4", BOB, day(4, 15, 14, 9),
              "Fine, we can start without them, they are for the spare desks."),
         _msg("b5", BOB, day(6, 2, 16, 30),
              "Test protocol done. Printer mapping fails on the two old "
              "devices, Dana is on it."),
         _msg("b6", people.ME, day(6, 2, 16, 41),
              "Noted, I will put it in the status for Thursday."),
         # Deleted at Microsoft the next morning – the archive keeps what
         # it said (DELETED below).
         _msg("b7", BOB, day(6, 2, 16, 45),
              "Could we leave the two old printers out of the audit?"),
     ],
     "attachments": [("test-protocol.txt", day(6, 2, 16, 30), f"""Test protocol {PROJECT}
Workplace setup        passed
Network drives         passed
Printer mapping        failed on 2 of 18 devices
Mail profile           passed
""")]},

    {"id": "chat-group", "kind": "group", "title": f"{PROJECT} core team",
     "subtitle": f"{ME}, {BOB[0]}, {CARLA[0]}",
     "messages": [
         _msg("g1", CARLA, day(4, 8, 9, 40),
              f"Budget for {PROJECT} is through. Well done everyone."),
         _msg("g2", people.ME, day(4, 8, 9, 44),
              "Thanks Carla. Then we order the rest of the hardware today."),
         _msg("g3", BOB, day(5, 5, 18, 0),
              "Status for week 19 is out. One risk: the network change at "
              "site 2 has no date yet."),
         _msg("g4", CARLA, day(5, 6, 7, 30),
              "I will ask the network team and come back on Thursday."),
         _msg("g5", people.ME, day(6, 10, 8, 0),
              "Steering meeting on Thursday at three, agenda is status and "
              "the network change."),
         # Pasted in from a document, heading and all: the archive keeps it
         # as Teams passed it on, and the heading belongs to this message –
         # not to the name of the chat.
         _msg("g6", CARLA, day(6, 11, 9, 15),
              f"<h1>Executive summary</h1><p>{PROJECT} is on plan: site 1 "
              "is done, site 2 waits for the network change. The budget "
              "holds.</p>", html=True),
     ]},

    {"id": "chat-meeting", "kind": "meeting", "title": f"{PROJECT} kickoff",
     "subtitle": "Meeting chat",
     "messages": [
         _msg("m1", BOB, day(3, 20, 13, 2), "Recording is off, minutes by me."),
         _msg("m2", DANA, day(3, 20, 13, 20),
              "Updated schedule follows this week."),
         _msg("m3", people.ME, day(3, 20, 13, 55),
              "Summary: start 23 March, hardware on our side, test window "
              "stays in May."),
     ]},

    {"id": "channel-general", "kind": "channels", "team": f"{people.COMPANY} {PROJECT}",
     "title": "General", "subtitle": f"Channel of {people.COMPANY} {PROJECT}",
     "messages": [
         _msg("c1", people.ME, day(3, 23, 9, 0),
              f"Channel for {PROJECT}. Everything about the rollout goes here."),
         _msg("c2", BOB, day(4, 2, 11, 15),
              "Workplace list for site 1 is in the files tab."),
         _msg("c3", DANA, day(5, 18, 9, 0),
              "Rollout plan, second draft, is uploaded."),
         _msg("c4", people.ME, day(6, 3, 17, 20),
              "Site 1 is done, 18 workplaces, no open incidents."),
     ],
     "files": [("workplaces-site-1.csv", day(4, 2, 11, 15), """room;user;device
1.01;Bob Baumeister;Laptop 14"
1.02;Alice Beispiel;Laptop 14"
1.03;spare;Laptop 14"
""")]},
]


# What happened to messages after they were first archived: an edit keeps
# the earlier text in the store (teams_export.EARLIER), a deletion keeps
# the text it had. message id -> (earlier text, when it was edited) and
# message id -> when it was deleted.
EDITED = {"msg-b5": ("Test protocol done. Everything passed.", day(6, 2, 17, 5))}
DELETED = {"msg-b7": day(6, 3, 9, 0)}


def _conversation_store(db, conv):
    """The conversation's message store as two runs of the export would
    leave it: the first saw every message as it was then, the second the
    edit and the deletion. Returns the stored messages in order."""
    area = conv["id"]
    store = teams_export.Nachrichtenspeicher(db, area)
    first = []
    for m in conv["messages"]:
        if m["id"] in EDITED:
            m = dict(m, body={"contentType": "text", "content": EDITED[m["id"]][0]})
        first.append(m)
    store.merge(first)
    later = []
    for m in conv["messages"]:
        if m["id"] in EDITED:
            later.append(dict(m, lastModifiedDateTime=graph_time(EDITED[m["id"]][1])))
        if m["id"] in DELETED:
            # Graph hands a deleted message out without its words.
            later.append(dict(m, body={"contentType": "text", "content": ""},
                              deletedDateTime=graph_time(DELETED[m["id"]]),
                              lastModifiedDateTime=graph_time(DELETED[m["id"]])))
    store.merge(later)
    store.sichern()
    return sorted(store.nachrichten(), key=lambda m: m.get("createdDateTime") or "")


def _conversation_rel(conv):
    stem = export_util.safe(conv["title"])
    short = export_util.kuerzel(conv["id"])
    if conv["kind"] == "channels":
        return f"channels/{export_util.safe(conv['team'])}/{stem}__{short}.html"
    return f"{conv['kind']}/{stem}__{short}.html"


def write_teams(root):
    # What the export writes into a conversation itself (a deleted
    # message's mark) speaks the archive's language.
    language, teams_export.LANG = teams_export.LANG, "en"
    try:
        return _write_teams(root)
    finally:
        teams_export.LANG = language


def _write_teams(root):
    written, records, spiegel_bestand = [], {}, {}
    db = state_db.StateDb(root)
    for conv in CONVERSATIONS:
        rel = _conversation_rel(conv)
        zeiten = [m["createdDateTime"][:10] for m in conv["messages"]]
        meta = f"{len(conv['messages'])} messages · {zeiten[0]} – {zeiten[-1]}"
        blocks = [teams_export.render_message(m) for m in _conversation_store(db, conv)]
        html = teams_export.render_conversation(conv["title"], conv["subtitle"],
                                                meta, blocks)
        written.append(write(root / rel, html))
        key = f"ch:{conv['id']}" if conv["kind"] == "channels" else conv["id"]
        records[key] = json.dumps({"rel": rel, "done": True,
                                   "titel": conv["title"]}, ensure_ascii=False)
        # Files shared in a chat sit beside the conversation, recorded
        # under the link they came from (kv files:<key>) – that is what
        # the archive check holds them against.
        dateien = {}
        for name, when, text in conv.get("attachments") or []:
            ziel = (f"{teams_export.anhang_ordner(rel)}/{Path(name).stem}"
                    f"__{export_util.kuerzel(name)}{Path(name).suffix}")
            written.append(write(root / ziel, text, when))
            dateien[f"{people.SHAREPOINT_SITE}/Freigegeben/{name}"] = {
                "rel": ziel, "ctag": f"ctag-{export_util.kuerzel(name)}",
                "size": len(text.encode("utf-8"))}
        if dateien:
            db.kv_schreiben(f"files:{key}", json.dumps(dateien, ensure_ascii=False))
        if conv.get("files"):
            # A standard channel's file tab is a mirror of the team's
            # library: its root is the team folder, the files sit below
            # Dateien/<channel>, and the bookkeeping lies at the root – not
            # inside Dateien, or the index would read it as a file. Several
            # channels share one team, so the inventory is collected and
            # written once per mirror; writing it per channel would leave
            # the earlier channels' files without a record.
            spiegel = root / "channels" / export_util.safe(conv["team"])
            bestand = spiegel_bestand.setdefault(spiegel, {})
            for name, when, text in conv["files"]:
                datei_rel = f"Dateien/{conv['title']}/{name}"
                written.append(write(spiegel / datei_rel, text, when))
                bestand[f"teamsfile-{export_util.kuerzel(datei_rel)}"] = {
                    "rel": datei_rel, "ctag": f"ctag-{export_util.kuerzel(datei_rel)}",
                    "size": len(text.encode("utf-8"))}
    for spiegel, bestand in spiegel_bestand.items():
        state_db.StateDb(spiegel).bestand_schreiben(bestand)
    db.saetze_schreiben("conversations", records)
    return written


# ---------------------------------------------------------------------------
# The mirrors: OneDrive and SharePoint
# ---------------------------------------------------------------------------
ONEDRIVE_FILES = [
    ("Documents/Ostwind/rollout-plan.md", day(5, 18, 8, 35), f"""# {PROJECT} rollout plan

Draft 2, as sent by Dana Dienstleister.

## Site 1
* 2 June   preparation, backup
* 3 June   rollout, 18 workplaces

## Site 2
* 15 June  wave 1
* 22 June  wave 2, after the network change
"""),
    ("Documents/Ostwind/test-protocol.md", day(6, 2, 17, 0), f"""# Test protocol {PROJECT}

| check | result |
|---|---|
| workplace setup | passed |
| network drives | passed |
| printer mapping | failed on two devices |
| mail profile | passed |
"""),
    ("Documents/Offers/offer-4711.txt", day(2, 10, 9, 20), f"""{OFFER}
Project: {PROJECT}
Stage 1  Analysis and setup      12 days
Stage 2  Rollout, first site     18 days
Stage 3  Handover and training    5 days
"""),
    ("Documents/Notes/meeting-notes.txt", day(3, 20, 15, 0), f"""Kickoff {PROJECT}

- start 23 March
- hardware on our side
- test window stays in May
- next steering meeting in June
"""),
    ("Pictures/logo.png", day(1, 8, 10, 0), PNG_8x8),
    ("Archive/old-budget.csv", day(1, 15, 9, 0), """year;budget
2025;120000
2026;150000
"""),
]

# The file that is no longer in the drive – it stays in the archive and is
# marked, which is the point of keeping one.
ONEDRIVE_GONE = "Dateien/Archive/old-budget.csv"

SHAREPOINT_FILES = [
    ("Ostwind/specification.md", day(3, 25, 10, 0), f"""# {PROJECT} specification

The workplace standard for the rollout: hardware, software, network
drives and the printer mapping every site gets.
"""),
    ("Ostwind/risk-log.csv", day(5, 5, 18, 10), """risk;likelihood;impact;owner
network change site 2;medium;high;Carla Chef
printer mapping old devices;high;low;Bob Baumeister
delivery of spare devices;low;low;Erik Einkauf
"""),
    ("Templates/offer-template.txt", day(1, 20, 8, 0), """Offer <number>
Project: <name>
Stage 1  ...
"""),
]

SHAREPOINT_SITE_DIR = people.COMPANY
SHAREPOINT_LIBRARY = "Documents"


def write_onedrive(root):
    written, bestand = [], {}
    for i, (rel, when, content) in enumerate(ONEDRIVE_FILES):
        written.append(write(root / "Dateien" / rel, content, when))
        bestand[f"drive-item-{i:02d}"] = {
            "rel": f"Dateien/{rel}", "ctag": f"ctag-{i:02d}",
            "size": len(content if isinstance(content, bytes)
                        else content.encode("utf-8"))}
    db = state_db.StateDb(root)
    db.bestand_schreiben(bestand)
    db.verschwunden_ergaenzen(
        [ONEDRIVE_GONE], day(6, 1).replace(tzinfo=UTC).isoformat(timespec="seconds"))
    return written


def write_sharepoint(root):
    written, bestand = [], {}
    library = root / SHAREPOINT_SITE_DIR / SHAREPOINT_LIBRARY
    for i, (rel, when, content) in enumerate(SHAREPOINT_FILES):
        written.append(write(library / "Dateien" / rel, content, when))
        bestand[f"sp-item-{i:02d}"] = {
            "rel": f"Dateien/{rel}", "ctag": f"ctag-{i:02d}",
            "size": len(content if isinstance(content, bytes)
                        else content.encode("utf-8"))}
    state_db.StateDb(library).bestand_schreiben(bestand)
    return written


# ---------------------------------------------------------------------------
# SharePoint pages
# ---------------------------------------------------------------------------
PAGES = [
    ("home", "Home", day(1, 12, 9, 0), f"""
<p>Welcome to the {people.COMPANY} team site. Everything about the running
projects is here.</p>
<ul><li><a href="Project-Ostwind.html">Project {PROJECT}</a></li>
<li><a href="Offer-process.html">How we handle offers</a></li></ul>"""),
    ("project", f"Project {PROJECT}", day(5, 20, 11, 0), f"""
<p>{PROJECT} replaces the workplace standard at both sites. Stage 1 is
finished, the rollout of site 1 ran on 3 June.</p>
<h2>Documents</h2>
<ul><li>Specification</li><li>Rollout plan</li><li>Risk log</li></ul>
<h2>Who is involved</h2>
<p>{ME} (lead), {BOB[0]} (technical), {CARLA[0]} (budget),
Dana Dienstleister (service provider).</p>"""),
    ("offers", "Offer process", day(2, 3, 8, 0), f"""
<p>An offer is checked by the project lead, then by operations. {OFFER} is
the current example: proposal, two open points, decision in the kickoff.</p>"""),
]


def page_rel(title):
    """Where a page lies below the pages folder."""
    name = export_util.safe(title.replace(" ", "-"), 100) + ".html"
    return f"{export_util.safe(people.COMPANY)}/{name}"


def page_html(title, when, body):
    """A page as the export writes it – its earlier versions are built the
    same way (testdata/history.py)."""
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f"<title>{title}</title></head><body>"
            f"<h1>{title}</h1>"
            f'<p class="meta">{graph_time(when)}</p>{body}</body></html>')


def write_pages(root):
    written, seiten = [], {}
    for key, title, when, body in PAGES:
        rel = page_rel(title)
        written.append(write(root / rel, page_html(title, when, body), when))
        seiten[f"page-{key}"] = {"rel": rel, "etag": f"etag-{key}"}
    state_db.StateDb(root).seiten_schreiben(seiten)
    return written


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
# The ids the tasks are assigned to: Graph hands out user ids, the export
# keeps a name for each so a board does not read as a list of ids.
# One id per person, the way Graph hands them out – the whole cast, so a
# card of any board can be assigned to whoever is on it.
PLANNER_NAMES = {f"user-{name.split()[0].lower()}": name
                 for name, _mail in people.EVERYONE}

PLAN = {"id": "plan-ostwind", "titel": f"{PROJECT} board"}
BUCKETS = [
    {"id": "bucket-backlog", "name": "Backlog", "orderHint": "1"},
    {"id": "bucket-doing", "name": "In progress", "orderHint": "2"},
    {"id": "bucket-done", "name": "Done", "orderHint": "3"},
]
PLANNER_LABELS = {"category1": "rollout", "category2": "risk"}

PLANNER_TASKS = [
    ("task-order", "bucket-done", "Order the hardware", ["user-bob"],
     day(3, 3, 10, 30),
     "Six laptops, five docking stations, five monitors, six headsets.",
     {"c1": "quantities checked", "c2": "order released"},
     {f"{people.SHAREPOINT_SITE}/Documents/order-list.csv": "order-list.csv"},
     [("user-alice", day(3, 4, 8, 0), "Released, delivery in four weeks.")]),
    ("task-stage1", "bucket-done", f"{PROJECT} stage 1: analysis and setup",
     ["user-bob", "user-alice"], day(3, 23, 8, 0),
     "Workplace standard defined, images built, 18 workplaces prepared.",
     {"c1": "image built", "c2": "drives mapped"},
     {}, [("user-bob", day(4, 30, 16, 0), "Finished one day early.")]),
    ("task-protocol", "bucket-done", "Walk through the test protocol",
     ["user-bob"], day(5, 20, 9, 0),
     "Together with Dana Dienstleister, on two old and two new devices.",
     {"c1": "workplace setup", "c2": "printer mapping"},
     {f"{people.SHAREPOINT_SITE}/Documents/test-protocol.txt": "test-protocol.txt"},
     [("user-bob", day(6, 2, 16, 35),
       "Printer mapping fails on the two old devices. Does not block us.")]),
    ("task-site1", "bucket-doing", "Rollout site 1", ["user-alice"],
     day(5, 25, 9, 0),
     "18 workplaces, one day. Backup the evening before.",
     {"c1": "backup", "c2": "rollout", "c3": "walkthrough"}, {},
     [("user-alice", day(6, 3, 18, 0), "Done, no open incidents.")]),
    ("task-network", "bucket-doing", "Get a date for the network change",
     ["user-carla"], day(5, 6, 7, 35),
     "Site 2 cannot go in one wave before the network team has a date. "
     "This is the one risk in the status.",
     {}, {}, [("user-carla", day(6, 10, 9, 0), "Network team answers this week.")]),
    ("task-site2", "bucket-backlog", "Rollout site 2, two waves",
     ["user-alice", "user-bob"], day(6, 8, 9, 0),
     "Nine workplaces per wave, the second one after the network change.",
     {"c1": "wave 1", "c2": "wave 2"},
     {f"{people.SHAREPOINT_SITE}/Documents/rollout-plan.md": "rollout-plan.md"}, []),
    ("task-training", "bucket-backlog", "Training for the new workplaces",
     ["user-carla"], day(6, 12, 10, 0),
     "Greta Gast runs it, two sessions per site.", {}, {}, []),
]


def _planner_entries(plan_id, tasks):
    eintraege = {}
    for (tid, bucket, title, assignees, created, description,
         checklist, references, comments) in tasks:
        eintraege[tid] = {
            "etag": f'W/"{tid}"',
            "task": {"id": tid, "title": title, "bucketId": bucket,
                     "planId": plan_id, "orderHint": tid,
                     "createdDateTime": graph_time(created),
                     "percentComplete": 100 if bucket == "bucket-done" else 0,
                     "assignments": {a: {"orderHint": a} for a in assignees}},
            "details": {
                "description": description,
                "checklist": {k: {"title": v, "isChecked": bucket == "bucket-done"}
                              for k, v in checklist.items()},
                "references": {url: {"alias": alias, "type": "Other"}
                               for url, alias in references.items()}},
            "kommentare": [{"wer": wer, "wann": graph_time(wann), "art": "neu",
                            "html": f"<div>{text}</div>"}
                           for wer, wann, text in comments]}
    return eintraege


# What a card refers to, downloaded beside the board – that is what makes
# the board an entry in the file browser.
PLANNER_ATTACHMENTS = [
    ("order-list.csv", day(3, 3, 10, 30), """item;quantity;note
Laptop 14";6;one spare
Docking station;5;
Monitor 27";5;
Headset;6;one spare
"""),
    ("test-protocol.txt", day(6, 2, 16, 30), f"""Test protocol {PROJECT}
Workplace setup        passed
Network drives         passed
Printer mapping        failed on 2 of 18 devices
Mail profile           passed
"""),
]


# Every board the archive holds: the story's, and the ones the volume
# adds. A board is its plan, its buckets, its labels, its tasks and what
# its cards refer to – one folder each, as the export writes them.
PLANS = [{"plan": PLAN, "buckets": BUCKETS, "labels": PLANNER_LABELS,
          "tasks": PLANNER_TASKS, "attachments": PLANNER_ATTACHMENTS}]


def write_planner(root):
    written = []
    for board in PLANS:
        plan, buckets_list = board["plan"], board["buckets"]
        ordner = root / f"{export_util.safe(plan['titel'])}__{export_util.kuerzel(plan['id'])}"
        eintraege = _planner_entries(plan["id"], board["tasks"])
        buckets = {b["id"]: b for b in buckets_list}
        stand = graph_time(day(6, 12, 9, 0))
        html = planner_export.render_board(plan, buckets, eintraege,
                                           board["labels"], PLANNER_NAMES, stand)
        written.append(write(ordner / "board.html", html, day(6, 12, 9, 0)))
        db = state_db.StateDb(ordner)
        db.kv_schreiben("plan", json.dumps(
            {"id": plan["id"], "titel": plan["titel"], "labels": board["labels"],
             "buckets": {b["id"]: b["name"] for b in buckets_list}},
            ensure_ascii=False))
        db.kv_schreiben("namen", json.dumps(PLANNER_NAMES, ensure_ascii=False,
                                            sort_keys=True))
        db.kv_schreiben("stand", stand)
        # What a card refers to and the export fetched: the file, the
        # record the archive check reads (kv "anhaenge"), and the link on
        # the card itself – without the last one a board would send the
        # reader back into the cloud for a file that lies beside it.
        anhaenge = {}
        for name, when, text in board["attachments"]:
            url = f"{people.SHAREPOINT_SITE}/Documents/{name}"
            ziel = (f"{Path(name).stem}__"
                    f"{export_util.kuerzel(url)}{Path(name).suffix}")
            rel = f"{planner_export.ANHANG_DIR}/{ziel}"
            written.append(write(ordner / rel, text, when))
            anhaenge[url] = {"rel": rel, "ctag": f"ctag-{export_util.kuerzel(url)}"}
        for eintrag in eintraege.values():
            lokal = {url: anhaenge[url]["rel"]
                     for url in eintrag["details"]["references"] if url in anhaenge}
            if lokal:
                eintrag["anhaenge"] = lokal
        db.kv_schreiben("tasks", json.dumps(eintraege, ensure_ascii=False))
        db.kv_schreiben("anhaenge", json.dumps(anhaenge, ensure_ascii=False))
    state_db.StateDb(root).saetze_schreiben("namen", dict(PLANNER_NAMES))
    return written


# ---------------------------------------------------------------------------
# To Do
# ---------------------------------------------------------------------------
TODO_LISTS = [
    {"id": "list-tasks", "titel": "Tasks", "art": "defaultList", "geteilt": False,
     "tasks": [
         ("todo-badge", "Visitor badge for Greta Gast", "completed",
          day(4, 22, 11, 30), "Arrange a badge for 7 May.", [], []),
         ("todo-status", "Write the status for week 19", "completed",
          day(5, 5, 17, 0), "Stage 1, hardware, risks.", [], []),
         ("todo-board", "Prepare the board report", "notStarted",
          day(6, 11, 8, 0), "Carla needs it for July.",
          ["numbers from the budget", "one slide on the network change"], []),
         ("todo-call", "Call the network team", "notStarted",
          day(6, 12, 9, 0), "About the date for site 2.", [], []),
     ]},
    {"id": "list-ostwind", "titel": PROJECT, "art": "list", "geteilt": True,
     "tasks": [
         ("todo-spare", "Reorder two docking stations", "completed",
          day(4, 15, 14, 30), "Erik has the order.", [], []),
         ("todo-printer", "Follow up the printer mapping", "notStarted",
          day(6, 2, 17, 0),
          "Fails on the two old devices, Dana is looking at it.",
          ["ask Dana for a date"], [("test-protocol.txt", 212)]),
         ("todo-wave2", "Plan wave 2 at site 2", "notStarted",
          day(6, 8, 9, 30), "After the network change, nine workplaces.",
          ["dates", "who is on site"], []),
         ("todo-old", "Collect the old devices", "notStarted",
          day(5, 2, 9, 0), "Thirteen devices from site 1.", [], []),
     ],
     # One task that is no longer in the list – it stays, marked.
     "gone": "todo-old"},
]


def write_todo(root):
    written = []
    for liste in TODO_LISTS:
        ordner = (root /
                  f"{export_util.safe(liste['titel'])}__{export_util.kuerzel(liste['id'])}")
        eintraege = {}
        for tid, title, status, when, body, steps, attachments in liste["tasks"]:
            eintraege[tid] = {
                "etag": f'W/"{tid}"',
                "task": {"id": tid, "title": title, "status": status,
                         "importance": "normal",
                         "body": {"content": body, "contentType": "text"},
                         "checklistItems": [{"displayName": s, "isChecked": False}
                                            for s in steps],
                         "createdDateTime": graph_time(when),
                         "lastModifiedDateTime": graph_time(when)},
                "anhaenge": [{"id": f"att-{tid}", "name": name, "size": size,
                              "rel": f"Anhaenge/{export_util.kuerzel(tid)}_{name}"}
                             for name, size in attachments]}
            for name, _size in attachments:
                written.append(write(
                    ordner / "Anhaenge" / f"{export_util.kuerzel(tid)}_{name}",
                    f"Attachment of the task: {title}\n", when))
        if liste.get("gone"):
            eintraege[liste["gone"]]["deleted"] = graph_time(day(6, 9, 8, 0))
        stand = graph_time(day(6, 12, 9, 0))
        html = todo_export.render_list(liste, eintraege, stand)
        written.append(write(ordner / "list.html", html, day(6, 12, 9, 0)))
        db = state_db.StateDb(ordner)
        db.kv_schreiben("list", json.dumps(
            {"id": liste["id"], "titel": liste["titel"], "art": liste["art"],
             "geteilt": liste["geteilt"]}, ensure_ascii=False))
        db.kv_schreiben("tasks", json.dumps(eintraege, ensure_ascii=False))
        db.kv_schreiben("stand", stand)
    return written


# ---------------------------------------------------------------------------
# OneNote
# ---------------------------------------------------------------------------
NOTEBOOK = {"id": "notebook-ostwind", "titel": f"{PROJECT} notebook"}
ONENOTE_PAGES = [
    ("page-kickoff", "Meetings", "Kickoff minutes", day(3, 20, 15, 30), f"""
<h1>Kickoff minutes</h1>
<p>Present: {ME}, {CARLA[0]}, {BOB[0]}, Dana Dienstleister.</p>
<ol><li>Schedule accepted, stage 1 starts on 23 March.</li>
<li>Hardware is ordered by us.</li>
<li>Test window stays in May.</li>
<li>Next steering meeting in June.</li></ol>"""),
    ("page-steering", "Meetings", "Steering meeting, June", day(6, 18, 16, 30), f"""
<h1>Steering meeting, June</h1>
<p>Status of {PROJECT}: site 1 done, site 2 waiting for the network
change. Budget on plan.</p>
<p>Decision: site 2 goes in two waves.</p>"""),
    ("page-workplaces", "Rollout", "Workplace standard", day(3, 25, 10, 30), """
<h1>Workplace standard</h1>
<ul><li>Laptop 14", docking station, monitor 27"</li>
<li>Network drives: team, project, archive</li>
<li>Printer mapping by room</li></ul>"""),
    ("page-protocol", "Rollout", "Test protocol, results", day(6, 2, 17, 15), """
<h1>Test protocol, results</h1>
<p>Workplace setup, network drives and the mail profile passed on all
eighteen devices. The printer mapping failed on the two old ones.</p>"""),
]


def write_onenote(root):
    written, records = [], {}
    notebook = root / export_util.safe(NOTEBOOK["titel"])
    for key, section, title, when, body in ONENOTE_PAGES:
        page = {"id": key, "title": title,
                "createdDateTime": graph_time(when - timedelta(hours=2)),
                "lastModifiedDateTime": graph_time(when)}
        kopf = (f'<div class="mn-kopf"><b>{title}</b>'
                f'{NOTEBOOK["titel"]} › {section} · '
                f'erstellt {str(page["createdDateTime"])[:10]} · '
                f'geändert {str(page["lastModifiedDateTime"])[:10]}</div>')
        roh = (f'<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">'
               f"<title>{title}</title></head><body>{body}</body></html>")
        rel = (f"{export_util.safe(section)}/"
               f"{export_util.safe(title, 60)}__{export_util.kuerzel(key)}.html")
        written.append(write(notebook / rel, onenote_export.seite_html(roh, kopf), when))
        records[key] = json.dumps(
            {"rel": rel, "lm": page["lastModifiedDateTime"],
             "notebook": NOTEBOOK["titel"], "deleted": None}, ensure_ascii=False)
    state_db.StateDb(notebook).saetze_schreiben("pages", records)
    folders.speichere(root, [{"id": NOTEBOOK["id"],
                              "pfad": export_util.safe(NOTEBOOK["titel"]),
                              "anzahl": len(ONENOTE_PAGES)}],
                      datei=folders.NOTIZBUECHER)
    return written


# ---------------------------------------------------------------------------
# The volume
# ---------------------------------------------------------------------------
# Everything above is the story the archive is about: small enough to read
# in one go, and what the browser tests hold their numbers against. What a
# real archive has beside its story – a year of other traffic, other
# projects, other people – is generated: testdata/bulk.py says how, keeps
# away from the words the story owns, and derives every value from the
# position of its item, so this stays the same archive on every machine.
#
# A list grows to FACTOR times its length; where a container carries the
# items (a board, a task list), the containers grow more slowly than what
# is in them – fifty boards of seven cards would be a filing cabinet, not
# an archive.
MAILS += bulk.mails(bulk.more(MAILS))
EVENTS += bulk.events(_event, bulk.more(EVENTS))
CONTACTS += bulk.contacts(bulk.more(CONTACTS))
CONVERSATIONS += bulk.conversations(_msg, bulk.more(CONVERSATIONS))
# A picture comes back without content: the one PNG stands for all of them,
# so the archive holds a binary and not three hundred.
ONEDRIVE_FILES += [(rel, when, PNG_8x8 if text is None else text)
                   for rel, when, text in bulk.onedrive_files(bulk.more(ONEDRIVE_FILES))]
SHAREPOINT_FILES += [(rel, when, PNG_8x8 if text is None else text)
                     for rel, when, text in bulk.sharepoint_files(bulk.more(SHAREPOINT_FILES))]
PAGES += bulk.pages(bulk.more(PAGES))
PLANS += bulk.plans(9, 38)
TODO_LISTS += bulk.todo_lists(18, 22)
ONENOTE_PAGES += bulk.onenote_pages(bulk.more(ONENOTE_PAGES))


# ---------------------------------------------------------------------------
# The organization (org_export.py): the directory as users/delta names it
# ---------------------------------------------------------------------------
def _user(uid, name, title, department, manager=None, **extra):
    """One directory user in Graph's shape – the fields org_export selects,
    the manager as the id `apply` leaves behind."""
    first, _, last = name.partition(" ")
    return {"id": uid, "displayName": name, "jobTitle": title, "department": department,
            "officeLocation": "Hamburg", "companyName": people.COMPANY, "city": "Hamburg",
            "country": "Germany", "mail": people.ADDRESS.get(name) or
            f"{first.lower()}.{last.lower()}@{people.DOMAIN}",
            "accountEnabled": True, "userType": "Member", "manager": manager, **extra}


# The story: Carla leads Nordwind; Alice runs the project under Bob. Petra
# has left, but Hanno still reports to her: her disabled account holds his
# line, as Teams shows it. The three at the end are what the export leaves
# out – a disabled account nobody reports to, a guest, a service account
# with no place in the tree.
ORG_ME = "org-alice"
ORG_STORY = [
    _user("org-carla", CARLA[0], "Managing Director", "Executive"),
    _user("org-bob", BOB[0], "Head of Projects", "Projects", "org-carla"),
    _user(ORG_ME, ME, "Project Lead", "Projects", "org-bob"),
    _user("org-kai", "Kai Kalkulation", "Cost Engineer", "Projects", ORG_ME),
    _user("org-lena", "Lena Lager", "Logistics Planner", "Projects", ORG_ME),
    _user("org-nina", "Nina Netzwerk", "Network Architect", "Projects", "org-bob"),
    _user("org-frida", "Frida Finanz", "Head of Finance", "Finance", "org-carla"),
    _user("org-ines", "Ines Innendienst", "Sales Assistant", "Finance", "org-frida"),
    _user("org-malte", "Malte Marketing", "Head of Marketing", "Marketing", "org-carla"),
    _user("org-petra", "Petra Pause", "Head of Service", "Marketing", "org-malte",
          accountEnabled=False),
    _user("org-hanno", "Hanno Helpdesk", "Service Desk Lead", "Marketing", "org-petra"),
    _user("org-olaf", "Olaf Organisation", "Office Manager", "Executive", "org-carla",
          accountEnabled=False),
    _user("org-greta", GRETA[0], "Consultant", "", "org-bob", userType="Guest"),
    _user("org-scanner", "Scanner Service", "", ""),
]
ORG_LEFT_OUT = ("org-olaf", "org-greta", "org-scanner")
ORG_LINKS = ("org-petra",)          # disabled, kept for the line, counted as nobody
ORG_TOP = "org-carla"


def _org_volume():
    """The volume under the three department heads: eight each, named from
    two fixed lists so every build names them alike."""
    firsts = ["Anna", "Ben", "Clara", "David", "Eva", "Felix", "Hanna", "Jonas"]
    lasts = {"org-hanno": "Service", "org-ines": "Vertrieb", "org-nina": "Technik"}
    out = []
    for boss, last in lasts.items():
        dept = next(u["department"] for u in ORG_STORY if u["id"] == boss)
        out += [_user(f"{boss}-{i}", f"{first} {last}", "Specialist", dept, boss)
                for i, first in enumerate(firsts)]
    return out


ORG_USERS = {u["id"]: u for u in ORG_STORY + _org_volume()}


def org_users(changes=None):
    """The directory, optionally with some users' fields replaced."""
    users = {uid: dict(u) for uid, u in ORG_USERS.items()}
    for uid, fields in (changes or {}).items():
        users.setdefault(uid, {"id": uid}).update(fields)
    return users


def org_text(users):
    return organization.text_of(organization.build(users, me=ORG_ME))


def write_organization(root):
    """organization/organization.json as org_export writes it."""
    return [write(Path(root) / organization.ORG_DIR / organization.FILE,
                  org_text(org_users()), day(6, 14, 6, 0))]


# ---------------------------------------------------------------------------
# All of it
# ---------------------------------------------------------------------------
WRITERS = [
    (settings.OUTLOOK_DIR, write_mail),
    (settings.OUTLOOK_DIR, write_calendar),
    (settings.OUTLOOK_DIR, write_contacts),
    (settings.TEAMS_DIR, write_teams),
    (settings.TEAMS_DIR, write_organization),
    (settings.ONEDRIVE_DIR, write_onedrive),
    (settings.SHAREPOINT_DIR, write_sharepoint),
    (settings.SHAREPOINT_PAGES_DIR, write_pages),
    (settings.PLANNER_DIR, write_planner),
    (settings.TODO_DIR, write_todo),
    (settings.ONENOTE_DIR, write_onenote),
]


def write_all(exports):
    """Every source into its folder below `exports`; returns the file count."""
    exports = Path(exports)
    written = 0
    for ordner, writer in WRITERS:
        root = exports / ordner
        root.mkdir(parents=True, exist_ok=True)
        written += len(writer(root))
    return written
