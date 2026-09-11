#!/usr/bin/env python3
"""
onenote_export.py – OneNote notebooks as a local archive.

One folder per notebook, its section groups and sections as folders below,
and one standalone HTML per page – the page as Graph renders it, with the
notebook, group and section named up top. Images come along embedded up to
a size limit, larger ones and file attachments land next to the page in a
folder of their own; every link then works offline. A notebook's state.db
remembers each page's last change, so a run fetches only pages that moved –
and where every image and attachment of it already lies, so a page that
moved costs its content and nothing else.

The mirror promise is the same as everywhere: the current version of every
page is kept, and a page that disappears from its section stays here with
a marker at the top. Absence in a clean listing of the whole notebook is
the deletion signal.

The drive mirror backs up the raw .one files of notebooks in OneDrive as
well – this export is what makes them readable and searchable.

OneNote rations requests: 120 a minute and 400 an hour per user, a 429
without Retry-After, and every retry counts. So the export paces itself
below those limits (graph_client.takt), remembers the hour's requests in
the output folder's state.db across processes, asks for a notebook's
structure and page list in as few calls as the API allows, and stops a
run cleanly when the hour's budget is spent – the next run continues
where it stopped, since only pages that changed or never arrived are
fetched. A first export of a large notebook therefore takes several runs;
that is the API's limit, not a fault.

Which notebooks come along is decided by ordered rules over the stored
notebook list (folders.py; it lives in the output folder's state.db) –
the same mechanics as mailbox folders and calendars, with "every notebook"
as the default. Each notebook has its own sync cadence (SYNC_CADENCE,
key "onenote:<id>") and its own "sync now" (ONENOTE_ONLY).

Runs as a subprogram of app.py: output folder as the only argument,
settings as environment variables (ONENOTE_RULES, ONENOTE_IMAGE_MAX_MB –
embed images up to this size, 0 = always; SYNC_CADENCE, SYNC_NOW – see
export_util). --notebooks refreshes the stored notebook list and exports
nothing. Progress, results and failures are structured lines (progress.py).
"""

import base64
import binascii
import html as html_lib
import json
import mimetypes
import os
import re
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

import auth
import export_util
import folders
import graph_client
import progress
import settings
import state_db

export_util.erzwinge_utf8()

GRAPH = graph_client.GRAPH
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Notes.Read", RES + "User.Read"]

DATEI_SUFFIX = ".files"          # per page: the folder for large images and attachments
SEITE = 100                      # $top – the OneNote maximum
KADENZ_PRAEFIX = "onenote:"      # sync_cadence key per notebook: onenote:<id>
SEITEN_BEREICH = "pages"         # records per notebook: page id -> bookkeeping
RESSOURCEN_BEREICH = "ressourcen"  # records per notebook: resource id -> where it lies
_RES_ATTR = "data-mn-res"        # an embedded image keeps its resource id for reuse

# OneNote's own limits are 120 a minute and 400 an hour per user and app;
# a little below them, so a stray request from elsewhere does not tip a
# run into refusals. The hour is the budget of one run.
PRO_MINUTE = 100
PRO_STUNDE = 380
_TAKT_KEY = "takt"               # the hour's request moments, in the root state.db


class BudgetLeer(Exception):
    """The hour's requests are spent – the run ends here and continues
    next time. Not an error: the notebook's state is complete as far as
    it got."""


class Abgebrochen(BaseException):
    """The app ended the run (Cancel): stop at the next page and keep what
    arrived. A BaseException on purpose – no page-level handler may take
    it for a failed page and carry on."""


def abbruch_einrichten():
    """Turn the app's terminate into a clean stop instead of a kill in the
    middle of a page. Only the main thread may install handlers, and on
    Windows a terminate cannot be caught at all – there the page-by-page
    bookkeeping alone keeps what arrived."""
    def _abbruch(signum, frame):
        raise Abgebrochen()
    try:
        signal.signal(signal.SIGTERM, _abbruch)
    except (ValueError, OSError, AttributeError):
        pass


def takt_einrichten(out):
    """Pace this process below OneNote's limits, starting from what the
    last hour already cost – the service counts across processes, and a
    run started twice within the hour must know that."""
    db = state_db.StateDb(out)
    try:
        zeiten = json.loads(db.kv_lesen(_TAKT_KEY) or "[]")
    except ValueError:
        zeiten = []
    jetzt = time.time()
    zeiten = [float(t) for t in zeiten if isinstance(t, (int, float))
              and jetzt - 3600 < float(t) <= jetzt]
    return graph_client.takt(PRO_MINUTE, PRO_STUNDE, zeiten)


