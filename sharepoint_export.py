#!/usr/bin/env python3
"""
SharePoint mirror: configured sites and document libraries as local copies.

To Graph a document library is a drive, so the machinery is drive_mirror.py –
the same promises as the OneDrive mirror: the CURRENT version of every file
is kept, deletions leave a tombstone in the state.db. What is SharePoint
here is only the addressing: URLs are resolved to sites, sites list their
libraries, and every library is mirrored into its own folder
(``<site>/<library>/Dateien/…``) with its own delta pointer and inventory.

Runs as a subprogram of app.py: output folder as the only argument, settings
as environment variables (SHAREPOINT_URLS – one site or library URL per
line; SHAREPOINT_RULES – ordered include/exclude rules on paths of the form
"<site>/<library>/Dateien/…", exactly as the export list shows them;
SHAREPOINT_TYPES_INCLUDE / SHAREPOINT_TYPES_EXCLUDE – comma-separated
file extensions, include empty = every type, exclude wins;
SHAREPOINT_MAX_MB – skip larger files, 0 = no limit; MIRROR_WORKERS –
parallel requests). Special runs: --folders syncs the folder trees,
--check enumerates without downloading and reports per library what a
mirror run would fetch – count, size, and what the filters leave out
(the size preview) – plus what is missing locally.

--pages is a separate export with its own configuration
(SHAREPOINT_PAGES_URLS): the modern site pages of the listed sites and all
their subsites, rendered to standalone HTML from the Graph Pages API
(canvasLayout). Text web parts keep their content, images are embedded as
data URIs (fetched via the Graph shares endpoint, which resolves any asset
URL the user can read), everything else becomes a named placeholder; classic
wiki pages are not part of that API. Incremental via eTag, deleted pages get
the usual tombstone note.

Access needs Sites.Read.All. A pasted key without that scope does not kill
the run: the affected site is reported and skipped.

Folder cadences: SYNC_CADENCE may carry keys "sharepoint:<site>/<library>/
<folder path>" – the very paths the export list and SHAREPOINT_RULES use
("Nordwind/Dokumente/Dateien/Archiv"). The library's own cadence stays the
merged per-URL one. A library has one delta stream, so a folder cadence
gates downloads, not the listing: the library is listed whenever any of
its units is due, files of units not due wait in the library's state.db
(records "wartend") and come when their cadence is round; every unit has
its own stamp there ("last_sync:sharepoint:<folder path>", the library's
"last_sync"). Every library's state.db also holds kv "urls", the configured
URLs that resolved to it – the settings page maps a library to its rows
by it. FULL_SYNC (export_util.voll_neu) makes every library forget pointer,
walk and file versions and the pages export forget every page's eTag, so
both fetch everything again as on their first run.

A folder URL narrows a library to that subtree. Scoped mirrors use the
same drive delta as everything else – the first run enumerates the library
once, every later run costs one request when nothing changed, and deletions
come from the authoritative delta feed. Entries outside the scope are
ignored silently; narrowing a URL later just stops syncing what fell out,
those files keep their last mirrored state.
"""

import base64
import json
import os
import re
import sys
import threading
from concurrent.futures import (ThreadPoolExecutor,
                                as_completed)
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, unquote

import auth
import export_util
import completeness
import folders
import progress
import settings

try:
    import msal  # noqa: F401
    import requests
except ImportError:
    print("Fehlende Pakete. Bitte installieren:  pip install msal requests")
    raise SystemExit(1) from None

import drive_mirror
import graph_client
import state_db
from drive_mirror import Selection, safe

export_util.erzwinge_utf8()

GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Sites.Read.All", RES + "Files.Read.All", RES + "User.Read"]

KADENZ_PRAEFIX = "sharepoint"    # folder cadence keys: sharepoint:<site>/<library>/<folder>


workers = drive_mirror.workers


def _url_liste(env_name, key):
    roh = os.environ.get(env_name)
    if roh is None:
        roh = settings.value(key, "") or ""
    zeilen = [z.strip() for z in str(roh).splitlines()]
    return [z for z in zeilen if z and not z.startswith("#")]


def configured_urls():
    """One URL per line, environment over file; blanks and comments drop out."""
    return _url_liste("SHAREPOINT_URLS", "sharepoint_urls")


def pages_urls():
    """The pages export has its own list – sites, not libraries."""
    return _url_liste("SHAREPOINT_PAGES_URLS", "sharepoint_pages_urls")


def _types(env_name, key):
    roh = os.environ.get(env_name)
    if roh is None:
        roh = settings.value(key, "") or ""
    return [t for t in (s.strip() for s in str(roh).split(",")) if t]


def max_bytes():
    return max(0, settings.number("SHAREPOINT_MAX_MB", "sharepoint_max_mb",
                                  low=0)) * 1024 * 1024


# The cadence machinery is shared by the URL-based exports (export_util).
kadenzen = export_util.kadenzen
sync_jetzt = export_util.sync_jetzt
_haeufigere = export_util.haeufigere
einheit_faellig = export_util.einheit_faellig


def aktuelle_regeln():
    """Include/exclude on paths – the same mechanics as for OneDrive, over
    "<site>/<library>/Dateien/…" the way the export list shows a path.

    Which libraries come along is the URL list's decision; the rules
    narrow within them. Without rules everything comes along.
    """
    roh = os.environ.get("SHAREPOINT_RULES")
    if roh is None:
        roh = settings.value("sharepoint_rules", None)
    return folders.lies_regeln(roh or "")


