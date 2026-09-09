#!/usr/bin/env python3
"""
drive_mirror.py – the shared mirror core for Graph drives.

To Graph, OneDrive and a SharePoint document library are the same thing: a
drive with a delta feed. This module holds everything a mirror needs – delta
walk, download, inventory, tombstones, folder tree, completeness check –
parametrised by three things the callers (onedrive_export.py,
sharepoint_export.py) supply:

  * the drive base URL (``/me/drive`` or ``/drives/{id}``) on the client,
  * the output folder (one per drive; its state.db lives inside it),
  * a Selection – path rules, size limit, extension include/exclude.

The mirror promise is the same everywhere: the CURRENT version of every
file is kept, deleted files stay here with a tombstone entry.
"""

import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import export_util
import settings
import folders
import graph_client
import progress

GRAPH = graph_client.GRAPH

DATEI_DIR = "Dateien"           # root in the output folder; the rules match on it

# Network, throttling, retry and paging live in graph_client.py; all that
# stays here is the download timeout – a big file takes longer than a page.
TIMEOUT_BYTES = (30, 600)


def workers():
    """Parallel drive requests – the mirrors' shared knob (mirror_workers).

    Drives are throttled by request budget, not by the mailbox's four-slot
    limit; Graph documents no fixed concurrency, so the cap of 16 is our own
    restraint and 429 waits stay visible in the log."""
    return max(1, min(settings.number("MIRROR_WORKERS", "mirror_workers"), 16))
SEITE = 999                     # $top: one page instead of many small ones


class Selection:
    """What the mirror takes: path rules, a size cap, extension filters.

    Everything optional – the empty Selection takes the whole drive. The
    extension lists hold bare lowercase suffixes ("pdf", "docx"); exclude
    wins over include, and an empty include list means "every type".
    """

    def __init__(self, rules=None, max_bytes=0, include_ext=None,
                 exclude_ext=None, scope=None):
        self.rules = rules or []
        # Scope rules are machinery (a folder URL narrowing a library), not
        # user choice: what falls outside is IGNORED silently, never counted
        # as "excluded" – the report would otherwise drown in the rest of
        # the library.
        self.scope = scope or []
        self.max_bytes = max(0, int(max_bytes or 0))
        self.include_ext = {e.lower().lstrip(".") for e in (include_ext or ())
                            if str(e).strip()}
        self.exclude_ext = {e.lower().lstrip(".") for e in (exclude_ext or ())
                            if str(e).strip()}

    def _ext_ok(self, rel):
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
        if ext in self.exclude_ext:
            return False
        return not self.include_ext or ext in self.include_ext

    def im_scope(self, rel):
        """Inside the narrowed subtree? Without scope rules: always."""
        return not self.scope or folders.gilt(rel, self.scope)

    def pfad_ok(self, rel):
        """Only the path scope – the type list counts inside it, unfiltered."""
        return folders.gilt(rel, self.rules)

    def takes(self, rel, size):
        """One verdict per file – used identically by plan, check and preview."""
        if not self.im_scope(rel) or not self.pfad_ok(rel):
            return False
        if self.max_bytes and int(size or 0) > self.max_bytes:
            return False
        return self._ext_ok(rel)


