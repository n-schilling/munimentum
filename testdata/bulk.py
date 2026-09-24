"""The volume around the story: the same archive, fifty times over.

`sources.py` holds what the synthetic archive is about – an offer, a
project, a rollout, a dozen mails one can read from end to end. That
story is small on purpose: the browser tests read it, and every number
they expect is derived from those lists. What it is not is an archive of
the size the app meets in reality, where a search has more hits than fit
on a page, a timeline needs its band and the people view has more than
five faces.

This module writes that size. Every list in `sources.py` ends with what
one of these generators produced: a year of everyday traffic among the
same cast, over the same eight sources, FACTOR times the story's volume.

Two rules keep it honest:

* **The story's words stay the story's.** RESERVED holds what the tests
  search for; nothing generated here may say it, so a search for
  "printer mapping" still finds the two items it was written for and no
  ranking turns on the mass around them.
* **Nothing is random.** Every value is derived from the position of its
  item, so the same archive comes out of every run and on every machine –
  what testdata/people.py promises for the dates holds for the rest.
"""

from datetime import timedelta

from testdata import people
from testdata.people import day

# How much bigger than the hand-written story the archive is. One number:
# the generators derive their counts from the lists they extend, so
# changing it here changes every source at once.
FACTOR = 50

# Everyone but the owner – the bulk is written with the whole cast, so
# every face in the people view stands for someone who really wrote.
CAST = [*people.COLLEAGUES, *people.EXTERNALS]
INSIDE = people.COLLEAGUES
OUTSIDE = people.EXTERNALS

# What the browser tests search for. The story owns these words; a
# generated item that used one would move the hit it is held to.
RESERVED = frozenset("""
ostwind offer 4711 proposal budget approved rollout printer mapping
protocol framework agreement executive summary visitor badge workshop
canteen blueprint warranty whiteboard
""".split())

# The other matters the archive knows: enough of them that a filter has
# something to narrow, all of them invented, none of them the story's.
PROJECTS = ["Seestern", "Bernstein", "Lichtblick", "Windrose", "Ankerplatz",
            "Deichweg", "Fernblick", "Gezeiten", "Hafenkante", "Inselweg"]

COMPANIES = ["Beispiel GmbH", "Muster AG", "Beispiel Handel KG",
             "Muster Technik GmbH"]

SITES = ["Hamburg", "Bremen", "Kiel", "Rostock"]


def more(story):
    """How many items a generator adds beside a story list: what is
    missing from FACTOR times its length."""
    return max(0, (FACTOR - 1) * len(story))


def _person(n):
    return CAST[n % len(CAST)]


def _inside(n):
    return INSIDE[n % len(INSIDE)]


def _project(n):
    return PROJECTS[n % len(PROJECTS)]


def _site(n):
    return SITES[n % len(SITES)]


def _company(n):
    return COMPANIES[n % len(COMPANIES)]


# One year, ending just before the day the archive is built around: a
# mail dated after that day would be a mail from the future, and the
# people view would sort everyone into "this week" as long as the real
# clock has not caught up. The story's own appointments stay the only
# thing ahead of the day.
FIRST_MOMENT = day(6, 20, 8, 20).replace(year=people.YEAR - 1)
SPREAD_DAYS = 355


def _spread(i, n, offset_minutes=0):
    """The i-th of n moments, spread evenly over the archive's year.

    Evenly and by position, so every item has a moment of its own: the
    timeline, the activity band and a date filter all read what is
    written here, and two items sharing a minute would make the order of
    a result a matter of chance.
    """
    minutes = round(i * SPREAD_DAYS * 24 * 60 / max(n, 1))
    return FIRST_MOMENT + timedelta(minutes=minutes + offset_minutes)


def _first(person):
    return person[0].split()[0]


GREETINGS = ["Hello {name},", "Hi {name},", "Dear {name},",
             "Good morning {name},"]
CLOSINGS = ["Best regards", "Kind regards", "Thanks and regards",
            "Many thanks", "Regards"]
# A line that could stand under anything – it is what keeps two mails of
# the same topic from reading like two copies of one.
ASIDES = ["The list is in the project folder.",
          "I have put it in the calendar.",
          "{person} knows the details.",
          "Nothing else is open from my side.",
          "Short notice, I know – it came up this morning.",
          "Call me if that does not work.",
          "I am at {site} until Thursday, after that by mail.",
          "No hurry, next week is early enough.",
          "{company} has been told as well.",
          "Sorry for the late answer, the week was full.",
          "",
          ""]


