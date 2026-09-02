#!/usr/bin/env python3
"""
planner_export.py – Microsoft Planner boards as a local archive.

One folder per plan, holding a standalone board.html (buckets, cards with
labels, assignees, checklists, descriptions – and the COMMENTS) plus the
plan's state.db with the raw task data. The mirror promise is the same as
everywhere: the current version of the board is kept, and a task that
disappears from the board stays here, rendered into a greyed "no longer on
the board" section.

Comments live in two worlds, and both come along:
  * legacy: posts in the owning M365 group's conversation
    (task.conversationThreadId) – read via /groups/{gid}/threads/{tid}/posts.
    Change detection is cheap: one listing of the group's threads carries
    lastDeliveredDateTime per thread.
  * new (chat-based): GET /beta/planner/tasks/{id}/messages. There is no
    change signal, so changed tasks are asked immediately and everything
    else at most once per day (a full sweep per plan).

Planner has no delta feed, but boards are small: every run lists all tasks
(one paged call) and refreshes only what changed, by task etag. Absence in
a clean listing is the deletion signal.

Runs as a subprogram of app.py: output folder as the only argument,
settings as environment variables (PLANNER_URLS – one plan URL per line;
SYNC_CADENCE/SYNC_NOW – see export_util). Progress, results and failures
are structured lines (progress.py).
"""

import base64
import html as html_lib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import auth
import export_util
import graph_client
import progress
import settings
import state_db

export_util.erzwinge_utf8()

GRAPH = graph_client.GRAPH
BETA = "https://graph.microsoft.com/beta"
RES = "https://graph.microsoft.com/"
SCOPES = [RES + "Tasks.Read", RES + "Group.Read.All", RES + "User.Read"]

OUT_ROOT = settings.value("planner_dir", settings.PLANNER_DIR)
SWEEP_S = 24 * 3600            # full new-comment sweep at most this often

# Planner's fixed label palette – the plan's details name the categories,
# the colours are Planner's own.
FARBEN = {"category1": "#e8919b", "category2": "#eb8f5b", "category3": "#edc23e",
          "category4": "#7bcf6f", "category5": "#4fc3ae", "category6": "#6fc4e8",
          "category7": "#9db6e8", "category8": "#b89ae8", "category9": "#e094d8",
          "category10": "#a8aeb8", "category11": "#8fd8b0", "category12": "#c9b98f",
          "category13": "#95a5c6", "category14": "#c695a5", "category15": "#85c6c0",
          "category16": "#c6b285", "category17": "#b0c685", "category18": "#c68585",
          "category19": "#8595c6", "category20": "#a5c695",
          "category21": "#c6a585", "category22": "#85c695", "category23": "#9585c6",
          "category24": "#c68595", "category25": "#95c6b5"}


def planner_urls():
    roh = os.environ.get("PLANNER_URLS")
    if roh is None:
        roh = settings.value("planner_urls", "") or ""
    return [z.strip() for z in str(roh).splitlines() if z.strip()]


def plan_id_aus(url):
    """The plan id from either Planner address – the new web UI
    (…/webui/v1/plan/<id>/…) or the legacy one (…planId=<id>)."""
    m = re.search(r"/plan/([A-Za-z0-9_-]{10,})", url)
    if not m:
        m = re.search(r"[?&]planId=([A-Za-z0-9_-]{10,})", url)
    return m.group(1) if m else None


def anhaenge_laden():
    """Download the files a task references? The boards' libraries are
    typically never mirrored on their own – opt-in, off by default."""
    return settings.flag("PLANNER_ATTACHMENTS", "planner_attachments")


class Graph(graph_client.Graph):
    def __init__(self, nur_still=False):
        scopes = list(SCOPES)
        if anhaenge_laden():
            scopes.append(RES + "Files.Read.All")
        super().__init__(scopes, nur_still=nur_still)


class TokenClient(graph_client.TokenClient):
    pass


