#!/usr/bin/env python3
"""
steps.py – the step registry: one entry per export action.

Until 7.0 every step was hand-threaded through four layers of app.py (API
handler, launch, build_steps, run record) plus side registers – the RUNNABLE
whitelist, the bundle module list, the scheduler call, the runs table's
naming in the page. Forgetting one of them was a recurring bug class: the
Planner export shipped with an empty alert (missing RUNNABLE entry) and a
nameless runs row (missing UI map), each found by a user.

Here every step is ONE entry, and the layers iterate this table:

    key       step name – runs.db, progress display, skip logic
    anfrage   the request/launch flag that switches the step on
    script    subprogram name; must be RUNNABLE and bundled (test_steps.py
              cross-checks both, plus the language files)
    label     i18n key of the step name, or callable(ctx) -> key
    argv      callable(cfg, ctx, pfade) -> argv after the script name
    env       callable(cfg, ctx) -> environment on top of the shared base
    corpus    counts as archive content ("New" column, index skip logic)
    zugang    needs a Graph token
    schedule  key of its toggle in cfg["schedule"], or None
    master    config switch that must be on for scheduled runs, or None
    quelle    how the runs table names this source – an i18n key, a literal,
              or None for derived steps (index, calendar, checks)
    aktiv     callable(cfg, ctx) -> bool, extra gate (category lists)
    ziel      callable(cfg, pfade) -> Path for "skip when nothing new", None
              otherwise (implies nur_bei_neuem)

The callables keep the table honest: they receive everything they need and
touch no module state, so the registry can be read – and tested – as data.
"""

import json


def _flag(wert):
    return "1" if wert else "0"


TEAMS_KATEGORIEN = ("1on1", "group", "meeting", "channels")


def kadenzen(cfg):
    """The cadence table as the exports read it. Every gate lives inside
    the export that it paces (per category, per URL, per notebook); the app
    only hands the table over. An old whole-source "teams" entry (before
    9.0) stands in for the four category keys until they are set."""
    kad = dict(cfg.get("sync_cadence") or {})
    alt = kad.pop("teams", None)
    if alt:
        for kat in TEAMS_KATEGORIEN:
            kad.setdefault(f"teams:{kat}", alt)
    return kad


def _kadenz_env(cfg, ctx):
    """SYNC_CADENCE for every paced export, SYNC_NOW when the run was asked
    to ignore the cadences once (a "sync now" button), FULL_SYNC – with
    SYNC_NOW alongside – when a source's "Force full sync" button asked
    for everything to be read again (export_util.voll_neu)."""
    voll = bool(ctx.get("full_sync"))
    return {"SYNC_CADENCE": json.dumps(kadenzen(cfg)),
            **({"SYNC_NOW": "1"} if ctx.get("sync_now") or voll else {}),
            **({"FULL_SYNC": "1"} if voll else {})}


def _sharepoint_env(cfg, ctx):
    # Always set, even empty: empty means "no filter", unset would mean
    # "whatever app_config.json says" – the run must mirror the form.
    return {**_kadenz_env(cfg, ctx),
            "SHAREPOINT_URLS": str(cfg.get("sharepoint_urls") or ""),
            "SHAREPOINT_RULES": str(cfg.get("sharepoint_rules") or ""),
            "SHAREPOINT_TYPES_INCLUDE": str(cfg.get("sharepoint_types_include") or ""),
            "SHAREPOINT_TYPES_EXCLUDE": str(cfg.get("sharepoint_types_exclude") or ""),
            "SHAREPOINT_MAX_MB": str(int(cfg.get("sharepoint_max_mb") or 0))}


def _onedrive_env(cfg, ctx):
    # Always set, even empty: empty means "take everything", unset would
    # mean "whatever app_config.json says".
    return {**_kadenz_env(cfg, ctx),
            "ONEDRIVE_RULES": str(cfg.get("onedrive_rules") or ""),
            "ONEDRIVE_MAX_MB": str(int(cfg.get("onedrive_max_mb") or 0))}


def _teams_env(cfg, ctx):
    return {**_kadenz_env(cfg, ctx),
            "EXPORT_CATEGORIES": ",".join(ctx["cats_teams"]),
            # Always set, even empty: empty means "every conversation".
            "TEAMS_RULES": str(cfg.get("teams_rules") or ""),
            "TEAMS_SINCE": str(cfg.get("teams_since") or ""),
            "EMBED_IMAGES": _flag(cfg.get("embed_images")),
            "CACHE_IMAGES": _flag(cfg.get("cache_images")),
            "REFRESH_CHANNELS": _flag(cfg.get("refresh_channels")),
            "SKIP_EMPTY_CHATS": _flag(cfg.get("skip_empty_chats")),
            "TEAMS_ATTACHMENTS": _flag(cfg.get("teams_attachments")),
            "TEAMS_CHANNEL_FILES": _flag(cfg.get("teams_channel_files")),
            "TEAMS_FILES_MAX_MB": str(int(cfg.get("teams_files_max_mb") or 0))}


