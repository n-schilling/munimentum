"""The synthetic archive's past: versions, an evidence chain, a changed case.

testdata/sources.py writes the archive as it stands today. This module
gives it the history a real archive gathers over months of runs, through
the app's own code paths (versions.py, evidence.py, faelle.py):

  * some files had an earlier version: the first chained run saw that
    one, a later run replaced it – the earlier version lies below
    versions/, and the chain says when each was captured;
  * one file was changed without an export saying so before the last
    run, which chained it as `outside`, and one after it, which only the
    archive check finds (and "Fetch again" would put right);
  * the organization has a past of its own: a version a week for some
    fifteen weeks before the latest runs, a few days with two runs –
    people joining, leaving, moving and being retitled –, ending in the
    version just before today's (ORG_TIMELINE);
  * a case holds three items pinned to the version they had when they
    came in – each has changed since, so the case marks them.

Teams messages carry their own history in the message store (the edit
and the deletion in sources.EDITED and sources.DELETED).

Everything is derived from the lists here; the browser tests read them.
"""

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import evidence
import faelle
import item_notes
import own_contacts
import uploads
import organization
import outlook_export
import settings
import store_layout
import versions

from testdata import bulk, people, sources
from testdata.people import day

PROJECT = people.PROJECT

# An earlier version of a file today's archive holds:
# (export folder, path below it, the earlier content, when it was made).
ROLLOUT_DRAFT_1 = f"""# {PROJECT} rollout plan

Draft 1, as sent by Dana Dienstleister.

## Site 1
* 2 June   preparation, backup
* 3 June   rollout, 18 workplaces

## Site 2
* 8 June   wave 1
* 15 June  wave 2
"""

_PROJECT_PAGE = next(p for p in sources.PAGES if p[0] == "project")
PROJECT_PAGE_BEFORE = f"""
<p>{PROJECT} replaces the workplace standard at both sites. Stage 1 is
running, the rollout of site 1 is planned for 3 June.</p>
<h2>Documents</h2>
<ul><li>Specification</li><li>Rollout plan</li></ul>"""

RISK_LOG_BEFORE = """risk;likelihood;impact;owner
network change site 2;medium;high;Carla Chef
printer mapping old devices;high;low;Bob Baumeister
"""

_SHAREPOINT_LIBRARY = f"{sources.SHAREPOINT_SITE_DIR}/{sources.SHAREPOINT_LIBRARY}"

EARLIER = [
    (settings.ONEDRIVE_DIR, "Dateien/Documents/Ostwind/rollout-plan.md",
     ROLLOUT_DRAFT_1, day(5, 4, 10, 0)),
    (settings.SHAREPOINT_PAGES_DIR, sources.page_rel(_PROJECT_PAGE[1]),
     sources.page_html(_PROJECT_PAGE[1], day(4, 20, 11, 0), PROJECT_PAGE_BEFORE),
     day(4, 20, 11, 0)),
    (settings.SHAREPOINT_DIR, f"{_SHAREPOINT_LIBRARY}/Dateien/Ostwind/risk-log.csv",
     RISK_LOG_BEFORE, day(4, 28, 9, 0)),
]

# The organization a week earlier: Alice still reported to Carla, Nina was
# an engineer, Olaf's account was still on, Kai had not joined. Today's
# version therefore brings one move, one change, one departure, one arrival.
ORG_BEFORE = sources.org_users({
    sources.ORG_ME: {"manager": "org-carla"},
    "org-nina": {"jobTitle": "Network Engineer"},
    "org-olaf": {"accountEnabled": True},
    "org-kai": {"accountEnabled": False},
})
ORG_EARLIER = (settings.TEAMS_DIR, f"{organization.ORG_DIR}/{organization.FILE}",
               sources.org_text(ORG_BEFORE), day(6, 7, 6, 0))
# A contact as Outlook had it before: Dana's card named another job title.
_DANA = next(c for c in sources.CONTACTS if c["displayName"] == people.EXTERNALS[0][0])
CONTACT_TITLE_BEFORE = "Consultant"
CONTACT_EARLIER = (settings.OUTLOOK_DIR, f"kontakte/{outlook_export.contact_filename(_DANA)}",
                   outlook_export.build_vcf({**_DANA, "jobTitle": CONTACT_TITLE_BEFORE}), day(5, 12, 9, 0))
# Every file with a kept earlier version.
KEPT = [*EARLIER, ORG_EARLIER, CONTACT_EARLIER]