def takt_merken(out):
    """Persist the request moments – for the next process."""
    takt = graph_client.TAKT
    if takt is None:
        return
    jetzt = time.time()
    with takt._lock:
        zeiten = [t for t in takt.zeiten if t > jetzt - 3600]
    state_db.StateDb(out).kv_schreiben(_TAKT_KEY, json.dumps(zeiten))


def budget_leer():
    """Is the hour spent? A page costs at least two requests (content and,
    as a rule, an image), so the last slot is left alone."""
    takt = graph_client.TAKT
    return takt is not None and takt.verbraucht(3600.0) >= PRO_STUNDE - 1


def budget_pruefen():
    """Before a page: raises BudgetLeer when there is no room for it."""
    if budget_leer():
        raise BudgetLeer()


def listen_erlaubt(name):
    """Before a listing: a spent hour is said once and ends the run, instead
    of a listing that runs into 429 after 429 – every refused request
    counts against the same budget."""
    if budget_leer():
        progress.event("run.onenote.budget_wait", "warn", name=name)
        raise BudgetLeer()

# Graph hands page resources out under a couple of hosts and path shapes
# (users('id') included, hence the tolerated quote); what identifies one is
# the id before "/$value".
RESOURCE_RE = re.compile(
    r"https://(?:graph\.microsoft\.com|www\.onenote\.com)/[^\s\"<>]*?"
    r"/resources/([^/\"'<>]+)/\$value")


def bild_max():
    """Embed images up to this many bytes; 0 means every image."""
    mb = settings.number("ONENOTE_IMAGE_MAX_MB", "onenote_image_max_mb", low=0)
    return int(mb) * 1024 * 1024


class Graph(graph_client.Graph):
    def __init__(self, nur_still=False):
        super().__init__(list(SCOPES), nur_still=nur_still)


class TokenClient(graph_client.TokenClient):
    pass


# ---------------------------------------------------------------------------
# Structure: notebooks, section groups, sections, pages
# ---------------------------------------------------------------------------
def _alle(graph, url):
    return list(graph.paged(url))


def list_notebooks(graph):
    """Every notebook the account sees – own and shared ones opened once."""
    out = []
    for nb in _alle(graph, f"{GRAPH}/me/onenote/notebooks"):
        if nb.get("id"):
            out.append({"id": nb["id"],
                        "titel": str(nb.get("displayName") or nb["id"]),
                        "standard": bool(nb.get("isDefault"))})
    out.sort(key=lambda e: (not e["standard"], e["titel"].lower()))
    return out


def notizbuch_eintraege(buecher, vorher=None):
    """The notebook list in the shape folders.py reckons with.

    The path is the folder the notebook lands in – the notebook's name,
    which is what the folder filter shows; only when two notebooks share
    one does the id short-code keep the second apart. A known notebook
    keeps its folder from the last sync even when a namesake appears
    later: renaming it would orphan every page already here."""
    alt = {e["id"]: e["pfad"] for e in (vorher or {}).get("ordner", [])}
    vergeben, out = set(), []
    for nb in buecher:
        pfad = alt.get(nb["id"])
        if pfad is None or pfad.lower() in vergeben:
            pfad = export_util.safe(nb["titel"])
            if pfad.lower() in vergeben:
                pfad = f'{pfad}__{export_util.kuerzel(nb["id"])}'
        vergeben.add(pfad.lower())
        out.append({"id": nb["id"], "pfad": pfad, "name": nb["titel"],
                    "standard": bool(nb.get("standard")),
                    "elemente": 0})       # Graph does not count pages here
    return out


def notizbuch_regeln():
    """Which notebooks get exported – environment beats file. Without rules
    of your own every notebook comes along: unlike the calendars, a
    notebook is rarely somebody else's noise."""
    roh = os.environ.get("ONENOTE_RULES")
    if roh is None:
        roh = settings.value("onenote_rules", None)
    return folders.lies_regeln(roh or "")


def waehle_notizbuecher(graph, out):
    """Pick notebooks from the stored list; fetch it once when it is
    missing. Returns the chosen notebooks with their folder and cadence."""
    daten = folders.lade(out, folders.NOTIZBUECHER)
    if daten is None:
        listen_erlaubt("OneNote")
        progress.event("run.notebooks.loading")
        eintraege = notizbuch_eintraege(list_notebooks(graph))
        if not eintraege:
            # Storing an empty list would mean never fetching it again.
            progress.event("run.onenote.none", "warn")
            return []
        daten = folders.speichere(out, eintraege, datei=folders.NOTIZBUECHER)
    alle = daten.get("ordner", [])
    gewaehlt = folders.gewaehlt(daten, notizbuch_regeln())
    nur = (os.environ.get("ONENOTE_ONLY") or "").strip()
    if nur:
        gewaehlt = [e for e in gewaehlt if e["id"] == nur]
    progress.event("run.selection", chosen=len(gewaehlt), total=len(alle),
                   unit=progress.atom("progress.unit.notebooks"))
    if daten.get("neu"):
        progress.event("run.selection.new", n=len(daten["neu"]))
    kadenzen = export_util.kadenzen()
    return [{"id": e["id"], "titel": e["name"], "ordner": e["pfad"],
             "kadenz": kadenzen.get(f'{KADENZ_PRAEFIX}{e["id"]}') or "always"}
            for e in gewaehlt]


