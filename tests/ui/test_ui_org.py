"""The organization seen through the interface, on the synthetic archive:
from the top down by click, to oneself in one, an earlier version picked
like a month, the changes a version brought, and the hand-over to Search.

The people, the tree and the changes are derived from testdata/sources.py
and testdata/history.py, never written down twice."""

import pytest

from playwright.sync_api import expect

from testdata import history, sources
from tests.ui.helpers import open_app, texts

pytestmark = pytest.mark.ui

EN = texts("en")
USERS = sources.ORG_USERS


def name(uid):
    return USERS[uid]["displayName"]


def open_org(page, archive):
    open_app(page, archive, tab="suche")
    with page.expect_response(lambda r: "/api/v1/organization/people/" in r.url):
        page.click('#sichten [data-sicht="org"]')
    return page.locator("#org-box")


def test_the_top_comes_first_and_a_click_walks_down(archive_page, archive):
    box = open_org(archive_page, archive)
    main = box.locator(".org-card.main")
    expect(main).to_contain_text(name(sources.ORG_TOP))
    kept = len(USERS) - len(sources.ORG_LEFT_OUT)
    expect(archive_page.locator("#org-stats")).to_have_text(f"{kept} people")
    reports = [u for u in USERS.values() if u["manager"] == sources.ORG_TOP
               and u["id"] not in sources.ORG_LEFT_OUT]
    expect(box.locator(".org-grid .org-card")).to_have_count(len(reports))
    box.locator(".org-grid .org-card", has_text=name("org-bob")).click()
    expect(main).to_contain_text(name("org-bob"))
    expect(box.locator(".org-chain .org-card").first).to_contain_text(name(sources.ORG_TOP))
    # Up again through the chain.
    box.locator(".org-chain .org-card").first.click()
    expect(main).to_contain_text(name(sources.ORG_TOP))


def test_to_me_shows_the_chain_above_and_the_people_below(archive_page, archive):
    box = open_org(archive_page, archive)
    archive_page.click("#org-me")
    main = box.locator(".org-card.main")
    expect(main).to_contain_text(name(sources.ORG_ME))
    expect(main).to_contain_text(USERS[sources.ORG_ME]["mail"])
    expect(box.locator(".org-chain .org-card")).to_have_count(3)
    for uid in ("org-kai", "org-lena"):
        expect(box.locator(".org-grid")).to_contain_text(name(uid))
    # Kai joined with this version: his card says so.
    expect(box.locator(".org-grid .org-card", has_text=name("org-kai"))).to_contain_text(
        EN["org.change.joined"])


def test_the_changes_of_a_version_and_an_earlier_one(archive_page, archive):
    box = open_org(archive_page, archive)
    fold = archive_page.locator("#org-changes-fold")
    n = sum(len(ids) for ids in history.ORG_CHANGES.values())
    expect(fold.locator("summary")).to_contain_text(f"{n} changes")
    fold.locator("summary").click()
    moved = fold.locator(".vzeile", has_text=name(sources.ORG_ME))
    expect(moved).to_contain_text(EN["org.change.moved"])
    moved.click()
    expect(box.locator(".org-card.main")).to_contain_text(name(sources.ORG_ME))
    # The version before: Alice under Carla directly, and no fold – there
    # is nothing before it to compare with.
    archive_page.click("#org-version")
    with archive_page.expect_response(lambda r: "/api/v1/organization/people/" in r.url):
        archive_page.locator("#org-versions .zeile").nth(1).click()
    expect(box.locator(".org-chain .org-card")).to_have_count(2)
    expect(archive_page.locator("#org-changes-fold")).to_have_count(0)
    expect(archive_page.locator("#org-version")).not_to_contain_text(EN["org.current"])


def test_the_hand_over_searches_the_person(archive_page, archive):
    box = open_org(archive_page, archive)
    box.locator(".org-grid .org-card", has_text=name("org-bob")).click()
    expect(box.locator(".org-card.main")).to_contain_text(name("org-bob"))
    archive_page.click("#org-search")
    expect(archive_page.locator("#sicht-treffer")).to_be_visible()
    expect(archive_page.locator("#f-person")).to_have_value(name("org-bob"))


def test_a_role_shows_everyone_holding_it(archive_page, archive):
    box = open_org(archive_page, archive)
    archive_page.click("#org-role-pill")
    specialists = [u for u in USERS.values() if u["jobTitle"] == "Specialist"]
    first = archive_page.locator("#org-role-list .zeile").first
    expect(first).to_contain_text("Specialist")
    expect(first).to_contain_text(str(len(specialists)))
    first.click()
    expect(box.locator(".org-grid .org-card")).to_have_count(len(specialists))
    expect(archive_page.locator("#org-role-pill")).to_have_class("pill on")
    # Enter takes every title containing the text.
    archive_page.click("#org-role-pill")
    archive_page.fill("#org-role-q", "head")
    archive_page.keyboard.press("Enter")
    heads = [u for u in USERS.values() if "head" in u["jobTitle"].lower()]
    expect(box.locator(".org-grid .org-card")).to_have_count(len(heads))
    # A card opens the person, and the role steps aside.
    box.locator(".org-grid .org-card", has_text=name("org-bob")).click()
    expect(box.locator(".org-card.main")).to_contain_text(name("org-bob"))
    expect(archive_page.locator("#org-role-x")).to_be_hidden()