# ---------------------------------------------------------------------------
# Auflösen: URL -> Plan samt Kadenz
# ---------------------------------------------------------------------------
def resolve_plans(graph, urls):
    """[(plan, kadenz)] for the configured URLs; broken URLs cost the others
    nothing."""
    kadenz_map = export_util.kadenzen()
    plaene, fehl, gesehen = [], 0, {}
    for url in urls:
        pid = plan_id_aus(url)
        if not pid:
            progress.event("run.planner.bad_url", "err", url=url)
            fehl += 1
            continue
        kadenz = kadenz_map.get(f"planner-url:{url}") or "always"
        if pid in gesehen:
            gesehen[pid]["kadenz"] = export_util.haeufigere(
                gesehen[pid]["kadenz"], kadenz)
            continue
        try:
            plan = graph.get(f"{GRAPH}/planner/plans/{pid}")
        except auth.TokenExpired:
            raise
        except Exception as e:
            progress.event("run.planner.plan_failed", "err", url=url,
                           error=f"{type(e).__name__}: {e}")
            fehl += 1
            continue
        container = plan.get("container") or {}
        eintrag = {"id": pid, "titel": str(plan.get("title") or pid),
                   "gruppe": (container.get("containerId")
                              if container.get("type", "").lower() == "group"
                              else plan.get("owner")),
                   "kadenz": kadenz}
        gesehen[pid] = eintrag
        plaene.append(eintrag)
    return plaene, fehl


def plan_ziel(out, plan):
    """One folder per plan; the id short-code keeps same-titled plans apart."""
    name = f'{export_util.safe(plan["titel"])}__{export_util.kuerzel(plan["id"])}'
    return Path(out) / name


# ---------------------------------------------------------------------------
# Holen
# ---------------------------------------------------------------------------
def _alle(graph, url):
    return list(graph.paged(url))


def _namen(graph, db, ids):
    """user id -> display name, cached in the plan's state.db forever –
    names hardly change, and the cache spares one request per person."""
    try:
        cache = json.loads(db.kv_lesen("namen") or "{}")
    except ValueError:
        cache = {}
    neu = False
    for kennung in ids:
        if not kennung or kennung in cache:
            continue
        try:
            u = graph.get(f"{GRAPH}/users/{kennung}?$select=displayName")
            cache[kennung] = str(u.get("displayName") or kennung)
        except auth.TokenExpired:
            raise
        except Exception:
            cache[kennung] = kennung
        neu = True
    if neu:
        db.kv_schreiben("namen", json.dumps(cache, ensure_ascii=False))
    return cache


ANHANG_DIR = "Anhaenge"


def _referenzen_laden(graph, db, ziel, task, det):
    """The task's referenced files, downloaded next to the board.

    Returns {url: rel} for the cards to link locally. Refreshed by the
    driveItem cTag whenever the task itself is refreshed; a file that will
    not come (gone, no permission, not a drive item) keeps its cloud link
    and says so once in the log."""
    try:
        stand = json.loads(db.kv_lesen("anhaenge") or "{}")
    except ValueError:
        stand = {}
    lokal = {}
    for roh in (det.get("references") or {}):
        url = unquote(roh)
        token = base64.urlsafe_b64encode(url.encode("utf-8")).decode().rstrip("=")
        alt = stand.get(url) or {}
        try:
            meta = graph.get(f"{GRAPH}/shares/u!{token}/driveItem"
                             "?$select=name,cTag")
            roh_name = export_util.safe(str(meta.get("name") or "datei"))
            # Zusätzlich URL-tauglich: der Name steht im relativen Link des
            # Boards und im path-Parameter der /source-Route.
            roh_name = re.sub(r"[&#%?]", "_", roh_name)
            stamm, punkt, endung = roh_name.rpartition(".")
            kurz = export_util.kuerzel(url)
            # Das URL-Kürzel im Namen: zwei gleichnamige Dateien aus zwei
            # Referenzen dürfen sich nicht überschreiben.
            name = f"{stamm}__{kurz}.{endung}" if punkt else \
                f"{roh_name}__{kurz}"
            rel = f"{ANHANG_DIR}/{name}"
            if alt.get("ctag") == (meta.get("cTag") or "") and                     (ziel / rel).exists():
                lokal[url] = rel
                continue
            daten, _typ = graph.get_bytes(
                f"{GRAPH}/shares/u!{token}/driveItem/content",
                label=" (Anhang)")
            (ziel / ANHANG_DIR).mkdir(parents=True, exist_ok=True)
            (ziel / rel).write_bytes(daten)
            stand[url] = {"rel": rel, "ctag": meta.get("cTag") or ""}
            lokal[url] = rel
        except auth.TokenExpired:
            raise
        except Exception as e:
            progress.event("run.planner.ref_failed", "warn",
                           name=str(task.get("title") or "?")[:60],
                           error=f"{type(e).__name__}: {e}")
    db.kv_schreiben("anhaenge", json.dumps(stand, ensure_ascii=False))
    return lokal


