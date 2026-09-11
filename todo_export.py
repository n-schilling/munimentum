#!/usr/bin/env python3
"""
todo_export.py – Microsoft To Do lists as a local archive.

One folder per list, holding a standalone list.html (open and completed
tasks with their steps, due dates, reminders, notes, linked resources and
attachments) plus the list's state.db with the raw task data. The mirror
promise is the same as everywhere: the current version of the list is kept,
and a task that disappears from the list stays here, rendered into a greyed
"no longer in the list" section.

Every list is read through its delta feed: the first round brings every
task (steps and linked resources expanded), later rounds only what changed
since the stored deltaLink – a task the feed reports as removed moves to
the greyed section. A feed the service no longer remembers (410 Gone, or
the syncStateNotFound the docs name for Outlook-backed entities) is read
once in full again; in a full, error-free read absence is the deletion
signal. Attachments – To Do caps them at 25 MB – are fetched next to the
list whenever the task they belong to changed. Lists run side by side
(EXPORT_WORKERS); list.html and the state blobs are written only when the
run changed something.

Runs as a subprogram of app.py: output folder as the only argument,
settings as environment variables (SYNC_CADENCE – key "todo" for the whole
source, "todo:<pfad>" for one list, <pfad> being the list title as a folder
name; SYNC_NOW steps over every gate; see export_util. TODO_RULES – ordered
include/exclude rules over the same paths, see folders.py, empty means
every list). --lists refreshes the stored list of lists and exports
nothing. Progress, results and failures are structured lines (progress.py).
"""

import html as html_lib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

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
SCOPES = [RES + "Tasks.Read", RES + "User.Read"]

ANHANG_DIR = "Anhaenge"

# What the cards say – German like every other archive file this app
# writes; the interface language does not reach into the data.
_WICHTIG = {"high": "wichtig", "low": "niedrig"}
_STATUS = {"notStarted": "offen", "inProgress": "in Arbeit",
           "completed": "erledigt", "waitingOnOthers": "wartet",
           "deferred": "zurückgestellt"}
_TAKT = {"daily": "täglich", "weekly": "wöchentlich",
         "absoluteMonthly": "monatlich", "relativeMonthly": "monatlich",
         "absoluteYearly": "jährlich", "relativeYearly": "jährlich"}

# The lists run side by side and each says its lines: one at a time, so
# the app's parser always gets whole lines.
_AUSGABE = threading.Lock()


def _event(key, level="info", **vars):
    with _AUSGABE:
        progress.event(key, level, **vars)


def _json(roh):
    """A stored JSON object, {} for nothing or nonsense."""
    try:
        daten = json.loads(roh) if roh else {}
    except ValueError:
        return {}
    return daten if isinstance(daten, dict) else {}


class Graph(graph_client.Graph):
    def __init__(self, nur_still=False):
        super().__init__(list(SCOPES), nur_still=nur_still)


class TokenClient(graph_client.TokenClient):
    pass


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------
def _alle(graph, url):
    return list(graph.paged(url))


def list_lists(graph):
    """Every list the account sees – own, shared, and the well-known ones
    (default list, flagged e-mails). Returns [{"id", "titel", "art"}]."""
    liste = []
    for eintrag in _alle(graph, f"{GRAPH}/me/todo/lists"):
        if not eintrag.get("id"):
            continue
        liste.append({"id": eintrag["id"],
                      "titel": str(eintrag.get("displayName") or eintrag["id"]),
                      "art": str(eintrag.get("wellknownListName") or "none"),
                      "geteilt": bool(eintrag.get("isShared"))})
    liste.sort(key=lambda e: (e["art"] != "defaultList", e["titel"].lower()))
    return liste


def list_pfad(liste):
    """What the rules see: the title as a folder name, without the id
    suffix – rules are written by people, and nobody knows the suffix."""
    return export_util.safe(liste["titel"])


def list_ordner(liste):
    """The list's folder name; the id short-code keeps same-named lists
    apart."""
    return f'{list_pfad(liste)}__{export_util.kuerzel(liste["id"])}'


def list_ziel(out, liste):
    """One folder per list."""
    return Path(out) / list_ordner(liste)


def todo_regeln():
    """Which lists get exported – environment beats file. Without rules of
    your own every list comes along."""
    roh = os.environ.get("TODO_RULES")
    if roh is None:
        roh = settings.value("todo_rules", None)
    return folders.lies_regeln(roh or "")


