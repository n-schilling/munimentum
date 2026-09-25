"""Versions and evidence, seen through the interface.

The synthetic archive has a past (testdata/history.py): a file and a page
with an earlier version, a Teams message that was edited and one that was
deleted, a case whose three items changed since they came in, a file
changed by hand that the archive check found – and a chain of checksums
over all of it. These tests look at each of those the way a user does:
the fold under a hit's content, the version and its difference, the
case's marks and its window, the row in Insights, the card in Settings.
Nothing here starts a run; what a run would do is covered in
tests/test_evidence*.py.
"""

import re
from urllib.parse import quote

import pytest

from playwright.sync_api import expect

from testdata import history, sources
from tests.ui.helpers import make_case, open_app, texts

pytestmark = pytest.mark.ui

EN = texts("en")


def search(page, archive, term):
    open_app(page, archive, tab="suche")
    page.fill("#q", term)
    with page.expect_response(lambda r: "/api/v1/search" in r.url):
        page.keyboard.press("Enter")
    expect(page.locator("#results .hit").first).to_be_visible()


def choose(page, text):
    """The first hit whose row says `text`, opened as the detail – with its
    versions asked for."""
    hit = page.locator("#results .hit", has_text=text).first
    with page.expect_response(lambda r: "/api/v1/documents/versions?" in r.url):
        hit.click()
    return page.locator("#detail")


def fold(page):
    """The versions fold, opened."""
    where = page.locator("#versions-fold")
    expect(where).to_be_visible()
    if not where.evaluate("e => e.open"):
        where.locator("summary").click()
    return where


def test_a_file_shows_its_versions_and_what_changed(archive_page, archive):
    search(archive_page, archive, "rollout plan")
    detail = choose(archive_page, "rollout-plan.md")
    versions = fold(archive_page)
    expect(versions.locator("summary")).to_have_text(
        EN["search.versions.title"].format(n=len([e for e in history.EARLIER if "rollout" in e[1]]) + 1))
    rows = versions.locator(".vzeile")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text(EN["search.versions.current"])
    # The earlier version: its difference to today's is what opens first
    with archive_page.expect_response(lambda r: "/documents/versions/diff" in r.url):
        rows.nth(1).click()
    body = detail.locator(".version-body")
    expect(body.locator("del").first).to_be_visible()
    expect(body.locator("ins", has_text="after the network change")).to_be_visible()
    expect(detail.locator(".version-bar")).to_contain_text(EN["search.versions.showing"].split("{")[0].strip())
    # This version alone reads as it was then
    with archive_page.expect_response(lambda r: "/documents/versions/diff" in r.url):
        detail.get_by_role("button", name=EN["search.versions.this"]).click()
    expect(body).to_contain_text("Draft 1")
    expect(body.locator("del")).to_have_count(0)
    # And back to today's
    detail.get_by_role("button", name=EN["search.versions.back"]).click()
    expect(detail.locator(".version-bar")).to_have_count(0)
    expect(detail.locator("#detail-text")).to_contain_text("rollout")


def test_a_page_version_shows_in_its_frame(archive_page, archive):
    search(archive_page, archive, "Project Ostwind")
    detail = choose(archive_page, "Project Ostwind")
    rows = fold(archive_page).locator(".vzeile")
    expect(rows).to_have_count(2)
    with archive_page.expect_response(lambda r: "/documents/versions/diff" in r.url):
        rows.nth(1).click()
    expect(detail.locator(".version-body ins", has_text="finished")).to_be_visible()
    with archive_page.expect_response(lambda r: "/documents/versions/content" in r.url):
        detail.get_by_role("button", name=EN["search.versions.this"]).click()
    frame = archive_page.frame_locator("#detail .version-body iframe")
    expect(frame.locator("body")).to_contain_text("planned for 3 June")


