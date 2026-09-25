"""
api_archive.py – the Build-archive door of /api/v1: runs and their logs,
the sources' checks and fetches, the completeness balance, the folder
plans, Insights, the schedule and the storage paths.
"""
import json

import api
import archive_check
import completeness
import evidence
import export_util
import folders
import i18n
import state_db
import steps as steps_mod
from api import Ablehnung


def laeufe(h, _p, q, _data):
    grenze = api.zahl(q, "limit", 50, 1, 200)
    return api.liste({"runs": h.app.history.list_runs(grenze + 1)}, "runs", grenze)


def lauf_starten(h, _p, _q, data):
    """A run: which steps, and how. The body names the steps by the
    keys the step registry uses (`outlook`, `index`, …); everything
    else steers how they work. One run at a time – while one is on,
    this is a 409."""
    app = h.app
    mit_outlook = bool(data.get("outlook"))
    kalender = bool(data.get("calendar"))
    rekonstruktion = None            # None: as configured
    if kalender and mit_outlook:
        # Part of an export run: the step follows what is actually
        # fetched. The "build calendar & contacts" button comes without
        # outlook and stays untouched.
        kalender, mit_mails = h.M.calendar_plan(app.cfg)
        if not mit_mails:
            rekonstruktion = False
    anfrage = steps_mod.anfrage_aus_request(data)
    anfrage.update(outlook=mit_outlook, calendar=kalender)
    ok, why = app.launch(
        anfrage,
        nur_einheit=(str(data.get("unit") or "").strip() or None),
        embeddings=data.get("embeddings"),
        label=str(data.get("label") or "job.export"),
        reconstruct=rekonstruktion,
        legacy_comments=bool(data.get("legacy_comments")),
        sync_now=bool(data.get("sync_now")),
        calendar_full=bool(data.get("calendar_full")),
        full_sync=bool(data.get("full_sync")),
        resync=bool(data.get("resync")),
        resync_ordner=(data.get("resync_folders")
                       if isinstance(data.get("resync_folders"), list) else None),
        # The Insights card's own selection: the balance rows to check,
        # ticked for the export or not. Absent: the checks follow the
        # export's switches, as the size preview expects.
        check_rows=([str(r) for r in data["check_rows"]]
                    if isinstance(data.get("check_rows"), list) else None))
    if not ok:
        raise Ablehnung(409, why)
    return lauf_antwort(h, why)


def lauf_antwort(h, warum=None, extra=None):
    """Every start answers the same way: the run is on, and where to
    watch it. What it is doing travels with the status."""
    daten = {"run": f"{api.API_V1}/runs/current", **(extra or {})}
    if warum:
        daten["message"] = warum
    return api.json(daten, 202, extra={"Location": f"{api.API_V1}/runs/current"})


def lauf_jetzt(h, _p, _q, _data):
    """The run that is on – what the `Location` of a started run names.
    Nothing running is a 404: the monitor exists while the run does."""
    # The run alone: app.status() would re-read the token, probe
    # Ollama and ask the MCP subprocess, all to throw it away – and
    # this is the route a caller polls.
    job = h.app.jobs.snapshot().get("job")
    if not job:
        raise Ablehnung(404, "srv.run.none")
    return api.json({"run": job})


def lauf_abbrechen(h, _p, _q, _data):
    """Nothing running is a 404 here as on GET: the resource is the
    run, and there is none – not a conflict to retry. While one is
    on, the wish is recorded whatever the runner does at that moment:
    between two steps there is no process to end, and the run stops
    at the next step all the same."""
    if not h.app.jobs.busy:
        raise Ablehnung(404, "srv.run.none")
    h.app.jobs.cancel()
    return api.leer()


def log(h, _p, q, _data):
    """The app's live log from a cursor on – not one run's: the lines
    of a run are a window into this, which `log_seq` marks. That is
    why it is not `/runs/current/log`, and why it answers when nothing
    is running."""
    lines, seq = h.app.jobs.log_since(api.zahl(q, "since", 0, 0))
    return api.json({"items": lines, "seq": seq})


