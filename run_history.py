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
CREATE TABLE IF NOT EXISTS log(
    run_id INTEGER,                     -- NULL: app line outside any run
    ts     REAL NOT NULL,
    level  TEXT NOT NULL,
    text   TEXT NOT NULL                -- JSON: raw line, or {k, v} to translate
);
CREATE INDEX IF NOT EXISTS ix_log_run ON log(run_id);
CREATE INDEX IF NOT EXISTS ix_log_ts ON log(ts);
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

    def __init__(self, path, readonly=False):
        """`readonly` opens what is there and creates nothing – the MCP
        server's way in, which must never write the app's file. Reads on
        a file that is not there answer empty, like every other read."""
        self.path = Path(path)
        self.readonly = readonly

    def _connect(self):
        if self.readonly:
            return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
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

    def log_lines(self, zeilen):
        """A batch of log lines: [(run_id, ts, level, text_json)].

        Batched by the JobRunner – a big export emits thousands of lines,
        and one commit per line would fsync its way through the run."""
        if not zeilen:
            return
        try:
            con = self._connect()
            try:
                con.executemany(
                    "INSERT INTO log(run_id, ts, level, text) VALUES(?,?,?,?)",
                    zeilen)
                con.commit()
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            pass

    def has_run(self, run_id):
        """Whether a run with this id was ever recorded – what a route needs
        before it can say "no lines left" rather than "no such run"."""
        try:
            con = self._connect()
            try:
                return con.execute("SELECT 1 FROM runs WHERE id = ?",
                                   (int(run_id),)).fetchone() is not None
            finally:
                con.close()
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return False

    def run_log(self, run_id, limit=4000):
        """The stored log of one run, oldest first."""
        try:
            con = self._connect()
            try:
                zeilen = []
                for ts, level, text in con.execute(
                        "SELECT ts, level, text FROM log WHERE run_id = ?"
                        " ORDER BY rowid LIMIT ?",
                        (int(run_id), max(1, min(int(limit), 10000)))):
                    try:
                        text = json.loads(text)
                    except ValueError:
                        pass               # a raw line stays a raw line
                    zeilen.append({"ts": ts, "level": level, "text": text})
                return zeilen
            finally:
                con.close()
        except (sqlite3.Error, OSError, TypeError):
            return []

    def prune_log(self, days):
        """Drop log lines older than the log's own retention window – it is
        configured separately from the run rows: the counts stay light, the
        lines are the heavy part."""
        try:
            grenze = time.time() - max(1, int(days)) * 86400
        except (TypeError, ValueError):
            return
        self._schreibe("DELETE FROM log WHERE ts < ?", (grenze,))

    def finish_run(self, run_id, result):
        if run_id is None:
            return
        self._schreibe("UPDATE runs SET finished_at = ?, result = ? WHERE id = ?",
                       (time.time(), str(result), run_id))

    def last_step_started(self, key):
        """When the step last RAN, successful or not.

        The archive's age asks this, not last_step_ok(): an export that died
        part-way still wrote what it had fetched until then, so a follow-up
        step from before it is stale even though the row says ok = 0.
        """
        try:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT MAX(started_at) FROM steps WHERE key = ?",
                    (str(key),)).fetchone()
                return row[0] if row and row[0] is not None else None
            except sqlite3.Error:
                return None
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            return None

    def last_step_ok(self, key):
        """When the step last finished successfully – the cadence gate's
        question. None when it never did (or history is unreadable)."""
        try:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT MAX(started_at) FROM steps WHERE key = ? AND ok = 1",
                    (str(key),)).fetchone()
                return row[0] if row and row[0] is not None else None
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            return None

    def last_resync(self, key):
        """When the step last finished successfully inside a resync, a
        targeted fetch or a full sync – the runs "Fetch now", "Fetch
        again" and "Force full sync" start. The archive check asks this: a
        file still missing after such a run is not coming back on its own."""
        try:
            con = self._connect()
            try:
                row = con.execute(
                    "SELECT MAX(s.started_at) FROM steps s JOIN runs r ON r.id = s.run_id"
                    " WHERE s.key = ? AND s.ok = 1 AND r.job_type IN"
                    " ('job.resync', 'job.full', 'job.archiv.nachholen', 'job.holen')",
                    (str(key),)).fetchone()
                return row[0] if row and row[0] is not None else None
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            return None

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
                grenze = max(1, min(int(limit), 1000))
                runs = [dict(zip(("id", "started_at", "finished_at", "job_type",
                                  "origin", "result", "elements", "semantic",
                                  "workers", "app_version"), row, strict=True))
                        for row in con.execute(
                            "SELECT id, started_at, finished_at, job_type,"
                            " origin, result, elements, semantic, workers,"
                            " app_version FROM runs"
                            " ORDER BY started_at DESC, id DESC LIMIT ?",
                            (grenze,))]
                # The steps of every run in one query, grouped here – not
                # one round-trip per run. The runs are named by the same
                # subquery, not by one bound id each: SQLite builds before
                # 3.32 allow 999 variables, and the cap is above that.
                schritte = {}
                for row in con.execute(
                        "SELECT run_id, key, label, started_at, duration_s,"
                        " new_items, unchanged, excluded, errors, skipped,"
                        " ok, detail FROM steps WHERE run_id IN"
                        " (SELECT id FROM runs ORDER BY started_at DESC, id DESC LIMIT ?)"
                        " ORDER BY run_id, rowid", (grenze,)):
                    schritt = dict(zip(("key", "label", "started_at", "duration_s",
                                        "new", "unchanged", "excluded", "errors",
                                        "skipped", "ok", "extra"), row[1:], strict=True))
                    schritt["extra"] = (json.loads(schritt["extra"])
                                        if schritt["extra"] else None)
                    schritte.setdefault(row[0], []).append(schritt)
                for lauf in runs:
                    lauf["elements"] = (json.loads(lauf["elements"])
                                        if lauf["elements"] else None)
                    lauf["steps"] = schritte.get(lauf["id"], [])
                return runs
            finally:
                con.close()
        except (sqlite3.Error, OSError, ValueError):
            return []