# ---------------------------------------------------------------------------
# HTTP – the client comes from graph_client.py; only drive specifics live here
# ---------------------------------------------------------------------------
class DriveOps:
    """Drive specifics on top of the shared client: download and delta.

    ``drive_base`` names the drive – callers set it (``…/me/drive`` for
    OneDrive, ``…/drives/{id}`` for a SharePoint library).
    """

    drive_base = f"{GRAPH}/me/drive"

    def lade(self, item_id, ziel, geaendert=None):
        """Write a file's content to `ziel` – in chunks, not in one piece.

        Pulling a large file completely into memory just to write it to
        disk afterwards would, on a small machine, be the one reason a run
        fails. The bytes go to a sidecar file renamed only at the end: an
        abort then leaves no half file that would count as complete on the
        next run.
        """
        url = f"{self.drive_base}/items/{item_id}/content"
        r = self.stream(url, timeout=TIMEOUT_BYTES, label=" (Inhalt)")
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_name(ziel.name + ".teil")
        groesse = 0
        with open(tmp, "wb") as f:
            for stueck in r.iter_content(chunk_size=1 << 20):
                if stueck:
                    f.write(stueck)
                    groesse += len(stueck)
        os.replace(tmp, ziel)
        # Take the modification time from the drive. Without it every file
        # would carry the moment of its download, and the index would hold
        # hundreds of files with the same date – sorting by date would be
        # worthless, and rebuilding the mirror would change them again.
        if geaendert:
            try:
                os.utime(ziel, (geaendert, geaendert))
            except OSError:
                pass
        return groesse

    def delta_seiten(self, weiter=None):
        """Delta page by page: (entries, resume link, final delta link).

        The nextLink of every page is a valid resumption point – exactly
        what a checkpointed walk persists so a killed run continues where
        it stopped instead of walking 300k entries again."""
        url = weiter or f"{self.drive_base}/root/delta?$top={SEITE}"
        while True:
            d = self.get(url)
            nxt = d.get("@odata.nextLink")
            if nxt:
                yield d.get("value", []), nxt, None
                url = nxt
            else:
                yield d.get("value", []), None, d.get("@odata.deltaLink")
                return

    def delta(self, weiter=None):
        """All delta entries, page by page. Yields the new link at the end."""
        for eintraege, cursor, fertig in self.delta_seiten(weiter):
            for eintrag in eintraege:
                yield eintrag, None
            if cursor is None:
                yield None, fertig


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
kuerzel = export_util.kuerzel


def safe(name, maxlen=120, kennung=None):
    """A name segment the file system can trust.

    Truncation is the delicate part: on a real drive it once ate the
    extension of two files whose names only differed after 120 characters –
    both landed on the same path, and the second download failed on the
    partial file the first had already cleaned away. So the extension is
    kept and a short tag from the ID makes the name unique again.
    """
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip().strip(". ")
    name = re.sub(r"\s+", " ", name) or "unbenannt"
    if len(name) <= maxlen:
        return name
    stamm, punkt, endung = name.rpartition(".")
    if not punkt or len(endung) > 12 or not stamm:
        stamm, endung = name, ""
    marke = "__" + kuerzel(kennung or name)
    schwanz = ("." + endung) if endung else ""
    platz = max(1, maxlen - len(marke) - len(schwanz))
    return stamm[:platz] + marke + schwanz


def rel_pfad(eintrag):
    """The path an entry lands under here – "Dateien/Ordner/Datei.pdf".

    Graph delivers the parent path as "/drive/root:/Ordner/Unter",
    percent-encoded. Each segment is sanitised on its own: otherwise a name
    containing "/" or ".." could lead out of the output folder.
    """
    roh = (eintrag.get("parentReference") or {}).get("path") or ""
    roh = unquote(roh)
    _, _, rest = roh.partition("root:")
    stuecke = [safe(s) for s in rest.strip("/").split("/") if s not in ("", ".", "..")]
    name = safe(eintrag.get("name") or "unbenannt", kennung=eintrag.get("id"))
    return "/".join([DATEI_DIR, *stuecke, name])


def geaendert_am(eintrag):
    """When the file was last modified – as a timestamp.

    fileSystemInfo carries the time of the client that uploaded it and is
    therefore the more honest value; lastModifiedDateTime on the entry is
    the fallback.
    """
    for roh in ((eintrag.get("fileSystemInfo") or {}).get("lastModifiedDateTime"),
                eintrag.get("lastModifiedDateTime")):
        dt = export_util.graph_zeit(roh)
        if dt is not None:
            return dt.timestamp()
    return None


def ist_ordner(eintrag):
    return "folder" in eintrag and "file" not in eintrag


def ist_paket(eintrag):
    """Graph reports OneNote notebooks as "package".

    They are folders with .one files inside; the individual files appear in
    the delta anyway and get backed up. The package itself is not content.
    """
    return "package" in eintrag


