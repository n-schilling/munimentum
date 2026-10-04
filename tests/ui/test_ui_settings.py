"""The settings as the browser shows them, on the synthetic archive.

The SharePoint addresses as a tree: what testdata/build.py configures –
one library whole, one only through two folders in it – has to appear
as the design says: the whole library with its cadence and *Sync now*,
the other one a placeholder with neither, its folders paced each on
their own.
"""

import pytest

from playwright.sync_api import expect

from testdata import sources
from tests.ui.helpers import open_app, texts

pytestmark = pytest.mark.ui


def _row(page, text):
    return page.locator("#sp-tree .addr-node").filter(has_text=text).first


def test_a_library_named_only_through_folders_is_a_placeholder(archive_page, archive):
    en = texts("en")
    page = open_app(archive_page, archive, tab="einstellungen")
    page.locator('#einst-nav [data-ziel="quellen-karte"]').click()
    block = page.locator("#q-sharepoint")
    block.locator("summary").first.click()
    expect(page.locator("#sp-tree")).to_be_visible()

    whole = _row(page, sources.SHAREPOINT_LIBRARY)
    expect(whole).to_contain_text(en["sp.whole"])
    expect(whole.locator("select")).to_have_count(1)
    expect(whole.get_by_role("button", name=en["cadence.sync_now"])).to_have_count(1)

    placeholder = page.locator("#sp-tree .addr-placeholder")
    expect(placeholder).to_have_count(1)
    expect(placeholder).to_contain_text(sources.SHAREPOINT_PROJECTS)
    expect(placeholder).to_contain_text(en["sp.lib.placeholder"])
    expect(placeholder.locator("select")).to_have_count(0)
    expect(placeholder.get_by_role("button", name=en["cadence.sync_now"])).to_have_count(0)

    # Its folders carry their own cadence – the one build.CONFIG paces weekly
    # says so, the other one runs with every run – and nothing offers to
    # take it "from the library".
    folders = page.locator("#sp-tree .addr-node.addr-sub")
    expect(folders).to_have_count(len(sources.SHAREPOINT_PROJECT_FOLDERS))
    for folder, cadence in zip(sources.SHAREPOINT_PROJECT_FOLDERS, ("always", "weekly"), strict=True):
        row = _row(page, folder)
        expect(row.locator("select")).to_have_value(cadence)
        expect(row.locator("select option").first).to_have_text(en["cadence.always"])
        expect(row.get_by_role("button", name=en["cadence.sync_now"])).to_have_count(1)


def test_the_pages_list_controls_reach_the_server(archive_page, archive):
    """A row of the flat lists is built as markup: its cadence select has to
    arrive at the server – and come back the same after the refill."""
    page = open_app(archive_page, archive, tab="einstellungen")
    page.locator('#einst-nav [data-ziel="quellen-karte"]').click()
    page.locator("#q-sharepoint summary").first.click()
    row = page.locator("#pg-list .addr-node").first
    select = row.locator("select")
    expect(select).to_have_value("always")
    with page.expect_response(lambda r: "/api/v1/config" in r.url and r.request.method == "PATCH"):
        select.select_option("weekly")
    expect(page.locator("#pg-list .addr-node").first.locator("select")).to_have_value("weekly")
    with page.expect_response(lambda r: "/api/v1/config" in r.url and r.request.method == "PATCH"):
        page.locator("#pg-list .addr-node").first.locator("select").select_option("always")
    expect(page.locator("#pg-list .addr-node").first.locator("select")).to_have_value("always")