def auswahl():
    """The SharePoint selection: path rules, extension filters and a size
    cap – shared by every library; drive_auswahl adds the library's own
    path prefix and scope."""
    return Selection(rules=aktuelle_regeln(), max_bytes=max_bytes(),
                     include_ext=_types("SHAREPOINT_TYPES_INCLUDE",
                                        "sharepoint_types_include"),
                     exclude_ext=_types("SHAREPOINT_TYPES_EXCLUDE",
                                        "sharepoint_types_exclude"))


class Graph(drive_mirror.DriveOps, graph_client.Graph):
    def __init__(self, nur_still=False):
        super().__init__(SCOPES, nur_still=nur_still)


class TokenClient(drive_mirror.DriveOps, graph_client.TokenClient):
    pass


# ---------------------------------------------------------------------------
# Addressing: URL -> site -> document libraries
# ---------------------------------------------------------------------------
def url_teile(url):
    """(site address, path inside the site) from whatever the browser hands
    out.

    People paste every shape SharePoint produces: the plain site, a library
    view with /Forms/AllItems.aspx?id=…, a sharing link (/:f:/r/…), a folder
    deep inside. The site part is the host plus the first two path segments
    when they follow the sites/teams/personal convention, else the root
    site; whatever follows names the library and folder – the run then
    mirrors exactly that subtree instead of the whole site.
    """
    u = urlsplit(url if "://" in url else "https://" + url)
    host = u.netloc
    if not host:
        return None
    stuecke = [unquote(s) for s in u.path.split("/") if s]
    # Sharing links: /:f:/r/sites/… – the marker and its one-letter mode.
    if stuecke and re.fullmatch(r":[a-z]:", stuecke[0]):
        stuecke = stuecke[1:]
        if stuecke and len(stuecke[0]) == 1:
            stuecke = stuecke[1:]
    # Library views: everything from Forms/… is view chrome, and the id= (or
    # RootFolder=) parameter carries the real server-relative folder path.
    if "Forms" in stuecke:
        stuecke = stuecke[:stuecke.index("Forms")]
    ziel = parse_qs(u.query)
    kennung = (ziel.get("id") or ziel.get("RootFolder") or [None])[0]
    if kennung:
        stuecke = [s for s in unquote(kennung).split("/") if s]
    if stuecke and stuecke[0].lower() in ("sites", "teams", "personal") and len(stuecke) >= 2:
        return f"{host}:/{stuecke[0]}/{stuecke[1]}", stuecke[2:]
    return host, stuecke


def site_address(url):
    teile = url_teile(url)
    return teile[0] if teile else None


def _drive_pfad(drive, adresse):
    """The library's own path segments, taken from its webUrl.

    A library's URL segment and its display name differ ("Shared Documents"
    vs "Documents"), so matching a pasted path against names would miss –
    the webUrl carries the real segment.
    """
    wp = [unquote(s) for s in
          urlsplit(drive.get("webUrl") or "").path.split("/") if s]
    return wp[2:] if ":" in adresse else wp


def resolve_drives(graph, urls):
    """The document libraries behind the configured URLs, deduplicated.

    A URL that points into one library (or a folder inside it) scopes the
    mirror to exactly that subtree; a plain site URL brings every library.
    Broken URLs and denied sites are reported and skipped – one bad line
    must not cost the other mirrors. Returns (drives, failures) where each
    drive is {"id", "site", "name", "prefixes"} and prefixes is None for the
    whole library or a set of folder paths inside it.
    """
    gefunden, fehl = [], 0
    nach_id = {}
    seiten_namen = {}
    kadenz_map = kadenzen()
    for url in urls:
        kadenz = kadenz_map.get(f"sharepoint-url:{url}") or "always"
        teile = url_teile(url)
        if not teile:
            progress.event("run.sharepoint.site_failed", "err", url=url,
                           error="invalid URL")
            fehl += 1
            continue
        adresse, rest = teile
        try:
            site = graph.get(f"{GRAPH}/sites/{adresse}")
            drives = list(graph.paged(f"{GRAPH}/sites/{site['id']}/drives"))
        except auth.TokenExpired:
            raise
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 403:
                # Missing Sites.Read.All on a pasted key – say so clearly.
                progress.event("run.sharepoint.denied", "warn", url=url)
            else:
                progress.event("run.sharepoint.site_failed", "err", url=url,
                               error=f"HTTP {status}")
            fehl += 1
            continue
        except Exception as e:
            progress.event("run.sharepoint.site_failed", "err", url=url,
                           error=f"{type(e).__name__}: {e}")
            fehl += 1
            continue
        sname = site.get("displayName") or site.get("name") or adresse
        # Two different sites can share a display name; their mirrors must
        # not share a folder – the second one gets a suffix from its id.
        kennung = site.get("id") or adresse
        bekannt = seiten_namen.setdefault(sname, kennung)
        if bekannt != kennung:
            sname = f"{sname}__{export_util.kuerzel(kennung)}"
        bibliotheken = [d for d in drives
                        if (d.get("driveType") or "") == "documentLibrary"]
        kandidaten, unterpfad = bibliotheken, None
        if rest:
            for d in bibliotheken:
                libsegs = _drive_pfad(d, adresse)
                if libsegs and rest[:len(libsegs)] == libsegs:
                    kandidaten = [d]
                    unterpfad = "/".join(rest[len(libsegs):]) or None
                    break
            else:
                # A path we cannot place: mirror the whole site rather than
                # silently nothing, and say why.
                progress.event("run.sharepoint.path_unmatched", "warn",
                               url=url, path="/".join(rest))
        progress.event("run.sharepoint.libraries", site=sname,
                       n=len(kandidaten))
        for d in kandidaten:
            if not d.get("id"):
                continue
            eintrag = nach_id.get(d["id"])
            if eintrag is None:
                eintrag = {"id": d["id"], "site": sname,
                           "name": d.get("name") or "Bibliothek",
                           "kadenz": kadenz,
                           "prefixes": None if unterpfad is None
                           else {unterpfad},
                           "urls": []}
                nach_id[d["id"]] = eintrag
                gefunden.append(eintrag)
            elif unterpfad is None:
                eintrag["kadenz"] = _haeufigere(eintrag.get("kadenz"), kadenz)
                eintrag["prefixes"] = None            # full scope wins
            elif eintrag["prefixes"] is not None:
                eintrag["kadenz"] = _haeufigere(eintrag.get("kadenz"), kadenz)
                _praefix_aufnehmen(eintrag["prefixes"], unterpfad)
            # Which configured lines led here – the library's state.db
            # remembers them for the settings page.
            if url not in eintrag["urls"]:
                eintrag["urls"].append(url)
    return gefunden, fehl