# ---------------------------------------------------------------------------
# Inventory: ID -> path, version, size
# ---------------------------------------------------------------------------
class Bestand:
    """What lies here, per Graph ID.

    The cTag changes when the CONTENT changes (the eTag also on pure
    metadata edits). That is exactly the question before every download, so
    it is what gets stored. The size sits next to it so an aborted file
    does not pass as complete.

    In-memory base; persistence comes from state_db.DbBestand.
    """

    def __init__(self):
        self.eintraege = {}
        self._lock = threading.Lock()

    def aktuell(self, kennung, ctag, groesse, wurzel):
        """Is exactly this version already here?"""
        e = self.eintraege.get(kennung)
        if not e or e["ctag"] != ctag:
            return False
        datei = wurzel / e["rel"]
        try:
            return datei.stat().st_size == groesse
        except OSError:
            return False

    def merke(self, kennung, rel, ctag, groesse):
        with self._lock:
            self.eintraege[kennung] = {"rel": rel, "ctag": ctag, "size": groesse}

    def vergiss(self, kennung):
        with self._lock:
            return self.eintraege.pop(kennung, None)

    def schreibe(self):
        pass                   # the backend persists (state_db.DbBestand)




# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
_EINTRAG_FELDER = ("id", "name", "size", "cTag", "lastModifiedDateTime")


def verschlanke(e):
    """Keep only what plan and check read – a raw Graph entry is about ten
    times bigger, and 300k of them once held 1.3 GB of RAM."""
    s = {k: e[k] for k in _EINTRAG_FELDER if k in e}
    for k in ("deleted", "root", "file", "package"):
        if k in e:
            s[k] = {}                  # only membership is ever tested
    if "folder" in e:
        s["folder"] = {"childCount": (e.get("folder") or {}).get("childCount")}
    if "parentReference" in e:
        s["parentReference"] = {
            "path": (e.get("parentReference") or {}).get("path")}
    zeit = (e.get("fileSystemInfo") or {}).get("lastModifiedDateTime")
    if zeit:
        s["fileSystemInfo"] = {"lastModifiedDateTime": zeit}
    return s


def seiten(graph, weiter):
    """Delta pages with their resume cursor.

    Falls back to the entry-wise ``delta()`` – one page, no mid-walk
    cursor – so the lean graph fakes in tests stay valid."""
    if hasattr(graph, "delta_seiten"):
        yield from graph.delta_seiten(weiter)
        return
    seite = []
    for eintrag, fertig in graph.delta(weiter):
        if eintrag is not None:
            seite.append(eintrag)
        else:
            yield seite, None, fertig


def walk(graph, zustand, weiter):
    """Write the delta feed page by page into the store – each page with
    its resume link, so an aborted run continues instead of starting over.
    Returns the new delta link."""
    gesamt = zustand.walk_status()["n"]
    fertig = None
    for eintraege, cursor, delta_link in seiten(graph, weiter):
        vorher = gesamt
        zustand.walk_ergaenzen([verschlanke(e) for e in eintraege], cursor)
        gesamt += len(eintraege)
        if gesamt // 2000 > vorher // 2000:
            progress.event("run.drive.walking", n=gesamt)
        if cursor is None:
            fertig = delta_link
    zustand.walk_abschliessen(fertig)
    return fertig


def sammle(graph, weiter):
    """Run through the delta once; returns (entries, new link).

    In-memory variant for the pure check runs – the mirror itself goes
    through walk() and the store in the state backend."""
    eintraege, link = [], None
    for eintrag, fertig in graph.delta(weiter):
        if eintrag is not None:
            eintraege.append(verschlanke(eintrag))
            if len(eintraege) % 2000 == 0:
                progress.event("run.drive.walking", n=len(eintraege))
        else:
            link = fertig
    return eintraege, link