def _text(n, sender, to, paragraphs):
    """A mail: the greeting, what it says, an aside, the signature. Which
    words are chosen follows the position, so the archive reads varied
    and comes out the same on every machine."""
    teile = [GREETINGS[n % len(GREETINGS)].format(name=_first(to)),
             *paragraphs, f"{CLOSINGS[n % len(CLOSINGS)]}\n{sender[0]}"]
    return "\n\n".join(t for t in teile if t)


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------
# What the everyday traffic is about. Each topic carries what the first
# mail says and what an answer says, so a thread reads like one.
TOPICS = [
    {"key": "delivery", "subject": "Delivery date for {project}",
     "says": ["the crates for {project} leave the depot on {date} and reach "
              "{site} two days later.",
              "Please say who takes them in – the store closes at four."],
     "answers": ["{site} is covered, {person} signs for them.",
                 "Anything that comes later goes straight into the store."],
     "table": "delivery"},
    {"key": "invoice", "subject": "Invoice {number} for {project}",
     "says": ["invoice {number} covers the second stage of {project}.",
              "The hours are the ones we agreed in January; the travel to "
              "{site} is listed separately."],
     "answers": ["invoice {number} is checked and goes out this week.",
                 "The travel line is fine, it matches the days on site."]},
    {"key": "hours", "subject": "Timesheets for week {week}",
     "says": ["the timesheets of week {week} are in the shared folder.",
              "Two days of {project} are booked on the wrong cost centre."],
     "answers": ["I have moved the two days, the rest stands.",
                 "The list for {project} is right again."]},
    {"key": "maintenance", "subject": "Maintenance window on {date}",
     "says": ["we take the file service down on {date}, from eight in the "
              "evening until midnight.",
              "Nothing of {project} should run in that window."],
     "answers": ["the window fits, nothing of ours runs that evening.",
                 "I have told the people at {site}."]},
    {"key": "access", "subject": "Access for a new colleague in {site}",
     "says": ["a new colleague starts at {site} on {date}.",
              "They need the drives of {project} and a seat in the ticket "
              "system."],
     "answers": ["the drives are set, the ticket system follows on the day.",
                 "The seat at {site} is prepared."]},
    {"key": "notes", "subject": "Notes of the {project} meeting",
     "says": ["here are the notes of our meeting about {project}.",
              "Three things are open: the dates at {site}, who writes the "
              "monthly figures, and the licence count."],
     "answers": ["thank you, I take the monthly figures.",
                 "The dates at {site} I will bring on Thursday."]},
    {"key": "licence", "subject": "Licence renewal {project}",
     "says": ["the licences of {project} run out at the end of the month.",
              "We need {number} of them, two more than last year."],
     "answers": ["{number} is right, I have asked {company} for a price.",
                 "It should be through before the month ends."]},
    {"key": "cover", "subject": "Holiday cover in {month}",
     "says": ["I am away for two weeks in {month}.",
              "{person} takes {project} while I am gone; everything else "
              "waits."],
     "answers": ["noted, I will keep {project} moving.",
                 "Have a good time off."]},
    {"key": "visit", "subject": "Site visit {site}",
     "says": ["we would like to see {site} before the next stage of "
              "{project} starts.",
              "{date} would suit us, two hours would be enough."],
     "answers": ["{date} works, I will be there from nine.",
                 "Bring a photo ID, the gate asks for one."]},
    {"key": "training", "subject": "Training dates for {project}",
     "says": ["the training for {project} can run on two afternoons.",
              "Twelve people per session, the room at {site} takes that."],
     "answers": ["two afternoons are fine, the second week suits better.",
                 "I will put the dates in the calendar."]},
    {"key": "inventory", "subject": "Inventory list {project}",
     "says": ["the inventory list of {project} is attached.",
              "Four devices are at {site} and not in the list – they came "
              "with the last delivery."],
     "answers": ["the four are added, the count is right now.",
                 "The list goes to {company} on Friday."],
     "table": "inventory"},
    {"key": "figures", "subject": "Monthly figures {month}",
     "says": ["the figures of {month} are in the report folder.",
              "{project} is the largest line, {site} the smallest."],
     "answers": ["I have read them, nothing surprising.",
                 "The line for {project} I will explain on Thursday."]},
    {"key": "audit", "subject": "Questions from the review of {project}",
     "says": ["the review asks three things about {project}: who releases "
              "an order, where the records lie, and how long we keep them.",
              "Answers by the end of the month would be good."],
     "answers": ["I will write the three answers this week.",
                 "The records of {project} lie in the project folder."]},
    {"key": "network", "subject": "Network work at {site}",
     "says": ["the line at {site} is replaced on {date}.",
              "Expect an hour without the network; {project} is not touched."],
     "answers": ["an hour is fine, nobody works on {project} that morning.",
                 "I will tell the people at {site} the day before."]},
    {"key": "onboarding", "subject": "Handover for {project}",
     "says": ["I hand {project} over to {person} at the end of the month.",
              "Everything they need is in the project folder; the open "
              "points are in the list."],
     "answers": ["I have read the folder, two points are unclear.",
                 "Let us go through them at {site} on {date}."]},
    {"key": "ticket", "subject": "Ticket {number}: the file service is slow",
     "says": ["ticket {number} says the file service at {site} is slow "
              "since Monday.",
              "It looks like one folder of {project} with far too many "
              "small files."],
     "answers": ["I have looked at the folder, it holds {number} files.",
                 "We will move the old ones into the archive."]},
    {"key": "room", "subject": "Room booking in {month}",
     "says": ["the large room at {site} is booked for {month}.",
              "If {project} needs it, say so this week."],
     "answers": ["two afternoons for {project} would be good.",
                 "The rest of {month} we do not need it."]},
    {"key": "contract", "subject": "Renewal for {project}",
     "says": ["the service for {project} runs until the end of the year.",
              "{company} would extend it by twelve months at the same "
              "rate."],
     "answers": ["twelve months are fine, {person} looks at the wording.",
                 "We should sign before the end of the quarter."]},
]

