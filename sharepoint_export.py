#!/usr/bin/env python3
"""
SharePoint mirror: configured sites and document libraries as local copies.

To Graph a document library is a drive, so the machinery is drive_mirror.py –
the same promises as the OneDrive mirror: the CURRENT version of every file
is kept, deletions leave a tombstone in verschwunden.tsv. What is SharePoint
here is only the addressing: URLs are resolved to sites, sites list their
libraries, and every library is mirrored into its own folder
(``<site>/<library>/Dateien/…``) with its own delta pointer and inventory.

Runs as a subprogram of app.py: output folder as the only argument, settings
as environment variables (SHAREPOINT_URLS – one site or library URL per
line; SHAREPOINT_TYPES_INCLUDE / SHAREPOINT_TYPES_EXCLUDE – comma-separated
file extensions, include empty = every type, exclude wins;
SHAREPOINT_MAX_MB – skip larger files, 0 = no limit; EXPORT_WORKERS –
parallel downloads). Special runs: --folders syncs the folder trees,
--check enumerates without downloading and reports per library what a
mirror run would fetch – count, size, and what the filters leave out
(the size preview) – plus what is missing locally.

Access needs Sites.Read.All. A pasted key without that scope does not kill
the run: the affected site is reported and skipped.
"""

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit, unquote

import auth
import export_util
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
from drive_mirror import Selection, safe

export_util.erzwinge_utf8()

GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Sites.Read.All", RES + "Files.Read.All", RES + "User.Read"]

OUT_ROOT = settings.value("sharepoint_dir", settings.SHAREPOINT_DIR)
BERICHT_DATEI = drive_mirror.BERICHT_DATEI


def workers():
    return max(1, min(settings.number("EXPORT_WORKERS", "workers"), 8))


def configured_urls():
    """One URL per line, environment over file; blanks and comments drop out."""
    roh = os.environ.get("SHAREPOINT_URLS")
    if roh is None:
        roh = settings.value("sharepoint_urls", "") or ""
    zeilen = [z.strip() for z in str(roh).splitlines()]
    return [z for z in zeilen if z and not z.startswith("#")]


def _types(env_name, key):
    roh = os.environ.get(env_name)
    if roh is None:
        roh = settings.value(key, "") or ""
    return [t for t in (s.strip() for s in str(roh).split(",")) if t]


def max_bytes():
    return max(0, settings.number("SHAREPOINT_MAX_MB", "sharepoint_max_mb",
                                  low=0)) * 1024 * 1024


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
    for url in urls:
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
                           "prefixes": None if unterpfad is None
                           else {unterpfad}}
                nach_id[d["id"]] = eintrag
                gefunden.append(eintrag)
            elif unterpfad is None:
                eintrag["prefixes"] = None            # full scope wins
            elif eintrag["prefixes"] is not None:
                eintrag["prefixes"].add(unterpfad)
    return gefunden, fehl


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
    return Selection(rules=_scope_regeln(drive["prefixes"]),
                     max_bytes=basis.max_bytes,
                     include_ext=basis.include_ext,
                     exclude_ext=basis.exclude_ext)