def test_an_edited_message_keeps_what_it_said(archive_page, archive):
    search(archive_page, archive, "printer mapping fails")
    detail = choose(archive_page, "Test protocol done")
    versions = fold(archive_page)
    expect(versions.locator("summary")).to_have_text(EN["search.versions.title.message"].format(n=2))
    with archive_page.expect_response(lambda r: "/documents/versions/diff" in r.url):
        versions.locator(".vzeile").nth(1).click()
    earlier = sources.EDITED["msg-b5"][0]
    expect(detail.locator(".version-body del", has_text=earlier.split()[-2])).to_be_visible()


def test_a_deleted_message_stays_readable_and_marked(archive_page, archive):
    search(archive_page, archive, "old printers audit")
    hit = archive_page.locator("#results .hit", has_text="Could we leave the two old printers").first
    expect(hit.locator(".tag.weg")).to_be_visible()
    hit.click()
    expect(archive_page.locator("#detail .dkopf .tag.weg")).to_be_visible()
    expect(archive_page.locator("#detail-text")).to_contain_text("Could we leave the two old printers")


def test_a_file_without_an_earlier_version_shows_no_fold(archive_page, archive):
    search(archive_page, archive, "offer template")
    detail = choose(archive_page, "offer-template.txt")
    expect(detail.locator(".fakten")).to_be_visible()
    expect(archive_page.locator("#versions-fold")).to_have_count(0)


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------
def open_case(page, archive, name):
    open_app(page, archive, tab="faelle")
    page.locator("#faelle-liste .fall", has_text=name).first.click()
    expect(page.locator("#fall-kopf")).to_contain_text(name)
    page.locator("#fall-werkzeug-ordner").get_by_title(EN["cases.fold.open"]).click()
    expect(page.locator("#fall-inhalt .eintrag").first).to_be_visible()


def test_a_case_marks_what_changed_since_it_came_in(archive_page, archive):
    open_case(archive_page, archive, history.CASE_NAME)
    marks = archive_page.locator("#fall-inhalt .eintrag .tag.weg", has_text=EN["cases.item.changed"])
    expect(marks).to_have_count(len(history.EARLIER) + 1)
    expect(archive_page.locator("#fall-inhalt .eintrag button", has_text=EN["cases.item.compare"])) \
        .to_have_count(len(history.EARLIER) + 1)


def test_compare_opens_on_the_version_the_item_came_in_with(archive_page, archive):
    # A version shown in Explore first: the detail keeps its own, the
    # window draws into its own place (both once shared one id).
    search(archive_page, archive, "rollout plan")
    choose(archive_page, "rollout-plan.md")
    with archive_page.expect_response(lambda r: "/documents/versions/diff" in r.url):
        fold(archive_page).locator(".vzeile").nth(1).click()
    archive_page.click('nav [data-tab="faelle"]')
    archive_page.locator("#faelle-liste .fall", has_text=history.CASE_NAME).first.click()
    archive_page.locator("#fall-werkzeug-ordner").get_by_title(EN["cases.fold.open"]).click()
    row = archive_page.locator("#fall-inhalt .eintrag", has_text="rollout-plan.md")
    with archive_page.expect_response(lambda r: "/documents/versions/diff" in r.url):
        row.get_by_role("button", name=EN["cases.item.compare"]).click()
    window = archive_page.locator("#modal")
    expect(window).to_contain_text(EN["cases.versions.title"].format(name="rollout-plan.md"))
    chosen = window.locator("#versions-list .vzeile.dies")
    expect(chosen).to_contain_text(EN["cases.versions.pinned"])
    expect(window.locator("#versions-view .version-body del").first).to_be_visible()
    expect(window.locator("#versions-view .version-body")).to_contain_text("wave")
    archive_page.keyboard.press("Escape")


def test_the_export_window_names_the_evidence(archive_page, archive):
    open_case(archive_page, archive, history.CASE_NAME)
    archive_page.locator("#fall-fuss").get_by_role("button", name=EN["cases.export.do"]).click()
    window = archive_page.locator("#modal")
    expect(window).to_contain_text("SHA256SUMS.txt")
    expect(window).to_contain_text(EN["cases.export.line.evidence"])
    expect(window).to_contain_text(EN["cases.export.line.versions"].format(
        folder=EN["cases.export.versions.folder"], n=len(history.EARLIER) + 1))