def lauf_protokoll(h, p, _q, _data):
    """The stored log of one run. A run that never was is a 404; one
    whose lines were pruned answers an empty list."""
    kennung = api.id_aus(p, "id", "srv.run.unknown")
    if not h.app.history.has_run(kennung):
        raise Ablehnung(404, "srv.run.unknown")
    return api.json({"items": h.app.history.run_log(kennung)})


def quelle_aus(h, p):
    """The source a path names – one of the eight the archive knows."""
    quelle = str(p.get("source") or "").strip()
    if h.M.EXPORT_ORDNER.get(quelle) is None or quelle not in archive_check.PRUEFER:
        raise Ablehnung(404, "srv.archiv.unknown", {"source": quelle})
    return quelle


def bilanz_holen(h, p, _q, _data):
    """Fetch what the balance found open – the cheapest way the row's
    source allows. The rows are the step registry's (`outlook_mail`,
    `outlook_calendar`, …), finer than the eight sources under
    /sources: the mailbox is three of them."""
    return bilanz_lauf(h, bilanzzeile(p))


def quelle_nachholen(h, p, _q, _data):
    return archiv(h, "nachholen", {"quelle": quelle_aus(h, p)})


def quelle_neu(h, p, _q, _data):
    return archiv(h, "neu-aufbauen", {"quelle": quelle_aus(h, p)})


def quelle_befunde(h, p, _q, data):
    """What the archive check found, and what should become of it:
    `noted` writes a tombstone, `set-aside` puts it away, `open` brings
    it back. `kinds` narrows it to a kind of finding."""
    aktion = {"noted": "vermerken", "set-aside": "beiseitelegen",
              "open": "zurueckholen"}.get(str(data.get("state") or ""))
    if aktion is None:
        raise Ablehnung(400, "srv.archiv.badstate",
                        {"state": str(data.get("state") or "")})
    # The two kinds a mark can apply to (archive_check.ARTEN_VERMERKBAR).
    # A kind nobody knows is refused rather than dropped: it would fall
    # back to "lost" downstream, and a caller who asked to mark one
    # thing would get a tombstone for another.
    arten = {"lost": "verloren", "missing": "fehlt"}
    roh = []
    for a in (data.get("kinds") or []):
        if str(a) not in arten:
            raise Ablehnung(400, "srv.archiv.badkind", {"kind": str(a)[:40]})
        roh.append(arten[str(a)])
    return archiv(h, aktion, {"quelle": quelle_aus(h, p), "arten": roh})


def quelle_oeffnen(h, p, _q, _data):
    return archiv(h, "ordner", {"quelle": quelle_aus(h, p)})


def bilanzzeile(p):
    """The balance row a path names – one of the step registry's
    (`outlook_mail`, `outlook_calendar`, …). The mailbox check writes
    three reports, one per row, and none of them under `outlook`;
    that is why the rows live under /balance, not /sources."""
    quelle = str(p.get("row") or "").strip()
    eintrag = next((e for e in steps_mod.PRUEFUNGEN if e["quelle"] == quelle), None)
    if eintrag is None:
        raise Ablehnung(404, "srv.archiv.unknown", {"source": quelle})
    return eintrag


def bilanz(h, p, _q, _data):
    """The report a balance row's last check wrote – null before the
    first."""
    eintrag = bilanzzeile(p)
    # The connection lives in the object's thread-local; on a threaded
    # server nothing else would ever close it.
    with state_db.StateDb(h.M.BASE / h.M.EXPORT_ORDNER[eintrag["ordner"]]) as db:
        return api.json({"report": completeness.lesen(db, eintrag["quelle"])})


def quelle_ordnerplan(h, p, _q, data):
    """What the next run would do with these rules – without starting
    it. The rules come from the body, not from the settings: otherwise
    the preview would show the state before the change. The mailbox
    has two rule sets; `unit: calendar` asks for the calendars' plan
    instead of the folders' (`mail`, the default)."""
    quelle = quelle_aus(h, p)
    if quelle not in api.PLAN_QUELLEN:
        raise Ablehnung(404, "srv.plan.nosource", {"source": quelle})
    einheit = data.get("unit")
    if einheit is not None and (quelle != "outlook" or einheit not in api.PLAN_EINHEITEN):
        raise Ablehnung(400, "srv.plan.badunit", {"unit": str(einheit)[:40]})
    ziel = "calendar" if einheit == "calendar" else quelle
    return api.json(ordnerplan(h, {**data, "quelle": ziel}))