def scope_sammler(prefixes):
    """A collector for one folder of a big library.

    Delta only exists on a drive's root – for a URL that points at one
    project folder it would enumerate the whole library on every run. The
    collector instead lists exactly the configured subtrees (one request per
    folder inside the scope) and synthesises the deletions from the
    inventory: what the walk no longer sees but dateien.tsv still knows is
    gone. No delta pointer is used or advanced.
    """
    regeln = _scope_regeln(prefixes)

    def sammler(graph, bestand):
        eintraege, gesehen = [], set()
        for pf in sorted(prefixes):
            teile = [s for s in pf.split("/") if s]
            pfad = "/".join(quote(s, safe="") for s in teile)
            try:
                wurzel = graph.get(f"{graph.drive_base}/root:/{pfad}")
            except auth.TokenExpired:
                raise
            except Exception as e:
                progress.event("run.sharepoint.scope_missing", "warn",
                               path=pf, error=f"{type(e).__name__}: {e}")
                continue
            eintraege.append(wurzel)
            gesehen.add(wurzel.get("id"))
            stapel = [(teile, wurzel)]
            while stapel:
                eltern_teile, _ = stapel.pop()
                kinder_pfad = "/".join(quote(s, safe="") for s in eltern_teile)
                for e in graph.paged(
                        f"{graph.drive_base}/root:/{kinder_pfad}:/children",
                        {"$top": drive_mirror.SEITE}):
                    eintraege.append(e)
                    gesehen.add(e.get("id"))
                    if "folder" in e and "file" not in e:
                        stapel.append((eltern_teile + [e.get("name") or ""], e))
        # Inside the scope, missing from the walk, still in the inventory:
        # that is a deletion – delta would have said so, the diff says it now.
        for kennung, e in list(bestand.eintraege.items()):
            if kennung not in gesehen and folders.gilt(e["rel"], regeln,
                                                       vorgabe=False):
                eintraege.append({"id": kennung, "deleted": {}})
        return eintraege, None

    return sammler


def _drive_sammler(drive):
    return scope_sammler(drive["prefixes"]) if drive.get("prefixes") else None


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
    for d in je_drive(graph, drives):
        _library_event(d)
        zahlen = drive_mirror.lauf(graph, drive_ziel(out, d),
                                   drive_auswahl(wahl, d),
                                   workers(), still=True,
                                   sammler=_drive_sammler(d))
        for k in summe:
            summe[k] += zahlen[k]
    progress.ergebnis(summe["new"], excluded=summe["excluded"],
                      errors=summe["errors"] + fehl,
                      extra={"moved": summe["moved"], "gone": summe["gone"]})
    return summe


def nur_ordner(graph, out, drives, fehl=0):
    wahl = auswahl()
    neu = gesamt = 0
    for d in je_drive(graph, drives):
        _library_event(d)
        daten = drive_mirror.nur_ordner(graph, drive_ziel(out, d),
                                        drive_auswahl(wahl, d),
                                        still=True,
                                        sammler=_drive_sammler(d))
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
    typen = {}
    for d in je_drive(graph, drives):
        _library_event(d)
        ziel = drive_ziel(out, d)
        b = drive_mirror.nur_pruefen(graph, ziel, drive_auswahl(wahl, d),
                                     still=True, sammler=_drive_sammler(d))
        zeilen.append({"ordner": f'{d["site"]}/{d["name"]}',
                       "erwartet": b["erwartet"], "vorhanden": b["vorhanden"],
                       "geloescht": b["geloescht"], "fehlt": b["fehlt"],
                       "ausgelassen": False, "bytes": b["bytes"]})
        ausgelassen += b["ausgelassen"]
        ausgelassen_bytes += b.get("bytes_ausgelassen", 0)
        fehl_summe += b["fehlt"]
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
               "ausgelassene_ordner": [],
               "bytes": sum(z["bytes"] for z in zeilen),
               "bytes_ausgelassen": ausgelassen_bytes,
               "typen": sorted(typen.values(), key=lambda z: -z["bytes"])}
    drive_mirror.schreibe_bericht(Path(out), bericht)
    progress.ergebnis(0, errors=fehl, excluded=ausgelassen,
                      extra={"expected": bericht["erwartet"],
                             "present": bericht["vorhanden"],
                             "missing": bericht["fehlt"],
                             "mb": round(bericht["bytes"] / 1048576)})
    return bericht


def main():
    argv = sys.argv[1:]
    if export_util.hilfe_gewuenscht(argv):
        print(__doc__)
        return
    struktur = "--folders" in argv
    pruefen = "--check" in argv
    argv = [a for a in argv if not a.startswith("--")]
    out = Path(argv[0]) if argv else Path(OUT_ROOT)
    urls = configured_urls()
    if not urls:
        progress.event("run.sharepoint.none", "warn")
        progress.ergebnis(0)
        return
    graph_client.konfiguriere(workers())
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    try:
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
