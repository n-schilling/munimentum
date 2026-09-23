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
  * a case holds three items pinned to the version they had when they
    came in – each has changed since, so the case marks them.

Teams messages carry their own history in the message store (the edit
and the deletion in sources.EDITED and sources.DELETED).

Everything is derived from the lists here; the browser tests read them.
"""

import json
import os
import sqlite3

import evidence
import faelle
import settings
import store_layout
import versions

from testdata import people, sources
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
    return [exports / folder for folder, _index in settings.QUELLEN.values()]


def _stamp(path, when):
    sources._touch(path, when)


def chain(exports):
    """Two chained runs over the archive, as described above. Returns the
    checksums of the earlier versions, by path below the data folder."""
    earlier = {}
    today = {}
    # 1. The archive as the first chained run found it: earlier versions.
    for folder, rel, content, when in EARLIER:
        path = exports / folder / rel
        today[path] = (path.read_bytes(), path.stat().st_mtime)
        data = content.encode("utf-8")
        path.write_bytes(data)
        _stamp(path, when)
        earlier[f"{folder}/{rel}"] = versions.sha256_bytes(data)
    evidence.sweep(exports, _roots(exports))
    # 2. A later run: the exports write today's versions the one way.
    saved = {k: os.environ.get(k) for k in _env(exports)}
    os.environ.update(_env(exports))
    try:
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
    # 3. After the last run.
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
         "--store", str(store), "--report", str(home / "archivpruefung.json"),
         "--data", str(exports)])
    return json.loads((home / "archivpruefung.json").read_text(encoding="utf-8"))