def _saeubere(html):
    """Comment HTML straight from Exchange/Planner: keep the markup, drop
    the executable parts – the file must open harmlessly offline."""
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html or "",
                  flags=re.I | re.S)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.I | re.S)
    return re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", "", html)


def _legacy_posts(graph, gruppe, thread):
    posts = _alle(graph, f"{GRAPH}/groups/{gruppe}/threads/{thread}/posts")
    out = []
    for p in posts:
        wer = ((p.get("from") or {}).get("emailAddress") or {})
        out.append({"art": "legacy", "wer": str(wer.get("name")
                                                or wer.get("address") or "?"),
                    "wann": p.get("receivedDateTime") or "",
                    "html": _saeubere(((p.get("body") or {}).get("content"))
                                      or "")})
    return out


def _neue_kommentare(graph, task_id):
    """The chat-based comments; 404 with "no chat thread" simply means none.
    Returns None when the endpoint refused for another reason."""
    try:
        d = graph.get(f"{BETA}/planner/tasks/{task_id}/messages")
    except auth.TokenExpired:
        raise
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", 0)
        if status == 404:
            return []
        return None
    out = []
    for m in d.get("value", []):
        if m.get("deletedDateTime"):
            continue
        out.append({"art": "neu",
                    "wer": ((m.get("createdBy") or {}).get("user")
                            or {}).get("id") or "?",
                    "wann": m.get("createdDateTime") or "",
                    "html": _saeubere(m.get("content") or "")})
    return out


# ---------------------------------------------------------------------------
# Rendern
# ---------------------------------------------------------------------------
_PRIO = {1: "urgent", 3: "important", 5: "medium", 9: "low"}

_STIL = """
body{font-family:-apple-system,'Segoe UI',sans-serif;margin:24px;color:#222;
  background:#fafafa;max-width:1100px}
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
.rumpf{padding:10px 14px}
.kopf{display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap}
.kopf b{font-size:14px}
.label{font-size:11px;padding:1px 8px;border-radius:99px;color:#222}
.zeile{color:#777;font-size:12px;margin-top:3px}
.beschreibung{font-size:13px;white-space:pre-wrap;margin:6px 0}
ul.check{list-style:none;padding-left:2px;font-size:13px;margin:6px 0}
ul.check .done{text-decoration:line-through;color:#888}
details.kommentare{border-top:1px dashed #ddd;margin-top:8px;padding-top:4px}
details.kommentare>summary{cursor:pointer;font-size:12.5px;color:#555;
  padding:4px 0}
.kommentar{font-size:13px;margin:6px 0}
.kommentar .wer{font-weight:600}
.kommentar .wann{color:#999;font-size:11px;margin-left:6px}
.refs a{font-size:12px;margin-right:10px}
"""

# Die Chips oben sollen die Lane nicht nur anspringen, sondern aufklappen.
_SKRIPT = """<script>
document.querySelectorAll('.lanes a').forEach(function(chip){
  chip.addEventListener('click', function(){
    var lane = document.getElementById(chip.getAttribute('href').slice(1));
    if(lane) lane.open = true;
  });
});
</script>"""


