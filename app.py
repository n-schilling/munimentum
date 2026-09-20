#!/usr/bin/env python3
"""
app.py – browser-based front end for the Office 365 export.

One start, one window: the script starts a small HTTP server on 127.0.0.1
and opens the interface in the default browser. From there every part of the
project runs without anyone needing a terminal:

    Access      Two ways, chosen in the wizard: paste an access token from
                the Graph Explorer (gx_token.txt), or sign in once by device
                code and let the refresh token carry unattended runs
                (auth.py, auth_mode = "login"). The wizard opens when no
                valid access is present.
    Export      Outlook and/or Teams, chosen by click instead of prompt
                (sets EXPORT_CATEGORIES for the export scripts).
    Index       rag_index.py afterwards, for search and MCP.
    Search      embedded – the same ranking as in the MCP server
                (BM25 + embeddings, fused via RRF).
    Schedule    while the app runs: export + index at a fixed interval.
    MCP         start/stop mcp_server.py, config snippets for Claude.

Without Ollama the app shows an installation wizard. Everything else keeps
working: the MCP server is started and indexing is skipped (or, on request,
built as a plain full-text index, rag_index.py --no-embeddings).

Since 7.0 the file is split: page.html is the interface (one page, a data
file), steps.py the step registry (one entry per export action), runner.py
runs the steps as subprocesses. What stays here: configuration, paths,
routes, and the wiring between them.

    python3 app.py [--port 8700] [--no-browser]

The server binds only to the loopback address and checks the Host header.
It has no authentication and serves the entire mail and chat archive – it
does not belong on 0.0.0.0.
"""

import os
import re
import sys
import copy
import json
import time
import gzip
import base64
import html
import posixpath
import shutil
import socket
import sqlite3
import subprocess
import argparse
import platform
import importlib
import threading
import webbrowser
import multiprocessing.spawn
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote, unquote
import socketserver
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import answer
import archive_check
import auth
import case_export
import completeness
import detail
import export_util
import faelle
import folders
import i18n
import rest
import notify
import ollama_client
import analytics_db
import run_history
from runner import JobRunner, McpProcess, _stream_lines  # noqa: F401
import steps as steps_mod
import settings
import state_db
import store_layout
import updates
import version

# On Windows the console defaults to a legacy codepage; force UTF-8 so
# print() does not choke on Unicode (macOS/Linux: no-op).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

APP_DIRNAME = settings.APP_DIRNAME
FROZEN = bool(getattr(sys, "frozen", False))

# Subprograms the bundled file can start itself via "--run <name>". As
# scripts they lie side by side, in the bundle as modules inside it.
RUNNABLE = ("outlook_export", "teams_export", "rag_index", "combined_search",
            "mcp_server", "case_export",
            # auth is not an export step but a self-report: which sign-in
            # path applies, is a key present, is there a cache. In the
            # bundle this is the only way to check that without network –
            # the smoke test does exactly that.
            "auth", "onedrive_export", "sharepoint_export",
            "planner_export", "todo_export", "onenote_export",
            "archive_check")


# Directory of the shipped scripts (in the bundle: the unpacked archive) –
# one answer, shared with the MCP server.
resource_dir = export_util.resource_dir


def standard_data_dir():
    """Where the data lives when nobody says otherwise.

    As a script: the project folder – exports and rag_store already live
    there. Bundled: the user's data folder, because the bundle itself
    unpacks into a temp directory that vanishes on every exit, and an app
    must not write into /Applications or C:\\Program Files.
    """
    return settings.app_wurzel(FROZEN)


DATEN_UNTERORDNER = "data"


def data_dir():
    """The app's HOME folder: configuration, token, run history.

    Four levels, deliberately separate: (1) the application itself lives
    wherever the operating system puts it; (2) this home folder is FIXED –
    only then can the configuration live here and itself say where (3) the
    index (index_dir) and (4) the exports (data_dir) reside; both default
    to subfolders of here. MUNIMENTUM_DATA_DIR and --data-dir remain the
    all-in-one override for individual runs and tests.

    Munimentum NEVER moves data itself – whoever relocates folders does so
    by hand and adjusts the paths afterwards.
    """
    env = settings.data_dir_env()
    if env:
        return Path(env).expanduser().resolve()
    return standard_data_dir()


def _split_pfade(heim, cfg=None):
    """(export root, index folder) – the override mode keeps the flat layout.

    Without `cfg` the profile's own file is read by path through
    settings.load(): without one it looks next to the module, which in the
    bundle is the unpacked archive. Another profile's configuration comes
    in as a dict (see profil_pfade) – settings' cache is this process's.
    """
    if settings.data_dir_env():
        return heim, heim / STORE_DIR
    if cfg is None:
        cfg = settings.load(heim / settings.CONFIG_NAME)
    return settings.datenpfade(heim, cfg)


RES = resource_dir()

_SEITE = None


def seite():
    """The interface: one HTML file next to the code, shipped as a data file
    like lang/ and openapi.yaml. Read on first use, not at import – the
    bundle's multiprocessing workers import this module too and have no
    business loading 200 KB of markup. The /-route fills its three
    placeholders, /*__I18N__*/ (language strings), /*__STEPS__*/ (the
    step metadata of the registry) and /*__PRUEFUNGEN__*/ (the balance
    rows of the overview), on every request."""
    global _SEITE
    if _SEITE is None:
        _SEITE = (RES / "page.html").read_text(encoding="utf-8")
    return _SEITE
HEIM = data_dir()
# The fixed app folder holds the profiles (profiles/<name>/); HEIM is the
# folder of the one this process runs, set by set_profil() in main().
WURZEL = HEIM
PROFIL = settings.STANDARD_PROFIL
PROFIL_REGISTER = "profiles.json"            # last opened, "open without asking"
_UMZUG = {}                                  # what layout_umzug() did this start
_NEUSTART = None                             # argv to start over with, once the server stopped
CONFIG_FILE = HEIM / settings.CONFIG_NAME   # the same file the scripts read
TOKEN_FILE = HEIM / "gx_token.txt"
BASE, STORE_PFAD = None, None                # set below, after STORE_DIR


def set_data_dir(path):
    """All-in-one override (--data-dir). Returns the new path."""
    global HEIM, BASE, STORE_PFAD, CONFIG_FILE, TOKEN_FILE
    HEIM = Path(path).expanduser().resolve()
    BASE = HEIM
    STORE_PFAD = HEIM / STORE_DIR
    CONFIG_FILE = HEIM / settings.CONFIG_NAME
    TOKEN_FILE = HEIM / "gx_token.txt"
    # The subprograms look up their defaults via the same variable – otherwise
    # a subprocess would read the file next to the script, not the one chosen
    # here.
    os.environ["MUNIMENTUM_DATA_DIR"] = str(HEIM)
    settings.reset()
    return HEIM

# --------------------------------------------------------------------------
# Profiles – every one its own archive
# --------------------------------------------------------------------------
def profile_moeglich():
    """Profiles live inside the fixed app folder; the all-in-one override
    (--data-dir, MUNIMENTUM_DATA_DIR) knows exactly one archive – and so
    does a start whose move into profiles/ failed: it runs the archive
    where it lies, as one archive, until a later start manages the move."""
    return not settings.data_dir_env() and not _UMZUG.get("fehler")


def profil_namen():
    return settings.profil_namen(WURZEL) if profile_moeglich() else [PROFIL]


