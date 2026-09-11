#!/usr/bin/env python3
"""
settings.py – the configuration schema and the app_config.json access layer.

The settings edited in the interface (app.py) live in app_config.json; the
subprograms read the same file through this module. Precedence, strongest
first:

    environment variable  >  app_config.json  >  built-in default (VORGABEN)

The environment stays on top because the app hands its in-memory settings to
every subprocess as variables – that keeps a run unambiguous even while the
file is being edited.

The file is looked up in MUNIMENTUM_DATA_DIR (OFFICE365_DATA_DIR until
4.2.0), otherwise next to this module. Standard library only.
"""

import json
import os
from pathlib import Path

import ollama_client

CONFIG_NAME = "app_config.json"

# The subfolders inside the data directory. They used to be configurable – a
# legacy from when these were loose scripts someone called by hand in some
# arbitrary directory. The app now hands every script its output directory
# itself (export_util.ausgabeordner); the teams_dir/outlook_dir/… keys from
# old config files are not read by anyone anymore.
TEAMS_DIR = "teams_export"
OUTLOOK_DIR = "outlook_export"
ONEDRIVE_DIR = "onedrive_export"
SHAREPOINT_DIR = "sharepoint_export"
SHAREPOINT_PAGES_DIR = "sharepoint_pages"
PLANNER_DIR = "planner_export"
TODO_DIR = "todo_export"
ONENOTE_DIR = "onenote_export"
STORE_DIR = "rag_store"

# Mailbox folders that the default selection of outlook_export.py skips.
SKIP_FOLDERS_STANDARD = {
    "archive", "archiv",
    "entwürfe", "drafts",
    "erneut erinnern aktiviert",
    "gelöschte elemente", "deleted items",
    "junk-e-mail", "junk email", "junk-email",
    "postausgang", "outbox",
}

# What bloats an attachment list without anyone searching for it: the
# signature and encryption attachments that mail programs attach themselves.
# They sit in the field as a default, visible and changeable there – a silent
# rule in the code would be exactly the thing nobody finds later.
FILETYPE_HIDDEN_STANDARD = {"p7s", "p7m", "asc", "pgp", "sig"}