def listen_eintraege(listen):
    """The list of lists in the shape folders.py reckons with: the path is
    what the rules match, "ordner" the folder the export really uses. The
    listing carries no task count, so every entry says 0."""
    return [{"id": e["id"], "pfad": list_pfad(e), "name": e["titel"],
             "elemente": 0, "ordner": list_ordner(e),
             "standard": e.get("art") == "defaultList"} for e in listen]


def gleiche_listen_ab(graph, out):
    """--lists: only fetch and store the list of lists, export nothing –
    the app shows the tree, the rules pick from it."""
    vorher = folders.lade(out)
    daten = folders.speichere(out, listen_eintraege(list_lists(graph)), vorher)
    gewaehlt = folders.gewaehlt(daten, todo_regeln())
    progress.event("run.sync.result", total=len(daten["ordner"]),
                   chosen=len(gewaehlt),
                   unit=progress.atom("progress.unit.lists"))
    if daten["neu"] or daten["verschwunden"] or daten["umbenannt"]:
        progress.event("run.sync.changed", new=len(daten["neu"]),
                       gone=len(daten["verschwunden"]),
                       renamed=len(daten["umbenannt"]))
    progress.ergebnis(len(daten["neu"]),
                      extra={"total": len(daten["ordner"]),
                             "chosen": len(gewaehlt),
                             "gone": len(daten["verschwunden"])})


def listenkadenzen(kadenzen):
    """Does any list carry a cadence of its own ("todo:<pfad>")?"""
    return any(str(k).startswith("todo:") for k in kadenzen)


def quelle_faellig(out):
    """The source's cadence (SYNC_CADENCE key "todo"), decided before any
    listing: a run that is not due costs no request. Once a list carries
    its own cadence the lists decide one by one (see lauf) – the listing is
    needed then, and this gate steps aside. So does "Sync now"."""
    kadenzen = export_util.kadenzen()
    if listenkadenzen(kadenzen):
        return True
    kadenz = kadenzen.get("todo") or "always"
    if export_util.einheit_faellig(state_db.StateDb(out), kadenz):
        return True
    progress.event("run.cadence.skip",
                   name=progress.atom("settings.todo.title"),
                   cadence=progress.atom(f"cadence.{kadenz}"))
    return False


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
def _dateiname(task_id, name):
    roh = export_util.safe(str(name or "datei"))
    # URL-safe as well: the name lands in the list's relative links and in
    # the /source route's path parameter.
    roh = re.sub(r"[&#%?]", "_", roh)
    return f"{export_util.kuerzel(task_id)}_{roh}"


def _anhaenge_laden(graph, liste, ziel, task):
    """The task's attachments, downloaded next to the list.

    Called only when the task itself changed; a file that will not come
    says so once in the log and the card keeps its name without a link."""
    basis = f'{GRAPH}/me/todo/lists/{liste["id"]}/tasks/{task["id"]}/attachments'
    out = []
    for a in _alle(graph, basis):
        name = str(a.get("name") or "datei")
        eintrag = {"id": a.get("id"), "name": name,
                   "size": int(a.get("size") or 0), "rel": None}
        try:
            daten, _typ = graph.get_bytes(f'{basis}/{a["id"]}/$value',
                                          label=" (Anhang)")
            rel = f"{ANHANG_DIR}/{_dateiname(task['id'], name)}"
            (ziel / ANHANG_DIR).mkdir(parents=True, exist_ok=True)
            (ziel / rel).write_bytes(daten)
            eintrag["rel"] = rel
        except auth.TokenExpired:
            raise
        except Exception as e:
            _event("run.todo.attachment_failed", "warn",
                   name=str(task.get("title") or "?")[:60],
                   error=f"{type(e).__name__}: {e}")
        out.append(eintrag)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_STIL = """
body{font-family:-apple-system,'Segoe UI',sans-serif;margin:24px;color:#222;
  background:#fafafa;max-width:1000px}
h1{font-size:22px;margin-bottom:2px}
.meta{color:#777;font-size:12px;margin:4px 0}
.lanes{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 18px}
.lanes a{font-size:13px;padding:4px 12px;border-radius:99px;background:#fff;
  border:1px solid #d8d8d8;color:#222;text-decoration:none}
.lanes a b{font-weight:600}
.lanes a span{color:#888;margin-left:4px}
details.lane{margin:10px 0}
details.lane>summary{font-size:16px;font-weight:600;cursor:pointer;
  padding:8px 4px;border-bottom:1px solid #ddd}
details.karte{background:#fff;border:1px solid #e2e2e2;border-radius:10px;
  margin:8px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
details.karte>summary{cursor:pointer;padding:10px 14px;display:block}
details.karte[open]>summary{border-bottom:1px dashed #eee}
details.karte.weg{opacity:.55;background:#f2f2f2}
details.karte.erledigt>summary .titel{text-decoration:line-through;color:#777}
.rumpf{padding:10px 14px}
.kopf{display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap}
.kopf .titel{font-size:14px;font-weight:600}
.label{font-size:11px;padding:1px 8px;border-radius:99px;background:#e8eefc;
  color:#222}
.zeile{color:#777;font-size:12px;margin-top:3px}
.notiz{font-size:13px;white-space:pre-wrap;margin:6px 0}
.notiz.html{white-space:normal}
ul.check{list-style:none;padding-left:2px;font-size:13px;margin:6px 0}
ul.check .done{text-decoration:line-through;color:#888}
.refs a{font-size:12px;margin-right:10px}
.refs span{font-size:12px;margin-right:10px;color:#777}
"""

