#!/usr/bin/env python3
"""
archive_check.py – does the bookkeeping match the disk?

The outward check (completeness.py, the exports' --check) asks Microsoft;
this one asks nobody. It walks every export folder and holds each state.db
against the files that lie there – five numbers per source, all of them
files (completeness.zustand):

    stimmig         record and file agree (mirrors: the size as well)
    fehlt           the bookkeeping knows a file that is not there – the
                    next run fetches it again, nothing else is needed
    fremd           a file lies here that no bookkeeping knows – it stays;
                    a run neither fetches nor removes it
    unvollstaendig  a mirrored file with a size other than recorded – half
                    arrived; the next run fetches it again
    verweigert      Microsoft refuses the item (a 403, a malware verdict):
                    no copy here, and no fetch will bring one – its own
                    number, outside the four above, because there is
                    nothing to do about it (export_util.permanent_mark)
    weg             the item was gone at Microsoft (404) before a copy
                    came – the same kind of number
    verloren        a tombstone says the file was kept, and it is gone: the
                    item is gone at Microsoft and here alike
    vermerkt        the user noted a loss (table verloren): the record
                    stays, the file is not expected back, the row is quiet

Two more things: the index against the archive – files changed, new or
gone since the index last read them, which is what "Index only" would
catch up on – and every state.db's own integrity (SQLite's quick_check).

A missing file is the next resync's job ("Fetch again"). When one is
still missing after that – gone at Microsoft, or left out by today's
rules – the report says so: every row carries `fehlt_seit`, the first
report that found its missing files, and `nachgeholt`, the source's last
completed resync (--nachgeholt, from the run history). A resync after the
finding that brought nothing makes the entry a dead card; noting it as
lost is then the user's one click.

The check itself writes nothing but the report (--report FILE). What the
user then does with a finding is one explicit action each, asked for in
the overview and run as a step of its own (--aktion, --quelle), so the
run window shows it and the run history keeps it:

    vermerken       lost tombstones – and, with --arten fehlt, files still
                    missing after a resync – are noted as lost; the
                    records stay
    beiseitelegen   foreign files move into _fremd/<stamp>/ inside the
                    export folder, path kept, with a list of what moved;
                    zurueckholen moves them back – nothing is ever deleted
    neu-aufbauen    a damaged state.db is set aside as state.db.beschaedigt-
                    <stamp>, what it still yields is salvaged into a fresh
                    one, and the source's run that follows in the same job
                    fills it as on a first export

Runs as a subprogram of app.py with the same folders as the index:

    teams outlook onedrive --sharepoint DIR --pages DIR --planner DIR
    --todo DIR --onenote DIR --store DIR --report FILE
    [--nachgeholt JSON | --aktion NAME --quelle KEY [--arten a,b]]
"""

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, UTC
from pathlib import Path

import completeness
import corpus
import drive_mirror
import export_util
import progress
import state_db
import store_layout

export_util.erzwinge_utf8()

DB_BESCHAEDIGT = "ana.archiv.reason.db"
KEIN_INDEX = "ana.archiv.reason.noindex"
ALTER_INDEX = "ana.archiv.reason.oldindex"
FREMD_DIR = corpus.FREMD_DIR         # where "set aside" puts foreign files
ONENOTE_SUFFIX = corpus.ONENOTE_SUFFIX   # a page's folder for images and attachments
LISTE = "liste.json"                 # what one set-aside batch moved
GRENZE = 5000                        # findings named per kind and source

# Teams: the four conversation folders; files below them are exports or,
# under a mirror root, mirrored channel folders.
TEAMS_ARTEN = ("1on1", "group", "meeting", "channels")

# How the log names each folder – text keys, spelled out so the language
# files know them.
TITEL = {"outlook": "ana.archiv.quelle.outlook", "teams": "ana.archiv.quelle.teams",
         "onedrive": "ana.archiv.quelle.onedrive", "sharepoint": "ana.archiv.quelle.sharepoint",
         "sharepoint_pages": "ana.archiv.quelle.sharepoint_pages",
         "planner": "ana.archiv.quelle.planner", "todo": "ana.archiv.quelle.todo",
         "onenote": "ana.archiv.quelle.onenote"}


# ---------------------------------------------------------------------------
# Helpers over the disk
# ---------------------------------------------------------------------------
def _dateien(wurzel, unter="", endungen=None, ohne=()):
    """rel -> size of every file below wurzel/unter, rels relative to
    wurzel. State files, dot files, the set-aside folder and the given
    subfolders (rels) are not files of the archive."""
    wurzel = Path(wurzel)
    basis = wurzel / unter if unter else wurzel
    if not basis.is_dir():
        return {}
    out = {}
    for p in basis.rglob("*"):
        if not p.is_file() or p.name.startswith(".") or p.name.startswith("state.db"):
            continue
        if endungen and p.suffix.lower() not in endungen:
            continue
        rel = p.relative_to(wurzel).as_posix()
        if rel.split("/", 1)[0] == FREMD_DIR:
            continue
        if any(rel == o or rel.startswith(o + "/") for o in ohne):
            continue
        try:
            out[rel] = p.stat().st_size
        except OSError:
            continue
    return out


