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
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, unquote

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
def site_address(url):
    """Turn a pasted URL into Graph's site address ("host:/sites/TeamX").

    People paste whatever the browser shows – the plain site, a library view
    with /Forms/AllItems.aspx, a folder deep inside. The site part is the
    host plus the first two path segments when they follow the
    sites/teams/personal convention, else the root site of the host.
    """
    u = urlsplit(url if "://" in url else "https://" + url)
    host = u.netloc
    if not host:
        return None
    stuecke = [s for s in unquote(u.path).split("/") if s]
    if stuecke and stuecke[0].lower() in ("sites", "teams", "personal") and len(stuecke) >= 2:
        return f"{host}:/{stuecke[0]}/{stuecke[1]}"
    return host


def resolve_drives(graph, urls):
    """All document libraries behind the configured URLs, deduplicated.

    Broken URLs and denied sites are reported and skipped – one bad line
    must not cost the other mirrors. Returns (drives, failures) where each
    drive is {"id", "site", "name"}.
    """
    gefunden, fehl = [], 0
    gesehen = set()
    for url in urls:
        adresse = site_address(url)
        if not adresse:
            progress.event("run.sharepoint.site_failed", "err", url=url,
                           error="invalid URL")
            fehl += 1
            continue
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
        progress.event("run.sharepoint.libraries", site=sname,
                       n=len(bibliotheken))
        for d in bibliotheken:
            if d.get("id") and d["id"] not in gesehen:
                gesehen.add(d["id"])
                gefunden.append({"id": d["id"], "site": sname,
                                 "name": d.get("name") or "Bibliothek"})
    return gefunden, fehl


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
        progress.event("run.sharepoint.library", site=d["site"], name=d["name"])
        zahlen = drive_mirror.lauf(graph, drive_ziel(out, d), wahl,
                                   workers(), still=True)
        for k in summe:
            summe[k] += zahlen[k]
    progress.ergebnis(summe["new"], excluded=summe["excluded"],
                      errors=summe["errors"] + fehl,
                      extra={"moved": summe["moved"], "gone": summe["gone"]})
    return summe


def nur_ordner(graph, out, drives, fehl=0):
    neu = gesamt = 0
    for d in je_drive(graph, drives):
        progress.event("run.sharepoint.library", site=d["site"], name=d["name"])
        daten = drive_mirror.nur_ordner(graph, drive_ziel(out, d), auswahl(),
                                        still=True)
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
    for d in je_drive(graph, drives):
        ziel = drive_ziel(out, d)
        b = drive_mirror.nur_pruefen(graph, ziel, wahl, still=True)
        zeilen.append({"ordner": f'{d["site"]}/{d["name"]}',
                       "erwartet": b["erwartet"], "vorhanden": b["vorhanden"],
                       "geloescht": b["geloescht"], "fehlt": b["fehlt"],
                       "ausgelassen": False, "bytes": b["bytes"]})
        ausgelassen += b["ausgelassen"]
        ausgelassen_bytes += b.get("bytes_ausgelassen", 0)
        fehl_summe += b["fehlt"]
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
               "bytes_ausgelassen": ausgelassen_bytes}
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