_SKRIPT = """<script>
document.querySelectorAll('.lanes a').forEach(function(chip){
  chip.addEventListener('click', function(){
    var lane = document.getElementById(chip.getAttribute('href').slice(1));
    if(lane) lane.open = true;
  });
});
</script>"""


def _saeubere(html):
    """Note HTML straight from Exchange: keep the markup, drop the
    executable parts – the file must open harmlessly offline."""
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html or "",
                  flags=re.I | re.S)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.I | re.S)
    return re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", "", html)


def _datum(feld):
    """A To Do date – {"dateTime": …, "timeZone": …} – as its day."""
    return str((feld or {}).get("dateTime") or "")[:10]


def _wiederholung(rec):
    muster = (rec or {}).get("pattern") or {}
    art = _TAKT.get(muster.get("type") or "")
    if not art:
        return None
    n = int(muster.get("interval") or 1)
    tage = muster.get("daysOfWeek") or []
    text = art if n == 1 else f"alle {n} ({art})"
    if tage:
        text += ", " + ", ".join(str(t) for t in tage)
    return text


def _task_html(eintrag, weg=False):
    """One card: closed, only title, labels and the meta line show; the
    body – notes, steps, links, attachments – opens on click."""
    t = eintrag.get("task") or {}
    erledigt = (t.get("status") == "completed")
    kopf = [f'<span class="titel">{html_lib.escape(str(t.get("title") or "?"))}</span>']
    for cat in t.get("categories") or []:
        kopf.append(f'<span class="label">{html_lib.escape(str(cat))}</span>')
    meta = [_STATUS.get(t.get("status") or "", str(t.get("status") or ""))]
    if t.get("importance") in _WICHTIG:
        meta.append(_WICHTIG[t["importance"]])
    if _datum(t.get("dueDateTime")):
        meta.append("fällig " + _datum(t.get("dueDateTime")))
    if t.get("isReminderOn") and _datum(t.get("reminderDateTime")):
        meta.append("Erinnerung " + _datum(t.get("reminderDateTime")))
    takt = _wiederholung(t.get("recurrence"))
    if takt:
        meta.append("wiederkehrend: " + takt)
    if erledigt and _datum(t.get("completedDateTime")):
        meta.append("erledigt am " + _datum(t.get("completedDateTime")))
    if weg and eintrag.get("deleted"):
        meta.append("nicht mehr in der Liste seit " + str(eintrag["deleted"])[:10])
    klassen = "karte" + (" weg" if weg else "") + (" erledigt" if erledigt else "")
    teile = [f'<details class="{klassen}"><summary>',
             '<span class="kopf">' + " ".join(kopf) + "</span>",
             '<div class="zeile">' + " · ".join(m for m in meta if m) + "</div>",
             '</summary><div class="rumpf">']
    body = t.get("body") or {}
    inhalt = str(body.get("content") or "").strip()
    if inhalt:
        if (body.get("contentType") or "text") == "html":
            teile.append('<div class="notiz html">' + _saeubere(inhalt) + "</div>")
        else:
            teile.append('<div class="notiz">' + html_lib.escape(inhalt) + "</div>")
    schritte = t.get("checklistItems") or []
    if schritte:
        teile.append('<ul class="check">' + "".join(
            f'<li class="{"done" if s.get("isChecked") else ""}">'
            f'{"☑" if s.get("isChecked") else "☐"} '
            f'{html_lib.escape(str(s.get("displayName") or ""))}</li>'
            for s in schritte) + "</ul>")
    glieder = []
    for r in t.get("linkedResources") or []:
        name = str(r.get("displayName") or r.get("applicationName") or "Link")
        url = r.get("webUrl")
        if url:
            glieder.append(f'<a href="{html_lib.escape(str(url))}">'
                           f"{html_lib.escape(name)}</a>")
        else:
            glieder.append(f"<span>{html_lib.escape(name)}</span>")
    for a in eintrag.get("anhaenge") or []:
        name = str(a.get("name") or "Anhang")
        if a.get("rel"):
            glieder.append(f'<a href="{html_lib.escape(a["rel"])}">📎 '
                           f"{html_lib.escape(name)}</a>")
        else:
            glieder.append(f"<span>📎 {html_lib.escape(name)}</span>")
    if glieder:
        teile.append('<div class="refs">' + " ".join(glieder) + "</div>")
    teile.append("</div></details>")
    return "".join(teile)