def analytics(h, _p, _q, _data):
    return api.json(h.M.analytics_daten(h.app.cfg))


def analytics_neu(h, _p, _q, _data):
    """The numbers again, computed afresh – the one way to invalidate
    everything at once."""
    return api.json(h.M.analytics_daten(h.app.cfg, neu=True))


def zeitplan(h, _p, _q, data):
    return api.json(zeitplan_speichern(h, data))


def speicherorte(h, _p, _q, data):
    """Where the archive and the index lie. Nothing is moved: relocating
    folders is the user's business, and the app never does it."""
    return speicherorte_setzen(h, data)


def speicherorte_setzen(h, data):
    """`data_dir` and `index_dir`: both are keys in app_config.json,
    which sits fixed in the home folder, so there is no chicken-and-egg.
    Empty means the default under the home folder. NOTHING is moved –
    relocating folders is the user's business. Both paths are fixed
    since startup and go to every subprocess as its working directory,
    so a change takes effect on the next start; the answer says so."""
    app = h.app
    felder = (("data_dir", h.M.HEIM / h.M.DATEN_UNTERORDNER, h.M.BASE),
              ("index_dir", h.M.HEIM / h.M.STORE_DIR, h.M.STORE_PFAD))
    antwort, neustart, neu = {}, False, {}
    for key, vorgabe, aktuell in felder:
        if key not in data:
            continue
        roh = str(data.get(key) or "").strip()
        if roh:
            ziel, fehler = h.M.pruefe_datenordner(roh)
            if fehler:
                # Into the log as well: the settings save sends this
                # along with everything else, and a refusal must not
                # hide in one small field.
                app.jobs.log(fehler, "err")
                raise Ablehnung(400, fehler)
        else:
            ziel = vorgabe
        neu[key] = "" if ziel == vorgabe else str(ziel)
        antwort[key] = str(ziel)
        neustart = neustart or str(ziel) != str(aktuell)
    # Two profiles must never share an export or index folder: their
    # archives would run into each other.
    anderes = h.M.profil_geteilt(h.M.PROFIL, antwort.get("data_dir", str(h.M.BASE)),
                             antwort.get("index_dir", str(h.M.STORE_PFAD)))
    if anderes:
        fehler = {"k": "srv.datadir.shared", "v": {"profile": anderes}}
        app.jobs.log(fehler, "err")
        raise Ablehnung(400, fehler)
    vorher = {key: app.cfg.get(key) or "" for key in neu}
    app.konfiguriere(lambda cfg: cfg.update(neu))
    # One line per path that really changed, each naming its own folder.
    for key, _vorgabe, _aktuell in felder:
        if key in neu and neu[key] != vorher[key]:
            app.jobs.logk("srv.datadir.set" if key == "data_dir" else "srv.indexdir.set",
                          "warn", path=antwort[key])
    return api.json({"data_dir": antwort.get("data_dir", str(h.M.BASE)),
                       "index_dir": antwort.get("index_dir", str(h.M.STORE_PFAD)),
                       "restart_required": neustart})