# The blueprint of app_config.json: every key once, with its default.
# app.py shows exactly these values in the settings; the individual scripts
# fetch them via flag()/number()/value() below – each call site used to carry
# its own copy of the default, and nothing kept the copies in sync.
VORGABEN = {
    # Ollama is optional. Off means: it is no longer even looked for (a
    # connection attempt used to run into the void every ten seconds),
    # semantic search and the AI summary disappear, and the index is built
    # as a pure full-text index. Everything else runs unchanged.
    "ollama_enabled": True,
    # Even with Ollama one may want the full-text index: embedding costs a
    # good hour on a real archive, and whoever only searches exactly pays it
    # for nothing.
    "index_semantic": True,
    # Off until someone turns it on: a drive can hold double-digit
    # gigabytes, and nobody should pull those on the first click.
    "onedrive_enabled": False,
    # Include/exclude on OneDrive paths, the same mechanics as the mailbox.
    "onedrive_rules": "",
    "onedrive_max_mb": 0,
    # Off until someone turns it on – the same caution as with the drive.
    "sharepoint_enabled": False,
    # Site or library URLs, one per line; each resolves to its libraries.
    "sharepoint_urls": "",
    # Extension filters, comma-separated ("pdf, docx"). Include empty means
    # every type; exclude wins when both name the same extension.
    "sharepoint_types_include": "",
    "sharepoint_types_exclude": "",
    "sharepoint_max_mb": 0,
    # Site pages as HTML – its own list: pages and libraries rarely overlap.
    "sharepoint_pages_enabled": False,
    "sharepoint_pages_urls": "",
    # Embed images up to this size; larger ones stay links. 0 = no limit.
    "sharepoint_pages_image_max_mb": 4,
    # Sync cadence per source or per SharePoint unit ("onedrive", "teams",
    # "sharepoint:<drive-id>", "pages:<site-id>") -> always|daily|weekly|
    # monthly. Missing key means always; the toggle narrows every run,
    # scheduled and manual alike.
    "sync_cadence": {},
    # Nothing preselected: each of these categories can mean tens of
    # thousands of items and many gigabytes. What gets fetched should be a
    # decision, not whatever happened to be ticked on first launch.
    "outlook_categories": [],
    "teams_categories": [],
    "workers": 4,
    # Drive mirrors (OneDrive/SharePoint) take their own concurrency: Graph
    # documents no fixed limit for drives – throttling is budget-based and
    # answered with visible waits – so more than the mailbox's four is fine.
    "mirror_workers": 8,
    # Switches of the export scripts (there via environment variable, see env_flag)
    "embed_images": True,
    "cache_images": True,
    "refresh_channels": True,
    "skip_empty_chats": True,
    "include_hidden": False,
    # Recovers deleted appointments from invitation and cancellation mails.
    # Every .eml is read for this – by far the most expensive step. On by
    # default because it makes appointments visible that no longer exist
    # anywhere else.
    "calendar_reconstruct": True,
    "skip_folders": sorted(SKIP_FOLDERS_STANDARD),
    # File types not offered in the search filter. Purely cosmetic:
    # everything stays exported and searchable, it just does not appear in
    # the selection list. A visible default instead of a rule in the code.
    "filetype_hidden": sorted(FILETYPE_HIDDEN_STANDARD),
    # People not counted in the analytics – usually oneself: one's own
    # messages otherwise top the list by a wide margin and say nothing about
    # the exchange with others. One per line, because names contain commas
    # ("Schilling, Nico").
    "analytics_skip": [],
    # Folder selection as ordered rules, last match wins.
    # Empty means: the old name list above still applies (see folders.py).
    "folder_rules": "",
    # Calendar selection, the same mechanics as above. Empty means: only the
    # default calendar (see folders.nur_standard).
    "calendar_rules": "",
    # After the first export the calendar is read as a window: this many
    # months back plus everything ahead. 0 = every appointment, every run.
    "calendar_months_back": 1,
    # First export only: nothing older than this day (YYYY-MM-DD). Empty =
    # the whole history. One for the mailbox, one for the chats.
    "outlook_since": "",
    "teams_since": "",
    # Teams selection: ordered rules over "1on1/<title>", "group/<title>",
    # "meeting/<title>", "channels/<team>/<channel>". Empty means: every
    # conversation of the ticked categories.
    "teams_rules": "",
    # SharePoint libraries: path rules over "<site>/<library>/<path>" on top
    # of the URL list and the type filters. Empty means: every path.
    "sharepoint_rules": "",
    # To Do: ordered rules over the list titles. Empty means: every list.
    "todo_rules": "",
    # Planner re-reads every task's chat comments this often – they carry no
    # change signal of their own.
    "planner_sweep_hours": 24,
    # 128 measured on a real archive: roughly a fifth faster than 64, and
    # even the longest chunks still get through. Ollama rejects 256.
    "index_batch": 128,
    "ollama": ollama_client.DEFAULT_URL,
    "embed_model": "bge-m3",
    "chat_model": "qwen3.6:27b",            # phrases the answer, locally
    "answer_sources": 8,                    # how many hits it reads for that
    # Lower bound of the semantic search; see mcp_server.SEM_MIN. As an
    # integer in percent so the UI can use a plain number field and nobody
    # trips over a decimal comma.
    "semantic_min": 45,
    # Hits per page in the search. More means less paging, but also a longer
    # list one has to look through first.
    "search_results": 20,
    # UI userflow recording: the last interaction steps for the error
    # report – only the kind (tab, search, run), never contents, purely in
    # the memory of the open page. 0 turns it off.
    "notifications": "errors",  # system notifications: off | errors | all
    "userflow_actions": 20,
    # How far back the run history (runs.db) reaches. Cleaned up at startup
    # and after every run; the file stays in the kilobyte range.
    "planner_enabled": False,
    "planner_urls": "",
    "planner_attachments": False,
    # To Do lists and OneNote notebooks: off until someone turns them on,
    # like every mirror-style source. Neither needs a URL list – both are
    # "mine" and come whole.
    "todo_enabled": False,
    "onenote_enabled": False,
    # Notebook selection, the same ordered rules as for mailbox folders over
    # the notebook folder names. Empty means: every notebook.
    "onenote_rules": "",
    # OneNote pages: embed images up to this size, larger ones land as
    # files next to the page. 0 = embed every image.
    "onenote_image_max_mb": 4,
    # Files a Teams message references live in the sender's OneDrive or the
    # team's library – both gone with the access. Off by default: a chat
    # history is small, its files may not be. The size cap applies to both
    # switches; 0 = no limit.
    "teams_attachments": False,
    "teams_channel_files": False,
    "teams_files_max_mb": 0,
    "runs_retention_months": 24,
    # Levels 3 and 4 of the storage model: empty means subfolders of the
    # fixed home directory ("rag_store" and "data"). Resolved in app.py.
    "data_dir": "",
    "index_dir": "",
    "log_retention_days": 14,
    "mcp_port": 8365,
    # The hard switch: off means mcp_server refuses service – over HTTP as
    # over stdio. Start/stop next to it only concerns the HTTP endpoint
    # that this app runs itself.
    "mcp_enabled": True,
    "mcp_autostart": True,
    "update_check": True,   # check GitHub once at startup
    # How the app signs in. "token" = pasted access key (no request to IT
    # needed, but valid only for hours); "login" = real sign-in with a
    # refresh token so the schedule runs unattended.
    "auth_mode": "token",
    "client_id": "",        # empty = Microsoft's public application
    "tenant": "",           # empty = organizations
    "device_code": False,   # scripts in the terminal: code instead of browser window
    "language": "auto",   # "auto" = browser language, otherwise a code from lang/
    # The tour's chapters someone has finished or skipped – it never comes
    # back on its own; Settings › App can start it again.
    "tour_seen": {},
    "schedule": {
        "enabled": False,
        "interval_minutes": 60,
        "outlook": True,
        "teams": True,
        # The mirrors ride along only where their master switch is on –
        # these toggles narrow a scheduled run, they cannot widen it.
        "onedrive": True,
        "sharepoint": True,
        "sharepoint_pages": True,
        "planner": True,
        "todo": True,
        "onenote": True,
        "index": True,
        "calendar": True,
    },
}