def _groesse(wurzel, rel):
    """The file's size, None when it is not there."""
    try:
        return (Path(wurzel) / rel).stat().st_size
    except OSError:
        return None


def _ordner(rel):
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _json(roh):
    try:
        daten = json.loads(roh) if roh else {}
    except ValueError:
        return {}
    return daten if isinstance(daten, dict) else {}


def _spiegel_wurzeln(basis):
    """Every mirror below a folder: a subfolder with a state.db of its own
    (a SharePoint library, a team's channel folders) – never inside the
    set-aside folder."""
    basis = Path(basis)
    if not basis.is_dir():
        return []
    return sorted(p.parent for p in basis.rglob(state_db.DB_NAME)
                  if p.parent != basis
                  and p.relative_to(basis).parts[:1] != (FREMD_DIR,))


def _db_ok(ordner):
    """"ok", or the reason a state.db cannot be trusted."""
    return state_db.StateDb(ordner).integritaet()


def _beschaedigt(quelle, grund):
    return completeness.zustand(quelle, stand=completeness.NICHT, grund=DB_BESCHAEDIGT,
                                fehler=[completeness.fehler(quelle, grund)])


def _beiseite(wurzel):
    """How many files an earlier "set aside" holds under _fremd/."""
    ordner = Path(wurzel) / FREMD_DIR
    if not ordner.is_dir():
        return 0
    return sum(1 for p in ordner.rglob("*") if p.is_file() and p.name != LISTE)


class _Zaehler:
    """The five numbers, the same per row, and the files behind them – plus
    what was noted as lost, a count that troubles no row."""

    def __init__(self):
        self.summe = Counter()
        self.je = {}
        self.befunde = {k: [] for k in completeness.BEFUND_ARTEN}
        self.gekappt = False

    def zaehle(self, pfad, was, rel=None):
        self.summe[was] += 1
        if was in ("stimmig", "vermerkt"):
            return
        self.je.setdefault(pfad, Counter())[was] += 1
        if rel is None:
            return
        if len(self.befunde[was]) < GRENZE:
            self.befunde[was].append(rel)
        else:
            self.gekappt = True

    def zeilen(self):
        return [completeness.zustandszeile(pfad, **z) for pfad, z in self.je.items()]

    def zustand(self, quelle, fehler=(), beiseite=0):
        return completeness.zustand(quelle, zeilen=self.zeilen(), fehler=fehler,
                                    befunde=self.befunde, gekappt=self.gekappt,
                                    beiseite=beiseite,
                                    **{k: self.summe[k] for k in
                                       ("stimmig", "fehlt", "fremd", "unvollstaendig",
                                        "verloren", "vermerkt", "verweigert", "weg")})


def _bestand_pruefen(z, wurzel, bekannt, gemessen=False, zeile=None, praefix="",
                     vermerkt=()):
    """Records against files: `bekannt` maps rel -> recorded size (None
    when only the presence counts). `praefix` puts the rel under the
    source's root for the findings list. A missing file the user noted as
    lost (`vermerkt`) is counted as such, not as missing."""
    for rel, soll in bekannt.items():
        ist = _groesse(wurzel, rel)
        pfad = zeile or _ordner(rel)
        if ist is None:
            z.zaehle(pfad, "vermerkt" if rel in vermerkt else "fehlt",
                     None if rel in vermerkt else praefix + rel)
        elif gemessen and soll is not None and ist != soll:
            z.zaehle(pfad, "unvollstaendig", praefix + rel)
        else:
            z.zaehle(pfad, "stimmig")


def _fremde(z, dateien, bekannt, weg=(), zeile=None, praefix=""):
    """Files nobody knows – a tombstone counts as knowing: the file it
    names was kept on purpose."""
    for rel in dateien:
        if rel not in bekannt and rel not in weg:
            z.zaehle(zeile or _ordner(rel), "fremd", praefix + rel)


def _verlorene(z, wurzel, db, zeile=None, praefix=""):
    """Tombstones whose file is gone – unless the user already noted the
    loss (state.db table verloren)."""
    vermerkt = db.verloren_lesen()
    for rel in db.verschwunden_lesen():
        if rel not in vermerkt and _groesse(wurzel, rel) is None:
            z.zaehle(zeile or _ordner(rel), "verloren", praefix + rel)


def _permanent(z, wurzel, db, zeile=None, praefix="", zeile_fuer=None):
    """The items no run asks for again (export_util.permanent_mark): one
    Microsoft refuses, one that was gone before a copy came – each its
    own number, outside the four, because nothing here can do anything
    about it. A mark whose copy lies here (an older version, fetched
    before the verdict) counts nothing: the copy is what the record
    knows, and the record is judged with the others."""
    zeile_fuer = zeile_fuer or _ordner
    for mark in db.permanent_lesen().values():
        rel = mark.get("rel")
        if mark.get("quiet") or (rel and _groesse(wurzel, rel) is not None):
            continue
        art = "verweigert" if mark.get("kind") == export_util.REFUSED else "weg"
        einheit = str(mark.get("unit") or "")
        name = rel or (f"{einheit}: {mark.get('name') or '?'}" if einheit
                       else str(mark.get("name") or "?"))
        z.zaehle(zeile or zeile_fuer(rel or einheit), art, praefix + name)