def bilanz_lauf(h, eintrag):
    """"Fetch now" on a balance row: only what the row found open, the
    cheapest way each source allows. A row whose check named the open
    items by id (the mirrors' files, Teams' conversations, the calendar's
    events, the contacts) gets exactly those, straight from the stored
    report – no walk, no listing – and the fetch adjusts the row itself;
    when the report had to cap them, the source's wider way follows. The
    mailbox otherwise: a resync limited to the folders with something
    open (mail is counted, not listed, by its check). The mirrors
    otherwise: a resync. Every other source: its regular run, which
    fetches what changed anyway. The row is then judged afresh at the
    end of the same run."""
    app = h.app
    quelle = eintrag["quelle"]
    if app.jobs.busy:
        raise Ablehnung(409, "srv.busy")
    ordner = eintrag["ordner"]
    with state_db.StateDb(h.M.BASE / h.M.EXPORT_ORDNER[ordner]) as db:
        bericht = completeness.lesen(db, quelle) or {}
    anfrage = {**eintrag["lauf"], "index": True, eintrag["anfrage"]: True}
    (key,) = [k for k, an in eintrag["lauf"].items() if an]
    if bericht.get("offene") and not bericht.get("offene_gekappt"):
        liste = h.M.HEIM / f"nachholen-{key}.json"
        export_util.schreibe_atomar(liste, json.dumps(
            {"quelle": key, "dateien": bericht["offene"]}, ensure_ascii=False))
        anfrage.pop(eintrag["anfrage"])          # the fetch adjusts the row itself
        ok, why = app.launch(anfrage, label='job.holen', sync_now=True,
                             nachholen={"quelle": key, "liste": str(liste)})
    elif ordner == "outlook":
        # The row's category runs, ticked for the export or not, and
        # the check afterwards judges that row (check_rows) – not
        # whatever the export happens to tick.
        ok, why = app.launch(anfrage, label='job.holen', sync_now=True, resync=True,
                             resync_ordner=[z["pfad"] for z in bericht.get("zeilen") or []],
                             check_rows=[quelle])
    elif ordner in ("onedrive", "sharepoint"):
        ok, why = app.launch(anfrage, label='job.holen', sync_now=True, resync=True,
                             check_rows=[quelle])
    else:
        ok, why = app.launch(anfrage, label='job.holen', sync_now=True, check_rows=[quelle])
    if not ok:
        raise Ablehnung(409, why)
    return lauf_antwort(h)


def evidence_findings(h, folder):
    """The files below one export folder the stored evidence check found
    changed or missing – named relative to that folder, as a fetch list
    wants them."""
    report = archive_check.bericht_lesen(h.M.archiv_bericht_pfad()).get("nachweis") or {}
    prefix = f"{folder}/"
    return [f["rel"][len(prefix):] for kind in ("changed", "missing", "ms_mismatch")
            for f in report.get(kind) or [] if str(f.get("rel") or "").startswith(prefix)]


def archiv(h, aktion, data):
    """The archive check's actions (archive_check.py): one source at a
    time, explicit, each a run of its own – the run window shows what
    happens, the run history keeps it, and nothing runs while another
    run writes into the folders. The step judges the source afresh
    afterwards and updates its row in the stored report; the rebuild's
    run carries the source's export and the index behind it. Opening
    the folder is no process and answers at once."""
    app = h.app
    quelle = str(data.get("quelle") or "").strip()
    unterordner = h.M.EXPORT_ORDNER.get(quelle)
    if unterordner is None or quelle not in archive_check.PRUEFER:
        # Unreachable from /sources, whose routes resolve the source
        # first – but when it answers, it answers like them: a name
        # that is none of the eight is a 404.
        raise Ablehnung(404, "srv.archiv.unknown", {"source": quelle})
    ordner = h.M.BASE / unterordner
    if aktion == "ordner":
        if not archive_check.ordner_oeffnen(ordner if ordner.is_dir() else h.M.BASE):
            raise Ablehnung(500, "srv.archiv.open.fail")
        return api.json({"path": str(ordner if ordner.is_dir() else h.M.BASE)})
    if app.jobs.busy:
        raise Ablehnung(409, "srv.busy")
    if not ordner.is_dir() or (aktion == "neu-aufbauen"
                               and not archive_check.beschaedigte(ordner)):
        return api.json({"message": {"k": "srv.archiv.nothing", "v": {}}})
    if aktion == "nachholen":
        # "Fetch again": exactly the files the stored report found
        # missing or short, written to a list the source's step reads;
        # the row is judged afresh at the end of the same run.
        zeile = archive_check.zeile_lesen(h.M.archiv_bericht_pfad(), quelle)
        befunde = (zeile or {}).get("befunde") or {}
        dateien = list(befunde.get("fehlt") or []) + list(befunde.get("unvollstaendig") or [])
        # What the evidence check found changed or gone below this source's
        # folder comes along: the fresh copy replaces it, the chain notes it.
        dateien += [f for f in evidence_findings(h, unterordner) if f not in dateien]
        if not dateien:
            return api.json({"message": {"k": "srv.archiv.nothing", "v": {}}})
        liste = h.M.HEIM / f"nachholen-{quelle}.json"
        export_util.schreibe_atomar(liste, json.dumps(
            {"quelle": quelle, "dateien": dateien}, ensure_ascii=False))
        ok, why = app.launch({quelle: True, "index": True, "archiv_pruefen": True},
                             label='job.archiv.nachholen', sync_now=True,
                             archiv={"quelle": quelle},
                             nachholen={"quelle": quelle, "liste": str(liste)})
        if not ok:
            raise Ablehnung(409, why)
        return lauf_antwort(h)
    anfrage = {"archiv_" + aktion.replace("-", "_"): True}
    if aktion == "neu-aufbauen":
        anfrage.update({quelle: True, "index": True})
    arten = [str(a) for a in (data.get("arten") or ["verloren"])
             if str(a) in archive_check.ARTEN_VERMERKBAR] or ["verloren"]
    ok, why = app.launch(anfrage, label='job.archiv.' + aktion,
                         sync_now=aktion == "neu-aufbauen",
                         archiv={"quelle": quelle, "arten": arten})
    if not ok:
        raise Ablehnung(409, why)
    return lauf_antwort(h)