def _config_lesen(pfad):
    """A configuration file as a dict – {} when missing or broken. Not
    settings.load(): its one cache slot belongs to THIS process's own
    configuration, and other profiles' files must not push it out."""
    try:
        daten = json.loads(Path(pfad).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return daten if isinstance(daten, dict) else {}


def _relativ(pfad, ordner):
    """`pfad` relative to `ordner` – None when it lies elsewhere."""
    try:
        return Path(pfad).relative_to(ordner)
    except ValueError:
        return None


def _aufgeloest(wert):
    try:
        return Path(wert).expanduser().resolve()
    except OSError:
        return None


def _pfade_folgen(cfg, alt, neu):
    """The path settings that pointed into folder `alt`, rewritten to
    `neu` – as a dict of the changed keys. Paths elsewhere are the user's
    choice and stay."""
    geaendert = {}
    for key in ("data_dir", "index_dir"):
        wert = str(cfg.get(key) or "").strip()
        pfad = _aufgeloest(wert) if wert else None
        rel = _relativ(pfad, alt) if pfad is not None else None
        if rel is not None:
            geaendert[key] = str(Path(neu) / rel)
    return geaendert


def profil_register_lesen():
    return _config_lesen(WURZEL / PROFIL_REGISTER)


def profil_register_schreiben(**werte):
    daten = profil_register_lesen()
    daten.update(werte)
    WURZEL.mkdir(parents=True, exist_ok=True)
    save_config(daten, WURZEL / PROFIL_REGISTER)
    return daten


def profil_pfade(name):
    """(home, exports, index) of a profile. The running one is wherever
    this process runs it – its globals, no disk; every other one is its
    folder, read from ITS configuration."""
    if name == PROFIL:
        return HEIM, BASE, STORE_PFAD
    heim = settings.profil_ordner(name, WURZEL)
    daten, store = _split_pfade(heim, _config_lesen(heim / settings.CONFIG_NAME))
    return heim, daten, store


def profil_konto(heim):
    """Whose archive: the account of the pasted key, else of the sign-in
    – the one thing that tells two profiles apart on the chooser."""
    token = read_token(heim / "gx_token.txt")
    if token:
        try:
            konto = token_status(token).get("account")
        except Exception:
            konto = None
        if konto:
            return konto
    cfg = _config_lesen(heim / settings.CONFIG_NAME)
    try:
        konto = auth.angemeldet(
            client=str(cfg.get("client_id") or "").strip() or auth.STANDARD_CLIENT_ID,
            mandant=str(cfg.get("tenant") or "").strip() or auth.STANDARD_TENANT,
            heim=heim)
    except Exception:
        return None
    return konto if isinstance(konto, str) else None


def profil_info(name):
    heim, daten, store = profil_pfade(name)
    return {"name": name, "aktiv": name == PROFIL, "ordner": str(heim),
            "konto": profil_konto(heim),
            "last_run": _mtime_iso(heim / run_history.DB_NAME),
            "index": store_layout.db_path(store).exists()}


def profil_zustand():
    """The little every status poll carries: which profile, whether there
    are others, whether profiles exist at all – one directory listing."""
    return {"name": PROFIL, "moeglich": profile_moeglich(),
            "mehrere": len(profil_namen()) > 1}


def profil_status():
    """Everything the storage card, the switch window and the chooser
    show – on demand (GET /api/v1/profiles), never in the status poll: it
    reads every profile's files."""
    namen = profil_namen()
    reg = profil_register_lesen()
    return {"name": PROFIL, "moeglich": profile_moeglich(), "mehrere": len(namen) > 1,
            "ohne_nachfrage": bool(reg.get("ohne_nachfrage")),
            "zuletzt": reg.get("zuletzt") if reg.get("zuletzt") in namen else None,
            "alle": [profil_info(n) for n in namen]}


def profil_geteilt(name, daten, store):
    """The other profile whose export or index folder this one would share
    or nest with – two archives in one tree would be one archive nobody
    asked for."""
    eigene = [Path(daten).resolve(), Path(store).resolve()]
    for anderes in profil_namen():
        if anderes == name:
            continue
        _heim, d2, s2 = profil_pfade(anderes)
        for fremd in (d2.resolve(), s2.resolve()):
            if any(_relativ(a, fremd) is not None or _relativ(fremd, a) is not None
                   for a in eigene):
                return anderes
    return None


def profil_anlegen(name):
    """A new, empty profile folder. Returns (info, error message)."""
    name = str(name or "").strip().lower()
    if not profile_moeglich():
        return None, {"k": "srv.profile.impossible", "v": {}}
    if not settings.profil_name_ok(name):
        return None, {"k": "srv.profile.badname", "v": {}}
    ordner = settings.profil_ordner(name, WURZEL)
    if ordner.exists():
        return None, {"k": "srv.profile.exists", "v": {"name": name}}
    try:
        ordner.mkdir(parents=True)
    except OSError as e:
        return None, {"k": "srv.profile.createfail", "v": {"detail": str(e)}}
    return profil_info(name), None


def profil_umbenennen(alt, neu, port=None):
    """Rename a profile that is not open – not here, not in a second
    instance next door: the folder under profiles/ gets the new name,
    paths in its configuration that pointed into the folder follow it,
    the register keeps up. Returns (name, error)."""
    alt = str(alt or "").strip().lower()
    neu = str(neu or "").strip().lower()
    if not profile_moeglich():
        return None, {"k": "srv.profile.impossible", "v": {}}
    if alt not in profil_namen():
        return None, {"k": "srv.profile.unknown", "v": {"name": alt}}
    if alt == PROFIL or (port and eigene_instanz(port, profil=alt)):
        return None, {"k": "srv.profile.active", "v": {"name": alt}}
    if not settings.profil_name_ok(neu):
        return None, {"k": "srv.profile.badname", "v": {}}
    von, nach = settings.profil_ordner(alt, WURZEL), settings.profil_ordner(neu, WURZEL)
    if nach.exists():
        return None, {"k": "srv.profile.exists", "v": {"name": neu}}
    cfg = _config_lesen(von / settings.CONFIG_NAME)
    folgen = _pfade_folgen(cfg, von.resolve(), nach)
    try:
        os.rename(von, nach)
        if folgen:
            cfg.update(folgen)
            try:
                save_config(cfg, nach / settings.CONFIG_NAME)
            except OSError:
                os.rename(nach, von)
                raise
    except OSError as e:
        return None, {"k": "srv.profile.renamefail", "v": {"detail": str(e)}}
    if profil_register_lesen().get("zuletzt") == alt:
        profil_register_schreiben(zuletzt=neu)
    return neu, None


# The files an archive consists of besides its data and index folders.
ARCHIV_DATEIEN = (settings.CONFIG_NAME, "gx_token.txt", "msal_cache.bin",
                  run_history.DB_NAME)


def layout_umzug(wurzel=None):
    """The one move this app makes: an archive that a version before 10.0
    left in the app folder itself goes into profiles/standard/ – once,
    and only while no profile exists yet.

    Renames on the same disk, so it is instant whatever the size, and all
    or nothing: a rename that fails puts the ones before it back. Data and
    index folders the user pointed outside the app folder are not touched;
    ones inside it move along and the configuration follows. Runs before
    any file is opened (Windows would refuse to move an open one) and says
    what it did once the app is up (serve). Returns what it did, and keeps
    it in _UMZUG.
    """
    global _UMZUG
    wurzel = Path(wurzel or WURZEL)
    ergebnis = {}
    profile = wurzel / settings.PROFIL_ORDNER
    if profile.is_dir() and any(p.is_dir() and settings.profil_name_ok(p.name)
                                for p in profile.iterdir()):
        # Profiles exist: the layout is the new one. A stray file in the
        # app folder is not an archive and must not become a profile.
        _UMZUG = ergebnis
        return ergebnis
    cfg = _config_lesen(wurzel / settings.CONFIG_NAME)
    ziel = profile / settings.STANDARD_PROFIL
    wurzel_r = _aufgeloest(wurzel) or wurzel
    zuege, neu = {}, {}
    for name in ARCHIV_DATEIEN:
        if (wurzel / name).is_file():
            zuege[wurzel / name] = ziel / name
    # The flat layout – export folders in the app folder itself, pinned
    # (data_dir names the app folder) or never pinned: they go along, and
    # the pin points at the new home so nothing has to find them again.
    daten_roh = str(cfg.get("data_dir") or "").strip()
    if not daten_roh or _aufgeloest(daten_roh) == wurzel_r:
        flach = [n for n in ALT_ORDNER if (wurzel / n).is_dir()]
        for n in flach:
            zuege[wurzel / n] = ziel / n
        if flach:
            neu["data_dir"] = str(ziel)
    # The default subfolders, and paths the user set INSIDE the app
    # folder: the top-level entry moves and the setting follows it.
    for key, vorgabe in (("data_dir", DATEN_UNTERORDNER), ("index_dir", STORE_DIR)):
        wert = str(cfg.get(key) or "").strip()
        if not wert:
            if (wurzel / vorgabe).is_dir():
                zuege[wurzel / vorgabe] = ziel / vorgabe
            continue
        pfad = _aufgeloest(wert)
        rel = _relativ(pfad, wurzel_r) if pfad is not None else None
        if not rel or not rel.parts or rel.parts[0] == settings.PROFIL_ORDNER:
            continue                       # elsewhere, or the pin handled above
        if (wurzel / rel.parts[0]).exists():
            zuege[wurzel / rel.parts[0]] = ziel / rel.parts[0]
        neu[key] = str(ziel / rel)
    if not zuege:
        _UMZUG = ergebnis                  # nothing of an archive here
        return ergebnis
    getan = []
    try:
        geraet = wurzel.stat().st_dev
        for quelle in zuege:
            if quelle.stat().st_dev != geraet:
                raise OSError(f"{quelle} lies on another volume")
        for nach in zuege.values():
            if nach.exists():
                raise OSError(f"{nach} already exists")
        ziel.mkdir(parents=True, exist_ok=True)
        for quelle, nach in zuege.items():
            os.rename(quelle, nach)
            getan.append((quelle, nach))
        if neu:
            cfg.update(neu)
            save_config(cfg, ziel / settings.CONFIG_NAME)   # tmp + replace
        ergebnis = {"nach": str(ziel),
                    "bewegt": [str(q.relative_to(wurzel)) for q in zuege]}
    except OSError as e:
        haengt = []
        for quelle, nach in reversed(getan):
            try:
                os.rename(nach, quelle)
            except OSError:
                haengt.append(str(nach))
        for leer in (ziel, profile):
            try:
                leer.rmdir()
            except OSError:
                pass
        fehler = str(e)
        if haengt:
            fehler += " – not put back: " + ", ".join(haengt)
        ergebnis = {"nach": str(ziel), "fehler": fehler}
    _UMZUG = ergebnis
    return ergebnis


def set_profil(name):
    """Point this process at one profile – before the App is built."""
    global HEIM, BASE, STORE_PFAD, CONFIG_FILE, TOKEN_FILE, PROFIL
    PROFIL = str(name or settings.STANDARD_PROFIL).strip().lower()
    HEIM = settings.profil_ordner(PROFIL, WURZEL)
    if (_UMZUG.get("fehler") and PROFIL == settings.STANDARD_PROFIL
            and not HEIM.is_dir()):
        # The move did not happen: this start runs the archive where it
        # still lies, says so, and the next start tries again.
        HEIM = WURZEL
    CONFIG_FILE = HEIM / settings.CONFIG_NAME
    TOKEN_FILE = HEIM / "gx_token.txt"
    # In-process readers (the sign-in, the settings helpers) find the
    # profile's files the same way every subprocess does.
    os.environ["MUNIMENTUM_HOME"] = str(HEIM)
    settings.reset()
    BASE, STORE_PFAD = _split_pfade(HEIM)
    return HEIM


def profil_waehlen(gewuenscht, port, open_browser):
    """Which profile this start runs: the named one, the only one, the last
    one when asking is switched off – otherwise the chooser in the
    browser. Returns (name, browser already open)."""
    namen = profil_namen()
    if gewuenscht:
        gewuenscht = str(gewuenscht).strip().lower()
        if gewuenscht not in namen:
            raise SystemExit(f"Unbekanntes Profil: {gewuenscht}. "
                             f"Vorhanden: {', '.join(namen)}")
        profil_register_schreiben(zuletzt=gewuenscht)
        return gewuenscht, False
    if len(namen) == 1:
        return namen[0], False
    reg = profil_register_lesen()
    if reg.get("ohne_nachfrage") and reg.get("zuletzt") in namen:
        return reg["zuletzt"], False
    return waehle_profil_im_browser(port, open_browser)


def profil_seite(code):
    """The chooser: one small page before the app, nothing of the app's
    own page in it – that one needs a profile to exist."""
    texte = i18n.strings(code, RES)
    t = lambda k: texte.get(k, k)          # noqa: E731
    return (RES / "profil.html").read_text(encoding="utf-8").replace(
        "/*__PROFIL__*/", json.dumps({
            "lang": code,
            "texte": {k: v for k, v in texte.items()
                      if k.startswith(("profile.", "srv.profile."))},
            "profile": profil_status()}, ensure_ascii=False).replace("<", "\\u003c")
    ).replace("__TITEL__", t("profile.title"))


def neustart_mit_profil(httpd, name, port):
    """Switch profiles from inside the app: the server stops, and serve()
    – on the main thread, after its own clean-up – starts this process
    over with --profile: the same executable, the same port; the page
    that asked reloads when the new instance answers.

    Deliberately NOT exec'd from this thread: main() returning would race
    a daemon thread on its way to execv, and whoever loses leaves a page
    waiting for a process that simply ended.
    """
    global _NEUSTART
    _NEUSTART = ([sys.executable] if FROZEN
                 else [sys.executable, str(Path(__file__).resolve())])
    _NEUSTART += ["--profile", name, "--port", str(port), "--no-browser"]
    threading.Thread(target=httpd.shutdown, daemon=True).start()


def neustart_ausfuehren():
    """The end of serve(): start this process over when a switch asked
    for it. Windows' execv joins the arguments with spaces and quotes
    nothing – a path with a space would fall apart – so there a child is
    started and this process ends."""
    if not _NEUSTART:
        return
    if sys.platform == "win32":
        subprocess.Popen(list(_NEUSTART), close_fds=True)
        return
    os.execv(_NEUSTART[0], list(_NEUSTART))


def pruefe_datenordner(pfad):
    """Is the folder usable? Returns (path, error key).

    Better to reject now than on the next start: a pointer to a folder
    without write permission would leave an app that can no longer save
    anything – and the setting to take it back lives exactly there.
    """
    roh = str(pfad or "").strip()
    if not roh:
        return None, "srv.datadir.empty"
    ziel = Path(roh).expanduser()
    try:
        ziel.mkdir(parents=True, exist_ok=True)
        probe = ziel / ".schreibprobe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return None, {"k": "srv.datadir.unwritable", "v": {"detail": str(e)}}
    return ziel.resolve(), None




GRAPH_EXPLORER = "https://developer.microsoft.com/en-us/graph/graph-explorer"
OLLAMA_SITE = "https://ollama.com/download"

# Schema and defaults of app_config.json live in settings.py – the same
# source the individual scripts take their values from; a second copy here
# would have nothing keeping the two in sync.
SKIP_FOLDERS_DEFAULT = settings.SKIP_FOLDERS_STANDARD
FILETYPE_HIDDEN_DEFAULT = settings.FILETYPE_HIDDEN_STANDARD
TEAMS_DIR = settings.TEAMS_DIR
OUTLOOK_DIR = settings.OUTLOOK_DIR
ONEDRIVE_DIR = settings.ONEDRIVE_DIR
SHAREPOINT_DIR = settings.SHAREPOINT_DIR
SHAREPOINT_PAGES_DIR = settings.SHAREPOINT_PAGES_DIR
PLANNER_DIR = settings.PLANNER_DIR
TODO_DIR = settings.TODO_DIR
ONENOTE_DIR = settings.ONENOTE_DIR
STORE_DIR = settings.STORE_DIR
DEFAULT_CONFIG = settings.VORGABEN
BASE, STORE_PFAD = _split_pfade(HEIM)
ALT_ORDNER = (TEAMS_DIR, OUTLOOK_DIR, ONEDRIVE_DIR, SHAREPOINT_DIR,
              SHAREPOINT_PAGES_DIR, PLANNER_DIR, TODO_DIR, ONENOTE_DIR)
_ALT_GEPINNT = False


# --------------------------------------------------------------------------
# Upgrade leftovers: recognise, pin what is safe, block the rest – never move
#
# Three situations an older installation can leave behind, one rule for all
# of them: the app moves no data. The flat pre-7.0 layout is PINNED (the
# config points at it, altbestand_pinnen); the pre-6.2 state files and the
# 6.x pointer file cannot be handled safely and BLOCK every run instead
# (lauf_sperren) – going ahead would mean a full re-download beside the real
# archive and orphaned, write-once tombstones. Startup says it, every run
# checks it again, so the block lifts as soon as the user has sorted it out.
# --------------------------------------------------------------------------
ALT_STATE = {"outlook": ("exported.tsv", "verschwunden.tsv", folders.DATEI,
                         folders.KALENDER, "vollstaendigkeit.json"),
             "teams": ("export_state.json",),
             "onedrive": ("dateien.tsv", "delta.txt", "verschwunden.tsv",
                          folders.DATEI, "vollstaendigkeit.json",
                          "walk.jsonl", "walk_cursor.txt", "walk_fertig.txt")}


def altbestand_state():
    """Export folders that still carry the pre-6.2 state files – [] when none.

    A folder that already has its state.db is done: the 6.x migration leaves
    the originals behind as .bak, so a stray old name there means nothing.
    """
    ordner = {"outlook": BASE / OUTLOOK_DIR, "teams": BASE / TEAMS_DIR,
              "onedrive": BASE / ONEDRIVE_DIR}
    gefunden = []
    for name, dateien in ALT_STATE.items():
        p = ordner[name]
        if (p / state_db.DB_NAME).exists():
            continue
        if any((p / d).is_file() for d in dateien):
            gefunden.append(name)
    return gefunden


ZEIGER_DATEI = "datenordner.txt"


def alter_zeiger():
    """The 6.x pointer file, when it still names a living archive.

    Up to 6.3.1 a single line at the standard location could send the whole
    archive elsewhere. 7.0 no longer follows it – the paths are settings
    now. Following it silently would be wrong, but so is ignoring it: the
    app would start a SECOND archive next to the real one and fetch
    everything again. So it is read for one purpose only: to say where the
    data is. Nothing is moved, and an explicitly chosen data_dir wins.
    """
    # Truthiness, not membership: save_config writes the whole schema, so
    # the key exists (as "") after the first save – asking "is it present"
    # would disarm this guard the moment anyone touches the settings.
    if settings.data_dir_env() or settings.load(CONFIG_FILE).get("data_dir"):
        return None
    try:
        roh = (standard_data_dir() / ZEIGER_DATEI).read_text(
            encoding="utf-8").strip()
    except OSError:
        return None
    if not roh:
        return None
    ziel = Path(roh).expanduser()
    # Only a target that really still holds an archive: a leftover file
    # pointing into the void must not block anybody.
    if any((ziel / name).is_dir() for name in ALT_ORDNER):
        return ziel
    return None


def altbestand_pinnen():
    """Upgrades keep finding their data without a manual step.

    When the home folder still holds the flat pre-7.0 export folders and no
    data_dir was ever decided, the config pins the data folder to the home
    folder – NOTHING moves, everything is found where it always was. Only
    fresh installs get the split defaults (data/ next to rag_store/). The
    caller logs a one-time hint that splitting is now possible – by moving
    the folders yourself and changing the paths in the settings.

    Deliberately keyed on the raw file: an explicit "Standard" reset in the
    settings writes data_dir="" and must not be overridden here."""
    global BASE, STORE_PFAD, _ALT_GEPINNT
    if settings.data_dir_env() or "data_dir" in settings.load(CONFIG_FILE):
        return False
    if not any((HEIM / name).is_dir() for name in ALT_ORDNER):
        return False
    def pinnen(cfg):
        cfg["data_dir"] = str(HEIM)
    konfiguration_aendern(load_config(), pinnen)
    BASE, STORE_PFAD = _split_pfade(HEIM)
    _ALT_GEPINNT = True
    return True


def lauf_sperren():
    """Every reason why no run may start on this installation – [] when none.

    The one answer both the startup log and the run gate read, so they
    cannot disagree; checked per run, not once, because the user fixes
    these things while the app is open.
    """
    sperren = []
    alt = altbestand_state()
    if alt:
        sperren.append({"k": "srv.legacy.state",
                        "v": {"stores": ", ".join(alt)}})
    zeiger = alter_zeiger()
    if zeiger:
        sperren.append({"k": "srv.layout.pointer",
                        "v": {"pointer": str(standard_data_dir() / ZEIGER_DATEI),
                              "data": str(zeiger), "home": str(HEIM)}})
    return sperren

# Category -> Graph permission. The wizard uses this to check whether the
# pasted token covers what is selected (scp claim in the JWT).
SCOPE_FOR = {
    "mail": "Mail.Read",
    "calendar": "Calendars.Read",
    "contacts": "Contacts.Read",
    "1on1": "Chat.Read",
    "group": "Chat.Read",
    "meeting": "Chat.Read",
    "channels": "ChannelMessage.Read.All",
    "files": "Files.Read.All",
    "sites": "Sites.Read.All",
    "tasks": "Tasks.Read",            # Planner and To Do alike
    "groups": "Group.Read.All",       # Planner: the legacy comments
    "notes": "Notes.Read",            # OneNote
}
# One row per mirror-style source: config switch -> (scope category for the
# token wizard, name in the system report). New sources register here.
SPIEGEL_QUELLEN = (("onedrive_enabled", "files", "onedrive"),
                   ("sharepoint_enabled", "sites", "sharepoint"),
                   ("sharepoint_pages_enabled", "sites", "pages"),
                   ("planner_enabled", "tasks", "planner"),
                   ("todo_enabled", "tasks", "todo"),
                   ("onenote_enabled", "notes", "onenote"))

LABEL_FOR = {
    "mail": "E-Mail", "calendar": "Kalender", "contacts": "Kontakte",
    "1on1": "1:1-Chats", "group": "Gruppenchats",
    "meeting": "Meeting-Chats", "channels": "Team-Kanäle", "files": "OneDrive",
    "sites": "SharePoint", "tasks": "Planner / To Do",
    "groups": "Planner-Kommentare", "notes": "OneNote",
}

# Additional permissions that each cover the required one. The Graph
# Explorer often grants the write variant right away: whoever has
# Mail.ReadWrite may certainly read, but the token then never lists
# Mail.Read. Without this table the wizard would report missing rights
# that are in fact there.
#
# Deliberately generous: a missing warning costs at most a 403 during the
# run (no worse than having no check at all), while a false warning sends
# someone off to hunt in the Graph Explorer for something they already
# have. NOT included are variants that can do less than the export needs:
# Chat.ReadBasic and Mail.ReadBasic deliver no message bodies.
SCOPE_COVERED_BY = {
    "Tasks.Read": ("Tasks.ReadWrite",),
    "Notes.Read": ("Notes.ReadWrite", "Notes.Read.All", "Notes.ReadWrite.All"),
    "Group.Read.All": ("Group.ReadWrite.All",),
    "Mail.Read": ("Mail.ReadWrite", "Mail.Read.Shared", "Mail.ReadWrite.Shared"),
    "Calendars.Read": ("Calendars.ReadWrite", "Calendars.Read.Shared",
                       "Calendars.ReadWrite.Shared"),
    "Contacts.Read": ("Contacts.ReadWrite", "Contacts.Read.Shared",
                      "Contacts.ReadWrite.Shared"),
    "Chat.Read": ("Chat.ReadWrite",),
    # Sites.Read.All also reads files (libraries ARE drives), and the
    # write variants read all the more.
    "Files.Read.All": ("Files.ReadWrite.All", "Sites.Read.All",
                       "Sites.ReadWrite.All", "Sites.FullControl.All"),
    "Sites.Read.All": ("Sites.ReadWrite.All", "Sites.Manage.All",
                       "Sites.FullControl.All"),
    # Graph also allows reading channel messages with the group permissions.
    "ChannelMessage.Read.All": ("Group.Read.All", "Group.ReadWrite.All"),
}


# The "Modify permissions" tab in the Graph Explorer only lists the rights
# for the query currently sitting in the address bar. Whoever never ran a
# mail query there is simply never offered Mail.Read and searches in vain.
# Hence, for every right, the query that makes it visible.
SCOPE_QUERY = {
    "Mail.Read": "https://graph.microsoft.com/v1.0/me/messages?$top=1",
    "Calendars.Read": "https://graph.microsoft.com/v1.0/me/events?$top=1",
    "Contacts.Read": "https://graph.microsoft.com/v1.0/me/contacts?$top=1",
    "Chat.Read": "https://graph.microsoft.com/v1.0/me/chats?$top=1",
    "ChannelMessage.Read.All": "https://graph.microsoft.com/v1.0/me/joinedTeams",
    "Files.Read.All": "https://graph.microsoft.com/v1.0/me/drive/root/children?$top=1",
    "Sites.Read.All": "https://graph.microsoft.com/v1.0/sites?search=*",
    "Tasks.Read": "https://graph.microsoft.com/v1.0/me/planner/plans?$top=1",
    "Notes.Read": "https://graph.microsoft.com/v1.0/me/onenote/notebooks?$top=1",
    "Group.Read.All": "https://graph.microsoft.com/v1.0/groups?$top=1",
    "User.Read": "https://graph.microsoft.com/v1.0/me",
}


def scope_missing(needed_scopes, have):
    """Which of the required permissions does the token not cover?"""
    have = set(have)
    return sorted(s for s in needed_scopes
                  if s not in have
                  and not have.intersection(SCOPE_COVERED_BY.get(s, ())))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def _merge_defaults(base, loaded):
    """Lay the loaded values over the defaults, recursing one level deep.

    This way no key is ever missing after an update, and a hand-trimmed
    app_config.json stays valid. Deep copy, because a later
    cfg["schedule"]["enabled"] = True would otherwise change DEFAULT_CONFIG
    itself – the defaults would then be skewed for the rest of the runtime.
    """
    out = copy.deepcopy(base)
    for k, v in (loaded or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = {**out[k], **v}
        elif k in out:
            out[k] = v
    return out


def load_config(path=None):
    path = Path(path or CONFIG_FILE)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = {}
    return _merge_defaults(DEFAULT_CONFIG, loaded)


# The configuration exists three times – the dict in memory, the file on
# disk, settings.py's cache of that file for the subprocesses – and the
# threading server may change it from two handlers at once. So there is ONE
# way to change it: konfiguration_aendern() edits the dict, writes the file
# and drops the cache under one lock. Nothing else writes the file at run
# time; save_config() is the plumbing underneath it.
_CONFIG_LOCK = threading.RLock()


def save_config(cfg, path=None):
    path = Path(path or CONFIG_FILE)
    # A tmp name of its own per write: two writers must never share a
    # half-written file even if one of them bypasses the lock.
    tmp = path.with_suffix(f".json.{os.getpid()}-{threading.get_ident()}.tmp")
    with _CONFIG_LOCK:
        try:
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def konfiguration_aendern(cfg, aendern):
    """The one way a setting changes: `aendern(cfg)` edits the live dict,
    then the file is written and settings.py's cache dropped – all three
    copies of the truth move together, and never half-way past another
    handler's change."""
    with _CONFIG_LOCK:
        aendern(cfg)
        save_config(cfg)
        settings.reset()


def _clean_categories(values, allowed):
    """Only known categories, in the order of `allowed`."""
    picked = {str(v).strip().lower() for v in (values or [])}
    return [k for k in allowed if k in picked]


def _ohne_kennung(name):
    """"Title__k3y" -> "Title": the exports suffix a short id so renamed
    units keep their folder; the rules and the export list speak titles."""
    kopf, trenner, _rest = name.rpartition("__")
    return kopf if trenner else name


def _clean_datum(wert):
    """A calendar day as YYYY-MM-DD, or empty."""
    roh = str(wert or "").strip()
    try:
        return datetime.strptime(roh, "%Y-%m-%d").date().isoformat() if roh else ""
    except ValueError:
        return ""


def _clean_endungen(values):
    """File extensions from list or text: lowercase, no dot, no duplicates."""
    if isinstance(values, str):
        values = values.replace("\n", ",").split(",")
    return sorted({str(v).strip().lower().lstrip(".")
                   for v in (values or []) if str(v).strip().strip(".")})


def _clean_zeilen(values):
    """One entry per line: lowercased, no duplicates, no blank lines.

    Not comma-separated like the folder list – names contain commas, and
    "Schilling, Nico" would otherwise be two entries, neither of which
    matches.
    """
    if isinstance(values, str):
        values = values.splitlines()
    return sorted({str(v).strip().lower() for v in (values or []) if str(v).strip()})


def _clean_folders(values):
    """Folder names from list or text (comma-separated): lowercased, no dupes.

    outlook_export.py compares display names case-insensitively, so we
    lowercase here already – otherwise the interface would show something
    other than what is compared against in the end.
    """
    if isinstance(values, str):
        values = values.replace("\n", ",").split(",")
    return sorted({str(v).strip().lower() for v in (values or []) if str(v).strip()})


def auswahlregeln(cfg, roh=None, namen=None):
    """The rules by which the export picks folders.

    Same order as outlook_export.aktuelle_regeln – the rules apply, and only
    as long as none exist does the old name list still take effect. Both
    live in one place here so the preview does not calculate differently
    from the run it predicts.

    `roh` and `namen` are whatever currently sits in the form fields:
    whoever types a rule wants to check it before saving it.
    """
    regeln = folders.lies_regeln(
        (cfg.get("folder_rules") if roh is None else roh) or "")
    if regeln:
        return regeln
    return folders.aus_namensliste(
        _clean_folders(cfg.get("skip_folders") if namen is None else namen))


def kalenderregeln(cfg, daten=None, roh=None):
    """The same for the calendars – see outlook_export.kalender_regeln.

    Without rules of its own it stays with the default calendar. That
    depends on the data, because only the list says which one that is;
    hence it is passed in here.
    """
    regeln = folders.lies_regeln(
        (cfg.get("calendar_rules") if roh is None else roh) or "")
    if regeln:
        return regeln
    return folders.nur_standard((daten or {}).get("ordner", []))


def notizbuchregeln(cfg, roh=None):
    """The same for the OneNote notebooks – see onenote_export.
    notizbuch_regeln. Empty means every notebook."""
    return folders.lies_regeln(
        (cfg.get("onenote_rules") if roh is None else roh) or "")


def teamsregeln(cfg, roh=None):
    """Teams conversations – paths "1on1/<title>", "group/<title>",
    "meeting/<title>", "channels/<team>/<channel>". Empty means every
    conversation of the ticked categories."""
    return folders.lies_regeln(
        (cfg.get("teams_rules") if roh is None else roh) or "")


def sharepointregeln(cfg, roh=None):
    """SharePoint libraries – path rules over "<site>/<library>/<path>" on
    top of the URL list. Empty means every path."""
    return folders.lies_regeln(
        (cfg.get("sharepoint_rules") if roh is None else roh) or "")


def todoregeln(cfg, roh=None):
    """To Do lists by title. Empty means every list."""
    return folders.lies_regeln(
        (cfg.get("todo_rules") if roh is None else roh) or "")


# --------------------------------------------------------------------------
# Token: paste, check, store
# --------------------------------------------------------------------------
def normalize_token(raw):
    """Clean a pasted token: quotes, "Bearer ", line breaks.

    The Graph Explorer often delivers the token with line breaks from the
    copy field; a JWT itself contains no whitespace, so all of it may go.
    """
    if not raw:
        return ""
    val = str(raw).strip().strip('"').strip("'").strip()
    if val.lower().startswith("bearer "):
        val = val[7:]
    return re.sub(r"\s+", "", val)


def decode_jwt(token):
    """Read a JWT's payload without verifying the signature. {} if not one.

    Display only (account, expiry, permissions) – the token is validated
    by Graph on the first call anyway.
    """
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def token_status(token, now=None, needed=()):
    """State of the token for the interface.

    `needed` are categories (mail, 1on1, …); missing permissions for them
    are named so the wizard can say what still needs consenting to in the
    Graph Explorer. If the token cannot be read as a JWT it counts as
    present with unknown expiry – it is then simply tried.
    """
    now = now if now is not None else time.time()
    out = {"present": bool(token), "valid": False, "expired": False,
           "readable": False, "account": None, "name": None,
           "expires_at": None, "expires_in_minutes": None,
           "scopes": [], "missing": []}
    if not token:
        return out
    claims = decode_jwt(token)
    if not claims:
        out["valid"] = True          # unreadable but present -> just try it
        return out
    out["readable"] = True
    out["account"] = (claims.get("upn") or claims.get("preferred_username")
                      or claims.get("unique_name"))
    out["name"] = claims.get("name")
    out["scopes"] = sorted((claims.get("scp") or "").split())
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        out["expires_at"] = datetime.fromtimestamp(exp, UTC).isoformat()
        out["expires_in_minutes"] = int((exp - now) // 60)
        out["expired"] = exp <= now
    out["valid"] = not out["expired"]
    # Only report missing rights when the scp claim was actually read –
    # otherwise a token without readable claims would look as if everything
    # were missing.
    if out["scopes"]:
        want = {SCOPE_FOR[c] for c in needed if c in SCOPE_FOR}
        out["missing"] = scope_missing(want, out["scopes"])
    return out


def read_token(path=None):
    try:
        return normalize_token(Path(path or TOKEN_FILE).read_text(encoding="utf-8"))
    except OSError:
        return ""


def write_token(token, path=None):
    """Store the token – readable only by the owning user."""
    token = normalize_token(token)
    p = Path(path or TOKEN_FILE)
    p.write_text(token + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass                          # e.g. Windows/FAT: permissions not settable
    return token


# --------------------------------------------------------------------------
# Ollama (HTTP in ollama_client.py)
# --------------------------------------------------------------------------
_hat_modell = ollama_client.hat_modell


def check_ollama(url, model, chat_model=None, timeout=1.5):
    """Is Ollama running – and which of the two models are available?

    The embedding model carries the semantic search, the chat model the
    worded answer. Reported separately: whoever has only the first should
    be able to search without the interface promising an answer no model
    can produce.
    """
    out = {"running": False, "models": [], "has_model": False,
           "has_chat_model": False, "error": None,
           "model": model, "chat_model": chat_model, "url": url}
    try:
        names = ollama_client.tags(url, timeout=timeout)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    out["running"] = True
    out["models"] = sorted(names)
    out["has_model"] = _hat_modell(names, model)
    out["has_chat_model"] = _hat_modell(names, chat_model)
    return out


def ollama_hint():
    """Installation hint matching the operating system – as text keys.

    The sentences live in the language files; here we only decide which
    steps apply and which values to fill them with.
    """
    sysname = platform.system()
    if sysname == "Darwin":
        return {"os": "macOS", "url": OLLAMA_SITE,
                "steps": ["wizard.ollama.step.mac1", "wizard.ollama.step.mac2",
                          "wizard.ollama.step.recheck"],
                "pkg": "brew install --cask ollama"}
    if sysname == "Windows":
        return {"os": "Windows", "url": OLLAMA_SITE,
                "steps": ["wizard.ollama.step.win1", "wizard.ollama.step.win2",
                          "wizard.ollama.step.recheck"],
                "pkg": "winget install Ollama.Ollama"}
    return {"os": sysname or "Linux", "url": OLLAMA_SITE,
            "steps": ["wizard.ollama.step.linux1", "wizard.ollama.step.linux2",
                      "wizard.ollama.step.recheck"],
            "pkg": None}


# --------------------------------------------------------------------------
# State of exports and index
# --------------------------------------------------------------------------
def _mtime_iso(p):
    try:
        return datetime.fromtimestamp(Path(p).stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def _einheiten_stand(wurzel):
    """The newest per-unit state.db – plan, list, notebook – dates the
    last run of an export that keeps one folder per unit."""
    try:
        pfade = list(wurzel.glob(f"*/{state_db.DB_NAME}"))
    except OSError:
        return None
    return _mtime_iso(max(pfade, key=lambda pf: pf.stat().st_mtime)) \
        if pfade else None


def _sharepoint_stand(wurzel):
    """The newest per-library state.db dates the last mirror run."""
    try:
        pfade = list(wurzel.glob(f"*/*/{state_db.DB_NAME}"))
    except OSError:
        return None
    if not pfade:
        return None
    return _mtime_iso(max(pfade, key=lambda pf: pf.stat().st_mtime))


def export_status(cfg):
    """Do the export folders exist, and when did they last run?

    The timestamp comes from each export's progress file – folder size is
    deliberately left out: a mailbox can hold tens of gigabytes, and
    counting that on every status poll would be expensive.
    """
    teams = BASE / TEAMS_DIR
    outlook = BASE / OUTLOOK_DIR
    onedrive = BASE / ONEDRIVE_DIR
    sharepoint = BASE / SHAREPOINT_DIR
    seiten = BASE / SHAREPOINT_PAGES_DIR
    planner = BASE / PLANNER_DIR
    todo = BASE / TODO_DIR
    onenote = BASE / ONENOTE_DIR
    return {
        # The state.db dates the last run: every export writes it at the
        # end, even when nothing new arrived.
        "teams": {"dir": str(teams), "exists": teams.is_dir(),
                  "last_run": _mtime_iso(teams / state_db.DB_NAME)},
        "outlook": {"dir": str(outlook), "exists": outlook.is_dir(),
                    "last_run": _mtime_iso(outlook / state_db.DB_NAME)},
        "onedrive": {"dir": str(onedrive), "exists": onedrive.is_dir(),
                     "last_run": _mtime_iso(onedrive / state_db.DB_NAME)},
        # One inventory per mirrored library – the newest one dates the run.
        "sharepoint": {"dir": str(sharepoint), "exists": sharepoint.is_dir(),
                       "last_run": _sharepoint_stand(sharepoint)},
        "pages": {"dir": str(seiten), "exists": seiten.is_dir(),
                  "last_run": _mtime_iso(seiten / state_db.DB_NAME)},
        # One state.db per plan, list or notebook – the newest dates the run.
        "planner": {"dir": str(planner), "exists": planner.is_dir(),
                    "last_run": _einheiten_stand(planner)},
        "todo": {"dir": str(todo), "exists": todo.is_dir(),
                 "last_run": _einheiten_stand(todo)},
        "onenote": {"dir": str(onenote), "exists": onenote.is_dir(),
                    "last_run": _einheiten_stand(onenote)},
    }


_ZAEHLUNG = {}          # db path -> (file fingerprint, counts)


def _faehigkeiten(spalten):
    """What an index with these columns can do, as the page reads it."""
    aus = spalten & {"thread", "gone", "ext", "key", "who_mail", "domains"}
    if set(store_layout.MAIL_NEU) <= spalten:
        aus.add("mail_lines")
    return sorted(aus)


def _zaehle(db):
    """Chunks and messages in the index – cached.

    The interface polls the state every few seconds. COUNT(DISTINCT uid)
    scans the whole index (about 30 ms on 270,000 rows); repeating that
    every time would be waste, because the numbers only change when the
    file does. Size and modification time are the fingerprint for that.
    """
    try:
        s = db.stat()
        kennung = (s.st_mtime_ns, s.st_size)
    except OSError:
        return {"chunks": 0, "messages": 0}
    alt = _ZAEHLUNG.get(str(db))
    if alt and alt[0] == kennung:
        return alt[1]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        zahlen = {
            "chunks": con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            # What the user calls a "message": a mail, a chat, an
            # appointment. Long messages sit in the index as several
            # chunks – the row count would thus be far higher than what
            # someone expects to find again in their archive.
            "messages": con.execute("SELECT COUNT(DISTINCT uid) FROM chunks").fetchone()[0],
            # What this index can do. An older one knows nothing of threads
            # and deletions; the interface then simply does not offer them
            # instead of letting the user run into an error. `mail_lines`
            # is no column but the one flag for the mail lines of 13.0: all
            # of store_layout.MAIL_NEU – the set the run gate rebuilds for.
            "features": _faehigkeiten({r[1] for r in con.execute("PRAGMA table_info(chunks)")}),
        }
    finally:
        con.close()
    _ZAEHLUNG[str(db)] = (kennung, zahlen)
    return zahlen


# The export folder behind each source, by the step registry's name (the
# `ordner` of steps.PRUEFUNGEN) – read off settings.QUELLEN, the one table.
EXPORT_ORDNER = {quelle: ordner for quelle, (ordner, _index) in settings.QUELLEN.items()}


def archiv_bericht_pfad():
    """Where the inward check (archive_check.py) writes its report: next to
    the settings of the open profile – read at call time, since the home
    folder is settled after import (profiles, --data-dir)."""
    return HEIM / "archivpruefung.json"


def archiv_bericht():
    """The last inward check, or None."""
    try:
        return json.loads(archiv_bericht_pfad().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pruefungen(cfg):
    """The last completeness balance of every source, and whether the
    source is in use – so the page draws a row for a used source even
    before its first check, and none for a source nobody uses. Reports are
    only ever written at the push of a button: the check queries
    Microsoft, and nothing should do that unasked because a view opens."""
    out = {}
    for e in steps_mod.PRUEFUNGEN:
        with state_db.StateDb(BASE / EXPORT_ORDNER[e["ordner"]]) as db:
            out[e["quelle"]] = {"bericht": completeness.lesen(db, e["quelle"]),
                                "genutzt": bool(e["nutzt"](cfg))}
    return out


# The analytics numbers come materialised from the index (analytics_db):
# the index run writes them, here we only read. "Refresh" rebuilds them on
# request – the only moment any computing happens here.
_ANALYTICS_LOCK = threading.Lock()


def analytics_ordner():
    return {"teams": BASE / TEAMS_DIR, "outlook": BASE / OUTLOOK_DIR,
            "onedrive": BASE / ONEDRIVE_DIR,
            "sharepoint": BASE / SHAREPOINT_DIR,
            "pages": BASE / SHAREPOINT_PAGES_DIR,
            "planner": BASE / PLANNER_DIR, "todo": BASE / TODO_DIR,
            "onenote": BASE / ONENOTE_DIR}


def analytics_daten(cfg, neu=False):
    """The Analytics payload: the materialised block plus the completeness
    reports, with the person skip list applied at read time so a settings
    change acts immediately."""
    store = STORE_PFAD
    daten = None if neu else analytics_db.lies(store)
    if daten is None:
        # First call after an update (or an explicit refresh): compute once,
        # then the block sits in the index again.
        with _ANALYTICS_LOCK:
            daten = (None if neu else analytics_db.lies(store)) \
                or analytics_db.baue(store, analytics_ordner())
    out = dict(daten or {})
    out["exists"] = daten is not None
    aus = {str(n).strip().lower() for n in (cfg.get("analytics_skip") or [])}
    out["top_personen"] = [pe for pe in out.get("top_personen") or []
                           if pe["who"].strip().lower() not in aus][:10]
    out["pruefungen"] = pruefungen(cfg)
    out["archiv"] = archiv_bericht()
    return out


def store_status(cfg):
    """State of the index: how much is in it, with or without embeddings."""
    store = STORE_PFAD
    db = store_layout.db_path(store)
    info = store_layout.info(store)
    out = {"dir": str(store), "exists": db.exists(), "chunks": 0, "messages": 0,
           "features": [],
           "semantic": store_layout.vectors_path(store, info) is not None,
           "built_at": _mtime_iso(db), "model": info.get("model")}
    if not out["exists"]:
        return out
    try:
        out.update(_zaehle(db))
    except sqlite3.Error as e:
        out["error"] = str(e)
    return out


# --------------------------------------------------------------------------
# Error report
#
# An error like "BrokenProcessPool" cannot be answered without the
# environment: operating system, bundled or script, how many cores, what
# the index currently holds. Asking for these details by hand costs two
# rounds of e-mail; here they stand ready.
#
# This app's corpus is mail and chat – the log inevitably names addresses
# and paths. Hence two precautions, and both are meant seriously: whatever
# is obviously personal is replaced beforehand (below), and whatever
# remains is shown to the human before sending, editable. The app sends
# nothing itself; it merely fills in a form on GitHub.
# --------------------------------------------------------------------------
# How much log goes into the report. Capped because GitHub carries the
# prefilled text in the URL and rejects long URLs – and because the last
# lines are the interesting ones.
BERICHT_ZEILEN = 80
BERICHT_ZEICHEN = 3000

_MAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+")
# C:\Users\name\… and /Users/name/…, /home/name/… – the login name sits in
# almost every path a log mentions.
_HOME_WIN = re.compile(r"([A-Za-z]:\\Users\\)[^\\/\r\n]+", re.I)
_HOME_NIX = re.compile(r"(/(?:Users|home)/)[^/\s:\"']+")


def anonymisiere(text):
    """Replace e-mail addresses and user names in paths.

    Deliberately coarse and with no claim to completeness: no pattern can
    recognise folder names, subject lines or display names. That is not an
    oversight but the division of labour – the machine removes the certain
    part, the rest is read by the human, who sees the text in the window
    before sending.
    """
    text = _MAIL.sub("…@…", text or "")
    text = _HOME_WIN.sub(r"\1…", text)
    return _HOME_NIX.sub(r"\1…", text)


def gekuerzt(text, zeilen=BERICHT_ZEILEN, zeichen=BERICHT_ZEICHEN):
    """The last lines – that is where what went wrong is written."""
    alle = (text or "").splitlines()
    weg = max(0, len(alle) - zeilen)
    rest = "\n".join(alle[weg:])
    if len(rest) > zeichen:
        rest = rest[-zeichen:]
        weg = weg or 1
    return (f"[… {weg} ältere Zeilen ausgelassen …]\n" + rest) if weg else rest


# Settings whose CONTENT names someone – folder names, one's own name in
# analytics_skip, the employer in the tenant. The report only says THAT
# they deviate and to what extent, never the value itself.
_UMFANG_ZEILEN = {"folder_rules", "calendar_rules", "onedrive_rules",
                  "onenote_rules",
                  "sharepoint_urls", "sharepoint_pages_urls", "planner_urls"}
_UMFANG_LISTE = {"skip_folders", "filetype_hidden", "analytics_skip"}
_NUR_GESETZT = {"client_id", "tenant"}
# Already appear as their own line in the report – do not list twice.
_SCHON_BERICHTET = {"outlook_categories", "teams_categories", "auth_mode"}


def einstellungs_abweichungen(cfg):
    """What deviates from the defaults – compact and without naming content.

    An error often hangs on a changed setting, and nobody mentions it
    unprompted. Paths are not in the schema and thus never even appear
    here; rule and name lists shrink to their extent.
    """
    aus = []
    for key, vorgabe in settings.VORGABEN.items():
        if key in _SCHON_BERICHTET:
            continue
        wert = cfg.get(key, vorgabe)
        if wert == vorgabe:
            continue
        if key in _UMFANG_ZEILEN:
            n = len([z for z in str(wert).splitlines() if z.strip()])
            aus.append(f"{key}: {n} " + ("Zeile" if n == 1 else "Zeilen"))
        elif key in _UMFANG_LISTE:
            n = len(wert or [])
            aus.append(f"{key}: {n} " + ("Eintrag" if n == 1 else "Einträge"))
        elif key in _NUR_GESETZT:
            aus.append(f"{key}: gesetzt")
        elif isinstance(vorgabe, dict):
            teile = [f"{k}={json.dumps((wert or {}).get(k))}"
                     for k in vorgabe if (wert or {}).get(k) != vorgabe[k]]
            aus.append(f"{key}: " + ", ".join(teile))
        else:
            aus.append(f"{key}={json.dumps(wert, ensure_ascii=False)}")
    return aus


def systemangaben(status, lang=None, cfg=None, datenordner=None, vorgabe=None):
    """The facts of a report as [{"k": text key, "v": value}, …].

    Translation happens only in the interface – as with the log lines.
    That way the reporter reads the report in their language rather than
    in that of a server, which has none.
    """
    store = status.get("store") or {}
    oll = status.get("ollama") or {}
    cfg = cfg or status.get("config") or {}
    letzter = (status.get("jobs") or {}).get("last") or {}

    def zeile(k, v):
        # Only the stem of the key; the interface prefixes "report.sys." and
        # translates – as with the log lines, nothing that has a language is
        # named here.
        return {"k": k, "v": str(v)}

    art = "Bündel" if FROZEN else "Skript"
    kerne = os.cpu_count() or "?"
    kats = sorted((cfg.get("outlook_categories") or [])
                  + (cfg.get("teams_categories") or []))
    kats += [name for flag, _kategorie, name in SPIEGEL_QUELLEN
             if cfg.get(flag)]
    angaben = [
        zeile("version", f"{version.VERSION}"
              + (f" build {version.build()}" if version.build() else "") + f" ({art})"),
        zeile("os", f"{platform.platform()} / {platform.machine()}"),
        zeile("python", platform.python_version()),
        zeile("cores", kerne),
        zeile("lang", lang or i18n.FALLBACK),
        zeile("auth", cfg.get("auth_mode") or "token"),
        zeile("categories", ", ".join(kats) or "–"),
        zeile("index", f'{store.get("chunks", 0)} / {store.get("messages", 0)}, '
                       f'{"hybrid" if store.get("semantic") else "BM25"}'),
        zeile("model", store.get("model") or cfg.get("embed_model") or "–"),
        zeile("ollama", f'{"läuft" if oll.get("running") else "aus"}, '
                        f'Modell {"da" if oll.get("has_model") else "fehlt"}'),
    ]
    abweichungen = einstellungs_abweichungen(cfg)
    if abweichungen:
        angaben.append(zeile("settings", "; ".join(abweichungen)))
    if letzter:
        angaben.append(zeile("lastjob", f'{letzter.get("label", "?")}: '
                                        f'{"ok" if letzter.get("ok") else "Fehler"}'))
    # The data folder only when it is NOT the default: otherwise it says
    # nothing the lines above do not, and merely carries a user name along.
    ordner = str(BASE if datenordner is None else datenordner)
    if ordner != str(HEIM / DATEN_UNTERORDNER if vorgabe is None else vorgabe):
        angaben.append(zeile("datadir", anonymisiere(ordner)))
    return angaben


def fehlerbericht(status, log_text="", hint="", lang=None, cfg=None):
    """Everything the interface needs for the GitHub form."""
    titel = anonymisiere(str(hint or "").strip()).strip()
    return {
        "system": systemangaben(status, lang, cfg),
        "log": anonymisiere(gekuerzt(log_text)),
        "title": titel[:120],
        "url": f"https://github.com/{version.REPO}/issues/new",
    }


# --------------------------------------------------------------------------
# Steps of a run (pure – no side effects, hence easy to test)
# --------------------------------------------------------------------------
def script_argv(name, *args):
    """Command line for one of the subprograms.

    As a script: python3 <name>.py …
    Bundled: our own executable with "--run <name>" – there is no Python
    interpreter and no .py files there any more, the modules sit in the
    bundle and are imported by run_bundled().
    """
    if name not in RUNNABLE:
        raise ValueError(f"Unbekanntes Teilprogramm: {name}")
    if FROZEN:
        return [sys.executable, "--run", name, *(str(a) for a in args)]
    return [sys.executable, str(RES / f"{name}.py"), *(str(a) for a in args)]


def run_bundled(name, argv):
    """Start a subprogram inside the bundle (counterpart to script_argv).

    The subprograms read their arguments from sys.argv themselves, so the
    list is arranged beforehand to look as it would on a direct call.
    """
    if name not in RUNNABLE:
        raise SystemExit(f"Unbekanntes Teilprogramm: {name}. "
                         f"Möglich: {', '.join(RUNNABLE)}")
    sys.argv = [f"{name}.py", *argv]
    importlib.import_module(name).main()


def calendar_file(cfg):
    return STORE_PFAD / "calendar.json"


def calendar_plan(cfg):
    """What the calendar step has to do in this run.

    Returns (noetig, mit_mails). Appointments and contacts come solely
    from the Outlook export – if neither category is selected, there is
    nothing to build. Reconstructing deleted appointments additionally
    reads every single .eml; that only pays off when this run also fetched
    mail. Someone exporting only contacts would otherwise wait minutes for
    an evaluation nothing can have changed in.
    """
    cats = set(_clean_categories(cfg.get("outlook_categories"),
                                 ["mail", "calendar", "contacts"]))
    return bool(cats & {"calendar", "contacts"}), "mail" in cats


HISTORIE_WAHL = ("off", "30", "90", "365", "forever")


def historie_tage(cfg):
    """The search history's retention as Fallbuch.aufraeumen takes it:
    None keeps everything, 0 keeps nothing, else days."""
    wahl = str(cfg.get("search_history") or "90").strip().lower()
    if wahl == "forever":
        return None
    if wahl == "off":
        return 0
    try:
        return max(1, int(wahl))
    except ValueError:
        return 90


def fall_export_basis(cfg):
    """Where case exports land: the configured folder, else "Munimentum
    cases" in the user's Documents folder (the home folder without one)."""
    eigen = str(cfg.get("case_export_dir") or "").strip()
    if eigen:
        return Path(eigen).expanduser()
    dokumente = Path.home() / "Documents"
    return (dokumente if dokumente.is_dir() else Path.home()) / "Munimentum cases"


def _auth_env(cfg):
    """Pass the sign-in on to the subprocesses.

    As with the categories: the app keeps its configuration in memory and
    hands it over as environment variables instead of relying on
    settings.py reading the same file the same way at the same time.
    """
    env = {"GRAPH_AUTH": ("login" if str(cfg.get("auth_mode", "token")).lower()
                          == "login" else "token")}
    if str(cfg.get("folder_rules") or "").strip():
        env["FOLDER_RULES"] = cfg["folder_rules"]
    if str(cfg.get("calendar_rules") or "").strip():
        env["CALENDAR_RULES"] = cfg["calendar_rules"]
    for schluessel, name in (("client_id", "GRAPH_CLIENT_ID"),
                             ("tenant", "GRAPH_TENANT")):
        wert = str(cfg.get(schluessel) or "").strip()
        if wert:
            env[name] = wert
    return env


def _quellname(eintrag):
    """How a message names a registry entry's source: the literal, or the
    text key as an atom the page translates."""
    q = eintrag.get("quelle") or eintrag["key"]
    return {"k": q, "v": {}} if "." in q else q


def build_steps(cfg, angefragt, *, embeddings=True, token="",
                reconstruct=None, nur_einheit=None, legacy_comments=False,
                sync_now=False, calendar_full=False, full_sync=False,
                resync=False, archiv=None, nachgeholt=None, nachholen=None,
                resync_ordner=None, fall_export=None):
    """Assemble the command lines for a run – from the registry.

    What a step is lives entirely in steps.REGISTRY; here we only hand in
    the app paths, the shared environment and the request – one dict of
    registry request keys to booleans, passed through untouched so a new
    entry needs no new parameter anywhere. The export scripts receive the
    selection via environment variables – so they run without any prompt,
    with exactly what is ticked in the interface.
    """
    # None means "as configured". The caller only sets it when it knows
    # better – say because this run fetched no mail at all.
    if reconstruct is None:
        reconstruct = bool(cfg.get("calendar_reconstruct", True))
    ctx = {
        "embeddings": embeddings, "reconstruct": reconstruct,
        "nur_einheit": nur_einheit,
        # The Planner settings' "Read legacy comments again" button.
        "legacy_comments": bool(legacy_comments),
        # "Sync now" for a whole source: the cadences step aside once.
        "sync_now": bool(sync_now),
        # "Force full sync" for a source: the export forgets its change
        # pointers and reads everything again (FULL_SYNC, see
        # export_util.voll_neu); the cadences step aside as well.
        "full_sync": bool(full_sync),
        # "Fetch now" / "Fetch again": the export forgets its change
        # pointers but keeps every file's version, so only what is not
        # here is fetched (RESYNC, see export_util.abgleich).
        "resync": bool(resync),
        # "Fetch now" from the balance: the folders with something open –
        # the resync lists those alone (RESYNC_FOLDERS, Outlook).
        "resync_ordner": [str(p) for p in (resync_ordner or []) if str(p).strip()],
        # An archive action's request: the source and, for the note, the
        # kinds to note (archive_check.py --aktion).
        "archiv": dict(archiv or {}),
        # "Fetch again": {quelle, liste} – the source whose step fetches
        # exactly the files in the list (FETCH_LIST), ticked or not.
        "nachholen": dict(nachholen or {}),
        # A case export: {faelle, fall, ziel, lang, res} for case_export.py
        # – the ZIP is named before the run starts.
        "fall_export": dict(fall_export or {}),
        # Source -> when its last resync completed, for the archive
        # check's "still missing after a fetch" verdict.
        "nachgeholt": dict(nachgeholt or {}),
        # The calendar's "read in full" button: only the calendar runs,
        # window and change tokens ignored once.
        "calendar_full": bool(calendar_full),
        "cats_outlook": (["calendar"] if calendar_full else
                         _clean_categories(cfg["outlook_categories"],
                                           ["mail", "calendar", "contacts"])),
        "cats_teams": _clean_categories(cfg["teams_categories"],
                                        ["1on1", "group", "meeting",
                                         "channels"]),
    }
    pfade = {
        "outlook": OUTLOOK_DIR, "teams": TEAMS_DIR, "onedrive": ONEDRIVE_DIR,
        "sharepoint": SHAREPOINT_DIR, "sharepoint_pages": SHAREPOINT_PAGES_DIR,
        "planner": PLANNER_DIR, "todo": TODO_DIR, "onenote": ONENOTE_DIR,
        "store": str(STORE_PFAD),
        "store_db": store_layout.db_path(STORE_PFAD),
        # ONE absolute path for both: the step writes it, the skip target,
        # the status and /api/v1/calendar read it. Spelled relative it landed
        # under the subprocess cwd (BASE) instead of the index folder.
        "calendar_json": str(calendar_file(cfg)),
        # The inward check's report – next to the settings, not in an
        # export folder: it speaks about all of them.
        "archiv_bericht": str(archiv_bericht_pfad()),
        "calendar_file": calendar_file(cfg),
    }
    # Subprocesses (and auth's MSAL cache) find configuration and token via
    # MUNIMENTUM_HOME in the fixed home folder – independent of the data dir.
    base_env = {"PYTHONUNBUFFERED": "1", "MUNIMENTUM_HOME": str(HEIM),
                "EXPORT_WORKERS": str(cfg.get("workers", 4)),
                "MIRROR_WORKERS": str(cfg.get("mirror_workers") or 8),
                **_auth_env(cfg)}
    if token:
        base_env["GRAPH_TOKEN"] = token
    return steps_mod.baue(cfg, ctx, pfade, base_env, script_argv, angefragt)


cadence_faellig = export_util.cadence_faellig


def due_now(last_run, interval_minutes, now):
    """Is the next scheduled run due? (last_run None = immediately)"""
    if last_run is None:
        return True
    return now >= last_run + max(1, int(interval_minutes)) * 60


# --------------------------------------------------------------------------
# Schedule: runs only while the app is open
# --------------------------------------------------------------------------
class Scheduler(threading.Thread):
    """Kicks off export + index at a fixed interval.

    Deliberately tied to the app's runtime (no launchd/Task Scheduler): the
    token is fetched by hand and is typically valid for about an hour – a
    schedule that keeps running in the background without an open interface
    would mostly produce expired tokens nobody sees.
    """

    TICK = 10                       # seconds between two due-date checks

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.last_run = None
        self.last_result = None

    @property
    def plan(self):
        return self.app.cfg["schedule"]

    def next_due(self):
        if not self.plan.get("enabled"):
            return None
        if self.last_run is None:
            return time.time()
        return self.last_run + max(1, int(self.plan.get("interval_minutes", 60))) * 60

    def reset(self):
        """After a change to the plan: count the interval afresh from now."""
        self.last_run = time.time() if self.plan.get("enabled") else None

    def run(self):
        while not self.stop_event.wait(self.TICK):
            try:
                self._tick()
            except Exception as e:
                self.app.jobs.logk("srv.sched.error", "err",
                                   error=f"{type(e).__name__}: {e}")

    def _tick(self):
        plan = self.plan
        if not plan.get("enabled") or self.app.jobs.busy:
            return
        if not due_now(self.last_run, plan.get("interval_minutes", 60), time.time()):
            return
        self.last_run = time.time()
        token = read_token()
        st = token_status(token)
        if not st["valid"]:
            self.app.jobs.logk("srv.sched.notoken", "warn")
            self.app.jobs.token_expired = True
            return
        # Calendar only when Outlook runs along – its data comes exclusively
        # from there – and only when the selection yields anything.
        noetig, mit_mails = calendar_plan(self.app.cfg)
        kalender = bool(plan.get("outlook", True) and plan.get("calendar", True) and noetig)
        cfg = self.app.cfg
        anfrage = dict(steps_mod.plan_anfrage(plan, cfg), calendar=kalender)
        ok, why = self.app.launch(anfrage, origin="schedule",
                                  reconstruct=None if mit_mails else False,
                                  label="job.scheduled")
        if not ok:
            self.app.jobs.logk("srv.sched.skipped", "warn", why=why)


# --------------------------------------------------------------------------
# MCP server as a subprocess
# --------------------------------------------------------------------------
def mcp_client_config(cfg, port):
    """Ready-made entries for Claude Code (HTTP) and Claude Desktop (stdio).

    Built server-side, because only here is it known how to invoke the
    subprogram (script or bundled file) and where the data lives. The stdio
    variant gets absolute paths: Claude starts it in an unknown working
    directory.
    """
    # The stdio entry is bound to its profile and named after it – the
    # first profile keeps the plain name, so a 9.x entry is replaced, not
    # joined, when the snippet is copied again. The HTTP endpoint is the
    # app's: it serves whichever profile is open, so its name never changes.
    name = ("munimentum" if PROFIL == settings.STANDARD_PROFIL
            else f"munimentum-{PROFIL}")
    if profile_moeglich():
        # The profile is the whole address: the server takes folders,
        # model, Ollama address and port from that profile's settings.
        eintrag = {"command": None, "args": script_argv(
            "mcp_server", "--transport", "stdio", "--profile", PROFIL)}
    else:
        # One archive under --data-dir: no profile to name, so the paths
        # and the home folder go along.
        argv = script_argv("mcp_server", "--transport", "stdio",
                           "--data-dir", str(BASE), "--store", str(STORE_PFAD))
        if not cfg.get("ollama_enabled", True):
            argv.append("--no-ollama")
        eintrag = {"command": None, "args": argv,
                   "env": {"MUNIMENTUM_HOME": str(HEIM)}}
    eintrag["command"] = eintrag["args"][0]
    eintrag["args"] = eintrag["args"][1:]
    return {
        "http": {"mcpServers": {"munimentum": {
            "type": "http", "url": f"http://127.0.0.1:{port}/mcp"}}},
        "stdio": {"mcpServers": {name: eintrag}},
    }


def _mcp_befehl(cfg):
    """Launch plan for the MCP subprocess – paths resolved at call time.

    McpProcess (runner.py) knows nothing about the storage layout; this is
    the one place that does. Resolving BASE/STORE_PFAD lazily keeps the
    sandboxed tests honest, which repoint those globals per test.
    """
    argv = script_argv("mcp_server", "--data-dir", str(BASE),
                       "--store", str(STORE_PFAD),
                       "--embed-model", cfg["embed_model"],
                       "--ollama", cfg["ollama"], "--port", str(cfg["mcp_port"]))
    if not cfg.get("ollama_enabled", True):
        argv.append("--no-ollama")
    return {"argv": argv, "cwd": str(BASE),
            "env": {"PYTHONUNBUFFERED": "1", "MUNIMENTUM_HOME": str(HEIM)},
            "db": store_layout.db_path(STORE_PFAD)}


# --------------------------------------------------------------------------
# Embedded search – uses the MCP server's ranking
# --------------------------------------------------------------------------
class SearchBridge:
    """Pulls in mcp_server.py as a library instead of rebuilding the search.

    The tool functions there are perfectly ordinary functions (the
    decorator merely also registers them with the MCP server) and work on
    mcp_server.STATE. We fill STATE just like its main() does and call them
    directly – the same hybrid ranking without maintaining a second search
    path.
    """

    def __init__(self):
        self.module = None
        self.stamp = None
        self.error = None
        self.lock = threading.Lock()

    def _store_stamp(self, cfg):
        """How a new index is recognised.

        The vector file is named differently after every run (store_layout)
        – so the name itself belongs in the stamp. Without it the old,
        still-mapped file would remain: same time, same size, and the
        in-app search would keep showing the previous state.
        """
        store = STORE_PFAD
        out = []
        for p in (store_layout.db_path(store), store_layout.vectors_path(store)):
            if p is None:
                out.append(("-", None, None))
                continue
            try:
                out.append((p.name, p.stat().st_mtime_ns, p.stat().st_size))
            except OSError:
                out.append((p.name, None, None))
        return tuple(out)

    def ensure(self, cfg):
        """Set up STATE (afresh) when the index has changed."""
        with self.lock:
            stamp = self._store_stamp(cfg)
            if self.module is not None and stamp == self.stamp:
                return self.module
            db = store_layout.db_path(STORE_PFAD)
            if not db.exists():
                self.error = {"k": "srv.noindex", "v": {}}
                self.module = None
                return None
            try:
                import mcp_server
            except ImportError as e:
                self.error = {"k": "srv.nomcpmodule", "v": {"error": str(e)}}
                self.module = None
                return None
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                con.close()
                np, V = mcp_server._open_vectors(str(STORE_PFAD), n)
            except sqlite3.Error as e:
                self.error = {"k": "srv.badindex", "v": {"error": str(e)}}
                self.module = None
                return None
            mcp_server.STATE.update(
                db=str(db), V=V, np=np, semantic=(np is not None),
                vector_dtype=str(V.dtype) if V is not None else None,
                teams_dir=str(BASE / TEAMS_DIR),
                outlook_dir=str(BASE / OUTLOOK_DIR),
                onedrive_dir=str(BASE / ONEDRIVE_DIR),
                sharepoint_dir=str(BASE / SHAREPOINT_DIR),
                pages_dir=str(BASE / SHAREPOINT_PAGES_DIR),
                planner_dir=str(BASE / PLANNER_DIR),
                todo_dir=str(BASE / TODO_DIR),
                onenote_dir=str(BASE / ONENOTE_DIR),
                runs_db=str(HEIM / run_history.DB_NAME),
                # The case book: the `case` filter and the mark on every
                # hit. The page writes through its own routes, never
                # through the MCP write tools – those stay off here.
                faelle_db=str(HEIM / faelle.DB_NAME), cases_write=False,
                embed_model=cfg["embed_model"], ollama=cfg["ollama"])
            self.module, self.stamp, self.error = mcp_server, stamp, None
            return mcp_server


# --------------------------------------------------------------------------
# Application: holds configuration, runs, schedule, MCP and search together
# --------------------------------------------------------------------------
class App:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.ui_lang = None      # language of the page served last
        self.history = run_history.RunHistory(HEIM / run_history.DB_NAME)
        self.history.prune(int(self.cfg.get("runs_retention_months") or 24))
        self.history.prune_log(int(self.cfg.get("log_retention_days") or 14))
        # Search history, saved searches and cases – one file per profile,
        # next to the run history. The history is pruned to its setting
        # at every start, and again whenever the setting changes.
        self.faelle = faelle.Fallbuch(HEIM / faelle.DB_NAME)
        self.faelle.aufraeumen(historie_tage(self.cfg))
        self.jobs = JobRunner(self.history, cwd=lambda: str(BASE), res=RES)
        self.mcp = McpProcess(self.jobs, _mcp_befehl, mcp_client_config)
        self.search = SearchBridge()
        self.scheduler = Scheduler(self)
        self._ollama_cache = (0.0, None)
        self._calendar_cache = None      # (fingerprint, raw, gzip)
        self.device_login = None         # device-code sign-in in progress
        self._update = {"status": "off", "current": version.VERSION,
                        "latest": None, "url": None, "newer": False,
                        "ahead": False, "error": None}

    # -- derived state ----------------------------------------------------
    def konfiguriere(self, aendern):
        """Change settings through the one door – see konfiguration_aendern."""
        konfiguration_aendern(self.cfg, aendern)

    def selected_categories(self):
        kats = (_clean_categories(self.cfg["outlook_categories"],
                                  ["mail", "calendar", "contacts"])
                + _clean_categories(self.cfg["teams_categories"],
                                    ["1on1", "group", "meeting", "channels"]))
        # The mirrors are plain switches, not category lists – but the token
        # wizard asks this list which permissions the run will need. One
        # table (SPIEGEL_QUELLEN) names every switch, so a new source cannot
        # forget the scope request.
        for flag, kategorie, _name in SPIEGEL_QUELLEN:
            if self.cfg.get(flag) and kategorie not in kats:
                kats.append(kategorie)
        # Planner reads the legacy comments from the group conversations –
        # a second scope on the same switch; attachment loading a third.
        if self.cfg.get("planner_enabled") and "groups" not in kats:
            kats.append("groups")
        if self.cfg.get("planner_enabled") and \
                self.cfg.get("planner_attachments") and "files" not in kats:
            kats.append("files")
        # Teams reads the files its messages point at and the channel
        # folders through the drive API – the same scope as OneDrive.
        if self.cfg.get("teams_categories") and "files" not in kats and (
                self.cfg.get("teams_attachments")
                or self.cfg.get("teams_channel_files")):
            kats.append("files")
        return kats

    def ollama(self, force=False):
        """Cache the result briefly – the status is polled every second.

        When disabled we do not even ask. That is the real purpose of the
        switch: without it the app attempts a connection that does not
        exist every ten seconds – permanently, on every machine without
        Ollama.
        """
        if not self.cfg.get("ollama_enabled", True):
            return {"running": False, "models": [], "has_model": False,
                    "has_chat_model": False, "error": None, "disabled": True,
                    "model": self.cfg["embed_model"],
                    "chat_model": self.cfg.get("chat_model"),
                    "url": self.cfg["ollama"]}
        age, cached = self._ollama_cache
        if not force and cached is not None and time.time() - age < 10:
            return cached
        res = check_ollama(self.cfg["ollama"], self.cfg["embed_model"],
                           self.cfg.get("chat_model"))
        res["disabled"] = False
        self._ollama_cache = (time.time(), res)
        return res

    def semantisch_gewollt(self):
        """Should the next index contain vectors?"""
        return bool(self.cfg.get("ollama_enabled", True)
                    and self.cfg.get("index_semantic", True))

    def check_updates(self, blockierend=False):
        """Check once whether a newer release exists.

        In the background, because startup must not wait for a network
        reply – whoever is offline still wants to see the app immediately.
        """
        def lauf():
            self._update = updates.check(version.VERSION, version.REPO,
                                         enabled=bool(self.cfg.get("update_check", True)))
            if self._update["newer"]:
                self.jobs.logk("srv.update.available", "info",
                               version=self._update["latest"],
                               url=self._update["url"] or version.RELEASES_URL)
            # Everything else stays quiet: no release, no network or
            # switched off are not events to bother anyone with.
        if blockierend:
            lauf()
        else:
            threading.Thread(target=lauf, daemon=True).start()
        return self._update

    def log_token_state(self):
        """Say once at startup where things stand.

        The wizard only opens when something is missing – without this line
        the common case (token present and valid) would be completely
        silent, and nobody would know how long it still carries.
        """
        st = token_status(read_token(), needed=self.selected_categories())
        if not st["present"]:
            return self.jobs.logk("srv.token.none", "warn")
        if st["expired"]:
            return self.jobs.logk("srv.token.expired", "warn")
        # Four complete sentences instead of assembled fragments: what works
        # concatenated in one language no longer forms a sentence in the
        # next.
        konto, minuten = st["account"], st["expires_in_minutes"]
        schluessel = ("srv.token.found" if konto and minuten is not None else
                      "srv.token.found.unknown" if konto else
                      "srv.token.found.nowho" if minuten is not None else
                      "srv.token.found.plain")
        self.jobs.logk(schluessel, "ok", account=konto or "", minutes=minuten)
        if st["missing"]:
            self.jobs.logk("srv.token.scopes", "warn", list=", ".join(st["missing"]))
        return None

    def status(self):
        token = read_token()
        tok = token_status(token, needed=self.selected_categories())
        oll = self.ollama()
        jobs = self.jobs.snapshot()
        plan = self.cfg["schedule"]
        nxt = self.scheduler.next_due()
        # The wizard only opens when it is needed: no token, an expired one,
        # or one a run has just found dead. A still-valid token is left
        # alone – its lifetime depends on the tenant and can well outlast a
        # working day. Whoever wants to replace it anyway clicks the token
        # tile at the top.
        wizard = None
        if not tok["valid"] or jobs["token_expired"]:
            wizard = "token"
        elif not oll.get("disabled") and (not oll["running"] or not oll["has_model"]):
            wizard = "ollama"
        # What changes on its own, and nothing else. Everything that only
        # ever changes when someone acts – the folders the archive holds,
        # the paths, the constants of the interface – has its own route and
        # is asked for once: this one is polled, and a polled answer should
        # not keep repeating the names of someone's mail folders.
        auth = self.auth_status()
        return {
            # Per node only what the interface acts on. What the settings
            # already say (the models by name, the port, the mode, the own
            # registration's id) is not repeated here, what nobody reads is
            # gone, and what can be decided here is decided here: `wizard`
            # is such a decision, and so is `ollama.has_model`.
            "token": {k: tok.get(k) for k in
                      ("present", "valid", "expired", "account", "name",
                       "expires_in_minutes", "missing")},
            "ollama": {k: oll.get(k) for k in
                       ("running", "has_model", "has_chat_model", "disabled")},
            "calendar": {"built_at": _mtime_iso(calendar_file(self.cfg))},
            "jobs": jobs,
            "mcp": {k: v for k, v in self.mcp.status(self.cfg).items()
                    if k in ("running", "url", "error")},
            "profile": profil_zustand(),
            "schedule_next": (datetime.fromtimestamp(nxt).isoformat(timespec="seconds")
                              if nxt and plan.get("enabled") else None),
            "wizard": wizard,
            "update": {k: self._update.get(k) for k in
                       ("status", "latest", "url", "newer", "ahead", "error")},
            "auth": {k: auth.get(k) for k in
                     ("signed_in", "account", "own_registration", "device")},
        }

    def umgebung(self):
        """What the interface needs once: where things lie, what the app
        is, the defaults behind two settings and the snippet for a Claude
        client. None of it changes while the app runs – except after a
        save, and the page asks again then."""
        return {
            "version": version.VERSION,
            "api_version": API_VERSION,
            "build": version.build(),
            "releases_url": version.RELEASES_URL,
            "default_client_id": auth.STANDARD_CLIENT_ID,
            "ollama_hint": ollama_hint(),
            "data_dir": str(BASE),
            "home_dir": str(HEIM),
            "app_location": (sys.executable if FROZEN
                             else str(Path(__file__).resolve().parent)),
            "data_dir_default": str(HEIM / DATEN_UNTERORDNER),
            "index_dir": str(STORE_PFAD),
            # Where a case export lands – the settings may name it, else it
            # is the default under the documents folder.
            "case_export_dir": str(fall_export_basis(self.cfg)),
            "index_dir_default": str(HEIM / STORE_DIR),
            "frozen": FROZEN,
            "skip_folders_default": sorted(SKIP_FOLDERS_DEFAULT),
            "filetype_hidden_default": sorted(FILETYPE_HIDDEN_DEFAULT),
            "graph_explorer": GRAPH_EXPLORER,
            "scopes_needed": sorted({SCOPE_FOR[c] for c in self.selected_categories()
                                     if c in SCOPE_FOR} | {"User.Read"}),
            "scope_queries": SCOPE_QUERY,
            "mcp_client": (self.mcp.status(self.cfg) or {}).get("config"),
        }

    def bestand(self):
        """What the archive holds per source – how many folders, chats,
        calendars, lists and notebooks there are and which of them come
        along, with their names. It changes with a run, not with a poll,
        so the page asks after a run and when it shows the sources."""
        store = store_status(self.cfg)
        return {
            # What the index is and what it can do. It changes with a run,
            # like everything else here – in the poll it repeated a list of
            # column names every thirty seconds.
            "store": {k: store.get(k) for k in
                      ("exists", "features", "semantic", "built_at")},
            "exports": export_status(self.cfg),
            "folders": folders.zusammenfassung(
                folders.lade(BASE / OUTLOOK_DIR),
                auswahlregeln(self.cfg)),
            "calendars": self._kalenderstand(),
            "notebooks": self._notizbuchstand(),
            "folders_onedrive": folders.zusammenfassung(
                folders.lade(BASE / ONEDRIVE_DIR),
                folders.lies_regeln(self.cfg.get("onedrive_rules") or "")),
            "conversations": folders.zusammenfassung(
                folders.lade(BASE / TEAMS_DIR), teamsregeln(self.cfg)),
            "lists": folders.zusammenfassung(
                folders.lade(BASE / TODO_DIR), todoregeln(self.cfg)),
        }

    def _kalenderstand(self):
        """How many calendars exist and how many of them come along.

        Own numbers instead of folders.zusammenfassung: that counts items,
        and Graph does not say when listing how many appointments a
        calendar holds. The names come along so the interface can name the
        selection instead of merely counting.
        """
        daten = folders.lade(BASE / OUTLOOK_DIR, folders.KALENDER)
        alle = (daten or {}).get("ordner", [])
        an = folders.gewaehlt(daten, kalenderregeln(self.cfg, daten))
        return {
            "abgeglichen": (daten or {}).get("abgeglichen"),
            "gesamt": len(alle),
            "gewaehlt": len(an),
            "namen": [e.get("name") or e["pfad"] for e in an],
            "neu": (daten or {}).get("neu", []),
        }

    def _notizbuchstand(self):
        """How many notebooks exist, how many come along – and the list
        itself: the settings show one row per notebook with its own cadence,
        so the page needs the entries, not only the count."""
        daten = folders.lade(BASE / ONENOTE_DIR, folders.NOTIZBUECHER)
        alle = (daten or {}).get("ordner", [])
        an = {e["id"] for e in folders.gewaehlt(daten, notizbuchregeln(self.cfg))}
        return {
            "abgeglichen": (daten or {}).get("abgeglichen"),
            "gesamt": len(alle),
            "gewaehlt": len(an),
            "namen": [e.get("name") or e["pfad"] for e in alle if e["id"] in an],
            "neu": (daten or {}).get("neu", []),
            "eintraege": [{"id": e["id"], "name": e.get("name") or e["pfad"],
                           "pfad": e["pfad"], "an": e["id"] in an}
                          for e in alle],
        }

    def auth_modus(self):
        """The app keeps its own configuration – not via settings.py.

        settings.py reads app_config.json and is the source for the scripts
        in the terminal. The app already has its cfg in memory; consulting
        both at once would mean maintaining two truths for the same
        setting. It is passed on to the subprocesses as an environment
        variable.
        """
        return "login" if str(self.cfg.get("auth_mode", "token")).lower() == "login" \
            else "token"

    def auth_ziel(self):
        """(client ID, tenant) – empty means Microsoft's public application."""
        return (str(self.cfg.get("client_id") or "").strip() or auth.STANDARD_CLIENT_ID,
                str(self.cfg.get("tenant") or "").strip() or auth.STANDARD_TENANT)

    def auth_status(self):
        """How the app signs in – and whether that currently holds.

        `signed_in` only asks the cache and opens nothing: the tile must be
        able to show the state without kicking off a sign-in unasked.
        """
        klient, mandant = self.auth_ziel()
        konto = auth.angemeldet(client=klient, mandant=mandant)
        laeuft = self.device_login
        return {
            "mode": self.auth_modus(),
            "signed_in": bool(konto),
            "account": konto if isinstance(konto, str) else None,
            "own_registration": (klient, mandant) != (auth.STANDARD_CLIENT_ID,
                                                      auth.STANDARD_TENANT),
            "client_id": klient,
            "tenant": mandant,
            "default_client_id": auth.STANDARD_CLIENT_ID,
            # Is a device-code sign-in in progress? Then code and address.
            "device": dict(laeuft) if laeuft else None,
        }

    # -- Actions -----------------------------------------------------------
    def calendar_payload(self):
        """Calendar data raw and gzipped, cached until the file changes.

        Around 5 MB of JSON – re-reading and compressing on every tab
        switch would be waste; compressed, 0.75 MB go over the wire.
        """
        p = calendar_file(self.cfg)
        try:
            st = p.stat()
        except OSError:
            return None, None
        stamp = (st.st_mtime_ns, st.st_size)
        if self._calendar_cache and self._calendar_cache[0] == stamp:
            return self._calendar_cache[1], self._calendar_cache[2]
        roh = p.read_bytes()
        self._calendar_cache = (stamp, roh, gzip.compress(roh, 6))
        return self._calendar_cache[1], self._calendar_cache[2]

    def nachgeholt(self):
        """Source -> when its last resync or full sync completed (ISO,
        UTC), from the run history – what the archive check needs to call
        a file "still missing after a fetch"."""
        out = {}
        for key in EXPORT_ORDNER:
            wann = self.history.last_resync(key)
            if wann:
                out[key] = datetime.fromtimestamp(wann, UTC).isoformat(timespec="seconds")
        return out

    def launch(self, anfrage, *, embeddings=None, label="Lauf",
               reconstruct=None, nur_einheit=None, legacy_comments=False,
               sync_now=False, calendar_full=False, full_sync=False,
               resync=False, archiv=None, nachholen=None, resync_ordner=None,
               fall_export=None, origin="manual"):
        """Start a run. `anfrage` maps registry request keys to booleans –
        the API body, the schedule plan and the tests all speak this one
        shape; unknown keys are ignored, missing ones are off."""
        if self.jobs.busy:
            return False, {"k": "srv.busy", "v": {}}
        # No run at all on an installation an upgrade left half-done – not
        # just exports: a folder sync writes a state.db, which would create
        # the very file the legacy check looks for and quietly disarm it.
        for sperre in lauf_sperren():
            return False, sperre
        gewaehlt = embeddings is not None      # explicitly set vs. self-determined
        if embeddings is None:
            embeddings = (self.semantisch_gewollt()
                          and self.ollama()["running"] and self.ollama()["has_model"])
        # The check queries the mailbox, so it needs the same access.
        # One picture of what this run was asked for – normalised to the
        # registry's keys, and read by the access question, the "nothing
        # new" gate and the run record alike, so none of them can fall
        # behind a new entry.
        angefragt = {e["anfrage"]: bool(anfrage.get(e["anfrage"]))
                     for e in steps_mod.REGISTRY}
        export_gewollt = any(angefragt.get(e["anfrage"])
                             for e in steps_mod.REGISTRY if e.get("corpus"))
        # The cadences narrow EVERY run, scheduled and manual alike – but
        # inside the exports, per category, URL or notebook: a source below
        # its interval says so in the log and reports nothing new, which is
        # what lets the runner skip the index afterwards.
        ausgelassen = {}                 # step key -> why it will not run
        braucht_zugang = steps_mod.braucht_zugang(angefragt)
        token = read_token() if braucht_zugang else ""
        # In login mode the on-disk cache carries the run – a pasted key is
        # then unnecessary, and its absence must not prevent a run.
        if braucht_zugang and not token and self.auth_modus() != "login":
            return False, {"k": "srv.notoken", "v": {}}
        if angefragt["index"] and not embeddings:
            self.jobs.logk("srv.lexical.choice" if gewaehlt
                           else "srv.lexical.noollama", "warn")
        steps = build_steps(self.cfg, angefragt, embeddings=embeddings,
                            token=token, reconstruct=reconstruct,
                            nur_einheit=nur_einheit,
                            legacy_comments=legacy_comments,
                            sync_now=sync_now, calendar_full=calendar_full,
                            full_sync=full_sync, resync=resync, archiv=archiv,
                            nachgeholt=self.nachgeholt(), nachholen=nachholen,
                            resync_ordner=resync_ordner, fall_export=fall_export)
        # A button of one source – sync now, fetch again, full sync, a
        # single URL – on a source the settings do not tick: say so, rather
        # than starting a run that carries nothing but the index step.
        if sync_now or resync or full_sync or nur_einheit:
            gebaut = {s["key"] for s in steps}
            for e in steps_mod.REGISTRY:
                if e.get("corpus") and angefragt.get(e["anfrage"]) and e["key"] not in gebaut:
                    return False, {"k": "srv.inactive", "v": {"source": _quellname(e)}}
        if not steps:
            return False, {"k": "srv.nothing", "v": {}}
        for s in steps:
            if s["key"] in ausgelassen:
                s["auslassen"] = ausgelassen[s["key"]]
        # Exports were asked for, but none survived its gate (cadence, empty
        # category list): by definition nothing new – the index and calendar
        # steps must not rebuild the archive for that. An index-only run
        # (expert mode) asked for no export and therefore still runs.
        nichts_neues = (export_gewollt
                        and not any(s.get("corpus") and not s.get("auslassen")
                                    for s in steps)
                        and self._folgeschritte_aktuell(steps))
        # What the run history records about this run – switches and counts
        # only, nothing personal.
        kontext = {
            "nichts_neues": nichts_neues,
            # Outlook and Teams record WHICH categories ran; every other
            # source is a yes/no, taken straight from the registry so the
            # runs table cannot miss a newly added one.
            "elements": {
                "outlook": (["calendar"] if calendar_full else
                            _clean_categories(self.cfg["outlook_categories"],
                                              ["mail", "calendar", "contacts"])
                            if angefragt["outlook"] else []),
                "teams": (_clean_categories(self.cfg["teams_categories"],
                                            ["1on1", "group", "meeting",
                                             "channels"])
                          if angefragt["teams"] else []),
                **{e["key"]: bool(angefragt.get(e["anfrage"]))
                   for e in steps_mod.REGISTRY
                   if e.get("corpus") and e["key"] not in ("outlook", "teams")},
            },
            "semantic": bool(angefragt["index"] and embeddings),
            "workers": int(self.cfg.get("workers") or 4),
            "retention_months": int(self.cfg.get("runs_retention_months") or 24),
            "log_retention_days": int(self.cfg.get("log_retention_days") or 14),
            "notify": str(self.cfg.get("notifications") or "errors"),
            "keep_awake": bool(self.cfg.get("keep_awake", True)),
            "lang": self.ui_lang or i18n.negotiate(self.cfg.get("language"),
                                                   None, RES),
        }
        if not self.jobs.start(steps, label, origin=origin, context=kontext):
            return False, {"k": "srv.nostart", "v": {}}
        return True, {"k": "srv.mcp.startok", "v": {}}

    def _folgeschritte_aktuell(self, steps):
        """Is every follow-up step in this run newer than the last export?

        Only then does "no export ran" really mean "nothing to do". If the
        last index or calendar step failed or was cancelled, the archive
        moved on without it, and a run whose exports are all gated must
        still catch up instead of skipping forever. The export side counts
        ATTEMPTS: one that died part-way still wrote what it had by then.
        """
        letzter_export = max(
            (self.history.last_step_started(e["key"]) or 0
             for e in steps_mod.REGISTRY if e.get("corpus")), default=0)
        for s in steps:
            if not s.get("nur_bei_neuem"):
                continue
            fertig = self.history.last_step_ok(s["key"])
            if fertig is None or fertig < letzter_export:
                return False
        return True

    def login_starten(self):
        """Fetch a device code and wait for consent in the background.

        There is no native sign-in window here – the app has none. The page
        shows the code instead; this thread waits until Microsoft confirms
        and puts the result into the on-disk cache.
        """
        if self.device_login and not self.device_login.get("done"):
            return True, self.device_login          # one already open
        scopes = sorted({auth.RES + s for s in
                         ({SCOPE_FOR[c] for c in self.selected_categories()
                           if c in SCOPE_FOR} | {"User.Read"})})
        klient, mandant = self.auth_ziel()
        try:
            vorgang = auth.DeviceLogin(scopes, client=klient, mandant=mandant)
            daten = vorgang.start()
        except Exception as e:                      # noqa: BLE001
            self.jobs.logk("srv.login.failed", "err", detail=f"{type(e).__name__}: {e}")
            return False, {"error": f"{type(e).__name__}: {e}"}
        self.device_login = {**daten, "done": False, "ok": False}
        self.jobs.logk("srv.login.code", code=daten["code"], url=daten["url"])

        def warten():
            ok, meldung = vorgang.warten()
            self.device_login = {**self.device_login, "done": True, "ok": ok,
                                 "error": None if ok else meldung}
            if ok:
                self.jobs.logk("srv.login.ok", "ok")
            else:
                self.jobs.logk("srv.login.failed", "err", detail=meldung)
        threading.Thread(target=warten, daemon=True).start()
        return True, self.device_login

    def abmelden(self):
        """Discard the refresh token. The pasted key stays where it is."""
        auth.cache_leeren()
        self.device_login = None
        self.jobs.logk("srv.logout")
        return True

    def autostart_mcp(self):
        """On app start: bring up MCP when an index exists.

        Exactly the case from the requirement "the MCP server runs even
        without Ollama": the server then keeps ranking purely lexically.
        """
        if not self.cfg.get("mcp_autostart") or not self.cfg.get("mcp_enabled", True):
            return
        ok, why = self.mcp.start(self.cfg)
        if not ok:
            self.jobs.logk("srv.mcp.notstarted", "warn", why=why)

    def shutdown(self):
        self.scheduler.stop_event.set()
        self.jobs.cancel()
        self.mcp.stop()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
FEHLERSPRACHE = "en"        # the language of error texts – see Handler._fehler

# The versioned surface, and since 13.0 the only one: resources, real
# methods, English names (rest.py). The page asks nothing outside it.
#
# Three of these routes are reads whose question is too big for a URL – the
# model's answer, the folder plan, the report. They use QUERY (RFC 10008):
# safe and idempotent like GET, with the question in the body. None of them
# stores anything, and a caller may repeat them at will.
API_VERSION = "v1"          # the contract's version, not the program's
# It moves when an operation goes away, changes its method or its answer
# loses a field – never for an addition. 13.0 moved the rest of the
# surface in here and removed none; what it did remove never carried a
# version. Two
# 12.0 answers changed spelling (rest.py): the case marks on a hit, and
# the criteria's case folder, which 12.0 wrote under the mailbox
# folder's key. Corrected under v1 and named in the release notes.
API_V1 = "/api/" + API_VERSION
ROUTEN_V1 = (
    ("GET", "/api/v1/cases", "_v1_faelle"),
    ("POST", "/api/v1/cases", "_v1_anlegen"),
    ("GET", "/api/v1/cases/{id}", "_v1_fall"),
    ("PATCH", "/api/v1/cases/{id}", "_v1_aendern"),
    ("DELETE", "/api/v1/cases/{id}", "_v1_loeschen"),
    # What hangs on a case, each its own collection under it.
    ("GET", "/api/v1/cases/{id}/items", "_v1_eintraege"),
    ("POST", "/api/v1/cases/{id}/items", "_v1_eintraege_hinzu"),
    ("PATCH", "/api/v1/cases/{id}/items", "_v1_eintraege_verschieben"),
    ("GET", "/api/v1/cases/{id}/items/{item}", "_v1_eintrag"),
    ("PATCH", "/api/v1/cases/{id}/items/{item}", "_v1_eintrag_aendern"),
    ("DELETE", "/api/v1/cases/{id}/items/{item}", "_v1_eintrag_loeschen"),
    ("GET", "/api/v1/cases/{id}/folders", "_v1_fallordner"),
    ("POST", "/api/v1/cases/{id}/folders", "_v1_fallordner_anlegen"),
    ("GET", "/api/v1/cases/{id}/folders/{folder}", "_v1_fallordner_eins"),
    ("PATCH", "/api/v1/cases/{id}/folders/{folder}", "_v1_fallordner_aendern"),
    ("DELETE", "/api/v1/cases/{id}/folders/{folder}", "_v1_fallordner_loeschen"),
    ("GET", "/api/v1/cases/{id}/notes", "_v1_notizen"),
    ("POST", "/api/v1/cases/{id}/notes", "_v1_notiz_anlegen"),
    ("GET", "/api/v1/cases/{id}/notes/{note}", "_v1_notiz"),
    ("PATCH", "/api/v1/cases/{id}/notes/{note}", "_v1_notiz_aendern"),
    ("DELETE", "/api/v1/cases/{id}/notes/{note}", "_v1_notiz_loeschen"),
    ("GET", "/api/v1/cases/{id}/lists", "_v1_listen"),
    ("POST", "/api/v1/cases/{id}/lists", "_v1_liste_anlegen"),
    ("GET", "/api/v1/cases/{id}/lists/{list}", "_v1_liste_eine"),
    ("DELETE", "/api/v1/cases/{id}/lists/{list}", "_v1_liste_loeschen"),
    # Two reads that are not stored anywhere: what the case's searches find
    # today, and the export it can start.
    ("GET", "/api/v1/cases/{id}/new-hits", "_v1_neue_treffer"),
    ("POST", "/api/v1/cases/{id}/export", "_v1_fall_export"),
    ("POST", "/api/v1/cases/{id}/export/open", "_v1_fall_export_oeffnen"),
    ("GET", "/api/v1/search", "_v1_suche"),
    ("GET", "/api/v1/similar", "_v1_aehnlich"),
    ("QUERY", "/api/v1/answer", "_v1_antwort"),
    # The search's two memories: what ran, and what is kept under a name.
    ("GET", "/api/v1/searches/history", "_v1_verlauf"),
    ("DELETE", "/api/v1/searches/history", "_v1_verlauf_leeren"),
    ("GET", "/api/v1/searches/saved", "_v1_gespeicherte"),
    ("POST", "/api/v1/searches/saved", "_v1_speichern"),
    ("GET", "/api/v1/searches/saved/{id}", "_v1_suche_eine"),
    ("PATCH", "/api/v1/searches/saved/{id}", "_v1_suche_aendern"),
    ("DELETE", "/api/v1/searches/saved/{id}", "_v1_suche_loeschen"),
    # The rest of the Explore door's reads. They answer English already –
    # they were built for the index and for Claude – so they move over as
    # they are; only a list is called `items` here, whatever the engine
    # names it.
    ("GET", "/api/v1/files", "_v1_dateien"),
    # The exported file itself – what "Open original" opens and what an
    # archived HTML page links to.
    ("GET", "/api/v1/files/content", "_v1_inhalt"),
    ("GET", "/api/v1/folders", "_v1_ordner"),
    ("GET", "/api/v1/filetypes", "_v1_dateitypen"),
    ("GET", "/api/v1/people", "_v1_personen"),
    ("GET", "/api/v1/addresses", "_v1_adressen"),
    ("GET", "/api/v1/threads", "_v1_gespraech"),
    ("GET", "/api/v1/documents", "_v1_dokument"),
    ("GET", "/api/v1/documents/facts", "_v1_fakten"),
    ("GET", "/api/v1/documents/attachments", "_v1_anhang"),
    ("GET", "/api/v1/calendar", "_v1_kalender"),
    # The settings are their own thing: they change when someone saves
    # them, not every other second, and the status is polled. Until 12.0
    # they travelled with every poll – a third of its weight.
    # What changes on its own: the one answer the page polls.
    ("GET", "/api/v1/status", "_v1_status"),
    ("GET", "/api/v1/app", "_v1_umgebung"),
    ("GET", "/api/v1/inventory", "_v1_bestand"),
    ("GET", "/api/v1/config", "_v1_konfig"),
    ("PATCH", "/api/v1/config", "_v1_konfig_aendern"),
    # Building the archive: a run is a thing that happened, and one of them
    # may be going on – that one is `current`.
    ("GET", "/api/v1/runs", "_v1_laeufe"),
    ("POST", "/api/v1/runs", "_v1_lauf_starten"),
    ("GET", "/api/v1/runs/current", "_v1_lauf_jetzt"),
    ("DELETE", "/api/v1/runs/current", "_v1_lauf_abbrechen"),
    ("GET", "/api/v1/log", "_v1_log"),
    ("GET", "/api/v1/runs/{id}/log", "_v1_lauf_protokoll"),
    # What a run works on. Everything here but `open` and `folder-plan`
    # starts a run of its own; the answer names it.
    # The completeness balance: its rows are finer than the sources – the
    # mailbox is three of them – so they have a path of their own.
    ("GET", "/api/v1/balance/{row}", "_v1_bilanz"),
    ("POST", "/api/v1/balance/{row}/fetch", "_v1_bilanz_holen"),
    ("POST", "/api/v1/sources/{source}/refetch", "_v1_quelle_nachholen"),
    ("POST", "/api/v1/sources/{source}/rebuild", "_v1_quelle_neu"),
    ("PATCH", "/api/v1/sources/{source}/findings", "_v1_quelle_befunde"),
    ("POST", "/api/v1/sources/{source}/open", "_v1_quelle_oeffnen"),
    ("QUERY", "/api/v1/sources/{source}/folder-plan", "_v1_quelle_ordnerplan"),
    # Insights
    ("GET", "/api/v1/analytics", "_v1_analytics"),
    ("POST", "/api/v1/analytics/refresh", "_v1_analytics_neu"),
    # The settings that are more than a key in the config file
    ("PATCH", "/api/v1/schedule", "_v1_zeitplan"),
    ("PATCH", "/api/v1/storage", "_v1_speicherorte"),
    ("PATCH", "/api/v1/mcp", "_v1_mcp"),
    ("POST", "/api/v1/ollama/recheck", "_v1_ollama"),
    ("POST", "/api/v1/updates/check", "_v1_update"),
    # Access to the Microsoft account
    ("PUT", "/api/v1/access/token", "_v1_token"),
    ("POST", "/api/v1/access/session", "_v1_anmelden"),
    ("DELETE", "/api/v1/access/session", "_v1_abmelden"),
    ("DELETE", "/api/v1/access/notice", "_v1_hinweis_weg"),
    # One archive per profile
    ("GET", "/api/v1/profiles", "_v1_profile"),
    ("POST", "/api/v1/profiles", "_v1_profil_anlegen"),
    ("PATCH", "/api/v1/profiles", "_v1_profile_einstellen"),
    ("GET", "/api/v1/profiles/{name}", "_v1_profil"),
    ("PATCH", "/api/v1/profiles/{name}", "_v1_profil_umbenennen"),
    ("POST", "/api/v1/profiles/{name}/open", "_v1_profil_oeffnen"),
    # The app itself
    ("QUERY", "/api/v1/reports", "_v1_bericht"),
    ("POST", "/api/v1/quit", "_v1_beenden"),
    ("GET", "/api/v1/openapi", "_v1_openapi"),
)


def antwort_version():
    """`11.4.0 (a1b2c3d)` – the program and, where it is known, the commit
    it was built from."""
    bau = version.build()
    return f"{version.VERSION} ({bau})" if bau else version.VERSION


def muster_passt(muster, pfad):
    """A route pattern with {name} placeholders against a path: the values
    it captured, or None when the shape differs."""
    # No slashes stripped: a trailing one makes a different path, as it
    # does everywhere else in this API. One resource, one spelling – that
    # is what `Location` hands out.
    erwartet, hat = muster.split("/"), pfad.split("/")
    if len(erwartet) != len(hat):
        return None
    werte = {}
    for e, h in zip(erwartet, hat, strict=True):
        if e.startswith("{") and e.endswith("}"):
            werte[e[1:-1]] = unquote(h)
        elif e != h:
            return None
    return werte


class Ablehnung(Exception):
    """A refusal raised inside a route; the handler turns it into the one
    error body (Handler._fehler). `grund` is a text key, an i18n message
    or a plain sentence from the search module; `extra` are the fields
    the answer keeps beside it (an empty list, the token's state)."""

    def __init__(self, code, grund, v=None, **extra):
        super().__init__(grund)
        self.code, self.grund, self.v, self.extra = code, grund, v, extra


# What a saved search stores and what the engine is asked for are two
# vocabularies for one thing: the interface named its three modes after
# what they do for the user, the engine after how it ranks. A stored
# criteria set is handed back to /api/v1/search unchanged, so the route
# understands both spellings.
MODUS = faelle.ENGINE_MODUS

# The six of the eight sources with folder rules to preview. The mailbox
# has two rule sets – its folders and its calendars – told apart by
# `unit`, not by a source of their own. A name outside the eight is
# unknown; one of the eight without rules says so.
PLAN_QUELLEN = ("outlook", "onedrive", "sharepoint", "teams", "todo", "onenote")
PLAN_EINHEITEN = ("mail", "calendar")

# What a sub-resource of a case says when its id names nothing there.
TEIL_FEHLT = {"folder": "srv.case.nofolder", "note": "srv.case.nonote",
              "list": "srv.case.nolist", "item": "srv.case.noitem"}


def _modus(wert):
    wert = str(wert or "auto").strip().lower()
    return MODUS.get(wert, wert)


class Handler(BaseHTTPRequestHandler):
    server_version = "munimentum-app"
    sys_version = ""           # the Python version is nobody's business
    protocol_version = "HTTP/1.1"
    app = None                 # set by serve()
    allowed_hosts = ()
    ZUSATZ = {}                # headers every answer of this handler carries

    # Which methods each route of the app's own surface serves. The
    # dispatchers below are the truth; this table only decides whether a
    # request that found no route is a wrong method (405, with Allow) or
    # no route at all (404) – and answers OPTIONS. A test derives it from
    # the dispatchers again and compares.
    ROUTEN = {
        "/": ("GET",),
        "/index.html": ("GET",),
    }

    def log_message(self, fmt, *args):
        pass                    # no access log on stdout

    def version_string(self):
        return self.server_version          # without the empty sys_version

    # -- Helpers -----------------------------------------------------------
    def _host_ok(self):
        """Accept only our own loopback address.

        Without this check any website could talk to the server via a DNS
        name pointing at 127.0.0.1 (rebinding) – and the server hands out
        the complete mail and chat archive.
        """
        host = (self.headers.get("Host") or "").lower()
        return host in self.allowed_hosts

    def _rumpf_weg(self):
        """Read away a body no route asked for.

        Every answer that comes before `_body()` – the Host check, a wrong
        method, a GET that carried a body anyway – would otherwise leave it
        on the connection, and the next request would be parsed out of it:
        the server would answer something nobody sent. Where the body
        cannot be read away, the connection ends instead.
        """
        if getattr(self, "_gelesen", False):
            return
        self._gelesen = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (AttributeError, ValueError):
            self.close_connection = True
            return
        if n <= 0:
            return
        if n > 4 * 1024 * 1024:
            self.close_connection = True
            return
        try:
            self.rfile.read(n)
        except OSError:
            self.close_connection = True

    def _kennung(self):
        """Which program answered, and which contract it answered under.
        The path says v1 and stays there for as long as the shape holds; a
        script that wants to know exactly which build it is talking to
        reads it here instead of guessing from the version in the path.

        Its own method because three answers write their own header block
        – the streamed answer and the two ways a file goes out – and the
        promise is that *every* answer carries these two."""
        self.send_header("X-Munimentum-Version", antwort_version())
        self.send_header("X-Munimentum-Api", API_VERSION)

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        self._rumpf_weg()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        if code != 204:
            # A 204 says there is no body; a Content-Length on it is a
            # framing error to a strict client (RFC 9110 §8.6).
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self._kennung()
        for k, v in {**self.ZUSATZ, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, ctype="application/json; charset=utf-8",
              extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str),
                   ctype, extra)

    # -- Methods -----------------------------------------------------------
    def _zahl(self, q, name, vorgabe=0, kleinste=None, groesste=None):
        """A number out of a query or a body, clamped where the route has
        bounds. Something that is not a number is the caller's mistake and
        answers 400 – not the 500 an uncaught ValueError would be."""
        roh = q.get(name, vorgabe)
        try:
            wert = int(roh if roh not in (None, "") else vorgabe)
        except (TypeError, ValueError):
            raise Ablehnung(400, "srv.badparam", {"name": name}) from None
        if kleinste is not None:
            wert = max(kleinste, wert)
        if groesste is not None:
            wert = min(wert, groesste)
        return wert

    @staticmethod
    def _routen(pfad):
        """Every versioned route this path fits, resolved once per request:
        (method, handler name, the values the pattern captured). The 405
        decision and the dispatch both read this list."""
        return [(m, name, werte) for m, muster, name in ROUTEN_V1
                if (werte := muster_passt(muster, pfad)) is not None]

    def _erlaubt(self, pfad, routen=None):
        """Every method this path answers – both surfaces, plus the two the
        server adds itself. Empty means: no such route."""
        methoden = set(self.ROUTEN.get(pfad, ()))
        methoden.update(m for m, _name, _werte in
                        (self._routen(pfad) if routen is None else routen))
        if "GET" in methoden:
            methoden.add("HEAD")
        if methoden:
            methoden.add("OPTIONS")
        return methoden

    def _methode_fehlt(self, pfad, methode, routen=None):
        """A request that found no route: 405 when the path exists under
        another method – with `Allow`, as the specification wants – and
        404 when it does not exist at all."""
        erlaubt = self._erlaubt(pfad, routen)
        if not erlaubt:
            return self._fehler(404, "srv.notfound", {"path": pfad})
        liste = ", ".join(sorted(erlaubt))
        return self._fehler(405, "srv.method", {"method": methode, "allowed": liste},
                            kopf={"Allow": liste, **self._fragenkopf(erlaubt)})

    def do_QUERY(self):
        """A read that carries its question in the body (RFC 10008): the
        search terms the model answers from, the rules a folder plan is
        drawn for. Safe and idempotent – nothing behind it writes."""
        self._nur_v1("QUERY")

    def do_PUT(self):
        self._nur_v1("PUT")

    def do_PATCH(self):
        self._nur_v1("PATCH")

    def do_DELETE(self):
        self._nur_v1("DELETE")

    def _nur_v1(self, methode):
        """The methods that carry a body exist on the versioned surface
        only; everywhere else they are a wrong method."""
        if not self._host_ok():
            return self._verboten()
        u = urlsplit(self.path)
        pfad = u.path
        routen = self._routen(pfad)
        if methode not in self._erlaubt(pfad, routen):
            # No such route, or not under this method: say so before the
            # body is judged – a QUERY at a path that does not exist is a
            # 404, not a malformed question. The body goes unread.
            self._rumpf_weg()
            return self._methode_fehlt(pfad, methode, routen)
        try:
            # Read first, route second: a body left on the connection would
            # be taken for the next request, wrong method or not.
            data = self._body()
            if pfad.startswith(API_V1 + "/"):
                return self._v1(methode, pfad,
                                {k: v[0] for k, v in parse_qs(u.query).items()}, data, routen)
        except Ablehnung as a:
            return self._fehler(a.code, a.grund, a.v, **a.extra)
        except Exception as e:                       # noqa: BLE001
            return self._fehler(500, "srv.internal", {"error": f"{type(e).__name__}: {e}"})
        return self._methode_fehlt(pfad, methode, routen)

    def do_OPTIONS(self):
        """What may be done here. No CORS headers: the server answers one
        origin, its own page, and a cross-origin caller is exactly what the
        Host check is there to stop."""
        if not self._host_ok():
            return self._verboten()
        pfad = urlsplit(self.path).path
        erlaubt = self._erlaubt(pfad)
        if not erlaubt:
            return self._fehler(404, "srv.notfound", {"path": pfad})
        return self._send(204, b"", extra={"Allow": ", ".join(sorted(erlaubt)),
                                           **self._fragenkopf(erlaubt)})

    @staticmethod
    def _fragenkopf(erlaubt):
        """`Accept-Query` (RFC 10008, Section 3) names the formats a route
        takes a question in – the way a caller finds out that QUERY is an
        option here at all. Ours take the one format everything here takes."""
        return {"Accept-Query": "application/json"} if "QUERY" in erlaubt else {}

    def send_error(self, code, message=None, explain=None):
        """Whatever the base class refuses – an unknown verb, a request line
        it cannot parse – answers the one error body as well, never its own
        HTML page."""
        try:
            code = int(code)
            pfad = urlsplit(getattr(self, "path", "") or "").path
            # What the base class refuses, it refuses before reading a body –
            # and it closed the connection for exactly that reason. Keeping
            # it open would make the unread body the next request.
            self.close_connection = True
            zu = {"Connection": "close"}
            if code == 501 and getattr(self, "command", None):
                erlaubt = sorted(self._erlaubt(pfad)) or ["GET", "POST"]
                return self._fehler(code, "srv.method",
                                    {"method": self.command, "allowed": ", ".join(erlaubt)},
                                    kopf={"Allow": ", ".join(erlaubt), **zu})
            return self._fehler(code, message or HTTPStatus(code).phrase, kopf=zu)
        except Exception:                            # noqa: BLE001
            super().send_error(code, message, explain)

    def _fehler(self, code, grund, v=None, kopf=None, **extra):
        """The one shape of a refusal, whatever the route and the status:
        a problem detail as RFC 9457 describes it – `type`, `title`,
        `status`, `detail`, `instance` – plus two members of our own. `ok`
        is there so the page can ask one question of every answer, and
        `error` carries the text key with its placeholders, which the page
        renders in the user's language. `detail` is always English: it ends
        up in scripts, logs and bug reports, and one language there beats a
        sentence that changes with a setting. The `message` of 11.3 is
        gone with 13.0, as announced – one shape, no second spelling.

        `grund` is a text key (with `v` its placeholders), an i18n message
        or a plain sentence from the search module; `extra` are the fields
        the route's answer keeps beside it, `kopf` the headers the status
        calls for. `message` repeats the reason the way 11.3 sent it and
        goes with 13.0 – the versioned surface is already without it."""
        if isinstance(grund, str) and (
                v is not None or i18n.satz(FEHLERSPRACHE, grund, RES) is not None):
            grund = {"k": grund, "v": v or {}}
        if isinstance(grund, dict):
            key = str(grund.get("k") or "")
            detail = i18n.satz(FEHLERSPRACHE, key, RES, grund.get("v")) or key
            eigenes = {"error": {"k": key, "v": grund.get("v") or {}}} if key else {}
        else:
            key, detail, eigenes = "", str(grund), {}
        koerper = {"ok": False,
                   "type": f"urn:munimentum:error:{key}" if key else "about:blank",
                   "title": HTTPStatus(code).phrase, "status": code,
                   "detail": detail,
                   "instance": urlsplit(getattr(self, "path", "") or "").path,
                   **eigenes, **extra}
        kopf = dict(kopf or {})
        if code == 503:
            # Nothing here waits on a remote service: what is missing is an
            # index, and that takes a run. Half a minute is a polite guess
            # for "ask again", not a promise.
            kopf.setdefault("Retry-After", "30")
        return self._json(koerper, code, "application/problem+json; charset=utf-8", kopf)

    def _verboten(self):
        """The DNS-rebinding guard's answer: a Host that is not ours."""
        return self._fehler(403, "srv.forbidden",
                            {"host": (self.allowed_hosts or ("127.0.0.1",))[0]})

    def _body(self):
        """The request body as a dict. An absent or empty one is no error –
        every route falls back to its defaults – but a body that is there
        has to be JSON, and has to say so.

        QUERY is the exception: there the body *is* the question. RFC 10008,
        Section 2, has the server fail a request whose media type is
        missing – and a question nobody asked is no question either.
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            # Without a length there is no way to know where the body ends,
            # so what follows on this connection cannot be a request.
            self._gelesen, self.close_connection = True, True
            raise Ablehnung(400, "srv.badlength") from None
        art = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if self.command == "QUERY":
            if n <= 0:
                self._gelesen = True
                raise Ablehnung(400, "srv.query.empty")
            if not art:
                raise Ablehnung(400, "srv.query.notype")   # _rumpf_weg drains it
        if n <= 0:
            self._gelesen = True
            return {}
        if n > 4 * 1024 * 1024:
            # Reading it away would mean reading it; the connection ends
            # instead, and with it the body.
            self._gelesen, self.close_connection = True, True
            raise Ablehnung(413, "srv.toobig")
        if art and art != "application/json":
            raise Ablehnung(415, "srv.mediatype", {"type": art})   # _rumpf_weg drains it
        roh = self.rfile.read(n)
        self._gelesen = True
        try:
            daten = json.loads(roh.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise Ablehnung(400, "srv.badjson", {"error": str(e)}) from None
        if not isinstance(daten, dict):
            raise Ablehnung(400, "srv.badjson", {"error": "the body must be an object"})
        return daten

    # -- Routes ------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._verboten()
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        one = {k: v[0] for k, v in q.items()}
        try:
            if u.path.startswith(API_V1 + "/"):
                return self._v1("GET", u.path, one, {})
            if u.path in ("/", "/index.html"):
                return self._send(200, self._page(), "text/html; charset=utf-8")
        except Ablehnung as a:
            return self._fehler(a.code, a.grund, a.v, **a.extra)
        except Exception as e:
            return self._fehler(500, "srv.internal", {"error": f"{type(e).__name__}: {e}"})
        return self._methode_fehlt(u.path, "GET")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._host_ok():
            return self._verboten()
        u = urlsplit(self.path)
        try:
            data = self._body()
            if u.path.startswith(API_V1 + "/"):
                return self._v1("POST", u.path, {}, data)
        except Ablehnung as a:
            return self._fehler(a.code, a.grund, a.v, **a.extra)
        except Exception as e:
            return self._fehler(500, "srv.internal", {"error": f"{type(e).__name__}: {e}"})
        return self._methode_fehlt(u.path, self.command or "POST")

    # -- The versioned surface (rest.py) -----------------------------------
    def _v1(self, methode, pfad, q, data, routen=None):
        """Hand a request to the route whose pattern it fits – resolved once
        (`_routen`), so a miss answers with `Allow` from the same list
        instead of walking the table again. The body is read once, by the
        dispatcher, and travels as an argument – reading it again here
        would wait for bytes that are already gone."""
        if routen is None:
            routen = self._routen(pfad)
        for m, name, werte in routen:
            if m == methode:
                try:
                    return getattr(self, name)(werte, q, data)
                # What the case book raises, wherever it is called from: a
                # closed case, a case or a folder that is not there.
                except faelle.FallGeschlossen:
                    raise Ablehnung(409, "srv.case.closed") from None
                except faelle.KeinFall:
                    raise Ablehnung(404, "srv.case.unknown") from None
                except faelle.KeinOrdner:
                    raise Ablehnung(404, "srv.case.nofolder") from None
        return self._methode_fehlt(pfad, methode, routen)

    @staticmethod
    def _v1_id(p, name, fehlt):
        """The {name} of a path as a number. A value that is not one names
        nothing that exists – `fehlt` says what: the case, the search,
        the run, the row of a case's collection."""
        try:
            return int(p[name])
        except (KeyError, TypeError, ValueError):
            raise Ablehnung(404, fehlt) from None

    @staticmethod
    def _v1_zeile(zeilen, kennung, fehlt):
        """The row with this id, or the 404 that names its collection."""
        for zeile in zeilen:
            if zeile.get("id") == kennung:
                return zeile
        raise Ablehnung(404, fehlt)

    def _v1_kennung(self, p):
        """The {id} of a case path."""
        return self._v1_id(p, "id", "srv.case.unknown")


    def _v1_faelle(self, _p, _q, _data):
        return self._json({"items": [rest.fall(f) for f in self.app.faelle.faelle()]})

    def _v1_anlegen(self, _p, _q, data):
        try:
            neu = self.app.faelle.fall_anlegen(data.get("name"), data.get("description"))
        except ValueError:
            raise Ablehnung(400, "srv.case.noname") from None
        return self._json({"case": rest.fall(self._fall_voll(neu))}, 201,
                          extra={"Location": f"{API_V1}/cases/{neu}"})

    def _v1_fall(self, p, _q, _data):
        fall = self.app.faelle.fall(self._v1_kennung(p))
        if fall is None:
            raise Ablehnung(404, "srv.case.unknown")
        self._index_stand(fall)
        return self._json({"case": rest.fall(fall)})

    def _v1_aendern(self, p, _q, data):
        """PATCH: the fields the body names, nothing else. `status` opens
        and closes the case – the action routes of the app's own surface
        do the same thing under their own names.

        Everything that can be judged is judged before anything changes:
        a PATCH takes hold whole, or leaves the case as it was. The case
        book has no transaction across two calls, so the order does the
        work – opening first, because the fields need the case open,
        closing last, because nothing follows it.
        """
        kennung, buch = self._v1_kennung(p), self.app.faelle
        fall = buch.fall(kennung)
        if fall is None:
            raise Ablehnung(404, "srv.case.unknown")
        ziel = None
        if "status" in data:
            ziel = rest.STATUS_ZURUECK.get(str(data.get("status") or ""))
            if ziel is None:
                raise Ablehnung(400, "srv.case.badstatus",
                                {"status": str(data.get("status") or "")})
            if ziel == fall["status"]:
                ziel = None                  # already there: nothing to do
        felder = "name" in data or "description" in data
        if "name" in data and not str(data.get("name") or "").strip():
            raise Ablehnung(400, "srv.case.noname")
        if felder and fall["status"] != faelle.OFFEN and ziel != faelle.OFFEN:
            raise Ablehnung(409, "srv.case.closed")
        try:
            if ziel == faelle.OFFEN:
                buch.oeffnen(kennung)
            if felder:
                buch.fall_aendern(kennung, data.get("name") if "name" in data else None,
                                  data.get("description") if "description" in data else None)
            if ziel is not None and ziel != faelle.OFFEN:
                buch.schliessen(kennung)
        except ValueError:
            raise Ablehnung(400, "srv.case.noname") from None
        return self._json({"case": rest.fall(self._fall_voll(kennung))})

    def _v1_loeschen(self, p, _q, _data):
        kennung = self._v1_kennung(p)
        if self.app.faelle.fall(kennung) is None:
            raise Ablehnung(404, "srv.case.unknown")
        self.app.faelle.fall_loeschen(kennung)
        return self._send(204, b"")

    # -- where the archive lies, and which one is open ----------------------
    def _speicherorte(self, data):
        """`data_dir` and `index_dir`: both are keys in app_config.json,
        which sits fixed in the home folder, so there is no chicken-and-egg.
        Empty means the default under the home folder. NOTHING is moved –
        relocating folders is the user's business. Both paths are fixed
        since startup and go to every subprocess as its working directory,
        so a change takes effect on the next start; the answer says so."""
        app = self.app
        felder = (("data_dir", HEIM / DATEN_UNTERORDNER, BASE),
                  ("index_dir", HEIM / STORE_DIR, STORE_PFAD))
        antwort, neustart, neu = {}, False, {}
        for key, vorgabe, aktuell in felder:
            if key not in data:
                continue
            roh = str(data.get(key) or "").strip()
            if roh:
                ziel, fehler = pruefe_datenordner(roh)
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
        anderes = profil_geteilt(PROFIL, antwort.get("data_dir", str(BASE)),
                                 antwort.get("index_dir", str(STORE_PFAD)))
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
        return self._json({"data_dir": antwort.get("data_dir", str(BASE)),
                           "index_dir": antwort.get("index_dir", str(STORE_PFAD)),
                           "restart_required": neustart})

    def _profil_oeffnen(self, name):
        """Open this archive: the one that is already open answers at once,
        one open next door hands over its address, and otherwise this
        server restarts with it (serve() brings it back up)."""
        name = name.strip().lower()
        if not profile_moeglich():
            raise Ablehnung(400, "srv.profile.impossible")
        if name not in profil_namen():
            raise Ablehnung(404, "srv.profile.unknown", {"name": name})
        port = self.server.server_address[1]
        if name == PROFIL:
            return self._json({"name": name, "url": "/"})
        lauft = eigene_instanz(port, profil=name)
        if lauft:
            return self._json({"name": name, "url": f"http://127.0.0.1:{lauft}/"})
        if self.app.jobs.busy:
            raise Ablehnung(409, "srv.busy")
        profil_register_schreiben(zuletzt=name)
        # The answer first, then the server stops (serve() restarts). No
        # `url` here: that field means "it is already open elsewhere, go
        # there" – the page would reload at once, against a port that is
        # just shutting down, instead of waiting for it to come back.
        self._json({"name": name})
        neustart_mit_profil(self.server, name, port)
        return None

    # -- building the archive ----------------------------------------------
    def _v1_laeufe(self, _p, q, _data):
        grenze = self._zahl(q, "limit", 50, 1, 200)
        return self._v1_liste({"runs": self.app.history.list_runs(grenze + 1)}, "runs", grenze)

    def _v1_lauf_starten(self, _p, _q, data):
        """A run: which steps, and how. The body names the steps by the
        keys the step registry uses (`outlook`, `index`, …); everything
        else steers how they work. One run at a time – while one is on,
        this is a 409."""
        app = self.app
        mit_outlook = bool(data.get("outlook"))
        kalender = bool(data.get("calendar"))
        rekonstruktion = None            # None: as configured
        if kalender and mit_outlook:
            # Part of an export run: the step follows what is actually
            # fetched. The "build calendar & contacts" button comes without
            # outlook and stays untouched.
            kalender, mit_mails = calendar_plan(app.cfg)
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
                           if isinstance(data.get("resync_folders"), list) else None))
        if not ok:
            raise Ablehnung(409, why)
        return self._lauf_antwort(why)

    def _lauf_antwort(self, warum=None, extra=None):
        """Every start answers the same way: the run is on, and where to
        watch it. What it is doing travels with the status."""
        daten = {"run": f"{API_V1}/runs/current", **(extra or {})}
        if warum:
            daten["message"] = warum
        return self._json(daten, 202, extra={"Location": f"{API_V1}/runs/current"})

    def _v1_lauf_jetzt(self, _p, _q, _data):
        """The run that is on – what the `Location` of a started run names.
        Nothing running is a 404: the monitor exists while the run does."""
        # The run alone: app.status() would re-read the token, probe
        # Ollama and ask the MCP subprocess, all to throw it away – and
        # this is the route a caller polls.
        job = self.app.jobs.snapshot().get("job")
        if not job:
            raise Ablehnung(404, "srv.run.none")
        return self._json({"run": job})

    def _v1_lauf_abbrechen(self, _p, _q, _data):
        """Nothing running is a 404 here as on GET: the resource is the
        run, and there is none – not a conflict to retry. While one is
        on, the wish is recorded whatever the runner does at that moment:
        between two steps there is no process to end, and the run stops
        at the next step all the same."""
        if not self.app.jobs.busy:
            raise Ablehnung(404, "srv.run.none")
        self.app.jobs.cancel()
        return self._send(204, b"")

    def _v1_log(self, _p, q, _data):
        """The app's live log from a cursor on – not one run's: the lines
        of a run are a window into this, which `log_seq` marks. That is
        why it is not `/runs/current/log`, and why it answers when nothing
        is running."""
        lines, seq = self.app.jobs.log_since(self._zahl(q, "since", 0, 0))
        return self._json({"items": lines, "seq": seq})

    def _v1_lauf_protokoll(self, p, _q, _data):
        """The stored log of one run. A run that never was is a 404; one
        whose lines were pruned answers an empty list."""
        kennung = self._v1_id(p, "id", "srv.run.unknown")
        if not self.app.history.has_run(kennung):
            raise Ablehnung(404, "srv.run.unknown")
        return self._json({"items": self.app.history.run_log(kennung)})

    # -- the sources a run works on -----------------------------------------
    def _v1_quelle(self, p):
        """The source a path names – one of the eight the archive knows."""
        quelle = str(p.get("source") or "").strip()
        if EXPORT_ORDNER.get(quelle) is None or quelle not in archive_check.PRUEFER:
            raise Ablehnung(404, "srv.archiv.unknown", {"source": quelle})
        return quelle

    def _v1_bilanz_holen(self, p, _q, _data):
        """Fetch what the balance found open – the cheapest way the row's
        source allows. The rows are the step registry's (`outlook_mail`,
        `outlook_calendar`, …), finer than the eight sources under
        /sources: the mailbox is three of them."""
        return self._bilanz_holen(self._v1_bilanzzeile(p))

    def _v1_quelle_nachholen(self, p, _q, _data):
        return self._archiv("nachholen", {"quelle": self._v1_quelle(p)})

    def _v1_quelle_neu(self, p, _q, _data):
        return self._archiv("neu-aufbauen", {"quelle": self._v1_quelle(p)})

    def _v1_quelle_befunde(self, p, _q, data):
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
        return self._archiv(aktion, {"quelle": self._v1_quelle(p), "arten": roh})

    def _v1_quelle_oeffnen(self, p, _q, _data):
        return self._archiv("ordner", {"quelle": self._v1_quelle(p)})

    @staticmethod
    def _v1_bilanzzeile(p):
        """The balance row a path names – one of the step registry's
        (`outlook_mail`, `outlook_calendar`, …). The mailbox check writes
        three reports, one per row, and none of them under `outlook`;
        that is why the rows live under /balance, not /sources."""
        quelle = str(p.get("row") or "").strip()
        eintrag = next((e for e in steps_mod.PRUEFUNGEN if e["quelle"] == quelle), None)
        if eintrag is None:
            raise Ablehnung(404, "srv.archiv.unknown", {"source": quelle})
        return eintrag

    def _v1_bilanz(self, p, _q, _data):
        """The report a balance row's last check wrote – null before the
        first."""
        eintrag = self._v1_bilanzzeile(p)
        # The connection lives in the object's thread-local; on a threaded
        # server nothing else would ever close it.
        with state_db.StateDb(BASE / EXPORT_ORDNER[eintrag["ordner"]]) as db:
            return self._json({"report": completeness.lesen(db, eintrag["quelle"])})

    def _v1_quelle_ordnerplan(self, p, _q, data):
        """What the next run would do with these rules – without starting
        it. The rules come from the body, not from the settings: otherwise
        the preview would show the state before the change. The mailbox
        has two rule sets; `unit: calendar` asks for the calendars' plan
        instead of the folders' (`mail`, the default)."""
        quelle = self._v1_quelle(p)
        if quelle not in PLAN_QUELLEN:
            raise Ablehnung(404, "srv.plan.nosource", {"source": quelle})
        einheit = data.get("unit")
        if einheit is not None and (quelle != "outlook" or einheit not in PLAN_EINHEITEN):
            raise Ablehnung(400, "srv.plan.badunit", {"unit": str(einheit)[:40]})
        ziel = "calendar" if einheit == "calendar" else quelle
        return self._json(self._ordnerplan({**data, "quelle": ziel}))

    # -- Insights -----------------------------------------------------------
    def _v1_analytics(self, _p, _q, _data):
        return self._json(analytics_daten(self.app.cfg))

    def _v1_analytics_neu(self, _p, _q, _data):
        """The numbers again, computed afresh – the one way to invalidate
        everything at once."""
        return self._json(analytics_daten(self.app.cfg, neu=True))

    # -- settings that are more than a key ----------------------------------
    def _v1_zeitplan(self, _p, _q, data):
        return self._json(self._save_schedule(data))

    def _v1_speicherorte(self, _p, _q, data):
        """Where the archive and the index lie. Nothing is moved: relocating
        folders is the user's business, and the app never does it."""
        return self._speicherorte(data)

    def _v1_mcp(self, _p, _q, data):
        """The endpoint Claude talks to: `running` says whether it should
        be on – a boolean, nothing that merely reads like one."""
        if not isinstance(data.get("running"), bool):
            raise Ablehnung(400, "srv.mcp.badaction")
        return self._json(self._mcp(data["running"]))

    def _v1_ollama(self, _p, _q, _data):
        return self._json({"ollama": self.app.ollama(force=True)})

    def _v1_update(self, _p, _q, _data):
        return self._json({"update": self.app.check_updates(blockierend=True)})

    # -- access to the Microsoft account ------------------------------------
    def _v1_token(self, _p, _q, data):
        return self._json(self._save_token(data))

    def _v1_anmelden(self, _p, _q, _data):
        ok, daten = self.app.login_starten()
        if not ok:
            raise Ablehnung(500, "srv.login.failed",
                            {"detail": str(daten.get("error") or "")})
        return self._json({"device": daten})

    def _v1_abmelden(self, _p, _q, _data):
        self.app.abmelden()
        return self._send(204, b"")

    def _v1_hinweis_weg(self, _p, _q, _data):
        """"Later": the note about a key that died mid-run goes, or the
        wizard would reopen on every poll."""
        self.app.jobs.token_expired = False
        return self._send(204, b"")

    # -- one archive per profile --------------------------------------------
    def _v1_profile(self, _p, _q, _data):
        return self._json(profil_status())

    def _v1_profil(self, p, _q, _data):
        """One profile – what `Location` names when one is created: its
        account, its folder, whether it is the one this instance runs."""
        name = str(p.get("name") or "").strip().lower()
        # One profile's files, not every profile's: profil_status reads
        # them all, which is why it is kept out of anything frequent.
        treffer = next((n for n in profil_namen() if n.lower() == name), None)
        if treffer is None:
            raise Ablehnung(404, "srv.profile.unknown", {"name": name})
        return self._json({"profile": profil_info(treffer)})

    def _v1_profil_anlegen(self, _p, _q, data):
        info, fehler = profil_anlegen(data.get("name"))
        if fehler:
            raise Ablehnung(400, fehler)
        self.app.jobs.logk("srv.profile.created", "info", name=info["name"])
        return self._json({"profile": info, "profiles": profil_status()}, 201,
                          extra={"Location": f"{API_V1}/profiles/{quote(info['name'])}"})

    def _v1_profile_einstellen(self, _p, _q, data):
        """A property of the register, not of one profile: whether the app
        asks which archive to open at start."""
        if "ask_at_start" not in data:
            raise Ablehnung(400, "srv.profile.nofield", {"name": "ask_at_start"})
        if not isinstance(data["ask_at_start"], bool):
            raise Ablehnung(400, "srv.profile.badvalue", {"name": "ask_at_start"})
        reg = profil_register_schreiben(ohne_nachfrage=not data["ask_at_start"])
        return self._json({"ask_at_start": not reg.get("ohne_nachfrage"),
                           "profiles": profil_status()})

    def _v1_profil_umbenennen(self, p, _q, data):
        if self.app.jobs.busy:
            raise Ablehnung(409, "srv.busy")
        alt = str(p.get("name") or "").strip().lower()
        neu, fehler = profil_umbenennen(alt, data.get("name"),
                                        port=self.server.server_address[1])
        if fehler:
            raise Ablehnung(400, fehler)
        self.app.jobs.logk("srv.profile.renamed", "info", old=alt, name=neu)
        return self._json({"name": neu, "profiles": profil_status()})

    def _v1_profil_oeffnen(self, p, _q, _data):
        return self._profil_oeffnen(str(p.get("name") or ""))

    # -- the app itself -----------------------------------------------------
    def _v1_bericht(self, _p, _q, data):
        """A report someone can paste into an issue: the state, the log the
        page shows, and what they typed – addresses and user names replaced
        before it leaves the machine."""
        return self._json(fehlerbericht(
            {**self.app.status(), "store": store_status(self.app.cfg)},
            str(data.get("log") or ""), str(data.get("hint") or ""),
            i18n.negotiate(self.app.cfg.get("language"),
                           self.headers.get("Accept-Language"), RES),
            cfg=self.app.cfg))

    def _v1_beenden(self, _p, _q, _data):
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        return self._send(204, b"")

    def _v1_openapi(self, _p, _q, _data):
        """The contract itself: this file, as it is shipped."""
        text = (RES / "openapi.yaml").read_text(encoding="utf-8")
        return self._send(200, text, "text/yaml; charset=utf-8")

    # -- what hangs on a case ----------------------------------------------
    def _v1_fall_da(self, p):
        """The case a path names, in full – or a 404. Only where its items
        are read with the index's word on them (`thread_open`,
        `who_mail`): everything else takes `_v1_fall_buch`, which does
        not walk the index for a case it is about to answer with anyway."""
        fall = self._fall_voll(self._v1_kennung(p))
        if fall is None:
            raise Ablehnung(404, "srv.case.unknown")
        return fall

    def _v1_fall_buch(self, p):
        """The case as the case book keeps it – items, folders, notes,
        lists, searches – or a 404. No index: enough to resolve a
        sub-resource before a write, and to read anything but an item."""
        fall = self.app.faelle.fall(self._v1_kennung(p))
        if fall is None:
            raise Ablehnung(404, "srv.case.unknown")
        return fall

    def _v1_fall_id(self, p):
        """The id of the case a path names – or a 404."""
        return self._v1_fall_buch(p)["id"]

    @staticmethod
    def _v1_fall_offen(fall, ordner=None):
        """What the case book would refuse on the first write, refused
        before anything expensive runs and with the same answer: the case
        is open, and the folder – where one is named – is its own."""
        if fall["status"] != faelle.OFFEN:
            raise Ablehnung(409, "srv.case.closed")
        if ordner is not None and all(o["id"] != ordner for o in fall["ordner_liste"]):
            raise Ablehnung(404, "srv.case.nofolder")

    def _v1_fall_antwort(self, kennung, extra=None, code=200, ort=None):
        """Every write answers the case it changed: the page never has to
        guess what a change did, and a script gets the new state in the
        same call."""
        daten = {**(extra or {}), "case": rest.fall(self._fall_voll(kennung))}
        return self._json(daten, code, extra={"Location": ort} if ort else None)

    def _v1_teilkennung(self, p, name):
        """The id of a sub-resource in the path – the folder, the note, the
        list, the item. A value that is not a number names nothing in that
        collection, and says so: the case it sits in is there."""
        return self._v1_id(p, name, TEIL_FEHLT[name])

    def _v1_eintraege(self, p, _q, _data):
        return self._json({"items": rest.fall(self._v1_fall_da(p))["item_list"]})

    def _v1_teil(self, p, liste, feld):
        """One row of a case's collection on its own – the object the
        collection answers, and what `Location` named when it was created.
        Read from the translated case, so one row says exactly what the
        list says about it; a note, a folder or a list is the case book's
        alone."""
        fall = rest.fall(self._v1_fall_buch(p))
        zeile = self._v1_zeile(fall[liste], self._v1_teilkennung(p, feld), TEIL_FEHLT[feld])
        return self._json({feld: zeile})

    def _v1_eintrag(self, p, _q, _data):
        """One item with the index's word on it (`thread_open`, `who_mail`)
        – asked for this item alone, not for the whole case's worth, which
        is what the list does."""
        e = self._v1_eintrag_da(self._v1_fall_buch(p), p)
        self._index_stand({"eintraege_liste": [e]})
        return self._json({"item": rest.eintrag(e)})

    def _v1_fallordner_eins(self, p, _q, _data):
        return self._v1_teil(p, "folder_list", "folder")

    def _v1_notiz(self, p, _q, _data):
        return self._v1_teil(p, "note_list", "note")

    def _v1_liste_eine(self, p, _q, _data):
        return self._v1_teil(p, "list_list", "list")

    def _v1_eintraege_hinzu(self, p, _q, data):
        """Items into the case. `items` are the rows themselves – what a
        hit carries; `threads` names items already in the case whose
        conversations should be completed. Both may stand in one body."""
        fall = self._v1_fall_buch(p)
        kennung = fall["id"]
        # A row without a key names nothing in the archive and is no item:
        # dropped here, as the case book would drop it – but a body that
        # carries nothing else is refused, not answered with `added: 0`.
        eintraege = [e for e in (rest.eintrag_hinein(e) for e in (data.get("items") or [])
                                 if isinstance(e, dict))
                     if str(e.get("key") or "").strip()]
        schluessel = [str(k) for k in (data.get("threads") or []) if isinstance(k, str)]
        if not eintraege and not schluessel:
            raise Ablehnung(400, "srv.case.noitems")
        ordner = self._ordner_aus(data, "folder")
        # Everything that can refuse happens before the first write: a
        # request that adds its items and then fails on the conversations
        # would have changed the case and reported failure, and a caller
        # who retries adds nothing twice but is told so.
        self._v1_fall_offen(fall, ordner)
        if schluessel:
            fehler = self._gespraeche_moeglich()
            if fehler:
                raise Ablehnung(409, fehler)
        neu = 0
        if eintraege:
            neu = self.app.faelle.hinzufuegen(kennung, eintraege, ordner_id=ordner)
        dazu = 0
        if schluessel:
            dazu, fehler = self._thread_holen(kennung, schluessel)
            if fehler:
                raise Ablehnung(409, fehler)
        gab_es = len(eintraege) - neu
        return self._v1_fall_antwort(kennung, {"added": neu + dazu, "already": gab_es},
                                     201 if neu + dazu else 200)

    def _v1_eintraege_verschieben(self, p, _q, data):
        """Several items into one folder at once – what a selection in the
        case does. `folder: null` means unsorted."""
        kennung = self._v1_fall_id(p)
        keys = [str(k) for k in (data.get("keys") or []) if isinstance(k, str)]
        if not keys:
            raise Ablehnung(400, "srv.case.noitems")
        n = self.app.faelle.verschieben(kennung, keys, self._ordner_aus(data, "folder"))
        return self._v1_fall_antwort(kennung, {"moved": n})

    def _v1_eintrag_aendern(self, p, _q, data):
        """One item: its remark, its folder, or both."""
        fall = self._v1_fall_buch(p)
        kennung, eintrag = fall["id"], self._v1_eintrag_da(fall, p)
        buch = self.app.faelle
        # The whole body is judged before the first write: a folder the
        # case does not have, or a closed case, would otherwise leave the
        # remark behind and refuse.
        ordner = self._ordner_aus(data, "folder") if "folder" in data else None
        self._v1_fall_offen(fall, ordner)
        if "remark" in data:
            buch.bemerkung_setzen(kennung, eintrag["key"], data.get("remark"))
        if "folder" in data:
            buch.verschieben(kennung, [eintrag["key"]], ordner)
        return self._v1_fall_antwort(kennung)

    def _v1_eintrag_loeschen(self, p, _q, _data):
        fall = self._v1_fall_buch(p)
        self.app.faelle.entfernen(fall["id"], self._v1_eintrag_da(fall, p)["key"])
        return self._v1_fall_antwort(fall["id"])

    def _v1_eintrag_da(self, fall, p):
        """The item a path names – by the id the case gave it."""
        return self._v1_zeile(fall["eintraege_liste"], self._v1_teilkennung(p, "item"),
                              TEIL_FEHLT["item"])

    def _v1_fallordner(self, p, _q, _data):
        return self._json({"items": rest.fall(self._v1_fall_buch(p))["folder_list"]})

    def _v1_fallordner_anlegen(self, p, _q, data):
        """A new folder. The case book hands back an existing one of that
        name; on this surface 201 means created, so a name that is there
        already is a 409 – as a rename onto it is."""
        fall = self._v1_fall_buch(p)
        kennung, name = fall["id"], str(data.get("name") or "").strip()
        if name and any(o["name"] == name for o in fall["ordner_liste"]):
            raise Ablehnung(409, "srv.case.folder.exists")
        try:
            ordner = self.app.faelle.ordner_anlegen(kennung, name)
        except ValueError as e:
            raise Ablehnung(*self._fall_wert_fehler(e)) from None
        return self._v1_fall_antwort(kennung, {"folder": ordner}, 201,
                                     f"{API_V1}/cases/{kennung}/folders/{ordner}")

    def _v1_fallordner_aendern(self, p, _q, data):
        kennung = self._v1_fall_id(p)
        try:
            self.app.faelle.ordner_umbenennen(kennung, self._v1_teilkennung(p, "folder"),
                                              data.get("name"))
        except ValueError as e:
            raise Ablehnung(*self._fall_wert_fehler(e)) from None
        return self._v1_fall_antwort(kennung)

    def _v1_fallordner_loeschen(self, p, _q, _data):
        """The folder goes, its items stay – unsorted, in the case."""
        kennung = self._v1_fall_id(p)
        self.app.faelle.ordner_loeschen(kennung, self._v1_teilkennung(p, "folder"))
        return self._v1_fall_antwort(kennung)

    def _v1_notizen(self, p, _q, _data):
        return self._json({"items": rest.fall(self._v1_fall_buch(p))["note_list"]})

    def _v1_notiz_anlegen(self, p, _q, data):
        kennung = self._v1_fall_id(p)
        try:
            notiz = self.app.faelle.notiz(kennung, data.get("text"))
        except ValueError:
            raise Ablehnung(400, "srv.case.noname") from None
        return self._v1_fall_antwort(kennung, {"note": notiz}, 201,
                                     f"{API_V1}/cases/{kennung}/notes/{notiz}")

    def _v1_notiz_aendern(self, p, _q, data):
        """The case book answers whether the note was there; one that is
        not is a 404, not a quiet 200 with the case unchanged."""
        kennung = self._v1_fall_id(p)
        try:
            da = self.app.faelle.notiz_aendern(kennung, self._v1_teilkennung(p, "note"),
                                               data.get("text"))
        except ValueError:
            raise Ablehnung(400, "srv.case.noname") from None
        if not da:
            raise Ablehnung(404, "srv.case.nonote")
        return self._v1_fall_antwort(kennung)

    def _v1_notiz_loeschen(self, p, _q, _data):
        kennung = self._v1_fall_id(p)
        if not self.app.faelle.notiz_loeschen(kennung, self._v1_teilkennung(p, "note")):
            raise Ablehnung(404, "srv.case.nonote")
        return self._v1_fall_antwort(kennung)

    def _v1_listen(self, p, _q, _data):
        return self._json({"items": rest.fall(self._v1_fall_buch(p))["list_list"]})

    def _v1_liste_anlegen(self, p, _q, data):
        """A whole result as it stood at this moment: the criteria are run
        once more here, every hit goes into the case, and the list keeps
        what it was."""
        fall = self._v1_fall_buch(p)
        kennung = fall["id"]
        ordner = self._ordner_aus(data, "folder")
        k = faelle.kriterien(data.get("criteria"))
        # The search is the expensive part: what the case book would
        # refuse after it – a closed case, a folder it does not have – is
        # refused before it.
        self._v1_fall_offen(fall, ordner)
        treffer, fehler = self._alle_treffer(k)
        if fehler:
            raise Ablehnung(409, fehler)
        liste, neu = self.app.faelle.liste_anlegen(kennung, k, treffer, ordner_id=ordner)
        return self._v1_fall_antwort(kennung, {"list": liste, "hits": len(treffer), "added": neu},
                                     201, f"{API_V1}/cases/{kennung}/lists/{liste}")

    def _v1_liste_loeschen(self, p, _q, _data):
        """The list goes, and with it the items it brought; what was added
        another way stays, folder and remark included."""
        kennung = self._v1_fall_id(p)
        if not self.app.faelle.liste_loeschen(kennung, self._v1_teilkennung(p, "list")):
            raise Ablehnung(404, "srv.case.nolist")
        return self._v1_fall_antwort(kennung)

    def _v1_neue_treffer(self, p, _q, _data):
        """What the searches attached to this case find today that the case
        does not hold yet – per search, never stored."""
        fall = self._v1_fall_buch(p)
        buch = self.app.faelle
        keys = buch.keys(fall["id"])
        bloecke = []
        for g in fall["suchen_liste"]:
            treffer, fehler = self._alle_treffer(g["kriterien"], grenze=500)
            if fehler:
                bloecke.append({"id": g["id"], "name": g["name"], "error": fehler, "new": []})
                continue
            neu = rest.treffer([h for h in treffer
                                if h.get("key") and h["key"] not in keys])
            bloecke.append({"id": g["id"], "name": g["name"], "new": neu[:200],
                            "new_count": len(neu), "folder": g.get("ordner"),
                            "folder_name": g.get("ordner_name")})
        return self._json({"case": {"id": fall["id"], "name": fall["name"]}, "items": bloecke})

    def _v1_fall_export(self, p, _q, _data):
        return self._fall_export(self._v1_fall_id(p))

    def _v1_fall_export_oeffnen(self, p, _q, _data):
        """Show the export in the file manager – a side effect on this
        machine, not a change to anything here."""
        fall = self._v1_fall_buch(p)
        pfad = Path(fall["exportiert"]) if fall.get("exportiert") else None
        if pfad is not None and pfad.suffix == ".zip":
            pfad = pfad.parent
        if pfad is None or not pfad.is_dir():
            pfad = fall_export_basis(self.app.cfg)
            if not pfad.is_dir():
                raise Ablehnung(404, "srv.case.noexport")
        if not archive_check.ordner_oeffnen(pfad):
            raise Ablehnung(500, "srv.archiv.open.fail", path=str(pfad))
        return self._json({"path": str(pfad)})

    @staticmethod
    def _fall_wert_fehler(e):
        """What the case book means by a ValueError: a folder that is there
        already, or a name that is none."""
        return (409, "srv.case.folder.exists") if str(e) == "exists" else (400, "srv.case.noname")

    def _v1_suche(self, _p, q, _data):
        """The same engine as the page's search, paged the usual way:
        `limit` and `offset`, and `has_more` instead of a total – the
        ranking would have to run to the end for a total, and a search
        across the whole archive is not worth that."""
        grenze = self._zahl(q, "limit", 20, 1, 100)
        versatz = self._zahl(q, "offset", 0, 0)
        # The search history is the page's. A caller says whether this
        # search belongs in it – `remember`, off unless asked for, because
        # a script paging through the archive would otherwise fill the
        # rows the page offers a human. `saved` only counts along with it.
        merken = str(q.get("remember") or "").strip().lower() in ("1", "true", "yes", "ja")
        kriterien = dict(q) if merken else {k: v for k, v in q.items() if k != "saved"}
        res = self._search({**kriterien, "k": str(grenze + 1),
                            "offset": str(versatz)}, merken=merken)
        treffer = rest.treffer(res.get("results") or [])
        return self._json({"items": treffer[:grenze], "limit": grenze,
                           "offset": versatz, "has_more": len(treffer) > grenze,
                           "backend": res.get("backend"),
                           "semantic": res.get("semantic")})

    def _v1_liste(self, res, schluessel, grenze=None):
        """A collection answers `items` – one name for every list on this
        surface, whatever the engine calls its own.

        These lists are capped, not paged: the engine orders them by what
        makes them useful (count, then name) and stops at `limit`. Whether
        the cap cut something off is `has_more` – a caller that needs the
        rest narrows with `contains` or `source` rather than paging."""
        daten = dict(res)
        roh = daten.pop(schluessel, [])
        if grenze:
            # The engine was asked for one more than the cap: whether it
            # came says exactly whether the cap cut something off.
            daten["items"] = roh[:grenze]
            daten["limit"] = grenze
            daten["has_more"] = len(roh) > grenze
            if "count" in daten:
                daten["count"] = len(daten["items"])
        else:
            daten["items"] = roh
        return self._json(daten)

    def _v1_dateien(self, _p, q, _data):
        return self._json(self._files(q))

    def _v1_ordner(self, _p, q, _data):
        grenze = self._zahl(q, "limit", 300, 1, 1000)
        return self._v1_liste(self._folders(q, grenze + 1), "folders", grenze)

    def _v1_dateitypen(self, _p, q, _data):
        return self._v1_liste(self._filetypes(q), "filetypes", self._zahl(q, "limit", 40, 1, 200))

    def _v1_status(self, _p, _q, _data):
        return self._json(self.app.status())

    def _v1_aehnlich(self, _p, q, _data):
        """More like this one hit – the index answers it without Ollama."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error, items=[], count=0)
        # The numbers first: an unusable one is the caller's mistake, and
        # saying so must not depend on the engine being there.
        cid = self._zahl(q, "cid", 0)
        grenze = self._zahl(q, "limit", 20, 1, 100)
        res = mod.similar_messages(cid=cid, k=grenze + 1)
        if res.get("error"):
            raise Ablehnung(409, res["error"], items=[], count=0)
        res["results"] = rest.treffer(res.get("results") or [])
        return self._v1_liste(res, "results", grenze)

    def _v1_antwort(self, _p, _q, data):
        return self._answer(data)

    def _v1_inhalt(self, _p, q, _data):
        return self._source(q)

    # -- the search's two memories -----------------------------------------
    def _v1_verlauf(self, _p, q, _data):
        """Every search that ran, newest first – criteria only, never hits.
        `retention` is what the setting keeps them for, in days."""
        buch = self.app.faelle
        grenze = self._zahl(q, "limit", 200, 1, 500)
        return self._v1_liste(
            {"verlauf": [rest.verlauf(x) for x in buch.suchen(grenze + 1)],
             "retention": str(self.app.cfg.get("search_history") or "90")},
            "verlauf", grenze)

    def _v1_verlauf_leeren(self, _p, _q, _data):
        self.app.faelle.suchen_leeren()
        return self._send(204, b"")

    def _v1_gespeicherte(self, _p, _q, _data):
        return self._json({"items": [rest.gespeicherte_suche(g)
                                     for g in self.app.faelle.gespeicherte()]})

    def _v1_suche_eine(self, p, _q, _data):
        """One saved search – what `Location` named when it was saved."""
        g = self.app.faelle.gespeichert(self._v1_suche_kennung(p))
        if g is None:
            raise Ablehnung(404, "srv.search.unknown")
        return self._json({"search": rest.gespeicherte_suche(g)})

    def _v1_speichern(self, _p, _q, data):
        buch = self.app.faelle
        fall_id = self._v1_fall_aus(data)
        ordner = self._ordner_aus(data, "case_folder")
        if ordner is not None and fall_id is None:
            # A folder is a case's: named without one, the request is
            # malformed – not accepted and dropped, as the case book would.
            raise Ablehnung(400, "srv.case.folder.nocase")
        try:
            neu = buch.speichern(data.get("name"), faelle.kriterien(data.get("criteria")),
                                 fall_id, ordner)
        except ValueError:
            raise Ablehnung(400, "srv.case.noname") from None
        return self._json({"search": rest.gespeicherte_suche(buch.gespeichert(neu))}, 201,
                          extra={"Location": f"{API_V1}/searches/saved/{neu}"})

    def _v1_suche_aendern(self, p, _q, data):
        """PATCH: the fields the body names. `name` renames, `case` attaches
        the search to a case (`null` detaches it) and `case_folder` says
        into which of its folders new hits go – on its own, for the case
        the search is attached to; with `case`, for the new one."""
        buch, kennung = self.app.faelle, self._v1_suche_kennung(p)
        aktuell = buch.gespeichert(kennung)
        if aktuell is None:
            raise Ablehnung(404, "srv.search.unknown")
        if "name" in data and not str(data.get("name") or "").strip():
            raise Ablehnung(400, "srv.case.noname")
        fall_id = self._v1_fall_aus(data) if "case" in data else aktuell["fall"]
        if "case_folder" in data:
            ordner = self._ordner_aus(data, "case_folder")
        else:
            # Only the named fields change: the folder stays with its case
            # and goes only when the case does – a folder is one case's.
            ordner = aktuell["ordner"] if fall_id == aktuell["fall"] else None
        if ordner is not None and fall_id is None:
            # A folder is a case's; a search attached to none has nothing
            # it could file into – the same 400 the POST gives.
            raise Ablehnung(400, "srv.case.folder.nocase")
        if fall_id is not None and ("case" in data or "case_folder" in data):
            # Judged before the rename: a closed case or a folder it lacks
            # must not leave the new name behind. The case book raises,
            # the dispatcher answers – the one mapping for every route.
            fall = self.app.faelle.fall(fall_id)
            if fall is None:
                raise Ablehnung(404, "srv.case.unknown")
            self._v1_fall_offen(fall, ordner)
        if "name" in data:
            buch.umbenennen(kennung, data.get("name"))
        if "case" in data or "case_folder" in data:
            buch.anhaengen(kennung, fall_id, ordner)
        return self._json({"search": rest.gespeicherte_suche(buch.gespeichert(kennung))})

    def _v1_suche_loeschen(self, p, _q, _data):
        if not self.app.faelle.loeschen(self._v1_suche_kennung(p)):
            raise Ablehnung(404, "srv.search.unknown")
        return self._send(204, b"")

    def _v1_suche_kennung(self, p):
        return self._v1_id(p, "id", "srv.search.unknown")

    def _v1_fall_aus(self, data, feld="case"):
        """The case a body names: a number, or None for "no case". A case
        that does not exist is a 404, not a silent detach."""
        wert = data.get(feld)
        if wert in (None, "", 0, "0"):
            return None
        try:
            kennung = int(wert)
        except (TypeError, ValueError):
            # Not a missing case but a malformed request – like a folder
            # that is no id (`_ordner_aus`).
            raise Ablehnung(400, "srv.case.badcase", {"value": str(wert)[:40]}) from None
        if self.app.faelle.fall(kennung) is None:
            raise Ablehnung(404, "srv.case.unknown")
        return kennung

    def _v1_personen(self, _p, q, _data):
        grenze = self._zahl(q, "limit", 50, 1, 200)
        return self._v1_liste(self._people(q, grenze + 1), "people", grenze)

    def _v1_adressen(self, _p, q, _data):
        grenze = self._zahl(q, "limit", 12, 1, 100)
        return self._v1_liste(self._addresses(q, grenze + 1), "addresses", grenze)

    def _v1_gespraech(self, _p, q, _data):
        grenze = self._zahl(q, "limit", 50, 1, 200)
        res = self._thread(q, grenze + 1)
        res["messages"] = rest.treffer(res.get("messages") or [])
        return self._v1_liste(res, "messages", grenze)

    def _v1_dokument(self, _p, q, _data):
        return self._json(self._document(q))

    def _v1_fakten(self, _p, q, _data):
        return self._json(self._detail(q))

    def _v1_anhang(self, _p, q, _data):
        """One attachment of a mail as a download: the n-th real one
        (1-based), in the order the facts list them – so what the detail
        shows as a chip is what this hands out."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error)
        n = self._zahl(q, "n", 1, 1, 999)
        con = mod._db()
        try:
            row = con.execute("SELECT src, root, rel FROM chunks WHERE uid = ? AND seq = 0",
                              (q.get("uid", ""),)).fetchone()
        finally:
            con.close()
        if row is None:
            raise Ablehnung(404, "srv.detail.none")
        teil = None
        if row["src"] == "outlook":
            ziel, _fehler = mod._resolve_source(row["root"], row["rel"])
            teil = detail.anhang(ziel, n) if ziel is not None else None
        if teil is None:
            raise Ablehnung(404, "srv.attach.none", {"n": n})
        name, inhalt, ctype = teil
        # What lies in a mail was written by someone else: offered as a
        # download under a harmless name, never shown in this origin.
        return self._send(200, inhalt, ctype, extra={
            "Content-Disposition": f'attachment; filename="{_sicherer_name(name)}"',
            "Content-Security-Policy": "sandbox"})

    def _v1_kalender(self, _p, _q, _data):
        return self._calendar()

    def _v1_umgebung(self, _p, _q, _data):
        return self._json(self.app.umgebung())

    def _v1_bestand(self, _p, _q, _data):
        return self._json(self.app.bestand())

    def _v1_konfig(self, _p, _q, _data):
        return self._json({"config": self.app.cfg})

    def _v1_konfig_aendern(self, _p, _q, data):
        """PATCH: the keys the body names, the rest stays – the same merge
        the app's own route does, under the method that says so."""
        return self._json({"config": self._save_config(data)["config"]})

    # -- Route implementations --------------------------------------------
    def _bilanz_holen(self, eintrag):
        """"Fetch now" on a balance row: only what the row found open, the
        cheapest way each source allows. The mailbox: a resync limited to
        the folders with something open. The mirrors: the open files by
        id, straight from the stored report – no walk – unless the report
        had to cap them, then a resync. Every other source: its regular
        run, which fetches what changed anyway. The row is judged afresh
        at the end of the same run (the mirrors adjust it themselves)."""
        app = self.app
        quelle = eintrag["quelle"]
        if app.jobs.busy:
            raise Ablehnung(409, "srv.busy")
        ordner = eintrag["ordner"]
        with state_db.StateDb(BASE / EXPORT_ORDNER[ordner]) as db:
            bericht = completeness.lesen(db, quelle) or {}
        anfrage = {**eintrag["lauf"], "index": True, eintrag["anfrage"]: True}
        (key,) = [k for k, an in eintrag["lauf"].items() if an]
        if ordner == "outlook":
            ok, why = app.launch(anfrage, label='job.holen', sync_now=True, resync=True,
                                 resync_ordner=[z["pfad"] for z in bericht.get("zeilen") or []])
        elif ordner in ("onedrive", "sharepoint") and bericht.get("offene") \
                and not bericht.get("offene_gekappt"):
            liste = HEIM / f"nachholen-{key}.json"
            export_util.schreibe_atomar(liste, json.dumps(
                {"quelle": key, "dateien": bericht["offene"]}, ensure_ascii=False))
            anfrage.pop(eintrag["anfrage"])          # the fetch adjusts the row itself
            ok, why = app.launch(anfrage, label='job.holen', sync_now=True,
                                 nachholen={"quelle": key, "liste": str(liste)})
        elif ordner in ("onedrive", "sharepoint"):
            ok, why = app.launch(anfrage, label='job.holen', sync_now=True, resync=True)
        else:
            ok, why = app.launch(anfrage, label='job.holen', sync_now=True)
        if not ok:
            raise Ablehnung(409, why)
        return self._lauf_antwort()

    def _archiv(self, aktion, data):
        """The archive check's actions (archive_check.py): one source at a
        time, explicit, each a run of its own – the run window shows what
        happens, the run history keeps it, and nothing runs while another
        run writes into the folders. The step judges the source afresh
        afterwards and updates its row in the stored report; the rebuild's
        run carries the source's export and the index behind it. Opening
        the folder is no process and answers at once."""
        app = self.app
        quelle = str(data.get("quelle") or "").strip()
        unterordner = EXPORT_ORDNER.get(quelle)
        if unterordner is None or quelle not in archive_check.PRUEFER:
            # Unreachable from /sources, whose routes resolve the source
            # first – but when it answers, it answers like them: a name
            # that is none of the eight is a 404.
            raise Ablehnung(404, "srv.archiv.unknown", {"source": quelle})
        ordner = BASE / unterordner
        if aktion == "ordner":
            if not archive_check.ordner_oeffnen(ordner if ordner.is_dir() else BASE):
                raise Ablehnung(500, "srv.archiv.open.fail")
            return self._json({"path": str(ordner if ordner.is_dir() else BASE)})
        if app.jobs.busy:
            raise Ablehnung(409, "srv.busy")
        if not ordner.is_dir() or (aktion == "neu-aufbauen"
                                   and not archive_check.beschaedigte(ordner)):
            return self._json({"message": {"k": "srv.archiv.nothing", "v": {}}})
        if aktion == "nachholen":
            # "Fetch again": exactly the files the stored report found
            # missing or short, written to a list the source's step reads;
            # the row is judged afresh at the end of the same run.
            zeile = archive_check.zeile_lesen(archiv_bericht_pfad(), quelle)
            befunde = (zeile or {}).get("befunde") or {}
            dateien = list(befunde.get("fehlt") or []) + list(befunde.get("unvollstaendig") or [])
            if not dateien:
                return self._json({"message": {"k": "srv.archiv.nothing", "v": {}}})
            liste = HEIM / f"nachholen-{quelle}.json"
            export_util.schreibe_atomar(liste, json.dumps(
                {"quelle": quelle, "dateien": dateien}, ensure_ascii=False))
            ok, why = app.launch({quelle: True, "index": True, "archiv_pruefen": True},
                                 label='job.archiv.nachholen', sync_now=True,
                                 archiv={"quelle": quelle},
                                 nachholen={"quelle": quelle, "liste": str(liste)})
            if not ok:
                raise Ablehnung(409, why)
            return self._lauf_antwort()
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
        return self._lauf_antwort()

    def _page(self):
        """Serve the interface together with its strings.

        The language is thus settled at first render – loading it later
        would mean briefly showing the wrong language.
        """
        code = i18n.negotiate(self.app.cfg.get("language"),
                              self.headers.get("Accept-Language"), RES)
        self.app.ui_lang = code       # notifications speak the page language
        nutzlast = json.dumps({"lang": code, "strings": i18n.strings(code, RES),
                               "languages": i18n.available(RES)},
                              ensure_ascii=False).replace("<", "\\u003c")
        schritte = json.dumps(steps_mod.ui_metadaten(),
                              ensure_ascii=False).replace("<", "\\u003c")
        pruefungen_meta = json.dumps(steps_mod.pruef_metadaten(),
                                     ensure_ascii=False).replace("<", "\\u003c")
        return (seite().replace("/*__I18N__*/", nutzlast)
                    .replace("/*__STEPS__*/", schritte)
                    .replace("/*__PRUEFUNGEN__*/", pruefungen_meta))

    def _save_token(self, data):
        token = normalize_token(data.get("token"))
        if not token:
            raise Ablehnung(400, "srv.token.empty")
        if len(token) < 40:
            raise Ablehnung(400, "srv.token.short")
        write_token(token)
        self.app.jobs.token_expired = False
        st = token_status(token, needed=self.app.selected_categories())
        if st["expired"]:
            raise Ablehnung(400, "srv.token.stale", token=st)
        msg = ({"k": "srv.token.saved.scopes", "v": {"list": ", ".join(st["missing"])}}
               if st["missing"] else {"k": "srv.token.saved", "v": {}})
        self.app.jobs.log(msg, "warn" if st["missing"] else "ok")
        return {"message": msg, "token": st}

    def _save_config(self, data):
        def uebernehmen(cfg):
            if "outlook_categories" in data:
                cfg["outlook_categories"] = _clean_categories(
                    data["outlook_categories"], ["mail", "calendar", "contacts"])
            if "teams_categories" in data:
                cfg["teams_categories"] = _clean_categories(
                    data["teams_categories"], ["1on1", "group", "meeting", "channels"])
            for key in ("embed_model",
                        "chat_model", "ollama"):
                if key in data and str(data[key]).strip():
                    cfg[key] = str(data[key]).strip()
            # Bounds so a mistyped number cannot cripple the next run: Graph
            # allows 4 concurrent requests per mailbox, anything above mostly
            # produces throttling; ports beyond 65535 do not exist.
            for key, low, high in (("workers", 1, 8), ("mirror_workers", 1, 16),
                                   ("mcp_port", 1024, 65535),
                                   ("index_batch", 1, 512), ("answer_sources", 1, 20),
                                   ("semantic_min", 0, 95),
                                   ("onedrive_max_mb", 0, 100000),
                                   ("sharepoint_max_mb", 0, 100000),
                                   ("sharepoint_pages_image_max_mb", 0, 100),
                                   ("onenote_image_max_mb", 0, 100),
                                   ("teams_files_max_mb", 0, 100000),
                                   # 0 = the whole calendar, every run.
                                   ("calendar_months_back", 0, 240),
                                   # 0 = never re-read the chat comments.
                                   ("planner_sweep_hours", 0, 8760),
                                   ("search_results", 5, 100),
                                   # Below five seconds the page would ask
                                   # more often than anything can change.
                                   ("status_poll_seconds", 5, 600),
                                   # 0 means: userflow recording off.
                                   ("userflow_actions", 0, 50),
                                   ("runs_retention_months", 1, 120),
                                   ("log_retention_days", 1, 365)):
                if key in data:
                    try:
                        cfg[key] = max(low, min(high, int(data[key])))
                    except (TypeError, ValueError):
                        pass
            for key in ("mcp_enabled", "mcp_autostart", "update_check", "embed_images", "cache_images",
                        "refresh_channels", "skip_empty_chats", "include_hidden",
                        "calendar_reconstruct", "ollama_enabled", "index_semantic",
                        # Missing since the checkbox exists: the state reached
                        # the run but never survived a page rebuild.
                        "onedrive_enabled", "sharepoint_enabled",
                        "sharepoint_pages_enabled", "planner_enabled",
                        "planner_attachments", "todo_enabled",
                        "onenote_enabled", "teams_attachments",
                        "teams_channel_files", "keep_awake", "mcp_cases_write"):
                if key in data:
                    cfg[key] = bool(data[key])
            if "search_history" in data:
                wahl = str(data["search_history"] or "").strip().lower()
                if wahl in HISTORIE_WAHL:
                    cfg["search_history"] = wahl
                    # The new rule applies at once – "off" empties the list.
                    self.app.faelle.aufraeumen(historie_tage(cfg))
            if "case_export_dir" in data:
                cfg["case_export_dir"] = str(data["case_export_dir"] or "").strip()
            # Who counts as internal, and who you are – both plain text
            for k in ("internal_domains", "own_name"):
                if k in data:
                    cfg[k] = str(data[k] or "").strip()
            # Whoever switches Ollama off no longer means the check from just now.
            if "ollama_enabled" in data:
                self.app._ollama_cache = (0, None)
            if "calendar_rules" in data:
                cfg["calendar_rules"] = folders.schreibe_regeln(
                    folders.lies_regeln(str(data["calendar_rules"] or "")))
            if "folder_rules" in data:
                cfg["folder_rules"] = folders.schreibe_regeln(
                    folders.lies_regeln(str(data["folder_rules"] or "")))
            for key in ("sharepoint_urls", "sharepoint_pages_urls",
                        "planner_urls"):
                if key in data:
                    cfg[key] = "\n".join(
                        z.strip() for z in str(data[key] or "").splitlines()
                        if z.strip())
            for key in ("sharepoint_types_include", "sharepoint_types_exclude"):
                if key in data:
                    cfg[key] = ", ".join(
                        e for e in (s.strip().lstrip(".").lower()
                                    for s in str(data[key] or "").split(","))
                        if e)
            if "onedrive_rules" in data:
                cfg["onedrive_rules"] = folders.schreibe_regeln(
                    folders.lies_regeln(str(data["onedrive_rules"] or "")))
            for key in ("onenote_rules", "teams_rules", "sharepoint_rules",
                        "todo_rules"):
                if key in data:
                    cfg[key] = folders.schreibe_regeln(
                        folders.lies_regeln(str(data[key] or "")))
            # A day or nothing: anything else would silently mean "nothing
            # older than never".
            for key in ("outlook_since", "teams_since"):
                if key in data:
                    cfg[key] = _clean_datum(data[key])
            if "analytics_skip" in data:
                cfg["analytics_skip"] = _clean_zeilen(data["analytics_skip"])
            if "mcp_enabled" in data and not cfg.get("mcp_enabled", True):
                self.app.mcp.stop()
            if "filetype_hidden" in data:
                cfg["filetype_hidden"] = _clean_endungen(data["filetype_hidden"])
            if "skip_folders" in data:
                cfg["skip_folders"] = _clean_folders(data["skip_folders"])
            if "auth_mode" in data:
                # Anything unknown becomes token mode – the path that works
                # without asking IT.
                cfg["auth_mode"] = ("login" if str(data["auth_mode"]).strip().lower()
                                    == "login" else "token")
            for key in ("client_id", "tenant"):
                if key in data:
                    cfg[key] = str(data[key] or "").strip()
            if "sync_cadence" in data and isinstance(data["sync_cadence"], dict):
                cfg["sync_cadence"] = {
                    str(k): v for k, v in data["sync_cadence"].items()
                    if v in ("always", "daily", "weekly", "monthly")}
            if "notifications" in data:
                wert = str(data["notifications"] or "").strip().lower()
                if wert in ("off", "errors", "all"):
                    cfg["notifications"] = wert
            if "tour_seen" in data and isinstance(data["tour_seen"], dict):
                cfg["tour_seen"] = {k: bool(v) for k, v in data["tour_seen"].items()
                                    if k in ("archiv", "suche", "quelle")}
            if "language" in data:
                # Only known codes – one typo otherwise and the interface would
                # speak the fallback language forever.
                gewuenscht = str(data["language"] or "auto").strip().lower()
                erlaubt = {e["code"] for e in i18n.available(RES)} | {"auto"}
                if gewuenscht in erlaubt:
                    cfg["language"] = gewuenscht
        self.app.konfiguriere(uebernehmen)
        return {"config": self.app.cfg}

    def _save_schedule(self, data):
        vorher = dict(self.app.cfg["schedule"])

        def uebernehmen(cfg):
            plan = cfg["schedule"]
            for key in ("enabled", "outlook", "teams", "onedrive",
                        "sharepoint", "sharepoint_pages", "planner", "todo",
                        "onenote", "index", "calendar"):
                if key in data:
                    plan[key] = bool(data[key])
            if "interval_minutes" in data:
                try:
                    plan["interval_minutes"] = max(
                        5, int(data["interval_minutes"]))
                except (TypeError, ValueError):
                    pass
        self.app.konfiguriere(uebernehmen)
        plan = self.app.cfg["schedule"]
        # Unchanged plan, unchanged clock. The page already asks before it
        # posts; this covers everyone else on the documented API, for whom
        # a no-op save would otherwise push a nearly due run back by a
        # whole interval.
        if plan != vorher:
            self.app.scheduler.reset()
            self.app.jobs.logk("srv.sched.state", "info",
                               min=plan["interval_minutes"],
                               state={"k": "srv.sched.on" if plan["enabled"]
                                      else "srv.sched.off", "v": {}})
        return {"schedule": plan,
                "next": self.app.scheduler.next_due()}

    def _mcp(self, an):
        """Start or stop the MCP server – what `running` on PATCH /mcp
        asks for. A refused start still answers the server's state, so
        the page can draw it."""
        if an:
            ok, why = self.app.mcp.start(self.app.cfg)
            stand = self.app.mcp.status(self.app.cfg)
            if not ok:
                raise Ablehnung(409, why, mcp=stand)
            return {"message": why, "mcp": stand}
        self.app.mcp.stop()
        return {"mcp": self.app.mcp.status(self.app.cfg)}

    def _answer(self, data):
        """Have an answer worded from the hits of a search.

        The search uses the same function as the tab next door – the answer
        thus sees exactly the hits that are in the list. A second retrieval
        here would mean it could cite things nobody can look up.

        The answer streams out piecewise (one JSON line per piece): a local
        model easily takes a minute for a paragraph, and nobody endures
        that much waiting in front of an empty box.
        """
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error)
        oll = self.app.ollama()
        if not (oll["running"] and oll["has_chat_model"]):
            raise Ablehnung(503, "srv.answer.nomodel",
                            {"model": self.app.cfg["chat_model"]})

        query = str(data.get("q") or "").strip()
        if not query:
            raise Ablehnung(400, "srv.answer.noquery")
        k = max(1, min(int(self.app.cfg.get("answer_sources", 8)), 20))
        res = mod.search_messages(
            query=query, person=str(data.get("person") or ""),
            date_from=str(data.get("from") or ""), date_to=str(data.get("to") or ""),
            source=str(data.get("source") or "all"), k=k, preview_chars=0)
        treffer = res.get("results") or []
        if not treffer:
            raise Ablehnung(404, "srv.answer.nohits")

        # Full text per hit: the preview in the list is too short to answer
        # anything from.
        quellen = []
        for h in treffer:
            doc = mod.get_document(uid=h["uid"])
            quellen.append({**h, "text": doc.get("text") or ""})

        lang = i18n.negotiate(self.app.cfg.get("language"),
                              self.headers.get("Accept-Language"), RES)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._kennung()
        self.send_header("Connection", "close")   # end of stream = end of connection
        self.end_headers()
        self.close_connection = True

        def schicke(obj):
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()

        schicke({"sources": [{"n": i, "uid": h["uid"], "title": h.get("title"),
                              "who": h.get("who"), "date": h.get("date"),
                              "uri": h.get("uri")}
                             for i, h in enumerate(treffer, 1)],
                 "model": self.app.cfg["chat_model"]})
        try:
            for stueck in answer.stream(query, quellen, self.app.cfg["chat_model"],
                                        self.app.cfg["ollama"], lang):
                schicke(stueck)
            schicke({"done": True})
        except (BrokenPipeError, ConnectionResetError):
            pass          # window closed or aborted – no reason for noise

    def _search(self, q, merken=True):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error, items=[], count=0)
        k = faelle.kriterien(q)
        mod.STATE["internal_domains"] = self._interne_domains()
        kw = dict(person=k["person"], date_from=k["from"], date_to=k["to"],
                  source=k["source"],
                  # `limit` is the name everywhere else in the API; `k` is
                  # the search's own and stays. One above the largest page
                  # is allowed: that is how /api/v1/search sees has_more.
                  k=self._zahl(q, "k", q.get("limit") or 20, 1, 101),
                  offset=self._zahl(q, "offset", 0, 0),
                  only_gone=k["gone"], folder=k["folder"], filetype=k["filetype"],
                  party=k["party"], with_attachments=k["attachments"],
                  # The four mail lines (13.0): they narrow mail, nothing else.
                  **{m: k[m] for m in faelle.MAIL},
                  # The "Cases" filter: only what one case – or one of its
                  # folders – holds.
                  case=str(k["fall"]) if k["fall"] else "",
                  case_folder=str(k["ordner"]) if k["fall"] and k["ordner"] else "")
        if k["q"]:
            res = mod.search_messages(query=k["q"], mode=_modus(q.get("mode")), **kw)
        else:
            res = mod.browse_messages(**kw)
        if res.get("error"):
            # The engine's own refusals: a filter naming an unknown case or
            # folder, a mode this index cannot rank, the embedder failing.
            raise Ablehnung(409, res["error"], hits=[], count=0)
        res["semantic"] = bool(mod.STATE.get("semantic"))
        if kw["offset"] == 0 and merken:
            # The first page of a search is the search: the history keeps
            # its criteria (never its hits) when the setting allows, and a
            # saved search that was run remembers when and how many.
            if historie_tage(self.app.cfg) != 0:
                self.app.faelle.suche_merken(k, res.get("count", 0))
            try:
                gespeichert = self._zahl(q, "saved", 0, 0)
            except ValueError:
                gespeichert = 0
            if gespeichert:
                self.app.faelle.gelaufen(gespeichert, res.get("count", 0))
        return res

    def _interne_domains(self):
        """Which mail domains are "us": the setting, else the domain of the
        signed-in account – handed to the search engine with every search,
        so a changed setting counts at once."""
        roh = str(self.app.cfg.get("internal_domains") or "").strip()
        if roh:
            return roh
        konto = str(token_status(read_token()).get("account") or "")
        return konto.rsplit("@", 1)[1].lower() if "@" in konto else ""

    def _alle_treffer(self, k, grenze=5000):
        """Every hit of a search, for a result list: the criteria as the
        page had them, paged through the same engine up to `grenze`."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error)
        mod.STATE["internal_domains"] = self._interne_domains()
        kw = dict(person=k["person"], date_from=k["from"], date_to=k["to"],
                  source=k["source"], only_gone=k["gone"], folder=k["folder"],
                  filetype=k["filetype"], with_attachments=k["attachments"],
                  case=str(k["fall"]) if k["fall"] else "",
                  case_folder=str(k["ordner"]) if k["fall"] and k["ordner"] else "",
                  party=k["party"], preview_chars=0,
                  **{m: k[m] for m in faelle.MAIL})
        modus = MODUS[k["mode"]]
        treffer, offset, schritt = [], 0, 100
        while offset < grenze:
            if k["q"]:
                res = mod.search_messages(query=k["q"], mode=modus, k=schritt, offset=offset, **kw)
            else:
                res = mod.browse_messages(k=schritt, offset=offset, **kw)
            if res.get("error"):
                return None, res["error"]
            seite = res.get("results") or []
            treffer += seite
            if len(seite) < schritt:
                break
            offset += schritt
        return treffer[:grenze], None

    # -- what a body names ---------------------------------------------------
    @staticmethod
    def _ordner_aus(data, feld="ordner"):
        """A folder id from a request body: an integer, or None for
        "unsorted". Anything else is the caller's mistake – silently
        unfiling an item on a value nobody could read looks exactly like
        the documented way to say "out of its folder"."""
        wert = data.get(feld)
        if wert in (None, "", 0, "0"):
            return None
        try:
            return int(wert)
        except (TypeError, ValueError):
            raise Ablehnung(400, "srv.case.badfolder",
                            {"value": str(wert)[:40]}) from None

    # -- what a case is, beyond what the case book keeps ---------------------
    def _fall_voll(self, kennung):
        """The case as every write answers it – with the conversations'
        state on its items."""
        fall = self.app.faelle.fall(kennung)
        if fall is not None:
            self._index_stand(fall)
        return fall

    def _index_stand(self, fall):
        """What the index knows about the case's items beyond what the case
        remembers: `thread_offen` – how many messages of the item's
        conversation the case lacks, 0 where there is none – and
        `wer_mail`, the address behind `wer` where the index has one. Two
        queries, whatever the case's size; an index without the columns
        leaves the zeros and the empty strings."""
        for e in fall["eintraege_liste"]:
            e["thread_offen"] = 0
            e["wer_mail"] = ""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None or not fall["eintraege_liste"]:
            return
        try:
            con = mod._db()
        except Exception:
            return
        try:
            if not mod._hat_spalte(con, "key"):
                return
            mit_thread = mod._hat_spalte(con, "thread")
            mit_adresse = mod._hat_spalte(con, "who_mail")
            if not (mit_thread or mit_adresse):
                return
            mod._keys_tabelle(con, {e["key"] for e in fall["eintraege_liste"]})
            felder = "key, " + ("thread" if mit_thread else "NULL") + ", " + ("who_mail" if mit_adresse else "NULL")
            je_key, im_fall, adressen = {}, {}, {}
            for key, thread, who_mail in con.execute(
                    f"SELECT {felder} FROM chunks WHERE seq = 0 AND key IN (SELECT key FROM fallkeys)"):
                if thread:
                    je_key[key] = thread
                    im_fall[thread] = im_fall.get(thread, 0) + 1
                if who_mail:
                    adressen[key] = who_mail
            for e in fall["eintraege_liste"]:
                e["wer_mail"] = adressen.get(e["key"], "")
            if not je_key:
                return
            con.execute("CREATE TEMP TABLE IF NOT EXISTS fallthreads(thread TEXT PRIMARY KEY)")
            con.execute("DELETE FROM fallthreads")
            con.executemany("INSERT OR IGNORE INTO fallthreads(thread) VALUES(?)",
                            ((t,) for t in im_fall))
            gesamt = dict(con.execute(
                "SELECT thread, COUNT(*) FROM chunks WHERE seq = 0 AND key IS NOT NULL AND key != '' "
                "AND thread IN (SELECT thread FROM fallthreads) GROUP BY thread"))
            for e in fall["eintraege_liste"]:
                t = je_key.get(e["key"])
                if t:
                    e["thread_offen"] = max(0, gesamt.get(t, 0) - im_fall.get(t, 0))
        finally:
            con.close()

    def _gespraeche_moeglich(self):
        """Can this index answer for conversations at all? The columns came
        with 11.0/11.1; an older index cannot, and that has to be known
        before anything is written."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error)
        con = mod._db()
        try:
            if not (mod._hat_spalte(con, "thread") and mod._hat_spalte(con, "key")):
                return {"k": "srv.case.nothread", "v": {}}
        finally:
            con.close()
        return None

    def _thread_holen(self, kennung, keys):
        """The rest of these items' conversations into the case – each
        message into the folder its item sits in. Returns (added, error)."""
        buch = self.app.faelle
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error)
        fall = buch.fall(kennung)
        con = mod._db()
        try:
            if not (mod._hat_spalte(con, "thread") and mod._hat_spalte(con, "key")):
                return 0, {"k": "srv.case.nothread", "v": {}}
            im_fall = {e["key"]: e for e in fall["eintraege_liste"]}
            neu = 0
            for key in keys:
                e = im_fall.get(key)
                if e is None:
                    continue
                r = con.execute("SELECT thread FROM chunks WHERE key = ? AND seq = 0 LIMIT 1",
                                (key,)).fetchone()
                if r is None or not r[0]:
                    continue
                rows = con.execute(
                    "SELECT * FROM chunks WHERE thread = ? AND seq = 0 AND key IS NOT NULL AND key != '' "
                    "ORDER BY ts IS NULL, ts LIMIT 500", (r[0],)).fetchall()
                eintraege = [{"key": m["key"], "src": m["src"], "root": m["root"], "rel": m["rel"],
                              "titel": m["title"], "datum": m["date"], "wer": m["who"]}
                             for m in rows if m["key"] not in im_fall]
                if eintraege:
                    neu += buch.hinzufuegen(kennung, eintraege, ordner_id=e.get("ordner"))
                    for x in eintraege:
                        im_fall[x["key"]] = x
            return neu, None
        finally:
            con.close()


    def _fall_export(self, kennung):
        """The export as a run of its own (case_export.py): one ZIP, named
        here before the run, so the answer can already say where it will
        lie."""
        app = self.app
        fall = app.faelle.fall(kennung)
        if app.jobs.busy:
            raise Ablehnung(409, "srv.busy")
        basis = fall_export_basis(app.cfg)
        try:
            basis.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise Ablehnung(400, "srv.case.exportdir", {"error": str(e)}) from None
        ziel = case_export.zielordner(basis, fall["name"])
        ok, why = app.launch({"fall_export": True}, label="job.case_export",
                             fall_export={"faelle": str(HEIM / faelle.DB_NAME), "fall": kennung,
                                          "ziel": str(ziel),
                                          "lang": app.ui_lang or i18n.negotiate(app.cfg.get("language"), None, RES),
                                          "res": str(RES)})
        if not ok:
            raise Ablehnung(409, why)
        return self._lauf_antwort(extra={"path": str(ziel.with_suffix(".zip"))})


    def _thread(self, q, grenze):
        """All messages of one conversation – the same evaluation as in MCP."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error, items=[], count=0)
        res = mod.get_thread(thread=q.get("key", ""),
                             limit=grenze)
        if res.get("error"):
            raise Ablehnung(409, res["error"], items=[], count=0)
        # Every message says which cases it sits in – the add-to-case
        # window counts from that how much of the conversation is there.
        marken = getattr(mod, "_mit_faellen", None)
        if res.get("messages") and marken:
            marken(res["messages"])
        return res

    def _folders(self, q, grenze):
        """Which mailbox folders are in the archive – for the filter."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error, items=[])
        return mod.list_folders(contains=q.get("contains", ""),
                                limit=grenze,
                                source=q.get("source", ""))

    def _ordnerplan(self, data):
        """What the next run would do – without starting it.

        Takes the rules from the form, not the saved ones: otherwise the
        preview would show the previous state while the new rule already
        sits next to it.

        Three sources, one evaluation. The difference is small enough that
        further copies do not pay: for the mailbox the `.eml` files count,
        for the calendars the `.ics`, for the mirror all files.
        """
        cfg = self.app.cfg
        quelle = str(data.get("quelle") or "")
        if quelle == "sharepoint":
            return self._sharepoint_plan(data.get("sharepoint_rules"))
        if quelle == "onenote":
            return self._notizbuch_plan(data.get("onenote_rules"))
        if quelle == "teams":
            return self._teams_plan(data.get("teams_rules"))
        if quelle == "todo":
            return self._todo_plan(data.get("todo_rules"))
        datei = folders.KALENDER if quelle == "calendar" else folders.DATEI
        if quelle == "onedrive":
            ordner, endung = BASE / ONEDRIVE_DIR, None
        else:
            ordner, endung = BASE / OUTLOOK_DIR, (
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
            regeln = kalenderregeln(cfg, daten, data.get("calendar_rules"))
        else:
            regeln = auswahlregeln(cfg, data.get("folder_rules"),
                                   data.get("skip_folders"))
        return {"regeln": folders.schreibe_regeln(regeln),
                **folders.plan(ordner, regeln, daten, endung, datei)}

    def _notizbuch_plan(self, roh):
        """The export list for the notebooks: the stored list against the
        rules from the form, pages counted per notebook. A notebook keeps
        its pages in section folders, so the count on disk is summed up
        to the notebook – and every folder in the export root is walked,
        so a notebook that left the list still shows as "only here"."""
        wurzel = BASE / ONENOTE_DIR
        daten = folders.lade(wurzel, folders.NOTIZBUECHER)
        if not daten:
            raise Ablehnung(404, "srv.plan.nolist", leer=True)
        regeln = notizbuchregeln(self.app.cfg, roh)
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

    def _teams_plan(self, roh):
        """The export list for Teams: the stored conversation list against
        the rules from the form. A conversation is one file, named after
        its title with the id as suffix – so "in the archive" is counted
        per title, and a file whose title left the list shows as "only
        here"."""
        wurzel = BASE / TEAMS_DIR
        daten = folders.lade(wurzel, folders.DATEI)
        if not daten:
            raise Ablehnung(404, "srv.plan.nolist", leer=True)
        regeln = teamsregeln(self.app.cfg, roh)
        archiv = {}
        if wurzel.is_dir():
            for datei in wurzel.glob("*/*.html"):
                archiv[f"{datei.parent.name}/{_ohne_kennung(datei.stem)}"] = \
                    archiv.get(f"{datei.parent.name}/{_ohne_kennung(datei.stem)}", 0) + 1
            for datei in wurzel.glob("channels/*/*.html"):
                pfad = f"channels/{datei.parent.name}/{_ohne_kennung(datei.stem)}"
                archiv[pfad] = archiv.get(pfad, 0) + 1
        return {"regeln": folders.schreibe_regeln(regeln),
                **folders.plan(wurzel, regeln, daten, ".html",
                               folders.DATEI, archiv=archiv)}

    def _todo_plan(self, roh):
        """The export list for To Do: the stored list of lists against the
        rules from the form; a list's folder carries the id as suffix, its
        tasks are counted from the folder's state."""
        wurzel = BASE / TODO_DIR
        daten = folders.lade(wurzel, folders.DATEI)
        if not daten:
            raise Ablehnung(404, "srv.plan.nolist", leer=True)
        regeln = todoregeln(self.app.cfg, roh)
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
                pfad = _ohne_kennung(ordner.name)
                archiv[pfad] = archiv.get(pfad, 0) + n
        return {"regeln": folders.schreibe_regeln(regeln),
                **folders.plan(wurzel, regeln, daten, ".html",
                               folders.DATEI, archiv=archiv)}

    def _sharepoint_plan(self, roh=None):
        """The export list for the SharePoint mirror: every library's tree,
        paths prefixed with site/library, judged by the path rules on top
        of the URL list. Each entry names the URLs its library came from,
        so the page can tell which cadence row paces it."""
        wurzel = BASE / SHAREPOINT_DIR
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
        regeln = sharepointregeln(self.app.cfg, roh)
        plan = folders.plan(wurzel, regeln, daten, None)
        # The walk under the site roots also sees each library's bookkeeping
        # (state.db and other leftovers) – real content lives below Dateien/.
        plan["weg"] = [z for z in plan["weg"]           # drive_mirror.DATEI_DIR
                       if "/Dateien/" in z["pfad"] + "/"]
        plan["mails_weg"] = sum(z["archiv"] for z in plan["weg"])
        return {"regeln": folders.schreibe_regeln(regeln), **plan}

    def _files(self, q):
        """One level of the mirrored file tree, sizes taken from disk."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error, roots=[], dirs=[], files=[])
        r = mod.list_files(root=q.get("root", ""), path=q.get("path", ""))
        # The root->directory knowledge lives in one place: the resolver's
        # STATE, which /source uses too – no second hand-copied map here.
        ordner = mod.STATE.get(f'{r.get("root")}_dir')
        basis = Path(ordner) if ordner else None
        for e in r.get("files") or ():
            try:
                e["size"] = (basis / e["rel"]).stat().st_size if basis else None
            except OSError:
                e["size"] = None
        return r

    def _filetypes(self, q):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error, items=[])
        # Hiding happens here and not in the tool: list_filetypes is meant
        # to say what is in the archive – to Claude as well. The trimming is
        # a question of the interface, not of the corpus. Hence fetch
        # everything first, then hide; the route cuts to the requested
        # number and knows from what is left whether it cut anything.
        aus = set(self.app.cfg.get("filetype_hidden") or [])
        r = mod.list_filetypes(limit=500, source=q.get("source", ""))
        liste = [e for e in r.get("filetypes", []) if e["type"] not in aus]
        return {"count": len(liste),
                "total_distinct": r.get("total_distinct", len(liste)),
                "hidden": sorted(aus),
                "filetypes": liste}

    def _people(self, q, grenze):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error, items=[])
        return mod.list_people(source=q.get("source", "all"),
                               contains=q.get("contains", ""),
                               limit=grenze)

    def _addresses(self, q, grenze):
        """The addresses of one mail line, for the Mail filter's suggestions
        (13.0). An unknown line is the caller's mistake, not ours."""
        mod = self.app.search.ensure(self.app.cfg)
        # A refusal keeps the shape of the answer it replaces: on this
        # surface a collection is `items`, empty or not.
        if mod is None:
            raise Ablehnung(503, self.app.search.error, items=[])
        res = mod.adressen(role=q.get("role", "from"), q=q.get("contains", ""),
                           limit=grenze)
        if res.get("error"):
            raise Ablehnung(400, res["error"], items=[])
        return res

    def _document(self, q):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error)
        res = mod.get_document(uid=q.get("uid", ""),
                               context_before=self._zahl(q, "before", 0, 0, 20),
                               context_after=self._zahl(q, "after", 0, 0, 20))
        if res.get("error"):
            raise Ablehnung(404, res["error"])
        return res

    def _detail(self, q):
        """The facts the hit's detail shows beyond the hit – per kind of
        item, from the index row and the source file (detail.py). The
        page draws only the keys that come back."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error)
        con = mod._db()
        try:
            row, text = mod._message_text(con, q.get("uid", ""))
        finally:
            con.close()
        if row is None:
            raise Ablehnung(404, "srv.detail.none")
        ziel, _fehler = mod._resolve_source(row["root"], row["rel"])
        return detail.fakten(row, text, ziel, mod.STATE)

    def _calendar(self):
        """Serve calendars, reconstructed appointments and contacts in one go.

        Compressed when the browser offers it: ~5 MB of JSON become
        ~0.75 MB. The evaluation itself runs as its own step (it reads
        every mail); here only its result file is passed through.
        """
        roh, gz = self.app.calendar_payload()
        if roh is None:
            return self._fehler(404, "cal.missing", recs=[])
        akzeptiert = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        if akzeptiert:
            return self._send(200, gz, "application/json; charset=utf-8",
                              {"Content-Encoding": "gzip"})
        return self._send(200, roh, "application/json; charset=utf-8")

    def _source(self, q):
        """Serve an exported source file (for the links in the hits).

        Content-Security-Policy: sandbox puts the page into its own opaque
        origin. An exported Teams HTML thus cannot run a script against
        this app's API, but still shows its embedded images.
        """
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            raise Ablehnung(503, self.app.search.error)
        target, err = mod._resolve_source(q.get("root", ""), q.get("path", ""))
        if err:
            raise Ablehnung(404, err)
        # Teams exports are made for reading and stay in the browser. All
        # the rest belongs to the program that knows it: an .eml as raw text
        # in a browser window is of use to nobody, in the mail client it is
        # a mail with attachments. The same goes for .ics and .vcf.
        endung = target.suffix.lower()
        ctype = _CONTENT_TYPE.get(endung, "application/octet-stream")
        # The archive's own HTML – boards, lists, pages, conversations –
        # links its files relatively ("Anhaenge/…", "Dateien/…", the page's
        # .files folder) so every file stands on its own offline. Viewed
        # through this route that would run into nothing at the app root –
        # so on delivery the relative links are rewritten onto the route.
        wurzel, pfad = q.get("root", ""), q.get("path", "")
        if endung in (".html", ".htm"):
            inhalt = _links_umleiten(target.read_bytes(), wurzel, pfad)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(inhalt)))
            self.send_header("Content-Security-Policy", "sandbox")
            self.send_header("X-Content-Type-Options", "nosniff")
            self._kennung()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(inhalt)
            return
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Security-Policy", "sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._kennung()
        if endung not in (".html", ".htm"):
            # The file name without path, and only with harmless characters:
            # it ends up in a header and in the download folder.
            self.send_header("Content-Disposition",
                             f'attachment; filename="{_sicherer_name(target.name)}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(target, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 64 * 1024)


_LINK_RE = re.compile(rb'(\b(?:href|src)=")([^"]+)(")')
_ABSOLUT_RE = re.compile(rb"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|/|#|\?)")


def _links_umleiten(inhalt, wurzel, pfad):
    """Relative href/src values of an archive HTML onto the file route.

    Absolute links, anchors, data URIs and the embedded images stay as they
    are; a relative path is resolved against the file's own folder and
    handed back through this route with the same root, so the sandboxed
    page reaches its attachments the way it does on disk."""
    ordner = pfad.rsplit("/", 1)[0] if "/" in pfad else ""

    def ersetze(m):
        wert = m.group(2)
        if _ABSOLUT_RE.match(wert):
            return m.group(0)
        ziel, _, anker = wert.partition(b"#")
        rel = unquote(html.unescape(ziel.decode("utf-8", "replace")))
        rel = posixpath.normpath(f"{ordner}/{rel}" if ordner else rel)
        neu = (f"{API_V1}/files/content?root={quote(wurzel, safe='')}"
               f"&path={quote(rel, safe='')}").encode()
        if anker:
            neu += b"#" + anker
        return m.group(1) + neu + m.group(3)

    return _LINK_RE.sub(ersetze, inhalt)


# What the operating system can make sense of. .eml opens the mail client,
# .ics the calendar, .vcf the contacts – provided the type is right.
_CONTENT_TYPE = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".eml": "message/rfc822",
    ".ics": "text/calendar; charset=utf-8",
    ".vcf": "text/vcard; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


def _sicherer_name(name):
    """File name for Content-Disposition: no quotes, no line breaks, no
    path – otherwise the header could be broken open."""
    sauber = re.sub(r'[\\"\r\n]', "_", Path(name).name).strip()
    return sauber or "datei"


def laeuft_bereits(port, host="127.0.0.1", timeout=1.5, profil=None):
    """Is an instance of this app already answering on the port?

    Without this check every further double-click would start a second
    instance on the next free port. Nobody notices that one – the app has
    no window and does not stay in the Dock either – and it could only be
    got rid of via Activity Monitor.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{API_V1}/status",
                                    timeout=timeout) as r:
            # The header says whose answer this is – every one of ours
            # carries it, and no other server on a free port will.
            unser = r.headers.get("X-Munimentum-Api")
            daten = json.loads(r.read().decode("utf-8"))
    except Exception:
        return False
    if not unser:
        return False
    # Something entirely different could be listening on the port; only
    # our own answer counts as "already running" – and only with the same
    # profile: another profile's instance keeps its port, this one takes
    # the next free one.
    if not (isinstance(daten, dict) and "token" in daten and "jobs" in daten):
        return False
    if profil is not None:
        laufend = (daten.get("profile") or {}).get("name") or settings.STANDARD_PROFIL
        return laufend == profil
    return True