def gleiche_notizbuecher_ab(graph, out):
    """--notebooks: only fetch and store the notebook list, export nothing."""
    listen_erlaubt("OneNote")
    vorher = folders.lade(out, folders.NOTIZBUECHER)
    daten = folders.speichere(out, notizbuch_eintraege(list_notebooks(graph), vorher),
                              vorher, datei=folders.NOTIZBUECHER)
    gewaehlt = folders.gewaehlt(daten, notizbuch_regeln())
    progress.event("run.sync.result", total=len(daten["ordner"]),
                   chosen=len(gewaehlt),
                   unit=progress.atom("progress.unit.notebooks"))
    if daten["neu"] or daten["verschwunden"] or daten["umbenannt"]:
        progress.event("run.sync.changed", new=len(daten["neu"]),
                       gone=len(daten["verschwunden"]),
                       renamed=len(daten["umbenannt"]))
    progress.ergebnis(len(daten["neu"]),
                      extra={"total": len(daten["ordner"]),
                             "chosen": len(gewaehlt),
                             "gone": len(daten["verschwunden"])})


def notebook_ziel(out, nb):
    """One folder per notebook – the path from the stored list; a notebook
    that arrives without one (tests, old state) gets the tagged form."""
    name = nb.get("ordner") or \
        f'{export_util.safe(nb["titel"])}__{export_util.kuerzel(nb["id"])}'
    return Path(out) / name


def sections(graph, nb):
    """Every section of the notebook with the path of the groups above
    it – [(section, ["Group", "Subgroup"])].

    One expanded request brings the whole tree; only when the API refuses
    the expansion does the walk fall back to one request per group – a
    request is the scarce thing here, and a notebook with twenty groups
    would otherwise cost twenty of the hour's four hundred."""
    try:
        baum = graph.get(
            f'{GRAPH}/me/onenote/notebooks/{nb["id"]}?$expand=sections,'
            "sectionGroups($expand=sections,sectionGroups(levels=max;$expand=sections))")
    except (auth.TokenExpired, graph_client.Ueberlastet):
        raise
    except requests.HTTPError:
        baum = None
    if isinstance(baum, dict) and "sections" in baum:
        return _sections_aus_baum(baum)
    return _sections_gelaufen(graph, nb)


def _sections_aus_baum(baum):
    gefunden = []

    def _knoten(knoten, pfad):
        for s in knoten.get("sections") or []:
            if s.get("id"):
                gefunden.append(({"id": s["id"],
                                  "titel": str(s.get("displayName") or s["id"])},
                                 list(pfad)))
        for g in knoten.get("sectionGroups") or []:
            if g.get("id"):
                _knoten(g, pfad + [str(g.get("displayName") or g["id"])])

    _knoten(baum, [])
    return gefunden


def _sections_gelaufen(graph, nb):
    gefunden = []

    def _gruppe(url_sections, url_groups, pfad):
        for s in _alle(graph, url_sections):
            if s.get("id"):
                gefunden.append(({"id": s["id"],
                                  "titel": str(s.get("displayName") or s["id"])},
                                 list(pfad)))
        for g in _alle(graph, url_groups):
            if not g.get("id"):
                continue
            name = str(g.get("displayName") or g["id"])
            _gruppe(f'{GRAPH}/me/onenote/sectionGroups/{g["id"]}/sections',
                    f'{GRAPH}/me/onenote/sectionGroups/{g["id"]}/sectionGroups',
                    pfad + [name])

    _gruppe(f'{GRAPH}/me/onenote/notebooks/{nb["id"]}/sections',
            f'{GRAPH}/me/onenote/notebooks/{nb["id"]}/sectionGroups', [])
    return gefunden


def pages(graph, section):
    """The section's pages – id, title, times."""
    url = (f'{GRAPH}/me/onenote/sections/{section["id"]}/pages'
           f"?$top={SEITE}"
           "&$select=id,title,createdDateTime,lastModifiedDateTime")
    return [p for p in _alle(graph, url) if p.get("id")]


