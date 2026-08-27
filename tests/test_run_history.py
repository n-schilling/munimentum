"""Tests for run_history.py – recording, reading, retention, resilience."""

import sqlite3
import time

import run_history


def _history(tmp_path):
    return run_history.RunHistory(tmp_path / "runs.db")


def test_records_a_run_with_steps(tmp_path):
    h = _history(tmp_path)
    run_id = h.start_run("job.export", "manual",
                         elements={"outlook": ["mail"], "onedrive": False},
                         semantic=True, workers=4)
    h.record_step(run_id, "outlook", "job.step.outlook", time.time(), 12.5,
                  result={"new": 3, "unchanged": 120, "extra": {"moved": 2}},
                  ok=True)
    h.record_step(run_id, "index", "job.step.index", time.time(), skipped=True)
    h.finish_run(run_id, "done")

    runs = h.list_runs()
    assert len(runs) == 1
    lauf = runs[0]
    assert lauf["job_type"] == "job.export" and lauf["origin"] == "manual"
    assert lauf["result"] == "done" and lauf["semantic"] == 1
    assert lauf["elements"] == {"outlook": ["mail"], "onedrive": False}
    assert lauf["workers"] == 4 and lauf["app_version"]
    assert lauf["finished_at"] >= lauf["started_at"]
    outlook, index = lauf["steps"]
    assert outlook["label"] == "job.step.outlook"
    assert outlook["new"] == 3 and outlook["unchanged"] == 120
    assert outlook["extra"] == {"moved": 2} and outlook["ok"] == 1
    assert index["skipped"] == 1 and index["new"] is None


def test_newest_first_and_limit(tmp_path):
    h = _history(tmp_path)
    for i in range(5):
        run_id = h.start_run(f"job.{i}", "manual")
        h.finish_run(run_id, "done")
    runs = h.list_runs(limit=3)
    assert len(runs) == 3
    assert runs[0]["job_type"] == "job.4"      # newest first


def test_prune_drops_old_runs_and_their_steps(tmp_path, monkeypatch):
    h = _history(tmp_path)
    echte_zeit = time.time
    alt = echte_zeit() - 25 * run_history._MONTH_S
    monkeypatch.setattr(run_history.time, "time", lambda: alt)
    alter_lauf = h.start_run("job.alt", "manual")
    h.record_step(alter_lauf, "outlook", "job.step.outlook", alt, 1.0)
    monkeypatch.setattr(run_history.time, "time", echte_zeit)
    neuer_lauf = h.start_run("job.neu", "manual")
    h.finish_run(neuer_lauf, "done")

    h.prune(24)
    runs = h.list_runs()
    assert [r["job_type"] for r in runs] == ["job.neu"]
    con = sqlite3.connect(tmp_path / "runs.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0
    finally:
        con.close()


def test_prune_keeps_recent_runs(tmp_path):
    h = _history(tmp_path)
    run_id = h.start_run("job.export", "manual")
    h.finish_run(run_id, "done")
    h.prune(1)
    assert len(h.list_runs()) == 1


def test_failures_never_raise(tmp_path):
    # The path is a directory – every SQLite call fails, none may escape.
    kaputt = run_history.RunHistory(tmp_path)
    assert kaputt.start_run("x", "manual") is None
    kaputt.record_step(1, "k", "l", 0.0)
    kaputt.finish_run(1, "done")
    kaputt.prune(24)
    assert kaputt.list_runs() == []


def test_record_step_without_run_is_a_noop(tmp_path):
    h = _history(tmp_path)
    h.record_step(None, "k", "l", 0.0)         # start_run failed upstream
    h.finish_run(None, "done")
    assert h.list_runs() == []