def _task_html(eintrag, labels, namen, weg=False):
    """One card: closed, only title, labels and the one meta line show;
    the body opens on click, the comments need a second one."""
    t, det = eintrag.get("task") or {}, eintrag.get("details") or {}
    kopf = [f"<b>{html_lib.escape(str(t.get('title') or '?'))}</b>"]
    for cat in sorted(t.get("appliedCategories") or {}):
        name = labels.get(cat) or cat
        kopf.append(f'<span class="label" style="background:'
                    f'{FARBEN.get(cat, "#ddd")}">{html_lib.escape(name)}</span>')
    meta = []
    zu = [namen.get(k, k) for k in (t.get("assignments") or {})]
    if zu:
        meta.append(", ".join(html_lib.escape(n) for n in sorted(zu)))
    p = t.get("percentComplete") or 0
    meta.append({0: "offen", 50: "in Arbeit", 100: "erledigt"}.get(p, f"{p}%"))
    if t.get("priority") in _PRIO:
        meta.append(_PRIO[t["priority"]])
    if t.get("dueDateTime"):
        meta.append("fällig " + str(t["dueDateTime"])[:10])
    if weg and eintrag.get("deleted"):
        meta.append("nicht mehr im Board seit " + str(eintrag["deleted"])[:10])
    kommentare = eintrag.get("kommentare") or []
    if kommentare:
        meta.append(f"{len(kommentare)} Kommentar"
                    + ("e" if len(kommentare) != 1 else ""))
    teile = ['<details class="karte%s"><summary>' % (" weg" if weg else ""),
             '<span class="kopf">' + " ".join(kopf) + "</span>",
             '<div class="zeile">' + " · ".join(meta) + "</div>",
             '</summary><div class="rumpf">']
    if det.get("description"):
        teile.append('<div class="beschreibung">'
                     + html_lib.escape(str(det["description"])) + "</div>")
    punkte = sorted((det.get("checklist") or {}).values(),
                    key=lambda c: str(c.get("orderHint") or ""))
    if punkte:
        teile.append('<ul class="check">' + "".join(
            f'<li class="{"done" if c.get("isChecked") else ""}">'
            f'{"☑" if c.get("isChecked") else "☐"} '
            f'{html_lib.escape(str(c.get("title") or ""))}</li>'
            for c in punkte) + "</ul>")
    refs = det.get("references") or {}
    if refs:
        lokal = eintrag.get("anhaenge") or {}
        glieder = []
        for roh, ref in refs.items():
            url = unquote(roh)                  # Graph kodiert die Schlüssel
            ziel_url = lokal.get(url, url)
            glieder.append(
                f'<a href="{html_lib.escape(ziel_url)}">'
                f'{html_lib.escape(str((ref or {}).get("alias") or "Link"))}'
                "</a>")
        teile.append('<div class="refs">' + " ".join(glieder) + "</div>")
    if kommentare:
        teile.append(
            '<details class="kommentare"><summary>Kommentare ('
            + str(len(kommentare)) + ")</summary>" + "".join(
                '<div class="kommentar"><span class="wer">'
                + html_lib.escape(namen.get(k["wer"], k["wer"])) + "</span>"
                + f'<span class="wann">{html_lib.escape(str(k["wann"])[:16])}'
                  "</span>"
                + f'<div>{k["html"]}</div></div>'
                for k in sorted(kommentare, key=lambda k: k["wann"]))
            + "</details>")
    teile.append("</div></details>")
    return "".join(teile)


def render_board(plan, buckets, eintraege, labels, namen):
    """Three collapsed levels: the chip row up top says which swimlanes the
    board has, a lane opens into its task list, a task into its body, the
    comments into their thread – native <details>, no library."""
    jetzt = datetime.now(UTC).isoformat(timespec="seconds")
    lebend = [e for e in eintraege.values() if not e.get("deleted")]
    reihen = sorted(buckets.values(), key=lambda b: str(b.get("orderHint") or ""))
    lanes = []
    for i, b in enumerate(reihen):
        im_bucket = sorted(
            (e for e in lebend
             if (e.get("task") or {}).get("bucketId") == b["id"]),
            key=lambda e: str((e.get("task") or {}).get("orderHint") or ""))
        if im_bucket:
            lanes.append((f"lane-{i}", str(b.get("name") or "?"), im_bucket,
                          False))
    ohne = [e for e in lebend
            if (e.get("task") or {}).get("bucketId") not in
            {b["id"] for b in reihen}]
    if ohne:
        lanes.append(("lane-ohne", "Ohne Bucket", ohne, False))
    weg = sorted((e for e in eintraege.values() if e.get("deleted")),
                 key=lambda e: str(e.get("deleted")), reverse=True)
    if weg:
        lanes.append(("lane-weg", "Nicht mehr im Board", weg, True))

    chips = "".join(
        f'<a href="#{kennung}"><b>{html_lib.escape(name)}</b>'
        f"<span>{len(gruppe)}</span></a>"
        for kennung, name, gruppe, _w in lanes)
    teile = ["<!doctype html><html><head><meta charset=\"utf-8\">"
             f"<title>{html_lib.escape(plan['titel'])}</title>"
             f"<style>{_STIL}</style></head><body>"
             f"<h1>{html_lib.escape(plan['titel'])}</h1>"
             f'<p class="meta">Stand {jetzt}</p>'
             f'<nav class="lanes">{chips}</nav>']
    for kennung, name, gruppe, weg_lane in lanes:
        teile.append(f'<details class="lane" id="{kennung}">'
                     f"<summary>{html_lib.escape(name)} ({len(gruppe)})"
                     "</summary>")
        teile += [_task_html(e, labels, namen, weg=weg_lane) for e in gruppe]
        teile.append("</details>")
    teile.append(_SKRIPT + "</body></html>")
    return "".join(teile)