def notebook_pages(graph, nb):
    """Every page of the notebook, keyed by section id – one paged listing
    for the whole notebook (a hundred pages a request) instead of one
    request per section. None when the API refuses the filter; the caller
    then lists per section."""
    url = (f"{GRAPH}/me/onenote/pages?$filter=parentNotebook/id eq "
           f"'{nb['id']}'&$expand=parentSection,parentNotebook&$top={SEITE}"
           "&$select=id,title,createdDateTime,lastModifiedDateTime")
    try:
        seiten = _alle(graph, url)
    except (auth.TokenExpired, graph_client.Ueberlastet):
        raise
    except requests.HTTPError:
        return None
    je_section = {}
    for p in seiten:
        sid = (p.get("parentSection") or {}).get("id")
        if p.get("id") and sid:
            je_section.setdefault(sid, []).append(p)
    return je_section


def seiten_rel(section, gruppen, page):
    """Where the page lands: Group/…/Section/Title__id.html – every segment
    sanitised on its own. Groups and sections carry no tag: OneNote keeps
    their names unique within the parent, and the folder path is what the
    folder filter shows. Page titles may repeat, hence the tag on the file."""
    stuecke = [export_util.safe(g) for g in gruppen]
    stuecke.append(export_util.safe(section["titel"]))
    titel = export_util.safe(str(page.get("title") or "Ohne Titel"), 60)
    titel = re.sub(r"[&#%?]", "_", titel)
    stuecke.append(f'{titel}__{export_util.kuerzel(page["id"])}.html')
    return "/".join(stuecke)


# ---------------------------------------------------------------------------
# Page content: resources offline, a header up top
# ---------------------------------------------------------------------------
_STIL = """
.mn-kopf{font-family:-apple-system,'Segoe UI',sans-serif;font-size:12px;
  color:#666;border-bottom:1px solid #ddd;padding:6px 0 8px;margin:0 0 12px}
.mn-kopf b{color:#222;font-size:15px;display:block;margin-bottom:2px}
.mn-weg{font-family:-apple-system,'Segoe UI',sans-serif;font-size:12px;
  background:#fff4e5;border:1px solid #f0c27a;color:#7a4b00;padding:6px 10px;
  border-radius:6px;margin:0 0 12px}
.mn-anhang{font-family:-apple-system,'Segoe UI',sans-serif;font-size:13px;
  display:inline-block;padding:4px 10px;border:1px solid #d8d8d8;
  border-radius:6px;background:#f7f7f7;color:#2b6cb0;text-decoration:none}
"""


def _endung(ctype, name=None):
    if name and "." in name:
        return ""
    e = mimetypes.guess_extension((ctype or "").split(";")[0].strip()) or ""
    return ".jpg" if e == ".jpe" else e


# An embedded image as the export wrote it: its resource id first, the
# data URI further on in the same tag.
_INLINE_RE = re.compile(
    r'<img\s+' + _RES_ATTR + r'="([^"]*)"[^>]*?\ssrc="(data:[^"]*)"', re.I)