def plane(eintraege, bestand, wurzel, auswahl):
    """Turn the delta entries into a task list – with no network access.

    Kept separate from downloading so the "what do we do" decision can be
    tested without signing in.
    """
    laden, verschoben, geloescht, ausgelassen, baum = [], [], [], 0, []
    entfernt = set()
    for e in eintraege:
        kennung = e.get("id")
        if not kennung:
            continue
        if "deleted" in e:
            entfernt.add(kennung)
            alt = bestand.vergiss(kennung)
            if alt:
                geloescht.append(alt["rel"])
            continue
        if "root" in e:
            # The root belongs in the tree even though Graph does not report
            # it as a folder. Without it, every file lying directly in the
            # drive counts as "only present locally" – a false alarm in the
            # export list.
            baum.append({"id": kennung, "pfad": DATEI_DIR, "name": DATEI_DIR,
                         "elemente": int((e.get("folder") or {}).get("childCount") or 0)})
            continue
        rel = rel_pfad(e)
        if not auswahl.im_scope(rel):
            continue                    # outside the narrowed subtree: silent
        if ist_ordner(e) or ist_paket(e):
            baum.append({"id": kennung, "pfad": rel, "name": e.get("name") or "",
                         "elemente": int((e.get("folder") or {}).get("childCount") or 0)})
            continue
        if "file" not in e:
            continue
        groesse = int(e.get("size") or 0)
        if not auswahl.takes(rel, groesse):
            ausgelassen += 1
            continue
        alt = bestand.eintraege.get(kennung)
        if alt and alt["rel"] != rel:
            # Renames and moves keep the ID; the cTag changes only with the
            # CONTENT. So moving the file along locally is enough – loading
            # it again would, for a renamed 300 MB video, be the most
            # expensive conceivable way to gain nothing. If the content
            # changed at the same time, it still ends up in `laden` below,
            # and the download then lands on the new path already.
            verschoben.append((alt["rel"], rel))
        if bestand.aktuell(kennung, e.get("cTag") or "", groesse, wurzel):
            if alt and alt["rel"] != rel:
                bestand.merke(kennung, rel, alt["ctag"], alt["size"])
            continue
        laden.append({"id": kennung, "rel": rel, "ctag": e.get("cTag") or "",
                      "size": groesse, "mtime": geaendert_am(e)})
    # A delta may name the same entry several times (and a resumed walk may
    # repeat a page): the last version counts – otherwise two threads would
    # write to the same target file at the same time.
    laden = list({a["id"]: a for a in laden}.values())
    return {"laden": laden, "verschoben": verschoben, "geloescht": geloescht,
            "ausgelassen": ausgelassen, "baum": baum, "entfernt": entfernt}


def baum_zusammenfuehren(alt, geaendert, entfernt):
    """Carry the folder tree forward instead of replacing it.

    A delta run delivers only the CHANGED folders. Overwriting the tree
    with them would shrink it to a handful on the second run – and report
    every other folder as gone.
    """
    nach_id = {e["id"]: e for e in (alt or {}).get("ordner", [])}
    for e in geaendert:
        nach_id[e["id"]] = e
    for kennung in entfernt or ():
        nach_id.pop(kennung, None)
    return sorted(nach_id.values(), key=lambda e: e["pfad"].lower())


def verschiebe(wurzel, paare):
    """Move renamed or relocated files along instead of downloading again."""
    bewegt = 0
    for alt, neu in paare:
        a, n = wurzel / alt, wurzel / neu
        if a.exists() and not n.exists():
            n.parent.mkdir(parents=True, exist_ok=True)
            try:
                a.replace(n)
                bewegt += 1
            except OSError:
                pass
    return bewegt


def hole_alle(graph, wurzel, bestand, aufgaben, arbeiter):
    """The planned downloads – in parallel, with progress."""
    fertig = fehler = 0
    gesamt = len(aufgaben)
    if not gesamt:
        return 0, 0
    progress.melde(0, gesamt, "files")
    with ThreadPoolExecutor(max_workers=arbeiter) as pool:
        auftrag = {pool.submit(graph.lade, a["id"], wurzel / a["rel"], a.get("mtime")): a
                   for a in aufgaben}
        for f in as_completed(auftrag):
            a = auftrag[f]
            try:
                geladen = f.result()
                bestand.merke(a["id"], a["rel"], a["ctag"], geladen)
                fertig += 1
            except Exception as e:
                fehler += 1
                progress.event("run.file_failed", "err", name=a["rel"],
                               error=f"{type(e).__name__}: {e}")
            if (fertig + fehler) % 10 == 0 or fertig + fehler == gesamt:
                progress.melde(fertig + fehler, gesamt, "files")
                bestand.schreibe()
    bestand.schreibe()
    return fertig, fehler