FOLDERS = ["Inbox", "Archive", "Projects/{project}", "Reports"]


def _table(kind, n):
    """A small attachment: what a list of that kind looks like."""
    if kind == "delivery":
        rows = "\n".join(f"{i + 1};crate {i + 1};{2 + (i * 3) % 7};{_site(n + i)}"
                         for i in range(6))
        return f"line;content;quantity;site\n{rows}\n"
    rows = "\n".join(f"{100 + i};device {100 + i};{_site(n + i)};in use"
                     for i in range(8))
    return f"number;device;site;state\n{rows}\n"


def _context(n, when):
    return {"project": _project(n), "site": _site(n), "company": _company(n),
            "number": 1000 + 7 * n % 8000, "date": when.strftime("%d %B"),
            "week": 1 + n % 52,
            "month": when.strftime("%B"), "person": _inside(n + 1)[0]}


def mails(count):
    """Threads of two to five mails, the way a mailbox fills up.

    One partner per thread, taken from the whole cast in turn, so
    everyone writes and everyone is written to; the owner's answers lie
    in Sent, what comes in lies in one of the other folders.
    """
    out, threads = [], max(1, round(count / 3.5))
    for n in range(threads):
        topic, project = TOPICS[n % len(TOPICS)], _project(n)
        partner = _person(n)
        start = _spread(n, threads)
        length = 2 + n % 4
        for j in range(length):
            if len(out) >= count:
                return out
            when = start + timedelta(days=j, minutes=11 * j)
            ctx = _context(n, when)
            von_mir = j % 2 == 1
            sender, to = (people.ME, partner) if von_mir else (partner, people.ME)
            # The first mail says the whole thing, an answer picks up one
            # point of it – three identical answers in a row would be a
            # copy, not a thread.
            says = (topic["says"] if j == 0
                    else [topic["answers"][(j - 1) % len(topic["answers"])]])
            subject = topic["subject"].format(**ctx)
            entry = {
                "key": f"bulk-{topic['key']}-{n:03d}-{j}",
                "folder": "Sent" if von_mir else
                          FOLDERS[n % len(FOLDERS)].format(project=project),
                "when": when, "frm": sender, "to": [to],
                "subject": subject if j == 0 else f"Re: {subject}",
                "body": _text(n + j, sender, to,
                              [s.format(**ctx) for s in says]
                              + [ASIDES[(n + 3 * j) % len(ASIDES)].format(**ctx)]),
            }
            if j:
                entry["reply_to"] = f"bulk-{topic['key']}-{n:03d}-{j - 1}"
            if n % 5 == 0:
                entry["cc"] = [_inside(n + j)]
            if j == 0 and n % 3 == 0:
                entry["attachments"] = [
                    (f"{topic['key']}-{n:03d}.csv",
                     _table(topic.get("table") or "inventory", n))]
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
MEETINGS = [
    ("Weekly {project}", "Online", 30),
    ("{project} planning", "Meeting room 3", 60),
    ("Handover {site}", "Site {site}", 90),
    ("Review with {company}", "Meeting room 2", 60),
    ("Figures for {month}", "Carla's office", 45),
    ("Service call {project}", "Online", 30),
    ("Site walk {site}", "Site {site}", 120),
    ("Training {project}", "Training room", 180),
]