def _todo_env(cfg, ctx):
    return {**_kadenz_env(cfg, ctx),
            # Always set, even empty: empty means "every list".
            "TODO_RULES": str(cfg.get("todo_rules") or "")}


def _index_argv(cfg, ctx, pfade):
    argv = [pfade["teams"], pfade["outlook"], pfade["onedrive"],
            "--sharepoint", pfade["sharepoint"],
            "--pages", pfade["sharepoint_pages"],
            "--planner", pfade["planner"],
            "--todo", pfade["todo"], "--onenote", pfade["onenote"],
            "--store", pfade["store"],
            "--model", cfg["embed_model"], "--ollama", cfg["ollama"],
            "--batch", cfg.get("index_batch", 128)]
    if not ctx["embeddings"]:
        argv.append("--no-embeddings")
    return argv


def _calendar_argv(cfg, ctx, pfade):
    argv = [pfade["outlook"], "--json", pfade["calendar_json"]]
    if not ctx["reconstruct"]:
        argv.append("--no-reconstruct")
    return argv


REGISTRY = (
    {"key": "outlook", "anfrage": "outlook", "script": "outlook_export",
     "start": "job.start.outlook",
     "label": "job.step.outlook", "corpus": True, "zugang": True,
     "schedule": "outlook", "master": None, "quelle": "Outlook",
     "aktiv": lambda cfg, ctx: bool(ctx["cats_outlook"]),
     "argv": lambda cfg, ctx, pfade: [pfade["outlook"]],
     "env": lambda cfg, ctx: {
         **_kadenz_env(cfg, ctx),
         "EXPORT_CATEGORIES": ",".join(ctx["cats_outlook"]),
         "INCLUDE_HIDDEN": _flag(cfg.get("include_hidden")),
         # Always set, even empty: empty means "skip nothing", unset would
         # mean "the script's default".
         "SKIP_FOLDERS": ",".join(cfg.get("skip_folders") or []),
         "OUTLOOK_SINCE": str(cfg.get("outlook_since") or ""),
         "CALENDAR_MONTHS_BACK": str(int(cfg.get("calendar_months_back") or 0)),
         # The "read the calendar in full" button: window and change
         # tokens step aside once. Always set, so the script never falls
         # back to app_config.json for it. A full sync of the source
         # reads the calendar the same way.
         "CALENDAR_FULL": _flag(ctx.get("calendar_full")
                                or ctx.get("full_sync")),
         **({"SYNC_NOW": "1"} if ctx.get("calendar_full") else {})}},

    {"key": "onedrive", "anfrage": "onedrive", "script": "onedrive_export",
     "start": "job.start.onedrive",
     "label": "job.step.onedrive", "corpus": True, "zugang": True,
     "schedule": "onedrive", "master": "onedrive_enabled",
     "quelle": "OneDrive",
     "argv": lambda cfg, ctx, pfade: [pfade["onedrive"]],
     "env": _onedrive_env},

    {"key": "sharepoint", "anfrage": "sharepoint",
     "start": "job.start.sharepoint",
     "script": "sharepoint_export",
     "label": "job.step.sharepoint", "corpus": True, "zugang": True,
     "schedule": "sharepoint", "master": "sharepoint_enabled",
     "quelle": "search.source.sharepoint",
     "argv": lambda cfg, ctx, pfade: [pfade["sharepoint"]],
     "env": lambda cfg, ctx: {
         **_sharepoint_env(cfg, ctx),
         **({"SHAREPOINT_URLS": ctx["nur_einheit"], "SYNC_NOW": "1"}
            if ctx["nur_einheit"] else {})}},

    {"key": "planner", "anfrage": "planner", "script": "planner_export",
     "start": "job.start.planner",
     "label": "job.step.planner", "corpus": True, "zugang": True,
     "schedule": "planner", "master": "planner_enabled",
     "quelle": "search.source.planner",
     "argv": lambda cfg, ctx, pfade: [pfade["planner"]],
     "env": lambda cfg, ctx: {
         **_kadenz_env(cfg, ctx),
         "PLANNER_URLS": (ctx["nur_einheit"] or
                          str(cfg.get("planner_urls") or "")),
         "PLANNER_ATTACHMENTS": _flag(cfg.get("planner_attachments")),
         "PLANNER_SWEEP_HOURS": str(int(cfg.get("planner_sweep_hours") or 0)),
         **({"SYNC_NOW": "1"} if ctx["nur_einheit"] else {}),
         **({"PLANNER_LEGACY_SYNC": "1"} if ctx["legacy_comments"]
            else {})}},

    {"key": "todo", "anfrage": "todo", "script": "todo_export",
     "start": "job.start.todo",
     "label": "job.step.todo", "corpus": True, "zugang": True,
     "schedule": "todo", "master": "todo_enabled",
     "quelle": "search.source.todo",
     "argv": lambda cfg, ctx, pfade: [pfade["todo"]],
     "env": _todo_env},

    {"key": "onenote", "anfrage": "onenote", "script": "onenote_export",
     "start": "job.start.onenote",
     "label": "job.step.onenote", "corpus": True, "zugang": True,
     "schedule": "onenote", "master": "onenote_enabled",
     "quelle": "search.source.onenote",
     "argv": lambda cfg, ctx, pfade: [pfade["onenote"]],
     "env": lambda cfg, ctx: {
         **_kadenz_env(cfg, ctx),
         "ONENOTE_IMAGE_MAX_MB":
             str(int(cfg.get("onenote_image_max_mb") or 0)),
         # Always set, even empty: empty means "every notebook".
         "ONENOTE_RULES": str(cfg.get("onenote_rules") or ""),
         **({"ONENOTE_ONLY": ctx["nur_einheit"], "SYNC_NOW": "1"}
            if ctx["nur_einheit"] else {})}},

    {"key": "sharepoint_pages", "anfrage": "sharepoint_pages",
     "start": "job.start.sharepoint_pages",
     "script": "sharepoint_export",
     "label": "job.step.pages", "corpus": True, "zugang": True,
     "schedule": "sharepoint_pages", "master": "sharepoint_pages_enabled",
     "quelle": "search.source.pages",
     "argv": lambda cfg, ctx, pfade: ["--pages", pfade["sharepoint_pages"]],
     "env": lambda cfg, ctx: {
         **_kadenz_env(cfg, ctx),
         "SHAREPOINT_PAGES_URLS": (ctx["nur_einheit"] or
                                   str(cfg.get("sharepoint_pages_urls") or "")),
         **({"SYNC_NOW": "1"} if ctx["nur_einheit"] else {}),
         "SHAREPOINT_PAGES_IMAGE_MAX_MB":
             str(int(cfg.get("sharepoint_pages_image_max_mb") or 0))}},

    {"key": "teams", "anfrage": "teams", "script": "teams_export",
     "start": "job.start.teams",
     "label": "job.step.teams", "corpus": True, "zugang": True,
     "schedule": "teams", "master": None, "quelle": "Teams",
     "aktiv": lambda cfg, ctx: bool(ctx["cats_teams"]),
     "argv": lambda cfg, ctx, pfade: [pfade["teams"]],
     "env": _teams_env},

    {"key": "index", "anfrage": "index", "script": "rag_index",
     "start": "job.start.index",
     "label": lambda ctx: ("job.step.index" if ctx["embeddings"]
                           else "job.step.index.lexical"),
     "corpus": False, "zugang": False,
     "schedule": "index", "master": None, "quelle": None,
     "argv": _index_argv, "env": lambda cfg, ctx: {},
     # If the export brought nothing new, this step indexes the same
     # corpus a second time. "ziel" is the condition under which skipping
     # is safe: only when an index already exists.
     "ziel": lambda cfg, pfade: pfade["store_db"]},

    {"key": "calendar", "anfrage": "calendar", "script": "combined_search",
     "start": "job.start.calendar",
     "label": lambda ctx: ("job.step.calendar" if ctx["reconstruct"]
                           else "job.step.calendar.plain"),
     "corpus": False, "zugang": False,
     "schedule": None, "master": None, "quelle": None,
     "argv": _calendar_argv, "env": lambda cfg, ctx: {},
     "ziel": lambda cfg, pfade: pfade["calendar_file"]},

    {"key": "onedrive_folders", "anfrage": "sync_onedrive",
     "start": "job.start.onedrive_folders",
     "script": "onedrive_export",
     "label": "job.step.folders", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--folders", pfade["onedrive"]],
     "env": lambda cfg, ctx: {
         "ONEDRIVE_RULES": str(cfg.get("onedrive_rules") or "")}},

    {"key": "sharepoint_folders", "anfrage": "sync_sharepoint",
     "start": "job.start.sharepoint_folders",
     "script": "sharepoint_export",
     "label": "job.step.folders", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--folders", pfade["sharepoint"]],
     "env": _sharepoint_env},

    {"key": "folders", "anfrage": "sync_folders", "script": "outlook_export",
     "start": "job.start.folders",
     "label": "job.step.folders", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--folders", pfade["outlook"]],
     "env": lambda cfg, ctx: {}},

    {"key": "teams_list", "anfrage": "sync_teams",
     "start": "job.start.teams_list",
     "script": "teams_export",
     "label": "job.step.teams_list", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--teams", pfade["teams"]],
     # The list names every conversation the ticked categories would see;
     # the same environment as the export, so both agree on the paths.
     "env": lambda cfg, ctx: {
         **_teams_env(cfg, ctx),
         "EXPORT_CATEGORIES": ",".join(
             ctx["cats_teams"] or list(TEAMS_KATEGORIEN))}},

    {"key": "todo_lists", "anfrage": "sync_todo",
     "start": "job.start.todo_lists",
     "script": "todo_export",
     "label": "job.step.todo_lists", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--lists", pfade["todo"]],
     "env": _todo_env},

    {"key": "notebooks", "anfrage": "sync_notebooks",
     "start": "job.start.notebooks",
     "script": "onenote_export",
     "label": "job.step.notebooks", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--notebooks", pfade["onenote"]],
     "env": lambda cfg, ctx: {
         "ONENOTE_RULES": str(cfg.get("onenote_rules") or "")}},

    {"key": "calendars", "anfrage": "sync_calendars",
     "start": "job.start.calendars",
     "script": "outlook_export",
     "label": "job.step.calendars", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--calendars", pfade["outlook"]],
     "env": lambda cfg, ctx: {}},

    {"key": "check", "anfrage": "check", "script": "outlook_export",
     "start": "job.start.check",
     "label": "job.step.check", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--check", pfade["outlook"]],
     "env": lambda cfg, ctx: {}},

    {"key": "check_onedrive", "anfrage": "check_onedrive",
     "start": "job.start.check_onedrive",
     "script": "onedrive_export",
     "label": "job.step.check", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--check", pfade["onedrive"]],
     "env": _onedrive_env},

    {"key": "check_sharepoint", "anfrage": "check_sharepoint",
     "start": "job.start.check_sharepoint",
     "script": "sharepoint_export",
     "label": "job.step.preview", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--check", pfade["sharepoint"]],
     "env": _sharepoint_env},

    {"key": "check_pages", "anfrage": "check_pages",
     "start": "job.start.check_pages",
     "script": "sharepoint_export",
     "label": "job.step.check", "corpus": False, "zugang": True,
     "schedule": None, "master": None, "quelle": None,
     "argv": lambda cfg, ctx, pfade: ["--check-pages",
                                      pfade["sharepoint_pages"]],
     "env": lambda cfg, ctx: {
         "SHAREPOINT_PAGES_URLS":
             str(cfg.get("sharepoint_pages_urls") or "")}},
)

