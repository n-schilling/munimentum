"""Working with a case: filling it, ordering it, saying why, closing it.

A case is the one part of the app that writes: items point into the
archive, notes and remarks say why they are there, folders order them, and
closing freezes the lot. These tests walk that from the search side and
from the case side, and check the case afterwards.

Each test makes its own case with its own name – the app under test is
shared for the whole session, so nothing may depend on what ran before.
"""

import pytest

from playwright.sync_api import expect

from testdata import people
from tests.ui.helpers import (fill_case, hit_title, make_case, make_folder,
                              open_app, texts)

pytestmark = pytest.mark.ui

EN = texts("en")
PROJECT = people.PROJECT


# --------------------------------------------------------------------------
# Driving the case door
# --------------------------------------------------------------------------
def open_case(page, archive, name):
    """The cases door with that case opened."""
    open_app(page, archive, tab="faelle")
    liste = page.locator("#faelle-liste")
    expect(liste).to_contain_text(name)
    liste.locator(".fall", has_text=name).first.click()
    expect(page.locator("#fall-kopf")).to_contain_text(name)
    return page.locator("#fall-detail")


def expand_all(page):
    """Every fold open – they start closed, so nothing inside is clickable."""
    page.locator("#fall-werkzeug-ordner").get_by_title(EN["cases.fold.open"]).click()
    expect(page.locator("#fall-inhalt .eintrag").first).to_be_visible()


def fold(page, label):
    """One fold of the folders view, opened – they all start closed."""
    stelle = page.locator("#fall-inhalt details.gruppe", has_text=label).first
    stelle.locator("summary").first.click()
    return stelle


def add_dialog(page):
    dialog = page.locator("#modal")
    expect(dialog).to_be_visible()
    return dialog


def search_and_open_first(page, archive, term):
    """A search, and its first hit chosen – the way into a case."""
    open_app(page, archive, tab="suche")
    page.fill("#q", term)
    with page.expect_response(lambda r: "/api/v1/search" in r.url):
        page.locator(".suchzeile").get_by_role("button", name=EN["search.go"],
                                               exact=True).click()
    page.wait_for_function(
        "t => !document.getElementById('results').textContent.includes(t)",
        arg=EN["search.running"])
    hits = page.locator("#results .hit")
    expect(hits.first).to_be_visible()
    return hits


# --------------------------------------------------------------------------
# Making one, filling it
# --------------------------------------------------------------------------
def test_a_case_is_made_in_the_cases_door(archive_page, archive):
    name, about = f"{PROJECT} handover", "Everything the handover needs."
    open_app(archive_page, archive, tab="faelle")
    archive_page.locator("#tab-faelle .abschnitt-kopf button.act").click()
    dialog = add_dialog(archive_page)
    dialog.locator("#fall-name").fill(name)
    dialog.locator("#fall-beschreibung").fill(about)
    dialog.get_by_role("button", name=EN["cases.new.do"], exact=True).click()
    expect(dialog).to_be_hidden()
    # The case opens straight away, and stands in the list beside it.
    kopf = archive_page.locator("#fall-kopf")
    expect(kopf).to_contain_text(name)
    expect(kopf).to_contain_text(about)
    expect(kopf.locator(".tag.offen")).to_have_text(EN["cases.status.offen"])
    expect(archive_page.locator("#faelle-liste")).to_contain_text(name)
    # A fresh case says what it is missing instead of showing nothing.
    expect(archive_page.locator("#fall-inhalt")).to_contain_text(EN["cases.items.none"])


