#!/usr/bin/env python3
"""
uploads.py – what the keeper adds to the archive by hand.

    uploads.py --staging <folder> <uploads export folder>

Everything else in the archive comes from Microsoft 365. A scanned
letter, a contract as a PDF, a mail someone forwarded as a file do not –
and belong to a matter all the same. They are added under *Build archive*
and kept in a folder of their own (`uploads_export/`), apart from what
Microsoft delivered and marked as added by hand wherever they appear.

What a file is decides how the archive reads it:

    mail      an .eml       E-Mail/Uploaded/      read as a mail
    event     an .ics       kalender/Uploaded/    read as an appointment
    contact   a .vcf        kontakte/Uploaded/    read as a contact card
    file      anything else Dateien/              found by what is typed
                                                  about it (meta/<name>.json:
                                                  title, date, people,
                                                  description)

The folder has the mailbox export's shape on purpose: the same readers
read it (corpus.py). Two steps bring a file in:

1. The page hands the bytes to the app, which keeps them in a staging
   folder in the profile – not in the archive – and says what it takes
   the file for and what it reads in it (`stage`, `preview`).
2. On *Add to archive* the app writes down what was chosen (`commit`)
   and starts a run whose first step is this script: it moves each
   staged file into the archive through versions.py – journaled, so the
   evidence step chains it like any other write – and the run goes on
   with the chain, the index and the calendar, as after an export.

A file added twice is one file: its name carries its checksum.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path

import corpus
import export_util
import faelle
import item_notes
import profile_db
import progress
import schluessel
import versions

STAGING_DIRNAME = "upload_staging"
COMMIT_NAME = "commit.json"
KINDS = ("mail", "event", "contact", "file")
# Where each kind lies below the uploads folder: the readers' layout (corpus).
FOLDER = corpus.UPLOAD_FOLDER
TYPED = ("title", "date", "people", "description")   # what a plain file is found by
MAX_BYTES = 500 * 1024 * 1024   # one file; a scan or a recording, not a disk image
STALE_SECONDS = 24 * 3600       # a staged file nobody added: gone after a day
_EXT = {".eml": "mail", ".ics": "event", ".vcf": "contact"}
_TEXT = {"title": 300, "people": 1000, "description": 20000, "note": item_notes.MOST}
BadValue = profile_db.BadValue   # a value the upload does not take; `field` names it


def staging_dir(home):
    return Path(home) / STAGING_DIRNAME


# Names the archive's own readers pass over (evidence.is_archive_file,
# corpus._dateien_uploads) or a disk refuses: a file called so would be
# taken in and then neither chained nor indexed.
_OWN_EXT = ("tmp", "teil")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
STEM_BYTES = 150       # leaves room for "__<sha8>", the extension and ".tmp" below a 255-byte name
INCOMING = ".incoming"  # the bytes before they are named: no name safe_name can give


def _split(name):
    """(stem, extension without its dot) – an extension only where the
    last part is letters and digits as it stands."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or not re.fullmatch(r"[A-Za-z0-9]{1,12}", ext):
        return name, ""
    return stem, ext


def safe_name(name):
    """A file's name for the disk: no path, no control character, not too
    long – the stem cut (by characters and by bytes), the extension kept
    in lower case, as the readers look for it; a name the archive keeps
    for itself or Windows reserves gets an underscore."""
    name = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", "_", Path(str(name or "")).name).strip(" .")
    # An extension is a last part of letters and digits as it stands:
    # "Dr. Smith letter" has none, and its dot is part of the name.
    stem, ext = _split(name)
    ext = ext.lower()
    stem = stem[:100]
    while len(stem.encode("utf-8")) > STEM_BYTES:
        stem = stem[:-1]
    stem = stem.strip(" .") or "file"
    if ext in _OWN_EXT:
        stem, ext = f"{stem}_{ext}", ""
    if stem.lower().startswith("state.db") or stem.lower().split(".")[0] in _RESERVED:
        stem = "_" + stem
    return f"{stem}.{ext}" if ext else stem


def kind_of(path, name):
    """What the file is taken for: by its extension, and only when it
    reads as that – an .eml without one mail header, an .ics without an
    event, a .vcf without a card are files."""
    kind = _EXT.get(Path(name).suffix.lower(), "file")
    if kind == "file":
        return kind
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError:
        return "file"
    if kind == "mail":
        msg = BytesHeaderParser(policy=policy.default).parsebytes(head)
        return kind if any(msg.get(h) for h in ("From", "Subject", "Date", "Message-ID")) else "file"
    mark = b"BEGIN:VEVENT" if kind == "event" else b"BEGIN:VCARD"
    return kind if mark in head.upper() else "file"