class _Ressourcen:
    """Fetches a page's images and attachments once each and decides for
    every one: inline data URI or a file next to the page.

    What an earlier run already brought is not asked for again: the
    notebook's state.db holds a record per resource id – the file next to
    the page, or the page itself for an embedded image, which keeps its id
    as an attribute. A re-fetched page then costs its content and nothing
    else; OneNote's 400 requests an hour are the reason. The bytes come
    back from disk and go through the same decision as fresh ones, so a
    changed size limit or a moved page still lands right."""

    def __init__(self, graph, ziel, seite_rel, grenze, stand=None):
        self.graph, self.ziel, self.grenze = graph, ziel, grenze
        self.seite_rel = seite_rel
        self.rel_dir = seite_rel[:-5] + DATEI_SUFFIX
        self.stand = stand if stand is not None else {}   # id -> record, from the db
        self.neu = {}                # id -> record, this page's outcome
        self.cache = {}
        self._seiten = {}            # old page file -> {id: data URI}, read once
        self._herkunft = {}          # id -> the file the reused bytes came from
        self.fehler = 0
        self.dateien = 0
        self.wiederverwendet = 0

    def _aus_seite(self, pfad, kennung):
        if pfad not in self._seiten:
            try:
                html = pfad.read_text(encoding="utf-8")
            except OSError:
                html = ""
            self._seiten[pfad] = {html_lib.unescape(k): v
                                  for k, v in _INLINE_RE.findall(html)}
        uri = self._seiten[pfad].get(kennung)
        if not uri:
            return None
        kopf, _, b64 = uri.partition(",")
        try:
            daten = base64.b64decode(b64)
        except (ValueError, binascii.Error):
            return None
        return daten, kopf[5:].split(";")[0]

    def _vorrat(self, kennung):
        """The resource as an earlier run left it – (bytes, type), or None
        when nothing usable lies here."""
        satz = self.stand.get(kennung)
        if not satz or not satz.get("rel"):
            return None
        pfad = self.ziel / satz["rel"]
        if satz.get("inline"):
            gefunden = self._aus_seite(pfad, kennung)
        else:
            try:
                daten = pfad.read_bytes()
            except OSError:
                return None
            if satz.get("size") is not None and len(daten) != int(satz["size"]):
                return None
            gefunden = (daten, satz.get("type")
                        or mimetypes.guess_type(pfad.name)[0] or "")
        if gefunden is not None:
            self._herkunft[kennung] = pfad
        return gefunden

    def _hole(self, url, kennung):
        if url in self.cache:
            return self.cache[url]
        vorrat = self._vorrat(kennung)
        if vorrat is not None:
            self.wiederverwendet += 1
            self.cache[url] = vorrat
            return vorrat
        daten, ctype = self.graph.get_bytes(url, label=" (OneNote)")
        self.cache[url] = (daten, ctype)
        return self.cache[url]

    def _merke(self, kennung, rel, daten, ctype, inline=False):
        satz = {"rel": rel, "size": len(daten),
                "type": (ctype or "").split(";")[0],
                "seen": datetime.now(UTC).isoformat(timespec="seconds")}
        if inline:
            satz["inline"] = True
        self.neu[kennung] = satz

    def _ablegen(self, name, daten, kennung, ctype):
        rel = f"{self.rel_dir}/{name}"
        pfad = self.ziel / rel
        if self._herkunft.get(kennung) != pfad:
            pfad.parent.mkdir(parents=True, exist_ok=True)
            pfad.write_bytes(daten)
            self.dateien += 1
        self._merke(kennung, rel, daten, ctype)
        # The link is relative to the page, which lies next to its folder.
        return f"{self.rel_dir.rsplit('/', 1)[-1]}/{name}"

    def bild(self, url, kennung):
        daten, ctype = self._hole(url, kennung)
        if not self.grenze or len(daten) <= self.grenze:
            self._merke(kennung, self.seite_rel, daten, ctype, inline=True)
            return (f"data:{ctype.split(';')[0] or 'image/png'};base64,"
                    + base64.b64encode(daten).decode())
        name = export_util.kuerzel(kennung) + _endung(ctype)
        return self._ablegen(name, daten, kennung, ctype)

    def anhang(self, url, kennung, name):
        daten, ctype = self._hole(url, kennung)
        sicher = re.sub(r"[&#%?]", "_", export_util.safe(name or "datei"))
        return self._ablegen(f"{export_util.kuerzel(kennung)}_{sicher}"
                             + _endung(ctype, sicher), daten, kennung, ctype)


def _attr(tag, name):
    m = re.search(r'\s' + re.escape(name) + r'\s*=\s*"([^"]*)"', tag, re.I)
    if not m:
        m = re.search(r"\s" + re.escape(name) + r"\s*=\s*'([^']*)'", tag, re.I)
    return html_lib.unescape(m.group(1)) if m else None


def _ohne_attr(tag, name):
    return re.sub(r'\s' + re.escape(name) + r'\s*=\s*("[^"]*"|\'[^\']*\')', "",
                  tag, flags=re.I)


def ressourcen_offline(html, res):
    """Rewrite the page HTML so every image and attachment works without
    Graph: <img src> becomes a data URI or a local file, <object> with an
    attachment becomes a link to the file next to the page."""

    def bild(m):
        tag = m.group(0)
        src = _attr(tag, "src")
        treffer = RESOURCE_RE.search(src or "")
        if not treffer:
            return tag
        try:
            neu = res.bild(src, treffer.group(1))
        except auth.TokenExpired:
            raise
        except Exception:
            res.fehler += 1
            return tag
        tag = _ohne_attr(tag, "data-fullres-src")
        tag = _ohne_attr(tag, "data-fullres-src-type")
        tag = re.sub(r'(\ssrc\s*=\s*)("[^"]*"|\'[^\']*\')',
                     lambda a: a.group(1) + '"' + html_lib.escape(neu, quote=True) + '"',
                     tag, count=1, flags=re.I)
        # The id stays on the tag: the next fetch of this page finds the
        # embedded bytes here instead of asking Graph again.
        return re.sub(r"^<img\b", '<img ' + _RES_ATTR + '="'
                      + html_lib.escape(treffer.group(1), quote=True) + '"',
                      tag, count=1, flags=re.I)

    def objekt(m):
        tag = m.group(1)
        data = _attr(tag, "data")
        treffer = RESOURCE_RE.search(data or "")
        if not treffer:
            return m.group(0)
        name = _attr(tag, "data-attachment") or "Anhang"
        try:
            rel = res.anhang(data, treffer.group(1), name)
        except auth.TokenExpired:
            raise
        except Exception:
            res.fehler += 1
            return (f'<span class="mn-anhang">📎 {html_lib.escape(name)}'
                    " (nicht geladen)</span>")
        return (f'<a class="mn-anhang" href="{html_lib.escape(rel, quote=True)}">'
                f"📎 {html_lib.escape(name)}</a>")

    html = re.sub(r"<img\b[^>]*>", bild, html, flags=re.I)
    html = re.sub(r"(<object\b[^>]*>)(?:.*?</object>)?", objekt, html,
                  flags=re.I | re.S)
    return html