def test_a_hit_goes_into_the_folder_it_is_meant_for(archive_page, archive):
    name = f"{PROJECT} rollout files"
    case_id = make_case(archive, name)
    make_folder(archive, case_id, "Plans")
    hits = search_and_open_first(archive_page, archive, f"{PROJECT} rollout plan")
    titel = hit_title(hits.first)
    hits.first.click()
    archive_page.locator("#detail").get_by_role(
        "button", name=EN["cases.add.one"]).click()
    dialog = add_dialog(archive_page)
    dialog.locator(f'input[name=fall-wahl][value="{case_id}"]').check()
    dialog.locator("#fall-wahl-ordner").select_option(label="Plans")
    dialog.get_by_role("button", name=EN["cases.add.do"], exact=True).click()
    expect(dialog).to_be_hidden()
    # The hit says where it went, without leaving the result.
    expect(archive_page.locator("#results .hit").first.locator(".im-fall")).to_be_visible()
    # And the case has it, in that folder.
    open_case(archive_page, archive, name)
    ordner = fold(archive_page, "Plans")
    expect(ordner).to_contain_text(titel)


def test_ticked_hits_go_into_a_case_through_the_bar(archive_page, archive):
    name = f"{PROJECT} two of them"
    hits = search_and_open_first(archive_page, archive, PROJECT)
    for i in range(2):
        hits.nth(i).locator("input.wahl").check()
    leiste = archive_page.locator("#auswahl-leiste")
    expect(leiste).to_be_visible()
    expect(leiste.locator("#auswahl-zahl")).to_have_text(EN["cases.selected"].format(n=2))
    leiste.get_by_role("button", name=EN["cases.add.selected"]).click()
    dialog = add_dialog(archive_page)
    dialog.locator("#fall-wahl-neu").fill(name)
    dialog.get_by_role("button", name=EN["cases.add.do"], exact=True).click()
    expect(dialog).to_be_hidden()
    open_case(archive_page, archive, name)
    expect(archive_page.locator("#fall-kopf .meta")).to_contain_text("2")


def test_the_whole_result_goes_into_a_case(archive_page, archive):
    name = f"{PROJECT} printer trouble"
    hits = search_and_open_first(archive_page, archive, '"printer mapping"')
    anzahl = hits.count()
    assert anzahl > 1
    archive_page.locator("#alle-in-fall").click()
    dialog = add_dialog(archive_page)
    dialog.locator("#fall-wahl-neu").fill(name)
    dialog.get_by_role("button", name=EN["cases.add.do"], exact=True).click()
    expect(dialog).to_be_hidden()
    expect(archive_page.locator("#treffer-meldung")).to_contain_text(name)
    open_case(archive_page, archive, name)
    expect(archive_page.locator("#fall-kopf .meta")).to_contain_text(str(anzahl))


# --------------------------------------------------------------------------
# Saying why, ordering, taking back
# --------------------------------------------------------------------------
def test_a_note_goes_into_the_casebook(archive_page, archive):
    name = f"{PROJECT} with a casebook"
    case_id = make_case(archive, name)
    fill_case(archive, case_id, PROJECT, 2)
    text = "Handover agreed for the first of July."
    open_case(archive_page, archive, name)
    buch = fold(archive_page, EN["cases.book"])
    buch.locator("#notiz-neu").fill(text)
    buch.get_by_role("button", name=EN["cases.note.add"], exact=True).click()
    expect(buch.locator(".notiz .text")).to_have_text(text)
    expect(archive_page.locator("#fall-kopf .meta")).to_contain_text(
        EN["cases.facts.notes"].format(n=1))


def test_a_remark_says_why_an_item_is_in_the_case(archive_page, archive):
    name = f"{PROJECT} with a remark"
    case_id = make_case(archive, name)
    eintraege = fill_case(archive, case_id, f"{PROJECT} rollout plan", 1)
    grund = "The draft everything else refers to."
    open_case(archive_page, archive, name)
    expand_all(archive_page)
    zeile = archive_page.locator("#fall-inhalt .eintrag").first
    zeile.locator(f'button[title="{EN["cases.remark.add"]}"]').click()
    dialog = add_dialog(archive_page)
    dialog.locator("#bemerkung-text").fill(grund)
    dialog.get_by_role("button", name=EN["cases.remark.save"], exact=True).click()
    expect(dialog).to_be_hidden()
    zeile = archive_page.locator("#fall-inhalt .eintrag").first
    expect(zeile.locator(".bem")).to_contain_text(grund)
    expect(zeile.locator(".t")).to_contain_text(eintraege[0]["title"][:20])