def preview(path, kind):
    """What the archive will read in the file – shown before it is added,
    so nobody types what the file says itself."""
    try:
        if kind in ("mail", "event"):
            read = corpus._outlook_file if kind == "mail" else corpus._calendar_file
            rec = read(str(path), str(Path(path).parent)) or {}
            title = "" if rec.get("title") == corpus.NO_SUBJECT else rec.get("title") or ""
            who = "" if rec.get("who") == corpus.NOBODY else rec.get("who") or ""
            return {"title": title, "date": rec.get("date") or "", "people": who}
        if kind == "contact":
            card = corpus.read_vcard(Path(path).read_text(encoding="utf-8", errors="replace"))
            return {"title": card["name"], "date": "",
                    "people": " · ".join(x for x in (card["organisation"], card["title"]) if x)}
    except Exception:                            # noqa: BLE001 – unreadable: nothing to show
        pass
    return {"title": "", "date": "", "people": ""}


def sweep(home, now=None):
    """Drop staged files nobody added: a dialog closed, a tab gone."""
    folder = staging_dir(home)
    if not folder.is_dir():
        return
    now = now if now is not None else datetime.now(UTC).timestamp()
    for entry in folder.iterdir():
        try:
            if entry.is_dir() and now - entry.stat().st_mtime > STALE_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


def new_slot(home):
    """A place for one incoming file: (id, folder). The bytes are written
    there by the caller; `stage` then says what they are."""
    ident = uuid.uuid4().hex
    folder = staging_dir(home) / ident
    folder.mkdir(parents=True, exist_ok=True)
    return ident, folder


def stage(home, ident, name):
    """Name the bytes that lie in slot `ident` (as INCOMING) and say
    what they are: {id, name, size, kind, facts}."""
    folder = staging_dir(home) / ident
    name = safe_name(name)
    path = folder / name
    (folder / INCOMING).replace(path)
    kind = kind_of(path, name)
    return {"id": ident, "name": name, "size": path.stat().st_size, "kind": kind,
            "facts": preview(path, kind)}


def _slot_ok(ident):
    return re.fullmatch(r"[0-9a-f]{32}", str(ident or "")) is not None


def staged(home, ident):
    """The one file in slot `ident`, or None."""
    if not _slot_ok(ident):
        return None
    folder = staging_dir(home) / ident
    files = [p for p in folder.iterdir() if p.is_file() and p.name != INCOMING] if folder.is_dir() else []
    return files[0] if len(files) == 1 else None


def discard_slot(home, ident):
    """Drop a slot whose bytes were never named."""
    if _slot_ok(ident):
        shutil.rmtree(staging_dir(home) / ident, ignore_errors=True)


def discard(home, ident):
    path = staged(home, ident)
    if path is None:
        return False
    shutil.rmtree(path.parent, ignore_errors=True)
    return True


def _text(item, field):
    value = item.get(field, "")
    if not isinstance(value, str) or len(value) > _TEXT[field]:
        raise BadValue(field)
    return " ".join(value.split()) if field in ("title", "people") else value.strip()


def people_of(typed):
    """The names typed into a plain file's People field: separated by
    commas or semicolons, except inside double quotes – a name picked as
    "Surname, Given" is one person. Joined with " · ", as the index lists
    people, so no reader splits it again at its comma."""
    names = [(quoted or bare).strip() for quoted, bare in re.findall(r'"([^"]*)"|([^,;"]+)', typed or "")]
    return " · ".join(name for name in names if name)


def clean_item(home, item):
    """One entry of what is to be added, checked: its staged file is
    there, and what was typed about it is text of a sensible length – a
    date a day (YYYY-MM-DD) or nothing. What a mail, an appointment or a
    card says itself is not typed over: only a plain file takes it."""
    if not isinstance(item, dict):
        raise BadValue("items")
    path = staged(home, item.get("id"))
    if path is None:
        raise BadValue("id")
    for field in item:
        if field not in ("id", "note", *TYPED):
            raise BadValue(str(field)[:40])
    kind = kind_of(path, path.name)
    # The keeper's note (item_notes.py) is for every kind: where the file
    # came from, what it is for – their word about it, not a field of it.
    out = {"id": item["id"], "kind": kind}
    note = _text(item, "note")
    if note:
        out["note"] = note
    if kind != "file":
        return out
    day = item.get("date", "")
    if day:
        try:
            datetime.strptime(day if isinstance(day, str) else "", "%Y-%m-%d")
        except ValueError:
            raise BadValue("date") from None
    out.update(title=_text(item, "title"), date=day or "", people=people_of(_text(item, "people")),
               description=_text(item, "description"))
    return out


def commit(home, items):
    """Write down what the next run of this script takes in."""
    folder = staging_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    listed = [clean_item(home, item) for item in items]
    if not listed:
        raise BadValue("items")
    (folder / COMMIT_NAME).write_text(json.dumps({"items": listed}, ensure_ascii=False), encoding="utf-8")
    return listed


def target_rel(kind, name, sha):
    """Where a file lies in the uploads folder: its kind's folder, its
    name with the first of its checksum – the same bytes under the same
    name, the same file; under another name, a file of its own."""
    stem, ext = _split(name)
    return f"{FOLDER[kind]}/{stem}__{sha[:8]}{'.' + ext if ext else ''}"


