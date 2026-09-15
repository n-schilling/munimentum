#!/usr/bin/env python3
"""
schluessel.py – the stable key of every item in the archive.

A search hit carries a `uid` that names its file and its position in it –
fine for a page load, useless for anything that must outlive a rename: a
Teams chat whose title changed, a file moved into another folder, a mail
folder renamed. A case (faelle.py) points at items for months, so it needs
a key that comes from the item itself, not from where it lies today.

One key per item, derived from what the source cannot change:

    mail:<Message-ID>          the header the sender wrote; folder-proof
    event:<UID>                the iCalendar UID of the .ics
    contact:<UID>              the vCard UID (the Graph id)
    teams:<conversation>#<n>   the chat or channel id from the export's
                               bookkeeping, then the message id the HTML
                               carries (data-id) – older HTML: the index
    file:<item id>             the drive item from the mirror's inventory
    page:<id>, note:<id>       the SharePoint page and OneNote page ids
    planner:<task>, todo:<task>

Where the bookkeeping does not know the file (a foreign file, an old
archive), the key falls back to the path – still a key, only not proof
against a rename; the index says which by its prefix.

The index assigns the key when it writes (rag_index.lese_bestand →
zuweisen): the corpus builders put it into the record where the parse
already has it (mail, calendar, contacts, Planner, To Do), the rest comes
from the exports' state.db – read once per index run, never per file –
and chunks taken over from an older index get theirs the same way.
"""

import json
from pathlib import Path

import state_db


def _json(roh):
    try:
        daten = json.loads(roh) if roh else {}
    except ValueError:
        return {}
    return daten if isinstance(daten, dict) else {}


def kopfzeile(pfad, name):
    """One header line of a text file (.eml, .ics, .vcf): the value after
    `name:`, folded continuation lines joined – read until the first blank
    line or the end, never the whole file."""
    marke = (name + ":").lower().encode()
    mail = marke == b"message-id:"
    try:
        with open(pfad, "rb") as f:
            wert = None
            for roh in f:
                if wert is not None:
                    if roh[:1] in (b" ", b"\t"):
                        wert += roh.strip()
                        continue
                    break
                if mail and roh in (b"\r\n", b"\n"):
                    break                     # end of the mail header
                if roh[:len(marke)].lower() == marke:
                    wert = roh[len(marke):].strip()
            return wert.decode("utf-8", "replace").strip() if wert else None
    except OSError:
        return None


class Karten:
    """The lookup maps behind the keys, one per export root, read on first
    use from the exports' bookkeeping. `ordner` maps the index's root
    names (teams, outlook, onedrive, sharepoint, pages, onenote) to their
    folders; a missing folder yields empty maps."""

    def __init__(self, ordner):
        self.ordner = {k: (Path(v) if v else None) for k, v in (ordner or {}).items()}
        self._cache = {}

    def _pfad(self, root):
        p = self.ordner.get(root)
        return p if p is not None and p.is_dir() else None

    @staticmethod
    def _spiegel(basis):
        """Every folder below `basis` with a state.db of its own."""
        return sorted(p.parent for p in basis.rglob(state_db.DB_NAME) if p.parent != basis)

    def konversationen(self):
        """Teams: rel of the conversation's HTML -> its chat or channel id."""
        if "teams" not in self._cache:
            karte = {}
            wurzel = self._pfad("teams")
            if wurzel is not None:
                db = state_db.StateDb(wurzel)
                saetze = {k: _json(v) for k, v in db.saetze_lesen("conversations").items()}
                if not saetze:
                    saetze = _json(db.kv_lesen("state") or "").get("conversations") or {}
                for key, rec in saetze.items():
                    if isinstance(rec, dict) and rec.get("rel"):
                        karte[rec["rel"]] = key[3:] if key.startswith("ch:") else key
            self._cache["teams"] = karte
        return self._cache["teams"]

    def dateien(self, root):
        """Mirrored files: rel below the root -> drive item id. OneDrive keeps
        one inventory at its root, SharePoint one per library, Teams one per
        channel mirror below channels/."""
        name = "dateien:" + root
        if name not in self._cache:
            karte = {}
            wurzel = self._pfad(root)
            if wurzel is not None:
                if root == "onedrive":
                    for kennung, e in state_db.StateDb(wurzel).bestand_lesen().items():
                        karte[e["rel"]] = kennung
                else:
                    basis = wurzel / "channels" if root == "teams" else wurzel
                    for spiegel in (self._spiegel(basis) if basis.is_dir() else []):
                        praefix = spiegel.relative_to(wurzel).as_posix() + "/"
                        for kennung, e in state_db.StateDb(spiegel).bestand_lesen().items():
                            karte[praefix + e["rel"]] = kennung
            self._cache[name] = karte
        return self._cache[name]

    def seiten(self):
        """SharePoint pages: rel -> page id."""
        if "pages" not in self._cache:
            karte = {}
            wurzel = self._pfad("pages")
            if wurzel is not None:
                for kennung, e in state_db.StateDb(wurzel).seiten_lesen().items():
                    if e.get("rel"):
                        karte[e["rel"]] = kennung
            self._cache["pages"] = karte
        return self._cache["pages"]

    def notizen(self):
        """OneNote: rel below the root -> page id, per notebook."""
        if "onenote" not in self._cache:
            karte = {}
            wurzel = self._pfad("onenote")
            if wurzel is not None:
                for nb in self._spiegel(wurzel):
                    db = state_db.StateDb(nb)
                    praefix = nb.relative_to(wurzel).as_posix() + "/"
                    saetze = {k: _json(v) for k, v in db.saetze_lesen("pages").items()}
                    if not saetze:
                        saetze = _json(db.kv_lesen("pages") or "")
                    for kennung, rec in saetze.items():
                        if isinstance(rec, dict) and rec.get("rel"):
                            karte[praefix + rec["rel"]] = kennung
            self._cache["onenote"] = karte
        return self._cache["onenote"]


