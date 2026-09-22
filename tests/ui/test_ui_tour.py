"""The tour, walked in the browser.

The help window's tour is the maintained description of what a user sees
where; the Node test only checks that its target ids exist in the markup.
Here the full tour runs in Chromium against the app as a new user finds
it: every card must say what the language file says, every element a step
points at must be on screen, the card must sit inside the window, and the
chapters must follow in order. The search chapter waits for a first run,
so with an empty archive the full tour walks past it – that is asserted,
not worked around.
"""

import pytest

from playwright.sync_api import expect

from tests.ui.helpers import eventually, open_app, texts

pytestmark = pytest.mark.ui

# The chapters the full tour walks while there is no index yet.
CHAPTERS_WITHOUT_INDEX = ["archiv", "quelle", "faelle", "insights", "claude"]

# Steps whose element is not on the page of a first start, so their card
# stands centred: the run button (the archive card shows the three steps
# of a first start instead), the hit list (there before a search only as
# the field), the case chapter's steps inside a case (there is none), and
# the source chapter's closing card, which has no element by design. A new
# entry here means a target vanished from the empty page – look before
# extending the set.
CENTRED_ON_FIRST_START = {
    "tour.archiv.lauf",
    "tour.quelle.speichern",
    "tour.faelle.hinein",
    "tour.faelle.kopf", "tour.faelle.sichten", "tour.faelle.werkzeuge",
    "tour.faelle.ordner", "tour.faelle.automatik", "tour.faelle.export",
}

READ_STEP = """() => {
  const card = document.getElementById('tour-karte');
  const target = TOURSTAND.ziel;
  const r = target && target.getBoundingClientRect ? target.getBoundingClientRect() : null;
  const c = card.getBoundingClientRect();
  return {chapter: TOURSTAND.kap, i: TOURSTAND.i,
          key: TOUR[TOURSTAND.kap][TOURSTAND.i].titel,
          head: card.querySelector('h3').textContent,
          body: card.querySelector('h3').nextElementSibling.textContent,
          part: card.querySelector('.kapitel').textContent,
          centred: document.getElementById('tour').classList.contains('frei'),
          cutout: !document.getElementById('tour-loch').classList.contains('weg'),
          target: r ? {top: r.top, left: r.left, width: r.width, height: r.height} : null,
          card: {top: c.top, left: c.left, width: c.width, height: c.height},
          window: {w: window.innerWidth, h: window.innerHeight}};
}"""


@pytest.fixture
def fresh_tour(server):
    """No chapter seen yet – the state of a first start, whatever an
    earlier test walked."""
    server.patch("/api/v1/config", {"tour_seen": {}})


def open_help(page):
    page.click("#nav-hilfe")
    modal = page.locator("#modal")
    expect(modal).to_be_visible()
    expect(modal.locator("button.hilfe-kapitel")).to_have_count(6)
    return modal


def check_step(step, en):
    """One card: the right words, the element on screen, the card in view."""
    where = f"{step['chapter']} step {step['i'] + 1} ({step['key']})"
    assert step["head"] == en[step["key"]], where
    assert step["body"].strip() and not step["body"].startswith("tour."), where
    w, h = step["window"]["w"], step["window"]["h"]
    card = step["card"]
    assert card["width"] > 0 and card["height"] > 0, where
    assert card["top"] >= 0 and card["left"] >= 0, f"{where}: card outside the window {card}"
    assert card["top"] + card["height"] <= h + 1 and card["left"] + card["width"] <= w + 1, \
        f"{where}: card outside the window {card}"
    if step["target"]:
        t = step["target"]
        assert step["cutout"] and not step["centred"], where
        assert t["height"] > 0 and t["width"] > 0, f"{where}: element without size {t}"
        assert t["top"] < h and t["top"] + t["height"] > 0 and t["left"] < w and t["left"] + t["width"] > 0, \
            f"{where}: element off screen {t}"
    else:
        assert step["centred"] and not step["cutout"], where


