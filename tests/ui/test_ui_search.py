"""Searching the archive, filter by filter.

The search is the one place every source comes together, and the filters
are where it goes wrong quietly: a pill that does not reach the query, a
narrowing that narrows the wrong way, a list that stops at the first page.
These tests set a filter the way a user does – open the pill, choose the
value – and check what comes back.

The expected counts come from testdata/sources.py, so the data and the
test cannot drift apart.
"""

import pytest

from playwright.sync_api import expect

from testdata import people, sources
from tests.ui.helpers import hit_title, open_app, open_calendar, texts

pytestmark = pytest.mark.ui

EN = texts("en")
PROJECT = people.PROJECT
DANA = people.EXTERNALS[0]

MAILS_IN = {folder: sum(1 for m in sources.MAILS if m["folder"] == folder)
            for folder in {m["folder"] for m in sources.MAILS}}
MAILS_WITH_ATTACHMENT = sum(1 for m in sources.MAILS if m.get("attachments"))
MAILS_FROM_DANA = sum(1 for m in sources.MAILS if m["frm"] is DANA)


# --------------------------------------------------------------------------
# Driving the search the way the interface offers it
# --------------------------------------------------------------------------
def search(page, archive=None, term=""):
    """Type and press the button, and wait for the answer to be drawn.

    Waiting matters more here than anywhere else: the header stays where
    it is between two searches, so anything that only looks at it would
    read the previous result – or the empty moment while "Searching…"
    stands there.
    """
    if archive is not None:
        open_app(page, archive, tab="suche")
    page.fill("#q", term)
    with page.expect_response(lambda r: "/api/v1/search" in r.url):
        page.locator(".suchzeile").get_by_role("button", name=EN["search.go"],
                                               exact=True).click()
    return results(page)


def results(page):
    """The drawn hits – after the page has replaced "Searching…"."""
    page.wait_for_function(
        "t => !document.getElementById('results').textContent.includes(t)",
        arg=EN["search.running"])
    expect(page.locator("#treffer-kopf")).to_be_visible()
    return page.locator("#results .hit")


def tags_of(hit):
    """The marks a hit row carries – source, external, gone, attachment."""
    return hit.locator(".wer .tag").all_inner_texts()


def open_pill(page, key):
    page.click(f"#p-{key}")
    expect(page.locator(f"#po-{key}")).to_be_visible()


def choose(page, key, label):
    """A pill whose popover is a list of values – source, folder, type…"""
    open_pill(page, key)
    page.locator(f"#pl-{key}").get_by_text(label, exact=True).first.click()
    expect(page.locator(f"#p-{key}")).to_have_class("pill on")


def shown(page):
    """What the header says it is showing."""
    return page.locator("#treffer-stand").inner_text()