# The organization's past: one export a week for fifteen weeks, three days
# with a second run in the afternoon, the last one ORG_BEFORE (three days
# ago). Each step is ORG_BEFORE with what had not yet happened undone:
# volume people who had not joined yet are disabled, six people who left
# meanwhile are still there, two titles and two managers are the old ones.
# The dates count back from the build, so the overview always shows the
# last months; what changes when is fixed, so the numbers are.
_LATE = sorted(uid for uid in sources.ORG_USERS
               if uid.startswith(("org-hanno-", "org-ines-", "org-nina-")))[::2]
_LEAVERS = [sources._user(f"org-leaver-{i}", name, "Specialist", dept, boss)
            for i, (name, dept, boss) in enumerate([
                ("Udo Umzug", "Projects", "org-bob"), ("Vera Vertrag", "Finance", "org-frida"),
                ("Willi Wechsel", "Marketing", "org-malte"), ("Xenia Xaver", "Finance", "org-ines"),
                ("Yara Yoga", "Projects", "org-nina"), ("Zeno Zukunft", "Marketing", "org-hanno")])]
# (week before the build, hour) per export, oldest first; the last is the
# day of ORG_BEFORE.
_ORG_RUNS = [(15, 6), (14, 6), (13, 6), (12, 6), (12, 14), (11, 6), (10, 6), (9, 6),
             (8, 6), (7, 6), (7, 15), (6, 6), (5, 6), (4, 6), (3, 6), (2, 6), (2, 13),
             (1, 6), (0.43, 6)]
_STEPS = len(_ORG_RUNS)


def _org_state(i):
    """The directory at export i of _ORG_RUNS – ORG_BEFORE at the last."""
    last = _STEPS - 1
    changes = {sources.ORG_ME: {"manager": "org-carla"},
               "org-nina": {"jobTitle": "Network Engineer"},
               "org-olaf": {"accountEnabled": True},
               "org-kai": {"accountEnabled": False}}
    for j, uid in enumerate(_LATE):             # one joins every other step or so
        if i < last and i < 1 + j * last // len(_LATE):
            changes[uid] = {"accountEnabled": False}
    for j, user in enumerate(_LEAVERS):         # leave at steps 3, 6, 9, 12, 14, 17
        if i < (3, 6, 9, 12, 14, 17)[j]:
            changes[user["id"]] = dict(user)
    if i < 5:
        changes["org-lena"] = {"jobTitle": "Logistics Assistant"}
    if i < 10:
        changes["org-hanno-1"] = {"jobTitle": "Junior Specialist"}
    if i < 8:
        changes["org-nina-3"] = {"manager": "org-hanno"}
    if i < 13:
        changes["org-ines-5"] = {"manager": "org-frida"}
    if i < 15:
        changes["org-ines-1"] = {"jobTitle": "Junior Specialist"}
    if i < 18:
        changes["org-hanno-6"] = {"manager": "org-ines"}
    return sources.org_users(changes)


def org_timeline(now=None):
    """[(when, text)] of every organization export before today's, oldest
    first – the last is ORG_EARLIER's text."""
    now = now or datetime.now(UTC).replace(microsecond=0)
    out = []
    for i, (weeks, hour) in enumerate(_ORG_RUNS):
        when = (now - timedelta(weeks=weeks)).replace(hour=hour, minute=0, second=0)
        out.append((when, sources.org_text(_org_state(i))))
    return out


ORG_TIMELINE_LEN = _STEPS
# Version files the versions folder keeps: one per earlier file version,
# and every organization version before today's.
KEPT_VERSIONS = len(EARLIER) + 1 + _STEPS       # … and the contact's
ORG_CHANGES = {"moved": [sources.ORG_ME], "changed": ["org-nina"], "left": ["org-olaf"],
               "joined": ["org-kai"]}

# What Microsoft said about a mirrored file when the later run fetched it
# (its quickXorHash): the rollout plan agrees with what came, the risk log
# does not – Microsoft still names the bytes of the earlier version.
MICROSOFT = {
    f"{settings.ONEDRIVE_DIR}/Dateien/Documents/Ostwind/rollout-plan.md": "match",
    f"{settings.SHAREPOINT_DIR}/{_SHAREPOINT_LIBRARY}/Dateien/Ostwind/risk-log.csv": "mismatch",
}

