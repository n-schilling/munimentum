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


# --------------------------------------------------------------------------
# Das gespeicherte Protokoll je Lauf
# --------------------------------------------------------------------------
def test_log_zeilen_roundtrip_je_lauf(tmp_path):
    h = _history(tmp_path)
    lauf = h.start_run("job.export", "manual")
    anderer = h.start_run("job.export", "manual")
    h.log_lines([(lauf, 1000.0, "head", '{"k": "srv.job.start", "v": {}}'),
                 (lauf, 1001.0, "err", '"rohe Zeile"'),
                 (anderer, 1002.0, "info", '"fremd"'),
                 (None, 1003.0, "info", '"App-Zeile ohne Lauf"')])
    zeilen = h.run_log(lauf)
    assert [z["level"] for z in zeilen] == ["head", "err"]
    assert zeilen[0]["text"] == {"k": "srv.job.start", "v": {}}
    assert zeilen[1]["text"] == "rohe Zeile"
    assert h.run_log(anderer) == [{"ts": 1002.0, "level": "info",
                                   "text": "fremd"}]
    h.log_lines([])                                    # leer: kein Krach


def test_log_aufbewahrung_ist_eigenstaendig(tmp_path):
    """Die Zeilen sind der schwere Teil – sie haben ihr eigenes Fenster,
    unabhängig von der Aufbewahrung der Läufe selbst."""
    h = _history(tmp_path)
    lauf = h.start_run("job.export", "manual")
    alt = time.time() - 10 * 86400
    h.log_lines([(lauf, alt, "info", '"alt"'),
                 (lauf, time.time(), "info", '"frisch"')])
    h.prune_log(7)
    assert [z["text"] for z in h.run_log(lauf)] == ["frisch"]
    assert len(h.list_runs()) == 1                     # der Lauf bleibt


def test_kaputte_logzeile_bleibt_roh(tmp_path):
    h = _history(tmp_path)
    lauf = h.start_run("job.export", "manual")
    h.log_lines([(lauf, 1.0, "info", "{kein json")])
    assert h.run_log(lauf) == [{"ts": 1.0, "level": "info",
                                "text": "{kein json"}]


def test_log_schreiben_wirft_nie(tmp_path):
    kaputt = run_history.RunHistory(tmp_path)          # Pfad ist ein Ordner
    kaputt.log_lines([(1, 1.0, "info", '"x"')])
    kaputt.prune_log(7)
    assert kaputt.run_log(1) == []