# ---------------------------------------------------------------------------
# One check per source
# ---------------------------------------------------------------------------
def pruefe_outlook(out):
    """The resume log holds mails, events and contacts alike – one row per
    folder they lie in."""
    out = Path(out)
    urteil = _db_ok(out)
    if urteil != "ok":
        return _beschaedigt("outlook", urteil)
    db = state_db.StateDb(out)
    z = _Zaehler()
    bekannt = {rel: None for rel in db.done_lesen().values()}
    weg = db.verschwunden_lesen()
    _bestand_pruefen(z, out, bekannt, vermerkt=db.verloren_lesen())
    _fremde(z, _dateien(out, endungen={".eml", ".ics", ".vcf"}), bekannt, weg)
    _verlorene(z, out, db)
    _permanent(z, out, db)
    return z.zustand("outlook", beiseite=_beiseite(out))


def pruefe_spiegel(wurzel, z=None, zeile=None, praefix=""):
    """A drive mirror: the inventory against the files below Dateien/,
    sizes included; tombstones must still have their file."""
    wurzel = Path(wurzel)
    urteil = _db_ok(wurzel)
    if urteil != "ok":
        return None, urteil
    db = state_db.StateDb(wurzel)
    z = z or _Zaehler()
    bekannt = {e["rel"]: int(e["size"]) for e in db.bestand_lesen().values()}
    weg = db.verschwunden_lesen()
    _bestand_pruefen(z, wurzel, bekannt, gemessen=True, zeile=zeile, praefix=praefix,
                     vermerkt=db.verloren_lesen())
    _fremde(z, _dateien(wurzel, unter=drive_mirror.DATEI_DIR), bekannt, weg,
            zeile=zeile, praefix=praefix)
    _verlorene(z, wurzel, db, zeile=zeile, praefix=praefix)
    _permanent(z, wurzel, db, zeile=zeile, praefix=praefix)
    return z, "ok"


def pruefe_onedrive(out):
    z, urteil = pruefe_spiegel(out)
    if z is None:
        return _beschaedigt("onedrive", urteil)
    return z.zustand("onedrive", beiseite=_beiseite(out))


def pruefe_sharepoint(out):
    """Every library below the root – one row per "site/library"."""
    out = Path(out)
    z = _Zaehler()
    fehler = []
    for wurzel in _spiegel_wurzeln(out):
        name = wurzel.relative_to(out).as_posix()
        _z, urteil = pruefe_spiegel(wurzel, z=z, zeile=name, praefix=name + "/")
        if _z is None:
            fehler.append(completeness.fehler(name, urteil))
    return z.zustand("sharepoint", fehler=fehler, beiseite=_beiseite(out))


def pruefe_seiten(out):
    out = Path(out)
    urteil = _db_ok(out)
    if urteil != "ok":
        return _beschaedigt("sharepoint_pages", urteil)
    db = state_db.StateDb(out)
    z = _Zaehler()
    bekannt = {e["rel"]: None for e in db.seiten_lesen().values()}
    weg = db.verschwunden_lesen()
    _bestand_pruefen(z, out, bekannt, vermerkt=db.verloren_lesen())
    _fremde(z, _dateien(out, endungen={".html"}), bekannt, weg)
    _verlorene(z, out, db)
    _permanent(z, out, db)
    return z.zustand("sharepoint_pages", beiseite=_beiseite(out))


def _teams_records(db):
    """The conversation records as the export reads them – one row each,
    or, for an archive the export has not touched since 9.0, the one kv
    blob "state". Read only: the export moves the blob, not the check."""
    records = {}
    for key, roh in db.saetze_lesen("conversations").items():
        rec = _json(roh)
        if rec:
            records[key] = rec
    if records:
        return records
    daten = _json(db.kv_lesen("state") or "")
    return daten.get("conversations") if isinstance(daten.get("conversations"), dict) else {}


def _onenote_seiten(db):
    """The page records as the export reads them – rows, else the kv blob
    "pages" of a notebook from before 9.0. Read only."""
    stand = {}
    for key, roh in db.saetze_lesen("pages").items():
        rec = _json(roh)
        if rec:
            stand[key] = rec
    return stand or _json(db.kv_lesen("pages") or "")


