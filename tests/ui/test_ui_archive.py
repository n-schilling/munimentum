"""The interface on an archive that holds something.

The other browser tests run against an empty folder and check that every
door opens. These run against the synthetic archive (testdata/) and check
what a user does with one: search across the sources, open a hit, walk the
calendar, the contacts and the files, read the key figures, and put a hit
into a case.

Expected numbers are never written down twice – they are derived from the
module that generated the archive, so adding a mail to testdata/sources.py
does not make a test here wrong.
"""

import pytest

from playwright.sync_api import expect

from testdata import people, sources
from tests.ui.helpers import hit_title, open_app, texts

pytestmark = pytest.mark.ui

PROJECT = people.PROJECT
EN = texts("en")


def search_for(page, archive, term):
    """Type a search and press the button, as a user would."""
    open_app(page, archive, tab="suche")
    page.fill("#q", term)
    page.locator("#sicht-treffer").get_by_role("button", name=EN["search.go"],
                                               exact=True).click()
    expect(page.locator("#treffer-kopf")).to_be_visible()
    return page.locator("#results .hit")


def test_a_search_finds_the_project_across_the_sources(archive_page, archive):
    hits = search_for(archive_page, archive, PROJECT)
    expect(hits.first).to_be_visible()
    # The project is talked about in mail, in chats, in the calendar and on
    # a page: a search that finds only one kind would mean the index read
    # only one source.
    kinds = set(archive_page.locator("#results .hit .wer .tag").all_inner_texts())
    assert len(kinds) >= 3, kinds


def test_a_chosen_hit_shows_the_facts_of_its_kind(archive_page, archive):
    hits = search_for(archive_page, archive, "budget approved")
    hits.first.click()
    detail = archive_page.locator("#detail")
    expect(detail).to_be_visible()
    # A mail is known by who wrote it, when, and where it lies.
    expect(detail.locator("#detail-fakten")).to_contain_text(people.COLLEAGUES[1][0])
    expect(detail.locator("#detail-fakten")).to_contain_text("2026")
    expect(detail.get_by_role("button", name=EN["cases.add.one"])).to_be_visible()
    expect(detail.locator("#detail-text")).to_contain_text(PROJECT)


def test_the_calendar_shows_what_the_export_holds(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    archive_page.click('#sichten [data-sicht="kalender"]')
    sicht = archive_page.locator("#sicht-kalender")
    expect(sicht).to_be_visible()
    # The line above the grid counts the appointments in the export.
    expect(sicht).to_contain_text(str(len(sources.EVENTS)))
    expect(archive_page.locator("#kalBox")).to_be_visible()


def test_the_contacts_hold_everyone_the_archive_knows(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    archive_page.click('#sichten [data-sicht="adressbuch"]')
    liste = archive_page.locator("#kbBox")
    expect(liste).to_be_visible()
    for name, _mail in [*people.COLLEAGUES, *people.EXTERNALS]:
        expect(liste).to_contain_text(name)


def test_the_file_browser_lists_every_mirror_and_walks_into_one(archive_page, archive):
    open_app(archive_page, archive, tab="suche")
    archive_page.click('#sichten [data-sicht="dateien"]')
    liste = archive_page.locator("#dateien-liste")
    expect(liste).to_be_visible()
    for name in ("OneDrive", "SharePoint", "Teams", "Planner"):
        expect(liste).to_contain_text(name)
    liste.get_by_text("OneDrive", exact=False).first.click()
    # One level in: the mirror's own top folder, and no file lost on the way.
    expect(liste).to_contain_text("Dateien")
    liste.get_by_text("Dateien", exact=False).first.click()
    expect(liste).to_contain_text("Documents")


def test_the_key_figures_count_what_was_written(archive_page, archive):
    open_app(archive_page, archive, tab="analytics")
    karte = archive_page.locator("#ana-zahlen-karte")
    expect(karte).to_be_visible()
    messages = len(sources.MAILS) + sum(len(c["messages"]) for c in sources.CONVERSATIONS)
    expect(karte).to_contain_text(str(messages))
    expect(karte).to_contain_text(str(len(sources.PAGES)))
    expect(karte).to_contain_text(str(len(sources.ONENOTE_PAGES)))
    expect(karte).to_contain_text("2026")


def test_a_hit_goes_into_a_new_case(archive_page, archive):
    name = f"{PROJECT} rollout"
    hits = search_for(archive_page, archive, f"{PROJECT} rollout plan")
    titel = hit_title(hits.first)
    hits.first.click()
    archive_page.locator("#detail").get_by_role(
        "button", name=EN["cases.add.one"]).click()
    dialog = archive_page.locator("#modal")
    expect(dialog).to_be_visible()
    # The dialog can make the case on the spot – type a name, add.
    dialog.locator("#fall-wahl-neu").fill(name)
    dialog.get_by_role("button", name=EN["cases.add.do"], exact=True).click()
    expect(dialog).to_be_hidden()
    # The third door now has the case, and it holds that one item.
    archive_page.click('nav [data-tab="faelle"]')
    leiste = archive_page.locator("#faelle-leiste")
    expect(leiste).to_contain_text(name)
    leiste.get_by_text(name, exact=False).first.click()
    expect(archive_page.locator("#fall-kopf")).to_contain_text(name)
    # The item is in there by its own title, filed under its source.
    expect(archive_page.locator("#fall-inhalt")).to_contain_text(titel)