# Changed by hand before the last chained run: the chain records it as
# happened outside the app.
OUTSIDE = (settings.ONEDRIVE_DIR, "Dateien/Documents/Notes/meeting-notes.txt",
           "- added later by hand\n")
# Changed by hand after the last run: the archive check finds it.
TAMPERED = (settings.ONEDRIVE_DIR, "Dateien/Archive/old-budget.csv", "2027;999999\n")

# The case whose items have changed since they came in.
CASE_NAME = "Site 1 acceptance"
CASE_MESSAGE = ("chat-bob", "msg-b5")        # its earlier text: sources.EDITED


def _env(exports):
    """What the app hands every step (versions.py reads it)."""
    return {"MUNIMENTUM_VERSIONS_DIR": str(exports / versions.VERSIONS_DIRNAME),
            "MUNIMENTUM_EVIDENCE_DIR": str(exports / versions.EVIDENCE_DIRNAME),
            "KEEP_VERSIONS": "1", "VERSIONS_MAX_MB": "50"}


def _roots(exports):
    return [exports / folder for folder in settings.ROOTS.values()]


def _stamp(path, when):
    sources._touch(path, when)


def _at(when, run):
    """Run `run` with the clock versions.py and evidence.py read set to
    `when` – the chain then says the run happened then."""
    real = versions.now
    versions.now = lambda: when.isoformat(timespec="seconds")
    try:
        return run()
    finally:
        versions.now = real


def chain(exports):
    """The chained runs over the archive, as described above. Returns the
    checksums of the earlier versions, by path below the data folder."""
    earlier = {}
    today = {}
    timeline = org_timeline()
    org_folder, org_rel = ORG_EARLIER[0], ORG_EARLIER[1]
    # 1. The archive as the first chained run found it, fifteen weeks ago:
    # earlier versions, the organization as it was then.
    for folder, rel, content, when in KEPT:
        path = exports / folder / rel
        today[path] = (path.read_bytes(), path.stat().st_mtime)
        if (folder, rel) == (org_folder, org_rel):
            content = timeline[0][1]
        data = content.encode("utf-8")
        path.write_bytes(data)
        _stamp(path, when)
        earlier[f"{folder}/{rel}"] = versions.sha256_bytes(data)
    _at(timeline[0][0], lambda: evidence.sweep(exports, _roots(exports)))
    saved = {k: os.environ.get(k) for k in _env(exports)}
    os.environ.update(_env(exports))
    try:
        # 2. The organization's exports since, each its own chained run.
        org_path = exports / org_folder / org_rel
        for when, text in timeline[1:]:
            _at(when, lambda text=text: versions.write_bytes(org_path, text.encode("utf-8")))
            _at(when, lambda: evidence.sweep(exports, _roots(exports)))
        earlier[f"{org_folder}/{org_rel}"] = versions.sha256_bytes(timeline[-1][1].encode("utf-8"))
        # 3. A later run: the exports write today's versions the one way.
        for path, (data, mtime) in today.items():
            rel = versions.rel_of(path, exports)
            extra = None
            if rel in MICROSOFT:
                ours = versions.QuickXor().update(data).b64()
                before = next(c for f, r, c, _w in EARLIER if f"{f}/{r}" == rel)
                theirs = ours if MICROSOFT[rel] == "match" else \
                    versions.QuickXor().update(before.encode("utf-8")).b64()
                extra = {"quickxor": ours, "ms_quickxor": theirs, "ms_match": ours == theirs}
            versions.write_bytes(path, data, extra=extra)
            os.utime(path, (mtime, mtime))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    folder, rel, extra = OUTSIDE
    path = exports / folder / rel
    with open(path, "a", encoding="utf-8") as f:
        f.write(extra)
    evidence.sweep(exports, _roots(exports))
    # 4. After the last run.
    folder, rel, extra = TAMPERED
    with open(exports / folder / rel, "a", encoding="utf-8") as f:
        f.write(extra)
    return earlier


def _key(store, root, rel):
    """What the index says about the file: key, source, title, date, who."""
    con = sqlite3.connect(str(store_layout.db_path(store)))
    try:
        row = con.execute("SELECT key, src, title, date, who FROM chunks WHERE root = ? AND rel = ? "
                          "ORDER BY seq LIMIT 1", (root, rel)).fetchone()
    finally:
        con.close()
    return row