def pruefe_teams(out):
    """Conversation files per kind, the referenced files each conversation
    fetched, and every team's mirrored channel folders."""
    out = Path(out)
    urteil = _db_ok(out)
    if urteil != "ok":
        return _beschaedigt("teams", urteil)
    db = state_db.StateDb(out)
    z = _Zaehler()
    fehler = []
    spiegel = _spiegel_wurzeln(out / "channels")
    ohne = [w.relative_to(out).as_posix() for w in spiegel]

    def zeile_fuer(rel):
        teile = rel.split("/")
        return "/".join(teile[:2]) if teile[0] == "channels" else teile[0]

    vermerkt = db.verloren_lesen()
    bekannt = {}
    conversations = _teams_records(db)
    for rec in conversations.values():
        if rec.get("done") and rec.get("rel") and not rec.get("empty"):
            bekannt[rec["rel"]] = None
    for rel in bekannt:
        if _groesse(out, rel) is not None:
            z.zaehle(zeile_fuer(rel), "stimmig")
        elif rel in vermerkt:
            z.zaehle(zeile_fuer(rel), "vermerkt")
        else:
            z.zaehle(zeile_fuer(rel), "fehlt", rel)
    dateien = {}
    for art in TEAMS_ARTEN:
        dateien.update(_dateien(out, unter=art, endungen={".html"}, ohne=ohne))
    for rel in dateien:
        if rel not in bekannt and "/Anhaenge/" not in rel:
            z.zaehle(zeile_fuer(rel), "fremd", rel)
    # The files a conversation fetched: kv "files:<key>" -> url -> {rel, size}.
    con = db._verbinden(lesend=True)
    if con is not None:
        for _key, roh in con.execute("SELECT key, value FROM kv WHERE key LIKE 'files:%'"):
            for eintrag in _json(roh).values():
                rel = (eintrag or {}).get("rel")
                if not rel:
                    continue
                ist = _groesse(out, rel)
                pfad = zeile_fuer(rel)
                if ist is None:
                    z.zaehle(pfad, "vermerkt" if rel in vermerkt else "fehlt",
                             None if rel in vermerkt else rel)
                elif eintrag.get("size") and ist != int(eintrag["size"]):
                    z.zaehle(pfad, "unvollstaendig", rel)
                else:
                    z.zaehle(pfad, "stimmig")
    # A file Microsoft refuses or no longer has: recorded under its URL,
    # with the conversation as its unit – by name, since no path exists.
    _permanent(z, out, db, zeile_fuer=zeile_fuer)
    for wurzel in spiegel:
        name = wurzel.relative_to(out).as_posix()
        _z, urteil = pruefe_spiegel(wurzel, z=z, zeile=zeile_fuer(name), praefix=name + "/")
        if _z is None:
            fehler.append(completeness.fehler(name, urteil))
    return z.zustand("teams", fehler=fehler, beiseite=_beiseite(out))


def _einheiten(out):
    """The per-unit folders of Planner, To Do and OneNote: every subfolder
    with a state.db."""
    return _spiegel_wurzeln(out)


def pruefe_planner(out):
    """Per board: the board file while it has cards, and every referenced
    file the export fetched; files under Anhaenge/ nobody references are
    foreign."""
    out = Path(out)
    z = _Zaehler()
    fehler = []
    for ordner in _einheiten(out):
        name = ordner.name
        urteil = _db_ok(ordner)
        if urteil != "ok":
            fehler.append(completeness.fehler(name, urteil))
            continue
        db = state_db.StateDb(ordner)
        karten = _json(db.kv_lesen("tasks") or "")
        bekannt = {}
        if karten:
            bekannt["board.html"] = None
        for eintrag in _json(db.kv_lesen("anhaenge") or "").values():
            if (eintrag or {}).get("rel"):
                bekannt[eintrag["rel"]] = None
        _bestand_pruefen(z, ordner, bekannt, zeile=name, praefix=name + "/",
                         vermerkt=db.verloren_lesen())
        _fremde(z, _dateien(ordner, unter="Anhaenge"), bekannt, zeile=name, praefix=name + "/")
        _permanent(z, ordner, db, zeile=name, praefix=name + "/")
    return z.zustand("planner", fehler=fehler, beiseite=_beiseite(out))


def pruefe_todo(out):
    out = Path(out)
    z = _Zaehler()
    fehler = []
    for ordner in _einheiten(out):
        name = ordner.name
        urteil = _db_ok(ordner)
        if urteil != "ok":
            fehler.append(completeness.fehler(name, urteil))
            continue
        db = state_db.StateDb(ordner)
        aufgaben = _json(db.kv_lesen("tasks") or "")
        bekannt = {}
        if aufgaben:
            bekannt["list.html"] = None
        for eintrag in aufgaben.values():
            for a in (eintrag or {}).get("anhaenge") or []:
                if isinstance(a, dict) and a.get("rel"):
                    bekannt[a["rel"]] = None
        _bestand_pruefen(z, ordner, bekannt, zeile=name, praefix=name + "/",
                         vermerkt=db.verloren_lesen())
        _fremde(z, _dateien(ordner, unter="Anhaenge"), bekannt, zeile=name, praefix=name + "/")
        _permanent(z, ordner, db, zeile=name, praefix=name + "/")
    return z.zustand("todo", fehler=fehler, beiseite=_beiseite(out))