def _sortiert_offen(eintraege):
    return sorted(eintraege, key=lambda e: (
        not _datum((e.get("task") or {}).get("dueDateTime")),
        _datum((e.get("task") or {}).get("dueDateTime")),
        str((e.get("task") or {}).get("createdDateTime") or "")))


def _sortiert_erledigt(eintraege):
    return sorted(eintraege, key=lambda e: str(
        (e.get("task") or {}).get("completedDateTime", {}).get("dateTime")
        or ""), reverse=True)


def render_list(liste, eintraege, stand=None):
    """Three collapsed lanes – open, completed, no longer in the list – as
    native <details>, no library. `stand` is the moment of the last change,
    not of this run: the file is only rewritten when something changed,
    and its timestamp says just that."""
    jetzt = stand or datetime.now(UTC).isoformat(timespec="seconds")
    lebend = [e for e in eintraege.values() if not e.get("deleted")]
    offen = _sortiert_offen(e for e in lebend
                            if (e.get("task") or {}).get("status") != "completed")
    erledigt = _sortiert_erledigt(e for e in lebend
                                  if (e.get("task") or {}).get("status") == "completed")
    weg = sorted((e for e in eintraege.values() if e.get("deleted")),
                 key=lambda e: str(e.get("deleted")), reverse=True)
    lanes = [("lane-offen", "Offen", offen, False),
             ("lane-erledigt", "Erledigt", erledigt, False)]
    if weg:
        lanes.append(("lane-weg", "Nicht mehr in der Liste", weg, True))
    chips = "".join(
        f'<a href="#{kennung}"><b>{html_lib.escape(name)}</b>'
        f"<span>{len(gruppe)}</span></a>"
        for kennung, name, gruppe, _w in lanes)
    untertitel = "geteilte Liste" if liste.get("geteilt") else "Liste"
    teile = ["<!doctype html><html><head><meta charset=\"utf-8\">"
             f"<title>{html_lib.escape(liste['titel'])}</title>"
             f"<style>{_STIL}</style></head><body>"
             f"<h1>{html_lib.escape(liste['titel'])}</h1>"
             f'<p class="meta">Microsoft To Do · {untertitel} · Stand {jetzt}</p>'
             f'<nav class="lanes">{chips}</nav>']
    for kennung, name, gruppe, weg_lane in lanes:
        offen_attr = " open" if kennung == "lane-offen" else ""
        teile.append(f'<details class="lane" id="{kennung}"{offen_attr}>'
                     f"<summary>{html_lib.escape(name)} ({len(gruppe)})"
                     "</summary>")
        teile += [_task_html(e, weg=weg_lane) for e in gruppe]
        teile.append("</details>")
    teile.append(_SKRIPT + "</body></html>")
    return "".join(teile)


# ---------------------------------------------------------------------------
# The delta feed
# ---------------------------------------------------------------------------
def _delta_lesen(graph, url):
    """One round of the list's delta feed: every page, and the deltaLink
    the last page carries for the next round."""
    tasks, link = [], None
    while url:
        d = graph.get(url)
        tasks.extend(d.get("value") or [])
        link = d.get("@odata.deltaLink") or link
        url = d.get("@odata.nextLink")
    return tasks, link