def _saeubere(html):
    """Keep the markup, drop what could execute – the file must open
    harmlessly offline."""
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html or "",
                  flags=re.I | re.S)
    return re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", "", html)


def _kopfzeile(nb, gruppen, section, page, weg=None):
    pfad = " › ".join(html_lib.escape(s) for s in
                      [nb["titel"], *gruppen, section["titel"]])
    zeiten = []
    if page.get("createdDateTime"):
        zeiten.append("erstellt " + str(page["createdDateTime"])[:10])
    if page.get("lastModifiedDateTime"):
        zeiten.append("geändert " + str(page["lastModifiedDateTime"])[:10])
    kopf = (f'<div class="mn-kopf"><b>{html_lib.escape(str(page.get("title") or "Ohne Titel"))}</b>'
            f'{pfad} · {" · ".join(zeiten)}</div>')
    if weg:
        kopf += (f'<div class="mn-weg">Nicht mehr im Notizbuch seit '
                 f"{html_lib.escape(str(weg)[:10])}</div>")
    return kopf


def seite_html(roh, kopf):
    """The page as Graph renders it, style and header added – wrapped
    should the content arrive without a document frame."""
    roh = _saeubere(roh)
    if re.search(r"<body\b", roh, re.I):
        roh = re.sub(r"</head>", f"<style>{_STIL}</style></head>", roh,
                     count=1, flags=re.I)
        return re.sub(r"(<body\b[^>]*>)", lambda m: m.group(1) + kopf, roh,
                      count=1, flags=re.I)
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f"<style>{_STIL}</style></head><body>{kopf}{roh}</body></html>")


_WEG_RE = re.compile(r'<div class="mn-weg">.*?</div>', re.S)


def markiere_weg(pfad, seit):
    """A page that left its section keeps its file and gets the marker –
    written once; the timestamp must not creep forward."""
    try:
        html = pfad.read_text(encoding="utf-8")
    except OSError:
        return
    if _WEG_RE.search(html):
        return
    marker = (f'<div class="mn-weg">Nicht mehr im Notizbuch seit '
              f"{html_lib.escape(str(seit)[:10])}</div>")
    neu = re.sub(r'(<div class="mn-kopf">.*?</div>)', lambda m: m.group(1) + marker,
                 html, count=1, flags=re.S)
    if neu == html:
        neu = re.sub(r"(<body\b[^>]*>)", lambda m: m.group(1) + marker, html,
                     count=1, flags=re.I)
    export_util.schreibe_atomar(pfad, neu)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def _entferne(ziel, rel):
    """An old copy of a page – renamed section or title – and its files."""
    datei = ziel / rel
    try:
        datei.unlink()
    except OSError:
        pass
    ordner = ziel / (rel[:-5] + DATEI_SUFFIX)
    if ordner.is_dir():
        for f in ordner.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            ordner.rmdir()
        except OSError:
            pass


def seite_lauf(graph, ziel, nb, gruppen, section, page, grenze, ressourcen=None):
    """Fetch one page and write it; returns (rel, resources that did not
    come, the resource records this page leaves behind)."""
    rel = seiten_rel(section, gruppen, page)
    roh, _typ = graph.get_bytes(f'{GRAPH}/me/onenote/pages/{page["id"]}/content',
                                label=" (Seite)")
    res = _Ressourcen(graph, ziel, rel, grenze, stand=ressourcen)
    html = ressourcen_offline(roh.decode("utf-8", "replace"), res)
    datei = export_util.schreibe_atomar(
        ziel / rel, seite_html(html, _kopfzeile(nb, gruppen, section, page)))
    # The page's own change date on the file: the index sorts by it, and
    # every page carrying the moment of its download would make that
    # order worthless.
    dt = export_util.graph_zeit(page.get("lastModifiedDateTime"))
    if dt is not None:
        try:
            os.utime(datei, (dt.timestamp(), dt.timestamp()))
        except OSError:
            pass
    return rel, res.fehler, res.neu