def pruefe_onenote(out):
    """Per notebook: every page record's file (a page that left its section
    keeps its file), every filed resource, and stray pages or files."""
    out = Path(out)
    z = _Zaehler()
    fehler = []
    for ordner in _einheiten(out):
        name = ordner.name
        urteil = _db_ok(ordner)
        if urteil != "ok":
            fehler.append(completeness.fehler(name, urteil))
            continue
        db = state_db.StateDb(ordner)
        bekannt = {}
        for rec in _onenote_seiten(db).values():
            if isinstance(rec, dict) and rec.get("rel"):
                bekannt[rec["rel"]] = None
        for roh in db.saetze_lesen("ressourcen").values():
            rec = _json(roh)
            if rec.get("rel") and not rec.get("inline"):
                bekannt[rec["rel"]] = None
        _bestand_pruefen(z, ordner, bekannt, zeile=name, praefix=name + "/",
                         vermerkt=db.verloren_lesen())
        # A page owns its .files folder: what lies there belongs to the
        # page whether or not a resource record names it – the records
        # came with 9.0, the folders are older.
        eigene = {rel[:-5] + ONENOTE_SUFFIX for rel in bekannt if rel.endswith(".html")}
        dateien = {rel: n for rel, n in _dateien(ordner).items()
                   if _ordner(rel) not in eigene and not any(
                       _ordner(rel).startswith(o + "/") for o in eigene)}
        _fremde(z, dateien, bekannt, zeile=name, praefix=name + "/")
        _permanent(z, ordner, db, zeile=name, praefix=name + "/")
    return z.zustand("onenote", fehler=fehler, beiseite=_beiseite(out))


PRUEFER = {"outlook": pruefe_outlook, "teams": pruefe_teams,
           "onedrive": pruefe_onedrive, "sharepoint": pruefe_sharepoint,
           "sharepoint_pages": pruefe_seiten, "planner": pruefe_planner,
           "todo": pruefe_todo, "onenote": pruefe_onenote}


# ---------------------------------------------------------------------------
# The index against the archive
# ---------------------------------------------------------------------------
def pruefe_index(store, ordner):
    """What the index read against what lies here now: changed, new and
    gone files since its last run – exactly what "Index only" catches up."""
    dbp = store_layout.db_path(Path(store))
    if not dbp.exists():
        return {"stand": completeness.NICHT, "grund": KEIN_INDEX,
                "geaendert": 0, "neu": 0, "weg": 0, "gebaut": None}
    try:
        con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        try:
            tabellen = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "dateien" not in tabellen:
                return {"stand": completeness.NICHT, "grund": ALTER_INDEX,
                        "geaendert": 0, "neu": 0, "weg": 0, "gebaut": None}
            alt = {(root, rel): (mtime, size) for root, rel, mtime, size in
                   con.execute("SELECT root, rel, mtime_ns, size FROM dateien")}
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"stand": completeness.NICHT, "grund": DB_BESCHAEDIGT,
                "geaendert": 0, "neu": 0, "weg": 0, "gebaut": None,
                "fehler": [completeness.fehler("index", f"{type(e).__name__}: {e}")]}
    geaendert = neu = 0
    gesehen = set()
    for art, wurzel in ordner.items():
        if not wurzel or not Path(wurzel).is_dir():
            continue
        for rel, signatur in corpus.manifest(art, wurzel).items():
            gesehen.add((art, rel))
            vorher = alt.get((art, rel))
            if vorher is None:
                neu += 1
            elif tuple(vorher) != tuple(signatur):
                geaendert += 1
    weg = sum(1 for k in alt if k not in gesehen)
    gebaut = datetime.fromtimestamp(dbp.stat().st_mtime, UTC).isoformat(timespec="seconds")
    return {"stand": completeness.GANZ, "grund": None, "geaendert": geaendert,
            "neu": neu, "weg": weg, "gebaut": gebaut}


# ---------------------------------------------------------------------------
# The whole archive, and one source afresh
# ---------------------------------------------------------------------------
def _jetzt():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _datieren(z, alt, nachgeholt=None):
    """`fehlt_seit`: the first report that found the row's missing files –
    kept from the previous report while something stays missing, fresh
    when the finding is new, gone when nothing is missing. `nachgeholt`:
    the source's last completed resync, as the app read it from the run
    history (None keeps what the previous report knew)."""
    alt = alt or {}
    if z["fehlt"] > 0:
        z["fehlt_seit"] = (alt.get("fehlt_seit") if alt.get("fehlt") else None) or _jetzt()
    else:
        z["fehlt_seit"] = None
    z["nachgeholt"] = nachgeholt if nachgeholt is not None else alt.get("nachgeholt")
    return z


def bericht_lesen(bericht_pfad):
    """The stored report, or an empty one."""
    try:
        return json.loads(Path(bericht_pfad).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"geprueft": None, "quellen": [],
                "index": {"stand": completeness.NICHT, "grund": KEIN_INDEX,
                          "geaendert": 0, "neu": 0, "weg": 0, "gebaut": None}}


def _zeile_von(bericht, quelle):
    return next((q for q in bericht.get("quellen") or [] if q.get("quelle") == quelle), None)