meta_rel = corpus.upload_meta_rel
read_meta = corpus.upload_meta


def item_keys(target, rels):
    """rel -> the stable key the index gives an uploaded file. Asked of
    the index's own readers (corpus, schluessel.zuweisen), never imitated:
    a mail's Message-ID, an appointment's or a card's UID, else its place
    – exactly what the next index run writes, so a note filed under it
    before that run is found after it."""
    if not rels:
        return {}
    # Appointments and cards are read whole, so only when one is asked for.
    cards = corpus.load_upload_cards(target) if any(rel.startswith(corpus.UPLOAD_CARDS) for rel in rels) else []
    records = corpus.load_uploads(target, nur=set(rels)) + cards
    schluessel.zuweisen(records, {corpus.UPLOADS_ROOT: target})
    keys = {}
    for rec in records:
        keys.setdefault(rec.get("rel"), rec["key"])
    return {rel: keys[rel] for rel in rels if rel in keys}


def file_notes(notes, target, noted):
    """Write the notes that came with the files: `noted` is (rel, name,
    text). Returns how many could not be filed."""
    failed = 0
    try:
        keys = item_keys(target, [rel for rel, _, _ in noted])
    except Exception as e:                       # noqa: BLE001 – a file no reader can read: its notes fail, the step goes on
        # Said once with its cause, counted once per note – not said again for each.
        progress.event("run.upload.note_failed", "err", name="*", error=f"{type(e).__name__}: {e}")
        return len(noted)
    for rel, name, text in noted:
        try:
            if rel not in keys:
                raise ValueError("no key")
            notes.put(keys[rel], text)
        except (ValueError, sqlite3.Error, item_notes.BadValue) as e:
            progress.event("run.upload.note_failed", "err", name=name, error=f"{type(e).__name__}: {e}")
            failed += 1
    return failed


def take_in(home, target, items, notes=None):
    """Move the committed files into the archive, each through
    versions.py; a file's note goes into `notes` (item_notes.ItemNotes)
    under the key the index will give it. Returns (new, unchanged, errors)."""
    target = Path(target)
    new = unchanged = errors = 0
    noted = []
    for item in items:
        path = staged(home, item.get("id"))
        if path is None:
            progress.event("run.upload.missing", "err", id=str(item.get("id"))[:8])
            errors += 1
            continue
        kind = item.get("kind") if item.get("kind") in KINDS else "file"
        try:
            sha = versions.sha256_file(path)
            rel = target_rel(kind, path.name, sha)
            place = target / rel
            place.parent.mkdir(parents=True, exist_ok=True)
            known = place.exists()
            tmp = place.with_name(place.name + ".tmp")
            shutil.copyfile(path, tmp)
            versions.replace(tmp, place, sha=sha)
            if kind == "file":
                meta = {**{k: item.get(k) or "" for k in TYPED}, "name": path.name, "sha256": sha,
                        "added": profile_db.now()}
                side = target / meta_rel(rel)
                side.parent.mkdir(parents=True, exist_ok=True)
                before = read_meta(target, rel)
                # The same file with the same words is no change: the
                # moment it was first added stays.
                if before and all(before.get(k) == meta[k] for k in TYPED):
                    meta = before
                else:
                    # … other words about the same bytes: the index reads a
                    # file again by its time, so the file says it changed.
                    if known:
                        os.utime(place)
                    known = False
                versions.write_text(side, json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True))
            if notes is not None and item.get("note"):
                noted.append((rel, path.name, item["note"]))
            shutil.rmtree(path.parent, ignore_errors=True)
        except OSError as e:
            progress.event("run.upload.failed", "err", name=path.name, error=f"{type(e).__name__}: {e}")
            errors += 1
            continue
        if known:
            unchanged += 1
            progress.event("run.upload.known", name=path.name)
        else:
            new += 1
            progress.event("run.upload.added", name=path.name, kind=progress.atom("upload.kind." + kind))
    errors += file_notes(notes, target, noted)
    return new, unchanged, errors


def main():
    # As a step only: the app imports this module and keeps its own streams.
    export_util.erzwinge_utf8()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="the uploads export folder")
    ap.add_argument("--staging", required=True, help="the profile folder the staged files lie in")
    a = ap.parse_args()
    listing = staging_dir(a.staging) / COMMIT_NAME
    try:
        items = json.loads(listing.read_text(encoding="utf-8")).get("items") or []
    except (OSError, ValueError, AttributeError):
        items = []
    if not items:
        progress.event("run.upload.nothing")
        progress.ergebnis(0)
        return
    new, unchanged, errors = take_in(a.staging, a.target, items,
                                     item_notes.ItemNotes(Path(a.staging) / faelle.DB_NAME))
    try:
        listing.unlink()
    except OSError:
        pass
    progress.event("run.upload.done", n=new)
    progress.ergebnis(new, unchanged=unchanged, errors=errors)
    if errors and not new and not unchanged:
        sys.exit(1)


if __name__ == "__main__":
    main()