# --------------------------------------------------------------------------
# The search line
# --------------------------------------------------------------------------
def test_without_ollama_only_the_text_search_is_offered(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    expect(archive_page.locator("#m-text")).to_have_class("on")
    expect(archive_page.locator("#m-aehnlich")).to_be_disabled()
    expect(archive_page.locator("#m-ki")).to_be_disabled()
    expect(archive_page.locator("#modus-fehlt")).to_have_text(EN["search.mode.off"])


def test_a_phrase_in_quotes_finds_less_than_the_words_alone(archive_page, archive):
    lose = search(archive_page, archive, "printer protocol").count()
    genau = search(archive_page, term='"printer mapping"').count()
    assert genau, "the phrase is in the archive and must be found"
    assert genau < lose, (genau, lose)
    # Every hit of the phrase really carries it, in that order.
    for text in archive_page.locator("#results .hit").all_inner_texts():
        assert "printer mapping" in text.lower(), text


def test_a_search_without_words_is_the_filter_alone(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    choose(archive_page, "source", EN["search.source.outlook"])
    search(archive_page)
    hits = archive_page.locator("#results .hit")
    expect(hits).to_have_count(len(sources.MAILS))
    for i in range(hits.count()):
        assert EN["search.source.outlook"] in tags_of(hits.nth(i))


# --------------------------------------------------------------------------
# One filter at a time
# --------------------------------------------------------------------------
def test_the_folder_filter_narrows_to_one_mail_folder(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    choose(archive_page, "source", EN["search.source.outlook"])
    open_pill(archive_page, "folder")
    archive_page.fill("#folder-filter", "Sent")
    archive_page.locator("#pl-folder button", has_text="Sent").first.click()
    search(archive_page)
    expect(archive_page.locator("#results .hit")).to_have_count(MAILS_IN["Sent"])
    expect(archive_page.locator("#pw-folder")).to_contain_text("Sent")


def test_the_date_filter_narrows_to_a_range(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    choose(archive_page, "source", EN["search.source.outlook"])
    open_pill(archive_page, "date")
    archive_page.fill("#f-from", "2026-06-01")
    archive_page.fill("#f-to", "2026-06-30")
    archive_page.keyboard.press("Escape")
    search(archive_page)
    dates = archive_page.locator("#results .hit .wann").all_inner_texts()
    assert dates, "June holds mail"
    for d in dates:
        assert d.startswith("2026-06"), d


def test_the_person_filter_narrows_to_that_person(archive_page, archive):
    alle = search(archive_page, archive, PROJECT).count()
    open_pill(archive_page, "person")
    archive_page.fill("#f-person", DANA[0])
    archive_page.keyboard.press("Escape")
    hits = search(archive_page, term=PROJECT)
    assert 0 < hits.count() < alle
    expect(archive_page.locator("#pw-person")).to_have_text(DANA[0])
    # She is a service provider, so her hits carry the external mark.
    expect(archive_page.locator("#results .hit .tag.extern").first).to_be_visible()


def test_only_mails_with_an_attachment(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    choose(archive_page, "source", EN["search.source.outlook"])
    open_pill(archive_page, "anhang")
    archive_page.check("#f-attach")
    archive_page.keyboard.press("Escape")
    hits = search(archive_page)
    expect(hits).to_have_count(MAILS_WITH_ATTACHMENT)
    # Each of them says so in the list, with the paperclip.
    expect(archive_page.locator("#results .hit .tag.anhang")).to_have_count(
        MAILS_WITH_ATTACHMENT)


def test_the_mail_filter_stands_only_for_mail_and_narrows_by_line(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    # Not a mail search: the line filter would turn every search into one.
    expect(archive_page.locator("#p-mail")).to_be_hidden()
    choose(archive_page, "source", EN["search.source.outlook"])
    open_pill(archive_page, "mail")
    archive_page.fill("#f-mail-from", DANA[1])
    with archive_page.expect_response(lambda r: "/api/v1/search" in r.url):
        archive_page.keyboard.press("Enter")      # searches straight away
    expect(results(archive_page)).to_have_count(MAILS_FROM_DANA)
    expect(archive_page.locator("#pw-mail")).to_contain_text(EN["search.mail.from"])


def test_deleted_only_shows_what_microsoft_no_longer_has(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    open_pill(archive_page, "weg")
    archive_page.check("#f-gone")
    archive_page.keyboard.press("Escape")
    hits = search(archive_page)
    assert hits.count() == 3, "one mail, one file, one task are gone"
    # Every one of them is marked as gone, and the archive still has it.
    expect(archive_page.locator("#results .hit .tag.weg")).to_have_count(3)


def test_the_parties_filter_separates_inside_from_outside(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    choose(archive_page, "source", EN["search.source.outlook"])
    choose(archive_page, "party", EN["search.party.external"])
    aussen = search(archive_page).count()
    choose(archive_page, "party", EN["search.party.internal"])
    innen = search(archive_page).count()
    assert aussen and innen, (aussen, innen)
    assert aussen + innen >= len(sources.MAILS)


# --------------------------------------------------------------------------
# The result: paging, the detail, the conversation
# --------------------------------------------------------------------------
def test_a_long_result_is_paged(archive_page, archive):
    hits = search(archive_page, archive, PROJECT)
    erste = hits.count()
    assert erste == 20, "the page shows twenty at a time"
    expect(archive_page.locator("#treffer-stand")).to_have_text(
        EN["cases.hits.count"].format(n=erste))
    with archive_page.expect_response(lambda r: "/api/v1/search" in r.url):
        archive_page.locator("#pager").get_by_text(EN["search.next"]).click()
    zweite = results(archive_page).count()
    assert 0 < zweite <= 20
    expect(archive_page.locator("#pager").get_by_text(EN["search.back"])).to_be_visible()


def test_a_mails_detail_names_its_lines_and_its_attachment(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    choose(archive_page, "source", EN["search.source.outlook"])
    search(archive_page, term="framework agreement")
    archive_page.locator("#results .hit").first.click()
    fakten = archive_page.locator("#detail-fakten")
    expect(fakten).to_contain_text(EN["search.fact.from"])
    expect(fakten).to_contain_text(EN["search.fact.folder"])
    expect(fakten).to_contain_text(EN["search.fact.attachments"])
    # The attachment is a download of its own, by name.
    chip = fakten.locator("a.chip").first
    expect(chip).to_contain_text("order-list.csv")
    assert "/api/v1/documents/attachments" in chip.get_attribute("href")
    expect(archive_page.locator("#detail .daktionen").get_by_text(
        EN["search.menu.source"])).to_be_visible()


def test_a_heading_inside_a_chat_message_is_not_the_name_of_the_chat(archive_page, archive):
    """Anything pasted into Teams arrives with its markup. A heading in a
    message once ended up glued to the chat's name on every message of
    that file, and missing from the text it was written in."""
    chat = next(c for c in sources.CONVERSATIONS if c["id"] == "chat-group")
    hits = search(archive_page, archive, "executive summary")
    treffer = hits.first
    expect(treffer).to_be_visible()
    assert hit_title(treffer) == chat["title"]
    expect(treffer).to_contain_text("Executive summary")
    # And the detail shows it as the message it is.
    treffer.click()
    expect(archive_page.locator("#detail-text")).to_contain_text("Executive summary")


def test_a_mail_knows_the_conversation_it_belongs_to(archive_page, archive):
    search(archive_page, archive, f"proposal for project {PROJECT}")
    archive_page.locator("#results .hit").first.click()
    fold = archive_page.locator("#detail-verlauf details.verlauf")
    expect(fold).to_be_visible()
    # Three mails went back and forth about the offer.
    expect(fold.locator("summary")).to_contain_text(EN["search.detail.thread"]
                                                    .format(n=3))
    fold.locator("summary").click()
    expect(fold.locator(".vzeile")).to_have_count(3)


# --------------------------------------------------------------------------
# What the search remembers, and the ways into it
# --------------------------------------------------------------------------
def test_a_search_lands_in_the_history_and_runs_again_from_there(archive_page, archive):
    term = "visitor badge workshop"
    search(archive_page, archive, term)
    archive_page.click("#suche-historie")
    fenster = archive_page.locator("#modal")
    expect(fenster).to_contain_text(EN["search.history.title"])
    zeile = fenster.locator("div.hist", has_text=term).first
    expect(zeile).to_be_visible()
    with archive_page.expect_response(lambda r: "/api/v1/search" in r.url):
        zeile.get_by_role("button", name=EN["search.saved.run"]).click()
    expect(fenster).to_be_hidden()
    expect(archive_page.locator("#q")).to_have_value(term)
    expect(results(archive_page).first).to_be_visible()


def test_the_current_search_is_saved_and_comes_back_on_the_next_visit(archive_page, archive):
    name = "Everything about the rollout"
    search(archive_page, archive, f"{PROJECT} rollout")
    archive_page.click("#suche-speichern")
    dialog = archive_page.locator("#modal")
    expect(dialog).to_contain_text(EN["search.save.title"])
    dialog.locator("#speichern-name").fill(name)
    dialog.get_by_role("button", name=EN["search.save.do"], exact=True).click()
    expect(dialog).to_be_hidden()
    # A fresh visit offers it before the first search, and it runs.
    open_app(archive_page, archive, tab="suche")
    anfang = archive_page.locator("#anfang-gespeichert")
    expect(anfang).to_contain_text(name)
    with archive_page.expect_response(lambda r: "/api/v1/search" in r.url):
        anfang.get_by_text(name).first.click()
    expect(results(archive_page).first).to_be_visible()
    expect(archive_page.locator("#q")).to_have_value(f"{PROJECT} rollout")


def test_the_calendar_hands_its_month_over_to_the_search(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    open_calendar(archive_page)
    with archive_page.expect_response(lambda r: "/api/v1/search" in r.url):
        archive_page.click("#kalSuchen")
    results(archive_page)
    # The source comes over set, and the date range with it.
    expect(archive_page.locator("#pw-source")).to_have_text(EN["search.source.kalender"])
    expect(archive_page.locator("#p-date")).to_have_class("pill on")