CALENDARS = ["Calendar", "Team", "Travel"]


def events(make, count):
    """Appointments over the year, in three calendars: the own one, the
    team's and what takes the owner out of the house."""
    out = []
    for n in range(count):
        when = _spread(n, count).replace(minute=0 if n % 2 else 30)
        titel, where, minutes = MEETINGS[n % len(MEETINGS)]
        ctx = _context(n, when)
        organizer = people.ME if n % 3 == 0 else _person(n)
        attendees = [_person(n + 1), _person(n + 4)]
        if organizer is not people.ME:
            attendees = [people.ME, _person(n + 2)]
        all_day = n % 37 == 0
        out.append((CALENDARS[n % len(CALENDARS)], make(
            f"bulk-event-{n:03d}", CALENDARS[n % len(CALENDARS)],
            titel.format(**ctx), when, minutes, organizer, attendees,
            location=where.format(**ctx),
            body=f"{titel.format(**ctx)} – {ctx['project']}, {ctx['site']}.",
            all_day=all_day)))
    return out


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
# The address book holds more than the people one writes with: it is
# imported, kept by hand and older than most of the traffic. These are
# its rest – entries with a company and a number and nothing else.
GIVEN = ["Anke", "Bernd", "Clara", "Detlef", "Elke", "Falk", "Gudrun",
         "Heiko", "Irma", "Jost", "Karin", "Lutz", "Maren", "Norbert",
         "Ortrud", "Peer", "Rieke", "Sigrid", "Torben", "Ulrike", "Volker",
         "Wiebke", "Xenia", "Yannick", "Zita"]
SURNAMES = ["Anlage", "Beschaffung", "Controlling", "Datenschutz",
            "Empfang", "Fuhrpark", "Gebaeude", "Hausdienst", "Instandhaltung",
            "Katalog", "Liegenschaft", "Meldestelle", "Nachtrag", "Ordnung",
            "Pruefstelle", "Registratur", "Schliessdienst", "Telefonie"]
ROLES = ["Office", "Purchasing", "Support", "Accounting", "Logistics",
         "Reception", "Quality", "Sales"]