# ---------------------------------------------------------------------------
# Der Lauf
# ---------------------------------------------------------------------------
def plan_lauf(graph, out, plan, threads_cache):
    """One plan: list, refresh what changed, mark what vanished, render."""
    ziel = plan_ziel(out, plan)
    db = state_db.StateDb(ziel)
    try:
        eintraege = json.loads(db.kv_lesen("tasks") or "{}")
    except ValueError:
        eintraege = {}

    details = graph.get(f"{GRAPH}/planner/plans/{plan['id']}/details")
    labels = {k: v for k, v in
              (details.get("categoryDescriptions") or {}).items() if v}
    buckets = {b["id"]: b for b in
               _alle(graph, f"{GRAPH}/planner/plans/{plan['id']}/buckets")}
    tasks = _alle(graph, f"{GRAPH}/planner/plans/{plan['id']}/tasks")

    gruppe = plan.get("gruppe")
    try:
        stand_threads = json.loads(db.kv_lesen("threads") or "{}")
    except ValueError:
        stand_threads = {}
    # Which legacy threads moved since last time – ONE listing per group.
    # NOT on the first comment sync: there every post is fetched anyway, and
    # the listing walks the group's ENTIRE conversation store (Teams posts
    # included) at Graph's tiny page size – on a big group that is minutes
    # of silence for nothing.
    erste = not stand_threads
    if gruppe and not erste and gruppe not in threads_cache:
        progress.event("run.planner.threads", name=plan["titel"])
        try:
            threads_cache[gruppe] = {
                th["id"]: th.get("lastDeliveredDateTime") or ""
                for th in _alle(graph,
                                f"{GRAPH}/groups/{gruppe}/threads?$top=100")}
        except auth.TokenExpired:
            raise
        except Exception as e:
            threads_cache[gruppe] = None
            progress.event("run.planner.comments_failed", "warn",
                           name=plan["titel"],
                           error=f"{type(e).__name__}: {e}")
    threads = threads_cache.get(gruppe)

    sweep = export_util.sync_jetzt() or \
        (time.time() - float(db.kv_lesen("sweep") or 0)) > SWEEP_S
    neu = unveraendert = fehler = 0
    gesehen = set()
    # Erst entscheiden, dann arbeiten: so kennt der Fortschrittsbalken sein
    # Ziel, und die Startzeile sagt, wie viel dieser Lauf wirklich vorhat –
    # beim Erstlauf sind das 2–3 Anfragen je Aufgabe, minutenlang.
    faellig = []
    for t in tasks:
        tid = t.get("id")
        if not tid:
            continue
        gesehen.add(tid)
        alt = eintraege.get(tid) or {}
        etag_neu = t.get("@odata.etag") or ""
        geaendert = alt.get("etag") != etag_neu or alt.get("deleted")
        thread = t.get("conversationThreadId")
        legacy_neu = bool(thread and (erste or (
            threads is not None and threads.get(thread, "") !=
            stand_threads.get(thread, ""))))
        if not (geaendert or legacy_neu or sweep):
            unveraendert += 1
            continue
        faellig.append((t, alt, etag_neu, geaendert, thread))
    progress.event("run.planner.start", name=plan["titel"], n=len(tasks),
                   m=len(faellig))
    if faellig:
        progress.melde(0, len(faellig), "tasks")
    for lfd, (t, alt, etag_neu, geaendert, thread) in enumerate(faellig):
        tid = t["id"]
        eintrag = {"etag": etag_neu, "task": t, "deleted": None,
                   "details": alt.get("details"),
                   "anhaenge": alt.get("anhaenge") or {},
                   "kommentare": alt.get("kommentare") or []}
        try:
            if geaendert or not eintrag["details"]:
                eintrag["details"] = graph.get(
                    f"{GRAPH}/planner/tasks/{tid}/details")
                if anhaenge_laden():
                    eintrag["anhaenge"] = _referenzen_laden(
                        graph, db, ziel, t, eintrag["details"])
                else:
                    eintrag["anhaenge"] = alt.get("anhaenge") or {}
            kommentare = []
            if thread and gruppe and (erste or threads is not None):
                kommentare += _legacy_posts(graph, gruppe, thread)
                # Ohne Auflistung (Erstlauf) datiert der letzte Post den
                # Faden – lastDeliveredDateTime der Gruppe ist genau das.
                stand_threads[thread] = (
                    threads.get(thread, "") if threads is not None else
                    max((k["wann"] for k in kommentare
                         if k["art"] == "legacy"), default=""))
            elif thread:
                kommentare += [k for k in eintrag["kommentare"]
                               if k["art"] == "legacy"]
            neue = _neue_kommentare(graph, tid) if (geaendert or sweep) \
                else None
            kommentare += (neue if neue is not None else
                           [k for k in eintrag["kommentare"]
                            if k["art"] == "neu"])
            eintrag["kommentare"] = kommentare
            eintraege[tid] = eintrag
            neu += 1
            progress.melde(lfd + 1, len(faellig), "tasks")
        except auth.TokenExpired:
            raise
        except Exception as e:
            fehler += 1
            progress.event("run.planner.task_failed", "err",
                           name=str(t.get("title") or tid)[:60],
                           error=f"{type(e).__name__}: {e}")
    # Absence in a complete, error-free listing is the deletion signal –
    # the record stays, the card moves to the greyed section.
    if not fehler:
        jetzt = datetime.now(UTC).isoformat(timespec="seconds")
        for tid, e in eintraege.items():
            if tid not in gesehen and not e.get("deleted"):
                e["deleted"] = jetzt
    if sweep and not fehler:
        db.kv_schreiben("sweep", str(time.time()))

    kennungen = set()
    for e in eintraege.values():
        kennungen |= set((e.get("task") or {}).get("assignments") or {})
        kennungen |= {k["wer"] for k in e.get("kommentare") or []
                      if k["art"] == "neu"}
    namen = _namen(graph, db, kennungen)

    db.kv_schreiben("plan", json.dumps(
        {"id": plan["id"], "titel": plan["titel"], "labels": labels,
         "buckets": {b["id"]: str(b.get("name") or "") for b in
                     buckets.values()}}, ensure_ascii=False))
    db.kv_schreiben("tasks", json.dumps(eintraege, ensure_ascii=False))
    db.kv_schreiben("threads", json.dumps(stand_threads, ensure_ascii=False))
    ziel.mkdir(parents=True, exist_ok=True)
    export_util.schreibe_atomar(
        ziel / "board.html",
        render_board(plan, buckets, eintraege, labels, namen))
    progress.event("run.planner.plan", name=plan["titel"], n=len(tasks))
    return neu, unveraendert, fehler