def _saetze(db, bereich):
    out = {}
    for key, roh in db.saetze_lesen(bereich).items():
        try:
            out[key] = json.loads(roh)
        except ValueError:
            continue
    return out


def _saetze_schreiben(db, bereich, eintraege):
    if eintraege:
        db.saetze_schreiben(bereich, {k: json.dumps(v, ensure_ascii=False)
                                      for k, v in eintraege.items()})


def seitenstand(db):
    """The notebook's page bookkeeping – one record per page.

    A notebook from before 9.0 holds it as one kv blob; it moves into the
    records once, and the blob goes with that."""
    stand = _saetze(db, SEITEN_BEREICH)
    if stand:
        return stand
    try:
        alt = json.loads(db.kv_lesen("pages") or "{}")
    except ValueError:
        alt = {}
    if isinstance(alt, dict) and alt:
        _saetze_schreiben(db, SEITEN_BEREICH, alt)
        db.kv_loeschen("pages")
        return alt
    return {}


def notebook_lauf(graph, out, nb, grenze):
    """One notebook: walk its sections, fetch the pages that moved, mark
    what vanished. Returns (new, unchanged, errors).

    The bookkeeping is written page by page, not at the end: a cancelled
    or killed run keeps every page that arrived, and the next run starts
    where it stopped."""
    listen_erlaubt(nb["titel"])
    ziel = notebook_ziel(out, nb)
    db = state_db.StateDb(ziel)
    stand = seitenstand(db)
    ressourcen = _saetze(db, RESSOURCEN_BEREICH)
    db.kv_schreiben("notebook", json.dumps(
        {"id": nb["id"], "titel": nb["titel"]}, ensure_ascii=False))

    def sichern(seiten=None, neue_ressourcen=None):
        """The rows this step touched – one per page, one per resource –
        and the pacer's moments; never the whole table."""
        _saetze_schreiben(db, SEITEN_BEREICH, seiten)
        _saetze_schreiben(db, RESSOURCEN_BEREICH, neue_ressourcen)
        takt_merken(out)

    abschnitte = sections(graph, nb)
    je_section = notebook_pages(graph, nb)
    faellig, gesehen = [], set()
    unveraendert = 0
    listen_fehler = 0
    for section, gruppen in abschnitte:
        try:
            liste = (je_section.get(section["id"], []) if je_section is not None
                     else pages(graph, section))
        except (auth.TokenExpired, graph_client.Ueberlastet):
            raise
        except Exception as e:
            listen_fehler += 1
            progress.event("run.onenote.section_failed", "warn",
                           name=f'{nb["titel"]} / {section["titel"]}',
                           error=f"{type(e).__name__}: {e}")
            continue
        for p in liste:
            gesehen.add(p["id"])
            alt = stand.get(p["id"]) or {}
            rel = seiten_rel(section, gruppen, p)
            if (alt.get("lm") == (p.get("lastModifiedDateTime") or "")
                    and alt.get("rel") == rel and not alt.get("deleted")
                    and (ziel / rel).exists()):
                unveraendert += 1
                continue
            faellig.append((section, gruppen, p, alt))
    progress.event("run.onenote.start", name=nb["titel"],
                   n=len(gesehen), m=len(faellig))
    neu = fehler = 0
    offen = None                       # pages left for the next run
    if faellig:
        progress.melde(0, len(faellig), "pages")
    for lfd, (section, gruppen, p, alt) in enumerate(faellig):
        try:
            budget_pruefen()
            rel, res_fehler, res_neu = seite_lauf(graph, ziel, nb, gruppen,
                                                  section, p, grenze, ressourcen)
            ressourcen.update(res_neu)
            if alt.get("rel") and alt["rel"] != rel:
                _entferne(ziel, alt["rel"])
            if res_fehler:
                progress.event("run.onenote.resources_failed", "warn",
                               name=str(p.get("title") or "?")[:60],
                               n=res_fehler)
            stand[p["id"]] = {"rel": rel, "lm": p.get("lastModifiedDateTime") or "",
                              "titel": str(p.get("title") or ""),
                              "section": section["titel"],
                              "notebook": nb["titel"], "deleted": None}
            neu += 1
            sichern({p["id"]: stand[p["id"]]}, res_neu)
            progress.melde(lfd + 1, len(faellig), "pages")
        except auth.TokenExpired:
            raise
        except Abgebrochen:
            sichern()
            raise
        except (BudgetLeer, graph_client.Ueberlastet) as e:
            # The hour is spent: keep what arrived, say what waits, and
            # let the run end cleanly – the next one continues here.
            offen = len(faellig) - lfd
            progress.event("run.onenote.budget" if isinstance(e, BudgetLeer)
                           else "run.onenote.throttled", "warn",
                           name=nb["titel"], n=offen)
            break
        except Exception as e:
            fehler += 1
            progress.event("run.onenote.page_failed", "err",
                           name=str(p.get("title") or p["id"])[:60],
                           error=f"{type(e).__name__}: {e}")
    # Absence in a complete, error-free walk is the deletion signal – the
    # file stays and gets its marker. A run cut short by the budget saw
    # the complete listing all the same: what is missing from it is gone.
    if not fehler and not listen_fehler:
        jetzt = datetime.now(UTC).isoformat(timespec="seconds")
        weg = {}
        for pid, e in stand.items():
            if pid not in gesehen and not e.get("deleted"):
                e["deleted"] = jetzt
                markiere_weg(ziel / e["rel"], jetzt)
                weg[pid] = e
        _saetze_schreiben(db, SEITEN_BEREICH, weg)
    takt_merken(out)
    progress.event("run.onenote.notebook", name=nb["titel"], n=len(gesehen))
    if offen:
        raise BudgetLeer((neu, unveraendert, fehler + listen_fehler))
    return neu, unveraendert, fehler + listen_fehler