def test_a_closed_case_says_its_state_was_recorded(archive_page, archive):
    case_id = make_case(archive, "Evidence closed")
    archive.patch(f"/api/v1/cases/{case_id}", {"status": "closed"})
    open_app(archive_page, archive, tab="faelle")
    # Closed cases sit behind their link in the overview
    archive_page.click("#faelle-zu-link")
    archive_page.locator("#faelle-liste .fall", has_text="Evidence closed").first.click()
    line = archive_page.locator("#case-evidence")
    expect(line).to_contain_text(EN["cases.evidence.written"].split("{")[0].strip())
    archive.send("DELETE", f"/api/v1/cases/{case_id}", {})


# --------------------------------------------------------------------------
# Insights and Settings
# --------------------------------------------------------------------------
def test_insights_names_the_file_changed_by_hand(archive_page, archive):
    open_app(archive_page, archive, tab="analytics")
    row = archive_page.locator("#evidence-row")
    expect(row).to_contain_text(EN["ana.evidence"])
    expect(row).to_contain_text(re.compile(r"\b1 changed\b"))
    row.get_by_role("button", name=EN["ana.evidence.show"]).click()
    window = archive_page.locator("#modal")
    folder, rel, _ = history.TAMPERED
    expect(window).to_contain_text(f"{folder}/{rel}")
    outside_folder, outside_rel, _ = history.OUTSIDE
    expect(window).to_contain_text(f"{outside_folder}/{outside_rel}")
    expect(window.get_by_role("button", name=EN["ana.evidence.fetch"].format(source="OneDrive"))).to_be_visible()


def test_the_settings_card_says_how_far_the_chain_reaches(archive_page, archive):
    open_app(archive_page, archive, tab="einstellungen")
    with archive_page.expect_response(lambda r: r.url.endswith("/api/v1/evidence")):
        archive_page.locator('#einst-nav [data-ziel="evidence-card"]').click()
    card = archive_page.locator("#evidence-card")
    expect(card.locator("#c-keep_versions")).to_be_checked()
    expect(card.locator("#c-evidence_timestamp")).not_to_be_checked()
    expect(card.locator("#c-versions_max_mb")).to_have_value("50")
    expect(card.locator("#evidence-state")).to_contain_text(
        EN["settings.evidence.chain.state"].split("{")[1].split("}")[1].strip())
    expect(card.locator("#evidence-kept")).to_contain_text(f"{len(history.KEPT)} files")


# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------
def checksums(page):
    """The checksums fold at the bottom of the detail, opened."""
    where = page.locator("#checksums-fold")
    expect(where).to_be_visible()
    where.locator("summary").click()
    return where


def test_a_files_checksums_wait_in_a_closed_fold(archive_page, archive):
    search(archive_page, archive, "rollout plan")
    with archive_page.expect_response(lambda r: "/documents/facts" in r.url):
        choose(archive_page, "rollout-plan.md")
    # Quiet: nothing of it among the facts, the fold closed
    expect(archive_page.locator("#detail-fakten")).not_to_contain_text("SHA-256")
    expect(archive_page.locator("#checksums-fold summary")).to_have_text(EN["search.checksums"])
    assert not archive_page.locator("#checksums-fold").evaluate("e => e.open")
    fold = checksums(archive_page)
    expect(fold.locator("code.checksum").first).to_have_text(re.compile(r"^[0-9a-f]{64}$"))
    expect(fold).to_contain_text("quickXorHash")
    expect(fold).to_contain_text(EN["search.checksum.match"])