def pruefe_alles(pfade, store, vorher=None, nachgeholt=None):
    """Every source that has a folder, in the order of the overview, plus
    the index. `pfade` maps outlook, teams, onedrive, sharepoint,
    sharepoint_pages, planner, todo, onenote -> folder; `vorher` is the
    stored report (for the dates), `nachgeholt` maps source -> when it was
    last resynced."""
    quellen = []
    for key, pruefen in PRUEFER.items():
        ordner = pfade.get(key)
        if not ordner or not Path(ordner).is_dir():
            continue
        z = _datieren(pruefen(ordner), _zeile_von(vorher or {}, key),
                      (nachgeholt or {}).get(key))
        n = z["stimmig"] + z["fehlt"] + z["fremd"] + z["unvollstaendig"] + z["verloren"]
        progress.event("run.archiv.quelle", name=progress.atom(TITEL[key]), n=n)
        quellen.append(z)
    index = pruefe_index(store, {
        "teams": pfade.get("teams"), "teams_files": pfade.get("teams"),
        "outlook": pfade.get("outlook"), "onedrive": pfade.get("onedrive"),
        "sharepoint": pfade.get("sharepoint"), "pages": pfade.get("sharepoint_pages"),
        "onenote": pfade.get("onenote")})
    return {"geprueft": datetime.now(UTC).isoformat(timespec="seconds"),
            "quellen": quellen, "index": index}


def bericht_aktualisieren(bericht_pfad, quelle, ordner, zustand=None, nachgeholt=None):
    """After an action: judge the one source afresh and put its entry into
    the stored report – the rest of the report stands. `zustand` replaces
    the fresh check when the caller knows better (a rebuild under way);
    `nachgeholt` dates the row as fetched (the end of a "Fetch again")."""
    bericht = bericht_lesen(bericht_pfad)
    neu = _datieren(zustand or PRUEFER[quelle](ordner), _zeile_von(bericht, quelle),
                    nachgeholt)
    quellen = [q for q in bericht.get("quellen") or [] if q.get("quelle") != quelle]
    reihe = list(PRUEFER)
    quellen.append(neu)
    quellen.sort(key=lambda q: reihe.index(q["quelle"]) if q["quelle"] in reihe else 99)
    bericht["quellen"] = quellen
    export_util.schreibe_atomar(Path(bericht_pfad), json.dumps(bericht, ensure_ascii=False))
    return neu


# ---------------------------------------------------------------------------
# The actions – each explicit, each reversible or additive, none deleting
# ---------------------------------------------------------------------------
def _stempel():
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _db_fuer(ordner, rel):
    """The state.db that owns a finding: the deepest mirror or unit folder
    on its path, else the source's root. Returns (db, rel within it)."""
    ordner = Path(ordner)
    beste, rest, tiefe = ordner, rel, -1
    for wurzel in _spiegel_wurzeln(ordner):
        praefix = wurzel.relative_to(ordner).as_posix() + "/"
        if rel.startswith(praefix) and len(praefix) > tiefe:
            beste, rest, tiefe = wurzel, rel[len(praefix):], len(praefix)
    return state_db.StateDb(beste), rest


ARTEN_VERMERKBAR = ("verloren", "fehlt")


def vermerken(ordner, quelle, arten=("verloren",)):
    """Note the source's lost tombstones – and, when asked (`arten`
    includes "fehlt"), the files a resync did not bring back – as lost:
    judged afresh, not from a stored report; the records stay. Returns
    the count."""
    z = PRUEFER[quelle](ordner)
    jetzt = _jetzt()
    je_db, n = {}, 0
    for art in arten:
        if art not in ARTEN_VERMERKBAR:
            continue
        for rel in z["befunde"][art]:
            db, innen = _db_fuer(ordner, rel)
            je_db.setdefault(db.pfad, (db, []))[1].append(innen)
            n += 1
    for db, rels in je_db.values():
        db.verloren_vermerken(rels, jetzt)
    return n


def beiseitelegen(ordner, quelle):
    """Move every foreign file of the source into _fremd/<stamp>/, path
    kept, and write the list of what moved next to them. A target that
    exists is skipped, never overwritten. Returns (moved, skipped, batch)."""
    ordner = Path(ordner)
    z = PRUEFER[quelle](ordner)
    fremd = z["befunde"]["fremd"]
    if not fremd:
        return 0, 0, None
    stapel = ordner / FREMD_DIR / _stempel()
    bewegt, uebersprungen = [], 0
    for rel in fremd:
        quelle_pfad, ziel = ordner / rel, stapel / rel
        if not quelle_pfad.is_file():
            continue
        if ziel.exists():
            uebersprungen += 1
            continue
        ziel.parent.mkdir(parents=True, exist_ok=True)
        try:
            quelle_pfad.replace(ziel)
        except OSError:
            uebersprungen += 1
            continue
        bewegt.append(rel)
    if bewegt:
        export_util.schreibe_atomar(stapel / LISTE, json.dumps(
            {"quelle": quelle, "wann": datetime.now(UTC).isoformat(timespec="seconds"),
             "dateien": bewegt}, ensure_ascii=False))
    elif stapel.is_dir():
        shutil.rmtree(stapel, ignore_errors=True)
    return len(bewegt), uebersprungen, stapel.name if bewegt else None