def _praefix_aufnehmen(vorhanden, neu):
    """Merge a scope prefix without nesting: an ancestor covers its
    descendants, and covered entries would make the walk visit (and
    download) the same files twice."""
    for p in vorhanden:
        if neu == p or neu.startswith(p + "/"):
            return
    for p in [p for p in vorhanden if p.startswith(neu + "/")]:
        vorhanden.discard(p)
    vorhanden.add(neu)


def _scope_regeln(prefixes):
    regeln = [(False, "**")]
    for pf in sorted(prefixes):
        muster = "/".join(safe(s) for s in pf.split("/"))
        regeln.append((True, f"{drive_mirror.DATEI_DIR}/{muster}/**"))
    return regeln


def drive_auswahl(basis, drive):
    """The per-library Selection: the shared filters and rules, the
    library's "<site>/<library>" in front of the rule paths, plus the
    subtree scope when the URL pointed below the library root."""
    return Selection(rules=basis.rules, prefix=drive_praefix(drive),
                     scope=(_scope_regeln(drive["prefixes"])
                            if drive.get("prefixes") else None),
                     max_bytes=basis.max_bytes,
                     include_ext=basis.include_ext,
                     exclude_ext=basis.exclude_ext)


def _library_event(drive):
    if drive.get("prefixes"):
        progress.event("run.sharepoint.library_scoped", site=drive["site"],
                       name=drive["name"],
                       scope=", ".join(sorted(drive["prefixes"])))
    else:
        progress.event("run.sharepoint.library", site=drive["site"],
                       name=drive["name"])


def _drive_segmente(drive):
    return (safe(drive.get("site") or "Site", 80),
            safe(drive.get("name") or "Bibliothek", 80))


def drive_praefix(drive):
    """"<site>/<library>" as the folders lie on disk – the path the rules
    and the export list speak of, in front of the mirror's "Dateien/…"."""
    return "/".join(_drive_segmente(drive))


def drive_ziel(out, drive):
    return Path(out).joinpath(*_drive_segmente(drive))


def drive_einheiten(kadenz_map, drive, db):
    """The library and its folder cadences as units over its state.db: the
    drive unit runs on the library's merged URL cadence, folder units come
    from the "sharepoint:<site>/<library>/<folder path>" keys, stamped as
    "last_sync:sharepoint:<folder path>" in the library's state.db."""
    return drive_mirror.Einheiten(kadenz_map, KADENZ_PRAEFIX, db,
                                  laufwerk=drive.get("kadenz") or "always",
                                  unter=drive_praefix(drive))


def _urls_merken(db, drive):
    """kv "urls": the configured URLs that resolved to this library."""
    db.kv_schreiben("urls", json.dumps(list(drive.get("urls") or []),
                                       ensure_ascii=False))


def _anzeigename(drive):
    return f'{drive["site"]} / {drive["name"]}'


def je_drive(graph, drives):
    """Yield (drive, client-ready) – the client is one, the base URL moves."""
    for d in drives:
        graph.drive_base = f"{GRAPH}/drives/{d['id']}"
        yield d


