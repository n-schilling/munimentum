#!/usr/bin/env python3
"""
run_history.py – persistent history of app-driven runs (SQLite).

One row per run the JobRunner executes, one row per step within it. The data
answers the questions a maintainer asks later: when did it run, was it manual
or scheduled, which elements were enabled, how long did each step take, what
did it produce – and which app version did it. Nothing personal is stored:
only counts, durations and switches.

The file lives in the data directory (runs.db), next to the exports – it is
history, not derivable state, so it does not belong into the rebuildable
rag_store. Retention is driven by the central configuration
(runs_retention_months) and enforced on start and after every run.

Every write is wrapped: history must never break a run. A failed insert
degrades to "this run is missing from the history", nothing more.
"""

import json
import sqlite3
import time
from pathlib import Path

import version

DB_NAME = "runs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id          INTEGER PRIMARY KEY,
    started_at  REAL NOT NULL,          -- Unix epoch
    finished_at REAL,
    job_type    TEXT NOT NULL,          -- the job label key, e.g. job.export
    origin      TEXT NOT NULL,          -- manual | schedule
    result      TEXT,                   -- done | error | aborted | token_expired
    elements    TEXT,                   -- JSON: what was enabled for this run
    semantic    INTEGER,                -- indexing with embeddings (Ollama)?
    workers     INTEGER,
    app_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_runs_started ON runs(started_at);
CREATE TABLE IF NOT EXISTS steps(
    run_id     INTEGER NOT NULL,
    key        TEXT NOT NULL,           -- outlook | teams | index | calendar | …
    label      TEXT NOT NULL,           -- i18n key, e.g. job.step.outlook
    started_at REAL NOT NULL,
    duration_s REAL,
    new_items  INTEGER,
    unchanged  INTEGER,
    excluded   INTEGER,
    errors     INTEGER,
    skipped    INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER,
    detail     TEXT                     -- JSON: the step's extra counts
);
CREATE INDEX IF NOT EXISTS ix_steps_run ON steps(run_id);
"""

# A month is bookkeeping here, not astronomy – the average length is fine.
_MONTH_S = 30.44 * 86400


class RunHistory:
    """Run history on one small SQLite file. Writes never raise."""

    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=5)
        con.executescript(_SCHEMA)
        return con

    def _schreibe(self, sql, params):
        """Execute one write; returns the rowid or None on any failure."""
        try:
            con = self._connect()
            try:
                cur = con.execute(sql, params)
                con.commit()
                return cur.lastrowid
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            return None

    # -- recording ---------------------------------------------------------
    def start_run(self, job_type, origin, elements=None, semantic=None,
                  workers=None):
        return self._schreibe(
            "INSERT INTO runs(started_at, job_type, origin, elements, semantic,"
            " workers, app_version) VALUES(?,?,?,?,?,?,?)",
            (time.time(), str(job_type), str(origin),
             json.dumps(elements, ensure_ascii=False) if elements else None,
             None if semantic is None else int(bool(semantic)),
             workers, version.VERSION))

    def record_step(self, run_id, key, label, started_at, duration_s=None,
                    result=None, skipped=False, ok=None):
        """One step. `result` is the parsed @@RESULT@@ dict, if the step sent one."""
        if run_id is None:
            return
        result = result or {}
        extra = result.get("extra")
        self._schreibe(
            "INSERT INTO steps(run_id, key, label, started_at, duration_s,"
            " new_items, unchanged, excluded, errors, skipped, ok, detail)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, str(key), str(label), started_at, duration_s,
             result.get("new"), result.get("unchanged"),
             result.get("excluded"), result.get("errors"),
             int(bool(skipped)), None if ok is None else int(bool(ok)),
             json.dumps(extra, ensure_ascii=False) if extra else None))

    def finish_run(self, run_id, result):
        if run_id is None:
            return
        self._schreibe("UPDATE runs SET finished_at = ?, result = ? WHERE id = ?",
                       (time.time(), str(result), run_id))

    # -- housekeeping ------------------------------------------------------
    def prune(self, months):
        """Drop runs older than the retention window, steps included."""
        try:
            grenze = time.time() - max(1, int(months)) * _MONTH_S
        except (TypeError, ValueError):
            return
        self._schreibe("DELETE FROM steps WHERE run_id IN"
                       " (SELECT id FROM runs WHERE started_at < ?)", (grenze,))
        self._schreibe("DELETE FROM runs WHERE started_at < ?", (grenze,))

    # -- reading -----------------------------------------------------------
    def list_runs(self, limit=50):
        """Newest first, each run with its steps in execution order."""
        try:
            con = self._connect()
            try:
                runs = [dict(zip(("id", "started_at", "finished_at", "job_type",
                                  "origin", "result", "elements", "semantic",
                                  "workers", "app_version"), row, strict=True))
                        for row in con.execute(
                            "SELECT id, started_at, finished_at, job_type,"
                            " origin, result, elements, semantic, workers,"
                            " app_version FROM runs"
                            " ORDER BY started_at DESC, id DESC LIMIT ?",
                            (max(1, min(int(limit), 200)),))]
                for lauf in runs:
                    lauf["elements"] = (json.loads(lauf["elements"])
                                        if lauf["elements"] else None)
                    lauf["steps"] = [
                        dict(zip(("key", "label", "started_at", "duration_s",
                                  "new", "unchanged", "excluded", "errors",
                                  "skipped", "ok", "extra"), row, strict=True))
                        for row in con.execute(
                            "SELECT key, label, started_at, duration_s,"
                            " new_items, unchanged, excluded, errors, skipped,"
                            " ok, detail FROM steps WHERE run_id = ?"
                            " ORDER BY rowid", (lauf["id"],))]
                    for schritt in lauf["steps"]:
                        schritt["extra"] = (json.loads(schritt["extra"])
                                            if schritt["extra"] else None)
                return runs
            finally:
                con.close()
        except (sqlite3.Error, OSError, ValueError):
            return []
