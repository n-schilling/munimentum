"""The page as a new user finds it: every door, every card, in the browser.

Nothing here changes a setting or starts a run. The `page` fixture judges
what the page must not do (console errors, answers of 500, requests
outside the app) after every test – so a door that renders, but throws
while drawing, is red here even when the Node tests are green.
"""

import pytest

from playwright.sync_api import expect

import version
from tests.ui.helpers import open_app, texts

pytestmark = pytest.mark.ui

DOORS = ["export", "suche", "faelle", "analytics", "einstellungen"]
SETTINGS_CARDS = ["zugang-karte", "quellen-karte", "sched-karte", "ki-karte", "mcp-karte",
                  "ablage-karte", "app-karte", "expert-karte"]
INSIGHT_CARDS = ["ana-zahlen-karte", "ana-verlauf-karte", "ana-check-karte",
                 "ana-archiv-karte", "ana-runs-karte"]


def test_every_door_opens_and_the_others_close(page, server):
    open_app(page, server)
    for door in DOORS:
        page.click(f'nav [data-tab="{door}"]')
        expect(page.locator(f"#tab-{door}")).to_be_visible()
        expect(page.locator(f'nav [data-tab="{door}"]')).to_have_class("on")
        for other in DOORS:
            if other != door:
                expect(page.locator(f"#tab-{other}")).to_be_hidden()


def test_the_five_ways_of_explore_render(page, server):
    open_app(page, server, tab="suche")
    strip = page.locator("#sichten")
    expect(strip).to_be_visible()
    for way in ["kalender", "adressbuch", "dateien", "org", "treffer"]:
        strip.locator(f'[data-sicht="{way}"]').click()
        expect(strip.locator(f'[data-sicht="{way}"]')).to_have_class("sicht on")
    # Search is the way with the field: it is the one that searches.
    expect(page.locator("#q")).to_be_visible()


def test_every_settings_card_is_reached_from_its_entry(page, server):
    open_app(page, server, tab="einstellungen")
    nav = page.locator("#einst-nav")
    for card in SETTINGS_CARDS:
        nav.locator(f'[data-ziel="{card}"]').click()
        expect(page.locator(f"#{card}")).to_be_visible()
        expect(nav.locator(f'[data-ziel="{card}"]')).to_have_class("snav-punkt on")
        expect(nav.locator(".snav-punkt.on")).to_have_count(1)


def test_every_insights_card_is_reached_from_its_entry(page, server):
    open_app(page, server, tab="analytics")
    nav = page.locator("#ana-nav")
    for card in INSIGHT_CARDS:
        nav.locator(f'[data-ziel="{card}"]').click()
        expect(page.locator(f"#{card}")).to_be_visible()
        expect(nav.locator(".snav-punkt.on")).to_have_count(1)


def test_the_app_card_names_the_version_and_a_check_switched_off(page, server):
    en = texts("en")
    open_app(page, server, tab="einstellungen")
    page.locator('#einst-nav [data-ziel="app-karte"]').click()
    expect(page.locator("#update-current")).to_contain_text(
        en["update.current"].format(v=version.VERSION))
    expect(page.locator("#update-current")).to_contain_text(en["update.api"].format(v="v1"))
    expect(page.locator("#update-state")).to_have_text(en["update.off"])
    expect(page.locator("#update-state")).to_have_attribute("title", "")


def test_the_language_follows_the_browser(page_for, server):
    for locale, lang in [("de-DE", "de"), ("en-US", "en"), ("fr-FR", "fr")]:
        page = open_app(page_for(locale), server)
        words = texts(lang)
        expect(page.locator("html")).to_have_attribute("lang", lang)
        expect(page.locator("#nav-hilfe")).to_have_text(words["nav.help"])
        expect(page.locator('nav [data-tab="export"]')).to_have_text(words["nav.export"])


def test_the_help_window_opens_and_escape_closes_it(page, server):
    en = texts("en")
    open_app(page, server)
    page.click("#nav-hilfe")
    modal = page.locator("#modal")
    expect(modal).to_be_visible()
    expect(modal).to_contain_text(en["help.title"])
    expect(modal.locator("button.act")).to_have_text(en["help.all"])
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()


def test_the_organization_says_what_would_fill_it(page, server):
    """No organization yet: the way says how it comes in and leads there."""
    open_app(page, server, tab="suche")
    page.click('#sichten [data-sicht="org"]')
    box = page.locator("#org-box")
    en = texts("en")
    expect(box).to_contain_text(en["org.empty"])
    expect(page.locator("#org-search")).to_be_disabled()
    box.get_by_role("button", name=en["org.empty.go"]).click()
    expect(page.locator("#c-org_enabled")).to_be_attached()
    expect(page.locator("#tab-export")).to_be_visible()