def test_a_file_microsoft_disagrees_with_is_marked(archive_page, archive):
    search(archive_page, archive, "risk log")
    with archive_page.expect_response(lambda r: "/documents/facts" in r.url):
        choose(archive_page, "risk-log.csv")
    # The one thing the closed fold says
    expect(archive_page.locator("#checksums-fold summary")).to_contain_text(EN["search.checksum.mismatch"])
    open_app(archive_page, archive, tab="analytics")
    row = archive_page.locator("#evidence-row")
    mismatched = sum(1 for v in history.MICROSOFT.values() if v == "mismatch")
    expect(row).to_contain_text(EN["ana.evidence.microsoft"].format(n=mismatched))
    row.get_by_role("button", name=EN["ana.evidence.show"]).click()
    window = archive_page.locator("#modal")
    expect(window).to_contain_text("risk-log.csv")
    # One click from the finding to the file
    with archive_page.expect_response(lambda r: "/documents/facts" in r.url):
        window.get_by_role("link", name=re.compile("risk-log.csv")).first.click()
    expect(archive_page.locator("#detail .dtitel")).to_have_text("risk-log.csv")
    # handed over to Search: the file chosen among the hits of its name
    expect(archive_page.locator("#results .hit.on")).to_contain_text("risk-log.csv")
    expect(archive_page.locator("#q")).to_have_value("risk-log.csv")
    expect(archive_page.locator("#checksums-fold summary")).to_contain_text(EN["search.checksum.mismatch"])


def test_a_mail_shows_its_own_checksum_only(archive_page, archive):
    search(archive_page, archive, "rollout plan second draft")
    with archive_page.expect_response(lambda r: "/documents/facts" in r.url):
        archive_page.locator("#results .hit", has_text="Ostwind rollout plan, second draft").first.click()
    fold = checksums(archive_page)
    expect(fold).to_contain_text("SHA-256")
    expect(fold).not_to_contain_text("quickXorHash")


# --------------------------------------------------------------------------
# A link to an item – copied from its detail, and pinned to a version
# --------------------------------------------------------------------------
def _plan(archive):
    uid = next(h["uid"] for h in archive.get("/api/v1/search?q=rollout%20plan&limit=20")["items"]
               if h["uid"].endswith("rollout-plan.md:0"))
    doc = archive.get(f"/api/v1/documents?uid={quote(uid)}")
    items = archive.get(f"/api/v1/documents/versions?uid={quote(uid)}")["items"]
    return doc["key"], doc["title"], items


def test_the_detail_copies_a_link_to_the_version_it_shows(archive_page, archive):
    archive_page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    key, _title, items = _plan(archive)
    current = next(v["sha256"] for v in items if v["current"])
    search(archive_page, archive, "rollout plan")
    choose(archive_page, "rollout-plan.md")
    button = archive_page.locator("#detail-link")
    expect(button).to_have_attribute("title", EN["search.detail.link"])
    button.click()
    expect(button).to_have_attribute("title", EN["search.detail.link.copied"])
    copied = archive_page.evaluate("() => navigator.clipboard.readText()")
    assert copied == f"{archive.base}/#item={quote(key, safe='')}&sha={current}"


def test_a_link_to_an_earlier_version_opens_that_version(archive_page, archive):
    key, title, items = _plan(archive)
    earlier = next(v["sha256"] for v in items if not v["current"])
    archive_page.goto(f"{archive.base}/#item={quote(key, safe='')}&sha={earlier}")
    detail = archive_page.locator("#detail")
    expect(detail.locator(".dtitel")).to_be_visible()
    expect(detail.locator("#detail-pin")).to_have_text(EN["search.pin.changed"])
    expect(detail.locator(".version-bar")).to_be_visible()
    # The current version: nothing to say.
    current = next(v["sha256"] for v in items if v["current"])
    archive_page.goto(f"{archive.base}/#item={quote(key, safe='')}&sha={current}")
    expect(detail.locator(".dtitel")).to_have_text(title)
    expect(detail.locator("#versions-fold")).to_be_visible()
    expect(detail.locator("#detail-pin")).to_have_text("")
    # A version the archive never held.
    archive_page.goto(f"{archive.base}/#item={quote(key, safe='')}&sha={'0' * 64}")
    expect(detail.locator("#detail-pin")).to_have_text(EN["search.pin.missing"])
    expect(detail.locator(".version-bar")).to_have_count(0)
