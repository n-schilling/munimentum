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
line; SHAREPOINT_TYPES_INCLUDE / SHAREPOINT_TYPES_EXCLUDE – comma-separated
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

OUT_ROOT = settings.value("sharepoint_dir", settings.SHAREPOINT_DIR)
OUT_PAGES = settings.value("sharepoint_pages_dir", settings.SHAREPOINT_PAGES_DIR)


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


def kadenzen():
    """Cadence per source URL ("sharepoint-url:<url>" / "pages-url:<url>")."""
    roh = os.environ.get("SYNC_CADENCE")
    if roh is None:
        roh = json.dumps(settings.value("sync_cadence", {}) or {})
    try:
        daten = json.loads(roh)
    except ValueError:
        return {}
    return daten if isinstance(daten, dict) else {}


def sync_jetzt():
    """The per-row "Sync now" button: the run carries just that URL and
    this flag – the cadence gate steps aside once."""
    return bool((os.environ.get("SYNC_NOW") or "").strip())


_KADENZ_RANG = {"always": 0, "daily": 1, "weekly": 2, "monthly": 3}


def _haeufigere(a, b):
    """Two URLs feeding one unit: the more frequent cadence wins."""
    return a if _KADENZ_RANG.get(a, 0) <= _KADENZ_RANG.get(b, 0) else b


def einheit_faellig(db, kadenz, kv_key="last_sync"):
    if sync_jetzt() or (kadenz or "always") == "always":
        return True
    roh = db._kv_lesen(kv_key)
    letzter = float(roh) if roh else None
    return export_util.cadence_faellig(kadenz, letzter)


