"""Browser tests: the real page in a real Chromium against the running app.

The Node tests under tests/ drive page.html's functions against a DOM stub
that knows neither CSS nor the real answers of /api/v1. That keeps them
fast, and it is also how a TypeError once reached the browser while every
test was green. These tests close that gap: the app starts with an empty
data folder, exactly like a first start, and Playwright walks Chromium
through the interface. They assert what a user would see.

Three things hold for every test here, enforced by the `page` fixture:

    - no console error and no uncaught exception in the page,
    - no answer of 500 or worse from the app,
    - no request to anything but the app. The configuration written here
      switches the update check and Ollama off and lets nothing autostart;
      a browser that still asks GitHub, Microsoft or a CDN fails the test.

Two apps run, both in temporary folders: one on an empty archive, which is
what a new user sees, and one on the synthetic archive from testdata/,
which has something in every source. The fixtures are `server`/`page` and
`archive`/`archive_page`.

Without Playwright (`pip install -r requirements-ui.txt`) the whole folder
skips itself; without a browser (`python -m playwright install chromium`,
or an installed Google Chrome) every test skips – unless
MUNIMENTUM_UI_STRICT=1 is set, which the workflow does, and then both are
a failure instead. `pytest -q tests/ui` runs
them alone, the full suite takes them along once Playwright is there, and
.github/workflows/ui.yml runs them on every push. A failed test leaves a
screenshot in tests/ui/output/ (MUNIMENTUM_UI_SHOTS=all keeps one per test).
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# Without a browser these tests cannot run. On a developer's machine that
# is a skip – not everyone needs Playwright installed to work on the app.
# In the workflow it has to be a failure: a green run that tested nothing
# is worse than a red one, and a browser that fails to install would
# otherwise pass unnoticed. .github/workflows/ui.yml sets the variable.
UI_STRICT = os.environ.get("MUNIMENTUM_UI_STRICT") == "1"


def _no_browser(reason, *, module_level=False):
    if UI_STRICT:
        raise RuntimeError(f"MUNIMENTUM_UI_STRICT is set: {reason}")
    pytest.skip(reason, allow_module_level=module_level)


if importlib.util.find_spec("playwright") is None:
    _no_browser("browser tests need Playwright: pip install -r requirements-ui.txt",
                module_level=True)

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from testdata import build as testdata_build  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent / "output"
VIEWPORT = {"width": 1440, "height": 900}

# A first start with every outward door shut. The update check would ask
# GitHub, the AI switch would probe Ollama and open its wizard on a machine
# without it, the MCP autostart would bind the port the maintainer's own
# instance holds. Everything else keeps its default – that is the state a
# new user sees.
CONFIG = {"update_check": False, "ollama_enabled": False, "mcp_autostart": False,
          "notifications": "off", "keep_awake": False}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """The app under test: its address, its folder, the process."""

    def __init__(self, base, folder, process):
        self.base, self.folder, self.process = base, folder, process

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def patch(self, path, body):
        return self.send("PATCH", path, body)

    def post(self, path, body):
        return self.send("POST", path, body)

    def send(self, method, path, body):
        """A write against the app – for setting a test up, never for what
        the test is about: that part is clicked."""
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            roh = r.read().decode("utf-8")
        return json.loads(roh) if roh else {}

    def log_tail(self, lines=40):
        try:
            text = (self.folder / "server.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])


def start_app(folder):
    """The app on a free port against `folder`, up and answering."""
    port = free_port()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MUNIMENTUM_", "OFFICE365_"))}
    env["TZ"] = "UTC"
    log = open(folder / "server.log", "w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(PROJECT / "app.py"), "--port", str(port), "--no-browser",
         "--data-dir", str(folder)],
        cwd=str(PROJECT), env=env, stdout=log, stderr=subprocess.STDOUT)
    srv = Server(f"http://127.0.0.1:{port}", folder, process)
    deadline = time.monotonic() + 60
    while True:
        try:
            srv.get("/api/v1/status")
            return srv, log
        except (urllib.error.URLError, ConnectionError, OSError):
            if process.poll() is not None or time.monotonic() > deadline:
                process.kill()
                log.close()
                raise RuntimeError("the app did not come up:\n" + srv.log_tail()) from None
            time.sleep(0.2)


def stop_app(srv, log):
    srv.process.terminate()
    try:
        srv.process.wait(10)
    except subprocess.TimeoutExpired:
        srv.process.kill()
    log.close()


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """An app on an empty folder – the first start, as a new user sees it."""
    folder = tmp_path_factory.mktemp("munimentum-empty")
    (folder / "app_config.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    srv, log = start_app(folder)
    yield srv
    stop_app(srv, log)


@pytest.fixture(scope="session")
def archive(tmp_path_factory):
    """An app on the synthetic archive (testdata/): every source with
    something in it, indexed. Built once for the whole session – writing
    the files takes a moment, the index run a few seconds."""
    folder = tmp_path_factory.mktemp("munimentum-archive")
    testdata_build.build(folder, flat=True)
    srv, log = start_app(folder)
    yield srv
    stop_app(srv, log)


@pytest.fixture(scope="session")
def browser():
    """Playwright's Chromium, or an installed Google Chrome; neither: skip."""
    pw = sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch()
        except PlaywrightError:
            try:
                b = pw.chromium.launch(channel="chrome")
            except PlaywrightError:
                _no_browser("no browser: python -m playwright install chromium")
        yield b
        b.close()
    finally:
        pw.stop()