def lauf(graph, out, auswahl, arbeiter, still=False, zustand=None):
    """One full mirror pass for one drive; returns the result counts.

    ``still`` suppresses the result event – sharepoint_export mirrors several
    drives in one step and reports one combined result at the end.

    The walk is checkpointed: every delta page lands in the state backend
    together with its resume link, so a killed run continues mid-walk
    instead of starting over – and when only the downloads were left, the
    next run replans from the stored walk without asking Graph at all.
    """
    out = Path(out)
    wurzel = out
    zustand = zustand or _db_zustand(out)
    bestand = zustand.bestand()
    status = zustand.walk_status()
    if status["fertig"]:
        # The aborted run had already finished enumerating – only the work
        # after it was missing. Do not ask again, just catch up.
        progress.event("run.drive.replan", n=status["n"])
        neuer_link = status["fertig"]
    else:
        weiter = status["cursor"] or zustand.delta_lesen()
        if status["cursor"]:
            progress.event("run.drive.resume", n=status["n"])
        else:
            progress.event("run.drive.delta" if weiter else "run.drive.full")
        try:
            neuer_link = walk(graph, zustand, weiter)
        except requests.HTTPError as e:
            # 410 Gone: Graph expired the link – the saved delta pointer or
            # a stale walk cursor. Resync once, in full.
            if weiter is None or getattr(e.response, "status_code", 0) != 410:
                raise
            progress.event("run.drive.resync", "warn")
            zustand.delta_loeschen()
            zustand.walk_leeren()
            neuer_link = walk(graph, zustand, None)
    progress.event("run.scanned", n=zustand.walk_status()["n"],
                   unit=progress.atom("progress.unit.entries"))
    plan = plane(zustand.walk_eintraege(), bestand, wurzel, auswahl)

    bewegt = verschiebe(wurzel, plan["verschoben"])
    fertig, fehler = hole_alle(graph, wurzel, bestand, plan["laden"], arbeiter)
    # Always, not only after downloads: a deletion or a move changes the
    # inventory too. Without this write a deleted file would still be listed
    # on the next run and its tombstone would be set a second time.
    bestand.schreibe()

    jetzt = datetime.now(UTC).isoformat(timespec="seconds")
    zustand.verschwunden_ergaenzen(plan["geloescht"], jetzt)
    alt_baum = zustand.baum_lesen()
    baum = baum_zusammenfuehren(alt_baum, plan["baum"], plan["entfernt"])
    if baum or alt_baum:
        zustand.baum_schreiben(baum, alt_baum)
    if not fehler:
        # Only now: an aborted run must not advance the delta pointer. The
        # walk store has done its duty and goes with it.
        if neuer_link:
            zustand.delta_schreiben(neuer_link, bestand=bestand)
        zustand.walk_leeren()
    else:
        # Walk store and finish link stay put: the next run replans from
        # them and only fetches what is still missing.
        progress.event("run.drive.retry", "warn")
    zahlen = {"new": fertig, "excluded": plan["ausgelassen"], "errors": fehler,
              "moved": bewegt, "gone": len(plan["geloescht"])}
    if not still:
        progress.ergebnis(fertig, excluded=plan["ausgelassen"], errors=fehler,
                          extra={"moved": bewegt, "gone": len(plan["geloescht"])})
    return zahlen


def _db_zustand(out):
    """Default state backend: the folder's state.db. Imported lazily –
    state_db imports this module for the Bestand base class."""
    import state_db
    return state_db.DbZustand(out)