ANFRAGEN = tuple(e["anfrage"] for e in REGISTRY)


def baue(cfg, ctx, pfade, base_env, script_argv, angefragt):
    """The step list of one run – the registry, filtered and instantiated."""
    schritte = []
    for e in REGISTRY:
        if not angefragt.get(e["anfrage"]):
            continue
        if "aktiv" in e and not e["aktiv"](cfg, ctx):
            continue
        label = e["label"](ctx) if callable(e["label"]) else e["label"]
        schritt = {"key": e["key"], "label": label, "start": e["start"],
                   "argv": script_argv(e["script"],
                                       *e["argv"](cfg, ctx, pfade)),
                   "env": {**base_env, **e["env"](cfg, ctx)}}
        if e.get("corpus"):
            schritt["corpus"] = True
        if e.get("ziel"):
            schritt["nur_bei_neuem"] = True
            schritt["ziel"] = e["ziel"](cfg, pfade)
        schritte.append(schritt)
    return schritte


def braucht_zugang(angefragt):
    """Does any requested step talk to Graph?"""
    return any(angefragt.get(e["anfrage"]) and e["zugang"] for e in REGISTRY)


def anfrage_aus_request(data):
    """The launch flags straight from an API request body."""
    return {e["anfrage"]: bool(data.get(e["anfrage"])) for e in REGISTRY}


def plan_anfrage(plan, cfg):
    """The launch flags of a scheduled run: every step with a schedule
    toggle, narrowed by its master switch – the schedule can only narrow,
    never widen."""
    anfrage = {}
    for e in REGISTRY:
        if not e["schedule"]:
            continue
        an = bool(plan.get(e["schedule"], True))
        if e["master"]:
            an = an and bool(cfg.get(e["master"]))
        anfrage[e["anfrage"]] = an
    return anfrage


def ui_metadaten():
    """What the page needs to name steps: per step the source label the
    runs table shows (an i18n key when it contains a dot, otherwise a
    literal) – injected into /*__STEPS__*/ at serve time."""
    return {e["key"]: {"quelle": e["quelle"]}
            for e in REGISTRY if e.get("quelle")}