def _index_aus_uid(uid):
    try:
        return int(str(uid).rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return 0


def fuer(chunk, karten):
    """The key of one chunk (record): what it already carries, else what the
    bookkeeping knows, else the path."""
    if chunk.get("key"):
        return chunk["key"]
    src, root, rel = chunk.get("src"), chunk.get("root"), chunk.get("rel") or ""
    wurzel = karten._pfad(root)
    if src == "outlook":
        kennung = kopfzeile(wurzel / rel, "Message-ID") if wurzel else None
        return f"mail:{kennung}" if kennung else f"mail:{rel}"
    if src == "kalender":
        kennung = kopfzeile(wurzel / rel, "UID") if wurzel else None
        return f"event:{kennung}" if kennung else f"event:{rel}"
    if src == "kontakte":
        kennung = kopfzeile(wurzel / rel, "UID") if wurzel else None
        return f"contact:{kennung}" if kennung else f"contact:{rel}"
    if src == "teams":
        konv = karten.konversationen().get(rel) or rel
        return f"teams:{konv}#{chunk.get('msg_id') or _index_aus_uid(chunk.get('uid'))}"
    if src == "datei":
        kennung = karten.dateien(root).get(rel)
        return f"file:{kennung}" if kennung else f"file:{root}:{rel}"
    if src == "pages":
        kennung = karten.seiten().get(rel)
        return f"page:{kennung}" if kennung else f"page:{rel}"
    if src == "onenote":
        kennung = karten.notizen().get(rel)
        return f"note:{kennung}" if kennung else f"note:{rel}"
    if src in ("planner", "todo"):
        # uid: "planner:<board folder>/<task id>:0"
        mitte = str(chunk.get("uid") or "").split(":", 1)[-1].rsplit(":", 1)[0]
        return f"{src}:{mitte.rsplit('/', 1)[-1]}"
    return f"{src}:{root}:{rel}"


def zuweisen(chunks, ordner):
    """Give every chunk without a key its key – the maps are read once."""
    karten = Karten(ordner)
    for c in chunks:
        if not c.get("key"):
            c["key"] = fuer(c, karten)
    return chunks


def stabil(key):
    """Does the key stand on the item's own identity – or only on its path?"""
    if not key:
        return False
    art, _, rest = key.partition(":")
    if art in ("mail", "event", "contact"):
        return "/" not in rest and rest != ""
    if art == "teams":
        return "/" not in rest.split("#", 1)[0]
    if art in ("file", "page", "note"):
        return "/" not in rest and ":" not in rest
    return art in ("planner", "todo")