def test_the_full_tour_walks_every_chapter_on_a_first_start(page, server, fresh_tour):
    en = texts("en")
    open_app(page, server)
    modal = open_help(page)
    modal.locator("button.act").click()
    tour = page.locator("#tour")
    expect(tour).to_be_visible()
    lengths = page.evaluate("Object.fromEntries(TOUR_REIHE.map(k => [k, TOUR[k].length]))")
    optional = page.evaluate("Object.fromEntries(TOUR_REIHE.map(k => [k, TOUR[k].filter(s => s.optional).length]))")

    walked = []
    for _ in range(80):
        if not tour.is_visible():
            break
        step = page.evaluate(READ_STEP)
        check_step(step, en)
        assert en["tour.part"].split("{")[0] in step["part"], step["part"]
        walked.append(step)
        page.click("#tour-weiter")
    else:
        pytest.fail("the tour did not end")

    chapters = []
    for step in walked:
        if not chapters or chapters[-1] != step["chapter"]:
            chapters.append(step["chapter"])
    assert chapters == CHAPTERS_WITHOUT_INDEX
    # Every step of every chapter, minus the optional ones an empty page
    # cannot show (the run window, the chip of an automatic search).
    for k in CHAPTERS_WITHOUT_INDEX:
        seen = sum(1 for s in walked if s["chapter"] == k)
        assert seen == lengths[k] - optional[k], f"{k}: {seen} of {lengths[k]} steps"
    centred = {s["key"] for s in walked if not s["target"]}
    assert centred == CENTRED_ON_FIRST_START
    # The tour marks its chapters seen – the search chapter, which it
    # walked past, stays unseen and comes back after the first run.
    def seen():
        return server.get("/api/v1/config")["config"]["tour_seen"]
    assert eventually(lambda: len(seen()) == len(CHAPTERS_WITHOUT_INDEX))
    assert seen() == dict.fromkeys(CHAPTERS_WITHOUT_INDEX, True)
    expect(page.locator("#modal")).not_to_be_visible()


def test_one_chapter_from_the_help_window_with_the_keyboard(page, server, fresh_tour):
    en = texts("en")
    open_app(page, server)
    modal = open_help(page)
    rows = modal.locator("button.hilfe-kapitel")
    # No chapter seen: the first one is up next, none is ticked.
    expect(rows.nth(0)).to_contain_text(en["help.next"])
    expect(modal.locator("button.hilfe-kapitel.gesehen")).to_have_count(0)
    rows.nth(4).click()                                   # Insights
    tour = page.locator("#tour")
    expect(tour).to_be_visible()
    card = page.locator("#tour-karte")
    expect(card).to_contain_text(en["tour.chapter.insights"])
    expect(card).to_contain_text(en["tour.counter"].format(i=1, n=5))
    expect(card.locator("h3")).to_have_text(en["tour.insights.zahlen"])
    page.keyboard.press("ArrowRight")
    expect(card).to_contain_text(en["tour.counter"].format(i=2, n=5))
    expect(card.locator("h3")).to_have_text(en["tour.insights.verlauf"])
    page.keyboard.press("ArrowLeft")
    expect(card.locator("h3")).to_have_text(en["tour.insights.zahlen"])
    # The card points at the Insights room: that door is open behind it.
    expect(page.locator("#tab-analytics")).to_be_visible()
    page.keyboard.press("Escape")
    expect(tour).to_be_hidden()
    # Skipped counts as seen: the row is ticked, the next unseen one is up next.
    assert eventually(lambda: server.get("/api/v1/config")["config"]["tour_seen"]) == {"insights": True}
    modal = open_help(page)
    expect(modal.locator("button.hilfe-kapitel.gesehen")).to_have_count(1)
    expect(rows.nth(4)).to_contain_text(en["help.seen"])
    expect(rows.nth(0)).to_contain_text(en["help.next"])
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()


def test_the_search_chapter_waits_for_the_first_run(page, server, fresh_tour):
    en = texts("en")
    open_app(page, server)
    modal = open_help(page)
    modal.locator("button.hilfe-kapitel").nth(2).click()  # Search
    card = page.locator("#tour-karte")
    expect(page.locator("#tour")).to_be_visible()
    expect(card.locator("h3")).to_have_text(en["tour.suche.later"])
    expect(card.locator("button.act")).to_have_text(en["tour.ok"])
    card.locator("button.act").click()
    expect(page.locator("#tour")).to_be_hidden()
    assert server.get("/api/v1/config")["config"]["tour_seen"] == {}