def zurueckholen(ordner, stapel=None):
    """Move a set-aside batch back – the newest when none is named. A file
    whose place is taken meanwhile stays in the batch. Returns (moved,
    skipped). Emptied batch folders go, they held nothing but the move."""
    ordner = Path(ordner)
    basis = ordner / FREMD_DIR
    if not basis.is_dir():
        return 0, 0
    stapel_ordner = basis / stapel if stapel else max(
        (p for p in basis.iterdir() if p.is_dir()), default=None, key=lambda p: p.name)
    if stapel_ordner is None or not stapel_ordner.is_dir():
        return 0, 0
    try:
        liste = json.loads((stapel_ordner / LISTE).read_text(encoding="utf-8"))
        dateien = list(liste.get("dateien") or [])
    except (OSError, ValueError):
        dateien = [p.relative_to(stapel_ordner).as_posix()
                   for p in stapel_ordner.rglob("*") if p.is_file() and p.name != LISTE]
    bewegt = uebersprungen = 0
    for rel in dateien:
        von, nach = stapel_ordner / rel, ordner / rel
        if not von.is_file():
            continue
        if nach.exists():
            uebersprungen += 1
            continue
        nach.parent.mkdir(parents=True, exist_ok=True)
        try:
            von.replace(nach)
            bewegt += 1
        except OSError:
            uebersprungen += 1
    # Only what is empty goes: the list, then folders without a file.
    rest = [p for p in stapel_ordner.rglob("*") if p.is_file() and p.name != LISTE]
    if not rest:
        shutil.rmtree(stapel_ordner, ignore_errors=True)
        if basis.is_dir() and not any(basis.iterdir()):
            basis.rmdir()
    return bewegt, uebersprungen


def _retten(alt, neu):
    """A fresh state.db from what the damaged one still yields: the whole
    file when it dumps through, else the tombstones alone, else nothing.
    Returns "ganz", "grabsteine" or "nichts"."""
    try:
        quelle = sqlite3.connect(f"file:{alt}?mode=ro", uri=True)
        try:
            ziel = sqlite3.connect(neu)
            try:
                ziel.executescript("\n".join(quelle.iterdump()))
                ziel.commit()
            finally:
                ziel.close()
        finally:
            quelle.close()
        if state_db.StateDb(neu.parent, neu.name).integritaet() == "ok":
            return "ganz"
    except sqlite3.Error:
        pass
    try:
        neu.unlink()
    except OSError:
        pass
    try:
        quelle = sqlite3.connect(f"file:{alt}?mode=ro", uri=True)
        try:
            weg = dict(quelle.execute("SELECT rel, seit FROM verschwunden"))
        finally:
            quelle.close()
    except sqlite3.Error:
        return "nichts"
    frisch = state_db.StateDb(neu.parent, neu.name)
    for rel, seit in weg.items():
        frisch.verschwunden_ergaenzen([rel], seit)
    frisch.close()
    return "grabsteine"


def beschaedigte(ordner):
    """The source's folders whose state.db cannot be trusted – what a
    rebuild would set aside."""
    ordner = Path(ordner)
    return [w for w in (ordner, *_spiegel_wurzeln(ordner))
            if (w / state_db.DB_NAME).exists() and _db_ok(w) != "ok"]


def neu_aufbauen(ordner, quelle):
    """Set every damaged state.db of the source aside as
    state.db.beschaedigt-<stamp> (its -wal and -shm alongside), salvage
    what it still yields into a fresh one, and leave the rest to the
    source's next run. Returns (units rebuilt, what was salvaged per
    unit)."""
    ordner = Path(ordner)
    einheiten, gerettet = 0, {}
    for wurzel in beschaedigte(ordner):
        pfad = wurzel / state_db.DB_NAME
        stempel = _stempel()
        neu = wurzel / f"{state_db.DB_NAME}.neu-{stempel}"
        ergebnis = _retten(pfad, neu)
        for anhang in ("", "-wal", "-shm"):
            p = wurzel / f"{state_db.DB_NAME}{anhang}"
            if p.exists():
                p.replace(wurzel / f"{state_db.DB_NAME}.beschaedigt-{stempel}{anhang}")
        if neu.exists():
            neu.replace(pfad)
        einheiten += 1
        name = wurzel.relative_to(ordner).as_posix()
        gerettet[quelle if name == "." else name] = ergebnis
    return einheiten, gerettet


def ordner_oeffnen(pfad):
    """Show the folder in the system's file manager. Returns False where
    there is no way to."""
    pfad = str(Path(pfad))
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", pfad])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", pfad])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", pfad])
        else:
            return False
        return True
    except OSError:
        return False


AKTIONEN = ("vermerken", "beiseitelegen", "zurueckholen", "neu-aufbauen")
SCHRITTE = AKTIONEN + ("pruefen",)   # what --aktion accepts