def contacts(count):
    out = []
    for n in range(count):
        given = GIVEN[n % len(GIVEN)]
        surname = SURNAMES[(n // len(GIVEN)) % len(SURNAMES)]
        runde = n // (len(GIVEN) * len(SURNAMES))
        name = f"{given} {surname}" + (f" {runde + 1}" if runde else "")
        mail = f"{given.lower()}.{surname.lower()}@example.com"
        out.append({
            "id": f"contact-bulk-{n:03d}", "displayName": name,
            "givenName": given, "surname": surname,
            "companyName": _company(n), "jobTitle": ROLES[n % len(ROLES)],
            "emailAddresses": [{"address": mail, "name": name}],
            "businessPhones": [f"+49 40 000000 {n % 100:02d}"],
            "personalNotes": f"Contact at {_company(n)}, {_site(n)}."})
    return out


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------
CHAT_LINES = [
    "Have you seen the list for {project}? The last two lines look odd.",
    "I am at {site} tomorrow, I can look at it there.",
    "The service call is moved to {date}, {company} asked for it.",
    "Fine by me, nothing runs that afternoon.",
    "{person} needs the figures of {month} before Thursday.",
    "They are in the report folder, I will send the link.",
    "The room at {site} is taken, we take the small one.",
    "Works for me. I will bring the list for {project}.",
]

CHANNELS = ["General", "Planning", "Reports", "Service"]


def conversations(msg, count):
    """Chats, group chats, meeting chats and channels, in that turn.

    The channels of one team lie together, as a team's export writes
    them; every fifteenth conversation carries a file, so the mirrors
    have something that came out of a chat.
    """
    out = []
    kinds = ["1on1", "group", "meeting", "channels"]
    for n in range(count):
        kind = kinds[n % len(kinds)]
        partner, project = _person(n), _project(n)
        when = _spread(n, count)
        ctx = _context(n, when)
        titles = {"1on1": partner[0],
                  "group": f"{project} team",
                  "meeting": f"{project} planning",
                  "channels": CHANNELS[n % len(CHANNELS)]}
        subtitles = {"1on1": f"1:1 with {partner[0]}",
                     "group": f"{people.ME[0]}, {partner[0]}, {_inside(n)[0]}",
                     "meeting": "Meeting chat",
                     "channels": f"Channel of {people.COMPANY} {project}"}
        wer = [people.ME, partner, _inside(n + 1)]
        messages = []
        for j in range(3 + n % 5):
            line = CHAT_LINES[(n + j) % len(CHAT_LINES)].format(**ctx)
            messages.append(msg(f"bulk-{n:03d}-{j}", wer[j % len(wer)],
                                when + timedelta(hours=j, minutes=7 * j), line))
        conv = {"id": f"bulk-chat-{n:03d}", "kind": kind, "title": titles[kind],
                "subtitle": subtitles[kind], "messages": messages}
        if kind == "channels":
            conv["team"] = f"{people.COMPANY} {project}"
        if n % 15 == 0:
            name = f"list-{n:03d}.csv"
            text = _table("inventory", n)
            if kind == "channels":
                conv["files"] = [(name, when, text)]
            else:
                conv["attachments"] = [(name, when, text)]
        out.append(conv)
    return out


# ---------------------------------------------------------------------------
# The mirrors: OneDrive and SharePoint
# ---------------------------------------------------------------------------
DOCUMENTS = [
    ("notes-{n:03d}.md", "# Notes {project}, {month}\n\n"
     "* the dates at {site} are set\n* {person} takes the figures\n"
     "* the licence count is checked\n"),
    ("figures-{n:03d}.csv", "month;project;site;days\n"
     "{month};{project};{site};{number}\n"),
    ("letter-{n:03d}.txt", "{company}\n\nSubject: {project}\n\n"
     "The service at {site} runs until the end of the year and is "
     "extended by twelve months.\n"),
    ("list-{n:03d}.md", "# List {project}\n\n| what | where |\n|---|---|\n"
     "| devices | {site} |\n| spare parts | store |\n"),
]

FOLDER_NAMES = ["Documents/{project}", "Documents/Reports", "Documents/Letters",
                "Archive/{project}", "Pictures"]


def _files(count, prefix=""):
    out = []
    for n in range(count):
        when = _spread(n, count)
        ctx = dict(_context(n, when), n=n)
        folder = FOLDER_NAMES[n % len(FOLDER_NAMES)].format(**ctx)
        if folder == "Pictures":
            out.append((f"{prefix}Pictures/picture-{n:03d}.png", when, None))
            continue
        name, text = DOCUMENTS[n % len(DOCUMENTS)]
        out.append((f"{prefix}{folder}/{name.format(**ctx)}", when,
                    text.format(**ctx)))
    return out


def onedrive_files(count):
    """The owner's drive: notes, figures, letters and a picture now and
    then. A picture is handed back as None – sources.py fills in the one
    PNG it keeps, so there is one binary and not three hundred."""
    return _files(count)


def sharepoint_files(count):
    """The team library: the same kinds, below the projects' folders."""
    return [(rel.split("/", 1)[1] if rel.startswith("Documents/") else rel,
             when, text) for rel, when, text in _files(count)]


# ---------------------------------------------------------------------------
# SharePoint pages
# ---------------------------------------------------------------------------
def pages(count):
    out = []
    for n in range(count):
        when = _spread(n, count)
        ctx = _context(n, when)
        title = f"{ctx['project']} at {ctx['site']} {n:03d}"
        body = (f"<p>{ctx['project']} runs at {ctx['site']} since "
                f"{ctx['month']}. {ctx['person']} keeps the dates, "
                f"{ctx['company']} does the service.</p>"
                f"<h2>What is open</h2><ul><li>licence count</li>"
                f"<li>the room at {ctx['site']}</li>"
                f"<li>the figures of {ctx['month']}</li></ul>")
        out.append((f"bulk-{n:03d}", title, when, body))
    return out


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
TASKS = [
    "Check the list for {project}",
    "Book the room at {site}",
    "Ask {company} for a price",
    "Write the figures of {month}",
    "Hand {project} over to {person}",
    "Count the licences of {project}",
    "Prepare the training at {site}",
    "Close the ticket about the file service",
    "Collect the records of {project}",
    "Order spare parts for {site}",
]


def plans(count, tasks_per):
    """One board per project, three buckets each, and the tasks spread
    over them – the same shape the story's board has."""
    out = []
    for n in range(count):
        project = PROJECTS[n % len(PROJECTS)]
        key = f"bulk-plan-{n:02d}"
        buckets = [{"id": f"bucket-{key}-{name}", "name": label,
                    "orderHint": str(i + 1)}
                   for i, (name, label) in enumerate(
                       [("backlog", "Backlog"), ("doing", "In progress"),
                        ("done", "Done")])]
        aufgaben = []
        for j in range(tasks_per):
            when = _spread(j, tasks_per)
            ctx = _context(n + j, when)
            bucket = buckets[j % 3]["id"]
            fertig = bucket.endswith("done")
            aufgaben.append((
                f"task-{key}-{j:02d}", bucket,
                TASKS[j % len(TASKS)].format(**ctx),
                [f"user-{_first(_person(n + j)).lower()}"], when,
                f"{ctx['project']} at {ctx['site']}, with {ctx['company']}.",
                {"c1": "checked", "c2": "written"} if j % 2 else {},
                {}, [(f"user-{_first(_inside(j)).lower()}",
                      when + timedelta(days=2),
                      f"Done for {ctx['site']}.")] if fertig else []))
        out.append({
            "plan": {"id": key, "titel": f"{project} board {n:02d}"},
            "buckets": buckets,
            "labels": {"category1": "service", "category2": "dates"},
            "tasks": aufgaben, "attachments": []})
    return out


# ---------------------------------------------------------------------------
# To Do
# ---------------------------------------------------------------------------
STEPS = ["ask {person}", "put it in the calendar", "check the list",
         "tell {site}", "write it down"]


def todo_lists(count, tasks_per):
    out = []
    for n in range(count):
        project = PROJECTS[n % len(PROJECTS)]
        key = f"bulk-list-{n:02d}"
        aufgaben = []
        for j in range(tasks_per):
            when = _spread(j, tasks_per)
            ctx = _context(n + j, when)
            fertig = (n + j) % 3 == 0
            aufgaben.append((
                f"task-{key}-{j:02d}",
                TASKS[(n + j) % len(TASKS)].format(**ctx),
                "completed" if fertig else "notStarted", when,
                f"{ctx['project']}, {ctx['site']}.",
                [STEPS[j % len(STEPS)].format(**ctx)] if j % 2 else [], []))
        out.append({"id": key, "titel": f"{project} {n:02d}", "art": "list",
                    "geteilt": n % 4 == 0, "tasks": aufgaben})
    return out


# ---------------------------------------------------------------------------
# OneNote
# ---------------------------------------------------------------------------
SECTIONS = ["Meetings", "Notes", "Decisions", "Reports", "Service", "Dates"]


def onenote_pages(count):
    out = []
    for n in range(count):
        when = _spread(n, count)
        ctx = _context(n, when)
        title = f"{ctx['project']} {ctx['month']} {n:03d}"
        body = (f"<h1>{title}</h1><p>Present: {people.ME[0]}, "
                f"{_person(n)[0]}, {_inside(n)[0]}.</p>"
                f"<ol><li>The dates at {ctx['site']} are set.</li>"
                f"<li>{ctx['company']} does the service until the year "
                f"ends.</li><li>The figures of {ctx['month']} go out on "
                f"Friday.</li></ol>")
        out.append((f"bulk-page-{n:03d}", SECTIONS[n % len(SECTIONS)], title,
                    when, body))
    return out


# The reserved words are a promise, and a promise nobody checks is a
# comment: everything the generators write their texts from is held
# against it once, when this module is imported.
def _guard():
    quellen = [t["subject"] for t in TOPICS]
    quellen += [s for t in TOPICS for s in t["says"] + t["answers"]]
    quellen += [m[0] for m in MEETINGS] + CHAT_LINES + TASKS + STEPS
    quellen += [d[1] for d in DOCUMENTS] + PROJECTS + SITES + COMPANIES
    quellen += SECTIONS + CHANNELS + ROLES + SURNAMES + GIVEN
    quellen += GREETINGS + CLOSINGS + ASIDES
    for text in quellen:
        for wort in RESERVED:
            if wort in text.lower():
                raise AssertionError(f"reserved word {wort!r} in {text!r}")


_guard()