def _delta_verfallen(e):
    """Does the service no longer know the stored token? 410 Gone, or the
    4xx with syncStateNotFound the Outlook-backed feeds answer once their
    token cache has moved on."""
    antwort = getattr(e, "response", None)
    status = int(getattr(antwort, "status_code", 0) or 0)
    if status == 410:
        return True
    text = str(getattr(antwort, "text", "") or "").lower()
    return 400 <= status < 500 and ("syncstatenotfound" in text
                                    or "resyncrequired" in text)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def list_lauf(graph, out, liste):
    """One list: read the delta round, refresh what changed, mark what
    vanished, render – and write only when this run changed something."""
    ziel = list_ziel(out, liste)
    db = state_db.StateDb(ziel)
    vorher = {k: db.kv_lesen(k) or "" for k in ("list", "tasks")}
    eintraege = _json(vorher["tasks"])
    delta_key = f'delta:{liste["id"]}'
    token = db.kv_lesen(delta_key) or ""
    anfang = (f'{GRAPH}/me/todo/lists/{liste["id"]}/tasks/delta'
              "?$expand=checklistItems,linkedResources&$top=100")
    voll = not token
    try:
        tasks, link = _delta_lesen(graph, token or anfang)
    except auth.TokenExpired:
        raise
    except Exception as e:
        if voll or not _delta_verfallen(e):
            raise
        _event("run.todo.delta_reset", name=liste["titel"])
        db.kv_schreiben(delta_key, "")
        voll = True
        tasks, link = _delta_lesen(graph, anfang)
    entfernt = {t["id"] for t in tasks if t.get("id") and t.get("@removed")}
    # A task may appear more than once in one round (replays); the last
    # word counts.
    lebend = {t["id"]: t for t in tasks
              if t.get("id") and not t.get("@removed")}
    neu = unveraendert = fehler = 0
    faellig = []
    for tid, t in lebend.items():
        alt = eintraege.get(tid) or {}
        etag_neu = t.get("@odata.etag") or ""
        if alt.get("etag") == etag_neu and not alt.get("deleted"):
            unveraendert += 1
            continue
        faellig.append((t, alt, etag_neu))
    if not voll:
        # An incremental round names only what moved; the rest stands.
        unveraendert += sum(
            1 for tid, e in eintraege.items()
            if tid not in lebend and tid not in entfernt and not e.get("deleted"))
    gesamt = unveraendert + len(faellig)
    _event("run.todo.start", name=liste["titel"], n=gesamt, m=len(faellig))
    for t, alt, etag_neu in faellig:
        tid = t["id"]
        eintrag = {"etag": etag_neu, "task": t, "deleted": None,
                   "anhaenge": alt.get("anhaenge") or []}
        try:
            if t.get("hasAttachments"):
                eintrag["anhaenge"] = _anhaenge_laden(graph, liste, ziel, t)
            else:
                eintrag["anhaenge"] = []
            eintraege[tid] = eintrag
            neu += 1
        except auth.TokenExpired:
            raise
        except Exception as e:
            fehler += 1
            _event("run.todo.task_failed", "err",
                   name=str(t.get("title") or tid)[:60],
                   error=f"{type(e).__name__}: {e}")
    jetzt = datetime.now(UTC).isoformat(timespec="seconds")
    # What the feed reports as removed is gone, errors or not – an explicit
    # signal, unlike the absence a full read infers: that one needs a
    # complete, error-free read. The record stays, the card moves to the
    # greyed section.
    for tid in entfernt:
        e = eintraege.get(tid)
        if e and not e.get("deleted"):
            e["deleted"] = jetzt
    if voll and not fehler:
        for tid, e in eintraege.items():
            if tid not in lebend and not e.get("deleted"):
                e["deleted"] = jetzt
    # Written only when different from what is stored: an unchanged list
    # keeps its blobs, its file and its "Stand" line.
    nachher = {
        "list": json.dumps(
            {"id": liste["id"], "titel": liste["titel"], "art": liste["art"],
             "geteilt": liste.get("geteilt", False)}, ensure_ascii=False),
        "tasks": json.dumps(eintraege, ensure_ascii=False),
    }
    geaendert = {k: v for k, v in nachher.items() if v != vorher[k]}
    stand = db.kv_lesen("stand")
    datei = ziel / "list.html"
    if geaendert or not stand or not datei.exists():
        if geaendert or not stand:
            stand = jetzt
        for k, v in geaendert.items():
            db.kv_schreiben(k, v)
        db.kv_schreiben("stand", stand)
        export_util.schreibe_atomar(datei, render_list(liste, eintraege, stand))
    # The next round starts where this one ended – only after a clean run
    # (a failed task is otherwise delivered again) and only once the tasks
    # are on disk: a pointer ahead of its payload would lose changes.
    if not fehler and link:
        db.kv_schreiben(delta_key, link)
    _event("run.todo.list", name=liste["titel"], n=gesamt)
    db.close()
    return neu, unveraendert, fehler