# ---------------------------------------------------------------------------
# The three runs: mirror, folder sync, check/preview
# ---------------------------------------------------------------------------
def lauf(graph, out, drives, fehl=0):
    """The mirror runs, one library after the other, behind each library's
    cadence gate. A library is skipped before listing only when none of
    its units is due; with folder cadences below it the listing runs and
    the units decide per file (drive_mirror.Einheiten stamps every due unit
    after its downloads succeeded – the library's "last_sync" included)."""
    wahl = auswahl()
    kadenz_map = kadenzen()
    summe = {"new": 0, "excluded": 0, "errors": 0, "moved": 0, "gone": 0}
    uebersprungen = wartend = 0
    getaktet = False
    if export_util.voll_neu():
        progress.event("run.full_sync")
    elif export_util.abgleich():
        progress.event("run.resync")
    for d in je_drive(graph, drives):
        ziel = drive_ziel(out, d)
        db = state_db.StateDb(ziel)
        _urls_merken(db, d)
        takt = drive_einheiten(kadenz_map, d, db)
        if not takt.irgendeine_faellig():
            uebersprungen += 1
            progress.event("run.cadence.skip", name=_anzeigename(d),
                           cadence=progress.atom(f"cadence.{takt.kadenz('')}"))
            continue
        _library_event(d)
        zahlen = drive_mirror.lauf(graph, ziel, drive_auswahl(wahl, d),
                                   workers(), still=True,
                                   zustand=state_db.DbZustand(ziel),
                                   name=_anzeigename(d), einheiten=takt)
        if takt.ordner:
            # One line per library for the folders held back, never one
            # per file; what waits for them adds up in the result.
            getaktet = True
            wartend += zahlen.get("waiting", 0)
            n = len(takt.zurueckgehalten())
            if n:
                progress.event("run.sharepoint.folders_paced",
                               name=_anzeigename(d), n=n)
        for k in summe:
            summe[k] += zahlen[k]
    extras = {"moved": summe["moved"], "gone": summe["gone"]}
    if uebersprungen:
        extras["skipped"] = uebersprungen
    if getaktet:
        extras["waiting"] = wartend
    progress.ergebnis(summe["new"], excluded=summe["excluded"],
                      errors=summe["errors"] + fehl, extra=extras)
    return summe


def _bibliotheken(out):
    """Every library folder below the root: two levels down, with a
    state.db of its own – as the mirrors wrote them."""
    out = Path(out)
    return sorted(p.parent for p in out.glob(f"*/*/{state_db.DB_NAME}"))


def nachholen(graph, out, rels):
    """A targeted fetch across the libraries: each file by its drive item
    – the id its library's inventory keeps, or the id the balance names.
    The library is found again through the URLs stored with it (kv
    "urls"), so no configured list is needed – a library whose URLs are
    gone counts its files as unknown. The stored balance follows what
    came."""
    out = Path(out)
    progress.event("run.nachholen.start", n=len(rels))
    je_bibliothek = {}
    unbekannt = 0
    for eintrag in rels:
        rel = eintrag["rel"] if isinstance(eintrag, dict) else eintrag
        for wurzel in _bibliotheken(out):
            praefix = wurzel.relative_to(out).as_posix() + "/"
            if rel.startswith(praefix):
                innen = rel[len(praefix):]
                je_bibliothek.setdefault(wurzel, []).append(
                    {"id": eintrag["id"], "rel": innen} if isinstance(eintrag, dict) else innen)
                break
        else:
            unbekannt += 1
    summe = {"new": 0, "errors": 0, "gone": 0}
    geholt = []
    for wurzel, unter in je_bibliothek.items():
        db = state_db.StateDb(wurzel)
        try:
            urls = json.loads(db.kv_lesen("urls") or "[]")
        except ValueError:
            urls = []
        drives, _fehl = resolve_drives(graph, urls) if urls else ([], 0)
        drive = next((d for d in drives if drive_ziel(out, d) == wurzel), None)
        if drive is None:
            unbekannt += len(unter)
            progress.event("run.nachholen.nolibrary", "warn",
                           name=wurzel.relative_to(out).as_posix(), n=len(unter))
            continue
        graph.drive_base = f"{GRAPH}/drives/{drive['id']}"
        zahlen = drive_mirror.nachholen(graph, wurzel, unter, workers(), still=True)
        for k in summe:
            summe[k] += zahlen[k]
        unbekannt += zahlen["unknown"]
        praefix = wurzel.relative_to(out).as_posix() + "/"
        geholt += [praefix + r for r in zahlen.get("geholt") or ()]
    completeness.abgeholt(state_db.StateDb(out), "sharepoint", geholt)
    export_util.nachholen_melden(summe["new"], summe["gone"], summe["errors"], unbekannt)


def nur_ordner(graph, out, drives, fehl=0):
    wahl = auswahl()
    neu = gesamt = 0
    for d in je_drive(graph, drives):
        _library_event(d)
        ziel = drive_ziel(out, d)
        _urls_merken(state_db.StateDb(ziel), d)
        daten = drive_mirror.nur_ordner(graph, ziel, drive_auswahl(wahl, d),
                                        still=True,
                                        zustand=state_db.DbZustand(ziel))
        neu += len(daten["neu"])
        gesamt += len(daten.get("ordner") or ())
    progress.ergebnis(neu, errors=fehl, extra={"total": gesamt})