def zeile_lesen(bericht_pfad, quelle):
    """One source's row of the stored report, or None."""
    return _zeile_von(bericht_lesen(bericht_pfad), quelle)


def aktion(name, quelle, pfade, bericht_pfad, arten="verloren"):
    """One action as a step of a run: does it, says what it did in the log,
    judges the source afresh into the stored report, and reports a result.
    The rebuild leaves the row saying "rebuilding": the source's export
    step follows in the same run and fills the fresh bookkeeping.
    "pruefen" does nothing but the judging – the last step of a "Fetch
    again" run, dating the row as fetched so what is still missing reads
    as a card to note."""
    ordner = Path(pfade.get(quelle) or "")
    titel = progress.atom(TITEL.get(quelle, quelle))
    if quelle not in PRUEFER or not ordner.is_dir():
        progress.event("run.archiv.nothing", name=titel)
        progress.ergebnis(0)
        return
    zustand, extra = None, {}
    if name == "pruefen":
        neu = bericht_aktualisieren(bericht_pfad, quelle, ordner, nachgeholt=_jetzt())
        progress.event("run.archiv.quelle", name=titel,
                       n=neu["stimmig"] + neu["fehlt"] + neu["fremd"]
                       + neu["unvollstaendig"] + neu["verloren"])
        if neu["fehlt"]:
            progress.event("run.archiv.stubborn", "warn", name=titel, n=neu["fehlt"])
        progress.ergebnis(0, extra={"missing": neu["fehlt"], "partial": neu["unvollstaendig"]})
        return
    if name == "vermerken":
        gewollt = tuple(x for x in arten.split(",") if x in ARTEN_VERMERKBAR) or ("verloren",)
        n = vermerken(ordner, quelle, gewollt)
        progress.event("run.archiv.noted", name=titel, n=n)
        extra = {"noted": n}
    elif name == "beiseitelegen":
        n, uebersprungen, stapel = beiseitelegen(ordner, quelle)
        progress.event("run.archiv.aside", name=titel, n=n, skipped=uebersprungen,
                       folder=f"{FREMD_DIR}/{stapel}" if stapel else FREMD_DIR)
        extra = {"aside": n, "skipped": uebersprungen}
    elif name == "zurueckholen":
        n, uebersprungen = zurueckholen(ordner)
        progress.event("run.archiv.restored", name=titel, n=n, skipped=uebersprungen)
        extra = {"restored": n, "skipped": uebersprungen}
    else:
        n, gerettet = neu_aufbauen(ordner, quelle)
        if n:
            progress.event("run.archiv.rebuilt", name=titel, n=n,
                           saved=", ".join(f"{k}: {v}" for k, v in gerettet.items()))
            # Until the run has filled the fresh bookkeeping, every file
            # would read as foreign – the row says what is going on instead.
            zustand = completeness.zustand(quelle, stand=completeness.NICHT,
                                           grund="ana.archiv.reason.rebuilding")
        else:
            progress.event("run.archiv.nothing", name=titel)
        extra = {"rebuilt": n}
    bericht_aktualisieren(bericht_pfad, quelle, ordner, zustand=zustand)
    progress.ergebnis(0, extra=extra)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("teams")
    ap.add_argument("outlook")
    ap.add_argument("onedrive")
    ap.add_argument("--sharepoint", required=True)
    ap.add_argument("--pages", required=True)
    ap.add_argument("--planner", required=True)
    ap.add_argument("--todo", required=True)
    ap.add_argument("--onenote", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--nachgeholt", default="{}",
                    help="JSON: source -> when its last resync completed")
    ap.add_argument("--aktion", choices=SCHRITTE, default=None)
    ap.add_argument("--quelle", default="")
    ap.add_argument("--arten", default="verloren",
                    help="vermerken: the kinds to note, comma-separated")
    a = ap.parse_args()
    pfade = {"outlook": a.outlook, "teams": a.teams, "onedrive": a.onedrive,
             "sharepoint": a.sharepoint, "sharepoint_pages": a.pages,
             "planner": a.planner, "todo": a.todo, "onenote": a.onenote}
    if a.aktion:
        aktion(a.aktion, a.quelle, pfade, Path(a.report), a.arten)
        return
    bericht = pruefe_alles(pfade, a.store, vorher=bericht_lesen(a.report),
                           nachgeholt=_json(a.nachgeholt))
    export_util.schreibe_atomar(Path(a.report), json.dumps(bericht, ensure_ascii=False))
    summe = Counter()
    for q in bericht["quellen"]:
        for k in ("fehlt", "fremd", "unvollstaendig", "verloren"):
            summe[k] += q[k]
    progress.ergebnis(0, extra={"missing": summe["fehlt"], "foreign": summe["fremd"],
                                "partial": summe["unvollstaendig"], "lost": summe["verloren"],
                                "index_changed": bericht["index"]["geaendert"] +
                                bericht["index"]["neu"] + bericht["index"]["weg"]})


if __name__ == "__main__":
    main()
