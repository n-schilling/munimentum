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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import answer
import auth
import export_util
import folders
import i18n
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
            "mcp_server",
            # auth is not an export step but a self-report: which sign-in
            # path applies, is a key present, is there a cache. In the
            # bundle this is the only way to check that without network –
            # the smoke test does exactly that.
            "auth", "onedrive_export", "sharepoint_export",
            "planner_export", "todo_export", "onenote_export")


def resource_dir():
    """Directory of the shipped scripts (in the bundle: the unpacked archive)."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


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
    business loading 200 KB of markup. The /-route fills its two
    placeholders, /*__I18N__*/ (language strings) and /*__STEPS__*/ (the
    step metadata of the registry), on every request."""
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
    show – on demand (GET /api/profiles), never in the status poll: it
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
            # instead of letting the user run into an error.
            "features": sorted({r[1] for r in con.execute("PRAGMA table_info(chunks)")}
                               & {"thread", "gone", "ext"}),
        }
    finally:
        con.close()
    _ZAEHLUNG[str(db)] = (kennung, zahlen)
    return zahlen


def lies_bericht(ordner=OUTLOOK_DIR):
    """The last completeness report, if there is one.

    It is only created at the push of a button: the check queries
    Microsoft, and nothing should do that unasked just because a view
    opens.
    """
    return state_db.StateDb(BASE / ordner).bericht_lesen()


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
    out.update({
        "vollstaendigkeit": lies_bericht(),
        "vollstaendigkeit_onedrive": lies_bericht(ONEDRIVE_DIR),
        "vollstaendigkeit_sharepoint":
            state_db.StateDb(BASE / SHAREPOINT_DIR).bericht_lesen(),
        "vollstaendigkeit_pages":
            state_db.StateDb(BASE / SHAREPOINT_PAGES_DIR).bericht_lesen(),
    })
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