def nur_pruefen(graph, out, drives, fehl=0):
    """--check: the balance of every listed library, merged into one report
    at the root – one row per "site/library", as the folders lie on disk.
    Doubles as the size preview: the events alongside say what a run would
    fetch and how big it is."""
    wahl = auswahl()
    berichte, fehler = [], []
    typen = {}
    for d in je_drive(graph, drives):
        _library_event(d)
        ziel = drive_ziel(out, d)
        _urls_merken(state_db.StateDb(ziel), d)
        try:
            b = drive_mirror.nur_pruefen(graph, ziel, drive_auswahl(wahl, d),
                                         still=True,
                                         zustand=state_db.DbZustand(ziel))
        except auth.TokenExpired:
            raise
        except Exception as e:
            progress.event("run.sharepoint.library_failed", "err",
                           name=_anzeigename(d), error=f"{type(e).__name__}: {e}")
            fehler.append(completeness.fehler(drive_praefix(d),
                                              "run.sharepoint.library_failed"))
            continue
        berichte.append((drive_praefix(d), b))
        for z in b.get("typen") or ():
            ganz = typen.setdefault(z["ext"], {"ext": z["ext"], "n": 0, "bytes": 0})
            ganz["n"] += z["n"]
            ganz["bytes"] += z["bytes"]
        progress.event("run.sharepoint.preview", site=d["site"], name=d["name"],
                       n=b["da"] + b["offen"] + b["wartend"],
                       mb=round(b["bytes"] / 1048576), skipped=b["ausgeschlossen"])
    bericht = completeness.bilanz(
        "sharepoint", "files",
        da=sum(b["da"] for _p, b in berichte),
        offen=sum(b["offen"] for _p, b in berichte),
        ausgeschlossen=sum(b["ausgeschlossen"] for _p, b in berichte),
        behalten=sum(b["behalten"] for _p, b in berichte),
        wartend=sum(b["wartend"] for _p, b in berichte),
        zeilen=[completeness.zeile(pfad, b["da"], b["offen"]) for pfad, b in berichte],
        fehler=fehler,
        extra={"bytes": sum(b["bytes"] for _p, b in berichte),
               "bytes_ausgeschlossen": sum(b["bytes_ausgeschlossen"] for _p, b in berichte),
               "typen": sorted(typen.values(), key=lambda z: -z["bytes"]),
               "kaputt": fehl,
               # The open files by id, under their library's path – what
               # "Fetch now" fetches without a second walk.
               "offene": [{"id": e["id"], "rel": f"{pfad}/{e['rel']}"}
                          for pfad, b in berichte for e in b.get("offene") or ()
                          ][:completeness.OFFENE_GRENZE],
               "offene_gekappt": any(b.get("offene_gekappt") for _p, b in berichte)
               or sum(len(b.get("offene") or ()) for _p, b in berichte)
               > completeness.OFFENE_GRENZE})
    completeness.schreiben(state_db.StateDb(out), bericht)
    completeness.melden(bericht)
    return bericht


# ---------------------------------------------------------------------------
# Site pages: rendered to HTML from the Graph Pages API
# ---------------------------------------------------------------------------
def resolve_page_sites(graph, urls):
    """The sites behind the URLs plus all their subsites, depth first.

    Returns ({"id", "name", "pfad"}…, failures); pfad is the folder chain the
    rendered pages land in.
    """
    gefunden, fehl, gesehen = [], 0, set()

    kadenz_map = kadenzen()
    belegt = {}       # tuple(pfad) -> site id, guards name collisions

    def eindeutig(pfad, sid):
        halter = belegt.setdefault(tuple(pfad), sid)
        if halter == sid:
            return pfad
        # Same display name, different site: the second one gets a suffix
        # from its id – two sites must never share one output folder.
        pfad = pfad[:-1] + [f"{pfad[-1]}__{export_util.kuerzel(sid)}"]
        belegt[tuple(pfad)] = sid
        return pfad

    def absteigen(sid, pfad, host, kadenz):
        if sid in gesehen:
            return
        gesehen.add(sid)
        pfad = eindeutig(pfad, sid)
        gefunden.append({"id": sid, "pfad": pfad, "host": host,
                         "kadenz": kadenz})
        try:
            unter = list(graph.paged(f"{GRAPH}/sites/{sid}/sites"))
        except auth.TokenExpired:
            raise
        except Exception:
            unter = []          # no subsites, or not listable – fine
        for u in unter:
            if u.get("id"):
                absteigen(u["id"], pfad + [safe(u.get("displayName")
                                                or u.get("name") or "Site", 80)],
                          host, kadenz)

    for url in urls:
        teile = url_teile(url)
        if not teile:
            progress.event("run.pages.site_failed", "err", url=url,
                           error="invalid URL")
            fehl += 1
            continue
        adresse, _ = teile
        try:
            site = graph.get(f"{GRAPH}/sites/{adresse}")
        except auth.TokenExpired:
            raise
        except Exception as e:
            progress.event("run.pages.site_failed", "err", url=url,
                           error=f"{type(e).__name__}: {e}")
            fehl += 1
            continue
        name = safe(site.get("displayName") or site.get("name") or adresse, 80)
        host = urlsplit(site.get("webUrl") or "").netloc or adresse.split(":")[0]
        absteigen(site["id"], [name], host,
                  kadenz_map.get(f"pages-url:{url}") or "always")
    return gefunden, fehl