def zeitplan_speichern(h, data):
    vorher = dict(h.app.cfg["schedule"])

    def uebernehmen(cfg):
        plan = cfg["schedule"]
        for key in ("enabled", "outlook", "teams", "onedrive",
                    "sharepoint", "sharepoint_pages", "planner", "todo",
                    "onenote", "organization", "index", "calendar"):
            if key in data:
                plan[key] = bool(data[key])
        if "interval_minutes" in data:
            try:
                plan["interval_minutes"] = max(
                    5, int(data["interval_minutes"]))
            except (TypeError, ValueError):
                pass
    h.app.konfiguriere(uebernehmen)
    plan = h.app.cfg["schedule"]
    # Unchanged plan, unchanged clock. The page already asks before it
    # posts; this covers everyone else on the documented API, for whom
    # a no-op save would otherwise push a nearly due run back by a
    # whole interval.
    if plan != vorher:
        h.app.scheduler.reset()
        h.app.jobs.logk("srv.sched.state", "info",
                           min=plan["interval_minutes"],
                           state={"k": "srv.sched.on" if plan["enabled"]
                                  else "srv.sched.off", "v": {}})
    return {"schedule": plan,
            "next": h.app.scheduler.next_due()}


def ordnerplan(h, data):
    """What the next run would do – without starting it.

    Takes the rules from the form, not the saved ones: otherwise the
    preview would show the previous state while the new rule already
    sits next to it.

    Three sources, one evaluation. The difference is small enough that
    further copies do not pay: for the mailbox the `.eml` files count,
    for the calendars the `.ics`, for the mirror all files.
    """
    cfg = h.app.cfg
    quelle = str(data.get("quelle") or "")
    if quelle == "sharepoint":
        return sharepoint_plan(h, data.get("sharepoint_rules"))
    if quelle == "onenote":
        return notizbuch_plan(h, data.get("onenote_rules"))
    if quelle == "teams":
        return teams_plan(h, data.get("teams_rules"))
    if quelle == "todo":
        return todo_plan(h, data.get("todo_rules"))
    datei = folders.KALENDER if quelle == "calendar" else folders.DATEI
    if quelle == "onedrive":
        ordner, endung = h.M.BASE / h.M.ONEDRIVE_DIR, None
    else:
        ordner, endung = h.M.BASE / h.M.OUTLOOK_DIR, (
            ".ics" if quelle == "calendar" else ".eml")
    daten = folders.lade(ordner, datei)
    if not daten:
        raise Ablehnung(404, "srv.plan.nolist", leer=True)
    if quelle == "onedrive":
        regeln = folders.lies_regeln(
            data.get("onedrive_rules")
            if data.get("onedrive_rules") is not None
            else cfg.get("onedrive_rules") or "")
    elif quelle == "calendar":
        regeln = h.M.kalenderregeln(cfg, daten, data.get("calendar_rules"))
    else:
        regeln = h.M.auswahlregeln(cfg, data.get("folder_rules"),
                               data.get("skip_folders"))
    return {"regeln": folders.schreibe_regeln(regeln),
            **folders.plan(ordner, regeln, daten, endung, datei)}