def lauf(graph, out, buecher):
    out = Path(out)
    grenze = bild_max()
    neu = unveraendert = fehler = fehl = uebersprungen = 0
    for nb in buecher:
        db = state_db.StateDb(notebook_ziel(out, nb))
        kadenz = nb.get("kadenz") or "always"
        if not export_util.einheit_faellig(db, kadenz):
            uebersprungen += 1
            progress.event("run.cadence.skip", name=nb["titel"],
                           cadence=progress.atom(f"cadence.{kadenz}"))
            continue
        try:
            n, u, f = notebook_lauf(graph, out, nb, grenze)
        except auth.TokenExpired:
            raise
        except BudgetLeer as e:
            # What the notebook got before the hour ran out counts; the
            # notebooks after it wait for the next run, and this one's
            # last_sync stays put so the cadence does not skip it.
            if e.args and isinstance(e.args[0], tuple):
                n, u, f = e.args[0]
                neu, unveraendert, fehler = neu + n, unveraendert + u, fehler + f
            rest = len(buecher) - buecher.index(nb) - 1
            if rest:
                progress.event("run.onenote.budget_rest", "warn", n=rest)
            break
        except graph_client.Ueberlastet:
            rest = len(buecher) - buecher.index(nb)
            progress.event("run.onenote.throttled", "warn", name=nb["titel"], n=0)
            if rest > 1:
                progress.event("run.onenote.budget_rest", "warn", n=rest - 1)
            break
        except Exception as e:
            progress.event("run.onenote.notebook_failed", "err",
                           name=nb["titel"], error=f"{type(e).__name__}: {e}")
            fehl += 1
            continue
        neu, unveraendert, fehler = neu + n, unveraendert + u, fehler + f
        if not f:
            db.kv_schreiben("last_sync", str(datetime.now(UTC).timestamp()))
    progress.ergebnis(neu, unchanged=unveraendert, errors=fehler + fehl,
                      extra={"notebooks": len(buecher),
                             **({"skipped": uebersprungen}
                                if uebersprungen else {})})


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if export_util.hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__)
        return
    out = export_util.ausgabeordner(argv)
    out.mkdir(parents=True, exist_ok=True)
    takt_einrichten(out)
    abbruch_einrichten()
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    try:
        if "--notebooks" in sys.argv[1:]:
            gleiche_notizbuecher_ab(graph, out)
            return
        buecher = waehle_notizbuecher(graph, out)
        if not buecher:
            progress.event("run.selection.empty")
            progress.ergebnis(0)
            return
        progress.event("run.onenote.notebooks", n=len(buecher))
        lauf(graph, out, buecher)
    except auth.TokenExpired:
        progress.fehler("token_expired")
        sys.exit(1)
    except graph_client.Ueberlastet:
        # The listing itself ran into the spent hour – nothing to do but
        # say so; the next run tries again.
        progress.event("run.onenote.throttled", "warn", name="OneNote", n=0)
        progress.ergebnis(0)
    except BudgetLeer:
        progress.ergebnis(0)          # said by listen_erlaubt already
    except Abgebrochen:
        sys.exit(130)                 # the app asked; what arrived is kept
    finally:
        takt_merken(out)


if __name__ == "__main__":
    main()