def _webpart_html(wp):
    if str(wp.get("@odata.type", "")).endswith("textWebPart"):
        return wp.get("innerHtml") or ""
    daten = wp.get("data") or {}
    quellen = [q.get("value") for q in
               ((daten.get("serverProcessedContent") or {})
                .get("imageSources") or ())
               if q.get("value")]
    if quellen:
        return "".join('<img src="' + u.replace('"', "&quot;") + '" alt="">'
                       for u in quellen)
    titel = (daten.get("title")
             or wp.get("webPartType") or "web part")
    return ('<p class="webpart">[' +
            str(titel).replace("<", "&lt;") + "]</p>")


def render_page(seite, layout):
    """One standalone HTML file per page – content over fidelity.

    Text web parts keep their HTML; everything else (lists, embeds, quick
    links) becomes a named placeholder, the same stance the Teams export
    takes with unloadable images. The file opens offline and indexes well."""
    teile = []
    for sec in (layout or {}).get("horizontalSections") or ():
        for col in sec.get("columns") or ():
            teile += [_webpart_html(wp) for wp in col.get("webparts") or ()]
    vert = (layout or {}).get("verticalSection") or {}
    teile += [_webpart_html(wp) for wp in vert.get("webparts") or ()]
    titel = str(seite.get("title") or seite.get("name") or "Seite")
    kopf = titel.replace("<", "&lt;")
    stand = seite.get("lastModifiedDateTime") or ""
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>{kopf}</title></head><body>"
            f"<h1>{kopf}</h1>"
            f'<p class="meta">{stand}</p>'
            + "\n".join(teile) + "</body></html>")


_IMG_SRC = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.I)


def _lade_bild(graph, url, grenze=0):
    """Any asset URL the user can read, fetched via the shares endpoint –
    no need to work out which drive the image lives in.

    With a size cap, a tiny metadata probe runs first: downloading a 50 MB
    photo just to discard it against a 4 MB cap wastes the whole transfer.
    Returns (None, None) for images the cap excludes."""
    token = base64.urlsafe_b64encode(url.encode("utf-8")).decode().rstrip("=")
    if grenze:
        meta = graph.get(f"{GRAPH}/shares/u!{token}/driveItem?$select=size")
        if int(meta.get("size") or 0) > grenze:
            return None, None
    return graph.get_bytes(f"{GRAPH}/shares/u!{token}/driveItem/content",
                           label=" (Bild)")


def bild_max():
    """Embed images up to this size (sharepoint_pages_image_max_mb);
    0 means no limit."""
    return max(0, settings.number("SHAREPOINT_PAGES_IMAGE_MAX_MB",
                                  "sharepoint_pages_image_max_mb",
                                  low=0)) * 1024 * 1024


def bilder_einbetten(graph, html, host, zaehler, grenze=0, cache=None,
                     lock=None, marks=None, verdicts=None):
    """Embed the page's images as data URIs so the file stands alone.

    Failures keep the original URL: signed in, the browser may still show
    it – better than a hole. Images over `grenze` stay links on purpose.
    The run-scoped cache matters: a site logo appears on every page, and
    without it every page re-downloads the same bytes."""
    import html as html_lib
    cache = {} if cache is None else cache
    lock = lock or threading.Lock()
    marks = marks or {}
    verdicts = {} if verdicts is None else verdicts

    def ersetze(m):
        roh = html_lib.unescape(m.group(2))
        if roh.startswith("data:"):
            return m.group(0)
        voll = (roh if "://" in roh
                else f"https://{host}{roh}" if roh.startswith("/") else None)
        if not voll:
            return m.group(0)
        if f"image:{voll}" in marks:
            return m.group(0)       # refused or gone on an earlier run: stays a link
        with lock:
            if voll in cache:
                ersatz = cache[voll]
                if ersatz is not None:
                    zaehler["bilder"] += 1
                return m.group(1) + ersatz + m.group(3) if ersatz else m.group(0)
        try:
            inhalt, ctype = _lade_bild(graph, voll, grenze)
        except auth.TokenExpired:
            raise
        except Exception as e:
            # A verdict is recorded (quietly: an image is part of a page,
            # not an item of the archive); anything else counts as a
            # failure that leaves the page due for the next run.
            kind = export_util.verdict(e)
            with lock:
                cache[voll] = None
                if kind:
                    verdicts[f"image:{voll}"] = export_util.permanent_mark(
                        kind, f"{type(e).__name__}: {e}", name=voll.rsplit("/", 1)[-1][:80],
                        quiet=True)
                else:
                    zaehler["fehl"] += 1
            if kind:
                export_util.permanent_event(kind, voll.rsplit("/", 1)[-1], e)
            return m.group(0)
        if inhalt is None or (grenze and len(inhalt) > grenze):
            with lock:
                cache[voll] = None      # too big: stays a link, no error
            return m.group(0)
        b64 = base64.b64encode(inhalt).decode()
        daten = f"data:{(ctype or 'image/png').split(';')[0]};base64,{b64}"
        with lock:
            zaehler["bilder"] += 1
            cache[voll] = daten
        return m.group(1) + daten + m.group(3)

    return _IMG_SRC.sub(ersetze, html)


def _marked(marks, sid, etag):
    """Is this version of the page one no run asks for again?"""
    mark = (marks or {}).get(f"page:{sid}")
    return bool(mark) and mark.get("version") == (etag or "")