def notizbuch_plan(h, roh):
    """The export list for the notebooks: the stored list against the
    rules from the form, pages counted per notebook. A notebook keeps
    its pages in section folders, so the count on disk is summed up
    to the notebook – and every folder in the export root is walked,
    so a notebook that left the list still shows as "only here"."""
    wurzel = h.M.BASE / h.M.ONENOTE_DIR
    daten = folders.lade(wurzel, folders.NOTIZBUECHER)
    if not daten:
        raise Ablehnung(404, "srv.plan.nolist", leer=True)
    regeln = h.M.notizbuchregeln(h.app.cfg, roh)
    wurzeln = {e["pfad"].split("/")[0] for e in daten.get("ordner", [])}
    if wurzel.is_dir():
        wurzeln |= {p.name for p in wurzel.iterdir() if p.is_dir()}
    je_buch = {}
    for pfad, n in folders.auf_platte(wurzel, sorted(wurzeln), ".html").items():
        buch = pfad.split("/")[0]
        je_buch[buch] = je_buch.get(buch, 0) + n
    return {"regeln": folders.schreibe_regeln(regeln),
            **folders.plan(wurzel, regeln, daten, ".html",
                           folders.NOTIZBUECHER, archiv=je_buch)}


def teams_plan(h, roh):
    """The export list for Teams: the stored conversation list against
    the rules from the form. A conversation is one file, named after
    its title with the id as suffix – so "in the archive" is counted
    per title, and a file whose title left the list shows as "only
    here"."""
    wurzel = h.M.BASE / h.M.TEAMS_DIR
    daten = folders.lade(wurzel, folders.DATEI)
    if not daten:
        raise Ablehnung(404, "srv.plan.nolist", leer=True)
    regeln = h.M.teamsregeln(h.app.cfg, roh)
    archiv = {}
    if wurzel.is_dir():
        for datei in wurzel.glob("*/*.html"):
            archiv[f"{datei.parent.name}/{h.M._ohne_kennung(datei.stem)}"] = \
                archiv.get(f"{datei.parent.name}/{h.M._ohne_kennung(datei.stem)}", 0) + 1
        for datei in wurzel.glob("channels/*/*.html"):
            pfad = f"channels/{datei.parent.name}/{h.M._ohne_kennung(datei.stem)}"
            archiv[pfad] = archiv.get(pfad, 0) + 1
    return {"regeln": folders.schreibe_regeln(regeln),
            **folders.plan(wurzel, regeln, daten, ".html",
                           folders.DATEI, archiv=archiv)}


def todo_plan(h, roh):
    """The export list for To Do: the stored list of lists against the
    rules from the form; a list's folder carries the id as suffix, its
    tasks are counted from the folder's state."""
    wurzel = h.M.BASE / h.M.TODO_DIR
    daten = folders.lade(wurzel, folders.DATEI)
    if not daten:
        raise Ablehnung(404, "srv.plan.nolist", leer=True)
    regeln = h.M.todoregeln(h.app.cfg, roh)
    archiv = {}
    if wurzel.is_dir():
        for ordner in wurzel.iterdir():
            if not ordner.is_dir() or ordner.name.startswith("."):
                continue
            try:
                with state_db.StateDb(ordner) as db:
                    n = len(json.loads(db.kv_lesen("tasks") or "{}"))
            except ValueError:
                n = 0
            pfad = h.M._ohne_kennung(ordner.name)
            archiv[pfad] = archiv.get(pfad, 0) + n
    return {"regeln": folders.schreibe_regeln(regeln),
            **folders.plan(wurzel, regeln, daten, ".html",
                           folders.DATEI, archiv=archiv)}


