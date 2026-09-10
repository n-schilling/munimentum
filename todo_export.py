#!/usr/bin/env python3
"""
todo_export.py – Microsoft To Do lists as a local archive.

One folder per list, holding a standalone list.html (open and completed
tasks with their steps, due dates, reminders, notes, linked resources and
attachments) plus the list's state.db with the raw task data. The mirror
promise is the same as everywhere: the current version of the list is kept,
and a task that disappears from the list stays here, rendered into a greyed
"no longer in the list" section.

To Do offers a delta feed, but lists are small: every run lists all tasks
of a list (steps and linked resources expanded, one paged call) and
refreshes only what changed, by task etag. Absence in a clean listing is
the deletion signal. Attachments – To Do caps them at 25 MB – are fetched
next to the list whenever the task they belong to changed.

Runs as a subprogram of app.py: output folder as the only argument,
settings as environment variables (SYNC_CADENCE – see export_util).
Progress, results and failures are structured lines (progress.py).
"""

import html as html_lib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import auth
import export_util
import graph_client
import progress
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


def list_ziel(out, liste):
    """One folder per list; the id short-code keeps same-named lists apart."""
    name = f'{export_util.safe(liste["titel"])}__{export_util.kuerzel(liste["id"])}'
    return Path(out) / name


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
            progress.event("run.todo.attachment_failed", "warn",
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


def render_list(liste, eintraege):
    """Three collapsed lanes – open, completed, no longer in the list – as
    native <details>, no library."""
    jetzt = datetime.now(UTC).isoformat(timespec="seconds")
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
# The run
# ---------------------------------------------------------------------------
def list_lauf(graph, out, liste):
    """One list: list, refresh what changed, mark what vanished, render."""
    ziel = list_ziel(out, liste)
    db = state_db.StateDb(ziel)
    try:
        eintraege = json.loads(db.kv_lesen("tasks") or "{}")
    except ValueError:
        eintraege = {}

    tasks = _alle(graph, f'{GRAPH}/me/todo/lists/{liste["id"]}/tasks'
                         "?$expand=checklistItems,linkedResources&$top=100")
    neu = unveraendert = fehler = 0
    gesehen = set()
    faellig = []
    for t in tasks:
        tid = t.get("id")
        if not tid:
            continue
        gesehen.add(tid)
        alt = eintraege.get(tid) or {}
        etag_neu = t.get("@odata.etag") or ""
        if alt.get("etag") == etag_neu and not alt.get("deleted"):
            unveraendert += 1
            continue
        faellig.append((t, alt, etag_neu))
    progress.event("run.todo.start", name=liste["titel"], n=len(tasks),
                   m=len(faellig))
    if faellig:
        progress.melde(0, len(faellig), "tasks")
    for lfd, (t, alt, etag_neu) in enumerate(faellig):
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
            progress.melde(lfd + 1, len(faellig), "tasks")
        except auth.TokenExpired:
            raise
        except Exception as e:
            fehler += 1
            progress.event("run.todo.task_failed", "err",
                           name=str(t.get("title") or tid)[:60],
                           error=f"{type(e).__name__}: {e}")
    # Absence in a complete, error-free listing is the deletion signal –
    # the record stays, the card moves to the greyed section.
    if not fehler:
        jetzt = datetime.now(UTC).isoformat(timespec="seconds")
        for tid, e in eintraege.items():
            if tid not in gesehen and not e.get("deleted"):
                e["deleted"] = jetzt

    db.kv_schreiben("list", json.dumps(
        {"id": liste["id"], "titel": liste["titel"], "art": liste["art"],
         "geteilt": liste.get("geteilt", False)}, ensure_ascii=False))
    db.kv_schreiben("tasks", json.dumps(eintraege, ensure_ascii=False))
    ziel.mkdir(parents=True, exist_ok=True)
    export_util.schreibe_atomar(ziel / "list.html",
                                render_list(liste, eintraege))
    progress.event("run.todo.list", name=liste["titel"], n=len(tasks))
    return neu, unveraendert, fehler


def lauf(graph, out, listen):
    out = Path(out)
    neu = unveraendert = fehler = fehl = 0
    for liste in listen:
        try:
            n, u, f = list_lauf(graph, out, liste)
        except auth.TokenExpired:
            raise
        except Exception as e:
            progress.event("run.todo.list_failed", "err", name=liste["titel"],
                           error=f"{type(e).__name__}: {e}")
            fehl += 1
            continue
        neu, unveraendert, fehler = neu + n, unveraendert + u, fehler + f
    progress.ergebnis(neu, unchanged=unveraendert, errors=fehler + fehl,
                      extra={"lists": len(listen)})


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if export_util.hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__)
        return
    out = export_util.ausgabeordner(argv)
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    try:
        listen = list_lists(graph)
        if not listen:
            progress.event("run.todo.none", "warn")
            progress.ergebnis(0)
            return
        progress.event("run.todo.lists", n=len(listen))
        lauf(graph, out, listen)
    except auth.TokenExpired:
        progress.fehler("token_expired")
        sys.exit(1)


if __name__ == "__main__":
    main()