def pruefe_vollstaendigkeit(eintraege, out, auswahl, weg=None):
    """What the drive holds against what lies here – per folder.

    The same shape as for the mailbox, so the UI can draw it without a
    second view. The difference is in the question: for the mailbox Graph
    counts items per folder, here the delta knows every single file – the
    check is therefore more precise and also knows whether a file arrived
    only half. The byte sums alongside make it double as the size preview:
    what would a mirror run fetch, and what would it leave out.
    """
    if weg is None:
        weg = _db_zustand(out).verschwunden_lesen()
    je = {}
    typen = {}
    ausgelassen = 0
    ausgelassen_bytes = 0
    ausgelassene = set()
    for e in eintraege:
        if "deleted" in e or "file" not in e or "root" in e:
            continue
        rel = rel_pfad(e)
        if not auswahl.im_scope(rel):
            continue                    # outside the narrowed subtree: silent
        ordner = rel.rsplit("/", 1)[0] if "/" in rel else DATEI_DIR
        groesse = int(e.get("size") or 0)
        # Counted inside the path scope but BEFORE type and size filters:
        # this list is what include/exclude gets decided on, so it must show
        # what is there, not what survived.
        if auswahl.pfad_ok(rel):
            name = rel.rsplit("/", 1)[-1]
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            z = typen.setdefault(ext, {"ext": ext, "n": 0, "bytes": 0})
            z["n"] += 1
            z["bytes"] += groesse
        if not auswahl.takes(rel, groesse):
            ausgelassen += 1
            ausgelassen_bytes += groesse
            ausgelassene.add(ordner)
            continue
        z = je.setdefault(ordner, {"ordner": ordner, "erwartet": 0, "vorhanden": 0,
                                   "geloescht": 0, "ausgelassen": False, "fehlt": 0,
                                   "bytes": 0})
        z["erwartet"] += 1
        z["bytes"] += groesse
        datei = Path(out) / rel
        try:
            da = datei.stat().st_size == groesse
        except OSError:
            da = False
        if da:
            z["vorhanden"] += 1
    # Tombstones belong in the balance: they explain why more lies here
    # than the drive still knows – they are not a gap.
    for rel in weg:
        ordner = rel.rsplit("/", 1)[0] if "/" in rel else DATEI_DIR
        z = je.setdefault(ordner, {"ordner": ordner, "erwartet": 0, "vorhanden": 0,
                                   "geloescht": 0, "ausgelassen": False, "fehlt": 0,
                                   "bytes": 0})
        z["geloescht"] += 1
    for o in ausgelassene:
        je.setdefault(o, {"ordner": o, "erwartet": 0, "vorhanden": 0,
                          "geloescht": 0, "ausgelassen": True, "fehlt": 0,
                          "bytes": 0})
    for z in je.values():
        z["fehlt"] = max(0, z["erwartet"] - z["vorhanden"])
    liste = sorted(je.values(), key=lambda z: (-z["fehlt"], z["ordner"]))
    return {
        "geprueft": datetime.now(UTC).isoformat(timespec="seconds"),
        "ordner": liste,
        "erwartet": sum(z["erwartet"] for z in liste),
        "vorhanden": sum(z["vorhanden"] for z in liste),
        "geloescht": sum(z["geloescht"] for z in liste),
        "fehlt": sum(z["fehlt"] for z in liste),
        "ausgelassen": ausgelassen,
        "ausgelassene_ordner": sorted(ausgelassene)[:20],
        "bytes": sum(z["bytes"] for z in liste),
        "bytes_ausgelassen": ausgelassen_bytes,
        "typen": sorted(typen.values(), key=lambda z: -z["bytes"]),
    }


def nur_pruefen(graph, out, auswahl, still=False, zustand=None):
    """--check: only report what is missing. Loads nothing, leaves the pointer alone."""
    out = Path(out)
    zustand = zustand or _db_zustand(out)
    eintraege, _ = sammle(graph, None)
    bericht = pruefe_vollstaendigkeit(eintraege, out, auswahl,
                                      weg=zustand.verschwunden_lesen())
    zustand.bericht_schreiben(bericht)
    if not still:
        progress.ergebnis(0, excluded=bericht["ausgelassen"],
                          extra={"expected": bericht["erwartet"],
                                 "present": bericht["vorhanden"],
                                 "missing": bericht["fehlt"]})
    return bericht


def nur_ordner(graph, out, auswahl, still=False, zustand=None):
    """--folders: fetch only the structure, download nothing.

    Deliberately enumerates IN FULL and ignores the stored delta pointer: a
    sync should show the whole tree, not the handful of folders that
    changed since yesterday. And it does not advance the pointer – the next
    export would otherwise consider the never-fetched files done.
    """
    out = Path(out)
    zustand = zustand or _db_zustand(out)
    bestand = zustand.bestand()
    eintraege, _ = sammle(graph, None)
    plan = plane(eintraege, bestand, out, auswahl)
    alt = zustand.baum_lesen()
    daten = zustand.baum_schreiben(
        baum_zusammenfuehren(alt, plan["baum"], plan["entfernt"]), alt)
    z = folders.zusammenfassung(daten, auswahl.rules)
    progress.event("run.sync.result", total=z["ordner_gesamt"],
                   chosen=z["ordner_gewaehlt"],
                   unit=progress.atom("progress.unit.folders"))
    if daten["neu"] or daten["verschwunden"] or daten["umbenannt"]:
        progress.event("run.sync.changed", new=len(daten["neu"]),
                       gone=len(daten["verschwunden"]),
                       renamed=len(daten["umbenannt"]))
    if not still:
        progress.ergebnis(len(daten["neu"]),
                          extra={"total": z["ordner_gesamt"],
                                 "gone": len(daten["verschwunden"]),
                                 "renamed": len(daten["umbenannt"])})
    return daten