class Server(ThreadingHTTPServer):
    """Like ThreadingHTTPServer, just without name resolution on bind.

    http.server calls `socket.getfqdn(host)` there – a reverse lookup for
    our own address whose result only lands in `server_name` and is needed
    nowhere. macOS 15 treats that as local-network access and asks on
    startup: „Darf Munimentum nach Geräten in lokalen Netzwerken suchen?“
    – a question this app has no claim to: it listens on 127.0.0.1 and
    otherwise talks only to Microsoft Graph.

    On top of that, the lookup cost time on every start before the
    interface appeared.
    """

    # SO_REUSEADDR (HTTPServer's default) lets a second Windows process bind
    # a port that is in use – two instances on one port, requests landing
    # on either. Exclusive use there; elsewhere the default keeps a quick
    # restart from tripping over TIME_WAIT.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]

    def handle_error(self, request, client_address):
        """A browser dropping its keep-alive connection is not an error.

        Reloads and closed tabs reset sockets all the time; the default
        handler prints a full traceback for each one and buries real errors
        in noise. Everything else still gets the standard report.
        """
        art = sys.exc_info()[0]
        if art is not None and issubclass(
                art, (ConnectionResetError, BrokenPipeError,
                      ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


PORT_VERSUCHE = 12          # a taken port: the next ones are tried
POLL = 0.5                  # serve_forever's poll interval (tests shorten it)


def make_server(app, port, host="127.0.0.1", tries=PORT_VERSUCHE, handler=None):
    """Bind the server and fix the allowed Host headers.

    Bind first, then build the list: with port=0 the operating system
    picks a free port, and that one must appear in the allowed headers.
    If the desired port is taken (second start, foreign program), the
    next ones are tried – a double-click must not end in a traceback
    nobody sees.
    """
    handler = handler or Handler
    httpd = None
    for versuch in range(tries if port else 1):
        try:
            httpd = Server((host, port + versuch), handler)
            break
        except OSError as e:
            if versuch == tries - 1:
                raise SystemExit(f"Kein freier Port ab {port}: {e}") from None
    real = httpd.server_address[1]
    handler.app = app
    handler.allowed_hosts = (f"{host}:{real}", f"localhost:{real}",
                             f"127.0.0.1:{real}", f"[::1]:{real}")
    return httpd


def eigene_instanz(port, host="127.0.0.1", profil=None, spanne=PORT_VERSUCHE):
    """The port on which an instance of this profile already answers – the
    one asked for, or one of the neighbours a bumped start may have taken
    (make_server tries the next ones) – or None. A second start must open
    that instance, never a second one on the same archive."""
    if not port:
        return None
    for p in range(max(1024, port - spanne + 1), port + spanne):
        if laeuft_bereits(p, host, timeout=0.5, profil=profil):
            return p
    return None


class Wahl(Handler):
    """The chooser's handler: the app's own transport – Host check, body
    cap, headers – with three routes of its own and no app behind it.
    Every connection is closed after its answer: a kept-alive one would
    outlive shutdown() and keep answering from the old server while the
    app already holds the port, and the page polling on it would wait
    forever."""
    ZUSATZ = {"Connection": "close"}
    ROUTEN = {
        API_V1 + "/status": ("GET",),
        API_V1 + "/profiles": ("GET",),
        API_V1 + "/profiles/{name}/open": ("POST",),
    }

    def _erlaubt(self, pfad, routen=None):
        """Every other path is the chooser's own page – it has no 404. The
        one parameterised route is matched by its pattern, so OPTIONS on a
        real profile path names POST as well."""
        methoden = set(self.ROUTEN.get(pfad, ()))
        for muster, erlaubt in self.ROUTEN.items():
            if "{" in muster and muster_passt(muster, pfad) is not None:
                methoden.update(erlaubt)
        return methoden | {"GET", "HEAD", "OPTIONS"}

    def _nur_v1(self, methode):
        """The chooser serves three routes of its own and has no app behind
        it – the versioned dispatcher it would inherit would walk into
        `self.app` and turn every write into a 500. Here they are simply
        methods this server does not have."""
        if not self._host_ok():
            return self._verboten()
        self._rumpf_weg()
        return self._methode_fehlt(urlsplit(self.path).path, methode)

    def do_GET(self):
        if not self._host_ok():
            return self._verboten()
        u = urlsplit(self.path)
        if u.path == API_V1 + "/profiles":
            return self._json(profil_status())
        if u.path == API_V1 + "/status":
            # Not the app yet: the page polls this until the app answers.
            return self._json({"chooser": True})
        code = i18n.negotiate(None, self.headers.get("Accept-Language"), RES)
        return self._send(200, profil_seite(code), "text/html; charset=utf-8")

    def do_POST(self):
        if not self._host_ok():
            return self._verboten()
        u = urlsplit(self.path)
        try:
            data = self._body()
        except Ablehnung as a:
            return self._fehler(a.code, a.grund, a.v, **a.extra)
        werte = muster_passt(API_V1 + "/profiles/{name}/open", u.path)
        if werte is not None:
            name = str(werte.get("name") or "").strip().lower()
            if name not in profil_namen():
                return self._fehler(404, "srv.profile.unknown", {"name": name})
            if not isinstance(data.get("ask_at_start", True), bool):
                return self._fehler(400, "srv.profile.badvalue", {"name": "ask_at_start"})
            profil_register_schreiben(zuletzt=name,
                                      ohne_nachfrage=not data.get("ask_at_start", True))
            antwort = {"name": name}
            lauft = eigene_instanz(self.server.server_address[1], profil=name)
            if lauft:
                # Already open in another instance: the page goes there,
                # and this start ends once the chooser has handed over.
                antwort["url"] = f"http://127.0.0.1:{lauft}/"
            self.server.gewaehlt["name"] = name
            self._json(antwort)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        return self._methode_fehlt(u.path, self.command or "POST")


def chooser_server(port, host="127.0.0.1"):
    """The chooser, bound on the app's port (or the next free one)."""
    httpd = make_server(None, port, host, handler=Wahl)
    httpd.gewaehlt = {}
    return httpd


def _wahl_abwarten(httpd, open_browser):
    """Serve the chooser until a profile is picked. Returns (name, True) –
    the browser is on the page by then, no second tab is wanted."""
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Profil wählen: {url}")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever(poll_interval=POLL)
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    finally:
        httpd.server_close()
    return httpd.gewaehlt["name"], True


def waehle_profil_im_browser(port, open_browser):
    return _wahl_abwarten(chooser_server(port), open_browser)


def serve(app, port, open_browser=True, host="127.0.0.1"):
    lauft = eigene_instanz(port, host, PROFIL) if port else None
    if lauft:
        url = f"http://{host}:{lauft}/"
        print(f"Läuft bereits – öffne {url}")
        print("Beenden geht dort oben rechts über „Beenden“.")
        if open_browser:
            webbrowser.open(url)
        return None
    httpd = make_server(app, port, host)
    port = httpd.server_address[1]
    url = f"http://{host}:{port}/"
    app.log_token_state()
    if _UMZUG.get("bewegt"):
        app.jobs.logk("srv.layout.moved", "info", path=_UMZUG["nach"])
    elif _UMZUG.get("fehler"):
        app.jobs.logk("srv.layout.movefail", "warn", path=_UMZUG["nach"],
                      detail=_UMZUG["fehler"])
    if _ALT_GEPINNT:
        # Detected, said, nothing moved: data and index stay in the flat
        # layout; splitting is on offer, never demanded.
        app.jobs.logk("srv.layout.kept", "info", data=str(BASE))
    # The paths stand in the settings; the log only speaks up when the
    # configured index folder holds no index.
    if app.cfg.get("index_dir") and not store_layout.db_path(STORE_PFAD).exists():
        app.jobs.logk("srv.layout.noindex", "warn", index=str(STORE_PFAD))
    for sperre in lauf_sperren():
        app.jobs.log(sperre, "err")
    app.check_updates()
    app.scheduler.start()
    app.autostart_mcp()
    print(f"Office-365-Export läuft: {url}")
    print("Beenden mit Strg+C (schließt auch den MCP-Server).")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    notify.set_open_url(url)       # Windows toasts open this on click
    if notify.install_click_handler(lambda: webbrowser.open(url)):
        # Bundled macOS app: a click on a notification reaches this process
        # only through the system event loop, and that must own the main
        # thread. The HTTP server moves to a worker; quitting shuts the
        # server down, which in turn stops the loop.
        def bedienen():
            try:
                httpd.serve_forever(poll_interval=POLL)
            finally:
                notify.stop_loop()
        threading.Thread(target=bedienen, daemon=True).start()
        try:
            notify.run_loop()
        finally:
            httpd.shutdown()
            app.shutdown()
            httpd.server_close()
        neustart_ausfuehren()
        return httpd
    try:
        httpd.serve_forever(poll_interval=POLL)
    except KeyboardInterrupt:
        print("\nBeende…")
    finally:
        app.shutdown()
        httpd.server_close()
    neustart_ausfuehren()
    return httpd


def ensure_streams():
    """Without a console (Windows bundle) sys.stdout is None – print() dies.

    Both then land in app.log in the app folder – outside every profile,
    because at this point none is chosen yet; otherwise a failed start of a
    windowless application would be completely mute.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        WURZEL.mkdir(parents=True, exist_ok=True)
        f = open(WURZEL / "app.log", "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    return f


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Self-invocation as a subprogram (see script_argv) – before the
    # argument parser, because the subprograms have their own options.
    if argv and argv[0] == "--run":
        if len(argv) < 2:
            raise SystemExit(f"--run braucht einen Namen: {', '.join(RUNNABLE)}")
        return run_bundled(argv[1], argv[2:])

    ensure_streams()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--no-browser", action="store_true",
                    help="Oberfläche nicht automatisch öffnen.")
    ap.add_argument("--data-dir", metavar="ORDNER",
                    help="Ordner für Exporte, Index, Konfiguration und Token "
                         "(Vorgabe gebündelt: Benutzerdatenordner, als Skript: "
                         "der Projektordner). Wie OFFICE365_DATA_DIR.")
    ap.add_argument("--profile", metavar="NAME",
                    help="Profil – ein eigenes Archiv im App-Ordner. Ohne "
                         "Angabe fragt die App, sobald es mehrere gibt. Wie "
                         "MUNIMENTUM_PROFILE.")
    ap.epilog = ("Als erstes Argument startet --run NAME [Optionen] ein "
                 f"Teilprogramm direkt: {', '.join(RUNNABLE)}. So ruft sich die "
                 "gebündelte Datei selbst auf; von Hand nur zum Nachsehen nötig.")
    a = ap.parse_args(argv)
    browser_offen = False
    if a.data_dir or settings.data_dir_env():
        # One archive, no profiles – the flag and the variable alike.
        set_data_dir(a.data_dir or settings.data_dir_env())
    else:
        # Before anything opens a file: an archive from before 10.0 moves
        # into its profile folder (ensure_streams touched only the app
        # folder's own log).
        layout_umzug()
        profil, browser_offen = profil_waehlen(
            a.profile or os.environ.get("MUNIMENTUM_PROFILE"), a.port, not a.no_browser)
        set_profil(profil)
        altbestand_pinnen()
    HEIM.mkdir(parents=True, exist_ok=True)
    BASE.mkdir(parents=True, exist_ok=True)
    serve(App(), a.port, open_browser=not a.no_browser and not browser_offen)



if __name__ == "__main__":
    # Must remain the first statement.
    #
    # corpus._pmap spreads parsing of the exports across a process pool.
    # Outside Linux, Python does not start a worker process via fork but by
    # invoking itself again – bundled, that means this executable, and with
    # "--multiprocessing-fork pipe_handle=…" instead of its own arguments.
    # Without this line the child would run into the argument parser in
    # main(), die there on an unknown option, and the pool would report
    # nothing but BrokenProcessPool to the caller – with no hint that no
    # file was at fault at all.
    #
    # That hit every corpus in which one source crossed the threshold in
    # corpus – mailbox, chats, calendar or mirror, whichever reached it
    # first.
    #
    # Deliberately spawn.freeze_support() and NOT
    # multiprocessing.freeze_support(): before Python 3.14 the latter first
    # checks sys.platform == "win32" and does nothing at all outside
    # Windows. PyInstaller does replace both names with its own
    # platform-independent version – but then macOS and Linux would depend
    # on a tool setting that hook and keeping it. The spawn route carries
    # itself, on every version and everywhere.
    #
    # freeze_support() recognises this invocation, works as the child and
    # exits afterwards. Started as a script the line does nothing – it only
    # looks whether the first argument is "--multiprocessing-fork".
    multiprocessing.spawn.freeze_support()
    main()
