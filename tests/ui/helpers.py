"""Small helpers the browser tests share."""

import json
import time
from pathlib import Path
from urllib.parse import quote

PROJECT = Path(__file__).resolve().parents[2]


def open_app(page, server, tab=None):
    """Load the page and wait until the first status arrived – before that
    the page knows neither the configuration nor the archive."""
    page.goto(server.base + "/")
    page.wait_for_function("() => window.S !== null && typeof window.S === 'object' && !!window.S.config")
    if tab:
        page.click(f'nav [data-tab="{tab}"]')
    return page


def open_calendar(page):
    """Switch to the calendar and wait until its data is there.

    The view draws before the answer arrives, and what it shows is what
    the hand-over into the search takes its date range from – so a click
    that is faster than the load would hand over nothing. Waiting for the
    response alone is not enough either: the page fills `events` in the
    handler that follows it.
    """
    with page.expect_response(lambda r: "/api/v1/calendar" in r.url):
        page.click('#sichten [data-sicht="kalender"]')
    page.wait_for_function("() => Array.isArray(window.events) && window.events.length")


def hit_title(hit):
    """The title of a hit row, without the marks that sit beside it – a
    hit that is already in a case carries the case's name in the same
    heading."""
    return hit.locator("h3").inner_text().splitlines()[0].strip()


def texts(lang="en"):
    """The language file – what the page must say, word for word."""
    return json.loads((PROJECT / "lang" / f"{lang}.json").read_text(encoding="utf-8"))


def eventually(fn, timeout=5.0):
    """The page saves in the background: poll until `fn` answers truthy,
    and hand that answer back – the last one when the time is up."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.1)


# --------------------------------------------------------------------------
# Setting a test up through the API.
#
# What a test is ABOUT is clicked; what it merely needs to exist beforehand
# is made here. A case with three items does not get more truthful by being
# clicked together three times, and a test that does so fails for reasons
# that have nothing to do with what it checks.
# --------------------------------------------------------------------------
def hits(server, query, limit=5):
    """The hits of a search, as the page would get them."""
    return server.get(f"/api/v1/search?q={quote(query)}&limit={limit}")["items"]


def make_case(server, name, description=""):
    """A new case; returns its id."""
    return server.post("/api/v1/cases",
                       {"name": name, "description": description})["case"]["id"]


def make_folder(server, case_id, name):
    """A folder in that case; returns its id."""
    fall = server.post(f"/api/v1/cases/{case_id}/folders", {"name": name})["case"]
    return [f for f in fall["folder_list"] if f["name"] == name][-1]["id"]


def fill_case(server, case_id, query, count=2, folder=None):
    """Put the first `count` hits of a search into the case."""
    body = {"items": hits(server, query, count)}
    if folder is not None:
        body["folder"] = folder
    server.post(f"/api/v1/cases/{case_id}/items", body)
    return body["items"]