def sharepoint_plan(h, roh=None):
    """The export list for the SharePoint mirror: every library's tree,
    paths prefixed with site/library, judged by the path rules on top
    of the URL list. Each entry names the URLs its library came from,
    so the page can tell which cadence row paces it."""
    wurzel = h.M.BASE / h.M.SHAREPOINT_DIR
    eintraege, stand = [], None
    if wurzel.is_dir():
        for lib in sorted(p for p in wurzel.glob("*/*") if p.is_dir()):
            with state_db.StateDb(lib) as db:
                d = db.baum_lesen()
                if not d:
                    continue
                try:
                    urls = json.loads(db.kv_lesen("urls") or "[]")
                except ValueError:
                    urls = []
            praefix = lib.relative_to(wurzel).as_posix()
            stand = max(stand or "", d.get("abgeglichen") or "") or None
            for e in d.get("ordner", []):
                eintraege.append({**e, "pfad": f"{praefix}/{e['pfad']}",
                                  **({"urls": urls} if urls else {})})
    if not eintraege:
        raise Ablehnung(404, "srv.plan.nolist", leer=True)
    daten = {"ordner": eintraege, "abgeglichen": stand}
    regeln = h.M.sharepointregeln(h.app.cfg, roh)
    plan = folders.plan(wurzel, regeln, daten, None)
    # The walk under the site roots also sees each library's bookkeeping
    # (state.db and other leftovers) – real content lives below Dateien/.
    plan["weg"] = [z for z in plan["weg"]           # drive_mirror.DATEI_DIR
                   if "/Dateien/" in z["pfad"] + "/"]
    plan["mails_weg"] = sum(z["archiv"] for z in plan["weg"])
    return {"regeln": folders.schreibe_regeln(regeln), **plan}


def bericht(h, _p, _q, data):
    """A report someone can paste into an issue: the state, the log the
    page shows, and what they typed – addresses and user names replaced
    before it leaves the machine."""
    return api.json(h.M.fehlerbericht(
        {**h.app.status(), "store": h.M.store_status(h.app.cfg)},
        str(data.get("log") or ""), str(data.get("hint") or ""),
        i18n.negotiate(h.app.cfg.get("language"),
                       h.headers.get("Accept-Language"), h.M.RES),
        cfg=h.app.cfg))


def evidence_summary(h, _p, _q, _data):
    """How far the evidence chain reaches: its length and head, since
    when it runs, when it was last stamped, and what the kept versions
    hold. Asked for when Settings opens, never polled."""
    ev = evidence.Evidence(h.M.BASE)
    if not ev.exists():
        return api.json({"lines": 0, "head": None, "since": None, "stamped": None,
                         "versions": evidence.kept_versions(ev.versions)[0],
                         "versions_bytes": evidence.kept_versions(ev.versions)[1]})
    try:
        return api.json(ev.summary())
    finally:
        ev.close()


# The routes of this door, in the order the table in app.py lists them.
ROUTEN = (
    ("GET", "/api/v1/runs", laeufe),
    ("POST", "/api/v1/runs", lauf_starten),
    ("GET", "/api/v1/runs/current", lauf_jetzt),
    ("DELETE", "/api/v1/runs/current", lauf_abbrechen),
    ("GET", "/api/v1/log", log),
    ("GET", "/api/v1/runs/{id}/log", lauf_protokoll),
    ("GET", "/api/v1/balance/{row}", bilanz),
    ("POST", "/api/v1/balance/{row}/fetch", bilanz_holen),
    ("POST", "/api/v1/sources/{source}/refetch", quelle_nachholen),
    ("POST", "/api/v1/sources/{source}/rebuild", quelle_neu),
    ("PATCH", "/api/v1/sources/{source}/findings", quelle_befunde),
    ("POST", "/api/v1/sources/{source}/open", quelle_oeffnen),
    ("QUERY", "/api/v1/sources/{source}/folder-plan", quelle_ordnerplan),
    ("GET", "/api/v1/evidence", evidence_summary),
    ("GET", "/api/v1/analytics", analytics),
    ("POST", "/api/v1/analytics/refresh", analytics_neu),
    ("PATCH", "/api/v1/schedule", zeitplan),
    ("PATCH", "/api/v1/storage", speicherorte),
    ("QUERY", "/api/v1/reports", bericht),
)
