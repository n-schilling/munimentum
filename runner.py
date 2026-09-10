"""Run steps as subprocesses and keep the MCP server alive – no app globals.

Extracted from app.py: JobRunner executes one step after the other and feeds
the log ring buffer plus the runs.db; McpProcess starts and stops the MCP
server. Neither reads a path of its own – working directory, resource dir and
the MCP launch plan are injected by app.py, which keeps this module import-
free of the storage layout and easy to test.
"""

import os
import re
import json
import time
import threading
import subprocess
from collections import deque
from datetime import datetime
from pathlib import Path

import i18n
import notify
import progress


# --------------------------------------------------------------------------
# Run jobs: one after the other, output live into the buffer
# --------------------------------------------------------------------------
class JobRunner:
    """Runs a sequence of steps as subprocesses, one at a time.

    One job after the other is intent, not a limitation: export and index
    write into the same folders, and Graph throttles per mailbox anyway.
    The output lands line by line in a ring buffer that the interface
    polls.
    """

    MAX_LINES = 4000

    def __init__(self, history=None, cwd=None, res=None):
        # Injected surroundings instead of app-module globals: the working
        # directory for subprocesses (a path or a zero-arg callable, resolved
        # per step) and the resource dir for translated notifications.
        self.cwd = cwd
        self.res = res
        self.lock = threading.Lock()
        self.lines = deque(maxlen=self.MAX_LINES)
        self.seq = 0
        self.thread = None
        self.proc = None
        self.cancelled = False
        self.job = None            # {"label", "steps", "step", "started"}
        self.last = None           # {"label", "ok", "finished", "detail"}
        self.token_expired = False
        # Sum of newly written pieces across all export steps of this run.
        # None means "no export step spoke up" – then nothing is skipped,
        # because not knowing is no reason.
        self.neu = None
        # Run history (run_history.RunHistory) – optional so tests can run
        # without a database; every call is guarded on its side too.
        self.history = history
        self._origin = "manual"
        self._context = {}
        self._step_result = None   # last @@RESULT@@ dict of the current step
        self._run_id = None        # tags log lines with the current run
        self._log_puffer = []      # lines waiting for runs.db, see _log_flush

    # -- Log ---------------------------------------------------------------
    def log(self, text, level="info"):
        """Raw log line – just as the export scripts emit it.

        Besides the ring buffer for the interface, every line goes into the
        log table of runs.db – lines of a run carry its id, the health
        section later shows them per run. Writes are batched; app lines
        outside a run are rare and go out immediately."""
        with self.lock:
            self.seq += 1
            self.lines.append({"n": self.seq, "level": level,
                               "t": datetime.now().strftime("%H:%M:%S"),
                               "text": text})
            self._log_puffer.append((self._run_id, time.time(), level,
                                     json.dumps(text, ensure_ascii=False)))
            voll = self._run_id is None or len(self._log_puffer) >= 50
        if voll:
            self._log_flush()

    def _log_flush(self):
        with self.lock:
            puffer, self._log_puffer = self._log_puffer, []
        if puffer and self.history:
            self.history.log_lines(puffer)

    def logk(self, key, level="info", **vars):
        """Log line as a text key; translated only when displayed.

        Separate from log() so nothing has to be guessed: a script line can
        look like a key. And translating only at display time means a
        language switch also converts the existing log, instead of freezing
        it in the language of back then.
        """
        self.log({"k": key, "v": vars}, level)

    def log_since(self, since):
        with self.lock:
            return [ln for ln in self.lines if ln["n"] > since], self.seq

    # -- State -------------------------------------------------------------
    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def snapshot(self):
        job = dict(self.job) if self.job else None
        return {"busy": self.busy, "job": job, "last": self.last,
                "token_expired": self.token_expired, "seq": self.seq}

    # -- Control -----------------------------------------------------------
    def start(self, steps, label, origin="manual", context=None):
        if self.busy:
            return False
        if not steps:
            return False
        self.cancelled = False
        self.token_expired = False
        self._origin = origin
        self._context = context or {}
        # None = no export step has reported yet (ignorance is no reason to
        # skip); 0 when the app already knows that every requested export
        # was dropped before it could run.
        self.neu = 0 if self._context.get("nichts_neues") else None
        # log_seq: the log cursor before the run's first line – the page
        # shows the run window with this run's lines only, not with what
        # the app logged before it.
        self.job = {"label": label, "steps": [s["label"] for s in steps],
                    "step": steps[0]["label"], "index": 0, "progress": None,
                    "started": datetime.now().isoformat(timespec="seconds"),
                    "log_seq": self.seq}
        self.thread = threading.Thread(target=self._run, args=(steps, label), daemon=True)
        self.thread.start()
        return True

    def cancel(self):
        self.cancelled = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            self.logk("srv.job.cancel", "warn")
            return True
        return False

    def _run(self, steps, label):
        hist = self.history
        run_id = hist.start_run(
            label, self._origin,
            elements=self._context.get("elements"),
            semantic=self._context.get("semantic"),
            workers=self._context.get("workers")) if hist else None
        self._run_id = run_id
        # Labels as a nested message ({"k": …}): mtext() in the browser
        # then translates them – as a bare string the key itself would end
        # up in the log ("job.step.outlook").
        self.logk("srv.job.start", "head", label={"k": label, "v": {}})
        # What was selected, right under the heading – the same wording the
        # run history uses, rendered by the page from the structured value.
        elemente = self._context.get("elements") or {}
        if any(elemente.values()):
            self.logk("srv.job.elements", "info", elements=elemente)
        ok = True
        detail = ""
        for i, step in enumerate(steps):
            if self.cancelled:
                ok, detail = False, {"k": "srv.job.cancelled", "v": {}}
                break
            self.job = {**self.job, "step": step["label"], "index": i,
                        "progress": None}      # every step counts up from zero
            # The heading comes first in every case – a skipped step is
            # still a step of this run, and its reason belongs under it. It
            # says what STARTS ("Microsoft Planner export starts"); the short
            # label serves the result, finish and skip lines and the table.
            self.logk("srv.job.step", "head",
                      step={"k": step.get("start") or step["label"], "v": {}})
            grund = step.get("auslassen")
            if grund is None and self._erspart(step):
                grund = {"k": "srv.job.skipped",
                         "v": {"step": {"k": step["label"], "v": {}}}}
            if grund is not None:
                self.log(grund, "info")
                if hist:
                    hist.record_step(run_id, step["key"], step["label"],
                                     time.time(), skipped=True)
                continue
            begonnen = time.time()
            self._step_result = None
            code = self._exec(step)
            if hist:
                hist.record_step(run_id, step["key"], step["label"], begonnen,
                                 duration_s=time.time() - begonnen,
                                 result=self._step_result, ok=(code == 0))
            if self._step_result is not None:
                # The interface builds the translated summary from the
                # event – the scripts no longer print prose of their own.
                self.logk("srv.job.result", "info", ergebnis=self._step_result)
            if code != 0:
                ok = False
                schritt = {"k": step["label"], "v": {}}
                detail = ({"k": "srv.job.aborted", "v": {"step": schritt}}
                          if self.cancelled else
                          {"k": "srv.job.exitcode",
                           "v": {"step": schritt, "code": code}})
                self.logk("srv.job.stepfail", "err", detail=detail)
                break
            self.logk("srv.job.stepdone", "ok", step={"k": step["label"], "v": {}})
        if ok:
            self.logk("srv.job.done", "ok", label={"k": label, "v": {}})
        art = ("done" if ok else "aborted" if self.cancelled
               else "token_expired" if self.token_expired else "error")
        self._run_id = None
        self._log_flush()          # the rest of the run, before anyone reads
        if hist:
            hist.finish_run(run_id, art)
            monate = self._context.get("retention_months")
            if monate:
                hist.prune(monate)
            hist.prune_log(self._context.get("log_retention_days") or 14)
        self._notify_user(art, label)
        self.last = {"label": label, "ok": ok, "detail": detail,
                     "finished": datetime.now().isoformat(timespec="seconds")}
        self.job = None
        self.proc = None

    def _notify_user(self, art, label):
        """One system notification per run – or none: the mode decides.

        "errors" (the default) keeps quiet on success; "all" also reports
        finished runs – the scheduler case, where no tab is open. A cancelled
        run is never reported: the user did that themselves.
        """
        try:
            mode = self._context.get("notify") or "errors"
            if (mode == "off" or art == "aborted"
                    or (art == "done" and mode != "all")):
                return
            key = {"done": "srv.notify.done",
                   "token_expired": "srv.notify.token"}.get(art, "srv.notify.failed")
            texte = i18n.strings(self._context.get("lang") or i18n.FALLBACK,
                                 self.res)
            text = (texte.get(key) or key).replace(
                "{label}", texte.get(label) or str(label))
            notify.send("Munimentum", text)
        except Exception:
            pass                # a missed notification must never break a run

    def _erspart(self, step):
        """May this step be skipped because the export brought nothing new?

        From practice: a run with only "Contacts" reported "Newly exported:
        0" and then indexed the same corpus for two minutes.

        Three conditions, each needed on its own:
          * The step is meant for this at all (index, calendar).
          * An export step ran that spoke up, and it brought nothing – or
            the app knew up front that every requested export was dropped
            by its gate (context "nichts_neues"). Without either, work
            proceeds – not knowing is no reason.
          * The result already exists. Otherwise, after the first run with
            an unchanged corpus, there would never be an index.
        """
        if not step.get("nur_bei_neuem") or self.neu is None or self.neu > 0:
            return False
        ziel = step.get("ziel")
        return bool(ziel and Path(ziel).exists())

    def _exec(self, step):
        env = {**os.environ, **step.get("env", {})}
        wd = self.cwd() if callable(self.cwd) else self.cwd
        try:
            self.proc = subprocess.Popen(
                step["argv"], cwd=wd, env=env, bufsize=0,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
        except OSError as e:
            self.logk("srv.job.spawnfail", "err", error=str(e))
            return -1
        for line in _stream_lines(self.proc.stdout):
            stand = progress.lies(line)
            if stand is not None:
                # Numbers for the bar – in the log they would be mere noise.
                if self.job:
                    self.job = {**self.job, "progress": stand}
                continue
            fazit = progress.lies_ergebnis(line)
            if fazit is not None:
                self._step_result = fazit
                # Only export steps count for the skip logic: index and
                # calendar report too, but do not change the corpus.
                if step.get("corpus"):
                    self.neu = (self.neu or 0) + fazit["new"]
                continue
            kaputt = progress.lies_fehler(line)
            if kaputt is not None:
                # Structured instead of prose patterns: a regex over the
                # scripts' message text used to sit here.
                if kaputt["error"] == "token_expired":
                    self.token_expired = True
                    self.logk("srv.job.token", "err")
                continue
            meldung = progress.lies_event(line)
            if meldung is not None:
                # The scripts speak in text keys; translation happens at
                # display time – as with the app's own lines.
                self.log({"k": meldung["k"], "v": meldung.get("v", {})},
                         meldung.get("level", "info"))
                continue
            self.log(line)
        return self.proc.wait()


def _stream_lines(stream):
    """Lines from a process stream, even with progress via \\r.

    The scripts overwrite progress lines with "\\r" instead of finishing
    them with "\\n" (rag_index.py: "… 500/12000 eingebettet"). readline()
    would wait on that until the end of the step, so the stream is read raw
    and split on both characters.
    """
    buf = ""
    while True:
        try:
            chunk = stream.read(4096)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        buf += chunk.decode("utf-8", errors="replace")
        parts = re.split(r"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            if p.strip():
                yield p.rstrip()
    if buf.strip():
        yield buf.rstrip()


class McpProcess:
    """Starts/stops mcp_server.py and collects its output in the log."""

    def __init__(self, jobs, befehl=None, client_config=None):
        self.jobs = jobs
        # befehl(cfg) -> {"argv", "cwd", "env", "db"}: how to launch the
        # server and which index file must exist first. client_config(cfg,
        # port) -> the ready-made client snippets shown in the settings.
        self.befehl = befehl
        self.client_config = client_config
        self.proc = None
        self.port = None
        self.error = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self, cfg):
        port = self.port or cfg["mcp_port"]
        return {"running": self.running, "port": port,
                "url": f"http://127.0.0.1:{port}/mcp",
                "error": self.error,
                "config": self.client_config(cfg, port)}

    def start(self, cfg):
        if self.running:
            return True, {"k": "srv.mcp.running", "v": {}}
        if not cfg.get("mcp_enabled", True):
            self.error = {"k": "srv.mcp.disabled", "v": {}}
            return False, self.error
        plan = self.befehl(cfg)
        if not plan["db"].exists():
            self.error = {"k": "srv.mcp.noindex", "v": {}}
            return False, self.error
        try:
            self.proc = subprocess.Popen(
                plan["argv"], cwd=plan["cwd"],
                env={**os.environ, **plan["env"]},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0)
        except OSError as e:
            self.error = {"k": "srv.mcp.spawnfail", "v": {"error": str(e)}}
            return False, self.error
        self.port = cfg["mcp_port"]
        self.error = None
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self.jobs.logk("srv.mcp.started", "ok", port=self.port)
        return True, {"k": "srv.mcp.startok", "v": {}}

    def _pump(self, proc):
        for line in _stream_lines(proc.stdout):
            self.jobs.log(f"[MCP] {line}")
        code = proc.wait()
        if proc is self.proc and code not in (0, -15):
            self.error = {"k": "srv.mcp.exit", "v": {"code": code}}
            self.jobs.logk("srv.mcp.exit", "err", code=code)

    def stop(self):
        if not self.running:
            return False
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.jobs.logk("srv.mcp.stopped", "warn")
        return True