def seiten_lauf(graph, out, sites, fehl=0):
    out = Path(out)
    db = state_db.StateDb(out)
    marks = db.permanent_lesen()         # pages and images no run asks for again
    if export_util.voll_neu():
        # "Force full sync": every page's version and every mark is
        # forgotten first, so each one is fetched and rendered again – and
        # a run cut short leaves the rest due for the next one.
        progress.event("run.full_sync")
        db.seiten_versionen_loeschen()
        db.permanent_leeren()
        marks = {}
    elif export_util.abgleich():
        # A resync needs nothing more: every run lists all pages and
        # fetches a page whose file is gone.
        progress.event("run.resync")
    marks_before = dict(marks)
    verdicts = {}                         # the marks this run's images earned
    eintraege_bestand = db.seiten_lesen()
    neu = unveraendert = fehler = 0
    zaehler = {"bilder": 0, "fehl": 0}
    grenze = bild_max()
    cache = {}
    lock = threading.Lock()
    gesehen = set()
    sauber = []      # sites whose page listing succeeded this run

    def exportiere(site, seite, rel):
        # The listing carries no canvasLayout today, so the changed page is
        # fetched once more with it – only the changed one. Should the
        # listing ever bring the layout along, that second call goes.
        # Returns the page's own count of images that did not come (and
        # earned no verdict): such a page is written but not recorded, so
        # the next run renders it once more.
        voll = seite
        if not seite.get("canvasLayout"):
            voll = graph.get(f"{GRAPH}/sites/{site['id']}/pages/{seite['id']}"
                             "/microsoft.graph.sitePage?$expand=canvasLayout")
        html = render_page(voll, voll.get("canvasLayout"))
        own = {"bilder": 0, "fehl": 0}
        html = bilder_einbetten(graph, html, site.get("host") or "", own,
                                grenze, cache=cache, lock=lock, marks=marks, verdicts=verdicts)
        with lock:
            for k, n in own.items():
                zaehler[k] += n
        ziel = out / rel
        ziel.parent.mkdir(parents=True, exist_ok=True)
        export_util.schreibe_atomar(ziel, html)
        return own["fehl"]

    uebersprungen = 0
    for s in sites:
        kadenz = s.get("kadenz") or "always"
        if not einheit_faellig(db, kadenz, kv_key=f'last_sync:{s["id"]}'):
            uebersprungen += 1
            pfad = "/".join(s["pfad"])
            progress.event("run.cadence.skip", name=pfad,
                           cadence=progress.atom(f"cadence.{kadenz}"))
            # Not judged this run: the site's pages stay untouched.
            for sid in [k for k, e in eintraege_bestand.items()
                        if e["rel"].startswith(pfad + "/")]:
                gesehen.add(sid)
            continue
        try:
            seiten = list(graph.paged(
                f"{GRAPH}/sites/{s['id']}/pages/microsoft.graph.sitePage"))
        except auth.TokenExpired:
            raise
        except Exception as e:
            progress.event("run.pages.site_failed", "err",
                           url="/".join(s["pfad"]),
                           error=f"{type(e).__name__}: {e}")
            fehl += 1
            continue
        progress.event("run.pages.site", name="/".join(s["pfad"]),
                       n=len(seiten))
        sauber.append("/".join(s["pfad"]))
        auftraege = []
        for seite in seiten:
            sid = seite.get("id")
            if not sid:
                continue
            gesehen.add(sid)
            name = safe((seite.get("name") or "Seite").removesuffix(".aspx"),
                        100, kennung=sid) + ".html"
            rel = "/".join([*s["pfad"], name])
            alt = eintraege_bestand.get(sid)
            etag = seite.get("eTag") or seite.get("lastModifiedDateTime") or ""
            if alt and alt["etag"] == etag and (out / alt["rel"]).is_file():
                unveraendert += 1
                continue
            if _marked(marks, sid, etag):
                unveraendert += 1        # refused or gone in this version: not asked again
                continue
            auftraege.append((seite, rel, etag, alt))
        # Pages fetch and render side by side – the same worker budget the
        # file mirrors use; the inventory is written by this thread only,
        # and only the rows that changed – the table is never rewritten.
        geaendert = {}
        with ThreadPoolExecutor(max_workers=workers()) as pool:
            offen = {pool.submit(exportiere, s, seite, rel): (seite["id"], rel, etag, alt)
                     for seite, rel, etag, alt in auftraege}
            for f in as_completed(offen):
                sid, rel, etag, alt = offen[f]
                try:
                    owed_images = f.result()
                except auth.TokenExpired:
                    raise
                except Exception as e:
                    kind = export_util.verdict(e)
                    if kind:
                        # A verdict on the page: recorded for this version,
                        # no error – the site's tombstones go on.
                        marks[f"page:{sid}"] = export_util.permanent_mark(
                            kind, f"{type(e).__name__}: {e}", name=rel.rsplit("/", 1)[-1],
                            rel=rel, unit="/".join(s["pfad"]), version=etag)
                        export_util.permanent_event(kind, rel, e)
                        continue
                    fehler += 1
                    progress.event("run.pages.page_failed", "err", name=rel,
                                   error=f"{type(e).__name__}: {e}")
                    continue
                marks.pop(f"page:{sid}", None)      # a new version came after all
                neu += 1
                if owed_images:
                    continue         # written, not recorded: rendered again next run
                if alt and alt["rel"] != rel:
                    # Renamed, not deleted: the old file would otherwise
                    # linger untracked as a stale duplicate in the index.
                    (out / alt["rel"]).unlink(missing_ok=True)
                eintraege_bestand[sid] = geaendert[sid] = {"rel": rel, "etag": etag}
        if geaendert:
            db.seiten_aktualisieren(geaendert)
        db._kv_schreiben(f'last_sync:{s["id"]}',
                         str(datetime.now(UTC).timestamp()))
    # Pages gone at Microsoft: in the inventory, reported by no site.
    # Judged ONLY below sites whose listing succeeded this run – a failed
    # listing (or a URL removed from the config) proves nothing about its
    # pages, and tombstones are write-once.
    def beurteilt(rel):
        return any(rel.startswith(pfad + "/") for pfad in sauber)

    weg_ids = [k for k, e in eintraege_bestand.items()
               if k not in gesehen and beurteilt(e["rel"])]
    weg = [eintraege_bestand.pop(k)["rel"] for k in weg_ids]
    if weg:
        jetzt = datetime.now(UTC).isoformat(timespec="seconds")
        db.verschwunden_ergaenzen(weg, jetzt)
        db.seiten_aktualisieren({}, weg_ids)
    marks.update(verdicts)
    db.permanent_abgleichen(marks_before, marks)
    if zaehler["fehl"]:
        progress.event("run.pages.images_failed", "warn", n=zaehler["fehl"])
    extras = {"sites": len(sites), "gone": len(weg),
              "images": zaehler["bilder"]}
    if uebersprungen:
        extras["skipped"] = uebersprungen
    progress.ergebnis(neu, unchanged=unveraendert, errors=fehler + fehl,
                      extra=extras)
    return neu