def case(home, exports, store, earlier):
    """The case pinned to the earlier versions, in the profile's case book."""
    book = faelle.Fallbuch(home / faelle.DB_NAME)
    case_id = book.fall_anlegen(CASE_NAME, "What site 1 looked like when it was accepted.")
    items, pins = [], {}
    for folder, rel, _content, _when in EARLIER:
        index = next(i for f, i in settings.QUELLEN.values() if f == folder)
        key, src, title, date, who = _key(store, index, rel)
        items.append({"key": key, "src": src, "root": index, "rel": rel,
                      "titel": title, "datum": date, "wer": who})
        pins[key] = earlier[f"{folder}/{rel}"]
    conv_id, msg_id = CASE_MESSAGE
    conv = next(c for c in sources.CONVERSATIONS if c["id"] == conv_id)
    message = next(m for m in conv["messages"] if m["id"] == msg_id)
    key = f"teams:{conv_id}#{msg_id}"
    items.append({"key": key, "src": "teams", "root": "teams",
                  "rel": sources._conversation_rel(conv), "titel": conv["title"],
                  "datum": message["createdDateTime"][:16].replace("T", " "),
                  "wer": message["from"]["user"]["displayName"]})
    pins[key] = evidence._said_sha({"contentType": "text", "content": sources.EDITED[msg_id][0]}, None)
    book.hinzufuegen(case_id, items)
    con = sqlite3.connect(str(home / faelle.DB_NAME))
    try:
        con.executemany("UPDATE eintraege SET fassung = ? WHERE fall_id = ? AND key = ?",
                        [(sha, case_id, key) for key, sha in pins.items()])
        con.commit()
    finally:
        con.close()
    return case_id


# The person the keeper added to the address book in the app: writes
# often, has no Outlook card – a card of the app's own, filled in by hand.
OWN_CONTACT = {"person": people.EXTERNALS[6], "title": "Site manager", "company": "Nordwind Montage GmbH",
               "mobile": "+49 170 0000066", "note": "On site Tuesdays and Thursdays."}


def own_contact(home):
    """The card of the app's own in the profile's database (own_contacts.py)."""
    name, address = OWN_CONTACT["person"]
    book = own_contacts.OwnContacts(home / faelle.DB_NAME)
    found = {"lines": bulk.footer(OWN_CONTACT["person"]).split("\n")[:-1], "basis": "repeated"}
    try:
        ident = book.add(address, name, found)
    except own_contacts.Exists:                  # a profile kept from an earlier build
        ident = next(c["id"] for c in book.all() if c["address"] == address)
    book.change(ident, {"title": OWN_CONTACT["title"], "company": OWN_CONTACT["company"],
                        "note": OWN_CONTACT["note"],
                        "phones": [{"type": "mobile", "number": OWN_CONTACT["mobile"]}]})
    return ident


# The keeper's note on the scan they added by hand – reworded once, so the
# window has an earlier wording to show.
ITEM_NOTE = ("Handed over by the landlord at the site meeting.",
             "Handed over by the landlord at the site meeting; the signed original is in the office safe.")


def item_note_key(exports):
    """The scan's key, asked of the index's own readers (uploads.item_keys)."""
    rel = sources.upload_rel("file")
    return uploads.item_keys(exports / settings.UPLOADS_DIR, [rel])[rel]


def item_note(home, exports):
    """The note on the uploaded scan (item_notes.py), in the profile's database."""
    notes = item_notes.ItemNotes(home / faelle.DB_NAME)
    for text in ITEM_NOTE:
        notes.put(item_note_key(exports), text)


def archive_report(home, exports, store, run):
    """The archive check's stored report, so Insights has one to show – at
    the place the app reads it from (app.archiv_bericht_pfad)."""
    run([str(sources.Path(__file__).resolve().parents[1] / "archive_check.py"),
         *[str(exports / f) for f in (settings.TEAMS_DIR, settings.OUTLOOK_DIR, settings.ONEDRIVE_DIR)],
         "--sharepoint", str(exports / settings.SHAREPOINT_DIR),
         "--pages", str(exports / settings.SHAREPOINT_PAGES_DIR),
         "--planner", str(exports / settings.PLANNER_DIR),
         "--todo", str(exports / settings.TODO_DIR),
         "--onenote", str(exports / settings.ONENOTE_DIR),
         "--uploads", str(exports / settings.UPLOADS_DIR),
         "--store", str(store), "--report", str(home / "archivpruefung.json"),
         "--data", str(exports)])
    return json.loads((home / "archivpruefung.json").read_text(encoding="utf-8"))