def lauf(graph, out, listen, workers=1):
    """Every chosen list, side by side. The rules decide which lists come
    along (a list already on disk that they now leave out stays as it is);
    the cadence decides which of those run this time – each list follows
    its own ("todo:<pfad>"), the source's otherwise, with its stamp in the
    root's state.db. Held-back lists are one log line, never one each."""
    out = Path(out)
    regeln = todo_regeln()
    kadenzen = export_util.kadenzen()
    wurzel = state_db.StateDb(out)
    gewaehlt, ausgeschlossen, gehalten = [], 0, []
    for liste in listen:
        pfad = list_pfad(liste)
        if not folders.gilt(pfad, regeln):
            ausgeschlossen += 1
            continue
        kadenz = export_util.kadenz_fuer(kadenzen, "todo", pfad)
        if export_util.einheit_faellig(wurzel, kadenz,
                                       kv_key=f'last_sync:{liste["id"]}'):
            gewaehlt.append(liste)
        else:
            gehalten.append(kadenz)
    if gehalten and not gewaehlt:
        # Nothing due: the source line, naming the one cadence that held
        # everything back – or the source's when the lists differ.
        einzig = set(gehalten)
        kadenz = einzig.pop() if len(einzig) == 1 else \
            (kadenzen.get("todo") or "always")
        progress.event("run.cadence.skip",
                       name=progress.atom("settings.todo.title"),
                       cadence=progress.atom(f"cadence.{kadenz}"))
    elif gehalten:
        progress.event("run.todo.paced", n=len(gehalten))
    neu = unveraendert = fehler = fehl = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers or 1))) as pool:
        offen = {pool.submit(list_lauf, graph, out, liste): liste
                 for liste in gewaehlt}
        if offen:
            progress.melde(0, len(offen), "lists")
        for lfd, fut in enumerate(as_completed(offen), 1):
            liste = offen[fut]
            try:
                n, u, f = fut.result()
            except auth.TokenExpired:
                pool.shutdown(cancel_futures=True)
                raise
            except Exception as e:
                _event("run.todo.list_failed", "err", name=liste["titel"],
                       error=f"{type(e).__name__}: {e}")
                fehl += 1
            else:
                neu, unveraendert, fehler = neu + n, unveraendert + u, fehler + f
                if not f:
                    # Stamped under the same condition as the delta link:
                    # a list with a failed task is due again next time.
                    wurzel.kv_schreiben(f'last_sync:{liste["id"]}',
                                        str(time.time()))
            progress.melde(lfd, len(offen), "lists")
    # The source's stamp stays maintained: should every list override go
    # away, the source gate falls back on it cleanly. A run that held every
    # list back synced nothing and stamps nothing.
    if not (fehler or fehl) and (gewaehlt or not gehalten):
        wurzel.kv_schreiben("last_sync", str(time.time()))
    progress.ergebnis(neu, unchanged=unveraendert, excluded=ausgeschlossen,
                      errors=fehler + fehl,
                      extra={"lists": len(gewaehlt),
                             **({"skipped": len(gehalten)} if gehalten else {})})


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if export_util.hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__)
        return
    out = export_util.ausgabeordner(argv)
    nur_listen = "--lists" in sys.argv[1:]
    # The cadence question needs no Graph: a run that is not due ends
    # before the sign-in.
    if not nur_listen and not quelle_faellig(out):
        progress.ergebnis(0, extra={"lists": 0, "skipped": 1})
        return
    workers = settings.number("EXPORT_WORKERS", "workers")
    graph_client.konfiguriere(workers)
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    try:
        if nur_listen:
            gleiche_listen_ab(graph, out)
            return
        listen = list_lists(graph)
        if not listen:
            progress.event("run.todo.none", "warn")
            progress.ergebnis(0)
            return
        progress.event("run.todo.lists", n=len(listen))
        lauf(graph, out, listen, workers)
    except auth.TokenExpired:
        progress.fehler("token_expired")
        sys.exit(1)


if __name__ == "__main__":
    main()