def lauf(graph, out, plaene, fehl=0):
    out = Path(out)
    neu = unveraendert = fehler = uebersprungen = 0
    threads_cache = {}
    for plan in plaene:
        db = state_db.StateDb(plan_ziel(out, plan))
        kadenz = plan.get("kadenz") or "always"
        if not export_util.einheit_faellig(db, kadenz):
            uebersprungen += 1
            progress.event("run.cadence.skip", name=plan["titel"],
                           cadence=progress.atom(f"cadence.{kadenz}"))
            continue
        try:
            n, u, f = plan_lauf(graph, out, plan, threads_cache)
        except auth.TokenExpired:
            raise
        except Exception as e:
            progress.event("run.planner.plan_failed", "err",
                           url=plan["titel"],
                           error=f"{type(e).__name__}: {e}")
            fehl += 1
            continue
        neu, unveraendert, fehler = neu + n, unveraendert + u, fehler + f
        if not f:
            db.kv_schreiben("last_sync",
                            str(datetime.now(UTC).timestamp()))
    progress.ergebnis(neu, unchanged=unveraendert, errors=fehler + fehl,
                      extra={"plans": len(plaene),
                             **({"skipped": uebersprungen}
                                if uebersprungen else {})})


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if export_util.hilfe_gewuenscht(sys.argv[1:]):
        print(__doc__)
        return
    out = Path(argv[0]) if argv else Path(OUT_ROOT)
    urls = planner_urls()
    if not urls:
        progress.event("run.planner.none", "warn")
        progress.ergebnis(0)
        return
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    try:
        plaene, fehl = resolve_plans(graph, urls)
        lauf(graph, out, plaene, fehl)
    except auth.TokenExpired:
        progress.fehler("token_expired")
        sys.exit(1)


if __name__ == "__main__":
    main()