def auswahl():
    """The SharePoint selection: extension filters and a size cap.

    No path rules here (v1) – which libraries come along is already the
    decision the URL list makes.
    """
    return Selection(max_bytes=max_bytes(),
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
                           else {unterpfad}}
                nach_id[d["id"]] = eintrag
                gefunden.append(eintrag)
            elif unterpfad is None:
                eintrag["kadenz"] = _haeufigere(eintrag.get("kadenz"), kadenz)
                eintrag["prefixes"] = None            # full scope wins
            elif eintrag["prefixes"] is not None:
                eintrag["kadenz"] = _haeufigere(eintrag.get("kadenz"), kadenz)
                _praefix_aufnehmen(eintrag["prefixes"], unterpfad)
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
    """The per-library Selection: the shared filters, plus the subtree scope
    when the URL pointed below the library root."""
    if not drive.get("prefixes"):
        return basis
    return Selection(scope=_scope_regeln(drive["prefixes"]),
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


def drive_ziel(out, drive):
    return Path(out) / safe(drive["site"], 80) / safe(drive["name"], 80)


def je_drive(graph, drives):
    """Yield (drive, client-ready) – the client is one, the base URL moves."""
    for d in drives:
        graph.drive_base = f"{GRAPH}/drives/{d['id']}"
        yield d


# ---------------------------------------------------------------------------
# The three runs: mirror, folder sync, check/preview
# ---------------------------------------------------------------------------
def lauf(graph, out, drives, fehl=0):
    wahl = auswahl()
    summe = {"new": 0, "excluded": 0, "errors": 0, "moved": 0, "gone": 0}
    uebersprungen = 0
    for d in je_drive(graph, drives):
        ziel = drive_ziel(out, d)
        db = state_db.StateDb(ziel)
        kadenz = d.get("kadenz") or "always"
        if not einheit_faellig(db, kadenz):
            uebersprungen += 1
            progress.event("run.cadence.skip",
                           name=f'{d["site"]} / {d["name"]}',
                           cadence=progress.atom(f"cadence.{kadenz}"))
            continue
        _library_event(d)
        zahlen = drive_mirror.lauf(graph, ziel, drive_auswahl(wahl, d),
                                   workers(), still=True,
                                   zustand=state_db.DbZustand(ziel))
        if not zahlen["errors"]:
            db._kv_schreiben("last_sync", str(datetime.now(UTC).timestamp()))
        for k in summe:
            summe[k] += zahlen[k]
    extras = {"moved": summe["moved"], "gone": summe["gone"]}
    if uebersprungen:
        extras["skipped"] = uebersprungen
    progress.ergebnis(summe["new"], excluded=summe["excluded"],
                      errors=summe["errors"] + fehl, extra=extras)
    return summe


def nur_ordner(graph, out, drives, fehl=0):
    wahl = auswahl()
    neu = gesamt = 0
    for d in je_drive(graph, drives):
        _library_event(d)
        ziel = drive_ziel(out, d)
        daten = drive_mirror.nur_ordner(graph, ziel, drive_auswahl(wahl, d),
                                        still=True,
                                        zustand=state_db.DbZustand(ziel))
        neu += len(daten["neu"])
        gesamt += len(daten.get("ordner") or ())
    progress.ergebnis(neu, errors=fehl, extra={"total": gesamt})


def nur_pruefen(graph, out, drives, fehl=0):
    """--check: the size preview. Enumerates every library without loading.

    Per library one report in its folder plus one merged report at the root –
    the row names carry "site/library", so the completeness view reads it
    like any folder table. The events alongside answer the question the
    preview exists for: what would a run fetch, and how big is it.
    """
    wahl = auswahl()
    zeilen, ausgelassen, ausgelassen_bytes, fehl_summe = [], 0, 0, 0
    ausgelassene = []
    typen = {}
    for d in je_drive(graph, drives):
        _library_event(d)
        ziel = drive_ziel(out, d)
        b = drive_mirror.nur_pruefen(graph, ziel, drive_auswahl(wahl, d),
                                     still=True,
                                     zustand=state_db.DbZustand(ziel))
        zeilen.append({"ordner": f'{d["site"]}/{d["name"]}',
                       "erwartet": b["erwartet"], "vorhanden": b["vorhanden"],
                       "geloescht": b["geloescht"], "fehlt": b["fehlt"],
                       "ausgelassen": False, "bytes": b["bytes"]})
        ausgelassen += b["ausgelassen"]
        ausgelassen_bytes += b.get("bytes_ausgelassen", 0)
        fehl_summe += b["fehlt"]
        ausgelassene += [f'{d["site"]}/{d["name"]}/{o}'
                         for o in b.get("ausgelassene_ordner") or ()]
        for z in b.get("typen") or ():
            ganz = typen.setdefault(z["ext"], {"ext": z["ext"], "n": 0, "bytes": 0})
            ganz["n"] += z["n"]
            ganz["bytes"] += z["bytes"]
        progress.event("run.sharepoint.preview", site=d["site"], name=d["name"],
                       n=b["erwartet"], mb=round(b["bytes"] / 1048576),
                       skipped=b["ausgelassen"])
    bericht = {"geprueft": datetime.now(UTC).isoformat(timespec="seconds"),
               "ordner": sorted(zeilen, key=lambda z: (-z["fehlt"], z["ordner"])),
               "erwartet": sum(z["erwartet"] for z in zeilen),
               "vorhanden": sum(z["vorhanden"] for z in zeilen),
               "geloescht": sum(z["geloescht"] for z in zeilen),
               "fehlt": fehl_summe,
               "ausgelassen": ausgelassen,
               "ausgelassene_ordner": sorted(ausgelassene)[:20],
               "bytes": sum(z["bytes"] for z in zeilen),
               "bytes_ausgelassen": ausgelassen_bytes,
               "typen": sorted(typen.values(), key=lambda z: -z["bytes"])}
    state_db.StateDb(out).bericht_schreiben(bericht)
    progress.ergebnis(0, errors=fehl, excluded=ausgelassen,
                      extra={"expected": bericht["erwartet"],
                             "present": bericht["vorhanden"],
                             "missing": bericht["fehlt"],
                             "mb": round(bericht["bytes"] / 1048576)})
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
                     lock=None):
    """Embed the page's images as data URIs so the file stands alone.

    Failures keep the original URL: signed in, the browser may still show
    it – better than a hole. Images over `grenze` stay links on purpose.
    The run-scoped cache matters: a site logo appears on every page, and
    without it every page re-downloads the same bytes."""
    import html as html_lib
    cache = {} if cache is None else cache
    lock = lock or threading.Lock()

    def ersetze(m):
        roh = html_lib.unescape(m.group(2))
        if roh.startswith("data:"):
            return m.group(0)
        voll = (roh if "://" in roh
                else f"https://{host}{roh}" if roh.startswith("/") else None)
        if not voll:
            return m.group(0)
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
        except Exception:
            with lock:
                zaehler["fehl"] += 1
                cache[voll] = None
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