def test_an_item_leaves_the_case_and_stays_in_the_archive(archive_page, archive):
    name = f"{PROJECT} one too many"
    case_id = make_case(archive, name)
    eintraege = fill_case(archive, case_id, PROJECT, 2)
    titel = eintraege[0]["title"]
    open_case(archive_page, archive, name)
    expand_all(archive_page)
    zeilen = archive_page.locator("#fall-inhalt .eintrag")
    expect(zeilen).to_have_count(2)
    zeilen.locator(f'button[title="{EN["cases.item.remove"]}"]').first.click()
    expect(archive_page.locator("#fall-inhalt .eintrag")).to_have_count(1)
    # The archive never lost it – the search still finds it.
    hits = search_and_open_first(archive_page, archive, titel)
    expect(hits.first).to_contain_text(titel[:20])


def test_the_case_shows_the_same_items_as_a_timeline_and_by_people(archive_page, archive):
    name = f"{PROJECT} three views"
    case_id = make_case(archive, name)
    fill_case(archive, case_id, f"{PROJECT} offer proposal", 3)
    open_case(archive_page, archive, name)
    # Timeline: the items in the order they happened.
    archive_page.locator('#fall-sichten [data-fallsicht="zeit"]').click()
    zeit = archive_page.locator("#fall-zeit")
    expect(zeit).to_be_visible()
    expect(zeit.locator(".zeit")).not_to_have_count(0)
    expect(zeit).to_contain_text("2026")
    # People: everyone the items name, the outside ones marked.
    archive_page.locator('#fall-sichten [data-fallsicht="personen"]').click()
    personen = archive_page.locator("#fall-personen")
    expect(personen).to_be_visible()
    expect(personen).to_contain_text(people.EXTERNALS[0][0])
    expect(personen.locator(".tag.extern").first).to_have_text(
        EN["people.external"])


def test_the_filter_narrows_what_the_case_shows(archive_page, archive):
    name = f"{PROJECT} filtered"
    case_id = make_case(archive, name)
    fill_case(archive, case_id, PROJECT, 6)
    open_case(archive_page, archive, name)
    archive_page.fill("#fall-filter", "budget")
    stand = archive_page.locator("#fall-filter-stand")
    expect(stand).not_to_have_text("")
    zeilen = archive_page.locator("#fall-inhalt .eintrag")
    assert 0 < zeilen.count() < 6
    for text in zeilen.all_inner_texts():
        assert "budget" in text.lower(), text


# --------------------------------------------------------------------------
# Closing and deleting
# --------------------------------------------------------------------------
def test_a_closed_case_is_read_only_and_can_be_deleted(archive_page, archive):
    name = f"{PROJECT} finished"
    case_id = make_case(archive, name)
    fill_case(archive, case_id, PROJECT, 1)
    open_case(archive_page, archive, name)
    archive_page.locator("#fall-fuss").get_by_text(EN["cases.close"], exact=True).click()
    dialog = add_dialog(archive_page)
    dialog.get_by_role("button", name=EN["cases.close"], exact=True).click()
    expect(dialog).to_be_hidden()
    kopf = archive_page.locator("#fall-kopf")
    expect(kopf.locator(".tag.zu")).to_have_text(EN["cases.status.zu"])
    # Nothing can be changed any more: no remove, no new folder, no note.
    expect(archive_page.locator("#fall-ordner-neu")).to_be_hidden()
    expect(archive_page.locator(
        f'#fall-inhalt button[title="{EN["cases.item.remove"]}"]')).to_have_count(0)
    expect(archive_page.locator("#fall-fuss").get_by_text(EN["cases.reopen"])).to_be_visible()
    # And it can be dropped – the question is asked first.
    archive_page.on("dialog", lambda d: d.accept())
    archive_page.locator("#fall-fuss").get_by_text(EN["cases.delete"], exact=True).click()
    expect(archive_page.locator("#faelle-liste")).not_to_contain_text(name)
