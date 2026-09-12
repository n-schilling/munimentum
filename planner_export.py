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
    else at most every PLANNER_SWEEP_HOURS (a full sweep per plan; 0 turns
    the sweep off).

Planner has no delta feed, but boards are small: every run lists all tasks
(one paged call) and refreshes only what changed, by task etag – the
refreshes run side by side (EXPORT_WORKERS). Absence in a clean listing is
the deletion signal. A board whose cadence is not due costs nothing at all:
its folder on disk answers the question before the plan is fetched. The
board file and the state blobs are written only when the run changed
something – an unchanged board keeps its file, bytes and timestamp.

Runs as a subprogram of app.py: output folder as the only argument,
settings as environment variables (PLANNER_URLS – one plan URL per line;
SYNC_CADENCE/SYNC_NOW – see export_util; FULL_SYNC – no task's etag or
reference cTag counts, every card and file is fetched again, see
export_util.voll_neu; PLANNER_SWEEP_HOURS – see above;
PLANNER_ATTACHMENTS; PLANNER_LEGACY_SYNC – read the legacy comment threads
again, see plan_lauf; EXPORT_WORKERS). Progress, results and failures are
structured lines (progress.py).
"""

import base64
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

# Log lines may come from a worker (a reference that would not download):
# one at a time, so the app's parser always gets whole lines.
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


def sweep_stunden():
    """How often the chat comments of otherwise untouched tasks are re-read
    (PLANNER_SWEEP_HOURS): the chat endpoint has no change signal, so the
    sweep is the only way to catch a comment on a task nobody edited.
    0 means never – the board then only follows task changes."""
    return settings.number("PLANNER_SWEEP_HOURS", "planner_sweep_hours", low=0)


def legacy_sync_erzwungen():
    """The "Read legacy comments again" button: this run lists the group
    conversation once more – in case someone still replied from Outlook –
    and ignores the boards' cadence, being the user's explicit wish."""
    return bool((os.environ.get("PLANNER_LEGACY_SYNC") or "").strip())


class Graph(graph_client.Graph):
    def __init__(self, nur_still=False):
        scopes = list(SCOPES)
        if anhaenge_laden():
            scopes.append(RES + "Files.Read.All")
        super().__init__(scopes, nur_still=nur_still)


class TokenClient(graph_client.TokenClient):
    pass


# ---------------------------------------------------------------------------
# Resolving: URL -> plan with its cadence
# ---------------------------------------------------------------------------
def plan_ordner(out, pid):
    """The folder a plan already has on disk, found by the id short-code
    its name ends with – so the cadence can be decided before the plan is
    fetched (the title, the name's other half, is not known yet). A plan
    renamed since its last run has two candidates; the one synced last
    wins. None on a first run."""
    if out is None:
        return None
    endung = f"__{export_util.kuerzel(pid)}"
    try:
        kandidaten = sorted(p for p in Path(out).iterdir()
                            if p.is_dir() and p.name.endswith(endung))
    except OSError:
        return None
    beste, juengst = None, -1.0
    for p in kandidaten:
        try:
            stand = float(state_db.StateDb(p).kv_lesen("last_sync") or 0)
        except ValueError:
            stand = 0.0
        if stand > juengst:
            beste, juengst = p, stand
    return beste


def _gemerkter_titel(ordner, pid):
    """The plan's title as its last run stored it – for the skip line of a
    board that is not fetched this time."""
    plan = _json(state_db.StateDb(ordner).kv_lesen("plan"))
    rest = ordner.name[:-len(f"__{export_util.kuerzel(pid)}")]
    return str(plan.get("titel") or rest or pid)


def resolve_plans(graph, urls, out=None):
    """The configured plans with their cadence, plus the number of broken
    URLs (they cost the others nothing). The cadence is decided BEFORE a
    plan is fetched: a board whose folder on disk says "not due" is handed
    on unfetched, with that folder and its stored title, so lauf() can say
    why it skips – without a single request for it."""
    kadenz_map = export_util.kadenzen()
    kadenz_je, url_je, fehl = {}, {}, 0
    for url in urls:
        pid = plan_id_aus(url)
        if not pid:
            progress.event("run.planner.bad_url", "err", url=url)
            fehl += 1
            continue
        kadenz = kadenz_map.get(f"planner-url:{url}") or "always"
        if pid in kadenz_je:
            kadenz = export_util.haeufigere(kadenz_je[pid], kadenz)
        else:
            url_je[pid] = url
        kadenz_je[pid] = kadenz
    plaene = []
    for pid, kadenz in kadenz_je.items():
        ordner = plan_ordner(out, pid)
        if ordner is not None and not legacy_sync_erzwungen() and \
                not export_util.einheit_faellig(state_db.StateDb(ordner), kadenz):
            plaene.append({"id": pid, "titel": _gemerkter_titel(ordner, pid),
                           "gruppe": None, "kadenz": kadenz,
                           "ordner": ordner.name})
            continue
        try:
            plan = graph.get(f"{GRAPH}/planner/plans/{pid}")
        except auth.TokenExpired:
            raise
        except Exception as e:
            progress.event("run.planner.plan_failed", "err", url=url_je[pid],
                           error=f"{type(e).__name__}: {e}")
            fehl += 1
            continue
        container = plan.get("container") or {}
        plaene.append({"id": pid, "titel": str(plan.get("title") or pid),
                       "gruppe": (container.get("containerId")
                                  if container.get("type", "").lower() == "group"
                                  else plan.get("owner")),
                       "kadenz": kadenz})
    return plaene, fehl


def plan_ziel(out, plan):
    """One folder per plan; the id short-code keeps same-titled plans apart.
    A plan that arrives with its folder (known from disk, not fetched)
    keeps it."""
    name = plan.get("ordner") or \
        f'{export_util.safe(plan["titel"])}__{export_util.kuerzel(plan["id"])}'
    return Path(out) / name


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _alle(graph, url):
    return list(graph.paged(url))


def _namen(graph, wurzel, ids, bekannt=None):
    """user id -> display name for the given ids, cached forever in the
    Planner root's state.db (records area "namen") – the same people sit on
    several boards, and names hardly change. The ids nobody knows yet go
    out as one JSON batch instead of one request per person. `bekannt` is
    a plan's own cache from before 9.0: taken over, not asked again."""
    cache = {}
    for kennung, roh in wurzel.saetze_lesen("namen").items():
        try:
            cache[kennung] = str(json.loads(roh))
        except ValueError:
            cache[kennung] = str(roh)
    ids = sorted(k for k in ids if k)
    neu = {k: str(bekannt[k]) for k in ids
           if k not in cache and (bekannt or {}).get(k) and bekannt[k] != k}
    offen = [k for k in ids if k not in cache and k not in neu]
    if offen:
        urls = {f"{GRAPH}/users/{k}?$select=displayName": k for k in offen}
        try:
            antworten = graph.batch_get(list(urls))
        except auth.TokenExpired:
            raise
        except Exception as e:
            _event("run.planner.names_failed", "warn", n=len(offen),
                   error=f"{type(e).__name__}: {e}")
            antworten = {}
        for url, k in urls.items():
            status, body = antworten.get(url, (0, None))
            if status == 200 and isinstance(body, dict):
                neu[k] = str(body.get("displayName") or k)
            elif status == 404:
                # Gone: the id stands in for the name and stays cached,
                # nobody asks a deleted user twice. Anything else – a
                # missing permission, a batch that failed – is asked again
                # next run, so a permission granted later heals the cards.
                neu[k] = k
    if neu:
        wurzel.saetze_schreiben("namen", {k: json.dumps(v, ensure_ascii=False)
                                          for k, v in neu.items()})
        cache.update(neu)
    return {k: cache.get(k, k) for k in ids}


ANHANG_DIR = "Anhaenge"


def _referenzen_laden(graph, stand, ziel, task, det):
    """The task's referenced files, downloaded next to the board.

    Returns ({url: rel}, {url: state}) – the local links for the card and
    the state entries the caller merges and stores once per run; `stand` is
    the stored state, read only (this runs in a worker). Refreshed by the
    driveItem cTag whenever the task itself is refreshed; a file that will
    not come (gone, no permission, not a drive item) keeps its cloud link
    and says so once in the log."""
    lokal, neu = {}, {}
    for roh in (det.get("references") or {}):
        url = unquote(roh)
        token = base64.urlsafe_b64encode(url.encode("utf-8")).decode().rstrip("=")
        alt = stand.get(url) or {}
        try:
            meta = graph.get(f"{GRAPH}/shares/u!{token}/driveItem"
                             "?$select=name,cTag")
            roh_name = export_util.safe(str(meta.get("name") or "datei"))
            # Additionally URL-safe: the name appears in the board's
            # relative link and in the /source route's path parameter.
            roh_name = re.sub(r"[&#%?]", "_", roh_name)
            stamm, punkt, endung = roh_name.rpartition(".")
            kurz = export_util.kuerzel(url)
            # The URL tag in the name: two same-named files from two
            # references must not overwrite each other.
            name = f"{stamm}__{kurz}.{endung}" if punkt else \
                f"{roh_name}__{kurz}"
            rel = f"{ANHANG_DIR}/{name}"
            if alt.get("ctag") == (meta.get("cTag") or "") and \
                    (ziel / rel).exists():
                lokal[url] = rel
                continue
            daten, _typ = graph.get_bytes(
                f"{GRAPH}/shares/u!{token}/driveItem/content",
                label=" (Anhang)")
            (ziel / ANHANG_DIR).mkdir(parents=True, exist_ok=True)
            # Two cards may reference one file and refresh side by side:
            # each writer finishes its own copy, the last replace wins –
            # never a half file under the shared name.
            tmp = (ziel / rel).with_name(f"{name}.{threading.get_ident()}.tmp")
            tmp.write_bytes(daten)
            tmp.replace(ziel / rel)
            neu[url] = {"rel": rel, "ctag": meta.get("cTag") or ""}
            lokal[url] = rel
        except auth.TokenExpired:
            raise
        except Exception as e:
            _event("run.planner.ref_failed", "warn",
                   name=str(task.get("title") or "?")[:60],
                   error=f"{type(e).__name__}: {e}")
    return lokal, neu


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
# Rendering
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

# The chips up top should not just jump to the lane but open it as well.
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
            url = unquote(roh)                  # Graph encodes the keys
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


def render_board(plan, buckets, eintraege, labels, namen, stand=None):
    """Three collapsed levels: the chip row up top says which swimlanes the
    board has, a lane opens into its task list, a task into its body, the
    comments into their thread – native <details>, no library. `stand` is
    the moment of the last change, not of this run: the file is only
    rewritten when something changed, and its timestamp says just that."""
    jetzt = stand or datetime.now(UTC).isoformat(timespec="seconds")
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
# The run
# ---------------------------------------------------------------------------
def _task_auffrischen(graph, ziel, plan, t, alt, geaendert, legacy_holen,
                      threads, sweep, anhang_stand):
    """One task's refresh – runs in a worker and touches no shared state.
    Returns (record, thread mark, attachment state): the new record, the
    legacy thread's (id, delivered-at) or None, and the attachment state
    entries the caller merges."""
    tid = t["id"]
    thread = t.get("conversationThreadId")
    # Same etag, same task: the stored copy stays, so a sweep that finds
    # nothing new leaves the record byte for byte as it was.
    eintrag = {"etag": t.get("@odata.etag") or "",
               "task": t if geaendert else (alt.get("task") or t),
               "deleted": None,
               "details": alt.get("details"),
               "anhaenge": alt.get("anhaenge") or {},
               "kommentare": alt.get("kommentare") or []}
    anhang_neu = {}
    if geaendert or not eintrag["details"]:
        eintrag["details"] = graph.get(f"{GRAPH}/planner/tasks/{tid}/details")
        if anhaenge_laden():
            eintrag["anhaenge"], anhang_neu = _referenzen_laden(
                graph, anhang_stand, ziel, t, eintrag["details"])
        else:
            eintrag["anhaenge"] = alt.get("anhaenge") or {}
    kommentare, faden = [], None
    if legacy_holen:
        kommentare += _legacy_posts(graph, plan.get("gruppe"), thread)
        # Without a listing the latest post dates the thread – the group's
        # lastDeliveredDateTime is exactly that.
        faden = (thread, threads.get(thread, "") if threads is not None else
                 max((k["wann"] for k in kommentare if k["art"] == "legacy"),
                     default=""))
    elif thread:
        kommentare += [k for k in eintrag["kommentare"] if k["art"] == "legacy"]
    neue = _neue_kommentare(graph, tid) if (geaendert or sweep) else None
    kommentare += (neue if neue is not None else
                   [k for k in eintrag["kommentare"] if k["art"] == "neu"])
    eintrag["kommentare"] = kommentare
    return eintrag, faden, anhang_neu


def plan_lauf(graph, out, plan, threads_cache, workers=1):
    """One plan: list, refresh what changed, mark what vanished, render –
    and write only when this run changed something."""
    ziel = plan_ziel(out, plan)
    db = state_db.StateDb(ziel)
    vorher = {k: db.kv_lesen(k) or ""
              for k in ("plan", "tasks", "threads", "namen", "anhaenge")}
    eintraege = _json(vorher["tasks"])
    alles = export_util.voll_neu()
    if alles:
        # "Force full sync": no task's etag counts – every card is fetched
        # again, its referenced files with it.
        for e in eintraege.values():
            e["etag"] = ""

    details = graph.get(f"{GRAPH}/planner/plans/{plan['id']}/details")
    labels = {k: v for k, v in
              (details.get("categoryDescriptions") or {}).items() if v}
    buckets = {b["id"]: b for b in
               _alle(graph, f"{GRAPH}/planner/plans/{plan['id']}/buckets")}
    tasks = _alle(graph, f"{GRAPH}/planner/plans/{plan['id']}/tasks")

    gruppe = plan.get("gruppe")
    stand_threads = _json(vorher["threads"])
    # Legacy comments live in the group conversation, and Planner has taken
    # no new ones since February 2026 (task chat replaced them). So a thread
    # synced once is final, and the ONE listing per group that used to spot
    # moved threads – a walk over the group's ENTIRE conversation store,
    # Teams posts included, at Graph's tiny page size: minutes of silence
    # and 504s on a big group – runs only when asked for
    # (PLANNER_LEGACY_SYNC, the button in the Planner settings). Never on
    # the first sync: there every post is fetched anyway. A thread the state
    # does not know (first run, or its task failed then) is fetched
    # directly, no listing needed.
    erste = not stand_threads
    erzwungen = legacy_sync_erzwungen()
    if gruppe and not erste and not erzwungen:
        progress.event("run.planner.legacy_skip", name=plan["titel"])
    if gruppe and not erste and erzwungen and gruppe not in threads_cache:
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

    # The sweep follows its own clock, never the "Sync now" button: that
    # button only lets the cadence gate step aside.
    takt = sweep_stunden() * 3600
    sweep = takt > 0 and \
        (time.time() - float(db.kv_lesen("sweep") or 0)) > takt
    anhang_stand = {} if alles else _json(vorher["anhaenge"])
    neu = unveraendert = fehler = 0
    gesehen = set()
    # Decide first, then work: the progress bar then knows its target, and
    # the start line says how much this run really intends – on a first run
    # that is 2–3 requests per task, minutes' worth.
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
        legacy_neu = bool(thread and (thread not in stand_threads or (
            threads is not None and threads.get(thread, "") !=
            stand_threads.get(thread, ""))))
        if not (geaendert or legacy_neu or sweep):
            unveraendert += 1
            continue
        legacy_holen = bool(thread and gruppe and (
            thread not in stand_threads or threads is not None))
        faellig.append((t, alt, geaendert, legacy_holen))
    progress.event("run.planner.start", name=plan["titel"], n=len(tasks),
                   m=len(faellig))
    if faellig:
        progress.melde(0, len(faellig), "tasks")
    # The refreshes run side by side; the shared state is touched only
    # here, in this thread, once the workers are done.
    ergebnisse = {}
    with ThreadPoolExecutor(max_workers=max(1, int(workers or 1))) as pool:
        offen = {pool.submit(_task_auffrischen, graph, ziel, plan, t, alt,
                             geaendert, legacy_holen, threads, sweep,
                             anhang_stand): t
                 for t, alt, geaendert, legacy_holen in faellig}
        for lfd, fut in enumerate(as_completed(offen), 1):
            t = offen[fut]
            try:
                ergebnisse[t["id"]] = fut.result()
            except auth.TokenExpired:
                pool.shutdown(cancel_futures=True)
                raise
            except Exception as e:
                fehler += 1
                _event("run.planner.task_failed", "err",
                       name=str(t.get("title") or t["id"])[:60],
                       error=f"{type(e).__name__}: {e}")
            progress.melde(lfd, len(faellig), "tasks")
    # Merged in listing order, whatever order the workers finished in: the
    # board (cards of one bucket with equal order hints) and the stored
    # blob must not depend on thread timing.
    anhang_neu = {}
    for t, _alt, _geaendert, _legacy in faellig:
        if t["id"] not in ergebnisse:
            continue
        eintrag, faden, anhang = ergebnisse[t["id"]]
        eintraege[t["id"]] = eintrag
        if faden:
            stand_threads[faden[0]] = faden[1]
        anhang_neu.update(anhang)
        neu += 1
    # Absence in a complete, error-free listing is the deletion signal –
    # the record stays, the card moves to the greyed section.
    if not fehler:
        jetzt = datetime.now(UTC).isoformat(timespec="seconds")
        for tid, e in eintraege.items():
            if tid not in gesehen and not e.get("deleted"):
                e["deleted"] = jetzt
    if sweep and not fehler:
        db.kv_schreiben("sweep", str(time.time()))
    anhang_stand.update(anhang_neu)

    kennungen = set()
    for e in eintraege.values():
        kennungen |= set((e.get("task") or {}).get("assignments") or {})
        kennungen |= {k["wer"] for k in e.get("kommentare") or []
                      if k["art"] == "neu"}
    namen = _namen(graph, state_db.StateDb(out), kennungen,
                   bekannt=_json(vorher["namen"]))

    # Written only when different from what is stored: an unchanged board
    # keeps its blobs, its file and its "Stand" line. The plan's own name
    # blob is the corpus reader's view of the shared cache.
    nachher = {
        "plan": json.dumps(
            {"id": plan["id"], "titel": plan["titel"], "labels": labels,
             "buckets": {b["id"]: str(b.get("name") or "") for b in
                         buckets.values()}}, ensure_ascii=False),
        "tasks": json.dumps(eintraege, ensure_ascii=False),
        "threads": json.dumps(stand_threads, ensure_ascii=False),
        "namen": json.dumps(namen, ensure_ascii=False, sort_keys=True),
        "anhaenge": json.dumps(anhang_stand, ensure_ascii=False),
    }
    geaendert = {k: v for k, v in nachher.items() if v != vorher[k]}
    stand = db.kv_lesen("stand")
    board = ziel / "board.html"
    if geaendert or not stand or not board.exists():
        if geaendert or not stand:
            stand = datetime.now(UTC).isoformat(timespec="seconds")
        for k, v in geaendert.items():
            db.kv_schreiben(k, v)
        db.kv_schreiben("stand", stand)
        export_util.schreibe_atomar(
            board, render_board(plan, buckets, eintraege, labels, namen, stand))
    progress.event("run.planner.plan", name=plan["titel"], n=len(tasks))
    return neu, unveraendert, fehler


def lauf(graph, out, plaene, fehl=0, workers=1):
    out = Path(out)
    neu = unveraendert = fehler = uebersprungen = 0
    threads_cache = {}
    if export_util.voll_neu():
        progress.event("run.full_sync")
    for plan in plaene:
        db = state_db.StateDb(plan_ziel(out, plan))
        kadenz = plan.get("kadenz") or "always"
        if not (legacy_sync_erzwungen()
                or export_util.einheit_faellig(db, kadenz)):
            uebersprungen += 1
            progress.event("run.cadence.skip", name=plan["titel"],
                           cadence=progress.atom(f"cadence.{kadenz}"))
            continue
        try:
            n, u, f = plan_lauf(graph, out, plan, threads_cache, workers)
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
    out = export_util.ausgabeordner(argv)
    urls = planner_urls()
    if not urls:
        progress.event("run.planner.none", "warn")
        progress.ergebnis(0)
        return
    workers = settings.number("EXPORT_WORKERS", "workers")
    graph_client.konfiguriere(workers)
    graph = auth.waehle_zugang(lambda tok: TokenClient(tok), Graph)
    try:
        plaene, fehl = resolve_plans(graph, urls, out)
        lauf(graph, out, plaene, fehl, workers)
    except auth.TokenExpired:
        progress.fehler("token_expired")
        sys.exit(1)


if __name__ == "__main__":
    main()