class Watch:
    """What the page must not do, collected while a test runs.

    Chromium logs every answer of 400 or worse as a console error of its
    own ("Failed to load resource"); those are judged by status here, not
    by their echo. A 503 from /api/v1 is the vocabulary for "no index yet"
    and a 404 there for "does not exist" – both are states a first start
    has, not faults. Everything of 500 or worse besides, and a 404 on
    anything but the API (an asset the page names and does not ship), is.
    """

    def __init__(self, page, base):
        self.console, self.errors, self.answers, self.outside = [], [], [], []
        self.base = base
        page.on("console", lambda m: self.console.append(m.text)
                if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("response", lambda r: self.answers.append((r.status, r.url)) if self.faulty(r) else None)
        page.on("request", lambda r: self.outside.append(r.url)
                if not r.url.startswith((base, "data:", "blob:", "about:")) else None)

    def faulty(self, r):
        api = r.url.startswith(self.base + "/api/")
        if r.status >= 500:
            return not (api and r.status == 503)
        return r.status == 404 and not api

    def problems(self):
        out = []
        if self.errors:
            out.append("uncaught in the page: " + "; ".join(self.errors))
        if self.console:
            out.append("console errors: " + "; ".join(self.console))
        if self.answers:
            out.append("faulty answers: " + ", ".join(f"{s} {u}" for s, u in self.answers))
        if self.outside:
            out.append("requests outside the app: " + ", ".join(sorted(set(self.outside))))
        return out


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, "report_" + report.when, report)
    return report


def _new_page(browser, base, locale):
    # UTC on both sides: the app runs in it, the synthetic archive is
    # written in it, so a date on the screen is the date in the data –
    # whatever the machine this runs on thinks the time is.
    context = browser.new_context(viewport=VIEWPORT, locale=locale,
                                  timezone_id="UTC", color_scheme="light")
    page = context.new_page()
    page.set_default_timeout(10_000)
    watch = Watch(page, base)
    return context, page, watch


def _finish(request, context, page, watch):
    failed = getattr(request.node, "report_call", None) is not None and request.node.report_call.failed
    if failed or os.environ.get("MUNIMENTUM_UI_SHOTS") == "all":
        OUTPUT.mkdir(exist_ok=True)
        try:
            page.screenshot(path=str(OUTPUT / f"{request.node.name}.png"), full_page=True)
        except PlaywrightError:
            pass
    context.close()
    problems = watch.problems()
    assert not problems, "\n".join(problems)


@pytest.fixture
def page(request, browser, server):
    """A fresh English page on the empty app, watched; the watch is judged
    after the test, so a test cannot forget it."""
    context, pg, watch = _new_page(browser, server.base, "en-US")
    yield pg
    _finish(request, context, pg, watch)


@pytest.fixture
def archive_page(request, browser, archive):
    """The same, on the app that holds the synthetic archive."""
    context, pg, watch = _new_page(browser, archive.base, "en-US")
    yield pg
    _finish(request, context, pg, watch)


@pytest.fixture
def page_for(request, browser, server):
    """The same, with the browser language of one's choosing."""
    opened = []

    def make(locale):
        context, pg, watch = _new_page(browser, server.base, locale)
        opened.append((context, pg, watch))
        return pg
    yield make
    problems = []
    for context, pg, watch in opened:
        try:
            _finish(request, context, pg, watch)
        except AssertionError as e:
            problems.append(str(e))
    assert not problems, "\n".join(problems)