def systemangaben(status, lang=None):
    """The facts of a report as [{"k": text key, "v": value}, …].

    Translation happens only in the interface – as with the log lines.
    That way the reporter reads the report in their language rather than
    in that of a server, which has none.
    """
    store = status.get("store") or {}
    oll = status.get("ollama") or {}
    cfg = status.get("config") or {}
    letzter = (status.get("jobs") or {}).get("last") or {}

    def zeile(k, v):
        # Only the stem of the key; the interface prefixes "report.sys." and
        # translates – as with the log lines, nothing that has a language is
        # named here.
        return {"k": k, "v": str(v)}

    art = "Bündel" if status.get("frozen") else "Skript"
    kerne = os.cpu_count() or "?"
    kats = sorted((cfg.get("outlook_categories") or [])
                  + (cfg.get("teams_categories") or []))
    kats += [name for flag, _kategorie, name in SPIEGEL_QUELLEN
             if cfg.get(flag)]
    angaben = [
        zeile("version", f"{version.VERSION} ({art})"),
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
    if status.get("data_dir") != status.get("data_dir_default"):
        angaben.append(zeile("datadir", anonymisiere(str(status.get("data_dir")))))
    return angaben


def fehlerbericht(status, log_text="", hint="", lang=None):
    """Everything the interface needs for the GitHub form."""
    titel = anonymisiere(str(hint or "").strip()).strip()
    return {
        "system": systemangaben(status, lang),
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


def build_steps(cfg, angefragt, *, embeddings=True, token="",
                reconstruct=None, nur_einheit=None, legacy_comments=False,
                sync_now=False, calendar_full=False):
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
        # the status and /api/calendar read it. Spelled relative it landed
        # under the subprocess cwd (BASE) instead of the index folder.
        "calendar_json": str(calendar_file(cfg)),
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
        store = store_status(self.cfg)
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
        return {
            "token": tok,
            "ollama": oll,
            "ollama_hint": ollama_hint(),
            "store": store,
            "calendar": {"exists": calendar_file(self.cfg).exists(),
                         "built_at": _mtime_iso(calendar_file(self.cfg))},
            "exports": export_status(self.cfg),
            "jobs": jobs,
            "mcp": self.mcp.status(self.cfg),
            "profile": profil_zustand(),
            "config": self.cfg,
            "schedule_next": (datetime.fromtimestamp(nxt).isoformat(timespec="seconds")
                              if nxt else None),
            "schedule_enabled": bool(plan.get("enabled")),
            "wizard": wizard,
            "data_dir": str(BASE),
            "home_dir": str(HEIM),
            "app_location": (sys.executable if FROZEN
                             else str(Path(__file__).resolve().parent)),
            "data_dir_default": str(HEIM / DATEN_UNTERORDNER),
            "index_dir": str(STORE_PFAD),
            "index_dir_default": str(HEIM / STORE_DIR),
            "frozen": FROZEN,
            "update": dict(self._update, releases_url=version.RELEASES_URL),
            "skip_folders_default": sorted(SKIP_FOLDERS_DEFAULT),
            "filetype_hidden_default": sorted(FILETYPE_HIDDEN_DEFAULT),
            "graph_explorer": GRAPH_EXPLORER,
            "scopes_needed": sorted({SCOPE_FOR[c] for c in self.selected_categories()
                                     if c in SCOPE_FOR} | {"User.Read"}),
            "scope_queries": SCOPE_QUERY,
            "auth": self.auth_status(),
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

    def launch(self, anfrage, *, embeddings=None, label="Lauf",
               reconstruct=None, nur_einheit=None, legacy_comments=False,
               sync_now=False, calendar_full=False, origin="manual"):
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
                            sync_now=sync_now, calendar_full=calendar_full)
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
class Handler(BaseHTTPRequestHandler):
    server_version = "munimentum-app"
    protocol_version = "HTTP/1.1"
    app = None                 # set by serve()
    allowed_hosts = ()

    def log_message(self, fmt, *args):
        pass                    # no access log on stdout

    # -- Helpers -----------------------------------------------------------
    def _host_ok(self):
        """Accept only our own loopback address.

        Without this check any website could talk to the server via a DNS
        name pointing at 127.0.0.1 (rebinding) – and the server hands out
        the complete mail and chat archive.
        """
        host = (self.headers.get("Host") or "").lower()
        return host in self.allowed_hosts

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 4 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- Routes ------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "Nur über http://127.0.0.1 erreichbar.",
                              "text/plain; charset=utf-8")
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        one = {k: v[0] for k, v in q.items()}
        app = self.app
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, self._page(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self._json(app.status())
            if u.path == "/api/profiles":
                return self._json(profil_status())
            if u.path == "/api/log":
                lines, seq = app.jobs.log_since(int(one.get("since", 0) or 0))
                return self._json({"lines": lines, "seq": seq})
            if u.path == "/api/search":
                return self._json(self._search(one))
            if u.path == "/api/similar":
                return self._json(self._similar(one))
            if u.path == "/api/thread":
                return self._json(self._thread(one))
            if u.path == "/api/sharepoint-report":
                # The preview/type views need only this one small file –
                # not the full analytics aggregation behind /api/analytics.
                return self._json(
                    {"bericht": state_db.StateDb(
                        BASE / SHAREPOINT_DIR).bericht_lesen()})
            if u.path == "/api/files":
                return self._json(self._files(one))
            if u.path == "/api/filetypes":
                return self._json(self._filetypes(one))
            if u.path == "/api/folders":
                return self._json(self._folders(one))
            if u.path == "/api/people":
                return self._json(self._people(one))
            if u.path == "/api/document":
                return self._json(self._document(one))
            if u.path == "/api/run-log":
                try:
                    kennung = int(one.get("id") or 0)
                except ValueError:
                    kennung = 0
                return self._json({"lines": app.history.run_log(kennung)})
            if u.path == "/api/analytics":
                return self._json(analytics_daten(app.cfg))
            if u.path == "/api/runs":
                try:
                    grenze = int(one.get("limit", 50))
                except ValueError:
                    grenze = 50
                return self._json({"runs": app.history.list_runs(grenze)})
            if u.path == "/api/calendar":
                return self._calendar()
            if u.path == "/api/openapi":
                # The expert-mode contract: the full HTTP API as OpenAPI 3.1,
                # shipped as a file so spec and code are reviewed together.
                text = (RES / "openapi.yaml").read_text(encoding="utf-8")
                return self._send(200, text, "text/yaml; charset=utf-8")
            if u.path == "/source":
                return self._source(one)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send(404, json.dumps({"error": "Unbekannter Pfad"}))

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "Nur über http://127.0.0.1 erreichbar.",
                              "text/plain; charset=utf-8")
        u = urlsplit(self.path)
        app = self.app
        data = self._body()
        try:
            if u.path == "/api/token":
                return self._json(self._save_token(data))
            if u.path == "/api/analytics-refresh":
                return self._json(analytics_daten(app.cfg, neu=True))
            if u.path == "/api/wizard-seen":
                # "Later": reset the note about a token that died mid-run,
                # otherwise the wizard would reopen on every status poll.
                app.jobs.token_expired = False
                return self._json({"ok": True})
            if u.path == "/api/run":
                mit_outlook = bool(data.get("outlook"))
                kalender = bool(data.get("calendar"))
                rekonstruktion = None            # None: as configured
                if kalender and mit_outlook:
                    # Part of an export run: the step follows what is
                    # actually fetched. The "build calendar & contacts"
                    # button comes without outlook and stays untouched.
                    kalender, mit_mails = calendar_plan(app.cfg)
                    if not mit_mails:
                        rekonstruktion = False
                anfrage = steps_mod.anfrage_aus_request(data)
                anfrage.update(outlook=mit_outlook, calendar=kalender)
                ok, why = app.launch(
                    anfrage,
                    nur_einheit=(str(data.get("nur_einheit") or "").strip()
                                 or None),
                    embeddings=data.get("embeddings"),
                    label=str(data.get("label") or "job.export"),
                    reconstruct=rekonstruktion,
                    legacy_comments=bool(data.get("legacy_comments")),
                    sync_now=bool(data.get("sync_now")),
                    calendar_full=bool(data.get("calendar_full")))
                return self._json({"ok": ok, "message": why}, 200 if ok else 409)
            if u.path == "/api/login":
                ok, daten = app.login_starten()
                return self._json({"ok": ok, "device": daten}, 200 if ok else 500)
            if u.path == "/api/data-dir":
                # Both paths are keys in app_config.json – which sits fixed
                # in the home folder, so there is no chicken-and-egg. Empty
                # means default (subfolder of the home folder). NOTHING is
                # moved: relocating folders is the user's business.
                felder = (("path", "data_dir", HEIM / DATEN_UNTERORDNER, BASE),
                          ("index", "index_dir", HEIM / STORE_DIR, STORE_PFAD))
                antwort, neustart, neu = {}, False, {}
                for feld, key, vorgabe, aktuell in felder:
                    if feld not in data:
                        continue
                    roh = str(data.get(feld) or "").strip()
                    if roh:
                        ziel, fehler = pruefe_datenordner(roh)
                        if fehler:
                            # Into the log as well: the settings save posts
                            # this along with everything else, and a
                            # refusal must not hide in one small field.
                            app.jobs.log(fehler, "err")
                            return self._json({"ok": False,
                                               "message": fehler}, 400)
                    else:
                        ziel = vorgabe
                    neu[key] = "" if ziel == vorgabe else str(ziel)
                    antwort[feld] = str(ziel)
                    neustart = neustart or str(ziel) != str(aktuell)
                # Two profiles must never share an export or index folder:
                # their archives would run into each other.
                anderes = profil_geteilt(
                    PROFIL, antwort.get("path", str(BASE)),
                    antwort.get("index", str(STORE_PFAD)))
                if anderes:
                    fehler = {"k": "srv.datadir.shared", "v": {"profile": anderes}}
                    app.jobs.log(fehler, "err")
                    return self._json({"ok": False, "message": fehler}, 400)
                vorher = {key: app.cfg.get(key) or "" for key in neu}
                app.konfiguriere(lambda cfg: cfg.update(neu))
                # One line per path that really changed, each naming its
                # own folder – an index change used to be logged as the
                # data folder, which read as if the wrong one had been saved.
                for feld, key, _vorgabe, _aktuell in felder:
                    if key in neu and neu[key] != vorher[key]:
                        app.jobs.logk("srv.datadir.set" if key == "data_dir"
                                      else "srv.indexdir.set", "warn",
                                      path=antwort[feld])
                # BASE is fixed since startup and goes to every subprocess
                # as its working directory. Repointing it mid-operation –
                # possibly while an export runs – would be grossly negligent.
                return self._json({"ok": True,
                                   "path": antwort.get("path", str(BASE)),
                                   "index": antwort.get("index",
                                                        str(STORE_PFAD)),
                                   "restart": neustart})
            if u.path == "/api/folder-plan":
                return self._json(self._ordnerplan(data))
            if u.path == "/api/profiles":
                info, fehler = profil_anlegen(data.get("name"))
                if fehler:
                    return self._json({"ok": False, "message": fehler}, 400)
                app.jobs.logk("srv.profile.created", "info", name=info["name"])
                return self._json({"ok": True, "profile": info,
                                   "profiles": profil_status()})
            if u.path == "/api/profile-switch":
                name = str(data.get("name") or "").strip().lower()
                if not profile_moeglich():
                    return self._json({"ok": False, "message": {
                        "k": "srv.profile.impossible", "v": {}}}, 400)
                if name not in profil_namen():
                    return self._json({"ok": False, "message": {
                        "k": "srv.profile.unknown", "v": {"name": name}}}, 400)
                port = self.server.server_address[1]
                if name == PROFIL:
                    return self._json({"ok": True, "name": name, "url": "/"})
                lauft = eigene_instanz(port, profil=name)
                if lauft:
                    # Already open next door: that instance, not a second one.
                    return self._json({"ok": True, "name": name,
                                       "url": f"http://127.0.0.1:{lauft}/"})
                if app.jobs.busy:
                    return self._json({"ok": False, "message": {"k": "srv.busy", "v": {}}}, 409)
                profil_register_schreiben(zuletzt=name)
                # The answer first, then the server stops (serve() restarts).
                self._json({"ok": True, "name": name})
                neustart_mit_profil(self.server, name, port)
                return None
            if u.path == "/api/profile-rename":
                if app.jobs.busy:
                    return self._json({"ok": False, "message": {"k": "srv.busy", "v": {}}}, 409)
                alt = str(data.get("name") or "").strip().lower()
                neu, fehler = profil_umbenennen(alt, data.get("neu"),
                                                port=self.server.server_address[1])
                if fehler:
                    return self._json({"ok": False, "message": fehler}, 400)
                app.jobs.logk("srv.profile.renamed", "info", old=alt, name=neu)
                return self._json({"ok": True, "name": neu, "profiles": profil_status()})
            if u.path == "/api/profile-prefs":
                reg = profil_register_schreiben(
                    ohne_nachfrage=bool(data.get("ohne_nachfrage")))
                return self._json({"ok": True,
                                   "ohne_nachfrage": bool(reg.get("ohne_nachfrage"))})
            if u.path == "/api/logout":
                return self._json({"ok": app.abmelden()})
            if u.path == "/api/cancel":
                return self._json({"ok": app.jobs.cancel()})
            if u.path == "/api/config":
                return self._json(self._save_config(data))
            if u.path == "/api/schedule":
                return self._json(self._save_schedule(data))
            if u.path == "/api/mcp":
                return self._json(self._mcp(data))
            if u.path == "/api/report":
                # The log comes from the interface, not from the buffer
                # here: there it is already translated (the messages are
                # text keys, see Jobs.logk).
                return self._json(fehlerbericht(
                    app.status(), str(data.get("log") or ""),
                    str(data.get("hint") or ""),
                    i18n.negotiate(app.cfg.get("language"),
                                   self.headers.get("Accept-Language"), RES)))
            if u.path == "/api/ollama-recheck":
                return self._json(app.ollama(force=True))
            if u.path == "/api/answer":
                return self._answer(data)
            if u.path == "/api/update-check":
                return self._json(app.check_updates(blockierend=True))
            if u.path == "/api/quit":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self._json({"ok": True})
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self._send(404, json.dumps({"error": "Unbekannter Pfad"}))

    # -- Route implementations --------------------------------------------
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
        return (seite().replace("/*__I18N__*/", nutzlast)
                    .replace("/*__STEPS__*/", schritte))

    def _save_token(self, data):
        token = normalize_token(data.get("token"))
        if not token:
            return {"ok": False, "message": {"k": "srv.token.empty", "v": {}}}
        if len(token) < 40:
            return {"ok": False, "message": {"k": "srv.token.short", "v": {}}}
        write_token(token)
        self.app.jobs.token_expired = False
        st = token_status(token, needed=self.app.selected_categories())
        if st["expired"]:
            return {"ok": False, "token": st,
                    "message": {"k": "srv.token.stale", "v": {}}}
        msg = ({"k": "srv.token.saved.scopes", "v": {"list": ", ".join(st["missing"])}}
               if st["missing"] else {"k": "srv.token.saved", "v": {}})
        self.app.jobs.log(msg, "warn" if st["missing"] else "ok")
        return {"ok": True, "message": msg, "token": st}

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
                        "teams_channel_files"):
                if key in data:
                    cfg[key] = bool(data[key])
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
        return {"ok": True, "config": self.app.cfg}

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
        return {"ok": True, "schedule": plan,
                "next": self.app.scheduler.next_due()}

    def _mcp(self, data):
        action = str(data.get("action") or "").lower()
        if action == "start":
            ok, why = self.app.mcp.start(self.app.cfg)
            return {"ok": ok, "message": why, "mcp": self.app.mcp.status(self.app.cfg)}
        if action == "stop":
            self.app.mcp.stop()
            return {"ok": True, "mcp": self.app.mcp.status(self.app.cfg)}
        return {"ok": False, "message": {"k": "srv.mcp.badaction", "v": {}}}

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
            return self._json({"error": self.app.search.error}, 503)
        oll = self.app.ollama()
        if not (oll["running"] and oll["has_chat_model"]):
            return self._json({"error": {"k": "srv.answer.nomodel",
                                         "v": {"model": self.app.cfg["chat_model"]}}}, 503)

        query = str(data.get("q") or "").strip()
        if not query:
            return self._json({"error": {"k": "srv.answer.noquery", "v": {}}}, 400)
        k = max(1, min(int(self.app.cfg.get("answer_sources", 8)), 20))
        res = mod.search_messages(
            query=query, person=str(data.get("person") or ""),
            date_from=str(data.get("from") or ""), date_to=str(data.get("to") or ""),
            source=str(data.get("source") or "all"), k=k, preview_chars=0)
        treffer = res.get("results") or []
        if not treffer:
            return self._json({"error": {"k": "srv.answer.nohits", "v": {}}}, 200)

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

    def _search(self, q):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "hits": [], "count": 0}
        kw = dict(person=q.get("person", ""), date_from=q.get("from", ""),
                  date_to=q.get("to", ""), source=q.get("source", "all"),
                  k=min(int(q.get("k", 20) or 20), 100),
                  offset=max(int(q.get("offset", 0) or 0), 0),
                  only_gone=str(q.get("gone", "")).lower() in ("1", "true", "ja"),
                  folder=str(q.get("folder", "") or ""),
                  filetype=str(q.get("filetype", "") or ""))
        query = (q.get("q") or "").strip()
        if query:
            res = mod.search_messages(query=query, mode=q.get("mode", "auto"), **kw)
        else:
            res = mod.browse_messages(**kw)
        res["semantic"] = bool(mod.STATE.get("semantic"))
        return res

    def _similar(self, q):
        """Similar to one hit – needs no Ollama (see mcp_server)."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "results": [], "count": 0}
        try:
            cid = int(q.get("cid", 0))
        except (TypeError, ValueError):
            return {"error": {"k": "srv.badindex", "v": {"error": "cid"}},
                    "results": [], "count": 0}
        return mod.similar_messages(cid=cid,
                                    k=min(int(q.get("k", 20) or 20), 100))

    def _thread(self, q):
        """All messages of one conversation – the same evaluation as in MCP."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "messages": [], "count": 0}
        return mod.get_thread(thread=q.get("key", ""),
                              limit=min(int(q.get("limit", 50) or 50), 200))

    def _folders(self, q):
        """Which mailbox folders are in the archive – for the filter."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "folders": []}
        return mod.list_folders(contains=q.get("contains", ""),
                                limit=min(int(q.get("limit", 300) or 300), 1000),
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
            return {"ok": False, "leer": True}
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
        return {"ok": True, "regeln": folders.schreibe_regeln(regeln),
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
            return {"ok": False, "leer": True}
        regeln = notizbuchregeln(self.app.cfg, roh)
        wurzeln = {e["pfad"].split("/")[0] for e in daten.get("ordner", [])}
        if wurzel.is_dir():
            wurzeln |= {p.name for p in wurzel.iterdir() if p.is_dir()}
        je_buch = {}
        for pfad, n in folders.auf_platte(wurzel, sorted(wurzeln), ".html").items():
            buch = pfad.split("/")[0]
            je_buch[buch] = je_buch.get(buch, 0) + n
        return {"ok": True, "regeln": folders.schreibe_regeln(regeln),
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
            return {"ok": False, "leer": True}
        regeln = teamsregeln(self.app.cfg, roh)
        archiv = {}
        if wurzel.is_dir():
            for datei in wurzel.glob("*/*.html"):
                archiv[f"{datei.parent.name}/{_ohne_kennung(datei.stem)}"] = \
                    archiv.get(f"{datei.parent.name}/{_ohne_kennung(datei.stem)}", 0) + 1
            for datei in wurzel.glob("channels/*/*.html"):
                pfad = f"channels/{datei.parent.name}/{_ohne_kennung(datei.stem)}"
                archiv[pfad] = archiv.get(pfad, 0) + 1
        return {"ok": True, "regeln": folders.schreibe_regeln(regeln),
                **folders.plan(wurzel, regeln, daten, ".html",
                               folders.DATEI, archiv=archiv)}

    def _todo_plan(self, roh):
        """The export list for To Do: the stored list of lists against the
        rules from the form; a list's folder carries the id as suffix, its
        tasks are counted from the folder's state."""
        wurzel = BASE / TODO_DIR
        daten = folders.lade(wurzel, folders.DATEI)
        if not daten:
            return {"ok": False, "leer": True}
        regeln = todoregeln(self.app.cfg, roh)
        archiv = {}
        if wurzel.is_dir():
            for ordner in wurzel.iterdir():
                if not ordner.is_dir() or ordner.name.startswith("."):
                    continue
                try:
                    n = len(json.loads(
                        state_db.StateDb(ordner).kv_lesen("tasks") or "{}"))
                except ValueError:
                    n = 0
                pfad = _ohne_kennung(ordner.name)
                archiv[pfad] = archiv.get(pfad, 0) + n
        return {"ok": True, "regeln": folders.schreibe_regeln(regeln),
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
                db = state_db.StateDb(lib)
                d = db.baum_lesen()
                if not d:
                    continue
                praefix = lib.relative_to(wurzel).as_posix()
                stand = max(stand or "", d.get("abgeglichen") or "") or None
                try:
                    urls = json.loads(db.kv_lesen("urls") or "[]")
                except ValueError:
                    urls = []
                for e in d.get("ordner", []):
                    eintraege.append({**e, "pfad": f"{praefix}/{e['pfad']}",
                                      **({"urls": urls} if urls else {})})
        if not eintraege:
            return {"ok": False, "leer": True}
        daten = {"ordner": eintraege, "abgeglichen": stand}
        regeln = sharepointregeln(self.app.cfg, roh)
        plan = folders.plan(wurzel, regeln, daten, None)
        # The walk under the site roots also sees each library's bookkeeping
        # (state.db and other leftovers) – real content lives below Dateien/.
        plan["weg"] = [z for z in plan["weg"]           # drive_mirror.DATEI_DIR
                       if "/Dateien/" in z["pfad"] + "/"]
        plan["mails_weg"] = sum(z["archiv"] for z in plan["weg"])
        return {"ok": True, "regeln": folders.schreibe_regeln(regeln), **plan}

    def _files(self, q):
        """One level of the mirrored file tree, sizes taken from disk."""
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "roots": []}
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
            return {"error": self.app.search.error, "filetypes": []}
        # Hiding happens here and not in the tool: list_filetypes is meant
        # to say what is in the archive – to Claude as well. The trimming is
        # a question of the interface, not of the corpus. Hence fetch
        # everything first, then hide, then cut to the requested number.
        wieviele = min(int(q.get("limit", 40) or 40), 200)
        aus = set(self.app.cfg.get("filetype_hidden") or [])
        r = mod.list_filetypes(limit=200, source=q.get("source", ""))
        liste = [e for e in r.get("filetypes", []) if e["type"] not in aus]
        return {"count": min(len(liste), wieviele),
                "total_distinct": r.get("total_distinct", len(liste)),
                "hidden": sorted(aus),
                "filetypes": liste[:wieviele]}

    def _people(self, q):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error, "people": []}
        return mod.list_people(source=q.get("source", "all"),
                               contains=q.get("contains", ""),
                               limit=min(int(q.get("limit", 50) or 50), 200))

    def _document(self, q):
        mod = self.app.search.ensure(self.app.cfg)
        if mod is None:
            return {"error": self.app.search.error}
        return mod.get_document(uid=q.get("uid", ""),
                                context_before=int(q.get("before", 0) or 0),
                                context_after=int(q.get("after", 0) or 0))

    def _calendar(self):
        """Serve calendars, reconstructed appointments and contacts in one go.

        Compressed when the browser offers it: ~5 MB of JSON become
        ~0.75 MB. The evaluation itself runs as its own step (it reads
        every mail); here only its result file is passed through.
        """
        roh, gz = self.app.calendar_payload()
        if roh is None:
            return self._json({"error": {"k": "cal.missing", "v": {}},
                               "recs": []}, 404)
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
            return self._send(503, "no index loaded", "text/plain; charset=utf-8")
        target, err = mod._resolve_source(q.get("root", ""), q.get("path", ""))
        if err:
            return self._send(404, err, "text/plain; charset=utf-8")
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
    """Relative href/src values of an archive HTML onto the /source route.

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
        neu = (f"/source?root={quote(wurzel, safe='')}"
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
        with urllib.request.urlopen(f"http://{host}:{port}/api/status",
                                    timeout=timeout) as r:
            daten = json.loads(r.read().decode("utf-8"))
    except Exception:
        return False
    # Something entirely different could be listening on the port; only
    # our own answer counts as "already running" – and only with the same
    # profile: another profile's instance keeps its port, this one takes
    # the next free one.
    if not (isinstance(daten, dict) and "data_dir" in daten and "token" in daten):
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
    ZU = {"Connection": "close"}

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), extra=self.ZU)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "Nur über http://127.0.0.1 erreichbar.",
                              "text/plain; charset=utf-8", extra=self.ZU)
        u = urlsplit(self.path)
        if u.path == "/api/profiles":
            return self._json(profil_status())
        if u.path == "/api/status":
            # Not the app yet: the page polls this until the app answers.
            return self._json({"chooser": True})
        code = i18n.negotiate(None, self.headers.get("Accept-Language"), RES)
        return self._send(200, profil_seite(code), "text/html; charset=utf-8",
                          extra=self.ZU)

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "Nur über http://127.0.0.1 erreichbar.",
                              "text/plain; charset=utf-8", extra=self.ZU)
        u = urlsplit(self.path)
        data = self._body()
        if not isinstance(data, dict):
            data = {}
        if u.path == "/api/profile-open":
            name = str(data.get("name") or "").strip().lower()
            if name not in profil_namen():
                return self._json({"ok": False, "message": {
                    "k": "srv.profile.unknown", "v": {"name": name}}}, 400)
            profil_register_schreiben(zuletzt=name,
                                      ohne_nachfrage=bool(data.get("ohne_nachfrage")))
            antwort = {"ok": True}
            lauft = eigene_instanz(self.server.server_address[1], profil=name)
            if lauft:
                # Already open in another instance: the page goes there,
                # and this start ends once the chooser has handed over.
                antwort["url"] = f"http://127.0.0.1:{lauft}/"
            self.server.gewaehlt["name"] = name
            self._json(antwort)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        return self._json({"error": "Unbekannter Pfad"}, 404)


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