def seiten_lauf(graph, out, sites, fehl=0):
    out = Path(out)
    db = state_db.StateDb(out)
    eintraege_bestand = db.seiten_lesen()
    neu = unveraendert = fehler = 0
    zaehler = {"bilder": 0, "fehl": 0}
    grenze = bild_max()
    cache = {}
    lock = threading.Lock()
    gesehen = set()
    sauber = []      # sites whose page listing succeeded this run

    def exportiere(site, sid, rel):
        voll = graph.get(f"{GRAPH}/sites/{site['id']}/pages/{sid}"
                         "/microsoft.graph.sitePage?$expand=canvasLayout")
        html = render_page(voll, voll.get("canvasLayout"))
        html = bilder_einbetten(graph, html, site.get("host") or "", zaehler,
                                grenze, cache=cache, lock=lock)
        ziel = out / rel
        ziel.parent.mkdir(parents=True, exist_ok=True)
        export_util.schreibe_atomar(ziel, html)

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
            auftraege.append((sid, rel, etag, alt))
        # Pages fetch and render side by side – the same worker budget the
        # file mirrors use; the inventory is written by this thread only.
        with ThreadPoolExecutor(max_workers=workers()) as pool:
            offen = {pool.submit(exportiere, s, sid, rel): (sid, rel, etag, alt)
                     for sid, rel, etag, alt in auftraege}
            for f in as_completed(offen):
                sid, rel, etag, alt = offen[f]
                try:
                    f.result()
                except auth.TokenExpired:
                    raise
                except Exception as e:
                    fehler += 1
                    progress.event("run.pages.page_failed", "err", name=rel,
                                   error=f"{type(e).__name__}: {e}")
                    continue
                if alt and alt["rel"] != rel:
                    # Renamed, not deleted: the old file would otherwise
                    # linger untracked as a stale duplicate in the index.
                    (out / alt["rel"]).unlink(missing_ok=True)
                eintraege_bestand[sid] = {"rel": rel, "etag": etag}
                neu += 1
        db.seiten_schreiben(eintraege_bestand)
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
    db.seiten_schreiben(eintraege_bestand)
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
    """--check-pages: what Microsoft lists against what lies here, per site.

    The same shape as the mirror check, so the completeness view draws it
    without a second renderer. Nothing is rendered or written except the
    report file."""
    out = Path(out)
    db = state_db.StateDb(out)
    eintraege_bestand = db.seiten_lesen()
    weg = db.verschwunden_lesen()
    zeilen = []
    for s in sites:
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
        pfad = "/".join(s["pfad"])
        vorhanden = sum(
            1 for seite in seiten
            if (e := eintraege_bestand.get(seite.get("id") or ""))
            and (out / e["rel"]).is_file())
        zeilen.append({"ordner": pfad, "erwartet": len(seiten),
                       "vorhanden": vorhanden, "geloescht": 0,
                       "ausgelassen": False,
                       "fehlt": max(0, len(seiten) - vorhanden)})
    # Each tombstone counts exactly once, on its deepest matching site –
    # subsites are rows of their own, nested under the parent's path.
    nach_tiefe = sorted(zeilen, key=lambda z: -z["ordner"].count("/"))
    for rel in weg:
        for z in nach_tiefe:
            if rel.startswith(z["ordner"] + "/"):
                z["geloescht"] += 1
                break
    bericht = {"geprueft": datetime.now(UTC).isoformat(timespec="seconds"),
               "ordner": sorted(zeilen, key=lambda z: (-z["fehlt"], z["ordner"])),
               "erwartet": sum(z["erwartet"] for z in zeilen),
               "vorhanden": sum(z["vorhanden"] for z in zeilen),
               "geloescht": sum(z["geloescht"] for z in zeilen),
               "fehlt": sum(z["fehlt"] for z in zeilen),
               "ausgelassen": 0, "ausgelassene_ordner": []}
    db.bericht_schreiben(bericht)
    progress.ergebnis(0, errors=fehl,
                      extra={"expected": bericht["erwartet"],
                             "present": bericht["vorhanden"],
                             "missing": bericht["fehlt"]})
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
    out = Path(argv[0]) if argv else Path(OUT_PAGES if seiten else OUT_ROOT)
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