_FALSCH = ("0", "false", "no", "nein", "off", "")

# Sentinel for "no default of its own": then VORGABEN[key] applies. An
# explicit default=None stays None, however – the rule keys (folder_rules
# and friends) distinguish "not set" from "empty".
_AUS_SCHEMA = object()


def _vorgabe(key, default):
    return VORGABEN[key] if default is _AUS_SCHEMA else default


_cache = {"pfad": None, "daten": None}


def data_dir_env():
    """The data directory set via the environment – or None.

    MUNIMENTUM_DATA_DIR is the current name; OFFICE365_DATA_DIR remains
    valid so existing scripts and shortcuts do not break. app.py asks the
    same place for its data directory – the two names live only here.
    """
    return os.environ.get("MUNIMENTUM_DATA_DIR") or os.environ.get("OFFICE365_DATA_DIR")


def home_env():
    """The app's home directory (configuration, token, history), handed by
    app.py to every subprocess – the all-in-one override
    (MUNIMENTUM_DATA_DIR) stays valid alongside and wins for data."""
    return os.environ.get("MUNIMENTUM_HOME")


def config_path():
    """Where the configuration lives: home directory, override, otherwise
    next to the module (source run: the project directory)."""
    env = home_env() or data_dir_env()
    base = Path(env).expanduser() if env else Path(__file__).resolve().parent
    return base / CONFIG_NAME


def load(path=None):
    """Read the configuration (cached once per path).

    A missing or broken file yields {} – an unreadable app_config.json must
    never prevent an export, after all it only supplies defaults.
    """
    p = Path(path) if path is not None else config_path()
    if _cache["pfad"] == str(p) and _cache["daten"] is not None:
        return _cache["daten"]
    try:
        daten = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        daten = {}
    if not isinstance(daten, dict):
        daten = {}
    _cache.update(pfad=str(p), daten=daten)
    return daten


def reset():
    """Clear the cache (tests, re-reading)."""
    _cache.update(pfad=None, daten=None)


def _truthy(raw):
    return str(raw).strip().lower() not in _FALSCH


def flag(env_name, key, default=_AUS_SCHEMA):
    """Switch: environment, else file, else default (from VORGABEN)."""
    default = _vorgabe(key, default)
    raw = os.environ.get(env_name)
    if raw is not None:
        return _truthy(raw)
    val = load().get(key)
    if isinstance(val, bool):
        return val
    return default


def number(env_name, key, default=_AUS_SCHEMA, low=1):
    """Number: environment, else file, else default. Unusable input is ignored."""
    default = _vorgabe(key, default)
    for roh in (os.environ.get(env_name), load().get(key)):
        if roh is None or isinstance(roh, bool):
            continue
        try:
            return max(low, int(roh))
        except (TypeError, ValueError):
            continue
    return default


def value(key, default=_AUS_SCHEMA):
    """Value from the file, else default (no environment variable).

    Deliberately without a note for report(): this branch only supplies
    defaults for arguments that the command line trumps (output directory,
    --store, --model). A message "taken from app_config.json" would simply
    be wrong there as soon as someone passes the argument.
    """
    default = _vorgabe(key, default)
    val = load().get(key)
    return default if val is None else val


def folders(env_name, key, default=_AUS_SCHEMA):
    """Folder list: environment (comma-separated), else file (list), else default.

    Set to empty means an empty list, not "default" – app.py needs this
    distinction to be able to express "really all folders".
    """
    default = _vorgabe(key, default)
    raw = os.environ.get(env_name)
    if raw is not None:
        return {t.strip().lower() for t in raw.split(",") if t.strip()}
    val = load().get(key)
    if isinstance(val, list):
        return {str(t).strip().lower() for t in val if str(t).strip()}
    return set(default)