def seiten_pruefen(graph, out, sites, fehl=0):
    """--check-pages: what Microsoft lists against what lies here, per
    site. Nothing is rendered or written except the report."""
    out = Path(out)
    db = state_db.StateDb(out)
    bestand = db.seiten_lesen()
    weg = db.verschwunden_lesen()
    marks = db.permanent_lesen()
    zeilen, fehler = [], []
    verdicts = {"verweigert": 0, "weg": 0}
    for s in sites:
        pfad = "/".join(s["pfad"])
        try:
            seiten = list(graph.paged(
                f"{GRAPH}/sites/{s['id']}/pages/microsoft.graph.sitePage"))
        except auth.TokenExpired:
            raise
        except Exception as e:
            progress.event("run.pages.site_failed", "err", url=pfad,
                           error=f"{type(e).__name__}: {e}")
            fehler.append(completeness.fehler(pfad, "run.pages.site_failed"))
            continue
        da = offen = 0
        for seite in seiten:
            sid = seite.get("id") or ""
            e = bestand.get(sid)
            if e and (out / e["rel"]).is_file():
                da += 1
            elif _marked(marks, sid, seite.get("eTag") or seite.get("lastModifiedDateTime") or ""):
                # refused or gone in this version: neither here nor open
                verdicts["verweigert" if marks[f"page:{sid}"].get("kind") == export_util.REFUSED
                        else "weg"] += 1
            else:
                offen += 1
        zeilen.append(completeness.zeile(pfad, da, offen))
    bericht = completeness.bilanz(
        "sharepoint_pages", "pages",
        da=sum(z["da"] for z in zeilen), offen=sum(z["offen"] for z in zeilen),
        behalten=len(weg), zeilen=zeilen, fehler=fehler, extra={"kaputt": fehl}, **verdicts)
    completeness.schreiben(db, bericht)
    completeness.melden(bericht)
    return bericht


def main():
    argv = sys.argv[1:]
    if export_util.hilfe_gewuenscht(argv):
        print(__doc__)
        return
    struktur = "--folders" in argv
    pruefen = "--check" in argv
    seiten_pruefung = "--check-pages" in argv
    seiten = "--pages" in argv or seiten_pruefung
    argv = [a for a in argv if not a.startswith("--")]
    out = export_util.ausgabeordner(argv)
    nachzuholen = export_util.nachhol_eintraege()
    if nachzuholen is not None and not seiten:
        # "Fetch again" for the libraries: the files by their inventory
        # ids, the libraries found through their stored URLs – no list.
        graph_client.konfiguriere(workers())
        graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
        try:
            nachholen(graph, out, nachzuholen)
        except auth.TokenExpired:
            progress.fehler("token_expired")
            sys.exit(1)
        return
    urls = pages_urls() if seiten else configured_urls()
    if not urls:
        progress.event("run.pages.none" if seiten else "run.sharepoint.none",
                       "warn")
        progress.ergebnis(0)
        return
    graph_client.konfiguriere(workers())
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    try:
        if seiten:
            sites, fehl = resolve_page_sites(graph, urls)
            if not sites:
                progress.ergebnis(0, errors=fehl)
                return
            (seiten_pruefen if seiten_pruefung else seiten_lauf)(
                graph, out, sites, fehl)
            return
        drives, fehl = resolve_drives(graph, urls)
        if not drives:
            progress.ergebnis(0, errors=fehl)
            return
        (nur_pruefen if pruefen else nur_ordner if struktur else lauf)(
            graph, out, drives, fehl)
    except auth.TokenExpired:
        progress.fehler("token_expired")
        sys.exit(1)


if __name__ == "__main__":
    main()
