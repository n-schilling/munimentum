#!/usr/bin/env python3
"""
build.py – writes a synthetic archive: one profile with all eight sources.

    python3 -m testdata.build --profile testdaten      # into profiles/<name>/
    python3 -m testdata.build --dir /tmp/somewhere     # into a folder of choice
    python3 app.py --profile testdaten                 # and look at it

What it produces is a whole profile: `app_config.json`, the export folders
below `data/` with the files the exports would have written, the folder
and list inventories in their `state.db`, and an index built by the real
indexer (lexically, no Ollama needed). The app opens it like any archive –
search, calendar, contacts, file browser, Insights and cases all have
something to show.

Why generated and not committed as files: the content is code here, so it
stays reviewable in a diff, cannot drift from the parsers it feeds, and no
binary lands in the repository. Everything it writes is fictional – see
testdata/people.py for the cast; nothing from a real mailbox, drive or
tenant may ever enter this folder.

The same builder makes the archive the browser tests run against
(tests/ui/), which is why it takes a target folder and returns what it
wrote instead of printing its way through.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import settings  # noqa: E402

from testdata import people  # noqa: E402

# The configuration of the profile: the sources the archive holds are
# ticked, the outward doors shut. Whoever opens this profile should see an
# archive, not a first-start wizard asking for a token.
CONFIG = {
    "update_check": False,
    "ollama_enabled": False,
    "mcp_autostart": False,
    "notifications": "off",
    "outlook_categories": ["mail", "calendar", "contacts"],
    "teams_categories": ["1on1", "group", "meeting", "channels"],
    "teams_files": True,
    "onedrive_enabled": True,
    "sharepoint_enabled": True,
    "sharepoint_pages_enabled": True,
    "planner_enabled": True,
    "todo_enabled": True,
    "onenote_enabled": True,
    "sharepoint_urls": people.SHAREPOINT_SITE,
    "sharepoint_pages_urls": people.SHAREPOINT_SITE,
    "internal_domains": people.DOMAIN,
    "own_name": people.ME[0],
}


def write_config(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / settings.CONFIG_NAME).write_text(
        json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


def _env(home):
    """The environment every subprogram gets: the profile, and a fixed time
    zone. Without the first one they would read the configuration of
    whatever archive this checkout otherwise holds; without the second one
    the timestamps in the archive would differ from machine to machine."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MUNIMENTUM_", "OFFICE365_"))}
    env["MUNIMENTUM_HOME"] = str(home)
    env["TZ"] = "UTC"
    return env


def run_index(home, exports, store):
    """The real indexer over the archive – lexical, so no Ollama is needed."""
    store.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(PROJECT / "rag_index.py"),
            str(exports / settings.TEAMS_DIR),
            str(exports / settings.OUTLOOK_DIR),
            str(exports / settings.ONEDRIVE_DIR),
            "--sharepoint", str(exports / settings.SHAREPOINT_DIR),
            "--pages", str(exports / settings.SHAREPOINT_PAGES_DIR),
            "--planner", str(exports / settings.PLANNER_DIR),
            "--todo", str(exports / settings.TODO_DIR),
            "--onenote", str(exports / settings.ONENOTE_DIR),
            "--store", str(store), "--no-embeddings"]
    subprocess.run(argv, cwd=str(PROJECT), env=_env(home), check=True,
                   capture_output=True, text=True)


def run_calendar(home, exports, store):
    """calendar.json, what the calendar and the address book read."""
    argv = [sys.executable, str(PROJECT / "combined_search.py"),
            str(exports / settings.OUTLOOK_DIR),
            "--json", str(store / "calendar.json"), "--no-reconstruct"]
    subprocess.run(argv, cwd=str(PROJECT), env=_env(home), check=True,
                   capture_output=True, text=True)


def run_step(home, argv):
    """One of the app's subprograms over the profile."""
    subprocess.run([sys.executable, *argv], cwd=str(PROJECT), env=_env(home), check=True,
                   capture_output=True, text=True)


def build(home, *, index=True, clean=True, flat=False):
    """Write the whole profile into `home` and return what was written.

    `flat` writes the layout the app uses when it is started with
    --data-dir: the export folders lie in `home` itself instead of below
    `data/`. That is how the browser tests run it, because a profile can
    only be reached through the app folder, and no test may write there.

    The environment is set before the writers are imported: they reach for
    the export modules, and those read the configuration at import time –
    of this profile, never of the archive the checkout may otherwise hold.
    The fixed time zone is what makes the result the same everywhere, since
    five of the sources take their date from the file system.
    """
    home = Path(home)
    if clean and home.exists():
        shutil.rmtree(home)
    write_config(home)
    before = os.environ.get("MUNIMENTUM_HOME")
    os.environ["MUNIMENTUM_HOME"] = str(home)
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    settings.reset()
    try:
        from testdata import history, sources  # noqa: PLC0415 – after the environment

        exports = home if flat else home / settings.DATEN_UNTERORDNER
        store = home / settings.STORE_DIR
        written = sources.write_all(exports)
        # The past: earlier versions, the evidence chain, a change by hand.
        earlier = history.chain(exports)
        case_id = None
        if index:
            run_index(home, exports, store)
            run_calendar(home, exports, store)
            case_id = history.case(home, exports, store, earlier)
            history.archive_report(home, exports, store, lambda argv: run_step(home, argv))
    finally:
        # The profile is written: whoever called this (a test session) goes
        # on with the home it had.
        if before is None:
            os.environ.pop("MUNIMENTUM_HOME", None)
        else:
            os.environ["MUNIMENTUM_HOME"] = before
        settings.reset()
    return {"home": home, "exports": exports, "store": store, "files": written,
            "earlier": earlier, "case": case_id}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--profile", metavar="NAME",
                       help="write into profiles/NAME below the app folder "
                            "(running from source: this project folder)")
    where.add_argument("--dir", metavar="FOLDER",
                       help="write into this folder instead, in the flat "
                            "layout an app started with --data-dir expects")
    ap.add_argument("--no-index", action="store_true",
                    help="write the files only, build no index")
    ap.add_argument("--flat", action="store_true",
                    help="the layout of a start with --data-dir: the export "
                         "folders in the target folder itself")
    ap.add_argument("--keep", action="store_true",
                    help="write beside what is already there instead of "
                         "emptying the folder first")
    a = ap.parse_args(argv)
    home = (settings.profil_ordner(a.profile, settings.app_wurzel())
            if a.profile else Path(a.dir).expanduser().resolve())
    out = build(home, index=not a.no_index, clean=not a.keep,
                flat=a.flat or bool(a.dir))
    print(f"{out['files']} files in {out['home']}")
    if a.profile:
        print(f"look at it: python3 app.py --profile {a.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
